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
        found = [f"machine:{machine['name']}", f"role:{machine['role']}"]
        caller = (caller or "").strip().lower()
        if caller and registry.bound_machine(caller) == machine["name"]:
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

    def check(self, op: str, arguments: dict, machine: dict, caller: str) -> str | None:
        """None when the operation may run for this machine, else the ERROR
        line to return. Binds an unbound acting instance to the machine."""
        principals = self.principals(machine, caller)
        if self._orchestrator_op(op, arguments) and not _matches(principals,
                                                                 self.orchestrators):
            return (f"ERROR: '{op}' is an orchestrator operation — not allowed for "
                    f"{', '.join(principals)} (hub policy: orchestrators).")
        acting = self._acting(op, arguments)
        if acting:
            refused = registry.bind(acting, machine["name"])
            if refused:
                return refused
            if acting not in principals:
                principals.append(acting)
        if op == "push_task":
            target, error = store.resolve_agent(str(arguments.get("target_agent", "")))
            if error is None:
                allowed = [senders for pattern, senders in self.accepts_from.items()
                           if fnmatch.fnmatchcase(target, pattern)]
                if allowed and not any(_matches(principals, senders) for senders in allowed):
                    return (f"ERROR: agent '{target}' does not accept work from "
                            f"{', '.join(principals)} (hub policy: accepts_from).")
        if op == "rename_agent":
            new = str(arguments.get("new", "")).strip().lower()
            owner = registry.bound_machine(new)
            if owner and owner != machine["name"]:
                return f"ERROR: agent '{new}' belongs to machine '{owner}'."
        return None

    def after(self, op: str, arguments: dict, result) -> None:
        """Follow-ups of a successful operation."""
        if op == "rename_agent" and isinstance(result, str) and result.startswith("OK"):
            registry.rename_binding(str(arguments["old"]), str(arguments["new"]))
