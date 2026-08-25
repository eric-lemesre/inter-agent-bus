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
import json
import os
import sys

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
        {"source": "env" if name else "none", "agent_name": name, "db": str(store.db_path())}
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
    }[args.command]()
    print(out)
    return 1 if out.startswith("ERROR") else 0


if __name__ == "__main__":
    sys.exit(main())
