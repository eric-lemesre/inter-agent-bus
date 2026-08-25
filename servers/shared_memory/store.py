"""Storage core of the inter-agent bus — shared SQLite, no MCP dependency.

Kept separate from the MCP wrapper (server.py) for two reasons:
  1. every agent client spawns ITS OWN stdio subprocess — state cannot live
     in server memory: it lives in a SQLite database shared by all instances
     (WAL mode, immediate transactions);
  2. the core is testable without the MCP SDK installed (store_test.py).

Database path: IAB_DB env var (legacy ORCHESTRATOR_DB still honored),
otherwise ~/.local/share/inter-agent-bus/bus.db — falling back to the
pre-rename default if that one exists. This path is the cross-client
rendezvous point — PLUGIN_DATA does not fit: it is managed PER CLIENT,
hence invisible to the other agents.

Task lifecycle: queued → claimed (lease) → done. An expired lease requeues
the task (attempts + 1): an agent dying after claim no longer loses the
task — the flaw of the destructive pop in the first version.
"""
from __future__ import annotations

import json
import os
import sqlite3
from datetime import datetime, timedelta, timezone
from pathlib import Path

DEFAULT_DB = Path.home() / ".local/share/inter-agent-bus/bus.db"
LEGACY_DB = Path.home() / ".local/share/multi-agent-orchestrator/orchestrator.db"


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def db_path() -> Path:
    """IAB_DB, else legacy ORCHESTRATOR_DB, else the default — reusing a
    pre-rename database when it exists and the new default does not."""
    for var in ("IAB_DB", "ORCHESTRATOR_DB"):
        value = os.environ.get(var)
        if value:
            return Path(value)
    if not DEFAULT_DB.exists() and LEGACY_DB.exists():
        return LEGACY_DB
    return DEFAULT_DB


def connect() -> sqlite3.Connection:
    path = db_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(path, timeout=10, isolation_level=None)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA busy_timeout=10000")
    conn.executescript(
        """
        CREATE TABLE IF NOT EXISTS agents (
            name          TEXT PRIMARY KEY,
            description   TEXT NOT NULL DEFAULT '',
            registered_at TEXT NOT NULL
        );
        CREATE TABLE IF NOT EXISTS tasks (
            task_id        TEXT PRIMARY KEY,
            agent          TEXT NOT NULL REFERENCES agents(name),
            payload        TEXT NOT NULL,
            priority       INTEGER NOT NULL DEFAULT 1,
            status         TEXT NOT NULL DEFAULT 'queued',
            attempts       INTEGER NOT NULL DEFAULT 0,
            created_at     TEXT NOT NULL,
            claimed_at     TEXT,
            lease_deadline TEXT
        );
        CREATE TABLE IF NOT EXISTS results (
            task_id    TEXT PRIMARY KEY,
            author     TEXT NOT NULL,
            content    TEXT NOT NULL,
            updated_at TEXT NOT NULL
        );
        """
    )
    return conn


def register_agent(name: str, description: str = "") -> str:
    name = name.strip().lower()
    if not name:
        return "ERROR: empty agent name."
    with connect() as conn:
        conn.execute(
            "INSERT INTO agents(name, description, registered_at) VALUES(?,?,?) "
            "ON CONFLICT(name) DO UPDATE SET description=excluded.description",
            (name, description, _now()),
        )
    return f"OK: agent '{name}' registered."


def _known_agents(conn: sqlite3.Connection) -> list[str]:
    return [r["name"] for r in conn.execute("SELECT name FROM agents ORDER BY name")]


def push_task(target_agent: str, task_id: str, payload: str, priority: int = 1) -> str:
    target = target_agent.strip().lower()
    with connect() as conn:
        known = _known_agents(conn)
        # Explicit refusal of an unregistered agent: keeps the typo detection
        # that hard-coded queues provided, without freezing the cast.
        if target not in known:
            return (
                f"ERROR: agent '{target_agent}' not registered. "
                f"Known agents: {known or 'none — call register_agent first'}."
            )
        try:
            conn.execute(
                "INSERT INTO tasks(task_id, agent, payload, priority, created_at) "
                "VALUES(?,?,?,?,?)",
                (task_id, target, payload, priority, _now()),
            )
        except sqlite3.IntegrityError:
            return f"ERROR: task_id '{task_id}' already exists."
    return f"OK: task '{task_id}' queued for {target}."


