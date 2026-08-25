#!/usr/bin/env python3
"""End-to-end smoke test — the ROADMAP's global acceptance criterion,
executable on any platform: from a clean bus, push a task, have a
headless `iab worker` execute it, read the result, run a guarded
review on a diff, and consult the history — without a single
`python -c` against the store. Stand-in commands (this interpreter)
play the worker and reviewer CLIs so the script runs anywhere.

    python3 scripts/smoke.py        # py scripts\\smoke.py on Windows
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
CLI = [sys.executable, str(ROOT / "servers" / "shared_memory" / "cli.py")]

WORKER_STANDIN = "import sys; print('executed: ' + sys.stdin.read().strip())"
REVIEWER_STANDIN = (
    "import sys, re, json; t = sys.stdin.read(); "
    "n = re.search(r'Nonce: (\\w+)', t).group(1); "
    "print(json.dumps({'nonce': n, 'verdict': 'approve', 'findings': []}))"
)
DIFF = (
    "diff --git a/mod.py b/mod.py\n--- a/mod.py\n+++ b/mod.py\n"
    "@@ -1,2 +1,3 @@\n def f():\n+    return 1\n     pass\n"
)

env: dict[str, str] = {}


def run(step: str, args: list[str], stdin: str | None = None) -> str:
    done = subprocess.run(CLI + args, input=stdin, env=env,
                          capture_output=True, text=True)
    ok = done.returncode == 0
    print(("ok — " if ok else "FAIL: ") + step)
    if not ok:
        print(done.stdout)
        print(done.stderr)
        sys.exit(1)
    return done.stdout


with tempfile.TemporaryDirectory() as tmp:
    env = os.environ.copy()
    env["IAB_DB"] = str(Path(tmp) / "smoke.db")
    env.pop("ORCHESTRATOR_DB", None)

    run("register the worker agent", ["register", "kimi", "-d", "smoke stand-in"])
    run("push a task, payload on stdin", ["push", "kimi", "T1", "-"],
        stdin="do the thing")
    run("headless worker claims, executes, publishes",
        ["worker", "--agent", "kimi", "--once", "--",
         sys.executable, "-c", WORKER_STANDIN])
    out = run("read the result", ["result", "T1"])
    assert json.loads(out)["content"].strip() == "executed: do the thing", out

    diff_path = Path(tmp) / "change.diff"
    diff_path.write_text(DIFF, encoding="utf-8")
    out = run("guarded review of a diff",
              ["review", "--agent", "kimi", "--diff", str(diff_path),
               "--task-id", "R1", "--", sys.executable, "-c", REVIEWER_STANDIN])
    assert json.loads(out)["verdict"] == "approve", out

    out = run("consult the transition history", ["log", "T1"])
    assert [e["event"] for e in json.loads(out)] == ["push", "claim", "publish"], out

print("smoke: OK — push → worker → result → guarded review → log, "
      "all through the CLI.")
