"""SQL dialect layer of the inter-agent bus — the few engine-specific
constructs of store.py, behind one small interface.

store.py writes portable SQL (`?` placeholders, `INSERT … ON CONFLICT`,
ISO text dates, no engine function) and asks the dialect for the rest:
opening a connection, the schema, the write transaction (the single
writer lock that claim, fencing and wake locks rely on), auto-generated
ids, schema introspection for the additive migrations, and the
integrity-error class. No ORM: the rule "no new dependency lightly"
holds, and the core stays standard-library only for SQLite.

The engine is chosen by the database target (see store.db_path): a
filesystem path selects SQLite — the default; a `postgresql://` URL
selects PostgreSQL (extra `[postgres]`, the psycopg driver), meant for a
hub: one writer lock (an advisory lock) keeps the core's transactional
guarantees unchanged, LISTEN/NOTIFY wakes the blocking waits.
"""
from __future__ import annotations

import os
import re
import sqlite3
import threading
import time
import urllib.parse
from contextlib import contextmanager
from functools import lru_cache
from pathlib import Path
from typing import Any, Iterator

# A DB-API connection of the active engine; rows behave like mappings
# (`row["name"]`, `dict(row)`) whatever the engine.
Connection = Any

# Text columns hold ISO-8601 timestamps compared as strings: the schema
# keeps them plain TEXT so that a byte-wise comparison orders them.
SCHEMA = """
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
    id      {serial},
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
    capabilities TEXT NOT NULL DEFAULT '{{}}'
);
CREATE TABLE IF NOT EXISTS channel (
    seq     {serial},
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
    seq     {serial},
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
CREATE TABLE IF NOT EXISTS wake_locks (
    agent  TEXT PRIMARY KEY,
    token  TEXT NOT NULL,
    until  TEXT NOT NULL,
    holder TEXT NOT NULL DEFAULT ''
);
"""


class _SQLiteConnection(sqlite3.Connection):
    """A sqlite3 connection that knows its dialect (`conn.dialect`)."""

    dialect: "SQLiteDialect"


class SQLiteDialect:
    """The default engine: one file, WAL mode, `BEGIN IMMEDIATE` as the
    single-writer lock. Several processes share the file directly — the
    local mode of the bus (and the storage of a hub)."""

    name = "sqlite"
    integrity_error: type[Exception] = sqlite3.IntegrityError

    def __init__(self, path: Path) -> None:
        self.path = Path(path)

    def connect(self) -> Connection:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        conn = sqlite3.connect(self.path, timeout=10, isolation_level=None,
                               factory=_SQLiteConnection)
        conn.dialect = self
        if os.name == "posix":
            # Writing the bus is prompt-injection-adjacent (payloads reach
            # autonomous agents): keep the file owner-only. Hardening, not
            # correctness — an exotic filesystem may refuse, the bus still
            # works.
            try:
                os.chmod(self.path, 0o600)
            except OSError:
                pass
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA busy_timeout=10000")
        return conn

    def create_schema(self, conn: Connection) -> None:
        conn.executescript(SCHEMA.format(serial="INTEGER PRIMARY KEY AUTOINCREMENT"))

    def prepare(self, conn: Connection, setup) -> None:
        """Run a module's `setup(conn)` (its tables, its additive
        migrations) at every connection: cheap on a local file, and a file
        replaced under a running process is still prepared."""
        setup(conn)

    def ddl(self, statement: str) -> str:
        return statement

    def pause(self, conn: Connection, seconds: float) -> None:
        """Wait between two polls of a blocking wait."""
        time.sleep(seconds)

    def columns(self, conn: Connection, table: str) -> set[str]:
        return {r["name"] for r in conn.execute(f"PRAGMA table_info({table})")}

    def insert_id(self, conn: Connection, sql: str, params: tuple, column: str = "seq") -> int:
        """Run an INSERT into a table with an auto-generated id and return
        that id."""
        return conn.execute(sql, params).lastrowid

    @contextmanager
    def write(self, conn: Connection) -> Iterator[Connection]:
        """One write transaction holding the writer lock from its first
        statement: reads inside it see a state no other writer can change
        before COMMIT (claim, fencing, wake locks rely on it). Leaving the
        block normally commits — an early `return` included — an exception
        rolls back and propagates."""
        conn.execute("BEGIN IMMEDIATE")
        try:
            yield conn
        except BaseException:
            _rollback(conn)
            raise
        conn.execute("COMMIT")


