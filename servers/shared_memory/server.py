"""MCP server of the inter-agent bus — thin wrapper over store.py.

Protocol: MCP over stdio (SDK >= 2.0). Every agent client spawns its own
instance of this server; sharing happens through the common SQLite database
(see store.py, IAB_DB env var). This file holds NO state logic.
"""
import json

from mcp.server.mcpserver import Context, MCPServer

try:  # installed package: `iab` maps to this directory (pyproject.toml)
    from . import store
    from .access import bus
except ImportError:  # bare script: python servers/shared_memory/server.py
    import store
    from access import bus

mcp = MCPServer("InterAgentBus")

# Resolved once: the launch directory of a stdio server never changes, and
# the identity (harness@workspace, model echo) derives from it and the env.
_IDENTITY: dict | None = None


def _identity() -> dict:
    global _IDENTITY
    if _IDENTITY is None:
        _IDENTITY = store.resolve_identity()
    return _IDENTITY


def _touch() -> None:
    """Register on first sight and refresh the caller's presence on every
    tool call when the environment names the harness. Transport-only:
    failures are swallowed so the actual tool call always proceeds."""
    try:
        identity = _identity()
        if identity.get("agent_name"):
            bus.touch_identity(identity)
    except Exception:
        pass


@mcp.tool()
def whoami(ctx: Context) -> str:
    """Identity of the connected client as one instance `harness@workspace`: harness from IAB_AGENT_NAME (authoritative; legacy ORCHESTRATOR_AGENT_NAME honored), workspace from IAB_WORKSPACE or derived from the launch directory (alias table, enclosing git repository, folder name — `workspace_source` says which), model echoed from IAB_MODEL; without a harness, the MCP clientInfo sent at the handshake (to match against the roster's client_hints). Also returns the resolved bus database (path, how it was chosen, project key), so a rendezvous mismatch between clients is diagnosable in one call."""
    _touch()
    rendezvous = json.loads(bus.db_info())
    identity = _identity()
    if identity.get("agent_name"):
        return json.dumps({**identity, **rendezvous})
    params = ctx.session.client_params
    if params is None or params.client_info is None:
        return json.dumps({**identity, **rendezvous})
    info = params.client_info
    return json.dumps(
        {
            **identity,
            "source": "client_info",
            "name": info.name,
            "title": info.title,
            "version": info.version,
            **rendezvous,
        }
    )


@mcp.tool()
def register_agent(name: str, description: str = "", cwd: str = "", model: str = "") -> str:
    """Declare an agent instance (creates its queue): `name` is `harness@workspace` (or a bare harness), `cwd` where it works, `model` the model carrying it right now. The cast comes from the project's roster, never from the plugin."""
    _touch()
    return bus.register_agent(name, description, cwd, model or None)


@mcp.tool()
def rename_agent(old: str, new: str) -> str:
    """Move an identity to a new name — queue, results, notifications, cursors, presence and journal follow in one transaction (migration of a legacy bare name to `harness@workspace`); refused when `new` exists."""
    _touch()
    return bus.rename_agent(old, new)


@mcp.tool()
def push_task(
    target_agent: str, task_id: str, payload: str, priority: int = 1, max_attempts: int = 3,
    notify: bool = True,
) -> str:
    """Queue a task for a registered agent (higher priority is served first; after max_attempts expired leases the task is dead-lettered); unless notify=False a directed notification wakes the target's watcher."""
    _touch()
    return bus.push_task(target_agent, task_id, payload, priority, max_attempts, notify)


@mcp.tool()
def claim_task(agent_name: str, lease_seconds: int = 900, task_id: str = "") -> str:
    """Claim the next task of one's queue under lease (re-offered if not settled before the deadline) — or one specific queued task of one's own queue when task_id is given."""
    _touch()
    return bus.claim_task(agent_name, lease_seconds, task_id or None)


@mcp.tool()
def publish_result(
    agent_name: str, task_id: str, result_content: str, attempt: int = 0, force: bool = False
) -> str:
    """Publish a task's result (settles the task); pass the attempt number received at claim so a stale worker cannot overwrite a re-offered task's result (force overrides every check)."""
    _touch()
    return bus.publish_result(agent_name, task_id, result_content, attempt or None, force)


@mcp.tool()
def cancel_task(task_id: str) -> str:
    """Cancel a task that is not settled yet (terminal status `cancelled`)."""
    _touch()
    return bus.cancel_task(task_id)


@mcp.tool()
def requeue_task(task_id: str) -> str:
    """Re-offer a claimed, dead or cancelled task, re-arming three more attempts; a done task stays done."""
    _touch()
    return bus.requeue_task(task_id)


@mcp.tool()
def extend_lease(task_id: str, lease_seconds: int = 900) -> str:
    """Renew the lease of a claimed task (deadline = now + lease_seconds) so long work is not re-offered mid-flight."""
    _touch()
    return bus.extend_lease(task_id, lease_seconds)


