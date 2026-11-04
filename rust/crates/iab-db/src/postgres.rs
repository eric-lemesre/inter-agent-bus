//! PostgreSQL engine: the same SQL as SQLite, one advisory writer lock,
//! LISTEN/NOTIFY for the waits (`CONTRAT.md`, §§ 2–4), TLS by rustls.
//!
//! The URL is libpq's. `tokio-postgres` understands most of it but not the
//! certificate parameters, so they are read here and turned into a rustls
//! configuration (`tls.rs`); the password may also come from `PGPASSWORD`
//! or the pgpass file, as with libpq.

use std::collections::HashMap;
use std::error::Error as StdError;
use std::time::Duration;

use bytes::BytesMut;
use postgres::fallible_iterator::FallibleIterator;
use postgres::types::{FromSql, IsNull, ToSql, Type, to_sql_checked};
use postgres::{Client, Statement};

use crate::tls::{self, TlsSettings};
use crate::{Connection, DbError, Row, SCHEMA, Value};

/// One writer at a time, like SQLite's `BEGIN IMMEDIATE`: a
/// transaction-level advisory lock released at COMMIT/ROLLBACK. Key: "iab"
/// in ASCII (0x696162).
const WRITER_LOCK: i64 = 0x0069_6162;
const CHANNEL: &str = "iab_bus";

/// A connection to a PostgreSQL bus.
pub struct PostgresConnection {
    client: Client,
    statements: HashMap<String, Statement>,
    listening: bool,
    /// A row changed in the current write transaction.
    dirty: bool,
}

impl PostgresConnection {
    /// Connect to a `postgresql://` URL (libpq format).
    pub fn open(url: &str) -> Result<Self, DbError> {
        let (config, settings) = parse(url)?;
        let client = match settings {
            None => config.connect(postgres::NoTls)?,
            Some(settings) => config.connect(tls::connector(&settings)?)?,
        };
        Ok(Self {
            client,
            statements: HashMap::new(),
            listening: false,
            dirty: false,
        })
    }

    fn statement(&mut self, sql: &str) -> Result<Statement, DbError> {
        if let Some(statement) = self.statements.get(sql) {
            return Ok(statement.clone());
        }
        let statement = self.client.prepare(&translate(sql))?;
        self.statements.insert(sql.to_owned(), statement.clone());
        Ok(statement)
    }
}

/// The core's portable SQL to PostgreSQL's: `?` → `$1…$n` outside string
/// literals.
pub fn translate(sql: &str) -> String {
    let mut out = String::with_capacity(sql.len() + 8);
    let mut quoted = false;
    let mut n = 0;
    for c in sql.chars() {
        match c {
            '\'' => {
                quoted = !quoted;
                out.push(c);
            }
            '?' if !quoted => {
                n += 1;
                out.push('$');
                out.push_str(&n.to_string());
            }
            c => out.push(c),
        }
    }
    out
}

fn is_word(c: char) -> bool {
    c.is_ascii_alphanumeric() || c == '_'
}

/// `\bTEXT\b` → `TEXT COLLATE "C"`: dates stored as text compare
/// byte-wise whatever the database's locale.
fn collate_text(statement: &str) -> String {
    let chars: Vec<char> = statement.chars().collect();
    let mut out = String::with_capacity(statement.len() + 32);
    let mut i = 0;
    while i < chars.len() {
        let at_word = chars[i..].starts_with(&['T', 'E', 'X', 'T'])
            && (i == 0 || !is_word(chars[i - 1]))
            && chars.get(i + 4).is_none_or(|&c| !is_word(c));
        if at_word {
            out.push_str("TEXT COLLATE \"C\"");
            i += 4;
        } else {
            out.push(chars[i]);
            i += 1;
        }
    }
    out
}

