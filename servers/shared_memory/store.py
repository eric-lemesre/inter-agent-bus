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

Task lifecycle: queued → claimed (lease) → done, with two terminal side
exits: dead (lease expired attempts >= max_attempts times — dead-letter,
revivable with requeue) and cancelled. An expired lease requeues the
task (attempts + 1): an agent dying after claim no longer loses the
task. `expired` is an event, not a status. Settling is fenced: the
claim response carries the attempt number, and publish_result refuses
a staler token — a slow worker whose lease expired cannot overwrite
the result of the worker that took the task over. Every transition is
journaled in the append-only `events` table (get_events).
"""
from __future__ import annotations

import hashlib
import json
import os
import re
import sqlite3
import sys
import time
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

DEFAULT_PRESENCE_TTL = 120


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
    if os.name == "posix":
        # Writing the bus is prompt-injection-adjacent (payloads reach
        # autonomous agents): keep the file owner-only. Hardening, not
        # correctness — an exotic filesystem may refuse, the bus still works.
        try:
            os.chmod(path, 0o600)
        except OSError:
            pass
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
        CREATE TABLE IF NOT EXISTS presence (
            agent        TEXT PRIMARY KEY,
            last_seen    TEXT NOT NULL,
            ttl_seconds  INTEGER NOT NULL,
            capabilities TEXT NOT NULL DEFAULT '{}'
        );
        CREATE TABLE IF NOT EXISTS channel (
            seq     INTEGER PRIMARY KEY AUTOINCREMENT,
            author  TEXT NOT NULL,
            at      TEXT NOT NULL,
            topic   TEXT NOT NULL,
            message TEXT NOT NULL
        );
        CREATE INDEX IF NOT EXISTS channel_topic ON channel(topic);
        CREATE TABLE IF NOT EXISTS channel_cursor (
            agent    TEXT PRIMARY KEY,
            last_seq INTEGER NOT NULL
        );

        CREATE TABLE IF NOT EXISTS notifications (
            seq     INTEGER PRIMARY KEY AUTOINCREMENT,
            target  TEXT NOT NULL,
            author  TEXT NOT NULL,
            at      TEXT NOT NULL,
            message TEXT NOT NULL
        );
        CREATE INDEX IF NOT EXISTS notifications_target ON notifications(target);
        CREATE TABLE IF NOT EXISTS notification_cursor (
            target   TEXT PRIMARY KEY,
            last_seq INTEGER NOT NULL
        );
        """
    )
    _migrate(conn)
    return conn


def _migrate(conn: sqlite3.Connection) -> None:
    """Additive migrations only — the database is a long-lived rendezvous
    shared by several clients; existing files must keep working."""
    columns = {r["name"] for r in conn.execute("PRAGMA table_info(tasks)")}
    if "max_attempts" not in columns:
        conn.execute("ALTER TABLE tasks ADD COLUMN max_attempts INTEGER NOT NULL DEFAULT 3")


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


def push_task(
    target_agent: str, task_id: str, payload: str, priority: int = 1, max_attempts: int = 3,
    notify: bool = True,
) -> str:
    """Queue a task and, unless notify=False, drop a directed notification in
    the target's mailbox ("task queued") so a watcher or a poll() wakes the
    worker — before this, a queued task sat invisible until a human relaunched
    the agent. The notification is a hint for claim_task, not a delivery
    channel: at-least-once still rules, and a woken worker may find an empty
    queue (lease expiry, cancellation) and must tolerate it."""
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
                "INSERT INTO tasks(task_id, agent, payload, priority, created_at, "
                "max_attempts) VALUES(?,?,?,?,?,?)",
                (task_id, target, payload, priority, _now(), max_attempts),
            )
        except sqlite3.IntegrityError:
            return f"ERROR: task_id '{task_id}' already exists."
        _log_event(conn, "push", task_id, target, f"priority {priority}")
        if notify:
            # Same transaction as the insert: a task never exists without its
            # wake-up hint (author 'bus' is the mechanism speaking, not a cast
            # member — rule 1 forbids hardcoding agents, not the bus itself).
            conn.execute(
                "INSERT INTO notifications(target, author, at, message) VALUES(?,?,?,?)",
                (target, "bus", _now(),
                 f"task '{task_id}' queued (priority {priority}) — claim it with claim_task"),
            )
    return f"OK: task '{task_id}' queued for {target}."


