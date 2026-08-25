"""MCP server of the inter-agent bus — thin wrapper over store.py.

Protocol: MCP over stdio (SDK >= 2.0). Every agent client spawns its own
instance of this server; sharing happens through the common SQLite database
(see store.py, IAB_DB env var). This file holds NO state logic.
"""
import json
import os

from mcp.server.mcpserver import Context, MCPServer

import store

mcp = MCPServer("InterAgentBus")


@mcp.tool()
def whoami(ctx: Context) -> str:
    """Identity hints of the connected client, so a worker can find its own roster entry: IAB_AGENT_NAME env var when the launcher set one (authoritative; legacy ORCHESTRATOR_AGENT_NAME honored), otherwise the MCP clientInfo sent at the handshake (to match against the roster's client_hints). Also returns the resolved bus database (path, how it was chosen, project key), so a rendezvous mismatch between clients is diagnosable in one call."""
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
    return store.register_agent(name, description)


@mcp.tool()
def push_task(
    target_agent: str, task_id: str, payload: str, priority: int = 1, max_attempts: int = 3
) -> str:
    """Queue a task for a registered agent (higher priority is served first; after max_attempts expired leases the task is dead-lettered)."""
    return store.push_task(target_agent, task_id, payload, priority, max_attempts)


@mcp.tool()
def claim_task(agent_name: str, lease_seconds: int = 900, task_id: str = "") -> str:
    """Claim the next task of one's queue under lease (re-offered if not settled before the deadline) — or one specific queued task of one's own queue when task_id is given."""
    return store.claim_task(agent_name, lease_seconds, task_id or None)


@mcp.tool()
def publish_result(
    agent_name: str, task_id: str, result_content: str, attempt: int = 0, force: bool = False
) -> str:
    """Publish a task's result (settles the task); pass the attempt number received at claim so a stale worker cannot overwrite a re-offered task's result (force overrides every check)."""
    return store.publish_result(agent_name, task_id, result_content, attempt or None, force)


@mcp.tool()
def cancel_task(task_id: str) -> str:
    """Cancel a task that is not settled yet (terminal status `cancelled`)."""
    return store.cancel_task(task_id)


@mcp.tool()
def requeue_task(task_id: str) -> str:
    """Re-offer a claimed, dead or cancelled task, re-arming three more attempts; a done task stays done."""
    return store.requeue_task(task_id)


@mcp.tool()
def extend_lease(task_id: str, lease_seconds: int = 900) -> str:
    """Renew the lease of a claimed task (deadline = now + lease_seconds) so long work is not re-offered mid-flight."""
    return store.extend_lease(task_id, lease_seconds)


@mcp.tool()
def read_result(task_id: str) -> str:
    """Read the published result of a task."""
    return store.read_result(task_id)


@mcp.tool()
def get_system_state() -> str:
    """Global state: registered agents, queue lengths (queued/claimed), settled tasks."""
    return store.get_system_state()


@mcp.tool()
def get_events(task_id: str = "", agent: str = "", limit: int = 100) -> str:
    """Chronological transition history of the bus (push/claim/expire/publish), filterable by task and/or agent."""
    return store.get_events(task_id or None, agent or None, limit)


if __name__ == "__main__":
    mcp.run()
