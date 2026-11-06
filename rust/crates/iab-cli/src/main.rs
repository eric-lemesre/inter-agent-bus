//! Native `iab` command of the inter-agent bus: the same subcommands,
//! options and answers as `servers/shared_memory/cli.py`, on the same
//! database (`CONTRAT.md`, § 8). No state logic here.
//!
//! Payloads, results and messages absent from the command line, or given
//! as `-`, are read from standard input, in UTF-8, with no line-ending
//! translation (`CONTRAT.md`, § 9).

mod child;
mod wake;
mod worker;

use std::io::{Read, Write};
use std::process::ExitCode;

use clap::{ArgGroup, CommandFactory, Parser, Subcommand, ValueEnum};
use iab_core::{Bus, Error, db_info, identity, pytext};

#[derive(Parser)]
#[command(
    name = "iab",
    version,
    infer_long_args = true,
    about = "Inter-agent bus: task queues under lease and a shared result store over one \
             SQLite database (IAB_DB)."
)]
struct Cli {
    /// pretty-print JSON output when applicable
    #[arg(long)]
    json: bool,
    #[command(subcommand)]
    command: Command,
}

#[derive(Clone, Copy, ValueEnum)]
enum On {
    Task,
    Notification,
    Any,
}

#[derive(Subcommand)]
enum Command {
    /// declare an agent instance `harness[@workspace]` (creates its queue)
    Register {
        name: String,
        #[arg(short, long, default_value = "")]
        description: String,
        /// directory the instance works in
        #[arg(long, default_value = "")]
        cwd: String,
        /// model carrying the instance right now
        #[arg(long)]
        model: Option<String>,
    },
    /// move an identity to a new name (queue, results, notifications,
    /// cursors, presence, journal follow)
    Rename { old: String, new: String },
    /// queue a task for a registered agent
    Push {
        agent: String,
        task_id: String,
        /// omit or use '-' to read from stdin
        payload: Option<String>,
        #[arg(short, long, default_value_t = 1, allow_negative_numbers = true)]
        priority: i64,
        /// expired leases before dead-lettering (default: 3)
        #[arg(long, default_value_t = 3, allow_negative_numbers = true)]
        max_attempts: i64,
        /// do not drop a wake-up notification in the target's mailbox
        #[arg(long)]
        no_notify: bool,
    },
    /// claim the next task of an agent's queue, under lease
    Claim {
        agent: String,
        #[arg(
            long,
            default_value_t = 900,
            value_name = "SECONDS",
            allow_negative_numbers = true
        )]
        lease: i64,
        /// claim this specific queued task of one's own queue
        #[arg(long)]
        task_id: Option<String>,
    },
    /// publish a task's result (settles the task)
    Publish {
        agent: String,
        task_id: String,
        /// omit or use '-' to read from stdin
        content: Option<String>,
        /// attempt number received at claim (lease fencing)
        #[arg(long, allow_negative_numbers = true)]
        attempt: Option<i64>,
        /// orchestrator override of ownership and fencing checks
        #[arg(long)]
        force: bool,
    },
    /// cancel a task that is not settled yet
    Cancel { task_id: String },
    /// re-offer a claimed, dead or cancelled task
    Requeue { task_id: String },
    /// renew the lease of a claimed task
    Extend {
        task_id: String,
        #[arg(
            long,
            default_value_t = 900,
            value_name = "SECONDS",
            allow_negative_numbers = true
        )]
        lease: i64,
    },
    /// read the published result of a task
    Result { task_id: String },
    /// global state: agents, queues, settled tasks, presence
    State,
    /// post or refresh a presence heartbeat with a capability card
    Heartbeat {
        agent: String,
        #[arg(
            long,
            default_value_t = 120,
            value_name = "SECONDS",
            allow_negative_numbers = true
        )]
        ttl: i64,
        /// JSON object describing the agent's capabilities
        #[arg(long)]
        capabilities: Option<String>,
        /// model carrying the instance right now
        #[arg(long)]
        model: Option<String>,
    },
    /// append a message to the global channel
    Announce {
        author: String,
        topic: String,
        /// omit or use '-' to read from stdin
        message: Option<String>,
    },
    /// read the global announcement channel
    #[command(group(ArgGroup::new("from").required(true).args(["agent", "since_seq"])))]
    Channel {
        /// read from this agent's cursor (at-least-once)
        #[arg(long)]
        agent: Option<String>,
        /// pure read from this sequence number
        #[arg(long = "since", allow_negative_numbers = true)]
        since_seq: Option<i64>,
        /// filter by topic (only with --since)
        #[arg(long)]
        topic: Option<String>,
        #[arg(long, default_value_t = 100, allow_negative_numbers = true)]
        limit: i64,
    },
    /// send a directed signal to one agent (pick it up with `poll`)
    Notify {
        author: String,
        agent: String,
        /// omit or use '-' to read from stdin
        message: Option<String>,
        /// answer a broadcast (collected by `iab replies`)
        #[arg(long, value_name = "BROADCAST_ID", allow_negative_numbers = true)]
        reply_to: Option<i64>,
        /// ask a supervisor to launch the recipient if asleep (roster permitting)
        #[arg(long)]
        wake: bool,
    },
    /// ask or tell several agents at once (scope: '*', 'harness@*',
    /// '*@workspace', 'model:<glob>' or a comma-separated list)
    Broadcast {
        author: String,
        scope: String,
        /// omit or use '-' to read from stdin
        message: Option<String>,
        /// skip asleep recipients
        #[arg(long)]
        alive_only: bool,
        /// ask a supervisor to launch asleep recipients (roster permitting)
        #[arg(long)]
        wake: bool,
    },
    /// answers received to a broadcast
    Replies {
        #[arg(allow_negative_numbers = true)]
        broadcast_id: i64,
    },
    /// read an agent's own directed notifications (at-least-once)
    Poll {
        agent: String,
        #[arg(long, default_value_t = 100, allow_negative_numbers = true)]
        limit: i64,
    },
    /// block until a signal lands for the agent: --on task (default) prints
    /// queued task_ids one per line; --on notification|any prints the signal
    /// snapshot as JSON; exits 0 either way
    Watch {
        agent: String,
        /// which signal ends the wait (default: task)
        #[arg(long, value_enum, default_value = "task")]
        on: On,
        #[arg(
            long,
            default_value_t = 300.0,
            value_name = "SECONDS",
            allow_negative_numbers = true
        )]
        timeout: f64,
        #[arg(
            long,
            default_value_t = 2.0,
            value_name = "SECONDS",
            allow_negative_numbers = true
        )]
        interval: f64,
    },
    /// what waits for an agent (tasks, unread notifications, broadcasts) — read only
    Signals { agent: String },
    /// list all known agents and their presence status
    Presence,
    /// transition history (push/claim/expire/publish/announce)
    Log {
        task_id: Option<String>,
        #[arg(short, long)]
        agent: Option<String>,
        #[arg(short = 'n', long, default_value_t = 100, allow_negative_numbers = true)]
        limit: i64,
    },
    /// identity from the environment and the resolved bus database
    Whoami,
    /// launch an asleep agent if a signal waits for it, from the roster's
    /// `launch` block (one decision, synchronous)
    Wake {
        agent: String,
        /// print the decision and the command, launch nothing
        #[arg(long)]
        dry_run: bool,
        /// this machine's name, matched against launch.host
        #[arg(long)]
        host: Option<String>,
    },
    /// the single watcher: wake every roster agent with a `launch` block
    /// when a signal waits
    Supervise {
        #[arg(
            long,
            default_value_t = 60.0,
            value_name = "SECONDS",
            allow_negative_numbers = true
        )]
        interval: f64,
        /// one pass, then exit
        #[arg(long)]
        once: bool,
        #[arg(long)]
        dry_run: bool,
        /// this machine's name, matched against launch.host (default: the
        /// host name)
        #[arg(long)]
        host: Option<String>,
    },
    /// headless worker loop: claim → run a command with the payload on
    /// stdin → publish its stdout (ERROR: on failure)
    Worker {
        /// identity to work as (roster name)
        #[arg(long)]
        agent: String,
        /// model behind the command, declared on the bus (default: IAB_MODEL)
        #[arg(long)]
        model: Option<String>,
        /// one task (or NO_TASK) then exit
        #[arg(long)]
        once: bool,
        /// lease per claim; extended at every heartbeat while running
        #[arg(
            long,
            default_value_t = 900,
            value_name = "SECONDS",
            allow_negative_numbers = true
        )]
        lease: i64,
        /// initial delay between empty claims (doubles up to 60s)
        #[arg(
            long,
            default_value_t = 5,
            value_name = "SECONDS",
            allow_negative_numbers = true
        )]
        poll: i64,
        /// kill the command and publish ERROR after this long (default: no
        /// limit — a hung command holds its task forever)
        #[arg(long, value_name = "SECONDS", allow_negative_numbers = true)]
        task_timeout: Option<i64>,
        /// the worker command, after `--`; receives the payload on stdin
        #[arg(
            trailing_var_arg = true,
            allow_hyphen_values = true,
            value_name = "-- command..."
        )]
        worker_cmd: Vec<String>,
    },
}