def wait_for_task(agent: str, timeout_s: float = 300.0, interval_s: float = 2.0) -> str:
    """Block until the agent's queue holds at least one queued task, then
    return the queued task_ids as a JSON list (highest priority first).
    Returns "[]" on timeout. The poll runs the same expiry sweep as
    claim_task, so a watcher also wakes on a lease-expired, re-offered task.

    Pure-Python sleep loop (no OS-specific primitive): this is the core that
    the CLI `iab watch` and the MCP tool wait_task wrap, so a supervisor
    (systemd user timer, cron, or an agent background task) can wake a worker
    without a hand-rolled SQLite watcher that dies with its session."""
    target = agent.strip().lower()
    if not target:
        return "ERROR: empty agent."
    interval_s = max(0.2, float(interval_s))
    deadline = time.monotonic() + max(0.0, float(timeout_s))
    while True:
        with connect() as conn:
            _requeue_expired(conn)
            rows = conn.execute(
                "SELECT task_id FROM tasks WHERE agent=? AND status='queued' "
                "ORDER BY priority DESC, created_at ASC",
                (target,),
            ).fetchall()
        if rows:
            return json.dumps([r["task_id"] for r in rows], ensure_ascii=False)
        if time.monotonic() >= deadline:
            return "[]"
        time.sleep(min(interval_s, max(0.0, deadline - time.monotonic())))


def _requeue_expired(conn: sqlite3.Connection) -> None:
    expired = conn.execute(
        "SELECT task_id, agent, attempts, max_attempts FROM tasks "
        "WHERE status='claimed' AND lease_deadline < ?",
        (_now(),),
    ).fetchall()
    for row in expired:
        if row["attempts"] >= row["max_attempts"]:
            conn.execute(
                "UPDATE tasks SET status='dead', claimed_at=NULL, lease_deadline=NULL "
                "WHERE task_id=?",
                (row["task_id"],),
            )
            _log_event(conn, "dead", row["task_id"], row["agent"],
                       f"lease expired after attempt {row['attempts']}/"
                       f"{row['max_attempts']} — dead-lettered (requeue to revive)")
        else:
            conn.execute(
                "UPDATE tasks SET status='queued', claimed_at=NULL, lease_deadline=NULL "
                "WHERE task_id=?",
                (row["task_id"],),
            )
            _log_event(conn, "expire", row["task_id"], row["agent"],
                       "lease expired — task re-offered")


def claim_task(agent_name: str, lease_seconds: int = 900, task_id: str | None = None) -> str:
    """Atomically claim the highest-priority queued task of one's queue,
    under lease — or one specific queued task of one's own queue when
    task_id is given (logged escape hatch; no cross-queue poaching)."""
    target = agent_name.strip().lower()
    deadline = (datetime.now(timezone.utc) + timedelta(seconds=lease_seconds)).isoformat()
    with connect() as conn:
        conn.execute("BEGIN IMMEDIATE")
        try:
            _requeue_expired(conn)
            if task_id is None:
                row = conn.execute(
                    "SELECT task_id, payload, priority, attempts, created_at FROM tasks "
                    "WHERE agent=? AND status='queued' "
                    "ORDER BY priority DESC, created_at ASC LIMIT 1",
                    (target,),
                ).fetchone()
                if row is None:
                    conn.execute("COMMIT")
                    return "NO_TASK"
            else:
                row = conn.execute(
                    "SELECT task_id, agent, status, payload, priority, attempts, "
                    "created_at FROM tasks WHERE task_id=?",
                    (task_id,),
                ).fetchone()
                if row is None:
                    conn.execute("COMMIT")
                    return f"ERROR: unknown task '{task_id}'."
                if row["agent"] != target:
                    conn.execute("COMMIT")
                    return (
                        f"ERROR: task '{task_id}' belongs to {row['agent']}'s queue — "
                        "no cross-queue claiming."
                    )
                if row["status"] != "queued":
                    conn.execute("COMMIT")
                    return f"ERROR: task '{task_id}' is not queued (status: {row['status']})."
            conn.execute(
                "UPDATE tasks SET status='claimed', claimed_at=?, lease_deadline=?, "
                "attempts=attempts+1 WHERE task_id=?",
                (_now(), deadline, row["task_id"]),
            )
            _log_event(conn, "claim", row["task_id"], target,
                       f"attempt {row['attempts'] + 1}, lease until {deadline}"
                       + (" (targeted)" if task_id is not None else ""))
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


