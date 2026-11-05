//! The bus operations, one by one the counterparts of store.py's, with the
//! same SQL, the same transactions and the same answers.

use std::time::{Duration, Instant};

use chrono::{DateTime, Utc};
use iab_db::{AnyConnection, Connection, PostgresConnection, Row, SqliteConnection, Value, write};
use serde_json::{Map, Value as Json, json};

use crate::Error;
use crate::clock;
use crate::identity::{self, Identity, Target, is_valid_name, split_name};
use crate::pytext::{self, fold, repr_list, strip};

/// The mechanism's own voice (task-queued notices): no client may register,
/// rename to or speak as it.
const SYSTEM_AUTHOR: &str = "bus";
const DEFAULT_PRESENCE_TTL: i64 = 120;
const MESSAGE_LIMIT: usize = 16 * 1024;

/// An answer, or the `ERROR: …` refusal that replaces it.
type Answer<T> = Result<T, String>;

/// A written broadcast: its id, its recipients, the agents alive then.
type Sent = (i64, Vec<String>, Vec<String>);

/// The bus database of the current process, opened per operation.
#[derive(Debug, Clone)]
pub struct Bus {
    target: Target,
}

impl Bus {
    /// The bus the environment designates (`CONTRAT.md`, § 1). A hub
    /// (`IAB_HUB`) is refused rather than ignored.
    pub fn from_env() -> Result<Self, Error> {
        if !strip(&std::env::var("IAB_HUB").unwrap_or_default()).is_empty() {
            return Err(Error::Unsupported(
                "IAB_HUB is set — the native client does not speak to a hub; use the Python \
                 implementation (pip install inter-agent-bus)."
                    .to_owned(),
            ));
        }
        Ok(Self {
            target: identity::resolve_db().0,
        })
    }

    /// A bus on an explicit SQLite file.
    pub fn at(path: impl Into<std::path::PathBuf>) -> Self {
        Self {
            target: Target::File(path.into()),
        }
    }

    /// A bus on an explicit target.
    pub fn on(target: Target) -> Self {
        Self { target }
    }

    /// One connection, schema and additive migrations applied.
    fn connect(&self) -> Result<AnyConnection, Error> {
        let mut conn = match &self.target {
            Target::File(path) => AnyConnection::Sqlite(SqliteConnection::open(path)?),
            Target::Url(url) => AnyConnection::Postgres(Box::new(PostgresConnection::open(url)?)),
        };
        setup(&mut conn)?;
        Ok(conn)
    }
}

fn setup<C: Connection>(conn: &mut C) -> Result<(), Error> {
    if conn.schema_needs_lock() {
        // PostgreSQL: schema and migrations under the writer lock, as
        // `PostgresDialect.prepare` does.
        return write(conn, |c| -> Result<(), Error> {
            c.create_schema()?;
            migrate(c)
        });
    }
    conn.create_schema()?;
    if needs_migration(conn)? {
        // Under the writer lock: two processes opening an old file at once
        // must not add the same column twice.
        write(conn, migrate)?;
    }
    Ok(())
}

fn has(columns: &[String], name: &str) -> bool {
    columns.iter().any(|c| c == name)
}

fn needs_migration<C: Connection>(conn: &mut C) -> Result<bool, Error> {
    let wanted: [(&str, &[&str]); 5] = [
        ("tasks", &["max_attempts"]),
        ("agents", &["harness", "workspace", "cwd"]),
        ("presence", &["model"]),
        ("notifications", &["broadcast_id", "reply_to", "wake"]),
        ("events", &["machine"]),
    ];
    for (table, names) in wanted {
        let columns = conn.columns(table)?;
        if names.iter().any(|n| !has(&columns, n)) {
            return Ok(true);
        }
    }
    Ok(!conn
        .query("SELECT name FROM agents WHERE harness = ''", &[])?
        .is_empty())
}

fn add_column<C: Connection>(conn: &mut C, table: &str, declaration: &str) -> Result<(), Error> {
    let statement = conn.ddl(&format!("ALTER TABLE {table} ADD COLUMN {declaration}"));
    conn.execute(&statement, &[])?;
    Ok(())
}

/// `_migrate` of store.py: additive only, in the same order.
fn migrate<C: Connection>(conn: &mut C) -> Result<(), Error> {
    if !has(&conn.columns("tasks")?, "max_attempts") {
        add_column(conn, "tasks", "max_attempts INTEGER NOT NULL DEFAULT 3")?;
    }
    let columns = conn.columns("agents")?;
    for column in ["harness", "workspace", "cwd"] {
        if !has(&columns, column) {
            add_column(conn, "agents", &format!("{column} TEXT NOT NULL DEFAULT ''"))?;
        }
    }
    for row in conn.query("SELECT name FROM agents WHERE harness = ''", &[])? {
        let name = row.text("name")?.to_owned();
        let (harness, workspace) = split_name(&name);
        conn.execute(
            "UPDATE agents SET harness=?, workspace=? WHERE name=?",
            &[harness.into(), workspace.into(), name.into()],
        )?;
    }
    if !has(&conn.columns("presence")?, "model") {
        add_column(conn, "presence", "model TEXT NOT NULL DEFAULT ''")?;
    }
    let columns = conn.columns("notifications")?;
    if !has(&columns, "broadcast_id") {
        add_column(conn, "notifications", "broadcast_id INTEGER")?;
    }
    if !has(&columns, "reply_to") {
        add_column(conn, "notifications", "reply_to INTEGER")?;
    }
    if !has(&columns, "wake") {
        add_column(conn, "notifications", "wake INTEGER NOT NULL DEFAULT 0")?;
    }
    if !has(&conn.columns("events")?, "machine") {
        add_column(conn, "events", "machine TEXT NOT NULL DEFAULT ''")?;
    }
    Ok(())
}

fn log_event<C: Connection>(
    conn: &mut C,
    event: &str,
    task_id: Option<&str>,
    agent: Option<&str>,
    detail: &str,
) -> Result<(), Error> {
    conn.execute(
        "INSERT INTO events(task_id, agent, event, at, detail, machine) VALUES(?,?,?,?,?,?)",
        &[
            task_id.into(),
            agent.into(),
            event.into(),
            clock::now_iso().into(),
            detail.into(),
            "".into(),
        ],
    )?;
    Ok(())
}

fn exists<C: Connection>(conn: &mut C, sql: &str, params: &[Value]) -> Result<bool, Error> {
    Ok(!conn.query(sql, params)?.is_empty())
}

fn known_agents<C: Connection>(conn: &mut C) -> Result<Vec<String>, Error> {
    conn.query("SELECT name FROM agents ORDER BY name", &[])?
        .iter()
        .map(|r| Ok(r.text("name")?.to_owned()))
        .collect()
}

