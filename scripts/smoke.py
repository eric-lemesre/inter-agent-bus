#!/usr/bin/env python3
"""End-to-end smoke test — the ROADMAP's global acceptance criterion,
executable on any platform: from a clean bus, push a task, have a
headless `iab worker` execute it, read the result, run a guarded
review on a diff, and consult the history — without a single
`python -c` against the store. The same scenario then runs through a
hub on the loopback interface (when the [hub] extra is installed).
Stand-in commands (this interpreter) play the worker and reviewer CLIs
so the script runs anywhere.

    python3 scripts/smoke.py        # py scripts\\smoke.py on Windows
"""
from __future__ import annotations

import importlib.util
import json
import os
import socket
import subprocess
import sys
import tempfile
import time
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


def scenario(tmp: Path, where: str) -> None:
    run(f"register the worker agent ({where})", ["register", "kimi", "-d", "smoke stand-in"])
    run(f"push a task, payload on stdin ({where})", ["push", "kimi", "T1", "-"],
        stdin="do the thing")
    run(f"headless worker claims, executes, publishes ({where})",
        ["worker", "--agent", "kimi", "--once", "--",
         sys.executable, "-c", WORKER_STANDIN])
    out = run(f"read the result ({where})", ["result", "T1"])
    assert json.loads(out)["content"].strip() == "executed: do the thing", out

    diff_path = Path(tmp) / "change.diff"
    diff_path.write_text(DIFF, encoding="utf-8")
    out = run(f"guarded review of a diff ({where})",
              ["review", "--agent", "kimi", "--diff", str(diff_path),
               "--task-id", "R1", "--", sys.executable, "-c", REVIEWER_STANDIN])
    assert json.loads(out)["verdict"] == "approve", out

    out = run(f"consult the transition history ({where})", ["log", "T1"])
    assert [e["event"] for e in json.loads(out)] == ["push", "claim", "publish"], out


def free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


base = {k: v for k, v in os.environ.items()
        if k not in ("IAB_DB", "ORCHESTRATOR_DB", "IAB_HUB", "IAB_HUB_HOME",
                     "IAB_MACHINE_DIR")}

with tempfile.TemporaryDirectory() as tmp:
    env = {**base, "IAB_DB": str(Path(tmp) / "smoke.db")}
    scenario(Path(tmp), "local bus")

if importlib.util.find_spec("cryptography") is None:
    print("smoke: hub scenario skipped — the [hub] extra (cryptography) is not installed.")
else:
    with tempfile.TemporaryDirectory() as tmp:
        port = free_port()
        env = {**base, "IAB_HUB_HOME": str(Path(tmp) / "hub"),
               "IAB_MACHINE_DIR": str(Path(tmp) / "machine")}
        run("create a hub on the loopback interface",
            ["hub", "init", "--name", "127.0.0.1", "--machine", "smoke", "--port", str(port)])
        hub_proc = subprocess.Popen(CLI + ["hub", "serve", "--listen", f"127.0.0.1:{port}"],
                                    env=env, stdout=subprocess.DEVNULL,
                                    stderr=subprocess.DEVNULL)
        try:
            env["IAB_HUB"] = f"https://127.0.0.1:{port}"
            for _ in range(50):
                ping = subprocess.run(CLI + ["whoami"], env=env, capture_output=True, text=True)
                if '"db_source": "hub"' in ping.stdout and '"error"' not in ping.stdout:
                    break
                time.sleep(0.1)
            scenario(Path(tmp), "through the hub")

            # A second machine joins by invitation (the Ansible path: token
            # on stdin, never in argv) and works a task the first pushed.
            token = run("invite a second machine", ["invite", "second"]).strip()
            first = dict(env)
            env = {**first, "IAB_MACHINE_DIR": str(Path(tmp) / "second")}
            run("the second machine joins with the invitation",
                ["join", f"127.0.0.1:{port + 1}", "--name", "second", "--token", "-"],
                stdin=token + "\n")
            run("the second machine registers its own instance",
                ["register", "kimi@second", "-d", "worker on the second machine"])
            refused = subprocess.run(CLI + ["publish", "kimi", "T1", "forged"], env=env,
                                     capture_output=True, text=True)
            print(("ok — " if refused.returncode != 0 and "belongs to machine" in refused.stdout
                   else "FAIL: ") + "the second machine cannot act as the first one's agent")
            if refused.returncode == 0:
                sys.exit(1)
            env = first
            run("the first machine pushes work for the second",
                ["push", "kimi@second", "T2", "-"], stdin="cross-machine")
            env = {**first, "IAB_MACHINE_DIR": str(Path(tmp) / "second")}
            run("the second machine's worker executes it",
                ["worker", "--agent", "kimi@second", "--once", "--",
                 sys.executable, "-c", WORKER_STANDIN])
            env = first
            out = run("the first machine reads the result", ["result", "T2"])
            assert json.loads(out)["content"].strip() == "executed: cross-machine", out
            out = run("the admin lists both machines", ["machines"])
            assert {m["name"] for m in json.loads(out)} == {"smoke", "second"}, out
        finally:
            hub_proc.terminate()
            hub_proc.wait(timeout=10)

print("smoke: OK — push → worker → result → guarded review → log, "
      "all through the CLI, on the local bus and through a hub; "
      "a second machine paired by invitation works across machines.")
