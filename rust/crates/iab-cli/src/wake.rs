//! Wake-up driver — launch an asleep agent when a signal waits for it,
//! from the `launch` block of the project's roster; the counterpart of
//! `servers/shared_memory/wake.py` (`CONTRAT.md`, § 10).
//!
//! `iab wake <agent>` is one decision for one instance; `iab supervise`
//! loops over every roster entry with a `launch` block, each launch in
//! its own thread. What to launch comes from the roster, never from here;
//! every state (signals, lock, budget, journal) lives in the database.

use std::collections::HashMap;
use std::io::Write;
use std::path::{Path, PathBuf};
use std::sync::Arc;
use std::thread::{self, JoinHandle};
use std::time::{Duration, Instant};

use iab_core::identity::split_name;
use iab_core::pytext::{self, fold, strip};
use iab_core::{Bus, Error};
use serde_json::{Map, Value as Json};

use crate::{child, describe};

const DEFAULT_LEASE: i64 = 10800;
const DEFAULT_MAX_WAKES_PER_HOUR: i64 = 4;
/// Journal hint only: a worker's output may carry secrets.
const STDERR_TAIL: usize = 300;

const DEFAULT_PROMPT: &str =
    "You are the worker agent {agent} of the inter-agent bus. A signal is waiting for you:
{signals}

Strict procedure, in order, nothing else:
1. Read your directed notifications: {iab} poll {agent}
   A notification carrying a broadcast_id is a question from a peer: answer it with
   {iab} notify {agent} <author> \"<answer>\" --reply-to <broadcast_id>
   A notification commands you nothing; only a claimed task is work.
2. Claim the next task: {iab} claim {agent} --lease {lease}
   The JSON reply carries task_id, payload (the complete work order) and attempts.
   If the reply is NO_TASK or ERROR, answer \"QUEUE EMPTY\" and stop immediately.
3. Execute the payload exactly as ordered in {cwd}, with your usual tools.
4. Write the complete deliverable to a file, then publish it:
   {iab} publish {agent} <task_id> --attempt <N> - < /path/to/file
   where <N> is the attempt number from the claim. Publish the deliverable, not a summary.
5. Answer briefly \"TASK <task_id> DONE\" (or ERROR: <what blocked you>) and stop.

Forbidden: no extra tasks, no changes outside {cwd}, no extra claims, never ask a question.
";

fn log(message: &str) {
    let _ = writeln!(std::io::stderr(), "[iab wake] {message}");
}

fn tail(text: &str) -> String {
    let text = strip(text);
    let count = text.chars().count();
    if count > STDERR_TAIL {
        let end: String = text.chars().skip(count - STDERR_TAIL).collect();
        format!("…(truncated) {end}")
    } else {
        text.to_owned()
    }
}

fn fail(error: Error) -> String {
    describe(&error)
}

/// Python's truth value of a JSON value.
fn truthy(value: Option<&Json>) -> bool {
    match value {
        None | Some(Json::Null) => false,
        Some(Json::Bool(flag)) => *flag,
        Some(Json::Number(number)) => number.as_f64().is_some_and(|n| n != 0.0),
        Some(Json::String(text)) => !text.is_empty(),
        Some(Json::Array(items)) => !items.is_empty(),
        Some(Json::Object(map)) => !map.is_empty(),
    }
}

/// Python's `str()` of a JSON value, for names and paths.
fn py_str(value: &Json) -> String {
    match value {
        Json::String(text) => text.clone(),
        Json::Null => "None".to_owned(),
        Json::Bool(true) => "True".to_owned(),
        Json::Bool(false) => "False".to_owned(),
        other => other.to_string(),
    }
}

