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

import contextvars
import fnmatch
import hashlib
import json
import os
import re
import sys
import time
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Iterator

try:  # installed package: `iab` maps to this directory (pyproject.toml)
    from . import dialect
except ImportError:  # bare script or test: the directory is on sys.path
    import dialect

Connection = dialect.Connection

# The machine a hub is serving the current operation for (empty in local
# mode): journaled with every event, so the history says WHERE an action
# came from. Set by the hub per request; a context variable, so one
# thread's request never stamps another's events.
CALLER_MACHINE: contextvars.ContextVar[str] = contextvars.ContextVar(
    "iab_caller_machine", default="")


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
WORKSPACES_FILE = DATA_DIR / "workspaces.json"

# Identity of an agent = one INSTANCE: a harness (the client/CLI carrying
# the model: claude, kimi, codewhale…) on a workspace (the repository or
# folder it works in: homelab, infra…), written `harness@workspace`. Both
# components share one character class; a bare harness (legacy names,
# headless workers) remains a valid name.
_COMPONENT = r"[a-z0-9][a-z0-9._-]{0,39}"
_NAME_RE = re.compile(rf"^{_COMPONENT}(@{_COMPONENT})?$")
_PATTERN_RE = re.compile(r"[*]")


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def split_name(name: str) -> tuple[str, str]:
    """`harness@workspace` → (harness, workspace); a bare name → (name, "")."""
    name = name.strip().lower()
    if "@" in name:
        harness, _, workspace = name.partition("@")
        return harness, workspace
    return name, ""


def slugify_component(text: str) -> str:
    """Fold a folder or client name into one identity component: lowercase,
    out-of-class runs → '-', leading punctuation dropped, 40 chars max."""
    slug = re.sub(r"[^a-z0-9._-]+", "-", text.lower()).lstrip("._-").rstrip("-")
    return slug[:40].rstrip("-") or "root"


def _workspace_aliases() -> dict[str, str]:
    """Operator table {path: workspace} (IAB_WORKSPACES, else the data
    directory's workspaces.json); keys are realpath-normalized. Unreadable
    or malformed → empty, the derivation falls back to the directory name."""
    path = Path(os.environ.get("IAB_WORKSPACES") or WORKSPACES_FILE)
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    if not isinstance(raw, dict):
        return {}
    return {
        os.path.realpath(os.path.expanduser(str(k))): slugify_component(str(v))
        for k, v in raw.items()
        if isinstance(v, str) and v.strip()
    }


def derive_workspace(cwd: str | os.PathLike | None = None) -> tuple[str, str]:
    """Workspace slug of a launch directory and how it was found: `alias`
    (operator table, longest matching prefix), `git` (name of the first
    ancestor holding a `.git` — file or directory, so a submodule yields its
    own name, not its superproject's) or `cwd` (the directory's own name).
    An alias never reaches past a repository boundary: a `.git` strictly
    between the alias path and the launch directory wins, so an alias on a
    parent folder (a home directory) does not swallow the repositories
    beneath it, while an alias on a repository still covers its subfolders."""
    real = Path(os.path.realpath(cwd if cwd is not None else os.getcwd()))
    git_top = next((c for c in (real, *real.parents) if (c / ".git").exists()), None)
    best = ""
    for prefix, slug in _workspace_aliases().items():
        pre = Path(prefix)
        if real != pre and pre not in real.parents:
            continue
        if git_top is not None and git_top != pre and git_top not in pre.parents:
            continue  # the repository lies below the alias: it wins
        if len(prefix) > len(best):
            best, chosen = prefix, slug
    if best:
        return chosen, "alias"
    if git_top is not None:
        return slugify_component(git_top.name), "git"
    return slugify_component(real.name), "cwd"


def resolve_identity(
    environ: dict | None = None, cwd: str | os.PathLike | None = None
) -> dict:
    """Who the caller is, from most explicit to most inferred. Harness from
    IAB_AGENT_NAME (legacy ORCHESTRATOR_AGENT_NAME honored); when it already
    holds an `@`, the full name wins (`workspace_source: name`). Otherwise
    the workspace comes from IAB_WORKSPACE (`env`), else is derived from the
    launch directory (`alias`/`git`/`cwd`, see derive_workspace). Without a
    harness, `source` is `none` and `agent_name` is None: the worker asks the
    operator. `model` echoes IAB_MODEL (a declaration, never a guess)."""
    env = os.environ if environ is None else environ
    real_cwd = os.path.realpath(cwd if cwd is not None else os.getcwd())
    model = (env.get("IAB_MODEL") or "").strip() or None
    forced = (env.get("IAB_AGENT_NAME") or env.get("ORCHESTRATOR_AGENT_NAME") or "").strip()
    if not forced:
        return {"source": "none", "agent_name": None, "harness": None,
                "workspace": None, "workspace_source": None, "cwd": real_cwd,
                "model": model}
    forced = forced.lower()
    if "@" in forced:
        harness, workspace = split_name(forced)
        ws_source = "name"
    else:
        harness = forced
        workspace = (env.get("IAB_WORKSPACE") or "").strip().lower()
        if workspace:
            workspace, ws_source = slugify_component(workspace), "env"
        else:
            workspace, ws_source = derive_workspace(real_cwd)
    name = f"{harness}@{workspace}" if workspace else harness
    return {"source": "env", "agent_name": name, "harness": harness,
            "workspace": workspace, "workspace_source": ws_source,
            "cwd": real_cwd, "model": model}


def project_key(cwd: str | os.PathLike | None = None) -> str:
    """Stable per-project key from the working directory: realpath (so
    symlinks and relative paths converge), case-folded on the platforms
    whose filesystems are case-insensitive, then slug + short hash."""
    real = os.path.realpath(cwd if cwd is not None else os.getcwd())
    normalized = real.lower() if sys.platform in ("win32", "darwin") else real
    digest = hashlib.sha256(normalized.encode("utf-8")).hexdigest()[:12]
    slug = re.sub(r"[^A-Za-z0-9_-]+", "-", Path(real).name)[:40] or "root"
    return f"{slug}-{digest}"