/// A refusal of the command line itself, printed like a bus refusal.
#[derive(Debug)]
struct Refusal(String);

/// A failure of the core as the `ERROR: …` line a refusal prints.
fn describe(error: &Error) -> String {
    // The causes too: "error performing TLS handshake" alone does not
    // say which certificate was refused, nor why.
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
    format!("ERROR: {text}")
}

/// An answer of the bus, a failure turned into its `ERROR: …` line.
fn answer(outcome: Result<String, Error>) -> String {
    outcome.unwrap_or_else(|error| describe(&error))
}

impl From<Error> for Refusal {
    fn from(error: Error) -> Self {
        Self(describe(&error))
    }
}

impl From<serde_json::Error> for Refusal {
    fn from(error: serde_json::Error) -> Self {
        Self(format!("ERROR: JSON: {error}"))
    }
}

/// A positional text, or standard input when absent or `-`.
fn content(arg: Option<String>) -> Result<String, Refusal> {
    match arg {
        Some(text) if text != "-" => Ok(text),
        _ => {
            let mut bytes = Vec::new();
            std::io::stdin()
                .read_to_end(&mut bytes)
                .map_err(|e| Refusal(format!("ERROR: cannot read standard input: {e}")))?;
            String::from_utf8(bytes)
                .map_err(|_| Refusal("ERROR: standard input is not valid UTF-8.".to_owned()))
        }
    }
}

