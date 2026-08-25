# Agent instructions — inter-agent-bus

Instructions for any AI agent (Claude Code, Kimi, DeepSeek, …) working
*on this repository*. For agents *using* the bus at runtime, see the
skills (`skills/pipeline-router/`, `skills/worker-loop/`) instead.

## What this is

An [Agent Plugins](https://agent-plugins.org/) v1.0.0 plugin: a
coordination bus between heterogeneous AI agents. Task queues with
claim/ack leases, a shared result store, an observable state. Transport
is MCP over stdio (one server instance per client); shared state lives
in a common SQLite database (WAL mode) — see the README for why both
layers coexist.

Planned work lives in [`ROADMAP.md`](ROADMAP.md); its invariants govern
every change and are summarized below.

## Layout

- `servers/shared_memory/store.py` — storage core. **All state logic
  lives here.** No MCP dependency, ever.
- `servers/shared_memory/server.py` — MCP wrapper (SDK >= 2.0,
  `MCPServer`). Thin: one `@mcp.tool()` per store function, no logic.
- `servers/shared_memory/store_test.py` — tests for the core, runnable
  **without** the MCP SDK, including cross-process sharing.
- `skills/` — agent-facing skills (router and worker sides).
- `mcp.json`, `plugin.json` — Agent Plugins manifests.

## Non-negotiable rules

1. **Mechanism, never the cast.** Never hardcode an agent name, role,
   model or provider in the plugin. The cast comes from the consuming
   project's roster (`IAB_ROSTER`, default `roster.json` at
   the project root).
2. **Core first.** New behavior goes into `store.py` with tests in
   `store_test.py`, then gets a thin wrapper in `server.py` (and in the
   CLI once it exists). If a change makes `server.py` hold logic, it is
   in the wrong place.
3. **Cross-platform.** Linux, macOS and Windows. Concretely:
   - paths via `pathlib`, no string concatenation, no POSIX literals;
   - platform defaults via `platformdirs`-style resolution, with
     `IAB_DB` always overriding;
   - tooling and scripts in pure Python — no `install.sh`-only or
     bash-only path; entry points via `console_scripts`;
   - never interpolate a payload into a shell command line (quoting
     differs per platform, argv is size-limited, and payloads are
     untrusted): pass payloads via stdin or a temp file;
   - remember venv layouts differ (`.venv/bin/python` vs
     `.venv\Scripts\python.exe`) — resolve, do not assume.
4. **Fail loud.** Errors are explicit strings (`ERROR: …`) or refusals;
   never truncate a payload silently; empty output from a subprocess is
   a failure. Preserve the lease semantics: at-least-once delivery is a
   documented property, not a bug to hide.
5. **Schema changes need a migration path.** The SQLite database is a
   long-lived rendezvous shared by several clients; a schema change
   must keep existing databases working (additive `CREATE TABLE IF NOT
   EXISTS` / `ALTER TABLE` guarded by inspection). Same spirit for
   names: the current env vars are `IAB_*`, and the legacy
   `ORCHESTRATOR_*` names (plus the pre-rename default DB path) must
   remain honored — do not remove the fallbacks.
6. **No new dependencies lightly.** The core currently needs only the
   standard library; the server needs only the MCP SDK. Keep it that
   way unless a dependency removes real complexity (e.g.
   `platformdirs`).

## Conventions

- Code, comments, docstrings and manifests: **English**.
- User-facing docs: **bilingual**, `X.md` (English) + `X.fr.md`
  (French), each linking to the other at the top. Keep both in sync in
  the same change.
- Markdown hard-wrapped around 76 columns, matching the existing files.
- Docstrings on MCP tools are the agent-visible contract — keep them
  one-sentence, behavior-first (see `server.py`).
- Commit messages: conventional prefix (`feat:`, `fix:`, `docs:`), in
  French, matching the repo history.

## Developing

```bash
python3 -m venv .venv                        # py -m venv .venv on Windows
.venv/bin/pip install -r requirements.txt    # .venv\Scripts\pip on Windows
.venv/bin/python servers/shared_memory/store_test.py
```

The test suite must pass standalone (no MCP SDK needed) before and
after any change to `store.py`. Tests that touch the database must use
a temporary `IAB_DB`, never the operator's real bus at the platform
default path.

## Security notes

- Payloads on the bus are untrusted input headed for autonomous,
  tool-wielding agents: treat write access to the database as
  code-execution-adjacent. Do not widen its permissions.
- Anything that spawns worker CLIs must keep the payload out of the
  shell (rule 3) and out of logs when it may contain secrets.