def _resolve_db(cwd: str | os.PathLike | None = None) -> tuple[Path | str, str]:
    for var in ("IAB_DB", "ORCHESTRATOR_DB"):
        value = os.environ.get(var)
        if value:
            # A postgresql:// URL stays a string; anything else is a file.
            return (value if dialect.is_url(value) else Path(value)), f"env:{var}"
    if GLOBAL_DB.exists():
        return GLOBAL_DB, "global (pre-isolation bus kept — set IAB_DB to opt out)"
    if LEGACY_DB.exists():
        return LEGACY_DB, "legacy (pre-rename bus kept — set IAB_DB to opt out)"
    return DATA_DIR / "projects" / f"{project_key(cwd)}.db", "project"


def db_path(cwd: str | os.PathLike | None = None) -> Path | str:
    return _resolve_db(cwd)[0]


def db_info() -> str:
    """Resolved bus database and how it was chosen — the rendezvous
    diagnosis: participants of one project must all see the same path."""
    path, source = _resolve_db()
    return json.dumps(
        {"db": dialect.describe(path), "db_source": source, "project_key": project_key()},
        ensure_ascii=False,
    )


def connect() -> Connection:
    """Open the bus database through its dialect, schema and additive
    migrations applied. The caller closes it (see _db)."""
    conn = dialect.for_target(db_path()).connect()
    try:
        conn.dialect.prepare(conn, _setup)
    except BaseException:
        conn.close()
        raise
    return conn


@contextmanager
def _db() -> Iterator[Connection]:
    """One connection for one operation, closed on the way out."""
    conn = connect()
    try:
        yield conn
    finally:
        conn.close()


def _write(conn: Connection):
    """The write transaction of the engine (writer lock held from the first
    statement; commit on normal exit, rollback on exception)."""
    return conn.dialect.write(conn)


def _insert_id(conn: Connection, sql: str, params: tuple) -> int:
    return conn.dialect.insert_id(conn, sql, params)


def _setup(conn: Connection) -> None:
    """The bus tables, then their additive migrations."""
    conn.dialect.create_schema(conn)
    _migrate(conn)


def _add_column(conn: Connection, table: str, declaration: str) -> None:
    conn.execute(conn.dialect.ddl(f"ALTER TABLE {table} ADD COLUMN {declaration}"))


def _migrate(conn: Connection) -> None:
    """Additive migrations only — the database is a long-lived rendezvous
    shared by several clients; existing files must keep working."""
    columns = conn.dialect.columns(conn, "tasks")
    if "max_attempts" not in columns:
        _add_column(conn, "tasks", "max_attempts INTEGER NOT NULL DEFAULT 3")
    columns = conn.dialect.columns(conn, "agents")
    for column in ("harness", "workspace", "cwd"):
        if column not in columns:
            _add_column(conn, "agents", f"{column} TEXT NOT NULL DEFAULT ''")
    # Rows registered before the composed identity: their harness is their
    # name (a legacy bare name stays addressable, see _resolve_target).
    # Split in Python: string functions differ between engines.
    for row in conn.execute("SELECT name FROM agents WHERE harness = ''").fetchall():
        harness, workspace = split_name(row["name"])
        conn.execute("UPDATE agents SET harness=?, workspace=? WHERE name=?",
                     (harness, workspace, row["name"]))
    columns = conn.dialect.columns(conn, "presence")
    if "model" not in columns:
        _add_column(conn, "presence", "model TEXT NOT NULL DEFAULT ''")
    columns = conn.dialect.columns(conn, "notifications")
    if "broadcast_id" not in columns:
        _add_column(conn, "notifications", "broadcast_id INTEGER")
    if "reply_to" not in columns:
        _add_column(conn, "notifications", "reply_to INTEGER")
    if "wake" not in columns:
        _add_column(conn, "notifications", "wake INTEGER NOT NULL DEFAULT 0")
    if "machine" not in conn.dialect.columns(conn, "events"):
        _add_column(conn, "events", "machine TEXT NOT NULL DEFAULT ''")


def _log_event(
    conn: Connection,
    event: str,
    task_id: str | None = None,
    agent: str | None = None,
    detail: str = "",
) -> None:
    conn.execute(
        "INSERT INTO events(task_id, agent, event, at, detail, machine) VALUES(?,?,?,?,?,?)",
        (task_id, agent, event, _now(), detail, CALLER_MACHINE.get()),
    )


def register_agent(
    name: str, description: str = "", cwd: str = "", model: str | None = None
) -> str:
    """Declare an agent instance (creates its queue). `name` is
    `harness@workspace` or a bare harness; `cwd` records where it works and
    `model` which model carries it right now (session state, kept in the
    presence row)."""
    name = name.strip().lower()
    if not name:
        return "ERROR: empty agent name."
    if not _NAME_RE.match(name):
        return (
            f"ERROR: invalid agent name '{name}' — expected harness[@workspace], "
            "each component [a-z0-9][a-z0-9._-]{0,39}."
        )
    harness, workspace = split_name(name)
    with _db() as conn, _write(conn):
        known = conn.execute("SELECT 1 FROM agents WHERE name=?", (name,)).fetchone()
        conn.execute(
            "INSERT INTO agents(name, description, registered_at, harness, workspace, cwd) "
            "VALUES(?,?,?,?,?,?) ON CONFLICT(name) DO UPDATE SET "
            "description=excluded.description, harness=excluded.harness, "
            "workspace=excluded.workspace, "
            "cwd=CASE WHEN excluded.cwd='' THEN agents.cwd ELSE excluded.cwd END",
            (name, description, _now(), harness, workspace, cwd or ""),
        )
        if known is None:
            _log_event(conn, "register", agent=name, detail=description)
        if model:
            _set_model(conn, name, model.strip(), force=True)
    return f"OK: agent '{name}' registered."