def _requeue_expired(conn: sqlite3.Connection) -> None:
    conn.execute(
        "UPDATE tasks SET status='queued', claimed_at=NULL, lease_deadline=NULL "
        "WHERE status='claimed' AND lease_deadline < ?",
        (_now(),),
    )


def claim_task(agent_name: str, lease_seconds: int = 900) -> str:
    """Atomically claim the highest-priority queued task of one's queue, under lease."""
    target = agent_name.strip().lower()
    deadline = (datetime.now(timezone.utc) + timedelta(seconds=lease_seconds)).isoformat()
    with connect() as conn:
        conn.execute("BEGIN IMMEDIATE")
        try:
            _requeue_expired(conn)
            row = conn.execute(
                "SELECT task_id, payload, priority, attempts, created_at FROM tasks "
                "WHERE agent=? AND status='queued' "
                "ORDER BY priority DESC, created_at ASC LIMIT 1",
                (target,),
            ).fetchone()
            if row is None:
                conn.execute("COMMIT")
                return "NO_TASK"
            conn.execute(
                "UPDATE tasks SET status='claimed', claimed_at=?, lease_deadline=?, "
                "attempts=attempts+1 WHERE task_id=?",
                (_now(), deadline, row["task_id"]),
            )
            conn.execute("COMMIT")
        except Exception:
            conn.execute("ROLLBACK")
            raise
    return json.dumps(
        {
            "task_id": row["task_id"],
            "payload": row["payload"],
            "priority": row["priority"],
            "attempts": row["attempts"] + 1,
            "created_at": row["created_at"],
            "lease_deadline": deadline,
        },
        ensure_ascii=False,
    )


def publish_result(agent_name: str, task_id: str, result_content: str) -> str:
    """Publish the result and settle (ack) the matching task if it exists."""
    author = agent_name.strip().lower()
    with connect() as conn:
        conn.execute(
            "INSERT INTO results(task_id, author, content, updated_at) VALUES(?,?,?,?) "
            "ON CONFLICT(task_id) DO UPDATE SET author=excluded.author, "
            "content=excluded.content, updated_at=excluded.updated_at",
            (task_id, author, result_content, _now()),
        )
        conn.execute("UPDATE tasks SET status='done' WHERE task_id=?", (task_id,))
    return f"OK: result for '{task_id}' published by {author}."


def read_result(task_id: str) -> str:
    with connect() as conn:
        row = conn.execute(
            "SELECT author, content, updated_at FROM results WHERE task_id=?",
            (task_id,),
        ).fetchone()
    if row is None:
        return f"ERROR: no result found for '{task_id}'."
    return json.dumps(dict(row), ensure_ascii=False)


def get_system_state() -> str:
    with connect() as conn:
        _requeue_expired(conn)
        agents = {
            r["name"]: r["description"]
            for r in conn.execute("SELECT name, description FROM agents ORDER BY name")
        }
        queues = {
            name: {
                "queued": conn.execute(
                    "SELECT COUNT(*) c FROM tasks WHERE agent=? AND status='queued'",
                    (name,),
                ).fetchone()["c"],
                "claimed": conn.execute(
                    "SELECT COUNT(*) c FROM tasks WHERE agent=? AND status='claimed'",
                    (name,),
                ).fetchone()["c"],
            }
            for name in agents
        }
        done = [
            r["task_id"]
            for r in conn.execute("SELECT task_id FROM results ORDER BY updated_at")
        ]
    return json.dumps(
        {"agents": agents, "queues": queues, "completed_tasks": done},
        indent=2,
        ensure_ascii=False,
    )
