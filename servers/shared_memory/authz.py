"""Authorization of the hub — who may do what, from the identity a machine
certificate proves (the study's § 7, phase 9d).

Locally, whoever writes the database drives the agents. Through a hub,
every request comes from an authenticated machine, which makes three
rules enforceable:

  1. BINDING — an agent instance belongs to the first machine that acts
     as it; no other machine may then claim its queue, publish, beat its
     heart, read its mailbox or take its wake lock in its name.
  2. ORCHESTRATOR operations — cancel, requeue, rename, a forced publish,
     a broadcast to a pattern — need a principal listed in
     `policy.orchestrators` (default: the admin machines).
  3. ACCEPTS_FROM — who may push work to whom: the main defence against a
     prompt injection travelling from one machine's agents to another's.

The policy is configuration of the hub (`policy` in hub.json), never
code — mechanism, not cast:

    "policy": {
      "orchestrators": ["role:admin", "claude@homelab"],
      "accepts_from": {"kimi@*": ["claude@*", "machine:yggdrasil"]}
    }

Principals of a request: `machine:<name>`, `role:<role>` and, when the
client names itself (X-IAB-Caller) with an instance bound to that same
machine, that instance. Patterns use shell wildcards.
"""
from __future__ import annotations

import fnmatch

try:  # installed package (`iab` maps to this directory)
    from . import registry, store
except ImportError:  # bare script usage
    import registry
    import store

# Operation → the argument naming the instance the caller acts as.
ACTING = {
    "register_agent": "name", "rename_agent": "old",
    "claim_task": "agent_name", "publish_result": "agent_name",
    "heartbeat": "agent", "touch_presence": "agent",
    "announce": "author", "notify": "author", "broadcast": "author",
    "poll": "agent",
    "acquire_wake_lock": "agent", "renew_wake_lock": "agent",
    "release_wake_lock": "agent", "record_wake": "agent",
}
# Operations whose core function uses the name AS WRITTEN (no address
# resolution): the literal name is what must be bound — resolving it here
# could skip the check on an ambiguous bare harness the core accepts.
_LITERAL = {"register_agent", "rename_agent", "publish_result", "touch_presence",
            "record_wake", "touch_identity", "acquire_wake_lock", "renew_wake_lock",
            "release_wake_lock"}
DEFAULT_ORCHESTRATORS = ("role:admin",)


def _is_pattern_scope(scope) -> bool:
    if not isinstance(scope, str):
        return False
    text = scope.strip().lower()
    return text == "*" or "@*" in text or text.startswith("*@") or text.startswith("model:")


def _matches(principals: list[str], patterns) -> bool:
    return any(fnmatch.fnmatchcase(p, str(pattern).lower())
               for p in principals for pattern in patterns)


