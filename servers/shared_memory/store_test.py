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

    # Naming migration: legacy ORCHESTRATOR_DB honored, IAB_DB wins over it.
    os.environ["ORCHESTRATOR_DB"] = str(Path(tmp) / "legacy.db")
    check("IAB_DB wins over the legacy variable", store.db_path().name == "bus.db")
    del os.environ["IAB_DB"]
    check("legacy ORCHESTRATOR_DB honored when IAB_DB is unset",
          store.db_path().name == "legacy.db")
    del os.environ["ORCHESTRATOR_DB"]

if fail:
    print("store_test: FAIL.")
    sys.exit(1)
print("store_test: OK.")
