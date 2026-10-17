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
filesystem path selects SQLite — the default.
"""
from __future__ import annotations

import os
import sqlite3
from contextlib import contextmanager
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

    def columns(self, conn: Connection, table: str) -> set[str]:
        return {r["name"] for r in conn.execute(f"PRAGMA table_info({table})")}

    def insert_id(self, conn: Connection, sql: str, params: tuple) -> int:
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
            conn.execute("ROLLBACK")
            raise
        conn.execute("COMMIT")


def for_target(target: Path) -> SQLiteDialect:
    """The dialect serving a database target (see store.db_path)."""
    return SQLiteDialect(target)