/// `(REFERENCES \w+\(\w+\))` → `… DEFERRABLE INITIALLY DEFERRED`: a rename
/// moves a name and its references in one transaction.
fn defer_references(statement: &str) -> String {
    let mut out = String::new();
    let mut rest = statement;
    while let Some(start) = rest.find("REFERENCES ") {
        out.push_str(&rest[..start]);
        let tail = &rest[start..];
        let end = tail.find(')').map(|i| i + 1);
        let clause = end.map(|e| &tail[..e]);
        let well_formed = clause.is_some_and(|c| {
            let body = &c["REFERENCES ".len()..];
            body.split_once('(').is_some_and(|(table, column)| {
                !table.is_empty()
                    && table.chars().all(is_word)
                    && column
                        .strip_suffix(')')
                        .is_some_and(|c| !c.is_empty() && c.chars().all(is_word))
            })
        });
        match (clause, end) {
            (Some(clause), Some(end)) if well_formed => {
                out.push_str(clause);
                out.push_str(" DEFERRABLE INITIALLY DEFERRED");
                rest = &tail[end..];
            }
            _ => {
                out.push_str("REFERENCES ");
                rest = &tail["REFERENCES ".len()..];
            }
        }
    }
    out.push_str(rest);
    out
}

/// Parameter value adapted to the type PostgreSQL inferred for it.
impl ToSql for Value {
    fn to_sql(&self, ty: &Type, out: &mut BytesMut) -> Result<IsNull, Box<dyn StdError + Sync + Send>> {
        match self {
            Value::Null => Ok(IsNull::Yes),
            Value::Text(text) => text.as_str().to_sql(ty, out),
            Value::Int(n) => match *ty {
                Type::INT8 => n.to_sql(ty, out),
                Type::INT4 => i32::try_from(*n)?.to_sql(ty, out),
                Type::INT2 => i16::try_from(*n)?.to_sql(ty, out),
                Type::BOOL => (*n != 0).to_sql(ty, out),
                _ => Err(format!("an integer cannot be sent as {ty}").into()),
            },
        }
    }

    fn accepts(_ty: &Type) -> bool {
        true
    }

    to_sql_checked!();
}

/// A column value read whatever its type: integers, booleans and text.
struct Raw(Value);

impl<'a> FromSql<'a> for Raw {
    fn from_sql(ty: &Type, raw: &'a [u8]) -> Result<Self, Box<dyn StdError + Sync + Send>> {
        let value = match *ty {
            Type::INT2 => Value::Int(i64::from(i16::from_sql(ty, raw)?)),
            Type::INT4 => Value::Int(i64::from(i32::from_sql(ty, raw)?)),
            Type::INT8 => Value::Int(i64::from_sql(ty, raw)?),
            Type::BOOL => Value::Int(i64::from(bool::from_sql(ty, raw)?)),
            Type::TEXT | Type::VARCHAR | Type::BPCHAR | Type::NAME | Type::UNKNOWN => {
                Value::Text(String::from_sql(ty, raw)?)
            }
            _ => return Err(format!("unsupported column type {ty}").into()),
        };
        Ok(Self(value))
    }

    fn from_sql_null(_ty: &Type) -> Result<Self, Box<dyn StdError + Sync + Send>> {
        Ok(Self(Value::Null))
    }

    fn accepts(_ty: &Type) -> bool {
        true
    }
}

fn changes_rows(sql: &str) -> bool {
    let head: String = sql
        .trim_start()
        .chars()
        .take(6)
        .collect::<String>()
        .to_ascii_uppercase();
    matches!(head.as_str(), "INSERT" | "UPDATE" | "DELETE")
}

fn bind(params: &[Value]) -> Vec<&(dyn ToSql + Sync)> {
    params.iter().map(|p| p as &(dyn ToSql + Sync)).collect()
}

impl Connection for PostgresConnection {
    fn query(&mut self, sql: &str, params: &[Value]) -> Result<Vec<Row>, DbError> {
        let statement = self.statement(sql)?;
        let rows = self.client.query(&statement, &bind(params))?;
        if !rows.is_empty() && changes_rows(sql) {
            self.dirty = true;
        }
        let columns: Vec<String> = statement.columns().iter().map(|c| c.name().to_owned()).collect();
        rows.iter()
            .map(|row| {
                let values = (0..columns.len())
                    .map(|i| row.try_get::<_, Raw>(i).map(|raw| raw.0))
                    .collect::<Result<Vec<_>, _>>()?;
                Ok(Row::new(columns.clone(), values))
            })
            .collect()
    }

