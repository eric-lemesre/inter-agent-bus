"""Storage core of the inter-agent bus — shared SQLite, no MCP dependency.

Kept separate from the MCP wrapper (server.py) for two reasons:
  1. every agent client spawns ITS OWN stdio subprocess — state cannot live
     in server memory: it lives in a SQLite database shared by all instances
     (WAL mode, immediate transactions);
  2. the core is testable without the MCP SDK installed (store_test.py).

Database path — the cross-client rendezvous point. Resolution order
(see db_info() for the diagnosis):
  1. IAB_DB env var (the authority — set it per project);
  2. legacy ORCHESTRATOR_DB env var;
  3. an existing pre-isolation global bus (bus.db in the platform data
     directory), then an existing pre-rename database — migrations;
  4. otherwise a PER-PROJECT database derived from the process working
     directory (realpath-normalized): a user-scope install must not
     merge every project into one bus (task_id collisions, mixed
     state). All participants of one project must resolve the same
     path — whoami/db_info make a mismatch diagnosable in one call.
(PLUGIN_DATA does not fit: it is managed PER CLIENT, hence invisible
to the other agents.)

Task lifecycle: queued → claimed (lease) → done. An expired lease requeues
the task (attempts + 1): an agent dying after claim no longer loses the
task — the flaw of the destructive pop in the first version. Every
transition is journaled in the append-only `events` table (get_events).
"""
from __future__ import annotations

import hashlib
import json
import os
import re
import sqlite3
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path


def _data_dir() -> Path:
    """Platform data directory, resolved like platformdirs would (rule:
    no new dependency while three branches suffice)."""
    if sys.platform == "win32":
        base = Path(os.environ.get("LOCALAPPDATA", str(Path.home() / "AppData/Local")))
    elif sys.platform == "darwin":
        base = Path.home() / "Library/Application Support"
    else:
        base = Path(os.environ.get("XDG_DATA_HOME", str(Path.home() / ".local/share")))
    return base / "inter-agent-bus"


DATA_DIR = _data_dir()
GLOBAL_DB = DATA_DIR / "bus.db"
LEGACY_DB = Path.home() / ".local/share/multi-agent-orchestrator/orchestrator.db"


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def project_key(cwd: str | os.PathLike | None = None) -> str:
    """Stable per-project key from the working directory: realpath (so
    symlinks and relative paths converge), case-folded on the platforms
    whose filesystems are case-insensitive, then slug + short hash."""
    real = os.path.realpath(cwd if cwd is not None else os.getcwd())
    normalized = real.lower() if sys.platform in ("win32", "darwin") else real
    digest = hashlib.sha256(normalized.encode("utf-8")).hexdigest()[:12]
    slug = re.sub(r"[^A-Za-z0-9_-]+", "-", Path(real).name)[:40] or "root"
    return f"{slug}-{digest}"


def _resolve_db(cwd: str | os.PathLike | None = None) -> tuple[Path, str]:
    for var in ("IAB_DB", "ORCHESTRATOR_DB"):
        value = os.environ.get(var)
        if value:
            return Path(value), f"env:{var}"
    if GLOBAL_DB.exists():
        return GLOBAL_DB, "global (pre-isolation bus kept — set IAB_DB to opt out)"
    if LEGACY_DB.exists():
        return LEGACY_DB, "legacy (pre-rename bus kept — set IAB_DB to opt out)"
    return DATA_DIR / "projects" / f"{project_key(cwd)}.db", "project"


def db_path(cwd: str | os.PathLike | None = None) -> Path:
    return _resolve_db(cwd)[0]


def db_info() -> str:
    """Resolved bus database and how it was chosen — the rendezvous
    diagnosis: participants of one project must all see the same path."""
    path, source = _resolve_db()
    return json.dumps(
        {"db": str(path), "db_source": source, "project_key": project_key()},
        ensure_ascii=False,
    )


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
        CREATE TABLE IF NOT EXISTS events (
            id      INTEGER PRIMARY KEY AUTOINCREMENT,
            task_id TEXT,
            agent   TEXT,
            event   TEXT NOT NULL,
            at      TEXT NOT NULL,
            detail  TEXT NOT NULL DEFAULT ''
        );
        CREATE INDEX IF NOT EXISTS events_task ON events(task_id);
        """
    )
    return conn


def _log_event(
    conn: sqlite3.Connection,
    event: str,
    task_id: str | None = None,
    agent: str | None = None,
    detail: str = "",
) -> None:
    conn.execute(
        "INSERT INTO events(task_id, agent, event, at, detail) VALUES(?,?,?,?,?)",
        (task_id, agent, event, _now(), detail),
    )


def register_agent(name: str, description: str = "") -> str:
    name = name.strip().lower()
    if not name:
        return "ERROR: empty agent name."
    with connect() as conn:
        known = conn.execute("SELECT 1 FROM agents WHERE name=?", (name,)).fetchone()
        conn.execute(
            "INSERT INTO agents(name, description, registered_at) VALUES(?,?,?) "
            "ON CONFLICT(name) DO UPDATE SET description=excluded.description",
            (name, description, _now()),
        )
        if known is None:
            _log_event(conn, "register", agent=name, detail=description)
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
        _log_event(conn, "push", task_id, target, f"priority {priority}")
    return f"OK: task '{task_id}' queued for {target}."


def _requeue_expired(conn: sqlite3.Connection) -> None:
    expired = conn.execute(
        "SELECT task_id, agent FROM tasks WHERE status='claimed' AND lease_deadline < ?",
        (_now(),),
    ).fetchall()
    for row in expired:
        conn.execute(
            "UPDATE tasks SET status='queued', claimed_at=NULL, lease_deadline=NULL "
            "WHERE task_id=?",
            (row["task_id"],),
        )
        _log_event(conn, "expire", row["task_id"], row["agent"],
                   "lease expired — task re-offered")


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
            _log_event(conn, "claim", row["task_id"], target,
                       f"attempt {row['attempts'] + 1}, lease until {deadline}")
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
        cur = conn.execute("UPDATE tasks SET status='done' WHERE task_id=?", (task_id,))
        _log_event(conn, "publish", task_id, author,
                   "settled" if cur.rowcount else "no matching task — result stored anyway")
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


def get_events(task_id: str | None = None, agent: str | None = None, limit: int = 100) -> str:
    """Chronological transition history (register/push/claim/expire/publish),
    filterable by task and/or agent; the `limit` most recent, oldest first."""
    query = "SELECT task_id, agent, event, at, detail FROM events"
    conds: list[str] = []
    params: list[object] = []
    if task_id:
        conds.append("task_id=?")
        params.append(task_id)
    if agent:
        conds.append("agent=?")
        params.append(agent.strip().lower())
    if conds:
        query += " WHERE " + " AND ".join(conds)
    query += " ORDER BY id DESC LIMIT ?"
    params.append(limit)
    with connect() as conn:
        rows = [dict(r) for r in conn.execute(query, params)]
    rows.reverse()
    return json.dumps(rows, indent=2, ensure_ascii=False)
