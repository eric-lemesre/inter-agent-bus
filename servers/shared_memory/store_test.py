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

    # =====================================================================
    # PHASE 7 — presence and global channel (spec PRESENCE-CHANNEL.md).
    # Written by the architect BEFORE the implementation (three-hands
    # protocol): these checks are the closed contract of the feature.
    # =====================================================================
    from datetime import datetime, timedelta, timezone

    os.environ["IAB_DB"] = str(Path(tmp) / "phase7.db")

    # Injectable clock: every presence/channel code path goes through
    # store._now() only — liveness is computed at read time, never slept on.
    real_now = store._now
    clock = {"t": datetime(2026, 8, 26, 12, 0, 0, tzinfo=timezone.utc)}
    store._now = lambda: clock["t"].isoformat()

    store.register_agent("gamma", "presence test")
    store.register_agent("delta", "registered, no heartbeat")

    # Loud refusals.
    check("heartbeat with an empty agent is refused",
          store.heartbeat("").startswith("ERROR"))
    check("heartbeat with invalid capabilities JSON is refused",
          store.heartbeat("gamma", capabilities="not json").startswith("ERROR"))
    check("heartbeat with a non-object capabilities JSON is refused",
          store.heartbeat("gamma", capabilities="[1, 2]").startswith("ERROR"))

    # Nominal: card posted, liveness horizon returned.
    r = json.loads(store.heartbeat(
        "gamma", ttl_seconds=120,
        capabilities='{"limits": {"max_inline_payload_bytes": 8192}}'))
    check("heartbeat returns the liveness horizon", "alive_until" in r)
    rows = {a["agent"]: a for a in json.loads(store.list_presence())}
    check("a fresh heartbeat reads alive", rows["gamma"]["status"] == "alive")
    check("the capability card is served back",
          rows["gamma"]["capabilities"]["limits"]["max_inline_payload_bytes"] == 8192)
    check("a registered agent without presence reads unknown",
          rows["delta"]["status"] == "unknown")

    # Liveness expires by the clock alone — no daemon, no sleep.
    clock["t"] += timedelta(seconds=121)
    rows = {a["agent"]: a for a in json.loads(store.list_presence())}
    check("an expired presence reads asleep", rows["gamma"]["status"] == "asleep")

    # touch_presence: refreshes last_seen, keeps TTL and card; creates a
    # default-TTL row for an agent without presence (piggyback never fails).
    store.touch_presence("gamma")
    store.touch_presence("delta")
    rows = {a["agent"]: a for a in json.loads(store.list_presence())}
    check("touch wakes a known agent up", rows["gamma"]["status"] == "alive")
    check("touch keeps the capability card",
          rows["gamma"]["capabilities"]["limits"]["max_inline_payload_bytes"] == 8192)
    check("touch creates a default presence for a new agent",
          rows["delta"]["status"] == "alive")
    clock["t"] += timedelta(seconds=121)
    rows = {a["agent"]: a for a in json.loads(store.list_presence())}
    check("touch did not change the declared TTL", rows["gamma"]["status"] == "asleep")

    # get_system_state exposes the computed presence statuses.
    state = json.loads(store.get_system_state())
    check("get_system_state carries presence statuses",
          state.get("presence", {}).get("gamma") == "asleep")

    # Channel: loud refusals.
    check("an invalid topic is refused",
          store.announce("gamma", "Bad Topic!", "x").startswith("ERROR"))
    check("an oversized message is refused",
          store.announce("gamma", "alerts", "x" * (16 * 1024 + 1)).startswith("ERROR"))

    # Append + pure reads (no cursor touched).
    s1 = json.loads(store.announce("gamma", "presence", '{"name": "gamma"}'))["seq"]
    s2 = json.loads(store.announce("gamma", "config", "roster digest"))["seq"]
    s3 = json.loads(store.announce("delta", "presence", '{"name": "delta"}'))["seq"]
    check("channel sequence is monotonic", s1 < s2 < s3)
    msgs = json.loads(store.read_channel(since_seq=0))
    check("a pure read returns everything in order",
          [m["seq"] for m in msgs] == [s1, s2, s3])
    msgs = json.loads(store.read_channel(since_seq=0, topic="presence"))
    check("a pure read filters by topic",
          [m["seq"] for m in msgs] == [s1, s3]
          and all(m["topic"] == "presence" for m in msgs))

    # Cursor semantics: agent reads advance to the last returned seq.
    check("cursor reads refuse a topic filter (at-least-once)",
          store.read_channel(agent="gamma", topic="config").startswith("ERROR"))
    batch = json.loads(store.read_channel(agent="gamma", limit=2))
    check("a cursor read starts from the beginning",
          [m["seq"] for m in batch] == [s1, s2])
    batch = json.loads(store.read_channel(agent="gamma"))
    check("the cursor advanced to the last returned seq",
          [m["seq"] for m in batch] == [s3])
    check("an empty cursor read returns nothing",
          json.loads(store.read_channel(agent="gamma")) == [])
    s4 = json.loads(store.announce("delta", "handoff", "later"))["seq"]
    batch = json.loads(store.read_channel(agent="gamma"))
    check("an empty read advanced nothing — the next message is served",
          [m["seq"] for m in batch] == [s4])
    check("pure reads did not move the other agent's cursor",
          [m["seq"] for m in json.loads(store.read_channel(agent="delta", limit=1))]
          == [s1])

    # Cross-process sharing: an announcement written by another process is
    # read here (same pattern as the historical cross-process check).
    env7 = os.environ.copy()
    env7["PYTHONPATH"] = str(HERE)
    probe7 = "import store; print(store.announce('delta', 'alerts', 'from-child'))"
    out = subprocess.run([sys.executable, "-c", probe7], env=env7,
                         capture_output=True, text=True).stdout.strip()
    check("another process can announce on the channel", out.startswith("{"))
    msgs = json.loads(store.read_channel(since_seq=s4))
    check("the child's announcement is visible here",
          any(m["message"] == "from-child" for m in msgs))

    # Directed notifications: point-to-point signal + cursor poll.
    check("notify to an unregistered agent is refused",
          store.notify("gamma", "ghost", "x").startswith("ERROR"))
    check("notify with an empty author is refused",
          store.notify("", "gamma", "x").startswith("ERROR"))
    check("notify with an empty target is refused",
          store.notify("gamma", "", "x").startswith("ERROR"))
    check("notify with an oversized message is refused",
          store.notify("gamma", "delta", "x" * (16 * 1024 + 1)).startswith("ERROR"))

    n1 = json.loads(store.notify("gamma", "delta", "tâche à regarder"))["seq"]
    n2 = json.loads(store.notify("gamma", "gamma", "own note"))["seq"]
    check("notification sequence is monotonic", n1 < n2)
    check("poll serves only the recipient's own notifications",
          [m["seq"] for m in json.loads(store.poll("delta"))] == [n1])
    check("another agent polls only its own notifications",
          [m["seq"] for m in json.loads(store.poll("gamma"))] == [n2])
    check("an empty poll returns nothing",
          json.loads(store.poll("delta")) == [])
    n3 = json.loads(store.notify("gamma", "delta", "after empty poll"))["seq"]
    check("an empty poll advanced nothing — the next signal is served",
          [m["seq"] for m in json.loads(store.poll("delta"))] == [n3])

    # Cross-process: a notification written by another process is polled here.
    env8 = os.environ.copy()
    env8["PYTHONPATH"] = str(HERE)
    probe8 = ("import store; store.register_agent('child', 'other process'); "
              "print(store.notify('child', 'delta', 'from-child'))")
    out = subprocess.run([sys.executable, "-c", probe8], env=env8,
                         capture_output=True, text=True).stdout.strip()
    check("another process can notify", out.startswith("{"))
    check("the child's notification is polled here",
          any(m["message"] == "from-child" for m in json.loads(store.poll("delta"))))

    # Task-watch: push_task drops a wake-up notification in the target's
    # mailbox (and only there), unless notify=False; wait_for_task blocks
    # until a task lands, and wakes on a push from another process.
    store.register_agent("epsilon", "watch tests")
    store.push_task("epsilon", "wake-1", "payload")
    wakes = json.loads(store.poll("epsilon"))
    check("push_task notifies the target",
          any("wake-1" in m["message"] for m in wakes))
    check("the wake-up hint is authored by the bus",
          all(m["author"] == "bus" for m in wakes))
    check("push_task does not notify other agents",
          not any("wake-1" in m["message"] for m in json.loads(store.poll("gamma"))))
    store.push_task("epsilon", "wake-2", "payload", notify=False)
    check("notify=False suppresses the wake-up hint",
          json.loads(store.poll("epsilon")) == [])

    check("wait_for_task returns queued ids immediately when the queue holds tasks",
          set(json.loads(store.wait_for_task("epsilon", timeout_s=5))) == {"wake-1", "wake-2"})
    check("wait_for_task on an empty queue times out with an empty list",
          json.loads(store.wait_for_task("delta", timeout_s=0.5, interval_s=0.2)) == [])
    check("wait_for_task refuses an empty agent",
          store.wait_for_task("").startswith("ERROR"))

    # Cross-process wake: a child pushes after a delay; the parent's wait
    # returns the id well before its timeout.
    env9 = os.environ.copy()
    env9["PYTHONPATH"] = str(HERE)
    probe9 = ("import time, store; time.sleep(1.0); "
              "print(store.push_task('delta', 'wake-3', 'late push'))")
    child = subprocess.Popen([sys.executable, "-c", probe9], env=env9,
                             stdout=subprocess.PIPE, text=True)
    t0 = time.monotonic()
    woken = json.loads(store.wait_for_task("delta", timeout_s=15, interval_s=0.2))
    elapsed = time.monotonic() - t0
    child.wait(timeout=15)
    check("wait_for_task wakes on a push from another process",
          woken == ["wake-3"])
    check("the wake happens before the timeout, not at it", elapsed < 10)


    # ---- Phase 8a: composed identity harness@workspace, declared model.
    # Workspace derivation: enclosing git repository (directory or file, so
    # a submodule yields its own name), bare folder, alias table, case.
    ws_root = Path(tmp) / "ws"
    (ws_root / "Home Lab" / ".git").mkdir(parents=True)
    (ws_root / "Home Lab" / "infra").mkdir()
    (ws_root / "Home Lab" / "infra" / ".git").write_text("gitdir: ../.git/modules/infra")
    (ws_root / "Home Lab" / "docs" / "deep").mkdir(parents=True)
    (ws_root / "Loose").mkdir()
    check("workspace = name of the enclosing git repository, slugified",
          store.derive_workspace(ws_root / "Home Lab" / "docs" / "deep") == ("home-lab", "git"))
    check("a submodule (.git file) yields its own name, not the superproject's",
          store.derive_workspace(ws_root / "Home Lab" / "infra") == ("infra", "git"))
    check("without a repository, the folder's own name",
          store.derive_workspace(ws_root / "Loose") == ("loose", "cwd"))
    aliases = Path(tmp) / "workspaces.json"
    aliases.write_text(json.dumps({str(ws_root / "Home Lab"): "homelab",
                                   str(ws_root / "Home Lab" / "infra"): "Infra!",
                                   str(ws_root): "poste"}))
    os.environ["IAB_WORKSPACES"] = str(aliases)
    check("alias table wins, longest prefix first, value slugified",
          store.derive_workspace(ws_root / "Home Lab" / "infra" ) == ("infra", "alias")
          and store.derive_workspace(ws_root / "Home Lab" / "docs") == ("homelab", "alias"))
    check("an alias on a parent folder applies to loose folders beneath it",
          store.derive_workspace(ws_root / "Loose") == ("poste", "alias"))
    (ws_root / "Other Repo" / ".git").mkdir(parents=True)
    check("…but never swallows a repository beneath it",
          store.derive_workspace(ws_root / "Other Repo") == ("other-repo", "git"))
    aliases.write_text(json.dumps({str(ws_root / "Home Lab"): "homelab"}))
    check("a repository alias stops at a submodule boundary",
          store.derive_workspace(ws_root / "Home Lab" / "infra") == ("infra", "git"))
    del os.environ["IAB_WORKSPACES"]

    ident = store.resolve_identity({"IAB_AGENT_NAME": "Claude"}, ws_root / "Home Lab" / "docs")
    check("identity = harness from env + workspace from the launch directory",
          ident["agent_name"] == "claude@home-lab" and ident["workspace_source"] == "git"
          and ident["model"] is None, json.dumps(ident))
    ident = store.resolve_identity({"IAB_AGENT_NAME": "kimi", "IAB_WORKSPACE": "Brother",
                                    "IAB_MODEL": "kimi-k2"}, ws_root / "Loose")
    check("IAB_WORKSPACE overrides the derivation; IAB_MODEL is echoed",
          ident["agent_name"] == "kimi@brother" and ident["workspace_source"] == "env"
          and ident["model"] == "kimi-k2", json.dumps(ident))
    ident = store.resolve_identity({"IAB_AGENT_NAME": "claude@conciliateur"}, ws_root / "Loose")
    check("a full name in the env wins over the directory",
          ident["agent_name"] == "claude@conciliateur" and ident["workspace_source"] == "name")
    ident = store.resolve_identity({}, ws_root / "Loose")
    check("no harness in the env → source none, no name",
          ident["source"] == "none" and ident["agent_name"] is None)

    # Registration and addressing.
    check("an invalid composed name is refused",
          store.register_agent("claude@home lab").startswith("ERROR")
          and store.register_agent("@homelab").startswith("ERROR"))
    store.register_agent("claude@homelab", "HomeLab session", cwd="/x/HomeLab",
                         model="claude-fable-5-1")
    store.register_agent("claude@leadsage", "LeadSage session")
    store.register_agent("kimi@brother", "Kimi on Brother")
    pres = {p["agent"]: p for p in json.loads(store.list_presence())}
    check("list_presence shows harness, workspace, cwd and declared model",
          pres["claude@homelab"]["harness"] == "claude"
          and pres["claude@homelab"]["workspace"] == "homelab"
          and pres["claude@homelab"]["cwd"] == "/x/HomeLab"
          and pres["claude@homelab"]["model"] == "claude-fable-5-1"
          and pres["kimi@brother"]["model"] is None, json.dumps(pres["claude@homelab"]))
    r = store.notify("kimi@brother", "claude", "which one of you?")
    check("a bare harness with several instances is refused with the candidates",
          r.startswith("ERROR") and "claude@homelab" in r and "claude@leadsage" in r, r)
    r = store.notify("claude@homelab", "kimi", "single instance")
    check("a bare harness with one instance resolves to it", r.startswith("{"), r)
    check("…and lands in that instance's mailbox",
          any(m["message"] == "single instance" for m in json.loads(store.poll("kimi@brother"))))
    check("poll also accepts the bare harness when unique",
          store.poll("kimi") == "[]")
    check("a wildcard is refused as a target (reserved to broadcast)",
          store.notify("kimi@brother", "claude@*", "x").startswith("ERROR")
          and store.push_task("*@homelab", "t-bc", "x").startswith("ERROR"))
    check("push_task resolves a bare harness too",
          store.push_task("kimi", "t-kimi", "x").startswith("OK")
          and json.loads(store.claim_task("kimi@brother"))["task_id"] == "t-kimi")
    check("an unregistered author is refused on announce and notify",
          store.announce("nobody", "alerts", "x").startswith("ERROR")
          and store.notify("nobody", "kimi@brother", "x").startswith("ERROR"))
    check("polling an unregistered name is an error, not an empty list",
          store.poll("claude-homelab").startswith("ERROR"))
    check("a cursor read of the channel by an unregistered name is refused",
          store.read_channel(agent="claude-homelab").startswith("ERROR"))
    check("claiming for an unregistered name is an error",
          store.claim_task("claude-homelab").startswith("ERROR"))
    check("wait_for_task refuses an unregistered name",
          store.wait_for_task("claude-homelab", timeout_s=0.2).startswith("ERROR"))
    check("two instances of one harness have separate queues",
          store.push_task("claude@homelab", "t-hl", "x").startswith("OK")
          and store.claim_task("claude@leadsage") == "NO_TASK"
          and json.loads(store.claim_task("claude@homelab"))["task_id"] == "t-hl")
    store.notify("kimi@brother", "claude@homelab", "for homelab only")
    check("two instances of one harness have separate notification cursors",
          json.loads(store.poll("claude@leadsage")) == []
          and any(m["message"] == "for homelab only"
                  for m in json.loads(store.poll("claude@homelab"))))

    # Model: session state in the presence row.
    store.touch_presence("claude@homelab", model="from-env")
    check("touch_presence never overrides a declared model",
          json.loads(store.heartbeat("claude@homelab"))["model"] == "claude-fable-5-1")
    store.touch_presence("claude@leadsage", model="from-env")
    check("touch_presence fills an empty model slot",
          json.loads(store.heartbeat("claude@leadsage"))["model"] == "from-env")
    check("heartbeat changes the declared model",
          json.loads(store.heartbeat("claude@leadsage", model="claude-opus-5-5"))["model"]
          == "claude-opus-5-5")
    check("heartbeat for an unregistered name is refused",
          store.heartbeat("ghost@nowhere").startswith("ERROR"))
    state = json.loads(store.get_system_state())
    check("get_system_state exposes instance fields and models",
          state["agents"]["claude@homelab"]["workspace"] == "homelab"
          and state["models"]["claude@leadsage"] == "claude-opus-5-5")

    # Piggyback of a connected client: auto-registration under the composed name.
    store.touch_identity({"agent_name": "codewhale@leadsage", "cwd": "/x/LeadSage",
                          "model": "deepseek-v4"})
    pres = {p["agent"]: p for p in json.loads(store.list_presence())}
    check("touch_identity registers an unknown instance with cwd and model echo",
          pres["codewhale@leadsage"]["cwd"] == "/x/LeadSage"
          and pres["codewhale@leadsage"]["model"] == "deepseek-v4"
          and pres["codewhale@leadsage"]["status"] == "alive")
    check("the auto-registration is journaled",
          any(e["event"] == "register" and "auto" in e["detail"]
              for e in json.loads(store.get_events(agent="codewhale@leadsage"))))

    # rename_agent: the whole history follows, in one transaction.
    store.register_agent("legacy", "old bare name")
    store.push_task("legacy", "t-leg-1", "x")
    json.loads(store.claim_task("legacy"))
    store.publish_result("legacy", "t-leg-1", "done")
    store.push_task("legacy", "t-leg-2", "y")
    store.notify("kimi@brother", "legacy", "to legacy")
    store.announce("legacy", "presence", "hello")
    store.heartbeat("legacy", model="m1")
    check("rename refuses an invalid or existing new name",
          store.rename_agent("legacy", "claude@homelab").startswith("ERROR")
          and store.rename_agent("legacy", "bad name").startswith("ERROR")
          and store.rename_agent("ghost", "x@y").startswith("ERROR"))
    check("rename succeeds", store.rename_agent("legacy", "claude@conciliateur").startswith("OK"))
    check("the old name is gone", store.poll("legacy").startswith("ERROR"))
    check("the queue followed the rename",
          json.loads(store.claim_task("claude@conciliateur"))["task_id"] == "t-leg-2")
    check("the mailbox followed the rename",
          any(m["message"] == "to legacy" for m in json.loads(store.poll("claude@conciliateur"))))
    check("results, channel and presence followed the rename",
          json.loads(store.read_result("t-leg-1"))["author"] == "claude@conciliateur"
          and any(e["author"] == "claude@conciliateur"
                  for e in json.loads(store.read_channel(since_seq=0, topic="presence")))
          and json.loads(store.heartbeat("claude@conciliateur"))["model"] == "m1")
    check("the rename is journaled",
          any(e["event"] == "rename" and e["detail"] == "was legacy"
              for e in json.loads(store.get_events(agent="claude@conciliateur"))))

    # Legacy database (pre-8a schema) opens and migrates additively.
    legacy_db = Path(tmp) / "legacy.db"
    raw = sqlite3.connect(legacy_db)
    raw.executescript(
        "CREATE TABLE agents(name TEXT PRIMARY KEY, description TEXT NOT NULL DEFAULT '', "
        "registered_at TEXT NOT NULL);"
        "INSERT INTO agents VALUES('claude', 'old row', '2026-08-25T05:23:06+00:00');"
        "INSERT INTO agents VALUES('kimi@brother', 'composed row', '2026-10-01T00:00:00+00:00');"
        "CREATE TABLE presence(agent TEXT PRIMARY KEY, last_seen TEXT NOT NULL, "
        "ttl_seconds INTEGER NOT NULL, capabilities TEXT NOT NULL DEFAULT '{}');"
        "INSERT INTO presence VALUES('claude', '2026-10-16T05:00:00+00:00', 7200, '{}');"
    )
    raw.close()
    saved_db = os.environ["IAB_DB"]
    os.environ["IAB_DB"] = str(legacy_db)
    pres = {p["agent"]: p for p in json.loads(store.list_presence())}
    check("a pre-8a database is migrated: harness backfilled from the name",
          pres["claude"]["harness"] == "claude" and pres["claude"]["workspace"] is None
          and pres["kimi@brother"]["harness"] == "kimi"
          and pres["kimi@brother"]["workspace"] == "brother"
          and pres["claude"]["model"] is None, json.dumps(pres))
    check("a legacy bare name stays addressable while it is the only instance",
          store.notify("kimi@brother", "claude", "still works").startswith("{"))
    store.register_agent("claude@homelab", "new instance")
    r = store.notify("kimi@brother", "claude", "now ambiguous")
    check("…and becomes ambiguous once a composed instance appears",
          r.startswith("ERROR") and "claude@homelab" in r, r)
    check("the error names the legacy row and the way out (rename)",
          "rename_agent" in r and store.poll("claude").startswith("ERROR"))
    store.rename_agent("claude", "claude@conciliateur")
    check("after the rename, the legacy mailbox is reachable under its new name",
          json.loads(store.poll("claude@conciliateur"))[0]["message"] == "still works"
          and store.notify("kimi@brother", "claude", "x").startswith("ERROR"))
    os.environ["IAB_DB"] = saved_db

    # CLI: whoami carries the composed identity; register/rename/heartbeat --model.
    env_cli = os.environ.copy()
    env_cli.update({"IAB_AGENT_NAME": "opencode", "IAB_MODEL": "qwen3-coder"})
    out = subprocess.run(cli + ["whoami"], env=env_cli, cwd=ws_root / "Loose",
                         capture_output=True, text=True)
    who = json.loads(out.stdout)
    check("cli whoami composes harness@workspace from env and cwd and echoes the model",
          who["agent_name"] == "opencode@loose" and who["model"] == "qwen3-coder"
          and who["workspace_source"] == "cwd", out.stdout + out.stderr)
    out = subprocess.run(cli + ["register", "opencode@sindri", "--model", "qwen3", "--cwd", "/srv"],
                         env=os.environ.copy(), capture_output=True, text=True)
    check("cli register accepts --model and --cwd", out.stdout.startswith("OK"), out.stdout)
    out = subprocess.run(cli + ["rename", "opencode@sindri", "opencode@vm"],
                         env=os.environ.copy(), capture_output=True, text=True)
    check("cli rename works", out.stdout.startswith("OK"), out.stdout + out.stderr)
    out = subprocess.run(cli + ["heartbeat", "opencode@vm", "--model", "qwen3.6"],
                         env=os.environ.copy(), capture_output=True, text=True)
    check("cli heartbeat --model declares the model",
          json.loads(out.stdout)["model"] == "qwen3.6", out.stdout + out.stderr)


    store._now = real_now

    # ---- Phase 8b: broadcast — fan-out into the existing mailboxes.
    # Fresh instances for the scenario (the earlier ones carry history).
    for n in ("claude@homelab", "claude@leadsage", "kimi@brother", "codewhale@leadsage"):
        store.register_agent(n, "8b")
    store.register_agent("opencode@homelab", "8b", model="qwen3-coder-next")
    store.heartbeat("claude@homelab", ttl_seconds=600)
    store.heartbeat("kimi@brother", ttl_seconds=600)
    store.heartbeat("opencode@homelab", ttl_seconds=600, model="qwen3-coder-next")
    for n in ("claude@leadsage", "codewhale@leadsage", "claude@conciliateur", "opencode@vm"):
        store.heartbeat(n, ttl_seconds=0)  # seen, but already expired: asleep
    # drain the mailboxes so the scenario reads only its own messages
    for n in ("claude@homelab", "claude@leadsage", "kimi@brother", "codewhale@leadsage",
              "opencode@homelab"):
        store.poll(n)

    r = store.broadcast("nobody", "x")
    check("broadcast refuses an unregistered author", r.startswith("ERROR"), r)
    r = store.broadcast("kimi@brother", "x", scope="*@nowhere")
    check("broadcast refuses an empty scope", r.startswith("ERROR"), r)
    r = store.broadcast("kimi@brother", "x", scope="claude@homelab,ghost@x")
    check("one unresolvable entry refuses the whole broadcast (atomic)",
          r.startswith("ERROR")
          and not any("x" == m["message"] for m in json.loads(store.poll("claude@homelab"))), r)

    b = json.loads(store.broadcast("kimi@brother", "who touched roles/mail_print?",
                                   scope="claude@*"))
    check("harness scope reaches every instance of the harness",
          set(b["recipients"]) == {"claude@homelab", "claude@leadsage", "claude@conciliateur"},
          json.dumps(b))
    check("delivered/asleep split follows presence",
          "claude@homelab" in b["delivered"] and "claude@leadsage" in b["asleep"]
          and b["to_wake"] == [], json.dumps(b))
    inbox = json.loads(store.poll("claude@homelab"))
    check("the recipient reads the broadcast with its usual poll, tagged with the id",
          any(m["broadcast_id"] == b["broadcast_id"]
              and m["message"] == "who touched roles/mail_print?" for m in inbox),
          json.dumps(inbox))
    chan = json.loads(store.read_channel(since_seq=b["broadcast_id"] - 1, topic="broadcast"))
    check("the broadcast leaves a public trace on the channel under its id",
          chan and chan[0]["seq"] == b["broadcast_id"] and chan[0]["author"] == "kimi@brother")
    check("…and is journaled", any(e["event"] == "broadcast" and f"id {b['broadcast_id']}" in e["detail"]
                                   for e in json.loads(store.get_events(agent="kimi@brother"))))

    r = store.notify("claude@homelab", "kimi@brother", "me, on 10/15", reply_to=999999)
    check("a reply to a non-broadcast id is refused", r.startswith("ERROR"), r)
    store.notify("claude@homelab", "kimi@brother", "me, on 10/15", reply_to=b["broadcast_id"])
    store.notify("claude@leadsage", "kimi@brother", "not me", reply_to=b["broadcast_id"])
    store.notify("claude@leadsage", "kimi@brother", "unrelated note")
    rep = json.loads(store.replies(b["broadcast_id"]))
    check("replies collects only the answers to that broadcast, oldest first",
          [m["message"] for m in rep] == ["me, on 10/15", "not me"], json.dumps(rep))
    kimi_inbox = json.loads(store.poll("kimi@brother"))
    check("replies also arrive in the asker's mailbox with reply_to set",
          sum(1 for m in kimi_inbox if m["reply_to"] == b["broadcast_id"]) == 2)
    check("replies refuses a non-broadcast id", store.replies(1).startswith("ERROR"))

    b2 = json.loads(store.broadcast("claude@homelab", "freeze at 07:45", scope="*@homelab"))
    check("workspace scope reaches every agent of the workspace, author included",
          set(b2["recipients"]) == {"claude@homelab", "opencode@homelab"}, json.dumps(b2))
    b3 = json.loads(store.broadcast("claude@homelab", "local models only", scope="model:qwen3-coder*"))
    check("model scope matches declared models by glob",
          b3["recipients"] == ["opencode@homelab"], json.dumps(b3))
    b4 = json.loads(store.broadcast("claude@homelab", "alive?", scope="*", alive_only=True))
    check("alive_only drops asleep recipients",
          set(b4["recipients"]) <= {"claude@homelab", "kimi@brother", "opencode@homelab"}
          and "claude@leadsage" not in b4["recipients"], json.dumps(b4))
    b5 = json.loads(store.broadcast("claude@homelab", "wake up", scope="claude@leadsage,codewhale",
                                    wake=True))
    check("list scope resolves bare harnesses and marks asleep recipients to wake",
          set(b5["recipients"]) == {"claude@leadsage", "codewhale@leadsage"}
          and set(b5["to_wake"]) == {"claude@leadsage", "codewhale@leadsage"}, json.dumps(b5))
    check("the wake flag travels with the notification",
          any(m["wake"] and m["broadcast_id"] == b5["broadcast_id"]
              for m in json.loads(store.poll("codewhale@leadsage"))))
    check("a JSON list scope is accepted too",
          json.loads(store.broadcast("kimi@brother", "x", scope='["claude@homelab"]'))["recipients"]
          == ["claude@homelab"])
    check("broadcast caps the message at 16 KiB",
          store.broadcast("kimi@brother", "x" * (16 * 1024 + 1)).startswith("ERROR"))

    out = subprocess.run(cli + ["broadcast", "kimi@brother", "claude@*", "-"],
                         input="via cli", env=os.environ.copy(), capture_output=True, text=True)
    check("cli broadcast reads the message from stdin and returns the id",
          "broadcast_id" in out.stdout, out.stdout + out.stderr)
    bid = json.loads(out.stdout)["broadcast_id"]
    out = subprocess.run(cli + ["notify", "claude@homelab", "kimi@brother", "cli reply",
                                "--reply-to", str(bid)],
                         env=os.environ.copy(), capture_output=True, text=True)
    out2 = subprocess.run(cli + ["replies", str(bid)], env=os.environ.copy(),
                          capture_output=True, text=True)
    check("cli notify --reply-to and cli replies round-trip",
          out.stdout.startswith("{") and json.loads(out2.stdout)[0]["message"] == "cli reply",
          out.stdout + out2.stdout + out.stderr + out2.stderr)

    del os.environ["IAB_DB"]

if fail:
    print("store_test: FAIL.")
    sys.exit(1)
print("store_test: OK.")