    fn execute(&mut self, sql: &str, params: &[Value]) -> Result<u64, DbError> {
        let statement = self.statement(sql)?;
        let changed = self.client.execute(&statement, &bind(params))?;
        if changed > 0 && changes_rows(sql) {
            self.dirty = true;
        }
        Ok(changed)
    }

    fn insert_id(&mut self, sql: &str, params: &[Value], column: &str) -> Result<i64, DbError> {
        // `column` is a constant of the core.
        let rows = self.query(&format!("{sql} RETURNING {column}"), params)?;
        rows.first()
            .ok_or_else(|| DbError::MissingColumn(column.to_owned()))?
            .int(column)
    }

    fn columns(&mut self, table: &str) -> Result<Vec<String>, DbError> {
        let rows = self.query(
            "SELECT column_name::text AS name FROM information_schema.columns \
             WHERE table_schema = current_schema() AND table_name = ?",
            &[table.into()],
        )?;
        rows.iter().map(|r| r.text("name").map(str::to_owned)).collect()
    }

    fn ddl(&self, statement: &str) -> String {
        defer_references(&collate_text(statement))
    }

    fn create_schema(&mut self) -> Result<(), DbError> {
        let schema = SCHEMA.replace("{serial}", "BIGSERIAL PRIMARY KEY");
        for statement in schema.split(';').filter(|s| !s.trim().is_empty()) {
            let statement = self.ddl(statement);
            self.client.batch_execute(&statement)?;
        }
        Ok(())
    }

    fn schema_needs_lock(&self) -> bool {
        // Concurrent CREATE TABLE IF NOT EXISTS races on the catalogue.
        true
    }

    fn begin_write(&mut self) -> Result<(), DbError> {
        self.client.batch_execute("BEGIN")?;
        self.dirty = false;
        if let Err(error) = self
            .client
            .batch_execute(&format!("SELECT pg_advisory_xact_lock({WRITER_LOCK})"))
        {
            self.rollback();
            return Err(error.into());
        }
        Ok(())
    }

    fn commit(&mut self) -> Result<(), DbError> {
        // Waiters are woken at COMMIT, and only when a row changed.
        if self.dirty {
            self.client.batch_execute(&format!("NOTIFY {CHANNEL}"))?;
        }
        self.client.batch_execute("COMMIT")?;
        Ok(())
    }

    fn rollback(&mut self) {
        let _ = self.client.batch_execute("ROLLBACK");
    }

    fn pause(&mut self, seconds: f64) -> Result<(), DbError> {
        if !self.listening {
            self.client.batch_execute(&format!("LISTEN {CHANNEL}"))?;
            self.listening = true;
        }
        let delay = Duration::try_from_secs_f64(seconds).unwrap_or(Duration::ZERO);
        let mut notifications = self.client.notifications();
        let mut wait = notifications.timeout_iter(delay);
        let _ = wait.next()?;
        Ok(())
    }
}