@mcp.tool()
def read_result(task_id: str) -> str:
    """Read the published result of a task."""
    _touch()
    return bus.read_result(task_id)


@mcp.tool()
def get_system_state() -> str:
    """Global state: registered agents, queue lengths (queued/claimed), settled tasks, and presence statuses."""
    _touch()
    return bus.get_system_state()


@mcp.tool()
def get_events(task_id: str = "", agent: str = "", limit: int = 100) -> str:
    """Chronological transition history of the bus (push/claim/expire/publish/announce), filterable by task and/or agent."""
    _touch()
    return bus.get_events(task_id or None, agent or None, limit)


@mcp.tool()
def heartbeat(agent: str, ttl_seconds: int = 120, capabilities: str = "", model: str = "") -> str:
    """Post or refresh a presence heartbeat with a capability card and the model carrying the instance (absent capabilities keep the existing card, absent model keeps the stored one); the agent must be registered."""
    _touch()
    return bus.heartbeat(agent, ttl_seconds, capabilities or None, model or None)


@mcp.tool()
def list_presence() -> str:
    """List every known agent instance (harness, workspace, cwd, declared model) with its computed presence status and capability card."""
    _touch()
    return bus.list_presence()


@mcp.tool()
def announce(author: str, topic: str, message: str) -> str:
    """Append a message to the global channel (topic must match ^[a-z0-9-]{1,64}$; message ≤ 16 KiB)."""
    _touch()
    return bus.announce(author, topic, message)


@mcp.tool()
def read_channel(agent: str = "", since_seq: int = -1, topic: str = "", limit: int = 100) -> str:
    """Read channel entries. Use since_seq >= 0 for a pure read, or agent (since_seq omitted) to read from that agent's cursor and advance it (topic filter only allowed with since_seq)."""
    _touch()
    # -1 is the "not given" sentinel: the core distinguishes None (cursor
    # read) from 0 (pure read from the beginning).
    return bus.read_channel(
        agent or None, None if since_seq < 0 else since_seq, topic or None, limit
    )


@mcp.tool()
def notify(
    author: str, target_agent: str, message: str, reply_to: int = 0, wake: bool = False
) -> str:
    """Send a directed signal to one agent instance (point-to-point; a bare harness is accepted only when it designates a single instance); the recipient picks it up with poll(). Author and target must be registered. reply_to=<broadcast id> answers a broadcast; wake=True asks a supervisor to launch an asleep recipient if its roster allows."""
    _touch()
    return bus.notify(author, target_agent, message, reply_to or None, wake)


@mcp.tool()
def broadcast(
    author: str, message: str, scope: str = "*", alive_only: bool = False, wake: bool = False
) -> str:
    """Ask or tell several agents at once — scope `*`, `harness@*`, `*@workspace`, `model:<glob>` or a comma-separated list — as one channel entry plus one notification per recipient (read with poll(), answered with notify(reply_to=broadcast_id)); returns who is alive to see it now and who is asleep. A question or a signal, never a work order: work goes through push_task."""
    _touch()
    return bus.broadcast(author, message, scope, alive_only, wake)


@mcp.tool()
def replies(broadcast_id: int) -> str:
    """Answers received to a broadcast (notifications sent with reply_to=broadcast_id), oldest first; a pure read."""
    _touch()
    return bus.replies(broadcast_id)


@mcp.tool()
def poll(agent: str, limit: int = 100) -> str:
    """Read an agent's own directed notifications and advance its cursor (at-least-once)."""
    _touch()
    return bus.poll(agent, limit)


@mcp.tool()
def wait_signal(
    agent: str, kinds: str = "any", timeout_seconds: float = 300.0, interval_seconds: float = 2.0
) -> str:
    """Block until a task is queued for the instance and/or an unread notification (broadcasts included) waits in its mailbox — kinds: task, notification or any — and return the snapshot with signalled true/false; no cursor moves. BLOCKING call for a background-capable runtime: the wake-up of an open session."""
    _touch()
    return bus.wait_signal(agent, kinds, timeout_seconds, interval_seconds)


@mcp.tool()
def pending_signals(agent: str) -> str:
    """What waits for an instance right now — queued task ids, unread notifications, broadcast ids — without moving any cursor."""
    _touch()
    return bus.pending_signals(agent)


@mcp.tool()
def wait_task(agent: str, timeout_seconds: float = 300.0, interval_seconds: float = 2.0) -> str:
    """Block until the agent's queue holds a queued task and return the task_ids (JSON list, "[]" on timeout) — BLOCKING call, use from a background-capable runtime."""
    _touch()
    return bus.wait_for_task(agent, timeout_seconds, interval_seconds)


def main() -> None:
    """Entry point of the `iab-server` console script (pyproject.toml):
    lets an installed runtime register without any repository path."""
    # A launched client is a present client: mark the agent as seen as soon
    # as the server starts, without waiting for its first tool call (the
    # per-call piggyback then keeps the presence fresh).
    _touch()
    mcp.run()


if __name__ == "__main__":
    main()