def publish_result(
    agent_name: str,
    task_id: str,
    result_content: str,
    attempt: int | None = None,
    force: bool = False,
) -> str:
    """Publish the result and settle (ack) the matching task. Refused for
    an unknown task or an author other than the queue's agent, and — the
    lease fencing — for an `attempt` token staler than the task's current
    attempt: a worker whose lease expired cannot overwrite the result of
    the worker that took the task over. `force` is the orchestrator's
    explicit override for all three checks."""
    author = agent_name.strip().lower()
    with connect() as conn:
        conn.execute("BEGIN IMMEDIATE")
        try:
            row = conn.execute(
                "SELECT agent, status, attempts FROM tasks WHERE task_id=?",
                (task_id,),
            ).fetchone()
            if not force:
                if row is None:
                    conn.execute("COMMIT")
                    return (
                        f"ERROR: unknown task '{task_id}' — nothing to settle "
                        "(force=True to store a result anyway)."
                    )
                if row["agent"] != author:
                    conn.execute("COMMIT")
                    return (
                        f"ERROR: task '{task_id}' belongs to {row['agent']}'s queue; "
                        f"'{author}' cannot settle it (force=True to override)."
                    )
                if attempt is not None and attempt < row["attempts"]:
                    conn.execute("COMMIT")
                    return (
                        f"ERROR: stale attempt token {attempt} < {row['attempts']} — "
                        f"the lease expired and task '{task_id}' was re-offered; "
                        "this result is refused (force=True to override)."
                    )
            conn.execute(
                "INSERT INTO results(task_id, author, content, updated_at) VALUES(?,?,?,?) "
                "ON CONFLICT(task_id) DO UPDATE SET author=excluded.author, "
                "content=excluded.content, updated_at=excluded.updated_at",
                (task_id, author, result_content, _now()),
            )
            cur = conn.execute("UPDATE tasks SET status='done' WHERE task_id=?", (task_id,))
            detail = "settled" if cur.rowcount else "no matching task — result stored anyway"
            if attempt is not None:
                detail += f" (attempt {attempt})"
            if force:
                detail += " (forced)"
            _log_event(conn, "publish", task_id, author, detail)
            conn.execute("COMMIT")
        except Exception:
            conn.execute("ROLLBACK")
            raise
    return f"OK: result for '{task_id}' published by {author}."


def cancel_task(task_id: str) -> str:
    """Cancel a task that is not settled yet (terminal status `cancelled`)."""
    with connect() as conn:
        conn.execute("BEGIN IMMEDIATE")
        try:
            row = conn.execute(
                "SELECT agent, status FROM tasks WHERE task_id=?", (task_id,)
            ).fetchone()
            if row is None:
                conn.execute("COMMIT")
                return f"ERROR: unknown task '{task_id}'."
            if row["status"] in ("done", "cancelled"):
                conn.execute("COMMIT")
                return f"ERROR: task '{task_id}' is already {row['status']}."
            conn.execute(
                "UPDATE tasks SET status='cancelled', claimed_at=NULL, "
                "lease_deadline=NULL WHERE task_id=?",
                (task_id,),
            )
            _log_event(conn, "cancel", task_id, row["agent"],
                       f"was {row['status']}")
            conn.execute("COMMIT")
        except Exception:
            conn.execute("ROLLBACK")
            raise
    return f"OK: task '{task_id}' cancelled."


def requeue_task(task_id: str) -> str:
    """Re-offer a claimed, dead or cancelled task, re-arming three more
    attempts (the attempt counter itself never goes backwards: it is the
    fencing token of publish_result). A done task stays done — push a new
    task_id instead of rewriting history."""
    with connect() as conn:
        conn.execute("BEGIN IMMEDIATE")
        try:
            row = conn.execute(
                "SELECT agent, status, attempts, max_attempts FROM tasks WHERE task_id=?",
                (task_id,),
            ).fetchone()
            if row is None:
                conn.execute("COMMIT")
                return f"ERROR: unknown task '{task_id}'."
            if row["status"] == "queued":
                conn.execute("COMMIT")
                return f"ERROR: task '{task_id}' is already queued."
            if row["status"] == "done":
                conn.execute("COMMIT")
                return (
                    f"ERROR: task '{task_id}' is settled — push a new task_id "
                    "instead of requeueing a done task."
                )
            rearmed = max(row["max_attempts"], row["attempts"] + 3)
            conn.execute(
                "UPDATE tasks SET status='queued', claimed_at=NULL, "
                "lease_deadline=NULL, max_attempts=? WHERE task_id=?",
                (rearmed, task_id),
            )
            _log_event(conn, "requeue", task_id, row["agent"],
                       f"was {row['status']}; max_attempts re-armed to {rearmed}")
            conn.execute("COMMIT")
        except Exception:
            conn.execute("ROLLBACK")
            raise
    return f"OK: task '{task_id}' requeued."