/// Split a libpq URL into the `tokio-postgres` configuration and the TLS
/// settings (`None`: clear text, only for a Unix socket or `sslmode=disable`).
fn parse(url: &str) -> Result<(postgres::Config, Option<TlsSettings>), DbError> {
    let (base, query) = url.split_once('?').unwrap_or((url, ""));
    let mut kept = Vec::new();
    let mut ssl = HashMap::new();
    let mut passfile = None;
    for pair in query.split('&').filter(|p| !p.is_empty()) {
        let (key, value) = pair.split_once('=').unwrap_or((pair, ""));
        let decoded = percent_decode(value);
        match key {
            "sslmode"
            | "sslrootcert"
            | "sslcert"
            | "sslkey"
            | "sslpassword"
            | "sslcrl"
            | "sslcrldir"
            | "sslsni"
            | "ssl_min_protocol_version"
            | "ssl_max_protocol_version" => {
                ssl.insert(key.to_owned(), decoded);
            }
            "passfile" => passfile = Some(decoded),
            "gssencmode" => {
                if decoded == "require" {
                    return Err(DbError::Config(
                        "gssencmode=require: GSSAPI is not supported".into(),
                    ));
                }
            }
            "client_encoding" => {
                if !decoded.eq_ignore_ascii_case("utf8") && !decoded.eq_ignore_ascii_case("utf-8") {
                    return Err(DbError::Config(
                        "the bus speaks UTF-8 only (client_encoding)".into(),
                    ));
                }
            }
            "fallback_application_name" => {}
            _ => kept.push(pair),
        }
    }
    let rebuilt = if kept.is_empty() {
        base.to_owned()
    } else {
        format!("{base}?{}", kept.join("&"))
    };
    let mut config: postgres::Config = rebuilt
        .parse()
        .map_err(|e: postgres::Error| DbError::Config(e.to_string()))?;
    apply_environment(&mut config);
    let unix_socket = config
        .get_hosts()
        .iter()
        .all(|h| !matches!(h, postgres::config::Host::Tcp(_)));
    let settings = tls::settings(&ssl, unix_socket)?;
    if config.get_password().is_none() {
        if let Some(password) = password_for(&config, passfile.as_deref()) {
            config.password(password);
        }
    }
    config.ssl_mode(if settings.is_some() {
        postgres::config::SslMode::Require
    } else {
        postgres::config::SslMode::Disable
    });
    Ok((config, settings))
}

/// libpq's environment defaults for what the URL leaves out.
fn apply_environment(config: &mut postgres::Config) {
    let env = |name: &str| std::env::var(name).ok().filter(|v| !v.is_empty());
    if config.get_hosts().is_empty() {
        config.host(&env("PGHOST").unwrap_or_else(|| "localhost".to_owned()));
    }
    if config.get_ports().is_empty() {
        if let Some(port) = env("PGPORT").and_then(|p| p.parse().ok()) {
            config.port(port);
        }
    }
    if config.get_user().is_none() {
        let user = env("PGUSER")
            .or_else(|| env("USER"))
            .or_else(|| env("USERNAME"))
            .unwrap_or_default();
        config.user(&user);
    }
    if config.get_dbname().is_none() {
        if let Some(dbname) = env("PGDATABASE") {
            config.dbname(&dbname);
        }
    }
}

/// `PGPASSWORD`, else the first matching line of the pgpass file.
fn password_for(config: &postgres::Config, passfile: Option<&str>) -> Option<String> {
    if let Some(password) = std::env::var("PGPASSWORD").ok().filter(|p| !p.is_empty()) {
        return Some(password);
    }
    let path = passfile
        .map(std::path::PathBuf::from)
        .or_else(|| std::env::var_os("PGPASSFILE").map(std::path::PathBuf::from))
        .or_else(default_passfile)?;
    if !private_enough(&path) {
        return None;
    }
    let text = std::fs::read_to_string(&path).ok()?;
    let host = match config.get_hosts().first() {
        Some(postgres::config::Host::Tcp(host)) => host.clone(),
        _ => "localhost".to_owned(),
    };
    let port = config.get_ports().first().copied().unwrap_or(5432).to_string();
    let user = config.get_user().unwrap_or_default().to_owned();
    let dbname = config.get_dbname().map_or_else(|| user.clone(), str::to_owned);
    pgpass_lookup(&text, [&host, &port, &dbname, &user])
}

#[cfg(windows)]
fn default_passfile() -> Option<std::path::PathBuf> {
    std::env::var_os("APPDATA").map(|d| std::path::PathBuf::from(d).join("postgresql").join("pgpass.conf"))
}

#[cfg(not(windows))]
fn default_passfile() -> Option<std::path::PathBuf> {
    std::env::home_dir().map(|h| h.join(".pgpass"))
}

