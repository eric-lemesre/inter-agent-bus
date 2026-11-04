# Pending improvement — task-arrival watch for workers

🇬🇧 English · 🇫🇷 [Français](AMELIORATIONS-2026-08-26-task-watch.fr.md)

Noted on 2026-08-26, after a field workaround in a Kimi CLI worker session
(J0011 milestone of `IA-conciliateur-justice`, three-hands protocol). To be
folded into the ROADMAP as a phase-8 candidate — decision left to the
maintainer.

## Observation

An interactive worker session only discovers its queued tasks when a human
(or the orchestrator through another channel) tells it so, or when it polls
manually. On 2026-08-26 the Kimi worker had to be woken by the user ("tu as
une tâche qui t'attends"), then installed a **session-bound bash watcher**
polling the SQLite bus directly
(`SELECT … FROM tasks WHERE agent=? AND status='queued'`) so it could be
woken on the next arrival. It works, but it violates the plugin's own
invariants:

- it **bypasses the MCP/store layer** by reading the database file
  directly — logic duplicated outside `store.py`;
- it **dies with the session** — no persistence, no supervision;
- it is a **shell script** — not cross-platform (invariant 4), and every
  agent runtime ends up reinventing it.

## Existing building blocks

- A `notifications` table and directed signals already exist
  (`store.notify` / `store.poll_notifications`, MCP tools `notify` / `poll`,
  CLI `iab notify` / `iab poll`).
- But `push_task` does **not** emit any notification to the target agent,
  and nothing lets a worker **block until a task arrives** — no
  `wait`-style entry point in the store, the CLI or the MCP server.

## Proposal

Mechanism only, never the cast (invariant 1). Core first (invariant 2):
everything below lands in `store.py` with tests in `store_test.py`
runnable without the MCP SDK, then thin wrappers.

1. **Notification on push.** `push_task` inserts a directed notification
   to the target agent ("task `<id>` queued, priority N"). Opt-out flag
   `notify=False` for high-frequency pushers. At-least-once discipline
   already applies: the notification is a hint to `claim_task`, not a
   delivery channel.
2. **Blocking wait in the core.** `store.wait_for_task(agent, timeout_s,
   interval_s) -> list[str]`: polls the agent's queue and returns the
   queued task ids as soon as one appears, or an empty list on timeout.
   Pure Python `time.sleep` loop — no OS-specific primitive.
3. **CLI wrapper.** `iab watch <agent> [--timeout N] [--interval N]` —
   prints the queued task ids on stdout and exits 0 when one appears,
   exits 0 with empty output on timeout. This is the primitive a
   supervisor (systemd user unit, cron, or an agent session's background
   task) can run to wake a worker without a hand-written script.
4. **Optional MCP tool** `wait_task(agent, timeout_seconds)` — same core
   call, for runtimes able to issue a tool call in the background.
   Documented as blocking: it holds the tool call until return.

The worker-loop skill then documents the pattern: *watch → claim → execute
→ publish → watch again*, and every worker gets persistence for free from
whatever supervisor wraps `iab watch`.

## Acceptance criteria

- `store_test.py`: `push_task` makes the notification visible through
  `poll_notifications` for the target (and not for others);
  `wait_for_task` returns the task id when another thread/process pushes
  mid-wait, and returns empty after the timeout otherwise.
- `iab watch` behaves identically on Linux, macOS and Windows (pure
  Python, `pathlib`, no shell).
- README (EN/FR) and `skills/worker-loop/` updated to teach the pattern;
  the at-least-once warning stays (a woken worker may still find an empty
  queue — lease expiry, cancellation — and must cope).