def extend_lease(task_id: str, lease_seconds: int = 900) -> str:
    """Renew the lease of a claimed task (deadline = now + lease_seconds):
    long work extends instead of overshooting and being re-offered."""
    deadline = (datetime.now(timezone.utc) + timedelta(seconds=lease_seconds)).isoformat()
    with connect() as conn:
        conn.execute("BEGIN IMMEDIATE")
        try:
            row = conn.execute(
                "SELECT agent, status FROM tasks WHERE task_id=?", (task_id,)
            ).fetchone()
            if row is None:
                conn.execute("COMMIT")
                return f"ERROR: unknown task '{task_id}'."
            if row["status"] != "claimed":
                conn.execute("COMMIT")
                return (
                    f"ERROR: task '{task_id}' is not claimed "
                    f"(status: {row['status']}) — no lease to extend."
                )
            conn.execute(
                "UPDATE tasks SET lease_deadline=? WHERE task_id=?",
                (deadline, task_id),
            )
            _log_event(conn, "extend", task_id, row["agent"],
                       f"lease until {deadline}")
            conn.execute("COMMIT")
        except Exception:
            conn.execute("ROLLBACK")
            raise
    return f"OK: lease of '{task_id}' extended until {deadline}."


def read_result(task_id: str) -> str:
    with connect() as conn:
        row = conn.execute(
            "SELECT author, content, updated_at FROM results WHERE task_id=?",
            (task_id,),
        ).fetchone()
    if row is None:
        return f"ERROR: no result found for '{task_id}'."
    return json.dumps(dict(row), ensure_ascii=False)


_TOPIC_RE = re.compile(r"^[a-z0-9-]{1,64}$")


def _parse_capabilities(capabilities: str | None) -> dict | str:
    """Validate and normalize capabilities JSON. Returns the parsed dict on
    success or an ERROR: string on failure."""
    if capabilities is None:
        return {}
    try:
        parsed = json.loads(capabilities)
    except Exception:
        return "ERROR: capabilities must be a JSON object."
    if not isinstance(parsed, dict):
        return "ERROR: capabilities must be a JSON object."
    return parsed


def heartbeat(agent: str, ttl_seconds: int = 120, capabilities: str | None = None) -> str:
    """Post or refresh a presence heartbeat with a capability card.
    Upserts the presence row; absent capabilities keep the existing card."""
    agent = agent.strip().lower()
    if not agent:
        return "ERROR: empty agent name."
    parsed = _parse_capabilities(capabilities)
    if isinstance(parsed, str):
        return parsed
    with connect() as conn:
        row = conn.execute(
            "SELECT ttl_seconds, capabilities FROM presence WHERE agent=?", (agent,)
        ).fetchone()
        if capabilities is None:
            if row is None:
                stored_capabilities = "{}"
            else:
                stored_capabilities = row["capabilities"]
        else:
            stored_capabilities = json.dumps(parsed, ensure_ascii=False)
        alive_until = datetime.fromisoformat(_now()) + timedelta(seconds=ttl_seconds)
        conn.execute(
            "INSERT INTO presence(agent, last_seen, ttl_seconds, capabilities) "
            "VALUES(?,?,?,?) ON CONFLICT(agent) DO UPDATE SET "
            "last_seen=excluded.last_seen, ttl_seconds=excluded.ttl_seconds, "
            "capabilities=excluded.capabilities",
            (agent, _now(), ttl_seconds, stored_capabilities),
        )
    return json.dumps(
        {"agent": agent, "alive_until": alive_until.isoformat()}, ensure_ascii=False
    )


