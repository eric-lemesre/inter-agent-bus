//! The tools: their announced schemas, the reading of their arguments and
//! the bus operation behind each one. No state logic here — every tool is
//! one call of the core, answered with its text (`OK: …`, JSON, `NO_TASK`
//! or an `ERROR: …` refusal).

use std::collections::HashMap;
use std::sync::OnceLock;

use iab_core::identity::{self, Identity};
use iab_core::{Bus, Error, db_info, pytext};
use serde_json::{Map, Value as Json, json};

use crate::spec::{Fallback, Kind, Param, Spec, TOOLS};

/// The caller's MCP client, as it introduced itself at the handshake.
#[derive(Debug, Clone)]
pub struct Client {
    /// `clientInfo.name`.
    pub name: String,
    /// `clientInfo.title`.
    pub title: Option<String>,
    /// `clientInfo.version`.
    pub version: String,
}

/// `inputSchema` of a tool, shaped as the reference's (pydantic) one.
pub fn input_schema(spec: &Spec) -> Map<String, Json> {
    let mut properties = Map::new();
    let mut required = Vec::new();
    for param in spec.params {
        let mut property = Map::new();
        match param.default {
            Some(fallback) => {
                property.insert("default".into(), fallback_json(fallback));
            }
            None => required.push(json!(param.name)),
        }
        property.insert("title".into(), json!(title(param.name)));
        property.insert("type".into(), json!(type_name(param.kind)));
        properties.insert(param.name.into(), Json::Object(property));
    }
    let mut schema = Map::new();
    schema.insert("properties".into(), Json::Object(properties));
    if !required.is_empty() {
        schema.insert("required".into(), Json::Array(required));
    }
    schema.insert("type".into(), json!("object"));
    schema.insert("title".into(), json!(format!("{}Arguments", spec.name)));
    schema
}

/// `outputSchema` of a tool: every answer is one text, `result`.
pub fn output_schema(spec: &Spec) -> Map<String, Json> {
    let mut schema = Map::new();
    schema.insert(
        "properties".into(),
        json!({"result": {"title": "Result", "type": "string"}}),
    );
    schema.insert("required".into(), json!(["result"]));
    schema.insert("type".into(), json!("object"));
    schema.insert("title".into(), json!(format!("{}Output", spec.name)));
    schema
}

/// pydantic's field title: `lease_seconds` → `Lease Seconds`.
fn title(name: &str) -> String {
    name.split('_')
        .map(|word| {
            let mut chars = word.chars();
            match chars.next() {
                Some(first) => first.to_uppercase().chain(chars).collect::<String>(),
                None => String::new(),
            }
        })
        .collect::<Vec<_>>()
        .join(" ")
}

fn type_name(kind: Kind) -> &'static str {
    match kind {
        Kind::Str => "string",
        Kind::Int => "integer",
        Kind::Float => "number",
        Kind::Bool => "boolean",
    }
}

fn fallback_json(fallback: Fallback) -> Json {
    match fallback {
        Fallback::Str(text) => json!(text),
        Fallback::Int(value) => json!(value),
        Fallback::Float(value) => json!(value),
        Fallback::Bool(value) => json!(value),
    }
}

/// One argument once read.
#[derive(Debug, Clone)]
enum Arg {
    Str(String),
    Int(i64),
    Float(f64),
    Bool(bool),
}

impl From<Fallback> for Arg {
    fn from(fallback: Fallback) -> Self {
        match fallback {
            Fallback::Str(text) => Self::Str(text.to_owned()),
            Fallback::Int(value) => Self::Int(value),
            Fallback::Float(value) => Self::Float(value),
            Fallback::Bool(value) => Self::Bool(value),
        }
    }
}