class Authz:
    def __init__(self, policy: dict | None = None) -> None:
        policy = policy or {}
        self.orchestrators = tuple(policy.get("orchestrators") or DEFAULT_ORCHESTRATORS)
        accepts = policy.get("accepts_from") or {}
        self.accepts_from = {str(k).lower(): list(v) for k, v in accepts.items()}

    # -- principals --------------------------------------------------------

    def principals(self, machine: dict, caller: str) -> list[str]:
        """`machine:` and `role:` are proven by the certificate. An instance
        name counts only when an ADMIN bound it to this machine (`iab
        bind`): a binding made on first use proves first come, not trust,
        so it never grants policy power."""
        found = [f"machine:{machine['name']}", f"role:{machine['role']}"]
        caller = (caller or "").strip().lower()
        if caller and registry.pinned_machine(caller) == machine["name"]:
            found.append(caller)
        return found

    # -- the three rules ---------------------------------------------------

    def _acting(self, op: str, arguments: dict) -> str | None:
        """The instance an operation acts as, resolved like the core does;
        None when the operation acts as nobody (or names no instance the
        core would accept — the core then refuses it on its own)."""
        if op == "touch_identity":
            identity = arguments.get("identity") or {}
            return str(identity.get("agent_name") or "").strip().lower() or None
        if op == "read_channel":
            if arguments.get("agent") is None or arguments.get("since_seq") is not None:
                return None
            name = arguments["agent"]
        elif op == "extend_lease":
            return store.task_agent(str(arguments.get("task_id", "")))
        else:
            key = ACTING.get(op)
            if key is None or not arguments.get(key):
                return None
            name = arguments[key]
        name = str(name)
        if op in _LITERAL:
            return name.strip().lower() or None
        # The core resolves this address itself and refuses what it cannot
        # resolve: an unresolvable one acts as nobody here.
        resolved, error = store.resolve_agent(name)
        return resolved if error is None else None

    def _orchestrator_op(self, op: str, arguments: dict) -> bool:
        if op in ("cancel_task", "requeue_task", "rename_agent"):
            return True
        if op == "publish_result" and arguments.get("force"):
            return True
        return op == "broadcast" and _is_pattern_scope(arguments.get("scope", "*"))

    def _recipients(self, op: str, arguments: dict) -> list[str]:
        """Who receives work or a message from this operation (resolved like
        the core; an address it would refuse yields nobody here)."""
        if op == "push_task":
            target, error = store.resolve_agent(str(arguments.get("target_agent", "")))
            return [target] if error is None else []
        if op == "notify":
            target, error = store.resolve_agent(str(arguments.get("target_agent", "")))
            return [target] if error is None else []
        if op == "broadcast":
            names, error = store.broadcast_recipients(arguments.get("scope", "*"))
            return names if error is None else []
        return []

    def check(self, op: str, arguments: dict, machine: dict,
              caller: str) -> tuple[str | None, str | None]:
        """(None, reserved) when the operation may run for this machine —
        `reserved` names an instance bound to it by this very check, to
        release if the operation fails (see after) — else (ERROR line,
        None). Every rule is checked BEFORE anything is bound."""
        principals = self.principals(machine, caller)
        if self._orchestrator_op(op, arguments) and not _matches(principals,
                                                                 self.orchestrators):
            return (f"ERROR: '{op}' is an orchestrator operation — not allowed for "
                    f"{', '.join(principals)} (hub policy: orchestrators)."), None
        acting = self._acting(op, arguments)
        owner = registry.bound_machine(acting) if acting else None
        if owner and owner != machine["name"]:
            return (f"ERROR: agent '{acting}' belongs to machine '{owner}' — refused from "
                    f"'{machine['name']}' (an admin can move it: "
                    f"`iab bind {acting} {machine['name']}`)."), None
        if acting and registry.pinned_machine(acting) == machine["name"] \
                and acting not in principals:
            principals.append(acting)
        # accepts_from applies to every recipient of work OR of a message: a
        # message reaches an agent's context too, and a wake launches it.
        for target in self._recipients(op, arguments):
            allowed = [senders for pattern, senders in self.accepts_from.items()
                       if fnmatch.fnmatchcase(target, pattern)]
            if allowed and not any(_matches(principals, senders) for senders in allowed):
                return (f"ERROR: agent '{target}' does not accept "
                        f"{'work' if op == 'push_task' else 'messages'} from "
                        f"{', '.join(principals)} (hub policy: accepts_from)."), None
        if op == "rename_agent":
            new = str(arguments.get("new", "")).strip().lower()
            new_owner = registry.bound_machine(new)
            if new_owner and new_owner != machine["name"]:
                return f"ERROR: agent '{new}' belongs to machine '{new_owner}'.", None
        if not acting:
            return None, None
        refused, reserved = registry.reserve(acting, machine["name"])
        if refused:
            return refused, None
        return None, acting if reserved else None

    def after(self, op: str, arguments: dict, result, machine: dict,
              reserved: str | None) -> None:
        """Follow-ups: a binding made for an operation that then failed is
        released (no squat left behind by a refused call); a rename moves
        the binding with the name."""
        failed = isinstance(result, str) and result.startswith("ERROR")
        if failed and reserved:
            registry.release(reserved, machine["name"])
        if op == "rename_agent" and not failed:
            registry.rename_binding(str(arguments["old"]), str(arguments["new"]))