def rename_agent(old: str, new: str) -> str:
    """Move an identity — queue, results, notifications, cursors, presence,
    journal — to a new name in one transaction (migration of a legacy bare
    name to `harness@workspace`). Refused when `new` already exists."""
    old = old.strip().lower()
    new = new.strip().lower()
    if not old or not new:
        return "ERROR: empty agent name."
    if not _NAME_RE.match(new):
        return f"ERROR: invalid agent name '{new}' — expected harness[@workspace]."
    harness, workspace = split_name(new)
    with _db() as conn:
        with _write(conn):
            if conn.execute("SELECT 1 FROM agents WHERE name=?", (old,)).fetchone() is None:
                return f"ERROR: agent '{old}' not registered."
            if conn.execute("SELECT 1 FROM agents WHERE name=?", (new,)).fetchone():
                return f"ERROR: agent '{new}' already exists — rename refused."
            conn.execute(
                "UPDATE agents SET name=?, harness=?, workspace=? WHERE name=?",
                (new, harness, workspace, old),
            )
            for table, column in (
                ("tasks", "agent"), ("results", "author"), ("events", "agent"),
                ("notifications", "target"), ("notifications", "author"),
                ("notification_cursor", "target"), ("channel", "author"),
                ("channel_cursor", "agent"),
            ):
                conn.execute(f"UPDATE {table} SET {column}=? WHERE {column}=?", (new, old))
            if conn.execute("SELECT 1 FROM presence WHERE agent=?", (new,)).fetchone():
                conn.execute("DELETE FROM presence WHERE agent=?", (old,))
            else:
                conn.execute("UPDATE presence SET agent=? WHERE agent=?", (new, old))
            _log_event(conn, "rename", agent=new, detail=f"was {old}")
    return f"OK: agent '{old}' renamed to '{new}'."


def resolve_agent(text: str) -> tuple[str | None, str | None]:
    """Read-only: the registered instance an address designates, with the
    same rules as every operation (see _resolve_target)."""
    with _db() as conn:
        return _resolve_target(conn, text)


def task_agent(task_id: str) -> str | None:
    """Read-only: the queue a task belongs to, or None."""
    with _db() as conn:
        row = conn.execute("SELECT agent FROM tasks WHERE task_id=?", (task_id,)).fetchone()
    return row["agent"] if row else None


def _known_agents(conn: Connection) -> list[str]:
    return [r["name"] for r in conn.execute("SELECT name FROM agents ORDER BY name")]


def _resolve_target(conn: Connection, text: str) -> tuple[str | None, str | None]:
    """Turn an address into one registered instance: an exact name, or a
    bare harness that designates exactly one registered instance (a legacy
    bare name counts). Ambiguity is refused with the candidates — a message
    or a task routed at random is worse than a refusal. Wildcards are
    reserved to broadcast. Returns (name, None) or (None, "ERROR: …")."""
    target = text.strip().lower()
    if not target:
        return None, "ERROR: empty agent name."
    if _PATTERN_RE.search(target):
        return None, (
            f"ERROR: '{text}' is a pattern — reserved to broadcast, not a valid target."
        )
    if "@" in target:
        if conn.execute("SELECT 1 FROM agents WHERE name=?", (target,)).fetchone():
            return target, None
    else:
        # A bare harness names the set of its instances; a legacy bare row
        # (harness = name) is one of them, never a silent default — that
        # was the bug: replies addressed to a shared name, read by nobody.
        candidates = [
            r["name"] for r in conn.execute(
                "SELECT name FROM agents WHERE harness=? ORDER BY name", (target,)
            )
        ]
        if len(candidates) == 1:
            return candidates[0], None
        if candidates:
            hint = (
                f" The legacy bare name '{target}' is one of them: rename_agent it "
                "to harness@workspace to lift the ambiguity."
                if target in candidates else ""
            )
            return None, (
                f"ERROR: '{text}' is ambiguous — instances: {', '.join(candidates)}. "
                f"Address one of them.{hint}"
            )
    known = _known_agents(conn)
    return None, (
        f"ERROR: agent '{text}' not registered. "
        f"Known agents: {known or 'none — call register_agent first'}."
    )


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
    with _db() as conn, _write(conn):
        # Explicit refusal of an unregistered or ambiguous agent: keeps the
        # typo detection that hard-coded queues provided, without freezing
        # the cast.
        target, error = _resolve_target(conn, target_agent)
        if error:
            return error
        if conn.execute("SELECT 1 FROM tasks WHERE task_id=?", (task_id,)).fetchone():
            return f"ERROR: task_id '{task_id}' already exists."
        conn.execute(
            "INSERT INTO tasks(task_id, agent, payload, priority, created_at, "
            "max_attempts) VALUES(?,?,?,?,?,?)",
            (task_id, target, payload, priority, _now(), max_attempts),
        )
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
    if not agent.strip():
        return "ERROR: empty agent."
    interval_s = max(0.2, float(interval_s))
    deadline = time.monotonic() + max(0.0, float(timeout_s))
    conn = connect()  # one connection for the whole wait, not one per round
    try:
        target, error = _resolve_target(conn, agent)
        if error:
            return error
        while True:
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
            conn.dialect.pause(conn, min(interval_s, max(0.0, deadline - time.monotonic())))
    finally:
        conn.close()


def _requeue_expired(conn: Connection) -> None:
    """Re-offer (or dead-letter) the tasks whose lease expired. Callers
    outside a write transaction race each other: every UPDATE re-checks
    the row it read (still claimed, still expired, same attempt), so a
    task claimed again in between is left alone and each transition is
    journaled once."""
    now = _now()
    expired = conn.execute(
        "SELECT task_id, agent, attempts, max_attempts FROM tasks "
        "WHERE status='claimed' AND lease_deadline < ?",
        (now,),
    ).fetchall()
    for row in expired:
        if row["attempts"] >= row["max_attempts"]:
            if not conn.execute(
                "UPDATE tasks SET status='dead', claimed_at=NULL, lease_deadline=NULL "
                "WHERE task_id=? AND status='claimed' AND lease_deadline < ? "
                "AND attempts=?",
                (row["task_id"], now, row["attempts"]),
            ).rowcount:
                continue
            _log_event(conn, "dead", row["task_id"], row["agent"],
                       f"lease expired after attempt {row['attempts']}/"
                       f"{row['max_attempts']} — dead-lettered (requeue to revive)")
        else:
            if not conn.execute(
                "UPDATE tasks SET status='queued', claimed_at=NULL, lease_deadline=NULL "
                "WHERE task_id=? AND status='claimed' AND lease_deadline < ? "
                "AND attempts=?",
                (row["task_id"], now, row["attempts"]),
            ).rowcount:
                continue
            _log_event(conn, "expire", row["task_id"], row["agent"],
                       "lease expired — task re-offered")