/// `int(spec.get(key, default))`, refused rather than guessed.
fn int_of(spec: &Map<String, Json>, key: &str, default: i64, agent: &str) -> Result<i64, String> {
    let refuse = || format!("ERROR: launch.{key} of '{agent}' must be an integer.");
    match spec.get(key) {
        None => Ok(default),
        Some(Json::Bool(flag)) => Ok(i64::from(*flag)),
        Some(Json::Number(number)) => number
            .as_i64()
            .or_else(|| {
                number
                    .as_f64()
                    .filter(|n| n.is_finite())
                    .map(|n| n.trunc() as i64)
            })
            .ok_or_else(refuse),
        Some(Json::String(text)) => strip(text).parse().map_err(|_| refuse()),
        Some(_) => Err(refuse()),
    }
}

/// `~` and `~/…` to the home directory, as `os.path.expanduser`.
fn expand_user(text: &str) -> PathBuf {
    let home = || {
        std::env::var_os("HOME")
            .filter(|h| !h.is_empty())
            .or_else(|| std::env::var_os("USERPROFILE"))
    };
    if text == "~" {
        if let Some(home) = home() {
            return PathBuf::from(home);
        }
    } else if let Some(rest) = text.strip_prefix("~/").or_else(|| text.strip_prefix("~\\")) {
        if let Some(home) = home() {
            return PathBuf::from(home).join(rest);
        }
    }
    PathBuf::from(text)
}

fn roster_path() -> PathBuf {
    ["IAB_ROSTER", "ORCHESTRATOR_ROSTER"]
        .iter()
        .filter_map(|key| std::env::var(key).ok())
        .find(|value| !value.is_empty())
        .map_or_else(|| PathBuf::from("roster.json"), PathBuf::from)
}

/// The roster's entries: `{"agents": [...]}` or a bare list.
fn load_roster() -> Result<Vec<Json>, String> {
    let path = roster_path();
    let shown = path.display();
    let text =
        std::fs::read_to_string(&path).map_err(|error| format!("roster unreadable at {shown}: {error}"))?;
    let data: Json = serde_json::from_str(&text)
        .map_err(|error| format!("roster at {shown} is not valid JSON: {error}"))?;
    let agents = match data {
        Json::Object(mut map) => map.shift_remove("agents").unwrap_or(Json::Null),
        other => other,
    };
    match agents {
        Json::Array(agents) => Ok(agents),
        _ => Err(format!("roster at {shown} has no 'agents' list")),
    }
}

/// The `launch` block of an instance: its own entry first, then its
/// harness's (one launcher per client, shared by every workspace).
fn launch_spec(roster: &[Json], agent: &str) -> Option<Map<String, Json>> {
    let agent = fold(agent);
    let (harness, _) = split_name(&agent);
    let mut by_name: HashMap<String, &Map<String, Json>> = HashMap::new();
    for entry in roster {
        if let Json::Object(map) = entry {
            let name = map.get("name").map_or_else(String::new, py_str);
            by_name.insert(name.to_lowercase(), map);
        }
    }
    [agent.as_str(), harness.as_str()].iter().find_map(|key| {
        by_name.get(*key).and_then(|entry| match entry.get("launch") {
            Some(Json::Object(launch)) => Some(launch.clone()),
            _ => None,
        })
    })
}

/// The machine this supervisor runs on: the host name, without domain.
fn this_machine() -> String {
    let name = gethostname::gethostname().to_string_lossy().into_owned();
    name.split('.').next().unwrap_or("").to_lowercase()
}

fn launches_here(spec: &Map<String, Json>, host: &str) -> bool {
    let wanted = if truthy(spec.get("host")) {
        spec.get("host").map_or_else(String::new, py_str)
    } else {
        String::new()
    };
    let wanted = fold(&wanted);
    wanted.is_empty() || wanted == host
}

/// This very executable: the prompt tells the agent to call it back.
fn iab_executable() -> String {
    std::env::current_exe()
        .map(|path| path.to_string_lossy().into_owned())
        .unwrap_or_else(|_| "iab".to_owned())
}