/// Turn an address into one registered instance: an exact name, or a bare
/// harness designating exactly one instance. Ambiguity, patterns and
/// unknown names are refused.
fn resolve_target<C: Connection>(conn: &mut C, text: &str) -> Result<Answer<String>, Error> {
    let target = fold(text);
    if target.is_empty() {
        return Ok(Err("ERROR: empty agent name.".to_owned()));
    }
    if target.contains('*') {
        return Ok(Err(format!(
            "ERROR: '{text}' is a pattern — reserved to broadcast, not a valid target."
        )));
    }
    if target.contains('@') {
        if exists(
            conn,
            "SELECT 1 FROM agents WHERE name=?",
            &[target.as_str().into()],
        )? {
            return Ok(Ok(target));
        }
    } else {
        let candidates: Vec<String> = conn
            .query(
                "SELECT name FROM agents WHERE harness=? ORDER BY name",
                &[target.as_str().into()],
            )?
            .iter()
            .map(|r| Ok(r.text("name")?.to_owned()))
            .collect::<Result<_, Error>>()?;
        if let [only] = candidates.as_slice() {
            return Ok(Ok(only.clone()));
        }
        if !candidates.is_empty() {
            let hint = if candidates.contains(&target) {
                format!(
                    " The legacy bare name '{target}' is one of them: rename_agent it to \
                     harness@workspace to lift the ambiguity."
                )
            } else {
                String::new()
            };
            return Ok(Err(format!(
                "ERROR: '{text}' is ambiguous — instances: {}. Address one of them.{hint}",
                candidates.join(", ")
            )));
        }
    }
    let known = known_agents(conn)?;
    let known = if known.is_empty() {
        "none — call register_agent first".to_owned()
    } else {
        repr_list(&known)
    };
    Ok(Err(format!(
        "ERROR: agent '{text}' not registered. Known agents: {known}."
    )))
}

/// `error.replace("agent", "author", 1)`.
fn as_author(error: &str) -> String {
    error.replacen("agent", "author", 1)
}

fn too_long(message: &str) -> bool {
    message.len() > MESSAGE_LIMIT
}

fn row_json(row: &Row) -> Json {
    let mut map = Map::new();
    for (column, value) in row.iter() {
        map.insert(column.to_owned(), value_json(value));
    }
    Json::Object(map)
}

fn value_json(value: &Value) -> Json {
    match value {
        Value::Null => Json::Null,
        Value::Int(n) => json!(n),
        Value::Text(text) => json!(text),
    }
}

fn none_if_empty(text: &str) -> Json {
    if text.is_empty() { Json::Null } else { json!(text) }
}

fn clamp_limit(limit: i64) -> i64 {
    limit.clamp(1, 1000)
}

fn alive(now: DateTime<Utc>, last_seen: &str, ttl: Option<i64>) -> Result<bool, Error> {
    let deadline = clock::shift(clock::parse(last_seen)?, ttl.unwrap_or(0))?;
    Ok(now < deadline)
}

/// A deadline for a blocking wait, `None` when it lies beyond any clock.
fn wait_deadline(timeout: f64) -> Option<Instant> {
    let timeout = if timeout.is_nan() { 0.0 } else { timeout.max(0.0) };
    Duration::try_from_secs_f64(timeout)
        .ok()
        .and_then(|d| Instant::now().checked_add(d))
}

fn remaining(deadline: Option<Instant>) -> f64 {
    deadline.map_or(f64::INFINITY, |d| {
        d.saturating_duration_since(Instant::now()).as_secs_f64()
    })
}

fn expired(deadline: Option<Instant>) -> bool {
    deadline.is_some_and(|d| Instant::now() >= d)
}

/// Re-offer (or dead-letter) the tasks whose lease expired. Outside a
/// write transaction callers race each other: every UPDATE re-checks the
/// row it read, so each transition is journaled once.
fn requeue_expired<C: Connection>(conn: &mut C) -> Result<(), Error> {
    let now = clock::now_iso();
    let rows = conn.query(
        "SELECT task_id, agent, attempts, max_attempts FROM tasks \
         WHERE status='claimed' AND lease_deadline < ?",
        &[now.as_str().into()],
    )?;
    for row in rows {
        let task_id = row.text("task_id")?;
        let agent = row.text("agent")?;
        let attempts = row.int("attempts")?;
        let max_attempts = row.int("max_attempts")?;
        let params = [task_id.into(), now.as_str().into(), attempts.into()];
        if attempts >= max_attempts {
            let changed = conn.execute(
                "UPDATE tasks SET status='dead', claimed_at=NULL, lease_deadline=NULL \
                 WHERE task_id=? AND status='claimed' AND lease_deadline < ? AND attempts=?",
                &params,
            )?;
            if changed == 0 {
                continue;
            }
            log_event(
                conn,
                "dead",
                Some(task_id),
                Some(agent),
                &format!(
                    "lease expired after attempt {attempts}/{max_attempts} — dead-lettered \
                     (requeue to revive)"
                ),
            )?;
        } else {
            let changed = conn.execute(
                "UPDATE tasks SET status='queued', claimed_at=NULL, lease_deadline=NULL \
                 WHERE task_id=? AND status='claimed' AND lease_deadline < ? AND attempts=?",
                &params,
            )?;
            if changed == 0 {
                continue;
            }
            log_event(
                conn,
                "expire",
                Some(task_id),
                Some(agent),
                "lease expired — task re-offered",
            )?;
        }
    }
    Ok(())
}

fn set_model<C: Connection>(conn: &mut C, agent: &str, model: &str, force: bool) -> Result<(), Error> {
    conn.execute(
        "INSERT INTO presence(agent, last_seen, ttl_seconds, capabilities, model) \
         VALUES(?,?,?,?,?) ON CONFLICT(agent) DO UPDATE SET model = CASE \
         WHEN ? OR presence.model = '' THEN excluded.model ELSE presence.model END",
        &[
            agent.into(),
            clock::now_iso().into(),
            DEFAULT_PRESENCE_TTL.into(),
            "{}".into(),
            model.into(),
            force.into(),
        ],
    )?;
    Ok(())
}

/// `touch_presence()` of store.py: refresh `last_seen` only, keeping the
/// declared TTL and capability card (a default-TTL row for an absent
/// agent); a `model` fills an empty slot only, never overrides a
/// declaration.
fn touch_presence<C: Connection>(conn: &mut C, agent: &str, model: Option<&str>) -> Result<(), Error> {
    let rows = conn.query(
        "SELECT ttl_seconds, capabilities, model FROM presence WHERE agent=?",
        &[agent.into()],
    )?;
    let (ttl, capabilities, mut stored_model) = match rows.first() {
        Some(row) => (
            row.int("ttl_seconds")?,
            row.text("capabilities")?.to_owned(),
            row.text("model")?.to_owned(),
        ),
        None => (DEFAULT_PRESENCE_TTL, "{}".to_owned(), String::new()),
    };
    if let Some(model) = model.filter(|m| !m.is_empty()) {
        if stored_model.is_empty() {
            strip(model).clone_into(&mut stored_model);
        }
    }
    conn.execute(
        "INSERT INTO presence(agent, last_seen, ttl_seconds, capabilities, model) \
         VALUES(?,?,?,?,?) ON CONFLICT(agent) DO UPDATE SET \
         last_seen=excluded.last_seen, model=excluded.model",
        &[
            agent.into(),
            clock::now_iso().into(),
            ttl.into(),
            capabilities.into(),
            stored_model.into(),
        ],
    )?;
    Ok(())
}

fn is_broadcast<C: Connection>(conn: &mut C, id: i64) -> Result<bool, Error> {
    exists(
        conn,
        "SELECT 1 FROM channel WHERE seq=? AND topic='broadcast'",
        &[id.into()],
    )
}

fn alive_set<C: Connection>(conn: &mut C) -> Result<Vec<String>, Error> {
    let now = clock::now();
    let mut alive_agents = Vec::new();
    for row in conn.query("SELECT agent, last_seen, ttl_seconds FROM presence", &[])? {
        if alive(now, row.text("last_seen")?, row.opt_int("ttl_seconds")?)? {
            alive_agents.push(row.text("agent")?.to_owned());
        }
    }
    Ok(alive_agents)
}

fn names_of(rows: &[Row], column: &str) -> Result<Vec<String>, Error> {
    rows.iter().map(|r| Ok(r.text(column)?.to_owned())).collect()
}