def claim_task(agent_name: str, lease_seconds: int = 900, task_id: str | None = None) -> str:
    """Atomically claim the highest-priority queued task of one's queue,
    under lease — or one specific queued task of one's own queue when
    task_id is given (logged escape hatch; no cross-queue poaching)."""
    deadline = (datetime.now(timezone.utc) + timedelta(seconds=lease_seconds)).isoformat()
    with _db() as conn:
        with _write(conn):
            target, error = _resolve_target(conn, agent_name)
            if error:
                return error
            _requeue_expired(conn)
            if task_id is None:
                row = conn.execute(
                    "SELECT task_id, payload, priority, attempts, created_at FROM tasks "
                    "WHERE agent=? AND status='queued' "
                    "ORDER BY priority DESC, created_at ASC LIMIT 1",
                    (target,),
                ).fetchone()
                if row is None:
                    return "NO_TASK"
            else:
                row = conn.execute(
                    "SELECT task_id, agent, status, payload, priority, attempts, "
                    "created_at FROM tasks WHERE task_id=?",
                    (task_id,),
                ).fetchone()
                if row is None:
                    return f"ERROR: unknown task '{task_id}'."
                if row["agent"] != target:
                    return (
                        f"ERROR: task '{task_id}' belongs to {row['agent']}'s queue — "
                        "no cross-queue claiming."
                    )
                if row["status"] != "queued":
                    return f"ERROR: task '{task_id}' is not queued (status: {row['status']})."
            conn.execute(
                "UPDATE tasks SET status='claimed', claimed_at=?, lease_deadline=?, "
                "attempts=attempts+1 WHERE task_id=?",
                (_now(), deadline, row["task_id"]),
            )
            _log_event(conn, "claim", row["task_id"], target,
                       f"attempt {row['attempts'] + 1}, lease until {deadline}"
                       + (" (targeted)" if task_id is not None else ""))
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
    with _db() as conn:
        with _write(conn):
            row = conn.execute(
                "SELECT agent, status, attempts FROM tasks WHERE task_id=?",
                (task_id,),
            ).fetchone()
            if not force:
                if row is None:
                    return (
                        f"ERROR: unknown task '{task_id}' — nothing to settle "
                        "(force=True to store a result anyway)."
                    )
                if row["agent"] != author:
                    return (
                        f"ERROR: task '{task_id}' belongs to {row['agent']}'s queue; "
                        f"'{author}' cannot settle it (force=True to override)."
                    )
                if attempt is not None and attempt < row["attempts"]:
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
    return f"OK: result for '{task_id}' published by {author}."


def cancel_task(task_id: str) -> str:
    """Cancel a task that is not settled yet (terminal status `cancelled`)."""
    with _db() as conn:
        with _write(conn):
            row = conn.execute(
                "SELECT agent, status FROM tasks WHERE task_id=?", (task_id,)
            ).fetchone()
            if row is None:
                return f"ERROR: unknown task '{task_id}'."
            if row["status"] in ("done", "cancelled"):
                return f"ERROR: task '{task_id}' is already {row['status']}."
            conn.execute(
                "UPDATE tasks SET status='cancelled', claimed_at=NULL, "
                "lease_deadline=NULL WHERE task_id=?",
                (task_id,),
            )
            _log_event(conn, "cancel", task_id, row["agent"],
                       f"was {row['status']}")
    return f"OK: task '{task_id}' cancelled."


def requeue_task(task_id: str) -> str:
    """Re-offer a claimed, dead or cancelled task, re-arming three more
    attempts (the attempt counter itself never goes backwards: it is the
    fencing token of publish_result). A done task stays done — push a new
    task_id instead of rewriting history."""
    with _db() as conn:
        with _write(conn):
            row = conn.execute(
                "SELECT agent, status, attempts, max_attempts FROM tasks WHERE task_id=?",
                (task_id,),
            ).fetchone()
            if row is None:
                return f"ERROR: unknown task '{task_id}'."
            if row["status"] == "queued":
                return f"ERROR: task '{task_id}' is already queued."
            if row["status"] == "done":
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
    return f"OK: task '{task_id}' requeued."


def extend_lease(task_id: str, lease_seconds: int = 900) -> str:
    """Renew the lease of a claimed task (deadline = now + lease_seconds):
    long work extends instead of overshooting and being re-offered."""
    deadline = (datetime.now(timezone.utc) + timedelta(seconds=lease_seconds)).isoformat()
    with _db() as conn:
        with _write(conn):
            row = conn.execute(
                "SELECT agent, status FROM tasks WHERE task_id=?", (task_id,)
            ).fetchone()
            if row is None:
                return f"ERROR: unknown task '{task_id}'."
            if row["status"] != "claimed":
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
    return f"OK: lease of '{task_id}' extended until {deadline}."


def read_result(task_id: str) -> str:
    with _db() as conn:
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


def _set_model(conn: Connection, agent: str, model: str, force: bool) -> None:
    """Record the model carrying an instance in its presence row (created
    at default TTL if absent). `force` overwrites a stored value — an
    explicit declaration; without it (environment echo) a declared model
    is kept."""
    # One upsert: a heartbeat written meanwhile outside the writer lock (an
    # autocommit statement) must not make this INSERT collide.
    conn.execute(
        "INSERT INTO presence(agent, last_seen, ttl_seconds, capabilities, model) "
        "VALUES(?,?,?,?,?) ON CONFLICT(agent) DO UPDATE SET model = CASE "
        "WHEN ? OR presence.model = '' THEN excluded.model ELSE presence.model END",
        (agent, _now(), DEFAULT_PRESENCE_TTL, "{}", model, bool(force)),
    )


