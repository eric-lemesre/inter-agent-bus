//! SQLite engine: one file, WAL mode, `BEGIN IMMEDIATE` as the single
//! writer lock. Several processes — Python and native alike — share the
//! file directly.

use std::path::Path;
use std::time::Duration;

use rusqlite::OpenFlags;
use rusqlite::types::ValueRef;

use crate::{Connection, DbError, Row, SCHEMA, Value};

/// A connection to a bus database file.
pub struct SqliteConnection {
    conn: rusqlite::Connection,
}

impl SqliteConnection {
    /// Open (creating it if needed) the database file, in WAL mode with a
    /// 10 s busy timeout, as `dialect.py` does.
    pub fn open(path: &Path) -> Result<Self, DbError> {
        if let Some(parent) = path.parent().filter(|p| !p.as_os_str().is_empty()) {
            std::fs::create_dir_all(parent).map_err(|source| DbError::Directory {
                path: parent.display().to_string(),
                source,
            })?;
        }
        // No URI flag: a path is a path, as with Python's sqlite3.connect.
        let flags = OpenFlags::SQLITE_OPEN_READ_WRITE
            | OpenFlags::SQLITE_OPEN_CREATE
            | OpenFlags::SQLITE_OPEN_NO_MUTEX;
        let conn = rusqlite::Connection::open_with_flags(path, flags)?;
        restrict_to_owner(path);
        conn.busy_timeout(Duration::from_secs(10))?;
        conn.query_row("PRAGMA journal_mode=WAL", [], |_| Ok(()))?;
        Ok(Self { conn })
    }
}

/// Writing the bus is prompt-injection-adjacent (payloads reach autonomous
/// agents): keep the file owner-only. Hardening, not correctness — an
/// exotic filesystem may refuse, the bus still works.
#[cfg(unix)]
fn restrict_to_owner(path: &Path) {
    use std::os::unix::fs::PermissionsExt;
    let _ = std::fs::set_permissions(path, std::fs::Permissions::from_mode(0o600));
}

/// On Windows the file inherits the ACLs of the user profile.
#[cfg(not(unix))]
fn restrict_to_owner(_path: &Path) {}

fn to_sql(value: &Value) -> rusqlite::types::Value {
    match value {
        Value::Null => rusqlite::types::Value::Null,
        Value::Int(n) => rusqlite::types::Value::Integer(*n),
        Value::Text(text) => rusqlite::types::Value::Text(text.clone()),
    }
}

fn from_sql(column: &str, value: ValueRef<'_>) -> Result<Value, DbError> {
    match value {
        ValueRef::Null => Ok(Value::Null),
        ValueRef::Integer(n) => Ok(Value::Int(n)),
        ValueRef::Text(bytes) => std::str::from_utf8(bytes)
            .map(|text| Value::Text(text.to_owned()))
            .map_err(|_| DbError::Type {
                column: column.to_owned(),
                expected: "UTF-8 text",
            }),
        ValueRef::Real(_) | ValueRef::Blob(_) => Err(DbError::Type {
            column: column.to_owned(),
            expected: "an integer or text",
        }),
    }
}

impl Connection for SqliteConnection {
    fn query(&mut self, sql: &str, params: &[Value]) -> Result<Vec<Row>, DbError> {
        let mut statement = self.conn.prepare_cached(sql)?;
        let columns: Vec<String> = statement.column_names().into_iter().map(str::to_owned).collect();
        let bound: Vec<rusqlite::types::Value> = params.iter().map(to_sql).collect();
        let mut rows = statement.query(rusqlite::params_from_iter(bound))?;
        let mut out = Vec::new();
        while let Some(row) = rows.next()? {
            let values = columns
                .iter()
                .enumerate()
                .map(|(i, column)| from_sql(column, row.get_ref(i)?))
                .collect::<Result<Vec<_>, DbError>>()?;
            out.push(Row::new(columns.clone(), values));
        }
        Ok(out)
    }

    fn execute(&mut self, sql: &str, params: &[Value]) -> Result<u64, DbError> {
        let mut statement = self.conn.prepare_cached(sql)?;
        let bound: Vec<rusqlite::types::Value> = params.iter().map(to_sql).collect();
        let changed = statement.execute(rusqlite::params_from_iter(bound))?;
        Ok(u64::try_from(changed).unwrap_or(u64::MAX))
    }

    fn insert_id(&mut self, sql: &str, params: &[Value], _column: &str) -> Result<i64, DbError> {
        self.execute(sql, params)?;
        Ok(self.conn.last_insert_rowid())
    }

    fn columns(&mut self, table: &str) -> Result<Vec<String>, DbError> {
        // Table names come from constants of the core, never from input.
        let rows = self.query(&format!("PRAGMA table_info({table})"), &[])?;
        rows.iter().map(|r| r.text("name").map(str::to_owned)).collect()
    }

    fn ddl(&self, statement: &str) -> String {
        statement.to_owned()
    }

    fn create_schema(&mut self) -> Result<(), DbError> {
        let schema = SCHEMA.replace("{serial}", "INTEGER PRIMARY KEY AUTOINCREMENT");
        self.conn.execute_batch(&schema)?;
        Ok(())
    }

    fn begin_write(&mut self) -> Result<(), DbError> {
        self.conn.execute_batch("BEGIN IMMEDIATE")?;
        Ok(())
    }

    fn commit(&mut self) -> Result<(), DbError> {
        self.conn.execute_batch("COMMIT")?;
        Ok(())
    }

    fn rollback(&mut self) {
        let _ = self.conn.execute_batch("ROLLBACK");
    }

    fn pause(&mut self, seconds: f64) -> Result<(), DbError> {
        if let Ok(delay) = Duration::try_from_secs_f64(seconds) {
            std::thread::sleep(delay);
        }
        Ok(())
    }
}