def touch_presence(agent: str) -> None:
    """Refresh last_seen only, keeping the declared TTL and capability card.
    Creates a default-TTL row for a previously absent agent; a blank name is
    a silent no-op so the piggyback cannot fail."""
    agent = agent.strip().lower()
    if not agent:
        return
    with connect() as conn:
        row = conn.execute(
            "SELECT ttl_seconds, capabilities FROM presence WHERE agent=?", (agent,)
        ).fetchone()
        if row is None:
            ttl = DEFAULT_PRESENCE_TTL
            capabilities = "{}"
        else:
            ttl = row["ttl_seconds"]
            capabilities = row["capabilities"]
        conn.execute(
            "INSERT INTO presence(agent, last_seen, ttl_seconds, capabilities) "
            "VALUES(?,?,?,?) ON CONFLICT(agent) DO UPDATE SET last_seen=excluded.last_seen",
            (agent, _now(), ttl, capabilities),
        )


def list_presence() -> str:
    """Return every known agent (registered or seen) with a computed status."""
    with connect() as conn:
        rows = conn.execute(
            """
            SELECT a.name, a.description, p.last_seen, p.ttl_seconds, p.capabilities
            FROM agents a LEFT JOIN presence p ON a.name = p.agent
            UNION
            SELECT p.agent AS name, '' AS description, p.last_seen, p.ttl_seconds,
                   p.capabilities
            FROM presence p
            WHERE p.agent NOT IN (SELECT name FROM agents)
            ORDER BY name
            """
        ).fetchall()
    now = datetime.fromisoformat(_now())
    result = []
    for row in rows:
        last_seen = row["last_seen"]
        ttl = row["ttl_seconds"]
        caps = row["capabilities"] or "{}"
        if last_seen is None:
            status = "unknown"
        else:
            deadline = datetime.fromisoformat(last_seen) + timedelta(seconds=ttl or 0)
            status = "alive" if now < deadline else "asleep"
        result.append(
            {
                "agent": row["name"],
                "status": status,
                "last_seen": last_seen,
                "ttl_seconds": ttl,
                "capabilities": json.loads(caps),
                "description": row["description"] or "",
            }
        )
    return json.dumps(result, ensure_ascii=False)


def announce(author: str, topic: str, message: str) -> str:
    """Append a message to the global channel and return its sequence number."""
    topic = topic.strip().lower()
    if not _TOPIC_RE.match(topic):
        return "ERROR: topic must match ^[a-z0-9-]{1,64}$."
    if len(message.encode("utf-8")) > 16 * 1024:
        return "ERROR: message exceeds 16 KiB."
    author = author.strip().lower()
    with connect() as conn:
        cur = conn.execute(
            "INSERT INTO channel(author, at, topic, message) VALUES(?,?,?,?)",
            (author, _now(), topic, message),
        )
        _log_event(conn, "announce", agent=author, detail=topic)
        seq = cur.lastrowid
    return json.dumps({"seq": seq}, ensure_ascii=False)


def read_channel(
    agent: str | None = None,
    since_seq: int | None = None,
    topic: str | None = None,
    limit: int = 100,
) -> str:
    """Read channel entries. Pure reads use since_seq; cursor reads use agent
    and advance the stored cursor to the last returned seq."""
    if limit < 1:
        limit = 1
    elif limit > 1000:
        limit = 1000
    if agent is None and since_seq is None:
        return "ERROR: provide either agent or since_seq."
    if agent is not None and since_seq is None and topic is not None:
        return "ERROR: topic filter is not allowed with a cursor read."
    with connect() as conn:
        if since_seq is not None:
            if topic is not None:
                rows = conn.execute(
                    "SELECT seq, author, at, topic, message FROM channel "
                    "WHERE seq > ? AND topic = ? ORDER BY seq ASC LIMIT ?",
                    (since_seq, topic, limit),
                ).fetchall()
            else:
                rows = conn.execute(
                    "SELECT seq, author, at, topic, message FROM channel "
                    "WHERE seq > ? ORDER BY seq ASC LIMIT ?",
                    (since_seq, limit),
                ).fetchall()
        else:
            conn.execute("BEGIN IMMEDIATE")
            try:
                cursor = conn.execute(
                    "SELECT last_seq FROM channel_cursor WHERE agent=?", (agent,)
                ).fetchone()
                last_seq = cursor["last_seq"] if cursor else 0
                rows = conn.execute(
                    "SELECT seq, author, at, topic, message FROM channel "
                    "WHERE seq > ? ORDER BY seq ASC LIMIT ?",
                    (last_seq, limit),
                ).fetchall()
                if rows:
                    new_last = rows[-1]["seq"]
                    conn.execute(
                        "INSERT INTO channel_cursor(agent, last_seq) VALUES(?,?) "
                        "ON CONFLICT(agent) DO UPDATE SET last_seq=excluded.last_seq",
                        (agent, new_last),
                    )
                conn.execute("COMMIT")
            except Exception:
                conn.execute("ROLLBACK")
                raise
    return json.dumps([dict(r) for r in rows], ensure_ascii=False)