/// `template.format_map(values)` with unknown placeholders kept as
/// written: `{{` and `}}` escape a brace; a field is replaced only when
/// it is one of the known names (no format specification is applied).
fn format_map(template: &str, values: &[(&str, String)]) -> Result<String, String> {
    let mut out = String::with_capacity(template.len());
    let mut chars = template.chars().peekable();
    while let Some(c) = chars.next() {
        match c {
            '{' if chars.peek() == Some(&'{') => {
                chars.next();
                out.push('{');
            }
            '}' if chars.peek() == Some(&'}') => {
                chars.next();
                out.push('}');
            }
            '{' => {
                let mut field = String::new();
                loop {
                    match chars.next() {
                        Some('}') => break,
                        Some('{') | None => {
                            return Err("ERROR: launch.prompt: unbalanced '{' in the template.".to_owned());
                        }
                        Some(other) => field.push(other),
                    }
                }
                let name = field.split([':', '!', '.', '[']).next().unwrap_or("");
                match values.iter().find(|(key, _)| *key == name) {
                    Some((_, value)) => out.push_str(value),
                    None => {
                        out.push('{');
                        out.push_str(&field);
                        out.push('}');
                    }
                }
            }
            '}' => return Err("ERROR: launch.prompt: single '}' in the template.".to_owned()),
            other => out.push(other),
        }
    }
    Ok(out)
}

fn render_prompt(
    spec: &Map<String, Json>,
    agent: &str,
    pending: &Map<String, Json>,
    cwd: &str,
    lease: i64,
) -> Result<String, String> {
    let source = if truthy(spec.get("prompt")) {
        spec.get("prompt").map_or_else(String::new, py_str)
    } else {
        "worker-wake".to_owned()
    };
    let template = if source == "worker-wake" {
        DEFAULT_PROMPT.to_owned()
    } else {
        let candidate = expand_user(&source);
        if candidate.is_file() {
            std::fs::read_to_string(&candidate).map_err(|error| {
                format!("ERROR: launch.prompt {} unreadable: {error}", candidate.display())
            })?
        } else {
            source
        }
    };
    let tasks: Vec<String> = pending
        .get("tasks")
        .and_then(Json::as_array)
        .map(|items| items.iter().map(py_str).collect())
        .unwrap_or_default();
    let signals = pytext::dumps(pending).map_err(|error| format!("ERROR: JSON: {error}"))?;
    format_map(
        &template,
        &[
            ("agent", agent.to_owned()),
            ("iab", iab_executable()),
            ("cwd", cwd.to_owned()),
            ("lease", lease.to_string()),
            ("task_ids", tasks.join(" ")),
            ("signals", signals),
        ],
    )
}

/// The kinds of signal that may wake the instance (`wake_on`).
fn wake_on(spec: &Map<String, Json>, agent: &str) -> Result<Vec<String>, String> {
    match spec.get("wake_on") {
        None => Ok(vec!["task".to_owned()]),
        Some(Json::Array(items)) => Ok(items.iter().map(|k| py_str(k).to_lowercase()).collect()),
        // A string is iterated character by character, as in wake.py.
        Some(Json::String(text)) => Ok(text.chars().map(|c| c.to_lowercase().collect()).collect()),
        Some(_) => Err(format!("ERROR: launch.wake_on of '{agent}' must be a list.")),
    }
}

/// `launch.after_seconds`: a number of seconds >= 0 (default 0).
fn after_seconds(spec: &Map<String, Json>, agent: &str) -> Result<f64, String> {
    match spec.get("after_seconds") {
        None | Some(Json::Null) => Ok(0.0),
        Some(Json::Number(number)) => number
            .as_f64()
            .filter(|n| n.is_finite() && *n >= 0.0)
            .ok_or_else(|| after_refused(agent)),
        Some(_) => Err(after_refused(agent)),
    }
}

fn after_refused(agent: &str) -> String {
    format!("ERROR: launch.after_seconds of '{agent}' must be a number of seconds >= 0.")
}

/// Seconds the oldest queued task has waited, `None` when unknown.
fn waited(pending: &Map<String, Json>) -> Option<f64> {
    pending
        .get("oldest_task_at")
        .and_then(Json::as_str)
        .and_then(iab_core::clock::seconds_since)
}

