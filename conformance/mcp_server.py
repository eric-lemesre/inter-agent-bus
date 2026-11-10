#!/usr/bin/env python3
"""MCP servers of the bus, judged against the reference (server.py) —
the MCP half of CONTRAT.md (§ 12), as run.py is the command-line half.

    python3 conformance/mcp_server.py --impl rust='["/path/to/iab-server"]'
    IAB_TEST_POSTGRES=<URL of an EMPTY test database> python3 conformance/mcp_server.py --impl …

Every server is driven as a PROCESS over stdio by the MCP client of the
`mcp` package (pip install 'inter-agent-bus[server]'). The reference is
servers/shared_memory/server.py of this checkout; every --impl is judged
against it:

  - interface: the same tools, descriptions and schemas;
  - transcript: one scripted session touching every tool, on a fresh
    database per server, answer for answer (dates and database masked,
    JSON compared parsed; of a tool error, only its first line);
  - crossed: the reference and the server under test on ONE database,
    each settling what the other started;
  - concurrency: a blocking wait_signal does not hold back the calls
    that follow it in the same session, and the push wakes it.
"""
from __future__ import annotations

import argparse
import asyncio
import json
import os
import re
import sys
import tempfile
import time
from contextlib import AsyncExitStack
from pathlib import Path

sys.stdout.reconfigure(encoding="utf-8", errors="backslashreplace")

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "servers" / "shared_memory"))
from testdb import PG, fresh_db  # noqa: E402  (engine under test)

from mcp import ClientSession  # noqa: E402
from mcp.client.stdio import StdioServerParameters, stdio_client  # noqa: E402

