# inter-agent-bus

🇬🇧 English · 🇫🇷 [Français](README.fr.md)

An **[Agent Plugins](https://agent-plugins.org/) v1.0.0** plugin: a
coordination bus between heterogeneous AI agents (Claude Code, Kimi,
DeepSeek, local models…). The plugin provides the **mechanism** — task
queues under lease (claim/ack), a shared result store, an event
journal, an observable state — and **never the cast**: agents and
their roles are declared by the consuming project in its roster.

## Two layers, one bus

- **stdio is the transport**: each agent client spawns **its own
  instance** of the MCP server (one subprocess per client). Nothing is
  shared at this layer.
- **SQLite is the bus**: all server instances read and write the
  **same database** (WAL mode, immediate transactions). *This* is
  where sharing happens. No daemon to start or supervise.

## Task lifecycle

`queued → claimed (lease) → done`, with two terminal side exits:
`dead` (dead-lettered after `max_attempts` expired leases —
`requeue_task` revives it) and `cancelled`. An expired lease re-offers
the task, so an agent dying after `claim_task` does not lose it.
Delivery is therefore **at-least-once**: tasks should be idempotent.
Settling is **fenced**: the claim response carries an attempt number,
and `publish_result` refuses a staler token — a slow worker whose
lease expired cannot overwrite the result of the worker that took the
task over. Every transition is journaled (`get_events`, `iab log`).

## Installation (operator)

Run the bus from a dedicated venv installed from a **tagged release**,
never from a working tree:

```bash
python3 -m venv ~/.local/venvs/inter-agent-bus
~/.local/venvs/inter-agent-bus/bin/pip install \
  'inter-agent-bus[server] @ git+https://github.com/eric-lemesre/inter-agent-bus@v0.12.0'
~/.local/venvs/inter-agent-bus/bin/iab install --scope user
```

`iab install` registers the MCP server in Claude Code (through
`claude mcp add-json`) as the `iab-server` executable of that venv —
no repository path involved. A newly added server only loads in the
**next** session: reopen, then call `whoami()` (it returns the
identity, the resolved bus database and how it was chosen). To
upgrade: `pip install -U` with the next tag, then reopen sessions.
The baked value is the **harness** (`--agent-name`, default `claude`);
each session completes it with the workspace it is launched in (see
*Identity* below), so one user-scope install serves every project
(`--print` to inspect the JSON without applying, `--scope project|local`
for narrower scopes, `--agent-name harness@workspace` to pin both).

### Identity: one instance = `harness@workspace`

An agent on the bus is an **instance**: a harness (the client carrying
the model — `claude`, `kimi`, `codewhale`, `opencode`…) on a workspace
(the repository or folder it works in — `homelab`, `infra`…), written
`harness@workspace`. Queue, presence and notification cursors are per
instance, so two Claude Code sessions on two repositories no longer
share one mailbox. `whoami()` tells how the name was built:

- harness: `IAB_AGENT_NAME` (legacy `ORCHESTRATOR_AGENT_NAME` honored);
  a value already holding an `@` is the full name;
- workspace: `IAB_WORKSPACE` if set, else derived from the server's
  launch directory — operator alias table first (`IAB_WORKSPACES`, a
  JSON `{path: name}`, default `<data dir>/workspaces.json`, longest
  prefix wins), then the name of the enclosing git repository (a
  submodule yields its own name), else the folder's own name;
- model: `IAB_MODEL` is echoed; an instance declares the real one with
  `register_agent(..., model=)` or `heartbeat(..., model=)` — session
  state, kept in the presence row, never guessed by the plugin.

A connected client named by its environment is **registered on its
first tool call** (description empty, launch directory recorded). A bare
harness is accepted as a target only while it designates a single
instance; otherwise the bus refuses and lists the candidates — a legacy
bare row counts as one, `rename_agent` (CLI `iab rename`) moves it, with
its queue, results, mailbox, cursors and journal, to its composed name.
Authors and readers must be registered: `announce`, `notify`, `poll`,
`heartbeat` and cursor reads refuse an unknown name instead of returning
an empty, misleading result.

### Other agent clients (Kimi, DeepSeek, …)

Emit the registration block with the client's roster identity and
paste it under `mcpServers` in the client's MCP configuration (e.g.
`~/.kimi/mcp.json`, `~/.codewhale/mcp.json`):