/// Python's `str(item)` for the entries of a JSON scope list.
fn py_str(item: &Json) -> String {
    match item {
        Json::String(text) => text.clone(),
        Json::Null => "None".to_owned(),
        Json::Bool(true) => "True".to_owned(),
        Json::Bool(false) => "False".to_owned(),
        other => other.to_string(),
    }
}

/// Recipients of a broadcast: `*`, `harness@*`, `*@workspace`,
/// `model:<glob>`, or a comma-separated or JSON list whose every entry
/// must resolve.
fn expand_scope<C: Connection>(conn: &mut C, scope: &str) -> Result<Answer<Vec<String>>, Error> {
    let text = fold(scope);
    if text.is_empty() {
        return Ok(Err("ERROR: empty scope.".to_owned()));
    }
    let found = |names: Vec<String>, error: String| -> Answer<Vec<String>> {
        if names.is_empty() { Err(error) } else { Ok(names) }
    };
    if text == "*" {
        return Ok(found(
            known_agents(conn)?,
            "ERROR: no registered agent.".to_owned(),
        ));
    }
    if text.matches('@').count() == 1 {
        if let Some(harness) = text.strip_suffix("@*") {
            let rows = conn.query(
                "SELECT name FROM agents WHERE harness=? ORDER BY name",
                &[harness.into()],
            )?;
            return Ok(found(
                names_of(&rows, "name")?,
                format!("ERROR: no instance of harness '{harness}'."),
            ));
        }
        if let Some(workspace) = text.strip_prefix("*@") {
            let rows = conn.query(
                "SELECT name FROM agents WHERE workspace=? ORDER BY name",
                &[workspace.into()],
            )?;
            return Ok(found(
                names_of(&rows, "name")?,
                format!("ERROR: no agent in workspace '{workspace}'."),
            ));
        }
    }
    if let Some(glob) = text.strip_prefix("model:") {
        let rows = conn.query(
            "SELECT a.name AS agent, p.model FROM agents a \
             JOIN presence p ON p.agent = a.name ORDER BY a.name",
            &[],
        )?;
        let mut names = Vec::new();
        for row in rows {
            let model = row.opt_text("model")?.unwrap_or_default();
            if !model.is_empty() && pytext::fnmatchcase(&model.to_lowercase(), glob) {
                names.push(row.text("agent")?.to_owned());
            }
        }
        return Ok(found(
            names,
            format!("ERROR: no instance with a model matching '{glob}'."),
        ));
    }
    let items: Vec<String> = if text.starts_with('[') {
        match serde_json::from_str::<Json>(scope) {
            Ok(Json::Array(items)) => items.iter().map(py_str).collect(),
            Ok(Json::String(text)) => text.chars().map(String::from).collect(),
            Ok(_) => return Ok(Err("ERROR: scope list is not valid JSON.".to_owned())),
            Err(_) => return Ok(Err("ERROR: scope list is not valid JSON.".to_owned())),
        }
    } else {
        text.split(',').map(str::to_owned).collect()
    };
    let mut names: Vec<String> = Vec::new();
    for item in items {
        match resolve_target(conn, &item)? {
            Ok(name) => {
                if !names.contains(&name) {
                    names.push(name);
                }
            }
            Err(error) => return Ok(Err(error)),
        }
    }
    Ok(found(names, "ERROR: empty scope.".to_owned()))
}

/// Signals waiting for an instance, without moving any cursor.
fn pending<C: Connection>(conn: &mut C, target: &str) -> Result<Map<String, Json>, Error> {
    requeue_expired(conn)?;
    let tasks = conn.query(
        "SELECT task_id FROM tasks WHERE agent=? AND status='queued' \
         ORDER BY priority DESC, created_at ASC",
        &[target.into()],
    )?;
    let cursor = conn.query(
        "SELECT last_seq FROM notification_cursor WHERE target=?",
        &[target.into()],
    )?;
    let last_seq = match cursor.first() {
        Some(row) => row.int("last_seq")?,
        None => 0,
    };
    let rows = conn.query(
        "SELECT seq, broadcast_id, wake FROM notifications WHERE target=? AND seq > ?",
        &[target.into(), last_seq.into()],
    )?;
    let mut wake_direct = 0;
    let mut wake_broadcasts = std::collections::BTreeSet::new();
    let mut broadcasts = std::collections::BTreeSet::new();
    for row in &rows {
        let wake = row.opt_int("wake")?.unwrap_or(0) != 0;
        let broadcast = row.opt_int("broadcast_id")?;
        if wake && broadcast.is_none() {
            wake_direct += 1;
        }
        if let Some(id) = broadcast.filter(|id| *id != 0) {
            broadcasts.insert(id);
            if wake {
                wake_broadcasts.insert(id);
            }
        }
    }
    let mut map = Map::new();
    map.insert("agent".into(), json!(target));
    map.insert("tasks".into(), json!(names_of(&tasks, "task_id")?));
    map.insert("notifications".into(), json!(rows.len()));
    map.insert("wake_notifications".into(), json!(wake_direct));
    map.insert("wake_broadcasts".into(), json!(wake_broadcasts));
    map.insert("broadcasts".into(), json!(broadcasts));
    Ok(map)
}

/// The kinds of signal a wait ends on.
fn kinds(text: &str) -> Answer<(bool, bool)> {
    let wanted: std::collections::BTreeSet<String> =
        text.split(',').map(fold).filter(|k| !k.is_empty()).collect();
    if wanted.is_empty() || wanted.contains("any") {
        return Ok((true, true));
    }
    if wanted.iter().any(|k| k != "task" && k != "notification") {
        let shown: Vec<String> = wanted.iter().cloned().collect();
        return Err(format!(
            "ERROR: kinds must be task, notification or any (got {}).",
            repr_list(&shown)
        ));
    }
    Ok((wanted.contains("task"), wanted.contains("notification")))
}

fn signalled(pending: &Map<String, Json>, (task, notification): (bool, bool)) -> bool {
    let tasks = pending
        .get("tasks")
        .and_then(Json::as_array)
        .is_some_and(|t| !t.is_empty());
    let notes = pending
        .get("notifications")
        .and_then(Json::as_u64)
        .is_some_and(|n| n > 0);
    (task && tasks) || (notification && notes)
}

fn presence_status(
    now: DateTime<Utc>,
    last_seen: Option<&str>,
    ttl: Option<i64>,
) -> Result<&'static str, Error> {
    Ok(match last_seen {
        None => "unknown",
        Some(seen) => {
            if alive(now, seen, ttl)? {
                "alive"
            } else {
                "asleep"
            }
        }
    })
}

impl Bus {
    /// Declare an agent instance (creates its queue).
    pub fn register_agent(
        &self,
        name: &str,
        description: &str,
        cwd: &str,
        model: Option<&str>,
    ) -> Result<String, Error> {
        let name = fold(name);
        if name.is_empty() {
            return Ok("ERROR: empty agent name.".to_owned());
        }
        if !is_valid_name(&name) {
            return Ok(format!(
                "ERROR: invalid agent name '{name}' — expected harness[@workspace], each \
                 component [a-z0-9][a-z0-9._-]{{0,39}}."
            ));
        }
        if name == SYSTEM_AUTHOR {
            return Ok(format!("ERROR: '{name}' is reserved for the bus itself."));
        }
        let (harness, workspace) = split_name(&name);
        let mut conn = self.connect()?;
        write(&mut conn, |c| -> Result<(), Error> {
            let known = exists(c, "SELECT 1 FROM agents WHERE name=?", &[name.as_str().into()])?;
            c.execute(
                "INSERT INTO agents(name, description, registered_at, harness, workspace, cwd) \
                 VALUES(?,?,?,?,?,?) ON CONFLICT(name) DO UPDATE SET \
                 description=excluded.description, harness=excluded.harness, \
                 workspace=excluded.workspace, \
                 cwd=CASE WHEN excluded.cwd='' THEN agents.cwd ELSE excluded.cwd END",
                &[
                    name.as_str().into(),
                    description.into(),
                    clock::now_iso().into(),
                    harness.as_str().into(),
                    workspace.as_str().into(),
                    cwd.into(),
                ],
            )?;
            if !known {
                log_event(c, "register", None, Some(&name), description)?;
            }
            if let Some(model) = model.filter(|m| !m.is_empty()) {
                set_model(c, &name, strip(model), true)?;
            }
            Ok(())
        })?;
        Ok(format!("OK: agent '{name}' registered."))
    }

