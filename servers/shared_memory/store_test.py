"""Tests of the storage core — no MCP SDK required, throwaway database.

The crucial case is CROSS-PROCESS SHARING: two distinct processes (like two
agent clients each spawning their own stdio server) must see the same
state — that was the flaw of the first, in-memory version.

    python3 store_test.py
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
import time
from pathlib import Path

HERE = Path(__file__).parent
fail = 0


def check(label: str, ok: bool, detail: str = "") -> None:
    global fail
    print(("ok — " if ok else "FAIL: ") + label + (f" ({detail})" if detail and not ok else ""))
    if not ok:
        fail = 1


with tempfile.TemporaryDirectory() as tmp:
    os.environ["IAB_DB"] = str(Path(tmp) / "bus.db")
    import store

    # Unknown agent refused (typo detection).
    r = store.push_task("ghost", "t0", "x")
    check("push to an unregistered agent is refused", r.startswith("ERROR"))

    store.register_agent("alpha", "test")
    store.register_agent("beta", "test")

    # Priority: highest first.
    store.push_task("alpha", "t-low", "p1", priority=1)
    store.push_task("alpha", "t-high", "p9", priority=9)
    t = json.loads(store.claim_task("alpha"))
    check("highest-priority task is served first", t["task_id"] == "t-high")

    # Duplicate task_id refused.
    r = store.push_task("alpha", "t-low", "duplicate")
    check("duplicate task_id refused", r.startswith("ERROR"))

    # publish settles the task; the result is readable.
    store.publish_result("alpha", "t-high", "done")
    state = json.loads(store.get_system_state())
    check("publish_result settles the task", "t-high" in state["completed_tasks"])
    check("published result is readable",
          json.loads(store.read_result("t-high"))["content"] == "done")

    # Expired lease: the task is requeued (a crashed agent no longer loses it).
    store.push_task("beta", "t-crash", "x")
    json.loads(store.claim_task("beta", lease_seconds=1))
    time.sleep(1.2)
    t2 = json.loads(store.claim_task("beta"))
    check("an expired lease re-offers the task", t2["task_id"] == "t-crash")
    check("attempts are counted", t2["attempts"] == 2)

    # CROSS-PROCESS SHARING: another Python process pushes; this process
    # must see the task.
    code = (
        "import store; store.register_agent('gamma','other process'); "
        "print(store.push_task('gamma','t-ipc','from another process'))"
    )
    out = subprocess.run(
        [sys.executable, "-c", code],
        cwd=HERE, env=os.environ.copy(), capture_output=True, text=True,
    )
    check("a second process writes into the same bus", "OK" in out.stdout, out.stderr)
    t3 = json.loads(store.claim_task("gamma"))
    check("this process sees the task pushed by the other", t3["task_id"] == "t-ipc")

    # EVENT JOURNAL: every transition leaves a trace, readable per task.
    kinds = [e["event"] for e in json.loads(store.get_events(task_id="t-high"))]
    check("journal traces push → claim → publish",
          kinds == ["push", "claim", "publish"], str(kinds))
    kinds = [e["event"] for e in json.loads(store.get_events(task_id="t-crash"))]
    check("journal traces the lease expiry",
          kinds == ["push", "claim", "expire", "claim"], str(kinds))

    # CLAIM CONTENTION: more processes than tasks race on one queue —
    # each task must be claimed exactly once, the surplus sees NO_TASK.
    store.register_agent("delta", "contention")
    for i in range(4):
        store.push_task("delta", f"t-race-{i}", "x")
    procs = [
        subprocess.Popen(
            [sys.executable, "-c", "import store; print(store.claim_task('delta'))"],
            cwd=HERE, env=os.environ.copy(), stdout=subprocess.PIPE, text=True,
        )
        for _ in range(8)
    ]
    outs = [p.communicate()[0].strip() for p in procs]
    claimed = sorted(json.loads(o)["task_id"] for o in outs if o != "NO_TASK")
    check("contended claims: each task claimed exactly once",
          claimed == [f"t-race-{i}" for i in range(4)], str(outs))

    # CLI: same core, drivable without MCP and without python -c.
    cli = [sys.executable, str(HERE / "cli.py")]
    out = subprocess.run(cli + ["state"], env=os.environ.copy(),
                         capture_output=True, text=True)
    check("cli state returns the system state", '"agents"' in out.stdout, out.stderr)
    out = subprocess.run(cli + ["push", "delta", "t-cli", "-"],
                         input="payload via stdin — quotes ' \" and $vars intact",
                         env=os.environ.copy(), capture_output=True, text=True)
    check("cli push reads the payload from stdin",
          out.stdout.startswith("OK"), out.stdout + out.stderr)
    check("cli-pushed payload is intact",
          json.loads(store.claim_task("delta"))["payload"]
          == "payload via stdin — quotes ' \" and $vars intact")
    out = subprocess.run(cli + ["result", "t-missing"], env=os.environ.copy(),
                         capture_output=True, text=True)
    check("cli exits non-zero on ERROR output", out.returncode == 1, str(out.returncode))
    out = subprocess.run(cli + ["install", "--print", "--agent-name", "kimi"],
                         env=os.environ.copy(), capture_output=True, text=True)
    cfg = json.loads(out.stdout)
    check("cli install --print emits an absolute stdio registration",
          cfg["type"] == "stdio"
          and Path(cfg["command"]).is_absolute()
          and Path(cfg["args"][0]).is_absolute()
          and cfg["env"]["IAB_AGENT_NAME"] == "kimi",
          out.stdout + out.stderr)

    # Naming migration: legacy ORCHESTRATOR_DB honored, IAB_DB wins over it.
    os.environ["ORCHESTRATOR_DB"] = str(Path(tmp) / "legacy.db")
    check("IAB_DB wins over the legacy variable", store.db_path().name == "bus.db")
    del os.environ["IAB_DB"]
    check("legacy ORCHESTRATOR_DB honored when IAB_DB is unset",
          store.db_path().name == "legacy.db")
    del os.environ["ORCHESTRATOR_DB"]

    # MULTI-PROJECT ISOLATION (no env var): a per-project database is
    # derived from the working directory. The migration fallbacks are
    # monkeypatched to absent files so the operator's real bus is never
    # touched — everything below is path computation only.
    store.GLOBAL_DB = Path(tmp) / "absent-global.db"
    store.LEGACY_DB = Path(tmp) / "absent-legacy.db"
    proj_a = Path(tmp) / "project-a"
    proj_b = Path(tmp) / "project-b"
    proj_a.mkdir()
    proj_b.mkdir()
    pa, pb = store.db_path(proj_a), store.db_path(proj_b)
    check("two projects resolve two different databases", pa != pb, f"{pa} == {pb}")
    check("per-project databases land under projects/", pa.parent.name == "projects")
    check("the project key is stable across calls",
          store.project_key(proj_a) == store.project_key(proj_a))
    if os.name == "posix":
        link = Path(tmp) / "link-to-a"
        link.symlink_to(proj_a)
        check("a symlinked path resolves to the same project",
              store.project_key(link) == store.project_key(proj_a))

    # Acceptance: two sessions launched from two projects, no env var —
    # each one resolves its own bus (cwd-based, in child processes).
    env2 = os.environ.copy()
    env2["PYTHONPATH"] = str(HERE)
    probe = (
        "import store; from pathlib import Path; "
        f"store.GLOBAL_DB = Path({str(Path(tmp) / 'absent-global.db')!r}); "
        f"store.LEGACY_DB = Path({str(Path(tmp) / 'absent-legacy.db')!r}); "
        "print(store.db_path())"
    )
    outs = [
        subprocess.run([sys.executable, "-c", probe], cwd=d, env=env2,
                       capture_output=True, text=True).stdout.strip()
        for d in (proj_a, proj_b)
    ]
    check("two sessions in two projects resolve distinct buses",
          outs[0] != outs[1] and all(outs), str(outs))

    # Migration fallbacks: an existing legacy bus is kept, an existing
    # global bus wins over it, and IAB_DB overrides everything.
    store.LEGACY_DB.touch()
    check("an existing pre-rename bus is kept", store.db_path() == store.LEGACY_DB)
    store.GLOBAL_DB.touch()
    check("an existing global bus wins over the legacy one",
          store.db_path() == store.GLOBAL_DB)
    os.environ["IAB_DB"] = str(Path(tmp) / "explicit.db")
    check("IAB_DB overrides every fallback", store.db_path().name == "explicit.db")
    del os.environ["IAB_DB"]

if fail:
    print("store_test: FAIL.")
    sys.exit(1)
print("store_test: OK.")
