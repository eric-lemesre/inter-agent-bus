//! The connection of whichever engine the target selects — static
//! dispatch, one match per call.

use crate::{Connection, DbError, PostgresConnection, Row, SqliteConnection, Value};

/// A SQLite or PostgreSQL connection.
pub enum AnyConnection {
    /// Local file.
    Sqlite(SqliteConnection),
    /// Remote server.
    Postgres(Box<PostgresConnection>),
}

macro_rules! each {
    ($self:ident, $conn:ident => $body:expr) => {
        match $self {
            AnyConnection::Sqlite($conn) => $body,
            AnyConnection::Postgres($conn) => $body,
        }
    };
}

impl Connection for AnyConnection {
    fn query(&mut self, sql: &str, params: &[Value]) -> Result<Vec<Row>, DbError> {
        each!(self, c => c.query(sql, params))
    }

    fn execute(&mut self, sql: &str, params: &[Value]) -> Result<u64, DbError> {
        each!(self, c => c.execute(sql, params))
    }

    fn insert_id(&mut self, sql: &str, params: &[Value], column: &str) -> Result<i64, DbError> {
        each!(self, c => c.insert_id(sql, params, column))
    }

    fn columns(&mut self, table: &str) -> Result<Vec<String>, DbError> {
        each!(self, c => c.columns(table))
    }

    fn ddl(&self, statement: &str) -> String {
        each!(self, c => c.ddl(statement))
    }

    fn create_schema(&mut self) -> Result<(), DbError> {
        each!(self, c => c.create_schema())
    }

    fn schema_needs_lock(&self) -> bool {
        each!(self, c => c.schema_needs_lock())
    }

    fn begin_write(&mut self) -> Result<(), DbError> {
        each!(self, c => c.begin_write())
    }

    fn commit(&mut self) -> Result<(), DbError> {
        each!(self, c => c.commit())
    }

    fn rollback(&mut self) {
        each!(self, c => c.rollback())
    }

    fn pause(&mut self, seconds: f64) -> Result<(), DbError> {
        each!(self, c => c.pause(seconds))
    }
}
