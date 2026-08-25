"""Command-line entry point of the inter-agent bus — thin wrapper over store.py.

Installed as the `iab` console script (pyproject.toml); also runnable as a
bare script. Mirrors the MCP tools one to one, so an operator or an agent
session without MCP access can drive the bus without fragile `python -c`
one-liners. This file holds NO state logic.

Payloads and result contents are read from stdin when the positional
argument is `-` or omitted: never build shell command lines around them
(quoting differs per platform, argv is size-limited).
"""
from __future__ import annotations

import argparse
import importlib.util
import json
import os
import shutil
import subprocess
import sys
from pathlib import Path

try:  # installed package: `iab` maps to this directory (pyproject.toml)
    from . import store
except ImportError:  # bare script: python servers/shared_memory/cli.py
    import store


def _version() -> str:
    try:
        from importlib.metadata import version

        return version("inter-agent-bus")
    except Exception:
        return "unknown (not installed — pip install -e .)"


def _content(arg: str | None) -> str:
    if arg is None or arg == "-":
        return sys.stdin.read()
    return arg


def _whoami() -> str:
    name = os.environ.get("IAB_AGENT_NAME") or os.environ.get("ORCHESTRATOR_AGENT_NAME")
    return json.dumps(
        {"source": "env" if name else "none", "agent_name": name,
         **json.loads(store.db_info())}
    )


def _server_config(agent_name: str) -> dict:
    """MCP registration for this very interpreter and checkout: absolute
    paths, identity in env — the reliable way to name a worker."""
    server = Path(__file__).resolve().parent / "server.py"
    return {
        "type": "stdio",
        "command": sys.executable,
        "args": [str(server)],
        "env": {"IAB_AGENT_NAME": agent_name},
    }


def _install(scope: str, agent_name: str, print_only: bool) -> str:
    config = _server_config(agent_name)
    blob = json.dumps(config, indent=2)
    if print_only:
        return blob
    if importlib.util.find_spec("mcp") is None:
        return (
            "ERROR: this interpreter has no MCP SDK — the registered server "
            "would not start. Install with: pip install -e '.[server]' "
            "(or -r requirements.txt), then rerun iab install."
        )
    claude = shutil.which("claude")
    if claude is None:
        return (
            "ERROR: `claude` CLI not found. Register the server manually in "
            f"your client at scope {scope}, under the name 'inter-agent-bus':\n{blob}"
        )
    done = subprocess.run(
        [claude, "mcp", "add-json", "inter-agent-bus", json.dumps(config),
         "--scope", scope],
        capture_output=True, text=True,
    )
    if done.returncode != 0:
        return f"ERROR: claude mcp add-json failed:\n{done.stderr or done.stdout}"
    return (
        f"OK: MCP server 'inter-agent-bus' registered at {scope} scope "
        f"(identity: {agent_name}).\n"
        "A newly added server only loads in the NEXT session — reopen, then "
        "verify with the whoami() tool (it also shows the resolved bus "
        "database).\n"
        "Caveat of a user-scope install: every session of this client shares "
        "that identity, and each project gets its own bus database unless "
        "IAB_DB says otherwise."
    )


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="iab",
        description="Inter-agent bus: task queues under lease and a shared "
        "result store over one SQLite database (IAB_DB).",
    )
    parser.add_argument("--version", action="version", version=f"%(prog)s {_version()}")
    sub = parser.add_subparsers(dest="command", required=True)

    p = sub.add_parser("register", help="declare an agent (creates its queue)")
    p.add_argument("name")
    p.add_argument("-d", "--description", default="")

    p = sub.add_parser("push", help="queue a task for a registered agent")
    p.add_argument("agent")
    p.add_argument("task_id")
    p.add_argument("payload", nargs="?", help="omit or use '-' to read from stdin")
    p.add_argument("-p", "--priority", type=int, default=1)

    p = sub.add_parser("claim", help="claim the next task of an agent's queue, under lease")
    p.add_argument("agent")
    p.add_argument("--lease", type=int, default=900, metavar="SECONDS")

    p = sub.add_parser("publish", help="publish a task's result (settles the task)")
    p.add_argument("agent")
    p.add_argument("task_id")
    p.add_argument("content", nargs="?", help="omit or use '-' to read from stdin")

    p = sub.add_parser("result", help="read the published result of a task")
    p.add_argument("task_id")

    sub.add_parser("state", help="global state: agents, queues, settled tasks")

    p = sub.add_parser("log", help="transition history (push/claim/expire/publish)")
    p.add_argument("task_id", nargs="?")
    p.add_argument("-a", "--agent")
    p.add_argument("-n", "--limit", type=int, default=100)

    sub.add_parser("whoami", help="identity from the environment and the resolved bus database")

    p = sub.add_parser(
        "install",
        help="register the MCP server in the client (Claude Code: claude mcp add-json)",
    )
    p.add_argument("--scope", choices=["user", "project", "local"], default="user")
    p.add_argument("--agent-name", default="claude",
                   help="identity baked into the registration env (default: claude)")
    p.add_argument("--print", action="store_true", dest="print_only",
                   help="print the JSON registration instead of applying it")

    args = parser.parse_args(argv)
    out = {
        "register": lambda: store.register_agent(args.name, args.description),
        "push": lambda: store.push_task(
            args.agent, args.task_id, _content(args.payload), args.priority
        ),
        "claim": lambda: store.claim_task(args.agent, args.lease),
        "publish": lambda: store.publish_result(args.agent, args.task_id, _content(args.content)),
        "result": lambda: store.read_result(args.task_id),
        "state": lambda: store.get_system_state(),
        "log": lambda: store.get_events(args.task_id, args.agent, args.limit),
        "whoami": _whoami,
        "install": lambda: _install(args.scope, args.agent_name, args.print_only),
    }[args.command]()
    print(out)
    return 1 if out.startswith("ERROR") else 0


if __name__ == "__main__":
    sys.exit(main())
