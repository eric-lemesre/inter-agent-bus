"""MCP server of the inter-agent bus — thin wrapper over store.py.

Protocol: MCP over stdio (SDK >= 2.0). Every agent client spawns its own
instance of this server; sharing happens through the common SQLite database
(see store.py, IAB_DB env var). This file holds NO state logic.
"""
import json
import os

from mcp.server.mcpserver import Context, MCPServer

try:  # installed package: `iab` maps to this directory (pyproject.toml)
    from . import store
except ImportError:  # bare script: python servers/shared_memory/server.py
    import store

mcp = MCPServer("InterAgentBus")


def _touch() -> None:
    """Refresh the caller's presence on every tool call when an agent name
    is set in the environment. Transport-only: failures are swallowed so the
    actual tool call always proceeds."""
    name = os.environ.get("IAB_AGENT_NAME") or os.environ.get("ORCHESTRATOR_AGENT_NAME")
    if name:
        try:
            store.touch_presence(name)
        except Exception:
            pass


@mcp.tool()
def whoami(ctx: Context) -> str:
    """Identity hints of the connected client, so a worker can find its own roster entry: IAB_AGENT_NAME env var when the launcher set one (authoritative; legacy ORCHESTRATOR_AGENT_NAME honored), otherwise the MCP clientInfo sent at the handshake (to match against the roster's client_hints). Also returns the resolved bus database (path, how it was chosen, project key), so a rendezvous mismatch between clients is diagnosable in one call."""
    _touch()
    bus = json.loads(store.db_info())
    forced = os.environ.get("IAB_AGENT_NAME") or os.environ.get("ORCHESTRATOR_AGENT_NAME")
    if forced:
        return json.dumps({"source": "env", "agent_name": forced, **bus})
    params = ctx.session.client_params
    if params is None or params.client_info is None:
        return json.dumps({"source": "none", **bus})
    info = params.client_info
    return json.dumps(
        {
            "source": "client_info",
            "name": info.name,
            "title": info.title,
            "version": info.version,
            **bus,
        }
    )


@mcp.tool()
def register_agent(name: str, description: str = "") -> str:
    """Declare an agent (creates its queue). The cast comes from the project's roster, never from the plugin."""
    _touch()
    return store.register_agent(name, description)


@mcp.tool()
def push_task(
    target_agent: str, task_id: str, payload: str, priority: int = 1, max_attempts: int = 3,
    notify: bool = True,
) -> str:
    """Queue a task for a registered agent (higher priority is served first; after max_attempts expired leases the task is dead-lettered); unless notify=False a directed notification wakes the target's watcher."""
    _touch()
    return store.push_task(target_agent, task_id, payload, priority, max_attempts, notify)


@mcp.tool()
def claim_task(agent_name: str, lease_seconds: int = 900, task_id: str = "") -> str:
    """Claim the next task of one's queue under lease (re-offered if not settled before the deadline) — or one specific queued task of one's own queue when task_id is given."""
    _touch()
    return store.claim_task(agent_name, lease_seconds, task_id or None)


@mcp.tool()
def publish_result(
    agent_name: str, task_id: str, result_content: str, attempt: int = 0, force: bool = False
) -> str:
    """Publish a task's result (settles the task); pass the attempt number received at claim so a stale worker cannot overwrite a re-offered task's result (force overrides every check)."""
    _touch()
    return store.publish_result(agent_name, task_id, result_content, attempt or None, force)


@mcp.tool()
def cancel_task(task_id: str) -> str:
    """Cancel a task that is not settled yet (terminal status `cancelled`)."""
    _touch()
    return store.cancel_task(task_id)


@mcp.tool()
def requeue_task(task_id: str) -> str:
    """Re-offer a claimed, dead or cancelled task, re-arming three more attempts; a done task stays done."""
    _touch()
    return store.requeue_task(task_id)


@mcp.tool()
def extend_lease(task_id: str, lease_seconds: int = 900) -> str:
    """Renew the lease of a claimed task (deadline = now + lease_seconds) so long work is not re-offered mid-flight."""
    _touch()
    return store.extend_lease(task_id, lease_seconds)


@mcp.tool()
def read_result(task_id: str) -> str:
    """Read the published result of a task."""
    _touch()
    return store.read_result(task_id)


@mcp.tool()
def get_system_state() -> str:
    """Global state: registered agents, queue lengths (queued/claimed), settled tasks, and presence statuses."""
    _touch()
    return store.get_system_state()


@mcp.tool()
def get_events(task_id: str = "", agent: str = "", limit: int = 100) -> str:
    """Chronological transition history of the bus (push/claim/expire/publish/announce), filterable by task and/or agent."""
    _touch()
    return store.get_events(task_id or None, agent or None, limit)


@mcp.tool()
def heartbeat(agent: str, ttl_seconds: int = 120, capabilities: str = "") -> str:
    """Post or refresh a presence heartbeat with a capability card (absent capabilities keep the existing card)."""
    _touch()
    return store.heartbeat(agent, ttl_seconds, capabilities or None)


@mcp.tool()
def list_presence() -> str:
    """List every known agent with its computed presence status and capability card."""
    _touch()
    return store.list_presence()


@mcp.tool()
def announce(author: str, topic: str, message: str) -> str:
    """Append a message to the global channel (topic must match ^[a-z0-9-]{1,64}$; message ≤ 16 KiB)."""
    _touch()
    return store.announce(author, topic, message)


@mcp.tool()
def read_channel(agent: str = "", since_seq: int = -1, topic: str = "", limit: int = 100) -> str:
    """Read channel entries. Use since_seq >= 0 for a pure read, or agent (since_seq omitted) to read from that agent's cursor and advance it (topic filter only allowed with since_seq)."""
    _touch()
    # -1 is the "not given" sentinel: the core distinguishes None (cursor
    # read) from 0 (pure read from the beginning).
    return store.read_channel(
        agent or None, None if since_seq < 0 else since_seq, topic or None, limit
    )


@mcp.tool()
def notify(author: str, target_agent: str, message: str) -> str:
    """Send a directed signal to one agent (point-to-point); the recipient picks it up with poll()."""
    _touch()
    return store.notify(author, target_agent, message)


@mcp.tool()
def poll(agent: str, limit: int = 100) -> str:
    """Read an agent's own directed notifications and advance its cursor (at-least-once)."""
    _touch()
    return store.poll(agent, limit)


@mcp.tool()
def wait_task(agent: str, timeout_seconds: float = 300.0, interval_seconds: float = 2.0) -> str:
    """Block until the agent's queue holds a queued task and return the task_ids (JSON list, "[]" on timeout) — BLOCKING call, use from a background-capable runtime."""
    _touch()
    return store.wait_for_task(agent, timeout_seconds, interval_seconds)


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
