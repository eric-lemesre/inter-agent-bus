"""Review driver of the inter-agent bus — diff in, verified verdict out.

Field facts behind it: one reviewer CLI returned a silently empty
successful review; another, without file access, HALLUCINATED the diff
it was asked to quote. Hence this driver: (1) the full diff is embedded
in the prompt (the only reliable transport), (2) the answer must be one
structured JSON object echoing a nonce (liveness), and (3) every
finding must reference a file and line actually covered by the diff's
hunks. The guard is necessary but NOT sufficient: it filters mechanical
failures, it does not judge the review's substance — cross-review by
another agent (the router's rule 4) still applies.

A prompt larger than the agent's roster `context_window` is REFUSED,
never truncated: a silently truncated diff produces a wrong review with
the appearance of success.
"""
from __future__ import annotations

import json
import os
import re
import secrets
import subprocess
import sys
from pathlib import Path

try:  # installed package (`iab` maps to this directory)
    from . import store, worker
except ImportError:  # bare script usage
    import store
    import worker

SEVERITIES = ("critical", "major", "minor")
VERDICTS = ("approve", "request_changes")
CHARS_PER_TOKEN = 3  # deliberately conservative estimate

PROMPT_TEMPLATE = """You are performing a code review of the unified diff below.
Respond with ONLY one JSON object — no prose, no markdown fences — shaped as:
{{"nonce": "<copy the nonce below verbatim>",
  "verdict": "approve" | "request_changes",
  "findings": [{{"file": "<path as it appears in the diff>",
                "line": <new-file line number inside a hunk of that file>,
                "severity": "critical" | "major" | "minor",
                "title": "<short>", "detail": "<why, and what to change>"}}]}}
Rules: every finding MUST point at a file and line present in the diff;
an empty findings list with verdict "approve" is a valid review; do not
invent content absent from the diff.
Nonce: {nonce}
=== DIFF ===
{diff}
"""


def staged_diff() -> str:
    done = subprocess.run(["git", "diff", "--staged"], capture_output=True, text=True)
    if done.returncode != 0:
        raise RuntimeError(f"git diff --staged failed: {done.stderr.strip()}")
    return done.stdout


def diff_targets(diff: str) -> dict[str, set[int]]:
    """Map each file of a unified diff to the new-file line numbers its
    hunks cover — the only (file, line) pairs a finding may reference."""
    targets: dict[str, set[int]] = {}
    current: str | None = None
    for line in diff.splitlines():
        if line.startswith("+++ "):
            name = line[4:].strip()
            name = name[2:] if name.startswith("b/") else name
            current = None if name == "/dev/null" else name
            if current is not None:
                targets.setdefault(current, set())
        elif line.startswith("@@") and current is not None:
            m = re.search(r"\+(\d+)(?:,(\d+))?", line)
            if m:
                start = int(m.group(1))
                count = int(m.group(2)) if m.group(2) is not None else 1
                targets[current].update(range(start, start + max(count, 1)))
    return targets


def check_review(output: str, nonce: str, targets: dict[str, set[int]]) -> tuple[dict | None, str]:
    """The anti-hallucination guard: returns (review, "") when the output
    is structurally sound, else (None, reason). Mechanical checks only."""
    text = output.strip()
    if not text:
        return None, "empty output"
    if text.startswith("```"):
        text = re.sub(r"^```[a-zA-Z]*\s*|\s*```$", "", text).strip()
    try:
        review = json.loads(text)
    except json.JSONDecodeError:
        start, end = text.find("{"), text.rfind("}")
        if start == -1 or end <= start:
            return None, "output is not JSON"
        try:
            review = json.loads(text[start:end + 1])
        except json.JSONDecodeError:
            return None, "output is not valid JSON"
    if not isinstance(review, dict):
        return None, "output is not a JSON object"
    if review.get("nonce") != nonce:
        return None, "nonce mismatch — the reviewer did not process this prompt"
    if review.get("verdict") not in VERDICTS:
        return None, f"verdict must be one of {list(VERDICTS)}"
    findings = review.get("findings")
    if not isinstance(findings, list):
        return None, "findings must be a list"
    bad: list[str] = []
    for i, f in enumerate(findings):
        if not isinstance(f, dict):
            bad.append(f"finding {i}: not an object")
            continue
        if f.get("severity") not in SEVERITIES:
            bad.append(f"finding {i}: severity must be one of {list(SEVERITIES)}")
        file, line = f.get("file"), f.get("line")
        if file not in targets:
            bad.append(f"finding {i}: file '{file}' is not in the diff")
        elif not isinstance(line, int) or line not in targets[file]:
            bad.append(f"finding {i}: line {line} of '{file}' is outside the diff hunks")
    if bad:
        return None, "; ".join(bad)
    return review, ""


def _roster_window(agent: str) -> int | None:
    path = (os.environ.get("IAB_ROSTER") or os.environ.get("ORCHESTRATOR_ROSTER")
            or "roster.json")
    try:
        data = json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    for entry in data.get("agents", []):
        if entry.get("name") == agent:
            return entry.get("context_window")
    return None


def run_review(
    agent: str,
    cmd: list[str],
    diff_text: str,
    task_id: str | None = None,
    lease_seconds: int = 900,
    task_timeout: int | None = None,
) -> int:
    agent = agent.strip().lower()
    if not diff_text.strip():
        print("ERROR: empty diff — nothing to review.")
        return 1
    targets = diff_targets(diff_text)
    if not targets:
        print("ERROR: no file hunks found — is this a unified diff?")
        return 1
    nonce = secrets.token_hex(8)
    prompt = PROMPT_TEMPLATE.format(nonce=nonce, diff=diff_text)
    window = _roster_window(agent)
    if window is not None and len(prompt) // CHARS_PER_TOKEN > window:
        print(
            f"ERROR: prompt ≈{len(prompt) // CHARS_PER_TOKEN} tokens exceeds "
            f"{agent}'s context_window ({window}) from the roster — refused, "
            "never truncated. Split the diff."
        )
        return 1
    task_id = task_id or f"review-{secrets.token_hex(4)}"
    print(store.register_agent(agent, "review driver"), file=sys.stderr)
    pushed = store.push_task(agent, task_id, prompt)
    if pushed.startswith("ERROR"):
        print(pushed)
        return 1
    claimed = store.claim_task(agent, lease_seconds, task_id)
    if claimed.startswith("ERROR") or claimed == "NO_TASK":
        print(claimed)
        return 1
    attempt = json.loads(claimed)["attempts"]
    try:
        code, out, err = worker._run_command(cmd, prompt, task_id, lease_seconds, task_timeout)
    except OSError as exc:  # command not found, …
        code, out, err = 127, "", str(exc)
    if code is None:
        result = (f"ERROR: review command killed after task timeout "
                  f"{task_timeout}s. stderr: {worker._tail(err)}")
    elif code != 0:
        result = f"ERROR: review command failed (exit {code}). stderr: {worker._tail(err)}"
    else:
        review, reason = check_review(out, nonce, targets)
        if review is None:
            result = (f"ERROR: review rejected by the anti-hallucination guard: "
                      f"{reason}. Raw output kept for the record:\n{out.strip()}")
        else:
            result = json.dumps(review, indent=2, ensure_ascii=False)
    print(store.publish_result(agent, task_id, result, attempt=attempt), file=sys.stderr)
    print(result)
    return 1 if result.startswith("ERROR") else 0