/// Reason to wake, or `None`: tasks always — once the oldest has waited
/// `after` seconds when the roster asks for a delay (an unknown age never
/// holds one back); notifications and broadcasts only when flagged by the
/// sender and allowed by the roster.
fn decide(wake_on: &[String], pending: &Map<String, Json>, after: f64) -> Option<String> {
    let allowed = |kind: &str| wake_on.iter().any(|k| k == kind);
    let tasks: Vec<String> = pending
        .get("tasks")
        .and_then(Json::as_array)
        .map(|items| items.iter().map(py_str).collect())
        .unwrap_or_default();
    let old_enough = after <= 0.0 || waited(pending).is_none_or(|w| w >= after);
    if !tasks.is_empty() && allowed("task") && old_enough {
        return Some(format!("{} queued task(s): {}", tasks.len(), tasks.join(" ")));
    }
    if truthy(pending.get("wake_notifications")) && allowed("notification") {
        let count = pending.get("wake_notifications").map_or_else(String::new, py_str);
        return Some(format!("{count} notification(s) asking for a wake"));
    }
    if truthy(pending.get("wake_broadcasts")) && allowed("broadcast") {
        let ids: Vec<String> = pending
            .get("wake_broadcasts")
            .and_then(Json::as_array)
            .map(|items| items.iter().map(py_str).collect())
            .unwrap_or_default();
        return Some(format!("broadcast(s) asking for a wake: [{}]", ids.join(", ")));
    }
    None
}

/// One decision for one instance: `NO_SIGNAL`, `NOT_HERE: …`, `BUSY`,
/// `REFUSED: …`, `DRY_RUN: …`, `OK: …` or `ERROR: …`.
pub fn wake_agent(
    bus: &Bus,
    agent: &str,
    roster: Option<&[Json]>,
    dry_run: bool,
    host: Option<&str>,
) -> String {
    decide_and_launch(bus, agent, roster, dry_run, host).unwrap_or_else(|text| text)
}