def _rollback(conn: Connection) -> None:
    """Roll back without masking the error that caused it: on a dead
    connection the ROLLBACK itself fails, and the original exception is
    the one worth propagating (the connection is discarded anyway)."""
    try:
        conn.execute("ROLLBACK")
    except Exception:
        pass


# -- PostgreSQL -------------------------------------------------------------

_SCHEMES = ("postgresql://", "postgres://")
# One writer at a time, like SQLite's BEGIN IMMEDIATE: a transaction-level
# advisory lock, released at COMMIT/ROLLBACK. Key: "iab" in ASCII.
_WRITER_LOCK = 0x696162
_CHANNEL = "iab_bus"
_POOL_SIZE = 8


def is_url(target) -> bool:
    return isinstance(target, str) and target.startswith(_SCHEMES)


def describe(target) -> str:
    """A target fit for display: a URL is rebuilt from its scheme, user,
    host, port and database only — no password, no query string (libpq
    accepts `?password=`, `?sslpassword=`…)."""
    if not is_url(target):
        return str(target)
    parts = urllib.parse.urlsplit(target)
    user = f"{parts.username}@" if parts.username else ""
    host = parts.hostname or ""
    port = f":{parts.port}" if parts.port else ""
    return f"{parts.scheme}://{user}{host}{port}{parts.path}"


@lru_cache(maxsize=512)
def _translate(sql: str) -> str:
    """The core's portable SQL to psycopg's: `?` → `%s` outside string
    literals, every literal `%` doubled (psycopg reads `%` everywhere when
    parameters are passed — and they always are)."""
    out, quoted = [], False
    for char in sql:
        if char == "'":
            quoted = not quoted
            out.append(char)
        elif char == "%":
            out.append("%%")
        elif char == "?" and not quoted:
            out.append("%s")
        else:
            out.append(char)
    return "".join(out)


class _PGConnection:
    """A psycopg connection behind the sqlite3-like surface the core uses:
    `execute(sql, params)` → cursor (fetchone, fetchall, iteration,
    rowcount), rows as mappings, `close()` back to the pool."""

    def __init__(self, raw, dialect: "PostgresDialect") -> None:
        self.raw = raw
        self.dialect = dialect
        self.listening = False
        self.dirty = False  # a row changed in the current write transaction

    def execute(self, sql: str, params=()):
        cursor = self.raw.execute(_translate(sql), tuple(params))
        if cursor.rowcount > 0 and sql.lstrip()[:6].upper() in ("INSERT", "UPDATE", "DELETE"):
            self.dirty = True
        return cursor

    def close(self) -> None:
        self.dialect._release(self)


