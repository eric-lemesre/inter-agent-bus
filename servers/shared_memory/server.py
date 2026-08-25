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
    """Identity hints of the connected client, so a worker can find its own roster entry: IAB_AGENT_NAME env var when the launcher set one (authoritative; legacy ORCHESTRATOR_AGENT_NAME honored), otherwise the MCP clientInfo sent at the handshake (to match against the roster's client_hints). Also returns the resolved bus database path, so a rendezvous mismatch between clients is diagnosable in one call."""
    db = str(store.db_path())
    forced = os.environ.get("IAB_AGENT_NAME") or os.environ.get("ORCHESTRATOR_AGENT_NAME")
    if forced:
        return json.dumps({"source": "env", "agent_name": forced, "db": db})
    params = ctx.session.client_params
    if params is None or params.client_info is None:
        return json.dumps({"source": "none", "db": db})
    info = params.client_info
    return json.dumps(
        {
            "source": "client_info",
            "name": info.name,
            "title": info.title,
            "version": info.version,
            "db": db,
        }
    )


@mcp.tool()
def register_agent(name: str, description: str = "") -> str:
    """Declare an agent (creates its queue). The cast comes from the project's roster, never from the plugin."""
    return store.register_agent(name, description)


@mcp.tool()
def push_task(target_agent: str, task_id: str, payload: str, priority: int = 1) -> str:
    """Queue a task for a registered agent (higher priority is served first)."""
    return store.push_task(target_agent, task_id, payload, priority)


@mcp.tool()
def claim_task(agent_name: str, lease_seconds: int = 900) -> str:
    """Claim the next task of one's queue, under lease: if not settled before the deadline, it is re-offered."""
    return store.claim_task(agent_name, lease_seconds)


@mcp.tool()
def publish_result(agent_name: str, task_id: str, result_content: str) -> str:
    """Publish a task's result (settles the task) into the shared memory readable by all agents."""
    return store.publish_result(agent_name, task_id, result_content)


@mcp.tool()
def read_result(task_id: str) -> str:
    """Read the published result of a task."""
    return store.read_result(task_id)


@mcp.tool()
def get_system_state() -> str:
    """Global state: registered agents, queue lengths (queued/claimed), settled tasks."""
    return store.get_system_state()


if __name__ == "__main__":
    mcp.run()
