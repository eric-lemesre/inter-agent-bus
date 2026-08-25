# inter-agent-bus

🇬🇧 English · 🇫🇷 [Français](README.fr.md)

An **[Agent Plugins](https://agent-plugins.org/) v1.0.0** plugin: a
coordination bus between heterogeneous AI agents (Claude Code, Kimi,
DeepSeek, local models…). The plugin provides the **mechanism** — task
queues with leases (claim/ack), a shared result store, an observable
state — and **never the cast**: agents and their roles are declared by the
consuming project in its roster.

## Transport vs shared state — two layers, one bus

These are different layers, and both are true at once:

- **stdio is the transport**: each agent client spawns **its own instance**
  of the MCP server (that is how stdio servers work — one subprocess per
  client). Nothing is shared at this layer.
- **SQLite is the bus**: all server instances read and write the **same
  database** (WAL mode, immediate transactions). *This* is where sharing
  happens.

The zero-daemon alternative would be a single `streamable-http` server that
*is* the memory — but someone must start, supervise and secure that daemon.
For a local multi-CLI setup, stdio + a SQLite bus wins.

Database path — resolution order: (1) the `IAB_DB` env var, the
authority — set it per project when in doubt (legacy `ORCHESTRATOR_DB`
honored); (2) an existing global or pre-rename database is kept
(migration); (3) otherwise **one database per project**, derived from
the launch working directory (realpath-normalized) under the platform
data directory — a user-scope install must not merge every project
into one bus. All participants of one project must resolve the same
path: `whoami()` returns the resolved path, its source and the project
key, so a rendezvous mismatch is diagnosable in one call.
(`PLUGIN_DATA` does not fit as the bus: the spec defines it *per
client*, hence invisible to the other agents.)

Planned evolutions and their invariants: [`ROADMAP.md`](ROADMAP.md).
Contributor rules (human or agent): [`AGENTS.md`](AGENTS.md).

## Components

- `servers/shared_memory/` — the MCP server (`server.py`, thin wrapper) and
  the storage core (`store.py`, no MCP dependency, testable standalone).
  Task lifecycle: `queued → claimed (lease) → done`, with terminal side
  exits `dead` (dead-lettered after `max_attempts` expired leases —
  `requeue_task` revives) and `cancelled`. An expired lease re-offers
  the task, so an agent dying after `claim_task` does not lose it; and
  settling is fenced by the claim's attempt token, so a stale worker
  cannot overwrite the result of the worker that took the task over.
- `skills/pipeline-router/` — routing skill (orchestrator side): universal
  rules (volume to flat-rate agents, bulk to the cheapest per token,
  critical work to the strongest reasoner, never self-review), roster as
  input.
- `skills/worker-loop/` — worker skill (consumer side): register under the
  operator-given identity, then loop `claim_task` → execute →
  `publish_result`; failure discipline (`ERROR:` results instead of
  silently expiring leases, self-review refusal, budget-cap handback).

## Setup

```bash
python3 -m venv .venv                                  # py -m venv .venv on Windows
.venv/bin/pip install -r requirements.txt -e .         # MCP SDK >= 2.0 + the `iab` CLI
.venv/bin/python servers/shared_memory/store_test.py   # tests, incl. cross-process sharing
```

The `iab` console script mirrors the MCP tools (`iab register / push /
claim / publish / cancel / requeue / extend / result / state / log /
whoami`) so the bus can be driven without MCP and without `python -c`. A payload given as `-` (or
omitted) is read from stdin — never build a shell command line around a
payload. `iab log [task_id]` renders the transition journal
(push/claim/expire/publish); `iab whoami` prints the identity from the
environment and the resolved database path.

`iab install --scope user` registers the MCP server at the user level
of Claude Code (through `claude mcp add-json`), with this venv's
interpreter and the server's path as absolute paths and
`IAB_AGENT_NAME=claude` baked into `env` (`--agent-name` to change,
`--print` to inspect the JSON without applying, `--scope
project|local` for narrower scopes). A newly added server only loads
in the *next* session — reopen, then verify with `whoami()`. Caveat of
a user-scope install: every session of that client shares the baked
identity, and each project gets its own bus database unless `IAB_DB`
says otherwise.

Point the clients' `command` to `.venv/bin/python` (absolute path): the
server must run under an interpreter that has the MCP SDK.

Consumer project side: copy
`skills/pipeline-router/references/roster.example.json` to `roster.json`,
adapt the cast, set `IAB_ROSTER` (and `IAB_DB` if the default path does
not suit). The legacy `ORCHESTRATOR_*` variable names remain honored.

Each agent client registers the MCP server (with generic MCP clients, use
**absolute paths** — the `cwd` = plugin-root convention only binds clients
implementing the Agent Plugins spec). The orchestrating session uses
`pipeline-router`; each worker session uses `worker-loop` with an identity
given by the operator.

## Headless workers

Some agent CLIs load no MCP servers in non-interactive mode (the field
failure behind the worker-daemon request): they cannot hold their own
claim/publish loop. `iab worker` holds it for them:

```bash
iab worker --agent kimi --once -- kimi --exec -    # adapt to the CLI's flags
```

Claim under lease → run the command with the payload **on stdin**
(never on the command line) → publish stdout with the claim's attempt
token. Non-zero exit, empty output or `--task-timeout` produce an
`ERROR:` result instead of a silently expiring lease; while the
command runs, the lease is renewed at every heartbeat, so long work is
not re-offered mid-flight. `--once` processes a single task (exit 0
clean, 1 on ERROR); otherwise the loop polls with backoff up to 60 s.
Delivery is at-least-once: worker commands should be idempotent.

## MCP tools

`whoami` (identity of the connected client — `IAB_AGENT_NAME` env var if
the launcher or the MCP registration set one, otherwise the MCP
handshake's clientInfo, to match against the roster's `client_hints` —
plus the resolved bus database path) ·
`register_agent` · `push_task` · `claim_task` (head of one's queue, or
one specific task via `task_id`) · `publish_result` (settles the task;
pass the claim's `attempt` token — lease fencing) · `cancel_task` ·
`requeue_task` · `extend_lease` · `read_result` · `get_system_state` ·
`get_events` (transition journal, filterable by task and/or agent).

Identity note: baking `IAB_AGENT_NAME` into each client's MCP
registration (`env` field) is the reliable way to give every worker its
identity — some clients announce only a generic SDK name in clientInfo.

## Security / trust model

The bus assumes a single-user machine. Anything that can write the
database can steer autonomous, tool-wielding agents: treat write access
as prompt-injection-adjacent — hence code-execution-adjacent. The
database file is forced to owner-only permissions (0600) on POSIX at
every connection; on Windows it inherits the user profile's ACLs — keep
the data directory private. The headless worker keeps payloads off the
command line (stdin only) and out of its logs (stderr shows task ids,
never payload content). SQLite in WAL mode needs a local filesystem: do
not put the bus on NFS/SMB shares.
