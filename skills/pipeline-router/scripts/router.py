"""Heuristic routing of a task towards an agent of the roster.

Fallback for scripts and hooks — in session, the orchestrating agent routes
by itself using the rules in SKILL.md. No agent name is hard-coded here:
everything comes from the roster.

    python3 router.py <roster.json> "<task description>"
"""
from __future__ import annotations

import json
import sys


def route_request(roster: list[dict], request_text: str) -> tuple[str, int]:
    text = request_text.lower()

    # 1. Specialty match: the agent whose keywords appear most in the
    #    request wins.
    best, best_hits = None, 0
    for agent in roster:
        hits = sum(1 for kw in agent.get("specialties", []) if kw.lower() in text)
        if hits > best_hits:
            best, best_hits = agent, hits
    if best is not None:
        return best["name"], 2

    # 2. Cost fallback: very long request → widest context window;
    #    otherwise a zero-marginal-cost agent (flat subscription or local).
    if len(request_text) > 10000:
        widest = max(roster, key=lambda a: a.get("context_window", 0))
        return widest["name"], 3
    free = [a for a in roster if a.get("cost_model") in ("flat", "local")]
    fallback = free[0] if free else roster[0]
    return fallback["name"], 1


def main() -> int:
    if len(sys.argv) < 3:
        print("Usage: python3 router.py <roster.json> '<task description>'")
        return 2
    with open(sys.argv[1], encoding="utf-8") as f:
        roster = json.load(f)["agents"]
    if not roster:
        print("ERROR: empty roster.")
        return 1
    target, priority = route_request(roster, sys.argv[2])
    print(json.dumps({"target_agent": target, "priority": priority}))
    return 0


if __name__ == "__main__":
    sys.exit(main())
