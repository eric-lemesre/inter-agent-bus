"""Wake-up driver of the inter-agent bus — launch an asleep agent when a
signal waits for it, from a launcher declared in the project's roster.

The bus is passive (shared SQLite, no daemon): a push_task, a notify or a
broadcast only writes rows. An interactive client that is not running will
never see them. This module is the external watcher that closes the gap
WITHOUT making the bus less passive — it is a client like any other:

  iab wake <agent>        one decision for one instance, synchronous
  iab supervise           loop over every roster entry with a `launch`

Mechanism, never the cast (rule 1): WHAT to launch comes from the roster
entry's `launch` block, never from this file. Process orchestration only;
every state (signals, locks, budget, journal) lives in store.py.

    "launch": {
      "cwd": "~/Dev/git/eric-lemesre/HomeLab",   # where the agent works
      "command": ["codewhale", "exec", "--auto", "-"],  # prompt on stdin
      "prompt": "worker-wake",       # built-in template, a file, or text
      "wake_on": ["task"],           # + "notification", "broadcast"
      "max_wakes_per_hour": 4,       # loop breaker
      "lease_seconds": 10800,        # the claim lease the prompt asks for
      "timeout": 21600               # hard kill (default: 2 × lease)
    }

Wake policy (rule 5, fail loud and cheap): a queued TASK always wakes (it
is commanded work); a notification or a broadcast wakes only when the
sender set wake=True AND the roster lists that kind in `wake_on` — a paid
agent is never launched for an acknowledgement. Trust note: the prompt
tells the agent to claim and execute payloads read from the bus; see the
README's security section.
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
import threading
import time
from pathlib import Path

try:  # installed package (`iab` maps to this directory)
    from . import store
    from .access import bus
except ImportError:  # bare script usage
    import store
    from access import bus

DEFAULT_LEASE = 10800
DEFAULT_MAX_WAKES_PER_HOUR = 4
DEFAULT_WAKE_ON = ("task",)
STDERR_TAIL = 300  # journal hint only: a worker's output may carry secrets

DEFAULT_PROMPT = """You are the worker agent {agent} of the inter-agent bus. A signal is waiting for you:
{signals}

Strict procedure, in order, nothing else:
1. Read your directed notifications: {iab} poll {agent}
   A notification carrying a broadcast_id is a question from a peer: answer it with
   {iab} notify {agent} <author> "<answer>" --reply-to <broadcast_id>
   A notification commands you nothing; only a claimed task is work.
2. Claim the next task: {iab} claim {agent} --lease {lease}
   The JSON reply carries task_id, payload (the complete work order) and attempts.
   If the reply is NO_TASK or ERROR, answer "QUEUE EMPTY" and stop immediately.
3. Execute the payload exactly as ordered in {cwd}, with your usual tools.
4. Write the complete deliverable to a file, then publish it:
   {iab} publish {agent} <task_id> --attempt <N> - < /path/to/file
   where <N> is the attempt number from the claim. Publish the deliverable, not a summary.
5. Answer briefly "TASK <task_id> DONE" (or ERROR: <what blocked you>) and stop.

