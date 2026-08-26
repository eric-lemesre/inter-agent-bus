"""Headless worker driver of the inter-agent bus — claim → run → publish.

Purpose (field report P2): some agent CLIs load no MCP servers in
non-interactive mode, so they cannot hold their own claim/publish loop.
This driver holds it for them: claim under lease, feed the payload to
the command ON STDIN (never on the command line — quoting differs per
platform, argv is size-limited, payloads are untrusted), extend the
lease while the command runs (heartbeat), then publish stdout with the
claim's attempt token — or an `ERROR:`-prefixed result on non-zero
exit, empty output or task timeout, per the worker-loop discipline.

At-least-once delivery still applies: worker commands should be
idempotent. Trust note: payloads come from the shared database and are
fed to an autonomous agent CLI — see the README's security section.
"""
from __future__ import annotations

import json
import subprocess
import sys
import time

try:  # installed package (`iab` maps to this directory)
    from . import store
except ImportError:  # bare script usage
    import store

STDERR_TAIL = 2000
MAX_POLL_SECONDS = 60


def _log(message: str) -> None:
    print(f"[iab worker] {message}", file=sys.stderr, flush=True)


def _tail(text: str) -> str:
    text = text.strip()
    if len(text) > STDERR_TAIL:
        return f"…(truncated to last {STDERR_TAIL} chars) {text[-STDERR_TAIL:]}"
    return text


def _run_command(
    cmd: list[str],
    payload: str,
    task_id: str,
    lease_seconds: int,
    task_timeout: int | None,
) -> tuple[int | None, str, str]:
    """Run the command, payload on stdin, extending the task's lease at
    every heartbeat. Returns (returncode, stdout, stderr); returncode is
    None when the command was killed by the task timeout."""
    heartbeat = max(1, lease_seconds // 2)
    deadline = time.monotonic() + task_timeout if task_timeout else None
    proc = subprocess.Popen(
        cmd, stdin=subprocess.PIPE, stdout=subprocess.PIPE,
        stderr=subprocess.PIPE, text=True,
    )
    pending_input: str | None = payload
    while True:
        try:
            out, err = proc.communicate(input=pending_input, timeout=heartbeat)
            return proc.returncode, out, err
        except subprocess.TimeoutExpired:
            pending_input = None  # communicate() keeps the unsent remainder
            if deadline is not None and time.monotonic() > deadline:
                proc.kill()
                out, err = proc.communicate()
                return None, out, err
            renewed = store.extend_lease(task_id, lease_seconds)
            _log(renewed if renewed.startswith("ERROR")
                 else f"heartbeat: lease of '{task_id}' extended")


def run_worker(
    agent: str,
    cmd: list[str],
    once: bool = False,
    lease_seconds: int = 900,
    poll_seconds: int = 5,
    task_timeout: int | None = None,
) -> int:
    agent = agent.strip().lower()
    _log(store.register_agent(agent, "headless worker (iab worker)"))
    delay = poll_seconds
    while True:
        _log(store.heartbeat(agent, ttl_seconds=2 * poll_seconds + 60))
        claimed = store.claim_task(agent, lease_seconds)
        if claimed == "NO_TASK":
            if once:
                _log("queue drained — exiting (--once)")
                return 0
            _log(f"queue empty — next poll in {delay}s")
            time.sleep(delay)
            delay = min(delay * 2, MAX_POLL_SECONDS)
            continue
        delay = poll_seconds
        task = json.loads(claimed)
        task_id, attempt = task["task_id"], task["attempts"]
        _log(f"claimed '{task_id}' (attempt {attempt})")
        try:
            code, out, err = _run_command(
                cmd, task["payload"], task_id, lease_seconds, task_timeout
            )
        except OSError as exc:  # command not found, not executable, …
            code, out, err = 127, "", str(exc)
        if code is None:
            result = (f"ERROR: worker command killed after task timeout "
                      f"{task_timeout}s. stderr: {_tail(err)}")
        elif code != 0:
            result = f"ERROR: worker command failed (exit {code}). stderr: {_tail(err)}"
        elif not out.strip():
            result = (f"ERROR: worker command produced no output (exit 0). "
                      f"stderr: {_tail(err)}")
        else:
            result = out
        published = store.publish_result(agent, task_id, result, attempt=attempt)
        _log(published)
        try:
            channel = store.read_channel(agent=agent)
            if channel.startswith("ERROR"):
                _log(f"channel read failed: {channel}")
            else:
                for entry in json.loads(channel):
                    _log(f"channel [{entry['topic']}] from {entry['author']}: "
                         f"{entry['message']}")
        except Exception as exc:  # channel read must not kill the worker loop
            _log(f"channel read failed: {exc}")
        if once:
            return 0 if published.startswith("OK") and not result.startswith("ERROR") else 1
