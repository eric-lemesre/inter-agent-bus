#!/usr/bin/env python3
"""Conformance suite of the bus — the executable half of CONTRAT.md.

Every scenario drives implementations as PROCESSES through their
command line, never by importing store.py: the same scenarios then
judge any implementation (Python today, the native client tomorrow) and
any mix of two of them writing ONE database. With implementations A and
B, each scenario runs as A/A, A/B, B/A and B/B on every engine.

    python3 conformance/run.py
    python3 conformance/run.py --impl rust='["/path/to/iab"]'
    python3 conformance/run.py --impl python='[…]' --impl rust='[…]' --without rust=wake
    IAB_TEST_POSTGRES=<URL of an EMPTY test database> python3 conformance/run.py

An implementation is a NAME=COMMAND pair, COMMAND a JSON list (the argv
prefix; no shell, no quoting rules). Without --impl, the Python
implementation of this checkout is used. Engines: SQLite always,
PostgreSQL when IAB_TEST_POSTGRES is set (each scenario gets a fresh
schema, see servers/shared_memory/testdb.py). `--without NAME=SCENARIO`
skips a scenario on every pair involving an implementation that does not
offer it yet (said in the report, never silently).

The child processes get no PYTHONUTF8/PYTHONIOENCODING: their standard
streams are judged as an operator's shell would see them. Every byte
they write must be UTF-8.
"""
from __future__ import annotations

import argparse
import itertools
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import time
from dataclasses import dataclass
from pathlib import Path

# The report holds non-ASCII (—, →): print it in UTF-8 even where the
# console code page cannot (cp1252 on Windows).
sys.stdout.reconfigure(encoding="utf-8", errors="backslashreplace")

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "servers" / "shared_memory"))
from testdb import PG, fresh_db  # noqa: E402  (engine under test)