    /// Move an identity — queue, results, notifications, cursors, presence,
    /// journal — to a new name in one transaction.
    pub fn rename_agent(&self, old: &str, new: &str) -> Result<String, Error> {
        let (old, new) = (fold(old), fold(new));
        if old.is_empty() || new.is_empty() {
            return Ok("ERROR: empty agent name.".to_owned());
        }
        if !is_valid_name(&new) {
            return Ok(format!(
                "ERROR: invalid agent name '{new}' — expected harness[@workspace]."
            ));
        }
        if new == SYSTEM_AUTHOR {
            return Ok(format!("ERROR: '{new}' is reserved for the bus itself."));
        }
        let (harness, workspace) = split_name(&new);
        let mut conn = self.connect()?;
        write(&mut conn, |c| -> Result<String, Error> {
            if !exists(c, "SELECT 1 FROM agents WHERE name=?", &[old.as_str().into()])? {
                return Ok(format!("ERROR: agent '{old}' not registered."));
            }
            if exists(c, "SELECT 1 FROM agents WHERE name=?", &[new.as_str().into()])? {
                return Ok(format!("ERROR: agent '{new}' already exists — rename refused."));
            }
            c.execute(
                "UPDATE agents SET name=?, harness=?, workspace=? WHERE name=?",
                &[
                    new.as_str().into(),
                    harness.into(),
                    workspace.into(),
                    old.as_str().into(),
                ],
            )?;
            for (table, column) in [
                ("tasks", "agent"),
                ("results", "author"),
                ("events", "agent"),
                ("notifications", "target"),
                ("notifications", "author"),
                ("notification_cursor", "target"),
                ("channel", "author"),
                ("channel_cursor", "agent"),
            ] {
                c.execute(
                    &format!("UPDATE {table} SET {column}=? WHERE {column}=?"),
                    &[new.as_str().into(), old.as_str().into()],
                )?;
            }
            if exists(c, "SELECT 1 FROM presence WHERE agent=?", &[new.as_str().into()])? {
                c.execute("DELETE FROM presence WHERE agent=?", &[old.as_str().into()])?;
            } else {
                c.execute(
                    "UPDATE presence SET agent=? WHERE agent=?",
                    &[new.as_str().into(), old.as_str().into()],
                )?;
            }
            log_event(c, "rename", None, Some(&new), &format!("was {old}"))?;
            Ok(format!("OK: agent '{old}' renamed to '{new}'."))
        })
    }

    /// Queue a task and, unless `notify` is false, drop a "task queued"
    /// notification in the target's mailbox in the same transaction.
    pub fn push_task(
        &self,
        target_agent: &str,
        task_id: &str,
        payload: &str,
        priority: i64,
        max_attempts: i64,
        notify: bool,
    ) -> Result<String, Error> {
        let mut conn = self.connect()?;
        write(&mut conn, |c| -> Result<String, Error> {
            let target = match resolve_target(c, target_agent)? {
                Ok(target) => target,
                Err(error) => return Ok(error),
            };
            if exists(c, "SELECT 1 FROM tasks WHERE task_id=?", &[task_id.into()])? {
                return Ok(format!("ERROR: task_id '{task_id}' already exists."));
            }
            c.execute(
                "INSERT INTO tasks(task_id, agent, payload, priority, created_at, max_attempts) \
                 VALUES(?,?,?,?,?,?)",
                &[
                    task_id.into(),
                    target.as_str().into(),
                    payload.into(),
                    priority.into(),
                    clock::now_iso().into(),
                    max_attempts.into(),
                ],
            )?;
            log_event(
                c,
                "push",
                Some(task_id),
                Some(&target),
                &format!("priority {priority}"),
            )?;
            if notify {
                c.execute(
                    "INSERT INTO notifications(target, author, at, message) VALUES(?,?,?,?)",
                    &[
                        target.as_str().into(),
                        SYSTEM_AUTHOR.into(),
                        clock::now_iso().into(),
                        format!("task '{task_id}' queued (priority {priority}) — claim it with claim_task")
                            .into(),
                    ],
                )?;
            }
            Ok(format!("OK: task '{task_id}' queued for {target}."))
        })
    }

    /// Block until the agent's queue holds a queued task; the queued task
    /// ids as a JSON list, `[]` on timeout.
    pub fn wait_for_task(&self, agent: &str, timeout: f64, interval: f64) -> Result<String, Error> {
        if strip(agent).is_empty() {
            return Ok("ERROR: empty agent.".to_owned());
        }
        let interval = if interval.is_nan() { 0.2 } else { interval.max(0.2) };
        let deadline = wait_deadline(timeout);
        let mut conn = self.connect()?;
        let target = match resolve_target(&mut conn, agent)? {
            Ok(target) => target,
            Err(error) => return Ok(error),
        };
        loop {
            requeue_expired(&mut conn)?;
            let rows = conn.query(
                "SELECT task_id FROM tasks WHERE agent=? AND status='queued' \
                 ORDER BY priority DESC, created_at ASC",
                &[target.as_str().into()],
            )?;
            if !rows.is_empty() {
                return Ok(pytext::dumps(&names_of(&rows, "task_id")?)?);
            }
            if expired(deadline) {
                return Ok("[]".to_owned());
            }
            conn.pause(interval.min(remaining(deadline)))?;
        }
    }

    /// Claim the highest-priority queued task of one's queue under lease —
    /// or one given queued task of one's own queue.
    pub fn claim_task(
        &self,
        agent: &str,
        lease_seconds: i64,
        task_id: Option<&str>,
    ) -> Result<String, Error> {
        let deadline = clock::after(lease_seconds)?;
        let mut conn = self.connect()?;
        write(&mut conn, |c| -> Result<String, Error> {
            let target = match resolve_target(c, agent)? {
                Ok(target) => target,
                Err(error) => return Ok(error),
            };
            requeue_expired(c)?;
            let row = match task_id {
                None => {
                    let rows = c.query(
                        "SELECT task_id, payload, priority, attempts, created_at FROM tasks \
                         WHERE agent=? AND status='queued' \
                         ORDER BY priority DESC, created_at ASC LIMIT 1",
                        &[target.as_str().into()],
                    )?;
                    match rows.into_iter().next() {
                        Some(row) => row,
                        None => return Ok("NO_TASK".to_owned()),
                    }
                }
                Some(wanted) => {
                    let rows = c.query(
                        "SELECT task_id, agent, status, payload, priority, attempts, \
                         created_at FROM tasks WHERE task_id=?",
                        &[wanted.into()],
                    )?;
                    let Some(row) = rows.into_iter().next() else {
                        return Ok(format!("ERROR: unknown task '{wanted}'."));
                    };
                    let owner = row.text("agent")?;
                    if owner != target {
                        return Ok(format!(
                            "ERROR: task '{wanted}' belongs to {owner}'s queue — no cross-queue claiming."
                        ));
                    }
                    let status = row.text("status")?;
                    if status != "queued" {
                        return Ok(format!(
                            "ERROR: task '{wanted}' is not queued (status: {status})."
                        ));
                    }
                    row
                }
            };
            let id = row.text("task_id")?.to_owned();
            let attempts = row.int("attempts")? + 1;
            c.execute(
                "UPDATE tasks SET status='claimed', claimed_at=?, lease_deadline=?, \
                 attempts=attempts+1 WHERE task_id=?",
                &[
                    clock::now_iso().into(),
                    deadline.as_str().into(),
                    id.as_str().into(),
                ],
            )?;
            let targeted = if task_id.is_some() { " (targeted)" } else { "" };
            log_event(
                c,
                "claim",
                Some(&id),
                Some(&target),
                &format!("attempt {attempts}, lease until {deadline}{targeted}"),
            )?;
            let mut answer = Map::new();
            answer.insert("task_id".into(), json!(id));
            answer.insert("payload".into(), json!(row.text("payload")?));
            answer.insert("priority".into(), json!(row.int("priority")?));
            answer.insert("attempts".into(), json!(attempts));
            answer.insert("created_at".into(), json!(row.text("created_at")?));
            answer.insert("lease_deadline".into(), json!(deadline));
            Ok(pytext::dumps(&answer)?)
        })
    }