class PostgresDialect:
    """PostgreSQL for a hub: the same SQL as SQLite, one advisory writer
    lock, a small connection pool, LISTEN/NOTIFY for the waits."""

    name = "postgresql"

    def __init__(self, url: str) -> None:
        try:
            import psycopg
            from psycopg.rows import dict_row
        except ImportError as exc:
            raise ImportError("a postgresql:// bus needs the psycopg driver: "
                              "pip install 'inter-agent-bus[postgres]'") from exc
        self._psycopg = psycopg
        self._dict_row = dict_row
        self.url = url
        self.integrity_error = psycopg.IntegrityError

    def connect(self) -> Connection:
        idle = _POOLS.setdefault(self.url, [])
        with _POOL_LOCK:
            while idle:
                conn = idle.pop()
                if not conn.raw.closed:
                    return conn
        raw = self._psycopg.connect(self.url, autocommit=True, row_factory=self._dict_row)
        return _PGConnection(raw, self)

    def _release(self, conn: _PGConnection) -> None:
        # A connection that LISTENed is never pooled: idle, it would keep
        # queueing every NOTIFY of the bus (psycopg's backlog is unbounded)
        # and wake its next waiter in a burst of stale signals.
        healthy = (not conn.raw.closed and not conn.listening
                   and conn.raw.info.transaction_status
                   == self._psycopg.pq.TransactionStatus.IDLE)
        with _POOL_LOCK:
            idle = _POOLS.setdefault(self.url, [])
            if healthy and len(idle) < _POOL_SIZE:
                idle.append(conn)
                return
        conn.raw.close()

    def ddl(self, statement: str) -> str:
        # Text columns compared as ISO dates must compare byte-wise, whatever
        # the database's locale: the "C" collation. Foreign keys are checked
        # at COMMIT: a rename moves a name and its references in one
        # transaction (SQLite does not enforce them at all).
        statement = re.sub(r"\bTEXT\b", 'TEXT COLLATE "C"', statement)
        return re.sub(r"(REFERENCES \w+\(\w+\))", r"\1 DEFERRABLE INITIALLY DEFERRED",
                      statement)

    def create_schema(self, conn: Connection) -> None:
        for statement in SCHEMA.format(serial="BIGSERIAL PRIMARY KEY").split(";"):
            if statement.strip():
                conn.execute(self.ddl(statement))

    def prepare(self, conn: Connection, setup) -> None:
        """Run a module's `setup(conn)` at most once per process and
        database, inside the writer lock (several hub threads, or hubs, may
        start at once; DDL is transactional here)."""
        key = (self.url, setup.__module__, setup.__qualname__)
        if key in _PREPARED:
            return
        with self.write(conn):
            setup(conn)
        _PREPARED.add(key)

    def columns(self, conn: Connection, table: str) -> set[str]:
        return {r["name"] for r in conn.execute(
            "SELECT column_name AS name FROM information_schema.columns "
            "WHERE table_schema = current_schema() AND table_name = ?", (table,))}

    def insert_id(self, conn: Connection, sql: str, params: tuple, column: str = "seq") -> int:
        return conn.execute(f"{sql} RETURNING {column}", params).fetchone()[column]

    @contextmanager
    def write(self, conn: Connection) -> Iterator[Connection]:
        """Same contract as SQLite's: the writer lock from the first
        statement, commit on normal exit, rollback on exception. Waiters
        are notified at commit (a NOTIFY is delivered only then) — and only
        when a row changed: a claim that found nothing, an empty poll, a
        refusal wake nobody."""
        conn.execute("BEGIN")
        conn.dirty = False
        try:
            conn.execute("SELECT pg_advisory_xact_lock(?)", (_WRITER_LOCK,))
            yield conn
            if conn.dirty:
                conn.execute(f"NOTIFY {_CHANNEL}")
        except BaseException:
            _rollback(conn)
            raise
        conn.execute("COMMIT")

    def pause(self, conn: Connection, seconds: float) -> None:
        """Sleep until a write commits somewhere or `seconds` elapse — a
        wait wakes at once instead of at its next poll."""
        if not conn.listening:
            conn.execute(f"LISTEN {_CHANNEL}")
            conn.listening = True
        for _ in conn.raw.notifies(timeout=seconds, stop_after=1):
            pass


_POOLS: dict[str, list] = {}
_POOL_LOCK = threading.Lock()
_PREPARED: set[tuple[str, str, str]] = set()


def for_target(target) -> SQLiteDialect | PostgresDialect:
    """The dialect serving a database target (see store.db_path)."""
    if is_url(target):
        return PostgresDialect(target)
    return SQLiteDialect(target)