/// The arguments of one call, every parameter present (given or default).
#[derive(Debug, Default)]
struct Args(HashMap<&'static str, Arg>);

// Read only after `read_args` has checked every parameter against the
// spec: the neutral values below are never reached.
impl Args {
    fn text(&self, name: &str) -> &str {
        match self.0.get(name) {
            Some(Arg::Str(text)) => text,
            _ => "",
        }
    }

    /// A text the reference passes on as `text or None`.
    fn opt_text(&self, name: &str) -> Option<&str> {
        Some(self.text(name)).filter(|text| !text.is_empty())
    }

    fn int(&self, name: &str) -> i64 {
        match self.0.get(name) {
            Some(Arg::Int(value)) => *value,
            _ => 0,
        }
    }

    fn float(&self, name: &str) -> f64 {
        match self.0.get(name) {
            Some(Arg::Float(value)) => *value,
            _ => 0.0,
        }
    }

    fn flag(&self, name: &str) -> bool {
        matches!(self.0.get(name), Some(Arg::Bool(true)))
    }
}

/// pydantic's lax conversions, the ones a model's call actually exercises:
/// `"5"` and `5.0` for an integer, `"yes"` or `1` for a flag; never a
/// number for a text.
fn convert(kind: Kind, value: &Json) -> Result<Arg, &'static str> {
    match kind {
        Kind::Str => value
            .as_str()
            .map(|text| Arg::Str(text.to_owned()))
            .ok_or("Input should be a valid string"),
        Kind::Int => match value {
            Json::Bool(flag) => Ok(Arg::Int(i64::from(*flag))),
            Json::Number(number) => match number.as_i64() {
                Some(int) => Ok(Arg::Int(int)),
                None => match number.as_f64() {
                    Some(float) if float.fract() != 0.0 => {
                        Err("Input should be a valid integer, got a number with a fractional part")
                    }
                    Some(float) if float.abs() < 9.2e18 => Ok(Arg::Int(float as i64)),
                    _ => Err("Input should be a valid integer"),
                },
            },
            Json::String(text) => strip_int(text)
                .map(Arg::Int)
                .ok_or("Input should be a valid integer, unable to parse string as an integer"),
            _ => Err("Input should be a valid integer"),
        },
        Kind::Float => match value {
            Json::Bool(flag) => Ok(Arg::Float(f64::from(u8::from(*flag)))),
            Json::Number(number) => number
                .as_f64()
                .map(Arg::Float)
                .ok_or("Input should be a valid number"),
            Json::String(text) => pytext::strip(text)
                .parse::<f64>()
                .map(Arg::Float)
                .map_err(|_| "Input should be a valid number, unable to parse string as a number"),
            _ => Err("Input should be a valid number"),
        },
        Kind::Bool => {
            let flag = match value {
                Json::Bool(flag) => Some(*flag),
                Json::Number(number) => match number.as_f64() {
                    Some(0.0) => Some(false),
                    Some(1.0) => Some(true),
                    _ => None,
                },
                Json::String(text) => match pytext::fold(text).as_str() {
                    "0" | "off" | "f" | "false" | "n" | "no" => Some(false),
                    "1" | "on" | "t" | "true" | "y" | "yes" => Some(true),
                    _ => None,
                },
                _ => None,
            };
            flag.map(Arg::Bool).ok_or("Input should be a valid boolean")
        }
    }
}

fn strip_int(text: &str) -> Option<i64> {
    pytext::strip(text).parse::<i64>().ok()
}

/// Every parameter read, or the list of what is wrong, worded as the
/// reference's validation error. Unknown arguments are ignored, as there.
fn read_args(spec: &Spec, given: &Map<String, Json>) -> Result<Args, String> {
    let mut args = Args::default();
    let mut errors = Vec::new();
    for Param { name, kind, default } in spec.params {
        match (given.get(*name), default) {
            (Some(value), _) => match convert(*kind, value) {
                Ok(arg) => {
                    args.0.insert(name, arg);
                }
                Err(message) => errors.push(format!("{name}\n  {message}")),
            },
            (None, Some(fallback)) => {
                args.0.insert(name, Arg::from(*fallback));
            }
            (None, None) => errors.push(format!("{name}\n  Field required")),
        }
    }
    if errors.is_empty() {
        return Ok(args);
    }
    let plural = if errors.len() == 1 { "" } else { "s" };
    Err(format!(
        "{} validation error{plural} for {}Arguments\n{}",
        errors.len(),
        spec.name,
        errors.join("\n")
    ))
}

/// The identity of this server's client, resolved once: the launch
/// directory and environment of a stdio server never change.
fn identity() -> &'static Identity {
    static IDENTITY: OnceLock<Identity> = OnceLock::new();
    IDENTITY.get_or_init(identity::resolve_identity)
}

/// The acting instance on a shared queue: as given, else this client.
fn acting(by: &str) -> &str {
    if by.is_empty() {
        identity().agent_name.as_deref().unwrap_or("")
    } else {
        by
    }
}

