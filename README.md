# multi-agent-orchestrator

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

Database path: `ORCHESTRATOR_DB` env var, default
`~/.local/share/multi-agent-orchestrator/orchestrator.db`. (`PLUGIN_DATA`
does not fit as the bus: the spec defines it *per client*, hence invisible
to the other agents.)

## Components

- `servers/shared_memory/` — the MCP server (`server.py`, thin wrapper) and
  the storage core (`store.py`, no MCP dependency, testable standalone).
  Task lifecycle: `queued → claimed (lease) → done`; an expired lease
  re-offers the task, so an agent dying after `claim_task` does not lose it.
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
pip install -r requirements.txt               # MCP SDK (the core does not need it)
python3 servers/shared_memory/store_test.py   # tests, incl. cross-process sharing
```

Consumer project side: copy
`skills/pipeline-router/references/roster.example.json` to `roster.json`,
adapt the cast, set `ORCHESTRATOR_ROSTER` (and `ORCHESTRATOR_DB` if the
default path does not suit).

Each agent client registers the MCP server (with generic MCP clients, use
**absolute paths** — the `cwd` = plugin-root convention only binds clients
implementing the Agent Plugins spec). The orchestrating session uses
`pipeline-router`; each worker session uses `worker-loop` with an identity
given by the operator.

## MCP tools

`register_agent` · `push_task` · `claim_task` · `publish_result`
(settles the task) · `read_result` · `get_system_state`.