fn decide_and_launch(
    bus: &Bus,
    agent: &str,
    roster: Option<&[Json]>,
    dry_run: bool,
    host: Option<&str>,
) -> Result<String, String> {
    let agent = fold(agent);
    let loaded;
    let roster = match roster {
        Some(roster) => roster,
        None => {
            loaded = load_roster().map_err(|error| format!("ERROR: {error}"))?;
            &loaded
        }
    };
    let Some(spec) = launch_spec(roster, &agent) else {
        return Err(format!(
            "ERROR: no 'launch' block for '{agent}' (nor for its harness) in the roster."
        ));
    };
    let host = host.map_or_else(this_machine, str::to_owned);
    if !launches_here(&spec, &host) {
        let wanted = spec.get("host").map_or_else(String::new, py_str);
        return Ok(format!(
            "NOT_HERE: '{agent}' is launched on {wanted}, this is {host}."
        ));
    }
    let command: Vec<String> = match spec.get("command") {
        Some(Json::Array(items)) if !items.is_empty() => items
            .iter()
            .map(|item| item.as_str().map(str::to_owned))
            .collect::<Option<_>>()
            .unwrap_or_default(),
        _ => Vec::new(),
    };
    if command.is_empty() {
        return Err(format!(
            "ERROR: launch.command of '{agent}' must be a non-empty list of strings."
        ));
    }
    let after = after_seconds(&spec, &agent)?;
    let snapshot = bus.pending_signals(&agent).map_err(fail)?;
    if snapshot.starts_with("ERROR") {
        return Ok(snapshot);
    }
    let pending: Map<String, Json> =
        serde_json::from_str(&snapshot).map_err(|error| format!("ERROR: JSON: {error}"))?;
    let Some(reason) = decide(&wake_on(&spec, &agent)?, &pending, after) else {
        return Ok("NO_SIGNAL".to_owned());
    };
    let budget = int_of(&spec, "max_wakes_per_hour", DEFAULT_MAX_WAKES_PER_HOUR, &agent)?;
    let used = bus.wake_count(&agent, 3600).map_err(fail)?;
    if used >= budget {
        if dry_run {
            return Ok(format!(
                "DRY_RUN: would refuse — {used}/{budget} wakes in the last hour ({reason})"
            ));
        }
        bus.record_wake(
            &agent,
            &format!("refused: budget {used}/{budget} per hour exhausted ({reason})"),
        )
        .map_err(fail)?;
        return Ok(format!(
            "REFUSED: {used}/{budget} wakes in the last hour — loop breaker."
        ));
    }
    let cwd = if truthy(spec.get("cwd")) {
        spec.get("cwd").map_or_else(String::new, py_str)
    } else {
        std::env::current_dir()
            .map(|dir| dir.to_string_lossy().into_owned())
            .unwrap_or_default()
    };
    let cwd = expand_user(&cwd).to_string_lossy().into_owned();
    if !Path::new(&cwd).is_dir() {
        return Err(format!("ERROR: launch.cwd '{cwd}' is not a directory."));
    }
    let lease = int_of(&spec, "lease_seconds", DEFAULT_LEASE, &agent)?;
    let timeout = int_of(&spec, "timeout", 2 * lease, &agent)?;
    let prompt = render_prompt(&spec, &agent, &pending, &cwd, lease)?;
    if dry_run {
        let shown = pytext::dumps(&command).map_err(|error| format!("ERROR: JSON: {error}"))?;
        return Ok(format!(
            "DRY_RUN: would launch {} in {cwd} (reason: {reason}; timeout {timeout}s; prompt {} chars)",
            pytext::ascii_escape(&shown),
            prompt.chars().count()
        ));
    }
    let lock = bus
        .acquire_wake_lock(&agent, timeout + 60, &format!("pid {}", std::process::id()))
        .map_err(fail)?;
    if lock == "BUSY" || lock.starts_with("ERROR") {
        return Ok(lock);
    }
    let token = serde_json::from_str::<Json>(&lock)
        .ok()
        .and_then(|held| held.get("token").and_then(Json::as_str).map(str::to_owned))
        .unwrap_or_default();
    let status = launch(bus, &agent, &command, &cwd, &prompt, &reason, timeout);
    let released = bus.release_wake_lock(&agent, &token).map_err(fail);
    let status = status?;
    released?;
    Ok(status)
}

/// Launch under the held lock, journal start and end; the status line.
fn launch(
    bus: &Bus,
    agent: &str,
    command: &[String],
    cwd: &str,
    prompt: &str,
    reason: &str,
    timeout: i64,
) -> Result<String, String> {
    // The prompt file, removed when dropped, after the command ends.
    let mut prompt_file = None;
    let mut argv = command.to_vec();
    if argv.iter().any(|arg| arg.contains("{prompt_file}")) {
        let mut file = tempfile::Builder::new()
            .prefix("iab-wake-")
            .suffix(".md")
            .tempfile()
            .map_err(|error| format!("ERROR: cannot write the prompt file: {error}"))?;
        file.write_all(prompt.as_bytes())
            .and_then(|()| file.flush())
            .map_err(|error| format!("ERROR: cannot write the prompt file: {error}"))?;
        let path = file.path().to_string_lossy().into_owned();
        argv = argv
            .iter()
            .map(|arg| arg.replace("{prompt_file}", &path))
            .collect();
        prompt_file = Some(file);
    }
    let program = argv.first().cloned().unwrap_or_default();
    let program_name = Path::new(&program)
        .file_name()
        .map(|name| name.to_string_lossy().into_owned())
        .unwrap_or_default();
    // The journal is shared and long-lived: the decision, the program name
    // and the directory — never the argv, the prompt or the output.
    bus.record_wake(agent, &format!("start: {reason}; {program_name} in {cwd}"))
        .map_err(fail)?;
    let started = Instant::now();
    let mut running = match child::start(&argv, Some(Path::new(cwd)), prompt.as_bytes().to_vec()) {
        Ok(running) => running,
        Err(error) => {
            bus.record_wake(agent, &format!("end: launch failed ({error})"))
                .map_err(fail)?;
            return Ok(format!("ERROR: cannot launch {program}: {error}"));
        }
    };
    let limit = Duration::from_secs(u64::try_from(timeout).unwrap_or(0));
    let finished = match running.wait_for(limit) {
        Ok(Some(status)) => running.finish(status),
        Ok(None) | Err(_) => running.kill(),
    };
    drop(prompt_file);
    let elapsed = started.elapsed().as_secs();
    let (detail, status) = match finished.code {
        None => (
            format!("end: killed after timeout {timeout}s"),
            format!("ERROR: worker killed after {timeout}s (task lease will expire, bus re-offers)"),
        ),
        Some(code) if code != 0 => (
            format!("end: exit {code} after {elapsed}s"),
            format!("ERROR: worker exited {code} after {elapsed}s"),
        ),
        Some(_) => (
            format!("end: exit 0 after {elapsed}s"),
            format!("OK: worker finished in {elapsed}s"),
        ),
    };
    let recorded = bus.record_wake(agent, &detail).map_err(fail);
    if finished.code != Some(0) {
        // Diagnosis to the supervisor's stderr, trimmed — not the journal.
        log(&format!(
            "{agent}: {status}; stderr tail: {}",
            tail(&finished.err)
        ));
    }
    recorded?;
    Ok(status)
}