/// The wake-up instructions of this server (`session_instructions`),
/// from its environment and the `iab` command next to it.
pub fn instructions() -> Option<String> {
    let name = if cfg!(windows) { "iab.exe" } else { "iab" };
    let iab = std::env::current_exe()
        .ok()
        .and_then(|exe| exe.parent().map(|dir| dir.join(name)))
        .filter(|path| path.exists())
        .map_or_else(|| "iab".to_owned(), |path| path.display().to_string());
    iab_core::instructions::session_instructions(
        identity().agent_name.as_deref(),
        |var| std::env::var(var).ok(),
        &iab,
    )
}

/// Register on first sight and refresh the caller's presence when the
/// environment names the harness. Transport-only: a failure never stops
/// the tool call.
pub fn touch() {
    let identity = identity();
    if identity.agent_name.is_none() {
        return;
    }
    if let Ok(bus) = Bus::from_env() {
        // Swallowed on purpose, as server.py does: the presence piggyback
        // must not turn a working call into a failed one.
        let _ = bus.touch_identity(identity);
    }
}

/// `whoami`: the identity, or the handshake's clientInfo when the
/// environment names no harness, and the resolved database.
fn whoami(client: Option<&Client>) -> Result<String, Error> {
    let identity = identity();
    let mut answer = match serde_json::to_value(identity)? {
        Json::Object(map) => map,
        _ => Map::new(),
    };
    if identity.agent_name.is_none()
        && let Some(client) = client
    {
        answer.insert("source".into(), json!("client_info"));
        answer.insert("name".into(), json!(client.name));
        answer.insert("title".into(), json!(client.title));
        answer.insert("version".into(), json!(client.version));
    }
    answer.extend(db_info());
    // server.py answers with json.dumps defaults: ASCII only.
    Ok(pytext::ascii_escape(&pytext::dumps(&answer)?))
}

fn operation(name: &str, args: &Args, client: Option<&Client>) -> Result<String, Error> {
    if name == "whoami" {
        return whoami(client);
    }
    let bus = Bus::from_env()?;
    let a = args;
    match name {
        "register_agent" => bus.register_agent(
            a.text("name"),
            a.text("description"),
            a.text("cwd"),
            a.opt_text("model"),
        ),
        "rename_agent" => bus.rename_agent(a.text("old"), a.text("new")),
        "push_task" => bus.push_task(
            a.text("target_agent"),
            a.text("task_id"),
            a.text("payload"),
            a.int("priority"),
            a.int("max_attempts"),
            a.flag("notify"),
        ),
        "claim_task" => bus.claim_task(
            a.text("agent_name"),
            a.int("lease_seconds"),
            a.opt_text("task_id"),
            acting(a.text("by")),
        ),
        "decline_task" => bus.decline_task(
            a.text("agent_name"),
            a.text("task_id"),
            a.text("reason"),
            Some(a.int("attempt")).filter(|n| *n != 0),
            acting(a.text("by")),
        ),
        "publish_result" => bus.publish_result(
            a.text("agent_name"),
            a.text("task_id"),
            a.text("result_content"),
            Some(a.int("attempt")).filter(|n| *n != 0),
            a.flag("force"),
        ),
        "cancel_task" => bus.cancel_task(a.text("task_id")),
        "requeue_task" => bus.requeue_task(a.text("task_id")),
        "extend_lease" => bus.extend_lease(a.text("task_id"), a.int("lease_seconds")),
        "read_result" => bus.read_result(a.text("task_id")),
        "get_system_state" => bus.get_system_state(),
        "get_events" => bus.get_events(a.opt_text("task_id"), a.opt_text("agent"), a.int("limit")),
        "heartbeat" => bus.heartbeat(
            a.text("agent"),
            a.int("ttl_seconds"),
            a.opt_text("capabilities"),
            a.opt_text("model"),
        ),
        "list_presence" => bus.list_presence(),
        "announce" => bus.announce(a.text("author"), a.text("topic"), a.text("message")),
        // since_seq -1 is server.py's "not given": None reads from the
        // agent's cursor, 0 is a pure read from the beginning.
        "read_channel" => bus.read_channel(
            a.opt_text("agent"),
            Some(a.int("since_seq")).filter(|n| *n >= 0),
            a.opt_text("topic"),
            a.int("limit"),
        ),
        "notify" => bus.notify(
            a.text("author"),
            a.text("target_agent"),
            a.text("message"),
            Some(a.int("reply_to")).filter(|n| *n != 0),
            a.flag("wake"),
        ),
        "broadcast" => bus.broadcast(
            a.text("author"),
            a.text("message"),
            a.text("scope"),
            a.flag("alive_only"),
            a.flag("wake"),
        ),
        "replies" => bus.replies(a.int("broadcast_id")),
        "poll" => bus.poll(a.text("agent"), a.int("limit")),
        "wait_signal" => bus.wait_signal(
            a.text("agent"),
            a.text("kinds"),
            a.float("timeout_seconds"),
            a.float("interval_seconds"),
            a.flag("new"),
            a.text("since"),
        ),
        "pending_signals" => bus.pending_signals(a.text("agent")),
        "wait_task" => bus.wait_for_task(
            a.text("agent"),
            a.float("timeout_seconds"),
            a.float("interval_seconds"),
        ),
        _ => Err(Error::Unsupported(format!("Unknown tool: {name}"))),
    }
}