PYTHON_IMPL = [sys.executable, str(ROOT / "servers" / "shared_memory" / "cli.py")]
# ISO 8601 in UTC as datetime.isoformat() writes it: the microseconds are
# omitted when they are zero (CONTRAT.md, "Dates").
TIMESTAMP = re.compile(r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(\.\d{6})?\+00:00$")
PROJECT_KEY = re.compile(r"^[A-Za-z0-9_-]{1,40}-[0-9a-f]{12}$")
# Payload fidelity: non-ASCII (BMP and beyond), CRLF and a bare CR,
# a trailing newline and enough bytes to exceed any argv limit.
PAYLOAD = ("Ligne 1 — é à ç ✓ 🚀\r\nLigne 2\rLigne 3\n" + "x" * 200_000 + "\n")
# A stand-in worker command: echoes its stdin byte for byte.
ECHO = [sys.executable, "-c",
        "import sys; sys.stdout.buffer.write(sys.stdin.buffer.read())"]

failures: list[str] = []


@dataclass
class Run:
    rc: int
    out: str
    err: str

    def json(self):
        return json.loads(self.out)


class Impl:
    """One implementation bound to one bus database."""

    def __init__(self, name: str, argv: list[str], db: str, cwd: Path) -> None:
        self.name, self.argv, self.db, self.cwd = name, argv, db, cwd

    def env(self, extra: dict[str, str] | None = None) -> dict[str, str]:
        env = {k: v for k, v in os.environ.items()
               if not k.startswith(("IAB_", "ORCHESTRATOR_"))
               and k not in ("PYTHONUTF8", "PYTHONIOENCODING")}
        env["IAB_DB"] = self.db
        env.update(extra or {})
        return env

    def __call__(self, *args: str, stdin: str | None = None, cwd: Path | None = None,
                 env: dict[str, str] | None = None, timeout: float = 120) -> Run:
        done = subprocess.run(
            [*self.argv, *args], input=None if stdin is None else stdin.encode("utf-8"),
            capture_output=True, cwd=cwd or self.cwd, env=self.env(env), timeout=timeout)
        return Run(done.returncode, _utf8(done.stdout, self, args), _utf8(done.stderr, self, args))

    def popen(self, *args: str, env: dict[str, str] | None = None) -> subprocess.Popen:
        return subprocess.Popen([*self.argv, *args], stdout=subprocess.PIPE,
                                stderr=subprocess.PIPE, cwd=self.cwd, env=self.env(env))


def _utf8(data: bytes, impl: Impl, args) -> str:
    try:
        return data.decode("utf-8")
    except UnicodeDecodeError as exc:
        check(f"{impl.name} {args[0] if args else ''} writes UTF-8", False, str(exc))
        return data.decode("utf-8", errors="replace")


def check(label: str, ok: bool, detail: str = "") -> None:
    print(("ok — " if ok else "FAIL: ") + label + (f" ({detail})" if detail and not ok else ""))
    if not ok:
        failures.append(label)


def strip_newline(text: str) -> str:
    """The printed value without the single line ending print() adds
    (LF, or CRLF where the platform translates it)."""
    for ending in ("\r\n", "\n"):
        if text.endswith(ending):
            return text[: -len(ending)]
    return text


def ok_line(run: Run) -> bool:
    return run.rc == 0 and run.out.startswith("OK")


def refused(run: Run) -> bool:
    return run.rc != 0 and run.out.startswith("ERROR")


# -- scenarios ----------------------------------------------------------------
# Each takes (a, b, tag); `a` and `b` share one fresh database.

def lifecycle(a: Impl, b: Impl, tag: str) -> None:
    check(f"{tag} register", ok_line(a("register", "alpha@conf", "-d", "test"))
          and ok_line(b("register", "beta@conf")))
    check(f"{tag} push", ok_line(a("push", "alpha@conf", "t1", "hello")))
    claim = b("claim", "alpha@conf", "--lease", "60")
    task = claim.json() if claim.rc == 0 and claim.out.startswith("{") else {}
    check(f"{tag} claim across implementations", task.get("task_id") == "t1"
          and task.get("payload") == "hello" and task.get("attempts") == 1, claim.out)
    check(f"{tag} publish with the claimed attempt",
          ok_line(b("publish", "alpha@conf", "t1", "done", "--attempt", "1")))
    result = a("result", "t1")
    data = result.json() if result.rc == 0 else {}
    check(f"{tag} result read back", data.get("author") == "alpha@conf"
          and data.get("content") == "done", result.out)
    events = [e["event"] for e in a("log", "t1").json()]
    check(f"{tag} journal push → claim → publish", events == ["push", "claim", "publish"],
          str(events))
    stamps = [task.get("created_at", ""), task.get("lease_deadline", ""), data.get("updated_at", "")]
    stamps += [e["at"] for e in b("log", "t1").json()]
    bad = [s for s in stamps if not TIMESTAMP.match(s)]
    check(f"{tag} timestamps in the contract format", not bad, str(bad))


def refusals(a: Impl, b: Impl, tag: str) -> None:
    check(f"{tag} push to an unknown agent refused", refused(a("push", "ghost@conf", "t", "x")))
    a("register", "alpha@conf")
    a("push", "alpha@conf", "dup", "x")
    check(f"{tag} duplicate task_id refused", refused(b("push", "alpha@conf", "dup", "y")))
    check(f"{tag} publish on an unknown task refused",
          refused(b("publish", "alpha@conf", "nope", "x", "--attempt", "1")))
    empty = b("claim", "beta@conf")
    check(f"{tag} claim for an unknown agent refused", empty.rc != 0 or empty.out.startswith("ERROR"),
          empty.out)


def priority(a: Impl, b: Impl, tag: str) -> None:
    a("register", "alpha@conf")
    a("push", "alpha@conf", "low", "p1", "-p", "1")
    b("push", "alpha@conf", "high", "p9", "-p", "9")
    first = b("claim", "alpha@conf").json()
    check(f"{tag} highest priority served first", first.get("task_id") == "high")
    second = a("claim", "alpha@conf").json()
    check(f"{tag} then the next one", second.get("task_id") == "low")
    check(f"{tag} empty queue answers NO_TASK", strip_newline(b("claim", "alpha@conf").out) == "NO_TASK")


def fencing(a: Impl, b: Impl, tag: str) -> None:
    a("register", "alpha@conf")
    a("push", "alpha@conf", "f1", "payload", "--max-attempts", "3")
    first = b("claim", "alpha@conf", "--lease", "1").json()
    time.sleep(1.6)
    second = a("claim", "alpha@conf", "--lease", "60").json()
    check(f"{tag} expired lease re-offers the task", second.get("task_id") == "f1"
          and second.get("attempts") == 2, str(second))
    check(f"{tag} stale attempt refused at settle",
          refused(b("publish", "alpha@conf", "f1", "late", "--attempt", str(first.get("attempts")))))
    check(f"{tag} current attempt settles",
          ok_line(a("publish", "alpha@conf", "f1", "fresh", "--attempt", "2")))
    check(f"{tag} the fresh result is kept", b("result", "f1").json().get("content") == "fresh")


def dead_letter(a: Impl, b: Impl, tag: str) -> None:
    a("register", "alpha@conf")
    a("push", "alpha@conf", "d1", "x", "--max-attempts", "1")
    b("claim", "alpha@conf", "--lease", "1")
    time.sleep(1.6)
    check(f"{tag} exhausted task is not re-offered",
          strip_newline(a("claim", "alpha@conf").out) == "NO_TASK")
    queues = b("state").json()["queues"]["alpha@conf"]
    check(f"{tag} exhausted task is dead", queues.get("dead") == 1, str(queues))
    check(f"{tag} requeue revives it", ok_line(a("requeue", "d1")))
    check(f"{tag} revived task claimable", b("claim", "alpha@conf").json().get("task_id") == "d1")
    check(f"{tag} extend renews the lease", ok_line(a("extend", "d1", "--lease", "60")))
    a("push", "alpha@conf", "c1", "x")
    check(f"{tag} cancel", ok_line(b("cancel", "c1")))
    check(f"{tag} cancelled task not claimable", strip_newline(a("claim", "alpha@conf").out) == "NO_TASK")


def contention(a: Impl, b: Impl, tag: str) -> None:
    a("register", "alpha@conf")
    a("push", "alpha@conf", "only", "x")
    procs = [(a if i % 2 else b).popen("claim", "alpha@conf") for i in range(8)]
    outs = [p.communicate(timeout=120)[0].decode("utf-8", errors="replace") for p in procs]
    winners = [o for o in outs if o.startswith("{")]
    losers = [o for o in outs if strip_newline(o) == "NO_TASK"]
    check(f"{tag} 8 concurrent claims: exactly one wins", len(winners) == 1 and len(losers) == 7,
          f"{len(winners)} winners, {len(losers)} NO_TASK, {outs}")


def messages(a: Impl, b: Impl, tag: str) -> None:
    a("register", "alpha@conf")
    b("register", "beta@conf")
    note = a("notify", "alpha@conf", "beta@conf", stdin="bonjour — ✓")
    check(f"{tag} notify", note.rc == 0 and "seq" in note.json(), note.out)
    signals = b("signals", "beta@conf").json()
    check(f"{tag} signals count it", signals.get("notifications") == 1, str(signals))
    polled = b("poll", "beta@conf").json()
    check(f"{tag} poll delivers it intact", [m["message"] for m in polled] == ["bonjour — ✓"]
          and polled[0]["author"] == "alpha@conf", str(polled))
    check(f"{tag} poll advances the cursor", a("poll", "beta@conf").json() == [])
    sent = a("broadcast", "alpha@conf", "beta@conf", "question ?").json()
    reply = b("notify", "beta@conf", "alpha@conf", "réponse", "--reply-to", str(sent["broadcast_id"]))
    check(f"{tag} reply to a broadcast", reply.rc == 0, reply.out)
    answers = a("replies", str(sent["broadcast_id"])).json()
    check(f"{tag} replies lists it", [r["message"] for r in answers] == ["réponse"], str(answers))


def wake_up(a: Impl, b: Impl, tag: str) -> None:
    a("register", "alpha@conf")
    b("register", "beta@conf")
    watcher = b.popen("watch", "beta@conf", "--on", "notification", "--timeout", "60",
                      "--interval", "0.2")
    time.sleep(1.5)
    sent = time.monotonic()
    a("notify", "alpha@conf", "beta@conf", "debout")
    try:
        out, _ = watcher.communicate(timeout=30)
    except subprocess.TimeoutExpired:
        watcher.kill()
        out = b""
    delay = time.monotonic() - sent
    snapshot = out.decode("utf-8", errors="replace")
    check(f"{tag} a write wakes a blocking wait of the other implementation",
          watcher.returncode == 0 and '"notifications"' in snapshot and delay < 15,
          f"rc={watcher.returncode}, {delay:.1f}s, {snapshot[:200]}")


def machine_queue(a: Impl, b: Impl, tag: str) -> None:
    """A machine's general queue: registered on first watch, a wait over
    several queues, `new`/`since` marks, claim `--by`, explicit refusal."""
    a("register", "alpha@conf")
    b("register", "beta@conf")
    first = a("watch", "alpha@conf", "odin@general", "--on", "any", "--new",
              "--timeout", "0", "--interval", "0.2").json()
    check(f"{tag} a watch over two queues answers one snapshot per queue, and a mark",
          set(first.get("by", {})) == {"alpha@conf", "odin@general"}
          and first.get("signalled") is False and ":" in first.get("since", ""), str(first))
    b("push", "odin@general", "g1", "probe")
    seen = b("watch", "alpha@conf", "odin@general", "--on", "task", "--since", first["since"])
    check(f"{tag} --on task refuses a mark", refused(seen), seen.out)
    seen = b("watch", "alpha@conf", "odin@general", "--on", "any",
             "--since", first["since"], "--timeout", "5", "--interval", "0.2").json()
    check(f"{tag} the other implementation's push wakes a wait from the mark",
          seen.get("signalled") is True and seen["by"]["odin@general"]["tasks"] == ["g1"]
          and seen["by"]["alpha@conf"]["tasks"] == [], str(seen))
    again = a("watch", "alpha@conf", "odin@general", "--on", "any",
              "--since", seen["since"], "--timeout", "0", "--interval", "0.2").json()
    check(f"{tag} a wait from the answer's mark does not report it again",
          again.get("signalled") is False, str(again))
    claimed = a("claim", "odin@general", "--by", "alpha@conf").json()
    check(f"{tag} claim --by from the machine queue", claimed.get("task_id") == "g1", str(claimed))
    check(f"{tag} a refusal needs a reason",
          refused(b("decline", "odin@general", "g1", "--reason", " ")))
    out = b("decline", "odin@general", "g1", "--attempt", str(claimed.get("attempts")),
            "--by", "alpha@conf", "--reason", "pas pour moi — ✓")
    check(f"{tag} decline puts it back", ok_line(out) and "back" in out.out, out.out)
    check(f"{tag} the refusing agent is not offered it again",
          strip_newline(b("claim", "odin@general", "--by", "alpha@conf").out) == "NO_TASK")
    mine = a("signals", "odin@general").json()
    check(f"{tag} signals of the queue still show it", mine.get("tasks") == ["g1"], str(mine))
    other = a("claim", "odin@general", "--by", "beta@conf").json()
    check(f"{tag} another agent takes it, no attempt spent by the refusal",
          other.get("task_id") == "g1" and other.get("attempts") == 2, str(other))
    events = b("log", "g1").json()
    check(f"{tag} the refusal is journaled identically",
          any(e["event"] == "decline" and e["detail"]
              == "by alpha@conf: pas pour moi — ✓ — re-offered to the other agents"
              for e in events), str(events))
    a("push", "beta@conf", "p1", "x")
    taken = b("claim", "beta@conf").json()
    out = a("decline", "beta@conf", "p1", "--attempt", str(taken.get("attempts")),
            "--reason", "hors cadre")
    check(f"{tag} a personal refusal settles the task",
          ok_line(out) and b("result", "p1").json().get("content") == "DECLINED: hors cadre",
          out.out)


def payload(a: Impl, b: Impl, tag: str) -> None:
    a("register", "alpha@conf")
    pushed = a("push", "alpha@conf", "big", "-", stdin=PAYLOAD)
    check(f"{tag} push from stdin", ok_line(pushed), pushed.out[:200])
    task = b("claim", "alpha@conf").json()
    check(f"{tag} payload intact byte for byte (non-ASCII, CRLF, CR, 200 kB)",
          task.get("payload") == PAYLOAD,
          f"{len(task.get('payload', ''))} chars vs {len(PAYLOAD)}")
    b("publish", "alpha@conf", "big", "-", "--attempt", "1", stdin=PAYLOAD)
    check(f"{tag} result intact byte for byte", a("result", "big").json().get("content") == PAYLOAD)


def worker(a: Impl, b: Impl, tag: str) -> None:
    a("register", "alpha@conf")
    a("push", "alpha@conf", "w1", "-", stdin=PAYLOAD)
    ran = b("worker", "--agent", "alpha@conf", "--once", "--", *ECHO)
    check(f"{tag} worker runs one task", ran.rc == 0, ran.err[-300:])
    content = a("result", "w1").json().get("content")
    check(f"{tag} worker passes the payload intact and publishes the output intact",
          content == PAYLOAD, f"{len(content or '')} chars vs {len(PAYLOAD)}")


def worker_failures(a: Impl, b: Impl, tag: str) -> None:
    """A failing, silent or hung command publishes an ERROR: result."""
    a("register", "alpha@conf")
    check(f"{tag} worker --once on an empty queue exits 0",
          b("worker", "--agent", "alpha@conf", "--once", "--", *ECHO).rc == 0)
    a("push", "alpha@conf", "fail", "x")
    ran = b("worker", "--agent", "alpha@conf", "--once", "--", sys.executable, "-c",
            "import sys; sys.stderr.write('boom'); sys.exit(3)")
    content = a("result", "fail").json().get("content", "")
    check(f"{tag} non-zero exit published as ERROR with the stderr tail",
          ran.rc == 1 and content == "ERROR: worker command failed (exit 3). stderr: boom", content)
    a("push", "alpha@conf", "mute", "x")
    b("worker", "--agent", "alpha@conf", "--once", "--", sys.executable, "-c", "pass")
    content = a("result", "mute").json().get("content", "")
    check(f"{tag} empty output published as ERROR",
          content == "ERROR: worker command produced no output (exit 0). stderr: ", content)
    a("push", "alpha@conf", "hang", "x")
    started = time.monotonic()
    b("worker", "--agent", "alpha@conf", "--once", "--lease", "2", "--task-timeout", "1",
      "--", sys.executable, "-c", "import time; time.sleep(60)")
    content = a("result", "hang").json().get("content", "")
    check(f"{tag} task timeout kills the command and publishes ERROR",
          content.startswith("ERROR: worker command killed after task timeout 1s.")
          and time.monotonic() - started < 30, content)


def _roster(tmp: Path, entries: list[dict]) -> dict[str, str]:
    path = tmp / "roster.json"
    path.write_text(json.dumps({"agents": entries}), encoding="utf-8")
    return {"IAB_ROSTER": str(path)}


def _writer(target: Path) -> list[str]:
    """A launcher that keeps the prompt it receives on stdin."""
    return [sys.executable, "-c",
            f"import sys, pathlib; pathlib.Path({str(target)!r}).write_bytes(sys.stdin.buffer.read())"]


def wake(a: Impl, b: Impl, tag: str) -> None:
    """One decision: nothing waits, dry run, launch, budget, other host,
    lock held by a launch of the other implementation."""
    with tempfile.TemporaryDirectory() as tmp:
        tmp = Path(tmp)
        prompt = tmp / "prompt.txt"
        env = _roster(tmp, [
            {"name": "alpha@conf", "launch": {
                "command": _writer(prompt), "cwd": str(tmp), "max_wakes_per_hour": 1,
                "lease_seconds": 600, "prompt": "agent={agent} tasks={task_ids} lease={lease} {other}"}},
            {"name": "beta@conf", "launch": {"command": ["true"], "host": "elsewhere"}},
            {"name": "gamma@conf", "launch": {
                "command": [sys.executable, "-c", "import time; time.sleep(12)"], "cwd": str(tmp)}},
            {"name": "odin@general", "launch": {"command": ["true"], "after_seconds": 3600}},
            {"name": "late@conf", "launch": {"command": ["true"], "after_seconds": "soon"}},
        ])
        a("register", "alpha@conf")
        quiet = b("wake", "alpha@conf", env=env)
        check(f"{tag} nothing waits: NO_SIGNAL", quiet.rc == 0 and strip_newline(quiet.out) == "NO_SIGNAL",
              quiet.out)
        a("push", "alpha@conf", "t1", "x")
        dry = [impl("wake", "alpha@conf", "--dry-run", env=env).out for impl in (a, b)]
        check(f"{tag} dry run: same decision from both",
              dry[0] == dry[1] and dry[0].startswith('DRY_RUN: would launch ["')
              and "(reason: 1 queued task(s): t1; timeout 1200s; prompt 43 chars)" in dry[0], str(dry))
        woken = b("wake", "alpha@conf", env=env)
        check(f"{tag} launch with the rendered prompt on stdin",
              woken.rc == 0 and woken.out.startswith("OK: worker finished in")
              and prompt.read_text(encoding="utf-8") == "agent=alpha@conf tasks=t1 lease=600 {other}",
              woken.out + woken.err[-300:])
        wakes = [e["detail"] for e in a("log", "-a", "alpha@conf").json() if e["event"] == "wake"]
        check(f"{tag} journal: start then end",
              len(wakes) == 2 and wakes[0].startswith("start: 1 queued task(s): t1; ")
              and wakes[1].startswith("end: exit 0 after "), str(wakes))
        again = a("wake", "alpha@conf", env=env)
        check(f"{tag} budget spent: REFUSED by the other implementation",
              strip_newline(again.out) == "REFUSED: 1/1 wakes in the last hour — loop breaker.", again.out)
        a("register", "beta@conf")
        a("push", "beta@conf", "t2", "x")
        away = [impl("wake", "beta@conf", "--host", "here", env=env).out for impl in (a, b)]
        check(f"{tag} entry of another machine: NOT_HERE",
              away[0] == away[1]
              and strip_newline(away[0]) == "NOT_HERE: 'beta@conf' is launched on elsewhere, this is here.",
              str(away))
        missing = a("wake", "nobody@conf", env=env)
        check(f"{tag} no launch block: refused", refused(missing), missing.out)
        a("watch", "odin@general", "--on", "any", "--timeout", "0")
        a("push", "odin@general", "g9", "x")
        held = [impl("wake", "odin@general", "--dry-run", env=env).out for impl in (a, b)]
        check(f"{tag} after_seconds holds a fresh task back, in both",
              [strip_newline(h) for h in held] == ["NO_SIGNAL", "NO_SIGNAL"], str(held))
        a("register", "late@conf")
        a("push", "late@conf", "t9", "x")
        bad = [impl("wake", "late@conf", "--dry-run", env=env) for impl in (a, b)]
        check(f"{tag} a malformed after_seconds is refused identically",
              all(refused(r) for r in bad) and bad[0].out == bad[1].out, str([r.out for r in bad]))
        a("register", "gamma@conf")
        a("push", "gamma@conf", "t3", "x")
        first = a.popen("wake", "gamma@conf", env=env)
        time.sleep(5)  # the lock is taken once the first process has started
        busy = b("wake", "gamma@conf", env=env)
        first.communicate(timeout=60)
        check(f"{tag} lock held by the other implementation: BUSY",
              strip_newline(busy.out) == "BUSY" and first.returncode == 0, busy.out)


def supervise(a: Impl, b: Impl, tag: str) -> None:
    with tempfile.TemporaryDirectory() as tmp:
        tmp = Path(tmp)
        prompt = tmp / "prompt.txt"
        env = _roster(tmp, [{"name": "alpha@conf", "launch": {
            "command": _writer(prompt), "cwd": str(tmp), "prompt": "{task_ids}"}}])
        a("register", "alpha@conf")
        a("push", "alpha@conf", "t1", "x")
        ran = b("supervise", "--once", env=env)
        check(f"{tag} supervise --once wakes the agent a signal waits for",
              ran.rc == 0 and "supervising alpha@conf on " in ran.err
              and "alpha@conf: OK: worker finished in" in ran.err
              and prompt.read_text(encoding="utf-8") == "t1", ran.err[-400:])
        starts = [e for e in a("log", "-a", "alpha@conf").json()
                  if e["event"] == "wake" and e["detail"].startswith("start")]
        check(f"{tag} the launch is journaled", len(starts) == 1, str(starts))


def identity(a: Impl, b: Impl, tag: str) -> None:
    """Same folder, same identity and project key — accents included."""
    if shutil.which("git") is None:
        print(f"skip — {tag} identity (git not found)")
        return
    with tempfile.TemporaryDirectory() as tmp:
        repo = Path(tmp) / "Dépôt d'essai"
        sub = repo / "sous dossier"
        sub.mkdir(parents=True)
        subprocess.run(["git", "init", "-q", str(repo)], check=True)
        env = {"IAB_AGENT_NAME": "claude"}
        seen = [impl("whoami", cwd=sub, env=env).json() for impl in (a, b)]
        keys = ("agent_name", "harness", "workspace", "project_key")
        check(f"{tag} identity derived identically",
              [{k: s.get(k) for k in keys} for s in seen][0]
              == [{k: s.get(k) for k in keys} for s in seen][1], str(seen))
        check(f"{tag} identity composed from the repository",
              seen[0].get("agent_name") == f"claude@{seen[0].get('workspace')}"
              and bool(seen[0].get("workspace")), str(seen[0]))
        check(f"{tag} project key format", bool(PROJECT_KEY.match(seen[0].get("project_key", ""))),
              seen[0].get("project_key", ""))


SCENARIOS = [lifecycle, refusals, priority, fencing, dead_letter, contention,
             messages, wake_up, machine_queue, payload, worker, worker_failures, wake, supervise, identity]


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--impl", action="append", default=[], metavar="NAME=JSON_ARGV")
    parser.add_argument("-k", dest="only", default="", help="run scenarios whose name contains this")
    parser.add_argument("--without", action="append", default=[], metavar="NAME=SCENARIO[,…]",
                        help="scenarios an implementation does not offer yet")
    args = parser.parse_args()
    impls: dict[str, list[str]] = {}
    for spec in args.impl:
        name, _, argv = spec.partition("=")
        impls[name] = json.loads(argv)
    if not impls:
        impls["python"] = PYTHON_IMPL
    without: dict[str, set[str]] = {}
    for spec in args.without:
        name, _, scenarios = spec.partition("=")
        without.setdefault(name, set()).update(s for s in scenarios.split(",") if s)
    engine = "postgresql" if PG else "sqlite"
    print(f"engine: {engine}; implementations: {', '.join(impls)}")
    pairs = list(itertools.product(impls.items(), repeat=2))
    with tempfile.TemporaryDirectory() as tmp:
        cwd = Path(tmp) / "cwd"
        cwd.mkdir()
        for n, scenario in enumerate(SCENARIOS):
            if args.only and args.only not in scenario.__name__:
                continue
            for m, ((na, argv_a), (nb, argv_b)) in enumerate(pairs):
                tag = f"[{na}/{nb}] {scenario.__name__}:"
                missing = [x for x in dict.fromkeys((na, nb))
                           if scenario.__name__ in without.get(x, set())]
                if missing:
                    print(f"skip — {tag} not offered by {', '.join(missing)} yet")
                    continue
                db = fresh_db(tmp, f"{scenario.__name__}_{n}_{m}")
                try:
                    scenario(Impl(na, argv_a, db, cwd), Impl(nb, argv_b, db, cwd), tag)
                except Exception as exc:  # a crash is a failure, not an abort
                    check(f"{tag} ran to completion", False, f"{type(exc).__name__}: {exc}")
    print(f"conformance: {'OK' if not failures else f'{len(failures)} FAILURE(S)'}.")
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main())