/// `{interval:g}` of Python for the intervals one gives.
fn shown_seconds(value: f64) -> String {
    if value.fract() == 0.0 && value.abs() < 1e16 {
        format!("{}", value as i64)
    } else {
        format!("{value}")
    }
}

/// The single watcher: every `interval` seconds, for each roster entry
/// launched on this machine whose previous launch has ended, decide and
/// launch in a thread. Nothing is lost if it dies: signals wait in the
/// database.
pub fn supervise(bus: &Bus, interval: f64, once: bool, dry_run: bool, host: Option<&str>) -> u8 {
    let roster = match load_roster() {
        Ok(roster) => Arc::new(roster),
        Err(error) => {
            log(&format!("ERROR: {error}"));
            return 1;
        }
    };
    let host = host.map_or_else(this_machine, str::to_owned);
    let launchable: Vec<&Map<String, Json>> = roster
        .iter()
        .filter_map(Json::as_object)
        .filter(|entry| matches!(entry.get("launch"), Some(Json::Object(_))) && truthy(entry.get("name")))
        .collect();
    if launchable.is_empty() {
        log(&format!(
            "ERROR: no roster entry with a 'launch' block in {}",
            roster_path().display()
        ));
        return 1;
    }
    let names: Vec<String> = launchable
        .iter()
        .filter(|entry| match entry.get("launch") {
            Some(Json::Object(launch)) => launches_here(launch, &host),
            _ => false,
        })
        .map(|entry| entry.get("name").map_or_else(String::new, py_str).to_lowercase())
        .collect();
    if names.is_empty() {
        log(&format!(
            "no roster entry is launched on {host} — nothing to supervise here"
        ));
        return 0;
    }
    log(&format!(
        "supervising {} on {host} (interval {}s)",
        names.join(", "),
        shown_seconds(interval)
    ));
    let host = Arc::new(host);
    let mut running: HashMap<String, JoinHandle<()>> = HashMap::new();
    loop {
        for name in &names {
            if running.get(name).is_some_and(|handle| !handle.is_finished()) {
                continue;
            }
            let (bus, roster, host, agent) =
                (bus.clone(), Arc::clone(&roster), Arc::clone(&host), name.clone());
            let spawned = thread::Builder::new().spawn(move || {
                let result = wake_agent(&bus, &agent, Some(&roster), dry_run, Some(&host));
                if result != "NO_SIGNAL" {
                    log(&format!("{agent}: {result}"));
                }
            });
            match spawned {
                Ok(handle) => {
                    running.insert(name.clone(), handle);
                }
                Err(error) => log(&format!("{name}: ERROR: cannot start a thread: {error}")),
            }
        }
        if once {
            for (_, handle) in running.drain() {
                let _ = handle.join();
            }
            return 0;
        }
        let pause = if interval.is_finite() {
            interval.max(1.0)
        } else {
            60.0
        };
        thread::sleep(Duration::from_secs_f64(pause));
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use serde_json::json;

    fn map(value: Json) -> Map<String, Json> {
        match value {
            Json::Object(map) => map,
            _ => Map::new(),
        }
    }

    #[test]
    fn format_map_should_keep_unknown_placeholders() {
        let out = format_map("{agent} {other} {{x}}", &[("agent", "a@w".to_owned())]);
        assert_eq!(out.as_deref(), Ok("a@w {other} {x}"));
    }

    #[test]
    fn format_map_should_refuse_an_unbalanced_brace() {
        assert!(format_map("oops {agent", &[]).is_err());
    }

    #[test]
    fn decide_should_hold_a_fresh_task_back_for_after_seconds() {
        let fresh = iab_core::clock::now_iso();
        let pending = map(json!({"tasks": ["g"], "oldest_task_at": fresh}));
        assert_eq!(decide(&["task".to_owned()], &pending, 600.0), None);
    }

    #[test]
    fn decide_should_wake_once_the_task_waited_long_enough() {
        let pending = map(json!({"tasks": ["g"], "oldest_task_at": "2020-01-01T00:00:00+00:00"}));
        assert!(decide(&["task".to_owned()], &pending, 600.0).is_some());
    }

    #[test]
    fn decide_should_not_hold_back_a_task_of_unknown_age() {
        let pending = map(json!({"tasks": ["g"]}));
        assert!(decide(&["task".to_owned()], &pending, 600.0).is_some());
    }

    #[test]
    fn after_seconds_should_refuse_a_text() {
        let spec = map(json!({"after_seconds": "ten"}));
        assert!(after_seconds(&spec, "odin@general").is_err());
    }

    #[test]
    fn decide_should_wake_on_a_task_by_default() {
        let pending = map(json!({"tasks": ["t1", "t2"], "wake_notifications": 0, "wake_broadcasts": []}));
        assert_eq!(
            decide(&["task".to_owned()], &pending, 0.0).as_deref(),
            Some("2 queued task(s): t1 t2")
        );
    }

    #[test]
    fn decide_should_ignore_a_flagged_notification_unless_allowed() {
        let pending = map(json!({"tasks": [], "wake_notifications": 1, "wake_broadcasts": []}));
        assert_eq!(decide(&["task".to_owned()], &pending, 0.0), None);
    }

    #[test]
    fn decide_should_list_flagged_broadcasts_as_python_does() {
        let pending = map(json!({"tasks": [], "wake_notifications": 0, "wake_broadcasts": [3, 5]}));
        assert_eq!(
            decide(&["broadcast".to_owned()], &pending, 0.0).as_deref(),
            Some("broadcast(s) asking for a wake: [3, 5]")
        );
    }

    #[test]
    fn launch_spec_should_fall_back_on_the_harness_entry() {
        let roster = vec![json!({"name": "codex", "launch": {"command": ["codex"]}})];
        assert!(launch_spec(&roster, "codex@homelab").is_some());
    }

    #[test]
    fn launches_here_should_accept_an_entry_without_host() {
        assert!(launches_here(&map(json!({"command": ["x"]})), "odin"));
    }

    #[test]
    fn int_of_should_truncate_a_float_as_int_does() {
        assert_eq!(int_of(&map(json!({"timeout": 12.9})), "timeout", 0, "a"), Ok(12));
    }

    #[test]
    fn shown_seconds_should_print_like_python_g() {
        assert_eq!(
            (shown_seconds(60.0), shown_seconds(0.5)),
            ("60".to_owned(), "0.5".to_owned())
        );
    }
}
