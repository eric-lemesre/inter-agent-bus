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
    from . import review, store, wake, worker
    from .access import bus
except ImportError:  # bare script: python servers/shared_memory/cli.py
    import review
    import store
    import wake
    import worker
    from access import bus


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
    return json.dumps({**store.resolve_identity(), **json.loads(bus.db_info())})


def _server_config(agent_name: str) -> dict:
    """MCP registration for this very install: the `iab-server` console
    script next to the interpreter when it exists (an installed runtime —
    no repository path involved), else the interpreter plus the absolute
    path of server.py. Identity in env — the reliable way to name a
    worker."""
    # No resolve() here: a venv's python is a symlink to the system one,
    # and resolving it would look for iab-server next to /usr/bin/python.
    exe = Path(sys.executable).parent / (
        "iab-server.exe" if os.name == "nt" else "iab-server"
    )
    if exe.exists():
        return {
            "type": "stdio",
            "command": str(exe),
            "env": {"IAB_AGENT_NAME": agent_name},
        }
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


def _watch(agent: str, timeout: float, interval: float, on: str) -> str:
    """Supervisor primitive. `--on task` (default): task_ids one per line so
    a shell/timer unit can consume them without a JSON parser; silent empty
    output on timeout, exit 0 either way. `--on notification|any`: the
    signal snapshot as JSON (`signalled` tells timeout from arrival) — the
    wake-up of an open interactive session run as a background task."""
    if on == "task":
        out = bus.wait_for_task(agent, timeout, interval)
        if out.startswith("ERROR"):
            return out
        return "\n".join(json.loads(out))
    return bus.wait_signal(agent, on, timeout, interval)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="iab",
        description="Inter-agent bus: task queues under lease and a shared "
        "result store over one SQLite database (IAB_DB).",
    )
    parser.add_argument("--version", action="version", version=f"%(prog)s {_version()}")
    parser.add_argument("--json", action="store_true",
                        help="pretty-print JSON output when applicable")
    sub = parser.add_subparsers(dest="command", required=True)

    p = sub.add_parser("register",
                       help="declare an agent instance `harness[@workspace]` (creates its queue)")
    p.add_argument("name")
    p.add_argument("-d", "--description", default="")
    p.add_argument("--cwd", default="", help="directory the instance works in")
    p.add_argument("--model", default=None, help="model carrying the instance right now")

    p = sub.add_parser("rename", help="move an identity to a new name (queue, results, "
                                      "notifications, cursors, presence, journal follow)")
    p.add_argument("old")
    p.add_argument("new")

    p = sub.add_parser("push", help="queue a task for a registered agent")
    p.add_argument("agent")
    p.add_argument("task_id")
    p.add_argument("payload", nargs="?", help="omit or use '-' to read from stdin")
    p.add_argument("-p", "--priority", type=int, default=1)
    p.add_argument("--max-attempts", type=int, default=3,
                   help="expired leases before dead-lettering (default: 3)")
    p.add_argument("--no-notify", action="store_true",
                   help="do not drop a wake-up notification in the target's mailbox")

    p = sub.add_parser("claim", help="claim the next task of an agent's queue, under lease")
    p.add_argument("agent")
    p.add_argument("--lease", type=int, default=900, metavar="SECONDS")
    p.add_argument("--task-id", help="claim this specific queued task of one's own queue")

    p = sub.add_parser("publish", help="publish a task's result (settles the task)")
    p.add_argument("agent")
    p.add_argument("task_id")
    p.add_argument("content", nargs="?", help="omit or use '-' to read from stdin")
    p.add_argument("--attempt", type=int,
                   help="attempt number received at claim (lease fencing)")
    p.add_argument("--force", action="store_true",
                   help="orchestrator override of ownership and fencing checks")

    p = sub.add_parser("cancel", help="cancel a task that is not settled yet")
    p.add_argument("task_id")

    p = sub.add_parser("requeue", help="re-offer a claimed, dead or cancelled task")
    p.add_argument("task_id")

    p = sub.add_parser("extend", help="renew the lease of a claimed task")
    p.add_argument("task_id")
    p.add_argument("--lease", type=int, default=900, metavar="SECONDS")

    p = sub.add_parser("result", help="read the published result of a task")
    p.add_argument("task_id")

    sub.add_parser("state", help="global state: agents, queues, settled tasks, presence")

    p = sub.add_parser("heartbeat", help="post or refresh a presence heartbeat with a capability card")
    p.add_argument("agent")
    p.add_argument("--ttl", type=int, default=120, metavar="SECONDS")
    p.add_argument("--capabilities", default=None, help="JSON object describing the agent's capabilities")
    p.add_argument("--model", default=None, help="model carrying the instance right now")

    p = sub.add_parser("announce", help="append a message to the global channel")
    p.add_argument("author")
    p.add_argument("topic")
    p.add_argument("message", nargs="?", help="omit or use '-' to read from stdin")

    p = sub.add_parser("channel", help="read the global announcement channel")
    g = p.add_mutually_exclusive_group(required=True)
    g.add_argument("--agent", help="read from this agent's cursor (at-least-once)")
    g.add_argument("--since", type=int, dest="since_seq", help="pure read from this sequence number")
    p.add_argument("--topic", help="filter by topic (only with --since)")
    p.add_argument("--limit", type=int, default=100)

    p = sub.add_parser("notify", help="send a directed signal to one agent (pick it up with `poll`)")
    p.add_argument("author")
    p.add_argument("agent")
    p.add_argument("message", nargs="?", help="omit or use '-' to read from stdin")
    p.add_argument("--reply-to", type=int, default=None, metavar="BROADCAST_ID",
                   help="answer a broadcast (collected by `iab replies`)")
    p.add_argument("--wake", action="store_true",
                   help="ask a supervisor to launch the recipient if asleep (roster permitting)")

    p = sub.add_parser("broadcast", help="ask or tell several agents at once "
                                         "(scope: '*', 'harness@*', '*@workspace', "
                                         "'model:<glob>' or a comma-separated list)")
    p.add_argument("author")
    p.add_argument("scope")
    p.add_argument("message", nargs="?", help="omit or use '-' to read from stdin")
    p.add_argument("--alive-only", action="store_true", help="skip asleep recipients")
    p.add_argument("--wake", action="store_true",
                   help="ask a supervisor to launch asleep recipients (roster permitting)")

    p = sub.add_parser("replies", help="answers received to a broadcast")
    p.add_argument("broadcast_id", type=int)

    p = sub.add_parser("poll", help="read an agent's own directed notifications (at-least-once)")
    p.add_argument("agent")
    p.add_argument("--limit", type=int, default=100)

    p = sub.add_parser("watch",
                       help="block until a signal lands for the agent: --on task (default) "
                            "prints queued task_ids one per line; --on notification|any "
                            "prints the signal snapshot as JSON; exits 0 either way")
    p.add_argument("agent")
    p.add_argument("--on", choices=["task", "notification", "any"], default="task",
                   help="which signal ends the wait (default: task)")
    p.add_argument("--timeout", type=float, default=300.0, metavar="SECONDS")
    p.add_argument("--interval", type=float, default=2.0, metavar="SECONDS")

    p = sub.add_parser("signals", help="what waits for an agent (tasks, unread "
                                       "notifications, broadcasts) — read only")
    p.add_argument("agent")

    p = sub.add_parser("wake", help="launch an asleep agent if a signal waits for it, "
                                    "from the roster's `launch` block (one decision, synchronous)")
    p.add_argument("agent")
    p.add_argument("--dry-run", action="store_true",
                   help="print the decision and the command, launch nothing")

    p = sub.add_parser("supervise", help="the single watcher: wake every roster agent "
                                         "with a `launch` block when a signal waits")
    p.add_argument("--interval", type=float, default=60.0, metavar="SECONDS")
    p.add_argument("--once", action="store_true", help="one pass, then exit")
    p.add_argument("--dry-run", action="store_true")

    sub.add_parser("presence", help="list all known agents and their presence status")

    p = sub.add_parser("log", help="transition history (push/claim/expire/publish/announce)")
    p.add_argument("task_id", nargs="?")
    p.add_argument("-a", "--agent")
    p.add_argument("-n", "--limit", type=int, default=100)

    sub.add_parser("whoami", help="identity from the environment and the resolved bus database")

    p = sub.add_parser(
        "worker",
        help="headless worker loop: claim → run a command with the payload "
        "on stdin → publish its stdout (ERROR: on failure)",
    )
    p.add_argument("--agent", required=True, help="identity to work as (roster name)")
    p.add_argument("--model", default=None,
                   help="model behind the command, declared on the bus (default: IAB_MODEL)")
    p.add_argument("--once", action="store_true", help="one task (or NO_TASK) then exit")
    p.add_argument("--lease", type=int, default=900, metavar="SECONDS",
                   help="lease per claim; extended at every heartbeat while running")
    p.add_argument("--poll", type=int, default=5, metavar="SECONDS",
                   help="initial delay between empty claims (doubles up to 60s)")
    p.add_argument("--task-timeout", type=int, metavar="SECONDS",
                   help="kill the command and publish ERROR after this long "
                   "(default: no limit — a hung command holds its task forever)")
    p.add_argument("worker_cmd", nargs=argparse.REMAINDER, metavar="-- command...",
                   help="the worker command, after `--`; receives the payload on stdin")

    p = sub.add_parser(
        "review",
        help="guarded review: embed a diff in a review prompt, run the "
        "reviewer command, verify its structured output, publish on the bus",
    )
    p.add_argument("--agent", required=True, help="reviewer identity (roster name)")
    src = p.add_mutually_exclusive_group(required=True)
    src.add_argument("--staged", action="store_true", help="review `git diff --staged`")
    src.add_argument("--diff", metavar="FILE", help="unified diff file ('-' for stdin)")
    p.add_argument("--task-id", help="bus task id (default: review-<random>)")
    p.add_argument("--lease", type=int, default=900, metavar="SECONDS")
    p.add_argument("--task-timeout", type=int, metavar="SECONDS")
    p.add_argument("review_cmd", nargs=argparse.REMAINDER, metavar="-- command...",
                   help="the reviewer command, after `--`; receives the prompt on stdin")

    p = sub.add_parser(
        "install",
        help="register the MCP server in the client (Claude Code: claude mcp add-json)",
    )
    p.add_argument("--scope", choices=["user", "project", "local"], default="user")
    p.add_argument("--agent-name", default="claude",
                   help="harness baked into the registration env (default: claude); the "
                        "workspace is derived from each session's launch directory — "
                        "pass harness@workspace to pin both")
    p.add_argument("--print", action="store_true", dest="print_only",
                   help="print the JSON registration instead of applying it")

    args = parser.parse_args(argv)
    if args.command == "supervise":
        return wake.supervise(args.interval, args.once, args.dry_run)
    if args.command == "review":
        cmd = args.review_cmd
        if cmd and cmd[0] == "--":
            cmd = cmd[1:]
        if not cmd:
            parser.error("review: missing reviewer command after `--`")
        if args.staged:
            try:
                diff_text = review.staged_diff()
            except RuntimeError as exc:
                print(f"ERROR: {exc}")
                return 1
        elif args.diff == "-":
            diff_text = sys.stdin.read()
        else:
            try:
                diff_text = Path(args.diff).read_text(encoding="utf-8")
            except OSError as exc:
                print(f"ERROR: cannot read diff file: {exc}")
                return 1
        return review.run_review(
            args.agent, cmd, diff_text, task_id=args.task_id,
            lease_seconds=args.lease, task_timeout=args.task_timeout,
        )
    if args.command == "worker":
        cmd = args.worker_cmd
        if cmd and cmd[0] == "--":
            cmd = cmd[1:]
        if not cmd:
            parser.error("worker: missing command after `--`")
        return worker.run_worker(
            args.agent, cmd, once=args.once, lease_seconds=args.lease,
            poll_seconds=args.poll, task_timeout=args.task_timeout,
            model=args.model or os.environ.get("IAB_MODEL") or None,
        )
    out = {
        "register": lambda: bus.register_agent(
            args.name, args.description, args.cwd, args.model),
        "rename": lambda: bus.rename_agent(args.old, args.new),
        "push": lambda: bus.push_task(
            args.agent, args.task_id, _content(args.payload), args.priority,
            args.max_attempts, not args.no_notify,
        ),
        "watch": lambda: _watch(args.agent, args.timeout, args.interval, args.on),
        "signals": lambda: bus.pending_signals(args.agent),
        "wake": lambda: wake.wake_agent(args.agent, dry_run=args.dry_run),
        "claim": lambda: bus.claim_task(args.agent, args.lease, args.task_id),
        "publish": lambda: bus.publish_result(
            args.agent, args.task_id, _content(args.content), args.attempt, args.force
        ),
        "cancel": lambda: bus.cancel_task(args.task_id),
        "requeue": lambda: bus.requeue_task(args.task_id),
        "extend": lambda: bus.extend_lease(args.task_id, args.lease),
        "result": lambda: bus.read_result(args.task_id),
        "state": lambda: bus.get_system_state(),
        "heartbeat": lambda: bus.heartbeat(
            args.agent, args.ttl, args.capabilities, args.model),
        "announce": lambda: bus.announce(args.author, args.topic, _content(args.message)),
        "channel": lambda: bus.read_channel(
            args.agent, args.since_seq, args.topic, args.limit
        ),
        "notify": lambda: bus.notify(args.author, args.agent, _content(args.message),
                                       args.reply_to, args.wake),
        "broadcast": lambda: bus.broadcast(args.author, _content(args.message), args.scope,
                                             args.alive_only, args.wake),
        "replies": lambda: bus.replies(args.broadcast_id),
        "poll": lambda: bus.poll(args.agent, args.limit),
        "presence": lambda: bus.list_presence(),
        "log": lambda: bus.get_events(args.task_id, args.agent, args.limit),
        "whoami": _whoami,
        "install": lambda: _install(args.scope, args.agent_name, args.print_only),
    }[args.command]()
    if args.json:
        try:
            out = json.dumps(json.loads(out), indent=2, ensure_ascii=False)
        except Exception:
            pass
    if out:
        print(out)
    return 1 if out.startswith("ERROR") else 0


if __name__ == "__main__":
    sys.exit(main())