    /// Publish a result and settle the task, fenced by the attempt number.
    pub fn publish_result(
        &self,
        agent: &str,
        task_id: &str,
        content: &str,
        attempt: Option<i64>,
        force: bool,
    ) -> Result<String, Error> {
        let author = fold(agent);
        let mut conn = self.connect()?;
        write(&mut conn, |c| -> Result<String, Error> {
            let rows = c.query(
                "SELECT agent, status, attempts FROM tasks WHERE task_id=?",
                &[task_id.into()],
            )?;
            if !force {
                let Some(row) = rows.first() else {
                    return Ok(format!(
                        "ERROR: unknown task '{task_id}' — nothing to settle (force=True to store a \
                         result anyway)."
                    ));
                };
                let owner = row.text("agent")?;
                if owner != author {
                    return Ok(format!(
                        "ERROR: task '{task_id}' belongs to {owner}'s queue; '{author}' cannot settle \
                         it (force=True to override)."
                    ));
                }
                let current = row.int("attempts")?;
                if let Some(attempt) = attempt.filter(|a| *a < current) {
                    return Ok(format!(
                        "ERROR: stale attempt token {attempt} < {current} — the lease expired and task \
                         '{task_id}' was re-offered; this result is refused (force=True to override)."
                    ));
                }
            }
            c.execute(
                "INSERT INTO results(task_id, author, content, updated_at) VALUES(?,?,?,?) \
                 ON CONFLICT(task_id) DO UPDATE SET author=excluded.author, \
                 content=excluded.content, updated_at=excluded.updated_at",
                &[
                    task_id.into(),
                    author.as_str().into(),
                    content.into(),
                    clock::now_iso().into(),
                ],
            )?;
            let settled = c.execute(
                "UPDATE tasks SET status='done' WHERE task_id=?",
                &[task_id.into()],
            )?;
            let mut detail = if settled > 0 {
                "settled".to_owned()
            } else {
                "no matching task — result stored anyway".to_owned()
            };
            if let Some(attempt) = attempt {
                detail.push_str(&format!(" (attempt {attempt})"));
            }
            if force {
                detail.push_str(" (forced)");
            }
            log_event(c, "publish", Some(task_id), Some(&author), &detail)?;
            Ok(format!("OK: result for '{task_id}' published by {author}."))
        })
    }

    /// Cancel a task that is not settled yet.
    pub fn cancel_task(&self, task_id: &str) -> Result<String, Error> {
        let mut conn = self.connect()?;
        write(&mut conn, |c| -> Result<String, Error> {
            let rows = c.query(
                "SELECT agent, status FROM tasks WHERE task_id=?",
                &[task_id.into()],
            )?;
            let Some(row) = rows.first() else {
                return Ok(format!("ERROR: unknown task '{task_id}'."));
            };
            let status = row.text("status")?;
            if status == "done" || status == "cancelled" {
                return Ok(format!("ERROR: task '{task_id}' is already {status}."));
            }
            c.execute(
                "UPDATE tasks SET status='cancelled', claimed_at=NULL, lease_deadline=NULL WHERE task_id=?",
                &[task_id.into()],
            )?;
            log_event(
                c,
                "cancel",
                Some(task_id),
                Some(row.text("agent")?),
                &format!("was {status}"),
            )?;
            Ok(format!("OK: task '{task_id}' cancelled."))
        })
    }

    /// Re-offer a claimed, dead or cancelled task, re-arming three attempts.
    pub fn requeue_task(&self, task_id: &str) -> Result<String, Error> {
        let mut conn = self.connect()?;
        write(&mut conn, |c| -> Result<String, Error> {
            let rows = c.query(
                "SELECT agent, status, attempts, max_attempts FROM tasks WHERE task_id=?",
                &[task_id.into()],
            )?;
            let Some(row) = rows.first() else {
                return Ok(format!("ERROR: unknown task '{task_id}'."));
            };
            let status = row.text("status")?;
            if status == "queued" {
                return Ok(format!("ERROR: task '{task_id}' is already queued."));
            }
            if status == "done" {
                return Ok(format!(
                    "ERROR: task '{task_id}' is settled — push a new task_id instead of requeueing a \
                     done task."
                ));
            }
            let rearmed = row.int("max_attempts")?.max(row.int("attempts")? + 3);
            c.execute(
                "UPDATE tasks SET status='queued', claimed_at=NULL, lease_deadline=NULL, \
                 max_attempts=? WHERE task_id=?",
                &[rearmed.into(), task_id.into()],
            )?;
            log_event(
                c,
                "requeue",
                Some(task_id),
                Some(row.text("agent")?),
                &format!("was {status}; max_attempts re-armed to {rearmed}"),
            )?;
            Ok(format!("OK: task '{task_id}' requeued."))
        })
    }

    /// Renew the lease of a claimed task.
    pub fn extend_lease(&self, task_id: &str, lease_seconds: i64) -> Result<String, Error> {
        let deadline = clock::after(lease_seconds)?;
        let mut conn = self.connect()?;
        write(&mut conn, |c| -> Result<String, Error> {
            let rows = c.query(
                "SELECT agent, status FROM tasks WHERE task_id=?",
                &[task_id.into()],
            )?;
            let Some(row) = rows.first() else {
                return Ok(format!("ERROR: unknown task '{task_id}'."));
            };
            let status = row.text("status")?;
            if status != "claimed" {
                return Ok(format!(
                    "ERROR: task '{task_id}' is not claimed (status: {status}) — no lease to extend."
                ));
            }
            c.execute(
                "UPDATE tasks SET lease_deadline=? WHERE task_id=?",
                &[deadline.as_str().into(), task_id.into()],
            )?;
            log_event(
                c,
                "extend",
                Some(task_id),
                Some(row.text("agent")?),
                &format!("lease until {deadline}"),
            )?;
            Ok(format!("OK: lease of '{task_id}' extended until {deadline}."))
        })
    }

    /// The published result of a task.
    pub fn read_result(&self, task_id: &str) -> Result<String, Error> {
        let mut conn = self.connect()?;
        let rows = conn.query(
            "SELECT author, content, updated_at FROM results WHERE task_id=?",
            &[task_id.into()],
        )?;
        match rows.first() {
            None => Ok(format!("ERROR: no result found for '{task_id}'.")),
            Some(row) => Ok(pytext::dumps(&row_json(row))?),
        }
    }