REFERENCE = [sys.executable, str(ROOT / "servers" / "shared_memory" / "server.py")]
AGENT = "claude@conformance"
TIMESTAMP = re.compile(r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(\.\d{6})?\+00:00")

failures: list[str] = []


def check(label: str, ok: bool, detail: str = "") -> None:
    print(("ok — " if ok else "FAIL: ") + label + (f" ({detail})" if detail and not ok else ""))
    if not ok:
        failures.append(label)


class Server:
    """One MCP server process, bound to one database."""

    def __init__(self, name: str, argv: list[str], db: str, cwd: Path,
                 extra: dict[str, str] | None = None) -> None:
        self.name, self.argv, self.db, self.cwd = name, argv, db, cwd
        self.extra = extra or {}

    async def __aenter__(self) -> "Server":
        env = {k: v for k, v in os.environ.items()
               if not k.startswith(("IAB_", "ORCHESTRATOR_", "PG"))}
        env.update(IAB_DB=self.db, IAB_AGENT_NAME=AGENT, IAB_MODEL="model-x")
        env.update(self.extra)
        self._stack = AsyncExitStack()
        params = StdioServerParameters(command=self.argv[0], args=self.argv[1:], env=env,
                                       cwd=str(self.cwd))
        read, write = await self._stack.enter_async_context(stdio_client(params))
        self.session = await self._stack.enter_async_context(ClientSession(read, write))
        self.init = await self.session.initialize()
        return self

    async def __aexit__(self, *exc) -> None:
        await self._stack.aclose()

    async def call(self, tool: str, **args) -> tuple[bool, str]:
        """(is_error, text) of one call."""
        result = await self.session.call_tool(tool, args)
        text = "".join(c.text for c in result.content if c.type == "text")
        return bool(result.is_error), text


def masked(server: Server, answer: tuple[bool, str]) -> tuple[bool, str]:
    is_error, text = answer
    if is_error:  # pydantic's wording is the reference's own; the first line is the contract
        text = text.split("\n", 1)[0]
    text = text.replace(json.dumps(server.db)[1:-1], "<db>").replace(server.db, "<db>")
    text = TIMESTAMP.sub("<date>", text)
    try:  # a JSON answer is judged parsed (CONTRAT.md, § 8): key order is free
        return is_error, json.loads(text)
    except ValueError:
        return is_error, text


# One session touching every tool, refusals and invalid arguments included.
# The server's own identity (IAB_AGENT_NAME) is registered by its first call.
SCRIPT: list[tuple[str, dict]] = [
    ("whoami", {}),
    ("get_system_state", {}),
    ("register_agent", {"name": "worker@conformance", "description": "worker", "cwd": "/w",
                        "model": "m1"}),
    ("register_agent", {"name": "Bad Name"}),
    ("register_agent", {"name": "old@conformance"}),
    ("rename_agent", {"old": "old@conformance", "new": "renamed@conformance"}),
    ("rename_agent", {"old": "nobody", "new": "x"}),
    ("push_task", {"target_agent": "worker@conformance", "task_id": "t1", "payload": "é ✓ 🚀\r\n",
                   "priority": "5"}),
    ("push_task", {"target_agent": "worker@conformance", "task_id": "t2", "payload": "two",
                   "notify": "no", "max_attempts": 1.0}),
    ("push_task", {"target_agent": "worker@conformance", "task_id": "t1", "payload": "again"}),
    ("push_task", {"target_agent": "ghost", "task_id": "t9", "payload": "p"}),
    ("push_task", {"target_agent": "worker@conformance", "task_id": "t3"}),
    ("pending_signals", {"agent": "worker@conformance"}),
    ("wait_task", {"agent": "worker@conformance", "timeout_seconds": 1}),
    ("wait_signal", {"agent": "worker@conformance", "kinds": "task", "timeout_seconds": "1"}),
    ("wait_signal", {"agent": "worker@conformance", "kinds": "bogus", "timeout_seconds": 0.2}),
    ("claim_task", {"agent_name": "worker@conformance"}),
    ("claim_task", {"agent_name": "worker@conformance", "task_id": "t2", "lease_seconds": 60}),
    ("claim_task", {"agent_name": "worker@conformance"}),
    ("extend_lease", {"task_id": "t1", "lease_seconds": 120}),
    ("extend_lease", {"task_id": "nope"}),
    ("publish_result", {"agent_name": "worker@conformance", "task_id": "t1", "result_content": "done",
                        "attempt": 7}),
    ("publish_result", {"agent_name": "worker@conformance", "task_id": "t1", "result_content": "done ✓",
                        "attempt": 1}),
    ("read_result", {"task_id": "t1"}),
    ("read_result", {"task_id": "t2"}),
    ("cancel_task", {"task_id": "t2"}),
    ("cancel_task", {"task_id": "t1"}),
    ("requeue_task", {"task_id": "t2"}),
    ("requeue_task", {"task_id": "t1"}),
    ("heartbeat", {"agent": "worker@conformance", "ttl_seconds": 300,
                   "capabilities": "{\"langs\": [\"rust\"]}"}),
    ("heartbeat", {"agent": "worker@conformance", "capabilities": "[1]"}),
    ("list_presence", {}),
    ("announce", {"author": "worker@conformance", "topic": "release", "message": "v1 — prête"}),
    ("announce", {"author": "worker@conformance", "topic": "Bad Topic", "message": "x"}),
    ("read_channel", {"since_seq": 0}),
    ("read_channel", {"agent": AGENT}),
    ("read_channel", {"agent": AGENT}),
    ("read_channel", {"agent": AGENT, "topic": "release"}),
    ("notify", {"author": AGENT, "target_agent": "worker@conformance", "message": "ping"}),
    ("notify", {"author": AGENT, "target_agent": "ghost", "message": "ping"}),
    ("broadcast", {"author": AGENT, "message": "who is there?", "scope": "*@conformance"}),
    ("poll", {"agent": "worker@conformance"}),
    ("notify", {"author": "worker@conformance", "target_agent": AGENT, "message": "me",
                "reply_to": "1"}),
    ("replies", {"broadcast_id": 1}),
    ("poll", {"agent": AGENT, "limit": 10}),
    ("pending_signals", {"agent": "worker@conformance"}),
    ("get_events", {"task_id": "t1"}),
    ("get_events", {"agent": "worker@conformance", "limit": 3}),
    ("get_system_state", {}),
    ("claim_task", {"agent_name": "worker@conformance", "lease_seconds": 1.5}),
    ("register_agent", {"name": 5}),
    ("no_such_tool", {}),
]


async def interface(reference: Server, server: Server, tag: str) -> None:
    ref = {t.name: t for t in (await reference.session.list_tools()).tools}
    got = {t.name: t for t in (await server.session.list_tools()).tools}
    check(f"{tag} the same tool names", sorted(ref) == sorted(got),
          f"missing {sorted(set(ref) - set(got))}, extra {sorted(set(got) - set(ref))}")
    for name in sorted(set(ref) & set(got)):
        for field in ("description", "inputSchema", "outputSchema"):
            a = ref[name].model_dump(by_alias=True).get(field)
            b = got[name].model_dump(by_alias=True).get(field)
            check(f"{tag} {name}: same {field}", a == b, f"{json.dumps(b)[:300]}")
    check(f"{tag} server named InterAgentBus", server.init.server_info.name == "InterAgentBus")


def _masked(text: str | None) -> str | None:
    """The instructions with the path of the `iab` command masked: each
    server names the command installed next to it."""
    return None if text is None else re.sub(r"\S*iab(\.exe)?(?= )", "IAB", text)


async def instructions(reference: list[str], argv: list[str], db: str, ref_db: str, cwd: Path,
                       tag: str) -> None:
    """The wake-up instructions of the initialization, word for word, for
    the default settings, a machine queue with an interval, and none."""
    for label, extra in (("default", {}),
                         ("machine queue", {"IAB_MACHINE": "Odin", "IAB_WATCH_INTERVAL": "90"}),
                         ("disabled", {"IAB_WATCH_AUTO": "0"})):
        async with Server("reference", reference, ref_db, cwd, extra) as ref, \
                Server("judged", argv, db, cwd, extra) as server:
            a, b = _masked(ref.init.instructions), _masked(server.init.instructions)
            check(f"{tag} same instructions ({label})", a == b, f"{b!r}"[:300])
            if label == "machine queue":
                check(f"{tag} the instructions watch the machine queue",
                      a is not None and "odin@general --on any --new --interval 90" in a, str(a))
            if label == "disabled":
                check(f"{tag} IAB_WATCH_AUTO=0 sends none", a is None and b is None)


async def transcript(ref_db: str, db: str, reference: list[str], argv: list[str], name: str,
                     cwd: Path, tag: str) -> None:
    answers: dict[str, list] = {}
    for label, command, path in (("reference", reference, ref_db), (name, argv, db)):
        async with Server(label, command, path, cwd) as server:
            answers[label] = [masked(server, await server.call(tool, **args)) for tool, args in SCRIPT]
    for (tool, args), want, got in zip(SCRIPT, answers["reference"], answers[name]):
        check(f"{tag} {tool}({', '.join(args)})", want == got,
              f"reference {want!r:.400} — {name} {got!r:.400}")


async def crossed(a: Server, b: Server, tag: str) -> None:
    w = "worker@conformance"
    check(f"{tag} register via {a.name}", (await a.call("register_agent", name=w))[1].startswith("OK"))
    check(f"{tag} the other server's identity seen", AGENT in (await b.call("list_presence"))[1])
    check(f"{tag} push via {a.name}",
          (await a.call("push_task", target_agent=w, task_id="x1", payload="données"))[1].startswith("OK"))
    claimed = json.loads((await b.call("claim_task", agent_name=w))[1])
    check(f"{tag} claim via {b.name}", claimed.get("payload") == "données")
    check(f"{tag} publish via {b.name}",
          (await b.call("publish_result", agent_name=w, task_id="x1", result_content="résultat",
                        attempt=claimed.get("attempts", 0)))[1].startswith("OK"))
    check(f"{tag} result read via {a.name}", "résultat" in (await a.call("read_result", task_id="x1"))[1])
    sent = json.loads((await a.call("broadcast", author=AGENT, message="q?", scope=w))[1])
    await b.call("notify", author=w, target_agent=AGENT, message="r!", reply_to=sent["broadcast_id"])
    check(f"{tag} broadcast via {a.name} answered via {b.name}",
          "r!" in (await a.call("replies", broadcast_id=sent["broadcast_id"]))[1])


async def concurrency(server: Server, tag: str) -> None:
    w = "sleeper@conformance"
    await server.call("register_agent", name=w)
    started = time.monotonic()
    waiting = asyncio.ensure_future(server.call("wait_signal", agent=w, kinds="task", timeout_seconds=20))
    await asyncio.sleep(0.5)
    t0 = time.monotonic()
    pushed = await server.call("push_task", target_agent=w, task_id="z1", payload="p")
    check(f"{tag} a call is served while wait_signal blocks", pushed[1].startswith("OK")
          and time.monotonic() - t0 < 5, f"{time.monotonic() - t0:.1f} s")
    snapshot = json.loads((await asyncio.wait_for(waiting, 30))[1])
    check(f"{tag} the push wakes wait_signal", snapshot.get("signalled") is True
          and time.monotonic() - started < 15, f"{time.monotonic() - started:.1f} s")


async def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--impl", action="append", default=[], metavar="NAME=JSON_ARGV")
    args = parser.parse_args()
    impls = {name: json.loads(argv) for name, _, argv in (s.partition("=") for s in args.impl)}
    print(f"engine: {'postgresql' if PG else 'sqlite'}; reference: server.py; "
          f"judged: {', '.join(impls) or 'the reference itself'}")
    impls = impls or {"python": REFERENCE}
    with tempfile.TemporaryDirectory() as tmp:
        cwd = Path(tmp) / "conformance"
        cwd.mkdir()
        for n, (name, argv) in enumerate(impls.items()):
            async with Server("reference", REFERENCE, fresh_db(tmp, f"ri{n}"), cwd) as ref, \
                    Server(name, argv, fresh_db(tmp, f"i{n}"), cwd) as server:
                await interface(ref, server, f"[{name}] interface:")
            await instructions(REFERENCE, argv, fresh_db(tmp, f"ii{n}"), fresh_db(tmp, f"rii{n}"),
                               cwd, f"[{name}] instructions:")
            await transcript(fresh_db(tmp, f"rt{n}"), fresh_db(tmp, f"t{n}"), REFERENCE, argv, name,
                             cwd, f"[{name}] transcript:")
            for m, (first, second) in enumerate(((("reference", REFERENCE), (name, argv)),
                                                 ((name, argv), ("reference", REFERENCE)))):
                db = fresh_db(tmp, f"x{n}_{m}")
                async with Server(*first, db, cwd) as a, Server(*second, db, cwd) as b:
                    await crossed(a, b, f"[{a.name}→{b.name}] crossed:")
            async with Server(name, argv, fresh_db(tmp, f"c{n}"), cwd) as server:
                await concurrency(server, f"[{name}] concurrency:")
    print(f"mcp: {'OK' if not failures else f'{len(failures)} FAILURE(S)'}.")
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