fn whoami() -> Result<String, Refusal> {
    let mut answer = match serde_json::to_value(identity::resolve_identity())? {
        serde_json::Value::Object(map) => map,
        _ => serde_json::Map::new(),
    };
    answer.extend(db_info());
    // cli.py prints this one with json.dumps defaults: ASCII only.
    Ok(pytext::ascii_escape(&pytext::dumps(&answer)?))
}

fn run(command: Command) -> Result<String, Refusal> {
    if let Command::Whoami = command {
        return whoami();
    }
    let bus = Bus::from_env()?;
    let out = match command {
        Command::Register {
            name,
            description,
            cwd,
            model,
        } => bus.register_agent(&name, &description, &cwd, model.as_deref())?,
        Command::Rename { old, new } => bus.rename_agent(&old, &new)?,
        Command::Push {
            agent,
            task_id,
            payload,
            priority,
            max_attempts,
            no_notify,
        } => {
            let payload = content(payload)?;
            bus.push_task(&agent, &task_id, &payload, priority, max_attempts, !no_notify)?
        }
        Command::Claim {
            agent,
            lease,
            task_id,
        } => bus.claim_task(&agent, lease, task_id.as_deref())?,
        Command::Publish {
            agent,
            task_id,
            content: text,
            attempt,
            force,
        } => {
            let text = content(text)?;
            bus.publish_result(&agent, &task_id, &text, attempt, force)?
        }
        Command::Cancel { task_id } => bus.cancel_task(&task_id)?,
        Command::Requeue { task_id } => bus.requeue_task(&task_id)?,
        Command::Extend { task_id, lease } => bus.extend_lease(&task_id, lease)?,
        Command::Result { task_id } => bus.read_result(&task_id)?,
        Command::State => bus.get_system_state()?,
        Command::Heartbeat {
            agent,
            ttl,
            capabilities,
            model,
        } => bus.heartbeat(&agent, ttl, capabilities.as_deref(), model.as_deref())?,
        Command::Announce {
            author,
            topic,
            message,
        } => {
            let message = content(message)?;
            bus.announce(&author, &topic, &message)?
        }
        Command::Channel {
            agent,
            since_seq,
            topic,
            limit,
        } => bus.read_channel(agent.as_deref(), since_seq, topic.as_deref(), limit)?,
        Command::Notify {
            author,
            agent,
            message,
            reply_to,
            wake,
        } => {
            let message = content(message)?;
            bus.notify(&author, &agent, &message, reply_to, wake)?
        }
        Command::Broadcast {
            author,
            scope,
            message,
            alive_only,
            wake,
        } => {
            let message = content(message)?;
            bus.broadcast(&author, &message, &scope, alive_only, wake)?
        }
        Command::Replies { broadcast_id } => bus.replies(broadcast_id)?,
        Command::Poll { agent, limit } => bus.poll(&agent, limit)?,
        Command::Watch {
            agent,
            on,
            timeout,
            interval,
        } => match on {
            On::Task => {
                let out = bus.wait_for_task(&agent, timeout, interval)?;
                if out.starts_with("ERROR") {
                    out
                } else {
                    let ids: Vec<String> = serde_json::from_str(&out)?;
                    ids.join("\n")
                }
            }
            On::Notification => bus.wait_signal(&agent, "notification", timeout, interval)?,
            On::Any => bus.wait_signal(&agent, "any", timeout, interval)?,
        },
        Command::Signals { agent } => bus.pending_signals(&agent)?,
        Command::Presence => bus.list_presence()?,
        Command::Log {
            task_id,
            agent,
            limit,
        } => bus.get_events(task_id.as_deref(), agent.as_deref(), limit)?,
        Command::Whoami => whoami()?,
        Command::Wake { agent, dry_run, host } => {
            wake::wake_agent(&bus, &agent, None, dry_run, host.as_deref())
        }
        Command::Supervise { .. } | Command::Worker { .. } => {
            return Err(Refusal("ERROR: not a one-shot command.".to_owned()));
        }
    };
    Ok(out)
}