    /// Post or refresh a presence heartbeat with a capability card.
    pub fn heartbeat(
        &self,
        agent: &str,
        ttl_seconds: i64,
        capabilities: Option<&str>,
        model: Option<&str>,
    ) -> Result<String, Error> {
        let parsed = match capabilities {
            None => None,
            Some(text) => match serde_json::from_str::<Json>(text) {
                Ok(value @ Json::Object(_)) => Some(value),
                _ => return Ok("ERROR: capabilities must be a JSON object.".to_owned()),
            },
        };
        let mut conn = self.connect()?;
        let agent = match resolve_target(&mut conn, agent)? {
            Ok(agent) => agent,
            Err(error) => return Ok(error),
        };
        let rows = conn.query(
            "SELECT ttl_seconds, capabilities, model FROM presence WHERE agent=?",
            &[agent.as_str().into()],
        )?;
        let row = rows.first();
        let stored_capabilities = match &parsed {
            Some(value) => pytext::dumps(value)?,
            None => match row {
                Some(row) => row.text("capabilities")?.to_owned(),
                None => "{}".to_owned(),
            },
        };
        let declared = strip(model.unwrap_or_default());
        let stored_model = if declared.is_empty() {
            match row {
                Some(row) => row.text("model")?.to_owned(),
                None => String::new(),
            }
        } else {
            declared.to_owned()
        };
        let alive_until = clock::shift(clock::now(), ttl_seconds)?;
        conn.execute(
            "INSERT INTO presence(agent, last_seen, ttl_seconds, capabilities, model) \
             VALUES(?,?,?,?,?) ON CONFLICT(agent) DO UPDATE SET \
             last_seen=excluded.last_seen, ttl_seconds=excluded.ttl_seconds, \
             capabilities=excluded.capabilities, model=excluded.model",
            &[
                agent.as_str().into(),
                clock::now_iso().into(),
                ttl_seconds.into(),
                stored_capabilities.into(),
                stored_model.as_str().into(),
            ],
        )?;
        let mut answer = Map::new();
        answer.insert("agent".into(), json!(agent));
        answer.insert("alive_until".into(), json!(clock::iso(alive_until)));
        answer.insert("model".into(), none_if_empty(&stored_model));
        Ok(pytext::dumps(&answer)?)
    }

    /// Piggyback of the MCP server: the resolved identity of a connected
    /// client is a present instance. Registers it on first sight (empty
    /// description, launch directory recorded), then refreshes its
    /// presence. A nameless or invalid identity is a silent no-op; callers
    /// swallow failures so the tool call always proceeds.
    pub fn touch_identity(&self, identity: &Identity) -> Result<(), Error> {
        let name = fold(identity.agent_name.as_deref().unwrap_or_default());
        if name.is_empty() || !is_valid_name(&name) || name == SYSTEM_AUTHOR {
            return Ok(());
        }
        let mut conn = self.connect()?;
        write(&mut conn, |c| -> Result<(), Error> {
            if exists(c, "SELECT 1 FROM agents WHERE name=?", &[name.as_str().into()])? {
                if !identity.cwd.is_empty() {
                    c.execute(
                        "UPDATE agents SET cwd=? WHERE name=?",
                        &[identity.cwd.as_str().into(), name.as_str().into()],
                    )?;
                }
                return Ok(());
            }
            let (harness, workspace) = split_name(&name);
            c.execute(
                "INSERT INTO agents(name, description, registered_at, harness, workspace, cwd) \
                 VALUES(?,?,?,?,?,?) ON CONFLICT(name) DO NOTHING",
                &[
                    name.as_str().into(),
                    "".into(),
                    clock::now_iso().into(),
                    harness.as_str().into(),
                    workspace.as_str().into(),
                    identity.cwd.as_str().into(),
                ],
            )?;
            log_event(c, "register", None, Some(&name), "auto (connected client)")
        })?;
        touch_presence(&mut conn, &name, identity.model.as_deref())
    }

    /// Every known agent (registered or seen) with a computed status.
    pub fn list_presence(&self) -> Result<String, Error> {
        let mut conn = self.connect()?;
        let rows = conn.query(
            "SELECT a.name, a.description, a.harness, a.workspace, a.cwd, \
                    p.last_seen, p.ttl_seconds, p.capabilities, p.model \
             FROM agents a LEFT JOIN presence p ON a.name = p.agent \
             UNION \
             SELECT p.agent AS name, '' AS description, '' AS harness, \
                    '' AS workspace, '' AS cwd, \
                    p.last_seen, p.ttl_seconds, p.capabilities, p.model \
             FROM presence p \
             WHERE p.agent NOT IN (SELECT name FROM agents) \
             ORDER BY harness, workspace, name",
            &[],
        )?;
        let now = clock::now();
        let mut result = Vec::new();
        for row in &rows {
            let name = row.text("name")?;
            let last_seen = row.opt_text("last_seen")?;
            let ttl = row.opt_int("ttl_seconds")?;
            let capabilities = row
                .opt_text("capabilities")?
                .filter(|c| !c.is_empty())
                .unwrap_or("{}");
            let (mut harness, mut workspace) =
                (row.text("harness")?.to_owned(), row.text("workspace")?.to_owned());
            if harness.is_empty() {
                (harness, workspace) = split_name(name);
            }
            let mut entry = Map::new();
            entry.insert("agent".into(), json!(name));
            entry.insert("harness".into(), json!(harness));
            entry.insert("workspace".into(), none_if_empty(&workspace));
            entry.insert("cwd".into(), none_if_empty(row.text("cwd")?));
            entry.insert(
                "model".into(),
                none_if_empty(row.opt_text("model")?.unwrap_or_default()),
            );
            entry.insert("status".into(), json!(presence_status(now, last_seen, ttl)?));
            entry.insert("last_seen".into(), json!(last_seen));
            entry.insert("ttl_seconds".into(), json!(ttl));
            entry.insert("capabilities".into(), serde_json::from_str::<Json>(capabilities)?);
            entry.insert("description".into(), json!(row.text("description")?));
            result.push(Json::Object(entry));
        }
        Ok(pytext::dumps(&result)?)
    }

    /// Append a message to the global channel.
    pub fn announce(&self, author: &str, topic: &str, message: &str) -> Result<String, Error> {
        let topic = fold(topic);
        let topic_ok = (1..=64).contains(&topic.len())
            && topic
                .bytes()
                .all(|b| b.is_ascii_lowercase() || b.is_ascii_digit() || b == b'-');
        if !topic_ok {
            return Ok("ERROR: topic must match ^[a-z0-9-]{1,64}$.".to_owned());
        }
        if too_long(message) {
            return Ok("ERROR: message exceeds 16 KiB.".to_owned());
        }
        let mut conn = self.connect()?;
        write(&mut conn, |c| -> Result<String, Error> {
            let author = match resolve_target(c, author)? {
                Ok(author) => author,
                Err(error) => return Ok(as_author(&error)),
            };
            let seq = c.insert_id(
                "INSERT INTO channel(author, at, topic, message) VALUES(?,?,?,?)",
                &[
                    author.as_str().into(),
                    clock::now_iso().into(),
                    topic.as_str().into(),
                    message.into(),
                ],
                "seq",
            )?;
            log_event(c, "announce", None, Some(&author), &topic)?;
            Ok(pytext::dumps(&json!({"seq": seq}))?)
        })
    }