/// libpq ignores a pgpass file readable by the group or others.
#[cfg(unix)]
fn private_enough(path: &std::path::Path) -> bool {
    use std::os::unix::fs::PermissionsExt;
    std::fs::metadata(path).is_ok_and(|m| m.permissions().mode() & 0o077 == 0)
}

#[cfg(not(unix))]
fn private_enough(_path: &std::path::Path) -> bool {
    true
}

/// One pgpass line split on unescaped `:`, `\` escaping `:` and `\`.
fn pgpass_fields(line: &str) -> Vec<String> {
    let mut fields = vec![String::new()];
    let mut chars = line.chars();
    while let Some(c) = chars.next() {
        match c {
            '\\' => {
                if let (Some(next), Some(field)) = (chars.next(), fields.last_mut()) {
                    field.push(next);
                }
            }
            ':' if fields.len() < 5 => fields.push(String::new()),
            c => {
                if let Some(field) = fields.last_mut() {
                    field.push(c);
                }
            }
        }
    }
    fields
}

/// The password of the first line matching host, port, database and user
/// (`*` matches anything).
pub fn pgpass_lookup(text: &str, wanted: [&str; 4]) -> Option<String> {
    text.lines()
        .filter(|l| !l.trim_start().starts_with('#') && !l.trim().is_empty())
        .find_map(|line| {
            let fields = pgpass_fields(line.trim_end_matches('\r'));
            let [host, port, db, user, password] = fields.as_slice() else {
                return None;
            };
            let matches = [host, port, db, user]
                .iter()
                .zip(wanted)
                .all(|(field, want)| *field == "*" || *field == want);
            matches.then(|| password.clone())
        })
}

fn hex(b: u8) -> Option<u8> {
    match b {
        b'0'..=b'9' => Some(b - b'0'),
        b'a'..=b'f' => Some(b - b'a' + 10),
        b'A'..=b'F' => Some(b - b'A' + 10),
        _ => None,
    }
}

/// `%XX` decoding of a URL query value (`+` stays `+`, as libpq reads it).
pub fn percent_decode(text: &str) -> String {
    let bytes = text.as_bytes();
    let mut out = Vec::with_capacity(bytes.len());
    let mut i = 0;
    while let Some(&b) = bytes.get(i) {
        let pair = bytes
            .get(i + 1)
            .copied()
            .and_then(hex)
            .zip(bytes.get(i + 2).copied().and_then(hex));
        match (b, pair) {
            (b'%', Some((hi, lo))) => {
                out.push(hi * 16 + lo);
                i += 3;
            }
            _ => {
                out.push(b);
                i += 1;
            }
        }
    }
    String::from_utf8_lossy(&out).into_owned()
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn translate_should_number_placeholders_outside_literals() {
        assert_eq!(
            translate("SELECT '?' WHERE a=? AND b=?"),
            "SELECT '?' WHERE a=$1 AND b=$2"
        );
    }

    #[test]
    fn ddl_should_collate_text_and_defer_references() {
        let conn_ddl = |s: &str| defer_references(&collate_text(s));
        assert_eq!(
            conn_ddl("agent TEXT NOT NULL REFERENCES agents(name),\n x TEXTUAL"),
            "agent TEXT COLLATE \"C\" NOT NULL REFERENCES agents(name) DEFERRABLE INITIALLY DEFERRED,\n x TEXTUAL"
        );
    }

    #[test]
    fn pgpass_should_match_wildcards_and_escapes() {
        let text = "# comment\nother:5432:*:iab:no\ndb.example:*:bus:iab:pa\\:ss\\\\word\n";
        assert_eq!(
            pgpass_lookup(text, ["db.example", "5433", "bus", "iab"]).as_deref(),
            Some("pa:ss\\word")
        );
    }

    #[test]
    fn percent_decode_should_decode_paths() {
        assert_eq!(percent_decode("%2Ftmp%2Fr%C3%A9p"), "/tmp/rép");
    }
}
