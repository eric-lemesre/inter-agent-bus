"""Tests of the storage core — no MCP SDK required, throwaway database.

The crucial case is CROSS-PROCESS SHARING: two distinct processes (like two
agent clients each spawning their own stdio server) must see the same
state — that was the flaw of the first, in-memory version.

    python3 store_test.py
"""
from __future__ import annotations

import json
import os
import sqlite3
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
          and all(Path(a).is_absolute() for a in cfg.get("args", []))
          and cfg["env"]["IAB_AGENT_NAME"] == "kimi",
          out.stdout + out.stderr)

    # REVIEW DRIVER (iab review): diff embedded in the prompt, structured
    # output enforced, anti-hallucination guard on every finding.
    diff_file = Path(tmp) / "change.diff"
    diff_file.write_text(
        "diff --git a/mod.py b/mod.py\n--- a/mod.py\n+++ b/mod.py\n"
        "@@ -1,2 +1,3 @@\n def f():\n+    return 1\n     pass\n",
        encoding="utf-8",
    )
    good_reviewer = (
        "import sys, re, json; t = sys.stdin.read(); "
        "n = re.search(r'Nonce: (\\w+)', t).group(1); "
        "print(json.dumps({'nonce': n, 'verdict': 'request_changes', "
        "'findings': [{'file': 'mod.py', 'line': 2, 'severity': 'minor', "
        "'title': 't', 'detail': 'd'}]}))"
    )

    def run_review_cli(task_id, reviewer, extra_env=None):
        env = os.environ.copy()
        env.update(extra_env or {})
        return subprocess.run(
            cli + ["review", "--agent", "delta", "--diff", str(diff_file),
                   "--task-id", task_id, "--", sys.executable, "-c", reviewer],
            env=env, capture_output=True, text=True)

    out = run_review_cli("R-ok", good_reviewer)
    check("a structurally sound review passes the guard",
          out.returncode == 0, out.stdout + out.stderr)
    check("the verified review is published on the bus",
          json.loads(json.loads(store.read_result("R-ok"))["content"])["verdict"]
          == "request_changes")

    out = run_review_cli("R-halluc", good_reviewer.replace("'line': 2", "'line': 99"))
    r = json.loads(store.read_result("R-halluc"))["content"]
    check("a finding outside the diff hunks is rejected as ERROR",
          out.returncode == 1 and r.startswith("ERROR") and "line 99" in r, r)

    out = run_review_cli("R-prose", "print('looks good to me')")
    r = json.loads(store.read_result("R-prose"))["content"]
    check("free-prose review output is rejected (not JSON)",
          r.startswith("ERROR") and "not JSON" in r, r)

    out = run_review_cli("R-empty", "pass")
    r = json.loads(store.read_result("R-empty"))["content"]
    check("an empty successful review is rejected",
          r.startswith("ERROR") and "empty output" in r, r)

    out = run_review_cli(
        "R-nonce",
        good_reviewer.replace("'nonce': n", "'nonce': 'forged'"))
    r = json.loads(store.read_result("R-nonce"))["content"]
    check("a wrong nonce is rejected (liveness)",
          r.startswith("ERROR") and "nonce" in r, r)

    roster = Path(tmp) / "roster.json"
    roster.write_text(json.dumps(
        {"agents": [{"name": "delta", "context_window": 10}]}), encoding="utf-8")
    out = run_review_cli("R-window", good_reviewer,
                         extra_env={"IAB_ROSTER": str(roster)})
    check("a prompt beyond the roster context_window is refused, never truncated",
          out.returncode == 1 and "context_window" in out.stdout, out.stdout)
    check("the refused review never reached the bus",
          store.read_result("R-window").startswith("ERROR"))

    # HEADLESS WORKER (iab worker): claim → command with the payload on
    # stdin → publish stdout with the attempt token.
    store.register_agent("zeta", "worker tests")
    store.push_task("zeta", "t-w1", "please do X")
    out = subprocess.run(
        cli + ["worker", "--agent", "zeta", "--once", "--",
               sys.executable, "-c",
               "import sys; print('did: ' + sys.stdin.read().strip())"],
        env=os.environ.copy(), capture_output=True, text=True)
    check("worker --once settles the task", out.returncode == 0, out.stderr)
    check("worker publishes the command's stdout",
          json.loads(store.read_result("t-w1"))["content"].strip()
          == "did: please do X")

    # Failure discipline: non-zero exit and empty output become ERROR:
    # results (settled, not abandoned to lease expiry).
    store.push_task("zeta", "t-w2", "x")
    out = subprocess.run(
        cli + ["worker", "--agent", "zeta", "--once", "--",
               sys.executable, "-c",
               "import sys; sys.stderr.write('boom'); sys.exit(3)"],
        env=os.environ.copy(), capture_output=True, text=True)
    r = json.loads(store.read_result("t-w2"))["content"]
    check("worker turns a non-zero exit into an ERROR result",
          r.startswith("ERROR") and "exit 3" in r and "boom" in r, r)
    check("worker --once exits non-zero on an ERROR result",
          out.returncode == 1, str(out.returncode))
    store.push_task("zeta", "t-w3", "x")
    subprocess.run(cli + ["worker", "--agent", "zeta", "--once", "--",
                          sys.executable, "-c", "pass"],
                   env=os.environ.copy(), capture_output=True, text=True)
    r = json.loads(store.read_result("t-w3"))["content"]
    check("worker turns empty output into an ERROR result",
          r.startswith("ERROR") and "no output" in r, r)

    # Heartbeat: a command running longer than the lease is not
    # re-offered — the worker extends the lease while it runs.
    store.push_task("zeta", "t-w4", "x")
    out = subprocess.run(
        cli + ["worker", "--agent", "zeta", "--once", "--lease", "2", "--",
               sys.executable, "-c",
               "import sys, time; sys.stdin.read(); time.sleep(3); print('slow done')"],
        env=os.environ.copy(), capture_output=True, text=True)
    check("a long command still settles (heartbeat)",
          json.loads(store.read_result("t-w4"))["content"].strip() == "slow done",
          out.stderr)
    kinds = [e["event"] for e in json.loads(store.get_events(task_id="t-w4"))]
    check("the heartbeat extends the lease", "extend" in kinds, str(kinds))

    # Task timeout: a hung command is killed and reported, instead of
    # holding its task forever behind the heartbeat.
    store.push_task("zeta", "t-w5", "x")
    subprocess.run(
        cli + ["worker", "--agent", "zeta", "--once", "--lease", "2",
               "--task-timeout", "3", "--",
               sys.executable, "-c", "import sys, time; sys.stdin.read(); time.sleep(60)"],
        env=os.environ.copy(), capture_output=True, text=True)
    r = json.loads(store.read_result("t-w5"))["content"]
    check("a hung command is killed after --task-timeout",
          r.startswith("ERROR") and "timeout" in r, r)

    if os.name == "posix":
        check("the bus database is owner-only (0600)",
              (Path(os.environ["IAB_DB"]).stat().st_mode & 0o777) == 0o600)

    # LIFECYCLE — dead-letter: after max_attempts expired leases the task
    # is dead-lettered instead of being re-offered forever.
    store.register_agent("eps", "lifecycle")
    store.push_task("eps", "t-dead", "x", max_attempts=2)
    json.loads(store.claim_task("eps", lease_seconds=1))
    time.sleep(1.2)
    t = json.loads(store.claim_task("eps", lease_seconds=1))
    check("below max_attempts the task is re-offered", t["task_id"] == "t-dead")
    time.sleep(1.2)
    check("beyond max_attempts the queue is empty (dead-letter)",
          store.claim_task("eps") == "NO_TASK")
    kinds = [e["event"] for e in json.loads(store.get_events(task_id="t-dead"))]
    check("dead-lettering is journaled", kinds[-1] == "dead", str(kinds))

    # requeue revives a dead task, re-arming attempts (never rewinding them).
    check("requeue revives a dead task", store.requeue_task("t-dead").startswith("OK"))
    t = json.loads(store.claim_task("eps"))
    check("a revived task is claimable again",
          t["task_id"] == "t-dead" and t["attempts"] == 3, str(t))
    store.publish_result("eps", "t-dead", "done", attempt=t["attempts"])

    # Ownership on settle: another agent cannot settle someone else's task.
    store.push_task("eps", "t-own", "x")
    json.loads(store.claim_task("eps"))
    r = store.publish_result("alpha", "t-own", "hijack")
    check("settling someone else's task is refused", r.startswith("ERROR"), r)
    r = store.publish_result("alpha", "t-own", "override", force=True)
    check("force overrides the ownership check", r.startswith("OK"), r)
    r = store.publish_result("eps", "t-ghost", "x")
    check("settling an unknown task is refused", r.startswith("ERROR"), r)
    r = store.publish_result("eps", "t-ghost", "x", force=True)
    check("force stores a result for an unknown task", r.startswith("OK"), r)

    # Lease fencing: a stale worker (expired lease) cannot overwrite the
    # result of the worker that took the task over.
    store.push_task("eps", "t-fence", "x")
    t1 = json.loads(store.claim_task("eps", lease_seconds=1))
    time.sleep(1.2)
    t2 = json.loads(store.claim_task("eps"))
    r = store.publish_result("eps", "t-fence", "stale", attempt=t1["attempts"])
    check("a stale attempt token is refused", r.startswith("ERROR"), r)
    r = store.publish_result("eps", "t-fence", "fresh", attempt=t2["attempts"])
    check("the current attempt token settles", r.startswith("OK"), r)
    check("the fresh result stands",
          json.loads(store.read_result("t-fence"))["content"] == "fresh")

    # extend_lease: a renewed lease is not re-offered mid-flight.
    store.push_task("eps", "t-long", "x")
    t = json.loads(store.claim_task("eps", lease_seconds=1))
    store.extend_lease("t-long", 60)
    time.sleep(1.2)
    check("an extended lease is not re-offered", store.claim_task("eps") == "NO_TASK")
    store.publish_result("eps", "t-long", "done", attempt=t["attempts"])

    # Targeted claim: one specific queued task of one's own queue.
    store.push_task("eps", "t-a", "x")
    store.push_task("eps", "t-b", "x")
    t = json.loads(store.claim_task("eps", task_id="t-b"))
    check("targeted claim picks the requested task", t["task_id"] == "t-b")
    r = store.claim_task("alpha", task_id="t-a")
    check("cross-queue targeted claim is refused", r.startswith("ERROR"), r)
    r = store.claim_task("eps", task_id="t-b")
    check("targeting a non-queued task is refused", r.startswith("ERROR"), r)

    # cancel: a cancelled task leaves the queue for good.
    check("a queued task can be cancelled", store.cancel_task("t-a").startswith("OK"))
    check("a cancelled task is not served", store.claim_task("eps") == "NO_TASK")
    check("cancelling a settled task is refused",
          store.cancel_task("t-fence").startswith("ERROR"))

    # SCHEMA MIGRATION: a pre-phase-3 database (no max_attempts column,
    # no events table) keeps working — additive ALTER on first contact.
    old = Path(tmp) / "old-schema.db"
    c = sqlite3.connect(old)
    c.executescript(
        """
        CREATE TABLE agents (name TEXT PRIMARY KEY,
            description TEXT NOT NULL DEFAULT '', registered_at TEXT NOT NULL);
        CREATE TABLE tasks (task_id TEXT PRIMARY KEY,
            agent TEXT NOT NULL REFERENCES agents(name), payload TEXT NOT NULL,
            priority INTEGER NOT NULL DEFAULT 1,
            status TEXT NOT NULL DEFAULT 'queued',
            attempts INTEGER NOT NULL DEFAULT 0,
            created_at TEXT NOT NULL, claimed_at TEXT, lease_deadline TEXT);
        CREATE TABLE results (task_id TEXT PRIMARY KEY, author TEXT NOT NULL,
            content TEXT NOT NULL, updated_at TEXT NOT NULL);
        INSERT INTO agents VALUES('old', '', '2026-01-01T00:00:00+00:00');
        INSERT INTO tasks(task_id, agent, payload, created_at)
            VALUES('t-old', 'old', 'x', '2026-01-01T00:00:00+00:00');
        """
    )
    c.commit()
    c.close()
    os.environ["IAB_DB"] = str(old)
    t = json.loads(store.claim_task("old"))
    check("a pre-phase-3 database is migrated on contact", t["task_id"] == "t-old")
    check("a migrated task settles with the fencing token",
          store.publish_result("old", "t-old", "ok", attempt=t["attempts"]).startswith("OK"))
    os.environ["IAB_DB"] = str(Path(tmp) / "bus.db")

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
