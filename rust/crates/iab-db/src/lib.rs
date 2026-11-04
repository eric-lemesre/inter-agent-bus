//! Database engines of the inter-agent bus — the counterpart of
//! `dialect.py`.
//!
//! The core writes portable SQL (`?` placeholders, `INSERT … ON CONFLICT`,
//! ISO text dates, no engine function) and asks the engine for the rest:
//! the schema, the write transaction holding the single writer lock,
//! auto-generated ids, schema introspection for the additive migrations
//! and the pause of a blocking wait. The schema and the lock are fixed by
//! `CONTRAT.md`: an implementation in another language writes the same
//! database.

mod sqlite;

pub use sqlite::SqliteConnection;

/// Errors of the database layer.
#[derive(Debug, thiserror::Error)]
pub enum DbError {
    /// Error reported by SQLite.
    #[error(transparent)]
    Sqlite(#[from] rusqlite::Error),
    /// The directory holding the database file cannot be created.
    #[error("cannot create {path}: {source}")]
    Directory {
        /// Directory that could not be created.
        path: String,
        /// Underlying I/O error.
        source: std::io::Error,
    },
    /// A query result lacks a column the caller selected.
    #[error("no column '{0}' in the row")]
    MissingColumn(String),
    /// A column holds a value of an unexpected type.
    #[error("column '{column}' is not {expected}")]
    Type {
        /// Column name.
        column: String,
        /// Expected type.
        expected: &'static str,
    },
}

/// A value bound to a statement or read from a row. The bus stores only
/// integers and text (dates are ISO 8601 text, see `CONTRAT.md`).
#[derive(Debug, Clone, PartialEq, Eq)]
pub enum Value {
    /// SQL NULL.
    Null,
    /// INTEGER column.
    Int(i64),
    /// TEXT column.
    Text(String),
}

impl From<&str> for Value {
    fn from(value: &str) -> Self {
        Self::Text(value.to_owned())
    }
}

impl From<String> for Value {
    fn from(value: String) -> Self {
        Self::Text(value)
    }
}

impl From<&String> for Value {
    fn from(value: &String) -> Self {
        Self::Text(value.clone())
    }
}

impl From<i64> for Value {
    fn from(value: i64) -> Self {
        Self::Int(value)
    }
}

impl From<bool> for Value {
    fn from(value: bool) -> Self {
        Self::Int(i64::from(value))
    }
}

impl<T: Into<Value>> From<Option<T>> for Value {
    fn from(value: Option<T>) -> Self {
        value.map_or(Self::Null, Into::into)
    }
}

/// One result row: column names in SELECT order, values alongside.
#[derive(Debug, Clone)]
pub struct Row {
    columns: Vec<String>,
    values: Vec<Value>,
}

impl Row {
    /// Build a row from its columns and values (same length).
    pub fn new(columns: Vec<String>, values: Vec<Value>) -> Self {
        Self { columns, values }
    }

    /// Columns and values in SELECT order.
    pub fn iter(&self) -> impl Iterator<Item = (&str, &Value)> {
        self.columns.iter().map(String::as_str).zip(self.values.iter())
    }

    /// The value of a column.
    pub fn get(&self, column: &str) -> Result<&Value, DbError> {
        self.columns
            .iter()
            .position(|c| c == column)
            .and_then(|i| self.values.get(i))
            .ok_or_else(|| DbError::MissingColumn(column.to_owned()))
    }

    /// A text column, NULL as `None`.
    pub fn opt_text(&self, column: &str) -> Result<Option<&str>, DbError> {
        match self.get(column)? {
            Value::Null => Ok(None),
            Value::Text(text) => Ok(Some(text)),
            Value::Int(_) => Err(type_error(column, "text")),
        }
    }

    /// A text column that may not be NULL.
    pub fn text(&self, column: &str) -> Result<&str, DbError> {
        self.opt_text(column)?.ok_or_else(|| type_error(column, "text"))
    }

    /// An integer column, NULL as `None`.
    pub fn opt_int(&self, column: &str) -> Result<Option<i64>, DbError> {
        match self.get(column)? {
            Value::Null => Ok(None),
            Value::Int(n) => Ok(Some(*n)),
            Value::Text(_) => Err(type_error(column, "an integer")),
        }
    }

    /// An integer column that may not be NULL.
    pub fn int(&self, column: &str) -> Result<i64, DbError> {
        self.opt_int(column)?
            .ok_or_else(|| type_error(column, "an integer"))
    }
}

fn type_error(column: &str, expected: &'static str) -> DbError {
    DbError::Type {
        column: column.to_owned(),
        expected,
    }
}

/// Tables of the bus, before the additive migrations of the core. Text
/// columns hold ISO 8601 timestamps compared as strings. `{serial}` is the
/// engine's auto-generated id. Identical to `SCHEMA` in `dialect.py`.
pub const SCHEMA: &str = "
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
    capabilities TEXT NOT NULL DEFAULT '{}'
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
";

/// A connection to the bus database, whatever the engine.
pub trait Connection {
    /// Run a SELECT and collect its rows.
    fn query(&mut self, sql: &str, params: &[Value]) -> Result<Vec<Row>, DbError>;

    /// Run a statement and return the number of rows it changed.
    fn execute(&mut self, sql: &str, params: &[Value]) -> Result<u64, DbError>;

    /// Run an INSERT into a table with an auto-generated id and return that
    /// id (`column` names it).
    fn insert_id(&mut self, sql: &str, params: &[Value], column: &str) -> Result<i64, DbError>;

    /// Column names of a table of the current schema.
    fn columns(&mut self, table: &str) -> Result<Vec<String>, DbError>;

    /// Adapt a DDL statement to the engine.
    fn ddl(&self, statement: &str) -> String;

    /// Create the tables of [`SCHEMA`] that do not exist yet.
    fn create_schema(&mut self) -> Result<(), DbError>;

    /// Open a write transaction whose first statement takes the writer
    /// lock.
    fn begin_write(&mut self) -> Result<(), DbError>;

    /// Commit the write transaction.
    fn commit(&mut self) -> Result<(), DbError>;

    /// Roll the write transaction back, ignoring a failure: on a dead
    /// connection the ROLLBACK itself fails, and the error that caused it
    /// is the one worth reporting.
    fn rollback(&mut self);

    /// Wait between two polls of a blocking wait.
    fn pause(&mut self, seconds: f64) -> Result<(), DbError>;
}

/// Run `body` in one write transaction holding the writer lock from its
/// first statement: commit when it returns `Ok` (a refusal included),
/// roll back and propagate when it returns `Err` or when COMMIT fails.
pub fn write<C, T, E>(conn: &mut C, body: impl FnOnce(&mut C) -> Result<T, E>) -> Result<T, E>
where
    C: Connection + ?Sized,
    E: From<DbError>,
{
    conn.begin_write()?;
    match body(conn) {
        Ok(value) => match conn.commit() {
            Ok(()) => Ok(value),
            Err(error) => {
                conn.rollback();
                Err(error.into())
            }
        },
        Err(error) => {
            conn.rollback();
            Err(error)
        }
    }
}