/// The long-running commands: their log goes to stderr, their outcome
/// is the exit status.
fn run_loop(command: Command) -> Result<u8, Refusal> {
    match command {
        Command::Worker {
            agent,
            model,
            once,
            lease,
            poll,
            task_timeout,
            mut worker_cmd,
        } => {
            if worker_cmd.first().is_some_and(|arg| arg == "--") {
                worker_cmd.remove(0);
            }
            if worker_cmd.is_empty() {
                Cli::command()
                    .error(
                        clap::error::ErrorKind::MissingRequiredArgument,
                        "worker: missing command after `--`",
                    )
                    .exit();
            }
            let model = model.or_else(|| std::env::var("IAB_MODEL").ok().filter(|m| !m.is_empty()));
            let bus = Bus::from_env()?;
            Ok(worker::run(
                &bus,
                &worker::Options {
                    agent,
                    command: worker_cmd,
                    once,
                    lease_seconds: lease,
                    poll_seconds: poll,
                    task_timeout,
                    model,
                },
            ))
        }
        Command::Supervise {
            interval,
            once,
            dry_run,
            host,
        } => {
            let bus = Bus::from_env()?;
            Ok(wake::supervise(&bus, interval, once, dry_run, host.as_deref()))
        }
        _ => Err(Refusal("ERROR: not a long-running command.".to_owned())),
    }
}

fn main() -> ExitCode {
    let cli = Cli::parse();
    if matches!(cli.command, Command::Worker { .. } | Command::Supervise { .. }) {
        return match run_loop(cli.command) {
            Ok(code) => ExitCode::from(code),
            Err(Refusal(text)) => {
                let _ = writeln!(std::io::stdout(), "{text}");
                ExitCode::from(1)
            }
        };
    }
    let mut out = run(cli.command).unwrap_or_else(|Refusal(text)| text);
    if cli.json {
        if let Ok(value) = serde_json::from_str::<serde_json::Value>(&out) {
            if let Ok(pretty) = pytext::dumps_indented(&value) {
                out = pretty;
            }
        }
    }
    if !out.is_empty() {
        let mut stdout = std::io::stdout().lock();
        // A closed pipe (`iab state | head`) is not an error of the bus.
        let _ = stdout
            .write_all(out.as_bytes())
            .and_then(|()| stdout.write_all(b"\n"));
        let _ = stdout.flush();
    }
    if out.starts_with("ERROR") {
        ExitCode::from(1)
    } else {
        ExitCode::SUCCESS
    }
}