def heartbeat(
    agent: str, ttl_seconds: int = 120, capabilities: str | None = None,
    model: str | None = None,
) -> str:
    """Post or refresh a presence heartbeat with a capability card and, when
    given, the model carrying the instance. Upserts the presence row; absent
    capabilities keep the existing card, absent model keeps the stored one.
    The agent must be registered (an instance declares itself after
    register_agent)."""
    parsed = _parse_capabilities(capabilities)
    if isinstance(parsed, str):
        return parsed
    with _db() as conn:
        agent, error = _resolve_target(conn, agent)
        if error:
            return error
        row = conn.execute(
            "SELECT ttl_seconds, capabilities, model FROM presence WHERE agent=?", (agent,)
        ).fetchone()
        if capabilities is None:
            if row is None:
                stored_capabilities = "{}"
            else:
                stored_capabilities = row["capabilities"]
        else:
            stored_capabilities = json.dumps(parsed, ensure_ascii=False)
        stored_model = (model or "").strip() or (row["model"] if row else "")
        alive_until = datetime.fromisoformat(_now()) + timedelta(seconds=ttl_seconds)
        conn.execute(
            "INSERT INTO presence(agent, last_seen, ttl_seconds, capabilities, model) "
            "VALUES(?,?,?,?,?) ON CONFLICT(agent) DO UPDATE SET "
            "last_seen=excluded.last_seen, ttl_seconds=excluded.ttl_seconds, "
            "capabilities=excluded.capabilities, model=excluded.model",
            (agent, _now(), ttl_seconds, stored_capabilities, stored_model),
        )
    return json.dumps(
        {"agent": agent, "alive_until": alive_until.isoformat(),
         "model": stored_model or None}, ensure_ascii=False
    )


def touch_presence(agent: str, model: str | None = None) -> None:
    """Refresh last_seen only, keeping the declared TTL and capability card.
    Creates a default-TTL row for a previously absent agent; a blank name is
    a silent no-op so the piggyback cannot fail. A `model` fills an empty
    slot only (environment echo never overrides a declaration)."""
    agent = agent.strip().lower()
    if not agent:
        return
    with _db() as conn:
        row = conn.execute(
            "SELECT ttl_seconds, capabilities, model FROM presence WHERE agent=?", (agent,)
        ).fetchone()
        if row is None:
            ttl, capabilities, stored_model = DEFAULT_PRESENCE_TTL, "{}", ""
        else:
            ttl, capabilities, stored_model = row["ttl_seconds"], row["capabilities"], row["model"]
        if model and not stored_model:
            stored_model = model.strip()
        conn.execute(
            "INSERT INTO presence(agent, last_seen, ttl_seconds, capabilities, model) "
            "VALUES(?,?,?,?,?) ON CONFLICT(agent) DO UPDATE SET "
            "last_seen=excluded.last_seen, model=excluded.model",
            (agent, _now(), ttl, capabilities, stored_model),
        )


def touch_identity(identity: dict) -> None:
    """Piggyback of the MCP server: the resolved identity of a connected
    client is a present instance. Registers it on first sight (empty
    description, launch directory recorded) so its queue, cursors and
    presence exist under its own composed name — a client named by its
    environment never has to call register_agent — then refreshes its
    presence. Failures must not break the tool call: callers swallow them."""
    name = str(identity.get("agent_name") or "").strip().lower()
    if not name or not _NAME_RE.match(name):
        return
    with _db() as conn, _write(conn):
        if conn.execute("SELECT 1 FROM agents WHERE name=?", (name,)).fetchone() is None:
            harness, workspace = split_name(name)
            conn.execute(
                "INSERT INTO agents(name, description, registered_at, harness, "
                "workspace, cwd) VALUES(?,?,?,?,?,?) ON CONFLICT(name) DO NOTHING",
                (name, "", _now(), harness, workspace, identity.get("cwd") or ""),
            )
            _log_event(conn, "register", agent=name, detail="auto (connected client)")
        elif identity.get("cwd"):
            conn.execute("UPDATE agents SET cwd=? WHERE name=?", (identity["cwd"], name))
    touch_presence(name, identity.get("model"))