/// The text of a failure with its causes: "error performing TLS
/// handshake" alone does not say which certificate was refused.
fn describe(error: &Error) -> String {
    let mut text = error.to_string();
    let mut source = std::error::Error::source(error);
    while let Some(cause) = source {
        let cause_text = cause.to_string();
        if !text.contains(&cause_text) {
            text.push_str(": ");
            text.push_str(&cause_text);
        }
        source = cause.source();
    }
    text
}

/// One tool call, blocking: the answer's text, or the text of a tool
/// error (unknown tool, invalid arguments, failure of the bus).
pub fn call(name: &str, given: &Map<String, Json>, client: Option<&Client>) -> Result<String, String> {
    let Some(spec) = TOOLS.iter().find(|spec| spec.name == name) else {
        return Err(format!("Unknown tool: {name}"));
    };
    let args = read_args(spec, given).map_err(|text| format!("Error executing tool {name}: {text}"))?;
    touch();
    operation(name, &args, client)
        .map_err(|error| format!("Error executing tool {name}: {}", describe(&error)))
}

#[cfg(test)]
mod tests {
    use super::*;

    fn spec(name: &str) -> &'static Spec {
        TOOLS.iter().find(|s| s.name == name).unwrap()
    }

    #[test]
    fn twenty_three_tools_with_unique_names() {
        let mut names: Vec<_> = TOOLS.iter().map(|s| s.name).collect();
        names.sort_unstable();
        names.dedup();
        assert_eq!(names.len(), 24);
    }

    #[test]
    fn title_follows_pydantic() {
        assert_eq!(title("lease_seconds"), "Lease Seconds");
    }

    #[test]
    fn schema_without_parameters_has_no_required_key() {
        assert!(!input_schema(spec("whoami")).contains_key("required"));
    }

    #[test]
    fn integer_accepts_numeric_text_and_whole_float() {
        let given = json!({"agent_name": "a", "lease_seconds": " 42 "});
        let args = read_args(spec("claim_task"), given.as_object().unwrap()).unwrap();
        assert_eq!(args.int("lease_seconds"), 42);
        let given = json!({"agent_name": "a", "lease_seconds": 7.0});
        let args = read_args(spec("claim_task"), given.as_object().unwrap()).unwrap();
        assert_eq!(args.int("lease_seconds"), 7);
    }

    #[test]
    fn integer_refuses_a_fraction() {
        let given = json!({"agent_name": "a", "lease_seconds": 1.5});
        assert!(read_args(spec("claim_task"), given.as_object().unwrap()).is_err());
    }

    #[test]
    fn text_refuses_a_number() {
        let given = json!({"name": 5});
        let error = read_args(spec("register_agent"), given.as_object().unwrap()).unwrap_err();
        assert!(error.contains("Input should be a valid string"));
    }

    #[test]
    fn missing_required_parameters_are_all_listed() {
        let error = read_args(spec("push_task"), &Map::new()).unwrap_err();
        assert!(error.starts_with("3 validation errors for push_taskArguments"));
    }

    #[test]
    fn flag_reads_pydantic_words() {
        let given = json!({"target_agent": "a", "task_id": "t", "payload": "p", "notify": "No"});
        let args = read_args(spec("push_task"), given.as_object().unwrap()).unwrap();
        assert!(!args.flag("notify"));
    }

    #[test]
    fn defaults_fill_absent_parameters() {
        let given = json!({"agent": "a"});
        let args = read_args(spec("wait_signal"), given.as_object().unwrap()).unwrap();
        assert_eq!(args.text("kinds"), "any");
        assert!((args.float("timeout_seconds") - 300.0).abs() < f64::EPSILON);
    }

    #[test]
    fn unknown_tool_is_a_tool_error() {
        assert_eq!(call("nope", &Map::new(), None).unwrap_err(), "Unknown tool: nope");
    }
}
