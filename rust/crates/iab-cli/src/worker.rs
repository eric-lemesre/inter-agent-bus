//! Headless worker driver — claim → run → publish, the counterpart of
//! `servers/shared_memory/worker.py` (`CONTRAT.md`, § 10).
//!
//! The payload goes to the command on stdin (never on the command line:
//! quoting differs per platform, argv is size-limited, payloads are
//! untrusted), the lease is extended while the command runs, stdout is
//! published with the claim's attempt — or an `ERROR:` result on a
//! non-zero exit, an empty output or the task timeout.

use std::io::Write;
use std::thread;
use std::time::{Duration, Instant};

use iab_core::Bus;
use iab_core::pytext::{fold, strip};
use serde_json::Value as Json;

use crate::{answer, child};

const STDERR_TAIL: usize = 2000;
const MAX_POLL_SECONDS: i64 = 60;

/// What `iab worker` was asked to do.
pub struct Options {
    pub agent: String,
    pub command: Vec<String>,
    pub once: bool,
    pub lease_seconds: i64,
    pub poll_seconds: i64,
    pub task_timeout: Option<i64>,
    pub model: Option<String>,
}

fn log(message: &str) {
    // A closed stderr must not stop the loop.
    let _ = writeln!(std::io::stderr(), "[iab worker] {message}");
}

fn tail(text: &str) -> String {
    let text = strip(text);
    let count = text.chars().count();
    if count > STDERR_TAIL {
        let end: String = text.chars().skip(count - STDERR_TAIL).collect();
        format!("…(truncated to last {STDERR_TAIL} chars) {end}")
    } else {
        text.to_owned()
    }
}

fn seconds(value: i64) -> Duration {
    Duration::from_secs(u64::try_from(value).unwrap_or(0))
}

/// Run the command, payload on stdin, extending the task's lease at every
/// heartbeat (half the lease). `code` is `None` when killed by the task
/// timeout, which is checked at those heartbeats, as worker.py does.
fn run_command(
    bus: &Bus,
    options: &Options,
    payload: &str,
    task_id: &str,
) -> std::io::Result<child::Finished> {
    let heartbeat = seconds(options.lease_seconds.div_euclid(2).max(1));
    let deadline = options
        .task_timeout
        .filter(|t| *t != 0)
        .map(|t| (Instant::now(), t));
    let mut running = child::start(&options.command, None, payload.as_bytes().to_vec())?;
    loop {
        if let Some(status) = running.wait_for(heartbeat)? {
            return Ok(running.finish(status));
        }
        if let Some((started, limit)) = deadline {
            if started.elapsed().as_secs_f64() > limit as f64 {
                return Ok(running.kill());
            }
        }
        let renewed = answer(bus.extend_lease(task_id, options.lease_seconds));
        if renewed.starts_with("ERROR") {
            log(&renewed);
        } else {
            log(&format!("heartbeat: lease of '{task_id}' extended"));
        }
    }
}

/// The channel entries the worker has not seen yet, to its log.
fn show_channel(bus: &Bus, agent: &str) {
    let channel = answer(bus.read_channel(Some(agent), None, None, 100));
    if channel.starts_with("ERROR") {
        log(&format!("channel read failed: {channel}"));
        return;
    }
    match serde_json::from_str::<Vec<Json>>(&channel) {
        Ok(entries) => {
            for entry in entries {
                let field = |key: &str| entry.get(key).and_then(Json::as_str).unwrap_or("").to_owned();
                log(&format!(
                    "channel [{}] from {}: {}",
                    field("topic"),
                    field("author"),
                    field("message")
                ));
            }
        }
        Err(error) => log(&format!("channel read failed: {error}")),
    }
}

/// The loop; the process exit status.
pub fn run(bus: &Bus, options: &Options) -> u8 {
    let agent = fold(&options.agent);
    let model = options.model.as_deref();
    let cwd = std::env::current_dir()
        .map(|dir| dir.to_string_lossy().into_owned())
        .unwrap_or_default();
    let registered = answer(bus.register_agent(&agent, "headless worker (iab worker)", &cwd, model));
    log(&registered);
    if registered.starts_with("ERROR") {
        return 1;
    }
    let mut delay = options.poll_seconds;
    loop {
        log(&answer(bus.heartbeat(
            &agent,
            2 * options.poll_seconds + 60,
            None,
            model,
        )));
        let claimed = answer(bus.claim_task(&agent, options.lease_seconds, None));
        if claimed == "NO_TASK" || claimed.starts_with("ERROR") {
            if claimed != "NO_TASK" {
                log(&claimed);
            }
            if options.once {
                if claimed == "NO_TASK" {
                    log("queue drained — exiting (--once)");
                    return 0;
                }
                return 1;
            }
            log(&format!("queue empty — next poll in {delay}s"));
            thread::sleep(seconds(delay));
            delay = (delay * 2).min(MAX_POLL_SECONDS);
            continue;
        }
        delay = options.poll_seconds;
        let task: Json = match serde_json::from_str(&claimed) {
            Ok(task) => task,
            Err(error) => {
                log(&format!("unreadable claim ({error}): {claimed}"));
                return 1;
            }
        };
        let task_id = task
            .get("task_id")
            .and_then(Json::as_str)
            .unwrap_or("")
            .to_owned();
        let attempt = task.get("attempts").and_then(Json::as_i64);
        let payload = task.get("payload").and_then(Json::as_str).unwrap_or("");
        log(&format!(
            "claimed '{task_id}' (attempt {})",
            attempt.map_or_else(|| "None".to_owned(), |a| a.to_string())
        ));
        let finished = run_command(bus, options, payload, &task_id).unwrap_or_else(|error| {
            // Command not found, not executable, …
            child::Finished {
                code: Some(127),
                out: String::new(),
                err: error.to_string(),
            }
        });
        let result = match finished.code {
            None => format!(
                "ERROR: worker command killed after task timeout {}s. stderr: {}",
                options.task_timeout.unwrap_or(0),
                tail(&finished.err)
            ),
            Some(code) if code != 0 => format!(
                "ERROR: worker command failed (exit {code}). stderr: {}",
                tail(&finished.err)
            ),
            Some(_) if strip(&finished.out).is_empty() => format!(
                "ERROR: worker command produced no output (exit 0). stderr: {}",
                tail(&finished.err)
            ),
            Some(_) => finished.out,
        };
        let published = answer(bus.publish_result(&agent, &task_id, &result, attempt, false));
        log(&published);
        show_channel(bus, &agent);
        if options.once {
            return u8::from(!published.starts_with("OK") || result.starts_with("ERROR"));
        }
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn tail_should_keep_a_short_text_stripped() {
        assert_eq!(tail("  boom \n"), "boom");
    }

    #[test]
    fn tail_should_keep_the_last_characters_of_a_long_text() {
        let long = format!("{}é", "x".repeat(STDERR_TAIL));
        let shown = tail(&long);
        assert!(shown.starts_with("…(truncated to last 2000 chars) x") && shown.ends_with('é'));
    }
}