    /// Read channel entries: a pure read from `since_seq`, or a cursor read
    /// for `agent` that advances its cursor.
    pub fn read_channel(
        &self,
        agent: Option<&str>,
        since_seq: Option<i64>,
        topic: Option<&str>,
        limit: i64,
    ) -> Result<String, Error> {
        let limit = clamp_limit(limit);
        let mut conn = self.connect()?;
        let rows = match (agent, since_seq) {
            (None, None) => return Ok("ERROR: provide either agent or since_seq.".to_owned()),
            (Some(_), None) if topic.is_some() => {
                return Ok("ERROR: topic filter is not allowed with a cursor read.".to_owned());
            }
            (_, Some(since)) => match topic {
                Some(topic) => conn.query(
                    "SELECT seq, author, at, topic, message FROM channel \
                     WHERE seq > ? AND topic = ? ORDER BY seq ASC LIMIT ?",
                    &[since.into(), topic.into(), limit.into()],
                )?,
                None => conn.query(
                    "SELECT seq, author, at, topic, message FROM channel \
                     WHERE seq > ? ORDER BY seq ASC LIMIT ?",
                    &[since.into(), limit.into()],
                )?,
            },
            (Some(agent), None) => {
                let agent = match resolve_target(&mut conn, agent)? {
                    Ok(agent) => agent,
                    Err(error) => return Ok(error),
                };
                write(&mut conn, |c| -> Result<Vec<Row>, Error> {
                    let cursor = c.query(
                        "SELECT last_seq FROM channel_cursor WHERE agent=?",
                        &[agent.as_str().into()],
                    )?;
                    let last_seq = match cursor.first() {
                        Some(row) => row.int("last_seq")?,
                        None => 0,
                    };
                    let rows = c.query(
                        "SELECT seq, author, at, topic, message FROM channel \
                         WHERE seq > ? ORDER BY seq ASC LIMIT ?",
                        &[last_seq.into(), limit.into()],
                    )?;
                    if let Some(last) = rows.last() {
                        c.execute(
                            "INSERT INTO channel_cursor(agent, last_seq) VALUES(?,?) \
                             ON CONFLICT(agent) DO UPDATE SET last_seq=excluded.last_seq",
                            &[agent.as_str().into(), last.int("seq")?.into()],
                        )?;
                    }
                    Ok(rows)
                })?
            }
        };
        Ok(pytext::dumps(&rows.iter().map(row_json).collect::<Vec<_>>())?)
    }

    /// Send a directed signal to one agent.
    pub fn notify(
        &self,
        author: &str,
        target_agent: &str,
        message: &str,
        reply_to: Option<i64>,
        wake: bool,
    ) -> Result<String, Error> {
        if strip(author).is_empty() {
            return Ok("ERROR: empty author.".to_owned());
        }
        if strip(target_agent).is_empty() {
            return Ok("ERROR: empty target agent.".to_owned());
        }
        if too_long(message) {
            return Ok("ERROR: message exceeds 16 KiB.".to_owned());
        }
        let mut conn = self.connect()?;
        write(&mut conn, |c| -> Result<String, Error> {
            let author = match resolve_target(c, author)? {
                Ok(author) => author,
                Err(error) => return Ok(as_author(&error)),
            };
            let target = match resolve_target(c, target_agent)? {
                Ok(target) => target,
                Err(error) => return Ok(error),
            };
            if let Some(id) = reply_to {
                if !is_broadcast(c, id)? {
                    return Ok(format!("ERROR: reply_to {id} is not a broadcast id."));
                }
            }
            let seq = c.insert_id(
                "INSERT INTO notifications(target, author, at, message, reply_to, wake) \
                 VALUES(?,?,?,?,?,?)",
                &[
                    target.as_str().into(),
                    author.as_str().into(),
                    clock::now_iso().into(),
                    message.into(),
                    reply_to.into(),
                    wake.into(),
                ],
                "seq",
            )?;
            let mut detail = target.clone();
            if let Some(id) = reply_to.filter(|id| *id != 0) {
                detail.push_str(&format!(" (reply to broadcast {id})"));
            }
            if wake {
                detail.push_str(" (wake)");
            }
            log_event(c, "notify", None, Some(&author), &detail)?;
            Ok(pytext::dumps(&json!({"seq": seq}))?)
        })
    }

    /// Ask or tell several agents at once: one channel entry plus one
    /// notification per recipient, in one transaction.
    pub fn broadcast(
        &self,
        author: &str,
        message: &str,
        scope: &str,
        alive_only: bool,
        wake: bool,
    ) -> Result<String, Error> {
        if strip(author).is_empty() {
            return Ok("ERROR: empty author.".to_owned());
        }
        if too_long(message) {
            return Ok("ERROR: message exceeds 16 KiB.".to_owned());
        }
        let mut conn = self.connect()?;
        let outcome = write(&mut conn, |c| -> Result<Answer<Sent>, Error> {
            let author = match resolve_target(c, author)? {
                Ok(author) => author,
                Err(error) => return Ok(Err(as_author(&error))),
            };
            let mut names = match expand_scope(c, scope)? {
                Ok(names) => names,
                Err(error) => return Ok(Err(error)),
            };
            let alive_agents = alive_set(c)?;
            if alive_only {
                names.retain(|n| alive_agents.contains(n));
                if names.is_empty() {
                    return Ok(Err("ERROR: no alive recipient in scope.".to_owned()));
                }
            }
            let id = c.insert_id(
                "INSERT INTO channel(author, at, topic, message) VALUES(?,?,?,?)",
                &[
                    author.as_str().into(),
                    clock::now_iso().into(),
                    "broadcast".into(),
                    message.into(),
                ],
                "seq",
            )?;
            let now = clock::now_iso();
            for name in &names {
                c.execute(
                    "INSERT INTO notifications(target, author, at, message, broadcast_id, wake) \
                     VALUES(?,?,?,?,?,?)",
                    &[
                        name.into(),
                        author.as_str().into(),
                        now.as_str().into(),
                        message.into(),
                        id.into(),
                        wake.into(),
                    ],
                )?;
            }
            let woken = if wake { " (wake)" } else { "" };
            log_event(
                c,
                "broadcast",
                None,
                Some(&author),
                &format!("id {id}, scope {scope} → {} recipient(s){woken}", names.len()),
            )?;
            Ok(Ok((id, names, alive_agents)))
        })?;
        let (id, names, alive_agents) = match outcome {
            Ok(done) => done,
            Err(error) => return Ok(error),
        };
        let (delivered, asleep): (Vec<String>, Vec<String>) =
            names.iter().cloned().partition(|n| alive_agents.contains(n));
        let mut answer = Map::new();
        answer.insert("broadcast_id".into(), json!(id));
        answer.insert("recipients".into(), json!(names));
        answer.insert("delivered".into(), json!(delivered));
        let to_wake = if wake { asleep.clone() } else { Vec::new() };
        answer.insert("asleep".into(), json!(asleep));
        answer.insert("to_wake".into(), json!(to_wake));
        Ok(pytext::dumps(&answer)?)
    }

    /// Answers to a broadcast, oldest first.
    pub fn replies(&self, broadcast_id: i64) -> Result<String, Error> {
        let mut conn = self.connect()?;
        if !is_broadcast(&mut conn, broadcast_id)? {
            return Ok(format!("ERROR: {broadcast_id} is not a broadcast id."));
        }
        let rows = conn.query(
            "SELECT seq, author, target, at, message FROM notifications \
             WHERE reply_to=? ORDER BY seq ASC",
            &[broadcast_id.into()],
        )?;
        Ok(pytext::dumps(&rows.iter().map(row_json).collect::<Vec<_>>())?)
    }

