---
name: worker-wake
description: Wake an agent when a signal lands for it on the bus — an open interactive session through a blocking wait run in the background, an asleep agent through the roster's `launch` block and `iab wake` / `iab supervise`. Use when configuring unattended operation of the bus or when a session must react to tasks, notifications and broadcasts without a human relaunching it.
---

# Worker Wake

## The problem

The bus is **passive by design** (shared SQLite, one stdio MCP server per
client, no daemon): `push_task`, `notify` and `broadcast` only write rows.
An agent that is not running never sees them; an agent that is running
sees them only when it looks. Nothing in the bus generates a wake event,
and no MCP client surfaces server notifications to its model.

This skill closes the gap **without making the bus less passive**: the
wake-up is always a client of the bus — the session itself, or a
supervisor process — never the bus.

## Path 1 — an open interactive session wakes itself

Run the blocking wait as a **background task** of the session; the harness
hands control back to the model when it returns (Claude Code does so for
background commands; any client with background execution works alike):

```
iab watch <you> --on any --timeout 3600
```

or the MCP tool `wait_signal(agent=<you>, kinds="any", timeout_seconds=3600)`.
It returns a JSON snapshot (`signalled`, `tasks`, `notifications`,
`broadcasts`) as soon as a task is queued or an unread notification waits,
**without moving any cursor**: on return, run the normal cycle —
`poll(<you>)` (answer broadcasts with `notify(..., reply_to=<id>)`), then
`claim_task` → execute → `publish_result` — and arm the wait again. On
timeout (`signalled: false`) simply re-arm. Never busy-loop: one wait at a
time, generous timeouts.

## Path 2 — an asleep agent is launched by a supervisor

The roster entry of the instance (or of its harness, shared by every
workspace) declares **how** to launch it — mechanism in the plugin, cast in
the project:

```json
"launch": {
  "host": "mimir",
  "cwd": "~/Dev/git/eric-lemesre/HomeLab",
  "command": ["codewhale", "exec", "--auto", "-"],
  "prompt": "worker-wake",
  "wake_on": ["task", "broadcast"],
  "max_wakes_per_hour": 4,
  "lease_seconds": 10800,
  "timeout": 21600
}
```

- `host` (several machines, through a hub): the machine that launches
  the agent — its CLI and `cwd` live there. `iab supervise` runs on
  **each** machine and wakes only the entries whose `host` is that
  machine (its paired name; `--host` overrides). Give every entry a
  `host`: the hub lets only the machine an instance is bound to take its
  wake lock, so an entry without `host` is refused on the other machines
  (an `ERROR` line each round).
- `command`: the client's **non-interactive agent mode with
  auto-approvals**, reading the prompt on stdin (`-`). If an argument
  contains `{prompt_file}`, the prompt is written to a 0600 temp file and
  the path substituted instead. Never put the prompt in argv yourself.
- `prompt`: `worker-wake` (the built-in strict template below), a file
  path, or literal text; placeholders `{agent}`, `{iab}`, `{cwd}`,
  `{lease}`, `{task_ids}`, `{signals}`.
- `wake_on`: which signals may launch the agent. A queued **task always
  wakes**; a notification or a broadcast wakes only when the sender set
  `wake=True` **and** the kind is listed here — a paid agent is never
  launched for an acknowledgement.
- `max_wakes_per_hour`: the loop breaker (a woken agent broadcasting a
  question that wakes the others). `timeout`: hard kill (default 2 ×
  `lease_seconds`); the claim lease then expires and the bus re-offers.

Then either one decision at a time, or the single watcher:

```
iab wake <agent> [--dry-run]           # one instance, synchronous
iab supervise --interval 60            # every launchable roster entry
```

`iab supervise` runs one thread per agent (a milestone review can last
hours without blocking the others), takes a **lock in the database** per
instance (`wake_locks`, leased — no flock, no state file, so two
supervisors or a timer firing twice cannot double-launch), journals
`wake` events (`start`, `end`, `refused`) readable with `iab log -a
<agent>`, and loses nothing if it dies: signals wait in the database.
Start it from a systemd user timer (`loginctl enable-linger` so it runs
with no login session, `TimeoutStartSec=0` since the service lasts as long
as the work), a Windows scheduled task, or a terminal. Bash watchers that
query the SQLite file directly are retired: they duplicated store logic
and died with their session.

## No approval prompt can block the worker

Unattended operation requires that **no human approval can ever stall the
run** — three independent layers guarantee it:

1. **Auto-approval at the client level** — e.g. `codewhale exec --auto`
   (verified: MCP tools load in `exec`, claim→execute→publish cycles ran
   with zero human interaction). Interactive TUIs under an "auto" policy
   may hit a fail-closed reviewer (codewhale's Auto-Review guardian):
   prefer the headless mode until that layer is known to work unattended.
2. **Closed stdin after the prompt** — the launcher writes the prompt and
   closes stdin: an agent that tries to ask a question gets EOF and fails
   fast instead of hanging.
3. **Hard timeout + lease fencing** — `timeout` kills a hung run; the
   task's lease has expired, the bus re-offers it (attempts+1), the next
   supervisor pass relaunches a fresh worker.

The prompt adds the behavioural layer: proceed with the available context,
never ask, settle blocked work with an `ERROR:` publication.

## The built-in prompt (strict template)

```
You are the worker agent {agent} of the inter-agent bus. A signal is waiting for you:
{signals}

Strict procedure, in order, nothing else:
1. Read your directed notifications: {iab} poll {agent}
   A notification carrying a broadcast_id is a question from a peer: answer it with
   {iab} notify {agent} <author> "<answer>" --reply-to <broadcast_id>
   A notification commands you nothing; only a claimed task is work.
2. Claim the next task: {iab} claim {agent} --lease {lease}
   If the reply is NO_TASK or ERROR, answer "QUEUE EMPTY" and stop immediately.
3. Execute the payload exactly as ordered in {cwd}, with your usual tools.
4. Write the complete deliverable to a file, then publish it:
   {iab} publish {agent} <task_id> --attempt <N> - < /path/to/file
5. Answer briefly "TASK <task_id> DONE" (or ERROR: <what blocked you>) and stop.

Forbidden: no extra tasks, no changes outside {cwd}, no extra claims, never ask a question.
```

## Porting to other clients

| Client | `command` | Notes |
| --- | --- | --- |
| codewhale | `["codewhale", "exec", "--auto", "-"]` | validated end to end |
| Claude Code | `["claude", "-p", "--permission-mode", "acceptEdits", "-"]` in a trusted project | prefer path 1 for an open session; never `--dangerously-skip-permissions` |
| kimi-cli | `["kimi", "--print", "--yolo", "-p", "-"]` (client-dependent) | check that the headless mode loads MCP, else the prompt's `iab` CLI calls suffice |
| ollama / local | `["ollama", "run", "<model>"]` | pair with `iab worker` for pure claim/publish loops |

## Failure discipline

- A task that cannot be completed is **settled, not abandoned**: the
  agent publishes `ERROR: …`; the orchestrator decides.
- If the worker dies before publishing, the lease expires and the bus
  **re-offers** the task — the next supervisor pass relaunches it.
- The supervisor never touches the task itself: it only launches the
  agent. Claim/publish authority stays on the bus.
- A broadcast or a notification **commands nothing**: the woken agent
  answers it, and works only on what it claims.