def list_presence() -> str:
    """Return every known agent (registered or seen) with a computed status."""
    with _db() as conn:
        rows = conn.execute(
            """
            SELECT a.name, a.description, a.harness, a.workspace, a.cwd,
                   p.last_seen, p.ttl_seconds, p.capabilities, p.model
            FROM agents a LEFT JOIN presence p ON a.name = p.agent
            UNION
            SELECT p.agent AS name, '' AS description, '' AS harness,
                   '' AS workspace, '' AS cwd,
                   p.last_seen, p.ttl_seconds, p.capabilities, p.model
            FROM presence p
            WHERE p.agent NOT IN (SELECT name FROM agents)
            ORDER BY harness, workspace, name
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
        harness, workspace = row["harness"], row["workspace"]
        if not harness:
            harness, workspace = split_name(row["name"])
        result.append(
            {
                "agent": row["name"],
                "harness": harness,
                "workspace": workspace or None,
                "cwd": row["cwd"] or None,
                "model": row["model"] or None,
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
    with _db() as conn, _write(conn):
        author, error = _resolve_target(conn, author)
        if error:
            return error.replace("agent", "author", 1)
        seq = _insert_id(
            conn,
            "INSERT INTO channel(author, at, topic, message) VALUES(?,?,?,?)",
            (author, _now(), topic, message),
        )
        _log_event(conn, "announce", agent=author, detail=topic)
    return json.dumps({"seq": seq}, ensure_ascii=False)


def system_announce(topic: str, message: str) -> str:
    """A channel entry written by the mechanism itself — author `bus`, like
    the task-queued notifications — for events no agent authored (a machine
    paired or revoked on a hub). Same limits as announce."""
    topic = topic.strip().lower()
    if not _TOPIC_RE.match(topic):
        return "ERROR: topic must match ^[a-z0-9-]{1,64}$."
    if len(message.encode("utf-8")) > 16 * 1024:
        return "ERROR: message exceeds 16 KiB."
    with _db() as conn, _write(conn):
        seq = _insert_id(
            conn,
            "INSERT INTO channel(author, at, topic, message) VALUES(?,?,?,?)",
            ("bus", _now(), topic, message),
        )
        _log_event(conn, "announce", agent="bus", detail=topic)
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
    with _db() as conn:
        if agent is not None and since_seq is None:
            agent, error = _resolve_target(conn, agent)
            if error:
                return error
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
            with _write(conn):
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
    return json.dumps([dict(r) for r in rows], ensure_ascii=False)


def notify(
    author: str, target_agent: str, message: str,
    reply_to: int | None = None, wake: bool = False,
) -> str:
    """Send a directed signal to one agent — point-to-point, unlike the
    broadcast channel; the recipient picks it up with poll(). Author and
    target must be registered (typo detection, same as push_task); the
    message is a notice, capped like the channel at 16 KiB. `reply_to` ties
    the notice to a broadcast (its id) so the asker can collect it with
    replies(); `wake` asks a supervisor to launch an asleep recipient —
    granted only if the recipient's roster allows it (see wake policy)."""
    if not author.strip():
        return "ERROR: empty author."
    if not target_agent.strip():
        return "ERROR: empty target agent."
    if len(message.encode("utf-8")) > 16 * 1024:
        return "ERROR: message exceeds 16 KiB."
    with _db() as conn, _write(conn):
        author, error = _resolve_target(conn, author)
        if error:
            return error.replace("agent", "author", 1)
        target, error = _resolve_target(conn, target_agent)
        if error:
            return error
        if reply_to is not None:
            if not _is_broadcast(conn, reply_to):
                return f"ERROR: reply_to {reply_to} is not a broadcast id."
        seq = _insert_id(
            conn,
            "INSERT INTO notifications(target, author, at, message, reply_to, wake) "
            "VALUES(?,?,?,?,?,?)",
            (target, author, _now(), message, reply_to, 1 if wake else 0),
        )
        _log_event(conn, "notify", agent=author,
                   detail=target + (f" (reply to broadcast {reply_to})" if reply_to else "")
                   + (" (wake)" if wake else ""))
    return json.dumps({"seq": seq}, ensure_ascii=False)


def _is_broadcast(conn: Connection, broadcast_id: int) -> bool:
    return conn.execute(
        "SELECT 1 FROM channel WHERE seq=? AND topic='broadcast'", (broadcast_id,)
    ).fetchone() is not None


def _alive_set(conn: Connection) -> set[str]:
    now = datetime.fromisoformat(_now())
    alive = set()
    for r in conn.execute("SELECT agent, last_seen, ttl_seconds FROM presence"):
        deadline = datetime.fromisoformat(r["last_seen"]) + timedelta(seconds=r["ttl_seconds"] or 0)
        if now < deadline:
            alive.add(r["agent"])
    return alive


def _expand_scope(conn: Connection, scope: str | list) -> tuple[list[str], str | None]:
    """Recipients of a broadcast: `*` (every registered agent), `harness@*`
    (every instance of a harness, a legacy bare row included), `*@workspace`
    (every agent of a workspace), `model:<glob>` (instances whose declared
    model matches), or an explicit list — comma-separated or a JSON list —
    where every entry must resolve (one typo refuses the whole broadcast:
    a partial fan-out would be a silent loss)."""
    if isinstance(scope, str):
        text = scope.strip().lower()
        if not text:
            return [], "ERROR: empty scope."
        if text == "*":
            names = _known_agents(conn)
            return names, None if names else "ERROR: no registered agent."
        if text.endswith("@*") and text.count("@") == 1:
            harness = text[:-2]
            names = [r["name"] for r in conn.execute(
                "SELECT name FROM agents WHERE harness=? ORDER BY name", (harness,))]
            return names, None if names else f"ERROR: no instance of harness '{harness}'."
        if text.startswith("*@") and text.count("@") == 1:
            workspace = text[2:]
            names = [r["name"] for r in conn.execute(
                "SELECT name FROM agents WHERE workspace=? ORDER BY name", (workspace,))]
            return names, None if names else f"ERROR: no agent in workspace '{workspace}'."
        if text.startswith("model:"):
            glob = text[len("model:"):]
            names = [r["agent"] for r in conn.execute(
                "SELECT a.name AS agent, p.model FROM agents a "
                "JOIN presence p ON p.agent = a.name ORDER BY a.name")
                if r["model"] and fnmatch.fnmatchcase(r["model"].lower(), glob)]
            return names, None if names else f"ERROR: no instance with a model matching '{glob}'."
        if text.startswith("["):
            try:
                items = json.loads(scope)
            except ValueError:
                return [], "ERROR: scope list is not valid JSON."
        else:
            items = [part for part in text.split(",")]
    else:
        items = list(scope)
    names: list[str] = []
    for item in items:
        name, error = _resolve_target(conn, str(item))
        if error:
            return [], error
        if name not in names:
            names.append(name)
    return names, None if names else "ERROR: empty scope."


def broadcast_recipients(scope: str | list) -> tuple[list[str], str | None]:
    """Read-only: who a broadcast scope designates (see _expand_scope)."""
    with _db() as conn:
        return _expand_scope(conn, scope)


def broadcast(
    author: str, message: str, scope: str | list = "*",
    alive_only: bool = False, wake: bool = False,
) -> str:
    """Ask or tell several agents at once: one channel entry (topic
    `broadcast`, its seq is the broadcast id) plus one directed notification
    per recipient, written in the SAME transaction, so recipients read it
    with the poll() they already have and can answer with
    notify(reply_to=id). Returns who will see it now (`delivered`, alive)
    and who is asleep (`asleep`, pending until their next poll or a
    supervisor wake when `wake` is set — `to_wake`). A broadcast is a
    question or a signal, never a work order: work goes through push_task."""
    if not author.strip():
        return "ERROR: empty author."
    if len(message.encode("utf-8")) > 16 * 1024:
        return "ERROR: message exceeds 16 KiB."
    with _db() as conn:
        with _write(conn):
            author_name, error = _resolve_target(conn, author)
            if error:
                return error.replace("agent", "author", 1)
            names, error = _expand_scope(conn, scope)
            if error:
                return error
            alive = _alive_set(conn)
            if alive_only:
                names = [n for n in names if n in alive]
                if not names:
                    return "ERROR: no alive recipient in scope."
            scope_text = scope if isinstance(scope, str) else ",".join(scope)
            broadcast_id = _insert_id(
                conn,
                "INSERT INTO channel(author, at, topic, message) VALUES(?,?,?,?)",
                (author_name, _now(), "broadcast", message),
            )
            now = _now()
            for name in names:
                conn.execute(
                    "INSERT INTO notifications(target, author, at, message, broadcast_id, wake) "
                    "VALUES(?,?,?,?,?,?)",
                    (name, author_name, now, message, broadcast_id, 1 if wake else 0),
                )
            _log_event(conn, "broadcast", agent=author_name,
                       detail=f"id {broadcast_id}, scope {scope_text} → {len(names)} recipient(s)"
                       + (" (wake)" if wake else ""))
    delivered = [n for n in names if n in alive]
    asleep = [n for n in names if n not in alive]
    return json.dumps(
        {"broadcast_id": broadcast_id, "recipients": names, "delivered": delivered,
         "asleep": asleep, "to_wake": asleep if wake else []},
        ensure_ascii=False,
    )


def replies(broadcast_id: int) -> str:
    """Answers to a broadcast (notifications sent with reply_to=id), oldest
    first — a pure read, no cursor moves. Refused for an id that is not a
    broadcast."""
    with _db() as conn:
        if not _is_broadcast(conn, broadcast_id):
            return f"ERROR: {broadcast_id} is not a broadcast id."
        rows = conn.execute(
            "SELECT seq, author, target, at, message FROM notifications "
            "WHERE reply_to=? ORDER BY seq ASC",
            (broadcast_id,),
        ).fetchall()
    return json.dumps([dict(r) for r in rows], ensure_ascii=False)


def poll(agent: str, limit: int = 100) -> str:
    """Read an agent's own directed notifications and advance its cursor to
    the last returned seq (at-least-once); an empty read advances nothing."""
    if not agent.strip():
        return "ERROR: empty agent."
    if limit < 1:
        limit = 1
    elif limit > 1000:
        limit = 1000
    with _db() as conn:
        with _write(conn):
            target, error = _resolve_target(conn, agent)
            if error:
                return error
            cursor = conn.execute(
                "SELECT last_seq FROM notification_cursor WHERE target=?", (target,)
            ).fetchone()
            last_seq = cursor["last_seq"] if cursor else 0
            rows = conn.execute(
                "SELECT seq, author, at, message, broadcast_id, reply_to, wake "
                "FROM notifications WHERE target=? AND seq > ? ORDER BY seq ASC LIMIT ?",
                (target, last_seq, limit),
            ).fetchall()
            if rows:
                conn.execute(
                    "INSERT INTO notification_cursor(target, last_seq) VALUES(?,?) "
                    "ON CONFLICT(target) DO UPDATE SET last_seq=excluded.last_seq",
                    (target, rows[-1]["seq"]),
                )
    return json.dumps(
        [{**dict(r), "wake": bool(r["wake"])} for r in rows], ensure_ascii=False
    )


def _pending(conn: Connection, target: str) -> dict:
    """Signals waiting for an instance, without moving any cursor: queued
    task ids (highest priority first), unread notifications (total, those
    flagged `wake`, and the broadcast ids among the flagged ones)."""
    _requeue_expired(conn)
    tasks = [r["task_id"] for r in conn.execute(
        "SELECT task_id FROM tasks WHERE agent=? AND status='queued' "
        "ORDER BY priority DESC, created_at ASC", (target,))]
    cursor = conn.execute(
        "SELECT last_seq FROM notification_cursor WHERE target=?", (target,)).fetchone()
    last_seq = cursor["last_seq"] if cursor else 0
    rows = conn.execute(
        "SELECT seq, broadcast_id, wake FROM notifications WHERE target=? AND seq > ?",
        (target, last_seq)).fetchall()
    wake_direct = sum(1 for r in rows if r["wake"] and r["broadcast_id"] is None)
    wake_broadcasts = sorted({r["broadcast_id"] for r in rows if r["wake"] and r["broadcast_id"]})
    return {
        "agent": target,
        "tasks": tasks,
        "notifications": len(rows),
        "wake_notifications": wake_direct,
        "wake_broadcasts": wake_broadcasts,
        "broadcasts": sorted({r["broadcast_id"] for r in rows if r["broadcast_id"]}),
    }


def _kinds(kinds: str | tuple | list) -> set[str] | str:
    wanted = {k.strip().lower() for k in (kinds.split(",") if isinstance(kinds, str) else kinds)}
    wanted.discard("")
    if "any" in wanted or not wanted:
        return {"task", "notification"}
    if not wanted <= {"task", "notification"}:
        return f"ERROR: kinds must be task, notification or any (got {sorted(wanted)})."
    return wanted


def _signalled(pending: dict, wanted: set[str]) -> bool:
    return bool(("task" in wanted and pending["tasks"])
                or ("notification" in wanted and pending["notifications"]))


def pending_signals(agent: str) -> str:
    """Snapshot of what waits for an instance (see _pending) — read only."""
    with _db() as conn:
        target, error = _resolve_target(conn, agent)
        if error:
            return error
        return json.dumps(_pending(conn, target), ensure_ascii=False)


def wait_signal(
    agent: str, kinds: str | tuple | list = "any",
    timeout_s: float = 300.0, interval_s: float = 2.0,
) -> str:
    """Block until a task is queued for the instance and/or an unread
    notification (a broadcast included) waits in its mailbox — whichever
    `kinds` asks for — and return the snapshot with `signalled: true`;
    `signalled: false` on timeout. Generalises wait_for_task: this is the
    wake-up of an OPEN interactive session (run it as a background task,
    the harness hands control back when it returns) and the probe of a
    supervisor. No cursor moves: the session still polls/claims itself."""
    wanted = _kinds(kinds)
    if isinstance(wanted, str):
        return wanted
    if not agent.strip():
        return "ERROR: empty agent."
    interval_s = max(0.2, float(interval_s))
    deadline = time.monotonic() + max(0.0, float(timeout_s))
    conn = connect()
    try:
        target, error = _resolve_target(conn, agent)
        if error:
            return error
        while True:
            pending = _pending(conn, target)
            if _signalled(pending, wanted):
                return json.dumps({"signalled": True, **pending}, ensure_ascii=False)
            if time.monotonic() >= deadline:
                return json.dumps({"signalled": False, **pending}, ensure_ascii=False)
            conn.dialect.pause(conn, min(interval_s, max(0.0, deadline - time.monotonic())))
    finally:
        conn.close()


def acquire_wake_lock(agent: str, lease_seconds: int = 600, holder: str = "") -> str:
    """Take the wake lock of an instance for `lease_seconds` — the guard
    that keeps two supervisors (or a timer firing twice) from launching the
    same agent at once. Lives in the database like everything else: no
    flock, no state file, same lease trick as tasks (an expired lock is
    free). Returns {token, until} or BUSY."""
    target = agent.strip().lower()
    if not target:
        return "ERROR: empty agent."
    token = hashlib.sha256(f"{target}{time.time_ns()}{os.getpid()}".encode()).hexdigest()[:16]
    until = (datetime.now(timezone.utc) + timedelta(seconds=lease_seconds)).isoformat()
    with _db() as conn:
        with _write(conn):
            row = conn.execute(
                "SELECT token, until FROM wake_locks WHERE agent=?", (target,)).fetchone()
            if row is not None and row["until"] > _now():
                return "BUSY"
            conn.execute(
                "INSERT INTO wake_locks(agent, token, until, holder) VALUES(?,?,?,?) "
                "ON CONFLICT(agent) DO UPDATE SET token=excluded.token, "
                "until=excluded.until, holder=excluded.holder",
                (target, token, until, holder),
            )
    return json.dumps({"token": token, "until": until}, ensure_ascii=False)


def renew_wake_lock(agent: str, token: str, lease_seconds: int = 600) -> str:
    """Extend a wake lock still held (an expired lock may already belong to
    another supervisor: renewing it would let two of them launch)."""
    target = agent.strip().lower()
    until = (datetime.now(timezone.utc) + timedelta(seconds=lease_seconds)).isoformat()
    with _db() as conn, _write(conn):
        held = conn.execute(
            "UPDATE wake_locks SET until=? WHERE agent=? AND token=? AND until > ?",
            (until, target, token, _now())).rowcount
    return "OK" if held else "ERROR: wake lock not held with this token (or expired)."


def release_wake_lock(agent: str, token: str) -> str:
    target = agent.strip().lower()
    with _db() as conn, _write(conn):
        held = conn.execute(
            "DELETE FROM wake_locks WHERE agent=? AND token=?", (target, token)).rowcount
    return "OK" if held else "ERROR: wake lock not held with this token."


def record_wake(agent: str, detail: str) -> None:
    """Journal a wake-up transition (`wake` event: start/end/refused) — the
    budget `wake_count` is computed from these starts."""
    with _db() as conn:
        _log_event(conn, "wake", agent=agent.strip().lower(), detail=detail)


def wake_count(agent: str, window_seconds: int = 3600) -> int:
    """Launches of an instance within the window (events `wake` whose detail
    starts with `start`)."""
    since = (datetime.now(timezone.utc) - timedelta(seconds=window_seconds)).isoformat()
    with _db() as conn:
        row = conn.execute(
            "SELECT COUNT(*) c FROM events WHERE event='wake' AND agent=? AND at >= ? "
            "AND detail LIKE 'start%'", (agent.strip().lower(), since)).fetchone()
    return row["c"]


def get_system_state() -> str:
    with _db() as conn:
        _requeue_expired(conn)
        agents = {
            r["name"]: {
                "description": r["description"], "harness": r["harness"] or None,
                "workspace": r["workspace"] or None, "cwd": r["cwd"] or None,
            }
            for r in conn.execute(
                "SELECT name, description, harness, workspace, cwd FROM agents "
                "ORDER BY harness, workspace, name"
            )
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
            SELECT a.name, p.last_seen, p.ttl_seconds, p.model
            FROM agents a LEFT JOIN presence p ON a.name = p.agent
            UNION
            SELECT p.agent AS name, p.last_seen, p.ttl_seconds, p.model
            FROM presence p
            WHERE p.agent NOT IN (SELECT name FROM agents)
            """
        ).fetchall()
    now = datetime.fromisoformat(_now())
    presence: dict[str, str] = {}
    models: dict[str, str | None] = {}
    for r in pres_rows:
        models[r["name"]] = r["model"] or None
        if r["last_seen"] is None:
            presence[r["name"]] = "unknown"
        else:
            deadline = datetime.fromisoformat(r["last_seen"]) + timedelta(
                seconds=r["ttl_seconds"] or 0
            )
            presence[r["name"]] = "alive" if now < deadline else "asleep"
    return json.dumps(
        {"agents": agents, "queues": queues, "completed_tasks": done,
         "presence": presence, "models": models},
        indent=2,
        ensure_ascii=False,
    )


def get_events(task_id: str | None = None, agent: str | None = None, limit: int = 100) -> str:
    """Chronological transition history (register/push/claim/expire/publish),
    filterable by task and/or agent; the `limit` most recent, oldest first.
    Events written through a hub carry the calling `machine`."""
    query = "SELECT task_id, agent, event, at, detail, machine FROM events"
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
    with _db() as conn:
        rows = [dict(r) for r in conn.execute(query, params)]
    rows.reverse()
    for row in rows:
        if not row["machine"]:
            del row["machine"]  # local mode: the history reads as before
    return json.dumps(rows, indent=2, ensure_ascii=False)