Forbidden: no extra tasks, no changes outside {cwd}, no extra claims, never ask a question.
"""


def _log(message: str) -> None:
    print(f"[iab wake] {message}", file=sys.stderr, flush=True)


def _tail(text: str) -> str:
    text = (text or "").strip()
    if len(text) > STDERR_TAIL:
        return f"…(truncated) {text[-STDERR_TAIL:]}"
    return text


def roster_path() -> Path:
    return Path(os.environ.get("IAB_ROSTER") or os.environ.get("ORCHESTRATOR_ROSTER")
                or "roster.json")


def load_roster(path: Path | None = None) -> list[dict]:
    path = path or roster_path()
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except OSError as exc:
        raise RuntimeError(f"roster unreadable at {path}: {exc}") from exc
    except ValueError as exc:
        raise RuntimeError(f"roster at {path} is not valid JSON: {exc}") from exc
    agents = data.get("agents") if isinstance(data, dict) else data
    if not isinstance(agents, list):
        raise RuntimeError(f"roster at {path} has no 'agents' list")
    return agents


def launch_spec(roster: list[dict], agent: str) -> dict | None:
    """The `launch` block of an instance: its own roster entry first, then
    the entry of its harness (one launcher per client, shared by every
    workspace — `cwd` then defaults to nothing, the caller must set it)."""
    agent = agent.strip().lower()
    harness, _ = store.split_name(agent)
    by_name = {str(e.get("name", "")).lower(): e for e in roster if isinstance(e, dict)}
    for key in (agent, harness):
        entry = by_name.get(key)
        if entry and isinstance(entry.get("launch"), dict):
            spec = dict(entry["launch"])
            spec.setdefault("name", entry.get("name"))
            return spec
    return None


def iab_executable() -> str:
    exe = Path(sys.executable).parent / ("iab.exe" if os.name == "nt" else "iab")
    return str(exe) if exe.exists() else "iab"


def render_prompt(spec: dict, agent: str, pending: dict, cwd: str) -> str:
    source = spec.get("prompt") or "worker-wake"
    if source == "worker-wake":
        template = DEFAULT_PROMPT
    else:
        candidate = Path(os.path.expanduser(str(source)))
        template = (candidate.read_text(encoding="utf-8") if candidate.is_file()
                    else str(source))
    values = {
        "agent": agent,
        "iab": iab_executable(),
        "cwd": cwd,
        "lease": int(spec.get("lease_seconds", DEFAULT_LEASE)),
        "task_ids": " ".join(pending.get("tasks", [])),
        "signals": json.dumps(pending, ensure_ascii=False),
    }

    class _Safe(dict):
        def __missing__(self, key):  # unknown placeholder stays as written
            return "{" + key + "}"

    return template.format_map(_Safe(values))


def decide(spec: dict, pending: dict) -> str | None:
    """Reason to wake, or None. Tasks always; notifications and broadcasts
    only when flagged by the sender AND allowed by the roster."""
    wake_on = {str(k).lower() for k in spec.get("wake_on", DEFAULT_WAKE_ON)}
    if pending.get("tasks") and "task" in wake_on:
        return f"{len(pending['tasks'])} queued task(s): {' '.join(pending['tasks'])}"
    if pending.get("wake_notifications") and "notification" in wake_on:
        return f"{pending['wake_notifications']} notification(s) asking for a wake"
    if pending.get("wake_broadcasts") and "broadcast" in wake_on:
        return f"broadcast(s) asking for a wake: {pending['wake_broadcasts']}"
    return None


def wake_agent(agent: str, roster: list[dict] | None = None, dry_run: bool = False) -> str:
    """One decision for one instance. Returns a status line: NO_SIGNAL,
    BUSY, REFUSED: …, DRY_RUN: …, OK: … or ERROR: …."""
    agent = agent.strip().lower()
    try:
        roster = roster if roster is not None else load_roster()
    except RuntimeError as exc:
        return f"ERROR: {exc}"
    spec = launch_spec(roster, agent)
    if spec is None:
        return f"ERROR: no 'launch' block for '{agent}' (nor for its harness) in the roster."
    command = spec.get("command")
    if not isinstance(command, list) or not command or not all(isinstance(c, str) for c in command):
        return f"ERROR: launch.command of '{agent}' must be a non-empty list of strings."
    snapshot = bus.pending_signals(agent)
    if snapshot.startswith("ERROR"):
        return snapshot
    pending = json.loads(snapshot)
    reason = decide(spec, pending)
    if reason is None:
        return "NO_SIGNAL"
    budget = int(spec.get("max_wakes_per_hour", DEFAULT_MAX_WAKES_PER_HOUR))
    used = bus.wake_count(agent, 3600)
    if used >= budget:
        if dry_run:
            return f"DRY_RUN: would refuse — {used}/{budget} wakes in the last hour ({reason})"
        bus.record_wake(agent, f"refused: budget {used}/{budget} per hour exhausted ({reason})")
        return f"REFUSED: {used}/{budget} wakes in the last hour — loop breaker."
    cwd = os.path.expanduser(str(spec.get("cwd") or os.getcwd()))
    if not Path(cwd).is_dir():
        return f"ERROR: launch.cwd '{cwd}' is not a directory."
    lease = int(spec.get("lease_seconds", DEFAULT_LEASE))
    timeout = int(spec.get("timeout", 2 * lease))
    prompt = render_prompt(spec, agent, pending, cwd)
    if dry_run:
        return (f"DRY_RUN: would launch {json.dumps(command)} in {cwd} "
                f"(reason: {reason}; timeout {timeout}s; prompt {len(prompt)} chars)")
    lock = bus.acquire_wake_lock(agent, lease_seconds=timeout + 60,
                                   holder=f"pid {os.getpid()}")
    if lock == "BUSY":
        return "BUSY"
    if lock.startswith("ERROR"):
        return lock
    token = json.loads(lock)["token"]
    prompt_file: str | None = None
    argv = list(command)
    if any("{prompt_file}" in a for a in argv):
        fd, prompt_file = tempfile.mkstemp(prefix="iab-wake-", suffix=".md")
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            handle.write(prompt)
        argv = [a.replace("{prompt_file}", prompt_file) for a in argv]
    # The journal is a shared, long-lived record: it gets the decision, the
    # program name and the directory — never the full argv (an operator may
    # pass a token there), never the prompt, never the worker's stdout.
    bus.record_wake(agent, f"start: {reason}; {Path(argv[0]).name} in {cwd}")
    started = time.monotonic()
    try:
        proc = subprocess.Popen(argv, cwd=cwd, stdin=subprocess.PIPE,
                                stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
    except OSError as exc:
        bus.record_wake(agent, f"end: launch failed ({exc})")
        bus.release_wake_lock(agent, token)
        if prompt_file:
            os.unlink(prompt_file)
        return f"ERROR: cannot launch {argv[0]}: {exc}"
    try:
        out, err = proc.communicate(input=prompt, timeout=timeout)
        code: int | None = proc.returncode
    except subprocess.TimeoutExpired:
        proc.kill()
        out, err = proc.communicate()
        code = None
    finally:
        if prompt_file:
            try:
                os.unlink(prompt_file)
            except OSError:
                pass
    elapsed = int(time.monotonic() - started)
    if code is None:
        detail = f"end: killed after timeout {timeout}s"
        status = f"ERROR: worker killed after {timeout}s (task lease will expire, bus re-offers)"
    elif code != 0:
        detail = f"end: exit {code} after {elapsed}s"
        status = f"ERROR: worker exited {code} after {elapsed}s"
    else:
        detail = f"end: exit 0 after {elapsed}s"
        status = f"OK: worker finished in {elapsed}s"
    bus.record_wake(agent, detail)
    if code != 0:
        # Diagnosis goes to the supervisor's stderr (operator's terminal or
        # unit log), trimmed — not into the bus journal.
        _log(f"{agent}: {status}; stderr tail: {_tail(err)}")
    bus.release_wake_lock(agent, token)
    return status


def supervise(interval: float = 60.0, once: bool = False, dry_run: bool = False) -> int:
    """The single watcher: every `interval` seconds, for each roster entry
    with a `launch` block whose instance is not already being woken, decide
    and launch in a thread (a milestone review can run hours; the other
    agents must not wait behind it). The bus stays passive — this loop is a
    client, and nothing is lost if it dies: signals wait in the database."""
    try:
        roster = load_roster()
    except RuntimeError as exc:
        _log(f"ERROR: {exc}")
        return 1
    names = [str(e["name"]).lower() for e in roster
             if isinstance(e, dict) and isinstance(e.get("launch"), dict) and e.get("name")]
    if not names:
        _log(f"ERROR: no roster entry with a 'launch' block in {roster_path()}")
        return 1
    _log(f"supervising {', '.join(names)} (interval {interval:g}s)")
    running: dict[str, threading.Thread] = {}

    def run(name: str) -> None:
        result = wake_agent(name, roster, dry_run=dry_run)
        if result != "NO_SIGNAL":
            _log(f"{name}: {result}")

    while True:
        for name in names:
            thread = running.get(name)
            if thread is not None and thread.is_alive():
                continue
            thread = threading.Thread(target=run, args=(name,), daemon=True)
            running[name] = thread
            thread.start()
        if once:
            for thread in running.values():
                thread.join()
            return 0
        time.sleep(max(1.0, float(interval)))