def notify(author: str, target_agent: str, message: str) -> str:
    """Send a directed signal to one agent — point-to-point, unlike the
    broadcast channel; the recipient picks it up with poll(). The target must
    be a registered agent (typo detection, same as push_task); the message is
    a notice, capped like the channel at 16 KiB."""
    target = target_agent.strip().lower()
    author = author.strip().lower()
    if not author:
        return "ERROR: empty author."
    if not target:
        return "ERROR: empty target agent."
    if len(message.encode("utf-8")) > 16 * 1024:
        return "ERROR: message exceeds 16 KiB."
    with connect() as conn:
        known = _known_agents(conn)
        if target not in known:
            return (
                f"ERROR: agent '{target_agent}' not registered. "
                f"Known agents: {known or 'none — call register_agent first'}."
            )
        cur = conn.execute(
            "INSERT INTO notifications(target, author, at, message) VALUES(?,?,?,?)",
            (target, author, _now(), message),
        )
        _log_event(conn, "notify", agent=author, detail=target)
        seq = cur.lastrowid
    return json.dumps({"seq": seq}, ensure_ascii=False)


def poll(agent: str, limit: int = 100) -> str:
    """Read an agent's own directed notifications and advance its cursor to
    the last returned seq (at-least-once); an empty read advances nothing."""
    target = agent.strip().lower()
    if not target:
        return "ERROR: empty agent."
    if limit < 1:
        limit = 1
    elif limit > 1000:
        limit = 1000
    with connect() as conn:
        conn.execute("BEGIN IMMEDIATE")
        try:
            cursor = conn.execute(
                "SELECT last_seq FROM notification_cursor WHERE target=?", (target,)
            ).fetchone()
            last_seq = cursor["last_seq"] if cursor else 0
            rows = conn.execute(
                "SELECT seq, author, at, message FROM notifications "
                "WHERE target=? AND seq > ? ORDER BY seq ASC LIMIT ?",
                (target, last_seq, limit),
            ).fetchall()
            if rows:
                conn.execute(
                    "INSERT INTO notification_cursor(target, last_seq) VALUES(?,?) "
                    "ON CONFLICT(target) DO UPDATE SET last_seq=excluded.last_seq",
                    (target, rows[-1]["seq"]),
                )
            conn.execute("COMMIT")
        except Exception:
            conn.execute("ROLLBACK")
            raise
    return json.dumps([dict(r) for r in rows], ensure_ascii=False)


def get_system_state() -> str:
    with connect() as conn:
        _requeue_expired(conn)
        agents = {
            r["name"]: r["description"]
            for r in conn.execute("SELECT name, description FROM agents ORDER BY name")
        }
        queues = {
            name: {
                status: conn.execute(
                    "SELECT COUNT(*) c FROM tasks WHERE agent=? AND status=?",
                    (name, status),
                ).fetchone()["c"]
                for status in ("queued", "claimed", "dead", "cancelled")
            }
            for name in agents
        }
        done = [
            r["task_id"]
            for r in conn.execute("SELECT task_id FROM results ORDER BY updated_at")
        ]
        pres_rows = conn.execute(
            """
            SELECT a.name, p.last_seen, p.ttl_seconds
            FROM agents a LEFT JOIN presence p ON a.name = p.agent
            UNION
            SELECT p.agent AS name, p.last_seen, p.ttl_seconds
            FROM presence p
            WHERE p.agent NOT IN (SELECT name FROM agents)
            """
        ).fetchall()
    now = datetime.fromisoformat(_now())
    presence: dict[str, str] = {}
    for r in pres_rows:
        if r["last_seen"] is None:
            presence[r["name"]] = "unknown"
        else:
            deadline = datetime.fromisoformat(r["last_seen"]) + timedelta(
                seconds=r["ttl_seconds"] or 0
            )
            presence[r["name"]] = "alive" if now < deadline else "asleep"
    return json.dumps(
        {"agents": agents, "queues": queues, "completed_tasks": done, "presence": presence},
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