    /// Read an agent's own notifications and advance its cursor.
    pub fn poll(&self, agent: &str, limit: i64) -> Result<String, Error> {
        if strip(agent).is_empty() {
            return Ok("ERROR: empty agent.".to_owned());
        }
        let limit = clamp_limit(limit);
        let mut conn = self.connect()?;
        let outcome = write(&mut conn, |c| -> Result<Answer<Vec<Row>>, Error> {
            let target = match resolve_target(c, agent)? {
                Ok(target) => target,
                Err(error) => return Ok(Err(error)),
            };
            let cursor = c.query(
                "SELECT last_seq FROM notification_cursor WHERE target=?",
                &[target.as_str().into()],
            )?;
            let last_seq = match cursor.first() {
                Some(row) => row.int("last_seq")?,
                None => 0,
            };
            let rows = c.query(
                "SELECT seq, author, at, message, broadcast_id, reply_to, wake \
                 FROM notifications WHERE target=? AND seq > ? ORDER BY seq ASC LIMIT ?",
                &[target.as_str().into(), last_seq.into(), limit.into()],
            )?;
            if let Some(last) = rows.last() {
                c.execute(
                    "INSERT INTO notification_cursor(target, last_seq) VALUES(?,?) \
                     ON CONFLICT(target) DO UPDATE SET last_seq=excluded.last_seq",
                    &[target.as_str().into(), last.int("seq")?.into()],
                )?;
            }
            Ok(Ok(rows))
        })?;
        let rows = match outcome {
            Ok(rows) => rows,
            Err(error) => return Ok(error),
        };
        let mut out = Vec::new();
        for row in &rows {
            let mut entry = row_json(row);
            if let Json::Object(map) = &mut entry {
                map.insert("wake".into(), json!(row.opt_int("wake")?.unwrap_or(0) != 0));
            }
            out.push(entry);
        }
        Ok(pytext::dumps(&out)?)
    }

    /// Snapshot of what waits for an instance — read only.
    pub fn pending_signals(&self, agent: &str) -> Result<String, Error> {
        let mut conn = self.connect()?;
        let target = match resolve_target(&mut conn, agent)? {
            Ok(target) => target,
            Err(error) => return Ok(error),
        };
        Ok(pytext::dumps(&pending(&mut conn, &target)?)?)
    }

    /// Block until a task and/or an unread notification waits for the
    /// instance; the snapshot with `signalled`.
    pub fn wait_signal(
        &self,
        agent: &str,
        kinds_text: &str,
        timeout: f64,
        interval: f64,
    ) -> Result<String, Error> {
        let wanted = match kinds(kinds_text) {
            Ok(wanted) => wanted,
            Err(error) => return Ok(error),
        };
        if strip(agent).is_empty() {
            return Ok("ERROR: empty agent.".to_owned());
        }
        let interval = if interval.is_nan() { 0.2 } else { interval.max(0.2) };
        let deadline = wait_deadline(timeout);
        let mut conn = self.connect()?;
        let target = match resolve_target(&mut conn, agent)? {
            Ok(target) => target,
            Err(error) => return Ok(error),
        };
        loop {
            let snapshot = pending(&mut conn, &target)?;
            let done = signalled(&snapshot, wanted);
            if done || expired(deadline) {
                let mut answer = Map::new();
                answer.insert("signalled".into(), json!(done));
                answer.extend(snapshot);
                return Ok(pytext::dumps(&answer)?);
            }
            conn.pause(interval.min(remaining(deadline)))?;
        }
    }

    /// Global state: agents, queues, settled tasks, presence, models.
    pub fn get_system_state(&self) -> Result<String, Error> {
        let mut conn = self.connect()?;
        requeue_expired(&mut conn)?;
        let mut agents = Map::new();
        let rows = conn.query(
            "SELECT name, description, harness, workspace, cwd FROM agents \
             ORDER BY harness, workspace, name",
            &[],
        )?;
        for row in &rows {
            agents.insert(
                row.text("name")?.to_owned(),
                json!({
                    "description": row.text("description")?,
                    "harness": none_if_empty(row.text("harness")?),
                    "workspace": none_if_empty(row.text("workspace")?),
                    "cwd": none_if_empty(row.text("cwd")?),
                }),
            );
        }
        let mut queues = Map::new();
        for name in agents.keys() {
            let mut counts = Map::new();
            for status in ["queued", "claimed", "dead", "cancelled"] {
                let found = conn.query(
                    "SELECT COUNT(*) c FROM tasks WHERE agent=? AND status=?",
                    &[name.into(), status.into()],
                )?;
                let count = match found.first() {
                    Some(row) => row.int("c")?,
                    None => 0,
                };
                counts.insert(status.into(), json!(count));
            }
            queues.insert(name.clone(), Json::Object(counts));
        }
        let done = names_of(
            &conn.query("SELECT task_id FROM results ORDER BY updated_at", &[])?,
            "task_id",
        )?;
        let presence_rows = conn.query(
            "SELECT a.name, p.last_seen, p.ttl_seconds, p.model \
             FROM agents a LEFT JOIN presence p ON a.name = p.agent \
             UNION \
             SELECT p.agent AS name, p.last_seen, p.ttl_seconds, p.model \
             FROM presence p \
             WHERE p.agent NOT IN (SELECT name FROM agents)",
            &[],
        )?;
        let now = clock::now();
        let mut presence = Map::new();
        let mut models = Map::new();
        for row in &presence_rows {
            let name = row.text("name")?.to_owned();
            models.insert(
                name.clone(),
                none_if_empty(row.opt_text("model")?.unwrap_or_default()),
            );
            let status = presence_status(now, row.opt_text("last_seen")?, row.opt_int("ttl_seconds")?)?;
            presence.insert(name, json!(status));
        }
        let mut state = Map::new();
        state.insert("agents".into(), Json::Object(agents));
        state.insert("queues".into(), Json::Object(queues));
        state.insert("completed_tasks".into(), json!(done));
        state.insert("presence".into(), Json::Object(presence));
        state.insert("models".into(), Json::Object(models));
        Ok(pytext::dumps_indented(&state)?)
    }

    /// Chronological transition history, filterable by task and agent.
    pub fn get_events(
        &self,
        task_id: Option<&str>,
        agent: Option<&str>,
        limit: i64,
    ) -> Result<String, Error> {
        let mut query = "SELECT task_id, agent, event, at, detail, machine FROM events".to_owned();
        let mut conditions = Vec::new();
        let mut params: Vec<Value> = Vec::new();
        if let Some(task_id) = task_id.filter(|t| !t.is_empty()) {
            conditions.push("task_id=?");
            params.push(task_id.into());
        }
        if let Some(agent) = agent.filter(|a| !a.is_empty()) {
            conditions.push("agent=?");
            params.push(fold(agent).into());
        }
        if !conditions.is_empty() {
            query.push_str(" WHERE ");
            query.push_str(&conditions.join(" AND "));
        }
        query.push_str(" ORDER BY id DESC LIMIT ?");
        params.push(limit.into());
        let mut conn = self.connect()?;
        let rows = conn.query(&query, &params)?;
        let mut out = Vec::new();
        for row in rows.iter().rev() {
            let mut entry = row_json(row);
            if let Json::Object(map) = &mut entry {
                if map
                    .get("machine")
                    .and_then(Json::as_str)
                    .is_some_and(str::is_empty)
                {
                    map.shift_remove("machine");
                }
            }
            out.push(entry);
        }
        Ok(pytext::dumps_indented(&out)?)
    }
}

/// `db_info()` of store.py: the resolved database, how it was chosen, and
/// the project key.
pub fn db_info() -> Map<String, Json> {
    let (target, source) = identity::resolve_db();
    let mut info = Map::new();
    info.insert("db".into(), json!(identity::describe(&target)));
    info.insert("db_source".into(), json!(source));
    info.insert("project_key".into(), json!(identity::project_key()));
    info
}