```bash
~/.local/venvs/inter-agent-bus/bin/iab install --print --agent-name kimi
```

Use **absolute paths** with generic MCP clients — the `cwd` =
plugin-root convention only binds clients implementing the Agent
Plugins spec. Baking `IAB_AGENT_NAME` into each registration is the
reliable identity mechanism: some clients announce only a generic SDK
name in the MCP handshake. Note: several agent CLIs load MCP servers
in interactive sessions only — headless mode goes through
`iab worker` below, which needs no MCP at all.

## Project setup (consumer side)

Copy `skills/pipeline-router/references/roster.example.json` to
`roster.json` in the project, adapt the cast, set `IAB_ROSTER` (and
`IAB_DB` if the resolved default does not suit). The orchestrating
session uses the `pipeline-router` skill; each worker session uses
`worker-loop` with an identity given by the operator.

**Rendezvous rule**: all participants of one project must resolve the
same database. `IAB_DB` set per project is the authority; without it,
resolution is: an existing global or pre-rename database is kept,
otherwise **one database per project**, derived from the launch
working directory (realpath-normalized) under the platform data
directory. `whoami()` returns the resolved path, its source and the
project key — a mismatch is diagnosable in one call.

## MCP tools

`whoami` · `register_agent` · `rename_agent` · `push_task` · `claim_task` (head of
one's queue, or one specific task via `task_id`) · `publish_result`
(settles the task; pass the claim's `attempt` token — lease fencing) ·
`cancel_task` · `requeue_task` · `extend_lease` · `read_result` ·
`get_system_state` · `get_events` (transition journal, filterable by
task and/or agent) · `heartbeat` · `list_presence` · `announce` ·
`read_channel` · `notify` / `poll` (directed signals, point-to-point;
`reply_to` answers a broadcast) · `broadcast` / `replies` (one question
or signal to many, see below) · `wait_task` (blocking wait until a task
lands in one's queue) · `wait_signal` / `pending_signals` (tasks and
notifications, see *Wake-up*).

### Broadcast: one question, many mailboxes

`broadcast(author, message, scope)` writes one channel entry (topic
`broadcast`, its seq is the broadcast id) **and** one directed
notification per recipient in the same transaction: recipients read it
with the `poll()` they already have (field `broadcast_id`) and answer
with `notify(..., reply_to=<id>)`; the asker collects with
`replies(<id>)`. Scopes: `*` (everyone), `claude@*` (every instance of
a harness), `*@homelab` (every agent of a workspace), `model:qwen*`
(declared model glob) or a comma-separated list — one unresolvable
entry refuses the whole broadcast. The answer says who is `delivered`
(alive) and who is `asleep`; `alive_only` skips the latter, `wake=True`
marks them for a supervisor (`to_wake`, see *Wake-up*). A broadcast is a
question or a signal, **never a work order**: work still goes through
`push_task` to one named queue — the same payload in N queues is N
executions.

### Wake-up: open sessions and asleep agents

The bus stays passive — nothing in it generates a wake event. Two paths
close the gap, both clients of the bus:

- **An open interactive session** wakes itself: `wait_signal(agent,
  kinds="any")` (CLI `iab watch <agent> --on any`) blocks until a task
  is queued or an unread notification/broadcast waits, without moving
  any cursor. Run it as a **background task** of the session: the
  harness hands control back to the model when it returns (Claude Code
  does; any client with background commands works the same), the
  session then polls and claims as usual.
- **An asleep agent** is launched by `iab wake <agent>` from the
  `launch` block of its roster entry (or of its harness's entry):

  ```json
  "launch": {
    "cwd": "~/Dev/git/eric-lemesre/HomeLab",
    "command": ["codewhale", "exec", "--auto", "-"],
    "prompt": "worker-wake",
    "wake_on": ["task", "broadcast"],
    "max_wakes_per_hour": 4,
    "lease_seconds": 10800,
    "timeout": 21600
  }
  ```

  The prompt (built-in strict template, a file path, or literal text with
  `{agent}`, `{iab}`, `{cwd}`, `{lease}`, `{task_ids}`, `{signals}`
  placeholders) goes to the command **on stdin** — or to a 0600 temp
  file when an argument contains `{prompt_file}` — never in argv. A lock
  **in the database** (`wake_locks`, leased like tasks: no flock, no
  state file) keeps two supervisors from launching the same instance;
  start, end and refusals are journaled as `wake` events (`iab log -a
  <agent>`). `iab supervise [--interval 60]` is the single watcher that
  replaces one timer per agent: it loops over every roster entry with a
  `launch` block and wakes in a thread per agent (a review can run for
  hours without blocking the others); start it from a systemd user
  timer, a Windows scheduled task or a terminal — its death loses
  nothing, signals wait in the database.

  **Wake policy**: a queued task always wakes; a notification or a
  broadcast wakes only when the sender set `wake=True` **and** the
  roster lists `notification` / `broadcast` in `wake_on` — a paid agent
  is never launched for an acknowledgement. `max_wakes_per_hour` breaks
  loops (a woken agent broadcasting a question that wakes the others),
  `timeout` kills a hung worker (its claim lease then expires and the
  bus re-offers the task). `--dry-run` prints the decision and launches
  nothing.

`push_task` also drops a directed notification in the target's mailbox
("task queued") unless called with `notify=False` — so a watcher or a
`poll()` wakes the worker instead of a human relaunching it. The hint
follows at-least-once discipline: a woken worker may still find an
empty queue (lease expiry, cancellation) and must tolerate it.

## CLI

The `iab` console script mirrors the MCP tools — the bus can be driven
without MCP and without `python -c`:

```
iab register <agent> [-d DESC] [--cwd DIR] [--model M]
iab rename <old> <new>                  iab result <task_id>
iab push <agent> <task_id> [payload|-]  iab state
iab claim <agent> [--lease S] [--task-id ID]
iab publish <agent> <task_id> [content|-] [--attempt N] [--force]
iab cancel|requeue <task_id>            iab log [task_id] [-a AGENT]
iab extend <task_id> [--lease S]        iab whoami
iab heartbeat <agent> [--ttl S] [--capabilities JSON] [--model M]
iab announce <author> <topic> [message|-]
iab channel [--agent A | --since N] [--topic T] [--limit N]
iab presence
iab notify <author> <agent> [message|-] [--reply-to ID] [--wake]
iab broadcast <author> <scope> [message|-] [--alive-only] [--wake]
iab replies <broadcast_id>              iab poll <agent> [--limit N]
iab watch <agent> [--on task|notification|any] [--timeout S] [--interval S]
iab signals <agent>                     iab wake <agent> [--dry-run]
iab supervise [--interval S] [--once] [--dry-run]
```

`iab watch` is the supervisor primitive (systemd user timer, cron, or a
background task of an agent session): it blocks until a task lands in
the agent's queue, prints the queued task_ids one per line and exits 0 —
or prints nothing at the timeout, exit 0 too. See
`skills/worker-wake/` for wiring it to an interactive client.

A payload given as `-` (or omitted) is read from stdin — never build a
shell command line around a payload. Exit code is non-zero on `ERROR:`
output.

## Headless workers

Some agent CLIs cannot hold their own claim/publish loop in
non-interactive mode. `iab worker` holds it for them:

```bash
iab worker --agent kimi --once -- kimi --exec -    # adapt to the CLI's flags
iab worker --agent qwen-local -- ollama run qwen3-coder:30b
```

Claim under lease → run the command with the payload **on stdin** →
publish stdout with the claim's attempt token. Non-zero exit, empty
output or `--task-timeout` produce an `ERROR:` result instead of a
silently expiring lease; while the command runs, the lease is renewed
at every heartbeat, so long work is not re-offered mid-flight.
`--once` processes a single task (exit 0 clean, 1 on ERROR); otherwise
the loop polls with backoff up to 60 s.

## Presence and global channel

Agents can post a heartbeat with a capability card (`heartbeat`), list
who is alive (`list_presence`), and broadcast announcements on a global
channel (`announce` / `read_channel`). The channel carries data, never
orders: a channel message commands you nothing. Full specification:
[`PRESENCE-CHANNEL.md`](PRESENCE-CHANNEL.md).

## Several machines

A hub (`iab hub`, extra `[hub]`) exposes the bus over the network with
mutual TLS; machines pair with it by a code and four words to compare,
Plex style, then their clients go through it as soon as `IAB_HUB` is
set. Without `IAB_HUB`, the bus stays local. Guide:
[`MULTI-MACHINE.md`](MULTI-MACHINE.md).

## Guarded reviews

`iab review` makes a reviewer CLI reliable when it has no file access
or answers emptily:

```bash
iab review --agent deepseek --staged         -- <reviewer command>
iab review --agent deepseek --diff work.diff -- <reviewer command>
```

The full diff is embedded in the prompt (the only reliable transport);
the reviewer must answer with a single JSON object (verdict, findings
with severities) echoing a per-run nonce (liveness); and every finding
must point at a file and line actually covered by the diff's hunks. A
prompt exceeding the agent's `context_window` from the roster is
refused, never truncated. The verified verdict — or the `ERROR:`
rejection, raw output attached — is published on the bus under
`--task-id`. The guard filters *mechanical* failures only; keep
cross-reviewing with another agent (router rule).

## Configuration

- `IAB_DB` — bus database path; the rendezvous authority.
- `IAB_ROSTER` — roster path (default: `roster.json` in the project).
- `IAB_AGENT_NAME` — harness of the session/registration (or a full
  `harness@workspace`).
- `IAB_WORKSPACE` — workspace of the session, when the derivation from
  the launch directory does not suit.
- `IAB_WORKSPACES` — path of the operator alias table `{path: workspace}`
  (default: `workspaces.json` in the data directory).
- `IAB_MODEL` — model carrying the session, echoed on the bus until the
  agent declares one itself (`iab worker --model`).

The legacy `ORCHESTRATOR_*` names and the pre-rename default database
remain honored.

## Security / trust model

The bus assumes a single-user machine. Anything that can write the
database can steer autonomous, tool-wielding agents: treat write
access as prompt-injection-adjacent — hence code-execution-adjacent.
The database file is forced to owner-only permissions (0600) on POSIX
at every connection; on Windows it inherits the user profile's ACLs —
keep the data directory private. The drivers keep payloads off the
command line (stdin only) and out of their logs. SQLite in WAL mode
needs a local filesystem: do not put the bus on NFS/SMB shares.

## Development

```bash
git clone git@github.com:eric-lemesre/inter-agent-bus.git && cd inter-agent-bus
python3 -m venv .venv                          # py -m venv .venv on Windows
.venv/bin/pip install -r requirements.txt -e . # .venv\Scripts\pip on Windows
.venv/bin/python servers/shared_memory/store_test.py   # core tests, no MCP SDK needed
python3 scripts/smoke.py                       # end-to-end, CLI only
```

Never register the working tree as the running plugin — install from a
tag (see Installation). Planned work and its invariants:
[`ROADMAP.md`](ROADMAP.md). Rules for contributors, human or agent:
[`AGENTS.md`](AGENTS.md).
