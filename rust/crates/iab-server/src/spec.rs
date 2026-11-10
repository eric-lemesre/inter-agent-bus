//! The 24 tools as `servers/shared_memory/server.py` declares them: names,
//! descriptions, parameters, types and defaults, so that both servers
//! announce the same interface (`CONTRAT.md`, § 12).

/// Type of a parameter, as the JSON Schema of the reference names it.
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum Kind {
    /// `string`
    Str,
    /// `integer`
    Int,
    /// `number`
    Float,
    /// `boolean`
    Bool,
}

/// Value of an optional parameter left out by the caller.
#[derive(Debug, Clone, Copy)]
pub enum Fallback {
    /// A text.
    Str(&'static str),
    /// An integer.
    Int(i64),
    /// A number.
    Float(f64),
    /// A flag.
    Bool(bool),
}

/// One parameter of a tool; required when it has no fallback.
#[derive(Debug)]
pub struct Param {
    /// Argument name.
    pub name: &'static str,
    /// Expected type.
    pub kind: Kind,
    /// Value when absent; `None` makes the parameter required.
    pub default: Option<Fallback>,
}

/// One tool.
#[derive(Debug)]
pub struct Spec {
    /// Tool name.
    pub name: &'static str,
    /// Description shown to the model.
    pub description: &'static str,
    /// Parameters, in declaration order.
    pub params: &'static [Param],
}

/// Every tool, in the order of server.py.
pub const TOOLS: &[Spec] = &[
    Spec {
        name: "whoami",
        description: "Identity of the connected client as one instance `harness@workspace`: harness from IAB_AGENT_NAME (authoritative; legacy ORCHESTRATOR_AGENT_NAME honored), workspace from IAB_WORKSPACE or derived from the launch directory (alias table, enclosing git repository, folder name — `workspace_source` says which), model echoed from IAB_MODEL; without a harness, the MCP clientInfo sent at the handshake (to match against the roster's client_hints). Also returns the resolved bus database (path, how it was chosen, project key), so a rendezvous mismatch between clients is diagnosable in one call.",
        params: &[],
    },
    Spec {
        name: "register_agent",
        description: "Declare an agent instance (creates its queue): `name` is `harness@workspace` (or a bare harness), `cwd` where it works, `model` the model carrying it right now. The cast comes from the project's roster, never from the plugin.",
        params: &[
            Param {
                name: "name",
                kind: Kind::Str,
                default: None,
            },
            Param {
                name: "description",
                kind: Kind::Str,
                default: Some(Fallback::Str("")),
            },
            Param {
                name: "cwd",
                kind: Kind::Str,
                default: Some(Fallback::Str("")),
            },
            Param {
                name: "model",
                kind: Kind::Str,
                default: Some(Fallback::Str("")),
            },
        ],
    },
    Spec {
        name: "rename_agent",
        description: "Move an identity to a new name — queue, results, notifications, cursors, presence and journal follow in one transaction (migration of a legacy bare name to `harness@workspace`); refused when `new` exists.",
        params: &[
            Param {
                name: "old",
                kind: Kind::Str,
                default: None,
            },
            Param {
                name: "new",
                kind: Kind::Str,
                default: None,
            },
        ],
    },
    Spec {
        name: "push_task",
        description: "Queue a task for a registered agent (higher priority is served first; after max_attempts expired leases the task is dead-lettered); unless notify=False a directed notification wakes the target's watcher.",
        params: &[
            Param {
                name: "target_agent",
                kind: Kind::Str,
                default: None,
            },
            Param {
                name: "task_id",
                kind: Kind::Str,
                default: None,
            },
            Param {
                name: "payload",
                kind: Kind::Str,
                default: None,
            },
            Param {
                name: "priority",
                kind: Kind::Int,
                default: Some(Fallback::Int(1)),
            },
            Param {
                name: "max_attempts",
                kind: Kind::Int,
                default: Some(Fallback::Int(3)),
            },
            Param {
                name: "notify",
                kind: Kind::Bool,
                default: Some(Fallback::Bool(true)),
            },
        ],
    },
    Spec {
        name: "claim_task",
        description: "Claim the next task of one's queue under lease (re-offered if not settled before the deadline) — or one specific queued task of one's own queue when task_id is given; from a machine's general queue, by (default: you) skips the tasks you declined.",
        params: &[
            Param {
                name: "agent_name",
                kind: Kind::Str,
                default: None,
            },
            Param {
                name: "lease_seconds",
                kind: Kind::Int,
                default: Some(Fallback::Int(900)),
            },
            Param {
                name: "task_id",
                kind: Kind::Str,
                default: Some(Fallback::Str("")),
            },
            Param {
                name: "by",
                kind: Kind::Str,
                default: Some(Fallback::Str("")),
            },
        ],
    },
    Spec {
        name: "publish_result",
        description: "Publish a task's result (settles the task); pass the attempt number received at claim so a stale worker cannot overwrite a re-offered task's result (force overrides every check).",
        params: &[
            Param {
                name: "agent_name",
                kind: Kind::Str,
                default: None,
            },
            Param {
                name: "task_id",
                kind: Kind::Str,
                default: None,
            },
            Param {
                name: "result_content",
                kind: Kind::Str,
                default: None,
            },
            Param {
                name: "attempt",
                kind: Kind::Int,
                default: Some(Fallback::Int(0)),
            },
            Param {
                name: "force",
                kind: Kind::Bool,
                default: Some(Fallback::Bool(false)),
            },
        ],
    },
    Spec {
        name: "decline_task",
        description: "Refuse a claimed task explicitly, with a reason: on a machine's general queue it goes back to the queue for another agent (by, default you, is not offered it again); elsewhere it is settled as declined, the reason readable with read_result.",
        params: &[
            Param {
                name: "agent_name",
                kind: Kind::Str,
                default: None,
            },
            Param {
                name: "task_id",
                kind: Kind::Str,
                default: None,
            },
            Param {
                name: "reason",
                kind: Kind::Str,
                default: None,
            },
            Param {
                name: "attempt",
                kind: Kind::Int,
                default: Some(Fallback::Int(0)),
            },
            Param {
                name: "by",
                kind: Kind::Str,
                default: Some(Fallback::Str("")),
            },
        ],
    },
    Spec {
        name: "cancel_task",
        description: "Cancel a task that is not settled yet (terminal status `cancelled`).",
        params: &[Param {
            name: "task_id",
            kind: Kind::Str,
            default: None,
        }],
    },
    Spec {
        name: "requeue_task",
        description: "Re-offer a claimed, dead or cancelled task, re-arming three more attempts; a done task stays done.",
        params: &[Param {
            name: "task_id",
            kind: Kind::Str,
            default: None,
        }],
    },
    Spec {
        name: "extend_lease",
        description: "Renew the lease of a claimed task (deadline = now + lease_seconds) so long work is not re-offered mid-flight.",
        params: &[
            Param {
                name: "task_id",
                kind: Kind::Str,
                default: None,
            },
            Param {
                name: "lease_seconds",
                kind: Kind::Int,
                default: Some(Fallback::Int(900)),
            },
        ],
    },
    Spec {
        name: "read_result",
        description: "Read the published result of a task.",
        params: &[Param {
            name: "task_id",
            kind: Kind::Str,
            default: None,
        }],
    },
    Spec {
        name: "get_system_state",
        description: "Global state: registered agents, queue lengths (queued/claimed), settled tasks, and presence statuses.",
        params: &[],
    },
    Spec {
        name: "get_events",
        description: "Chronological transition history of the bus (push/claim/expire/publish/announce), filterable by task and/or agent.",
        params: &[
            Param {
                name: "task_id",
                kind: Kind::Str,
                default: Some(Fallback::Str("")),
            },
            Param {
                name: "agent",
                kind: Kind::Str,
                default: Some(Fallback::Str("")),
            },
            Param {
                name: "limit",
                kind: Kind::Int,
                default: Some(Fallback::Int(100)),
            },
        ],
    },
    Spec {
        name: "heartbeat",
        description: "Post or refresh a presence heartbeat with a capability card and the model carrying the instance (absent capabilities keep the existing card, absent model keeps the stored one); the agent must be registered.",
        params: &[
            Param {
                name: "agent",
                kind: Kind::Str,
                default: None,
            },
            Param {
                name: "ttl_seconds",
                kind: Kind::Int,
                default: Some(Fallback::Int(120)),
            },
            Param {
                name: "capabilities",
                kind: Kind::Str,
                default: Some(Fallback::Str("")),
            },
            Param {
                name: "model",
                kind: Kind::Str,
                default: Some(Fallback::Str("")),
            },
        ],
    },
    Spec {
        name: "list_presence",
        description: "List every known agent instance (harness, workspace, cwd, declared model) with its computed presence status and capability card.",
        params: &[],
    },
    Spec {
        name: "announce",
        description: "Append a message to the global channel (topic must match ^[a-z0-9-]{1,64}$; message ≤ 16 KiB).",
        params: &[
            Param {
                name: "author",
                kind: Kind::Str,
                default: None,
            },
            Param {
                name: "topic",
                kind: Kind::Str,
                default: None,
            },
            Param {
                name: "message",
                kind: Kind::Str,
                default: None,
            },
        ],
    },
    Spec {
        name: "read_channel",
        description: "Read channel entries. Use since_seq >= 0 for a pure read, or agent (since_seq omitted) to read from that agent's cursor and advance it (topic filter only allowed with since_seq).",
        params: &[
            Param {
                name: "agent",
                kind: Kind::Str,
                default: Some(Fallback::Str("")),
            },
            Param {
                name: "since_seq",
                kind: Kind::Int,
                default: Some(Fallback::Int(-1)),
            },
            Param {
                name: "topic",
                kind: Kind::Str,
                default: Some(Fallback::Str("")),
            },
            Param {
                name: "limit",
                kind: Kind::Int,
                default: Some(Fallback::Int(100)),
            },
        ],
    },
    Spec {
        name: "notify",
        description: "Send a directed signal to one agent instance (point-to-point; a bare harness is accepted only when it designates a single instance); the recipient picks it up with poll(). Author and target must be registered. reply_to=<broadcast id> answers a broadcast; wake=True asks a supervisor to launch an asleep recipient if its roster allows.",
        params: &[
            Param {
                name: "author",
                kind: Kind::Str,
                default: None,
            },
            Param {
                name: "target_agent",
                kind: Kind::Str,
                default: None,
            },
            Param {
                name: "message",
                kind: Kind::Str,
                default: None,
            },
            Param {
                name: "reply_to",
                kind: Kind::Int,
                default: Some(Fallback::Int(0)),
            },
            Param {
                name: "wake",
                kind: Kind::Bool,
                default: Some(Fallback::Bool(false)),
            },
        ],
    },
    Spec {
        name: "broadcast",
        description: "Ask or tell several agents at once — scope `*`, `harness@*`, `*@workspace`, `model:<glob>` or a comma-separated list — as one channel entry plus one notification per recipient (read with poll(), answered with notify(reply_to=broadcast_id)); returns who is alive to see it now and who is asleep. A question or a signal, never a work order: work goes through push_task.",
        params: &[
            Param {
                name: "author",
                kind: Kind::Str,
                default: None,
            },
            Param {
                name: "message",
                kind: Kind::Str,
                default: None,
            },
            Param {
                name: "scope",
                kind: Kind::Str,
                default: Some(Fallback::Str("*")),
            },
            Param {
                name: "alive_only",
                kind: Kind::Bool,
                default: Some(Fallback::Bool(false)),
            },
            Param {
                name: "wake",
                kind: Kind::Bool,
                default: Some(Fallback::Bool(false)),
            },
        ],
    },
    Spec {
        name: "replies",
        description: "Answers received to a broadcast (notifications sent with reply_to=broadcast_id), oldest first; a pure read.",
        params: &[Param {
            name: "broadcast_id",
            kind: Kind::Int,
            default: None,
        }],
    },
    Spec {
        name: "poll",
        description: "Read an agent's own directed notifications and advance its cursor (at-least-once).",
        params: &[
            Param {
                name: "agent",
                kind: Kind::Str,
                default: None,
            },
            Param {
                name: "limit",
                kind: Kind::Int,
                default: Some(Fallback::Int(100)),
            },
        ],
    },
    Spec {
        name: "wait_signal",
        description: "Block until a task is queued for the instance(s) — one name, or several separated by commas, e.g. you and your machine's general queue — and/or an unread notification (broadcasts included) waits — kinds: task, notification or any — and return the snapshot with signalled true/false (one per instance under by when several); new ignores what already waits and answers a since mark to pass back; no cursor moves. BLOCKING call for a background-capable runtime: the wake-up of an open session.",
        params: &[
            Param {
                name: "agent",
                kind: Kind::Str,
                default: None,
            },
            Param {
                name: "kinds",
                kind: Kind::Str,
                default: Some(Fallback::Str("any")),
            },
            Param {
                name: "timeout_seconds",
                kind: Kind::Float,
                default: Some(Fallback::Float(300.0)),
            },
            Param {
                name: "interval_seconds",
                kind: Kind::Float,
                default: Some(Fallback::Float(2.0)),
            },
            Param {
                name: "new",
                kind: Kind::Bool,
                default: Some(Fallback::Bool(false)),
            },
            Param {
                name: "since",
                kind: Kind::Str,
                default: Some(Fallback::Str("")),
            },
        ],
    },
    Spec {
        name: "pending_signals",
        description: "What waits for an instance right now — queued task ids, unread notifications, broadcast ids — without moving any cursor.",
        params: &[Param {
            name: "agent",
            kind: Kind::Str,
            default: None,
        }],
    },
    Spec {
        name: "wait_task",
        description: "Block until the agent's queue holds a queued task and return the task_ids (JSON list, \"[]\" on timeout) — BLOCKING call, use from a background-capable runtime.",
        params: &[
            Param {
                name: "agent",
                kind: Kind::Str,
                default: None,
            },
            Param {
                name: "timeout_seconds",
                kind: Kind::Float,
                default: Some(Fallback::Float(300.0)),
            },
            Param {
                name: "interval_seconds",
                kind: Kind::Float,
                default: Some(Fallback::Float(2.0)),
            },
        ],
    },
];
