---
name: worker-wake
description: Wake an INTERACTIVE agent (not a degraded headless mode) when a task lands in its queue on the bus — launch the client's full-featured TUI inside a terminal multiplexer with auto-approvals, so the worker claims, executes and publishes unattended, and the operator can attach later. Use when configuring unattended operation of the bus (timer/cron watcher on the queue).
---

# Worker Wake

## The problem

The bus is **passive by design** (shared SQLite, one stdio MCP server per
client, "no daemon to start or supervise"): a `push_task` only queues a
task and logs a transition; `announce` only appends to the channel.
Nothing generates a wake event. An interactive agent that is not running
will never see the task, and the `worker-loop` skill explicitly leaves the
poll cadence to the operator ("in an interactive client, ask rather than
busy-loop"). The result: tasks sit in the queue until someone manually
launches the agent.

This skill closes that gap **without making the bus less passive**: an
external watcher (systemd timer, cron, or a plain loop) watches the queue
and, when a task appears, launches the target agent's client in its
**full-featured interactive mode**, pre-loaded with a strict worker
prompt. The agent then runs the normal `worker-loop` cycle (claim →
execute → publish) completely unattended.

## Why headless-first, and when to use the full TUI

Headless one-shot modes (`codewhale exec`, `claude -p`, …) were once
suspected of not loading every capability of the interactive client. On
codewhale this is **verified wrong for the critical capability**: MCP
servers are loaded in `exec` mode (`mcp_inter-agent-bus_*` tools answered
`whoami` in a headless run). Conversely, the full TUI currently hits the
**Auto-Review guardian**: under `--approval-policy auto` every tool call is
reviewed by a separate reviewer model, and when that reviewer is
unavailable the call is denied **fail-closed** (“Auto-Review guardian
unavailable … the call was denied”) — an unattended TUI stalls on its
first shell call (`review_model` did not fix it in the tested build).

So the working default is **headless `exec --auto`** — validated
end-to-end (claim → execute → publish, MCP included). The tmux-TUI remains
the enhanced variant once the Auto-Review layer is functional (or on
clients without such a layer): same prompt, same cycle, plus the ability
to `tmux attach` and a full transcript.

## The method

1. **Watch** — a watcher (systemd user timer every 1–2 min, cron, or
   loop) reads the bus state: any task `queued` for the target agent.
   The task's `status` is the deduplication: a claimed task is no longer
   queued, so no state file is needed.
2. **Guard** — a `flock` on the watcher prevents overlapping launches;
   the bus **lease fencing** (`attempts` token on `publish_result`)
   settles any race with a concurrently running interactive session:
   whoever claims first wins, the other gets `NO_TASK`.
3. **Launch** — run the client's non-interactive agent mode with
   auto-approvals (`codewhale exec --auto`) in the project workspace,
   passing a strict **worker prompt** (see below). Enhanced variant:
   full TUI in `tmux` with the client's auto-approval policy
   (`codewhale --approval-policy auto -p "<prompt>"`) once the
   Auto-Review layer is available.
4. **Execute** — the agent runs the `worker-loop` cycle from the prompt:
   claim (with a generous lease — milestone reviews can run hours),
   execute the payload exactly, publish the deliverable with the attempt
   token, answer and stop.
5. **Settle** — `exec` is synchronous: the service lasts as long as the
   work. If the agent dies without publishing, the lease expires and the
   bus **re-offers** the task (attempts+1) — the next watcher pass
   relaunches it. In the tmux variant, the watcher polls the task
   status and kills the session on settle (or timeout, letting the bus
   re-offer). Never leave a zombie TUI behind.
6. **Repeat** — the next watcher pass drains the next queued task.

## No approval prompt can block the worker

Unattended operation requires that **no human approval can ever stall the
run** — three independent layers guarantee it:

1. **Auto-approval at the client level** — `codewhale exec --auto`
   auto-grants every tool call. Verified empirically: commands with no
   matching exec-policy rule ran, MCP tools answered, and two
   claim→execute→publish cycles completed with zero human interaction.
   (The interactive TUI is the opposite: under `--approval-policy auto`
   its Auto-Review guardian is fail-closed when unavailable — another
   reason the headless mode is the default.)
2. **Closed stdin** — the launcher runs `exec --auto … < /dev/null`:
   if the agent ever tries to elicit user input, it gets EOF and fails
   fast instead of hanging on a prompt.
3. **Hard timeout + lease fencing** — the launcher wraps the run in
   `timeout` (two lease cycles) and the worker prompt forbids asking
   questions. If a run still hangs, it is killed; the task's lease has
   already expired, the bus re-offers it (attempts+1), and the next
   watcher pass relaunches a fresh worker. Nothing on the bus ever waits
   on a human.

The worker prompt adds the behavioural layer: the agent must proceed
with the available context, never ask, and settle blocked work with an
`ERROR:` publication for the orchestrator to decide.

## The worker prompt (strict template)

```
You are the worker agent <NAME> of the inter-agent bus. A task is queued for you.

Strict procedure, in order, nothing else:
1. Claim the next task by running: <iab> claim <NAME> --lease 10800
   The JSON reply carries task_id, payload (the complete work order) and attempts.
   If the reply contains NO_TASK or ERROR, answer "QUEUE EMPTY" and stop immediately.
2. Read the payload: it is the exact work order (typically a code review on <REPO>).
3. Execute the work exactly as ordered, with your usual tools (file reads, search,
   shell). For any bus interaction (state, log, channel), use the <iab> commands.
4. Write the complete deliverable (report, findings, verdict) to a file, then publish it:
   <iab> publish <NAME> <task_id> --attempt <N> - < /path/to/file
   where <N> is the attempt number from the claim. Publish the deliverable itself,
   not a summary.
5. Answer briefly "TASK <task_id> DONE" (or ERROR: <what blocked you>) and stop.

Forbidden: no channel announcements, no extra tasks, no changes outside the work
repository, no extra claims.
```

## Worked implementation (codewhale on Linux/systemd)

Operator-side files (reference layout, adapt paths):

- `~/.local/bin/iab-wake-deepseek.sh` — the watcher: flock guard, queue
  probe (`SELECT … FROM tasks WHERE agent=… AND status='queued'`), launch
  `codewhale exec --auto "$(cat <prompt>)"` in the project workspace,
  log the run. Synchronous: the service lasts as long as the work.
- `~/.local/bin/iab-worker-run.sh` — the tmux-TUI payload (enhanced
  variant): reads the prompt file, then
  `exec codewhale --approval-policy auto -C <repo> -p "$(cat <prompt>)"`
  inside `tmux new-session -d -s codewhale-worker …`; the watcher polls
  the task and `tmux kill-session` on settle. Requires the Auto-Review
  guardian to be functional (see “Why headless-first”).
- `~/.config/systemd/user/iab-wake.{service,timer}` — timer every 2 min;
  service `Type=oneshot`, **`TimeoutStartSec=0`** (a milestone review can
  run for hours), env `XDG_RUNTIME_DIR` + `DBUS_SESSION_BUS_ADDRESS` for
  the optional desktop notification.
- Enable **lingering** (`loginctl enable-linger <user>`) so the user
  manager — and the timer — run even with no login session.

Observability: append-only wake log
(`~/.local/state/iab-wake-deepseek.log`) — deliberately **silent**: no
desktop notification, no operator contact. The deliverable itself lives in
the bus result store.

## Porting to other clients

Same shape, different launcher:

| Client | Interactive launch in tmux | Auto-approval |
| --- | --- | --- |
| codewhale | `codewhale -C <repo> -p "<prompt>"` | `--approval-policy auto` |
| Claude Code | `claude --print` is headless; interactive: `tmux … claude -p "<prompt>"` in a trusted project | `--dangerously-skip-permissions` is NOT recommended — prefer `--permission-mode acceptEdits`/trusted project or the headless fallback |
| kimi-cli | `tmux … kimi -p "<prompt>"` (client-dependent) | client approval flag or headless fallback |

Always prefer the client's **non-interactive agent mode with
auto-approvals** (validated: MCP is loaded in `exec` on codewhale); use
the interactive-in-multiplexer path only when the client's auto-approval
layer (Auto-Review guardian or equivalent) is known to work unattended,
and say so in the launch log so a blocked run is traceable.

## Failure discipline

- A task that cannot be completed is **settled, not abandoned**: the
  agent publishes `ERROR: …`; the orchestrator decides.
- If the worker session dies before publishing, the lease expires and the
  bus **re-offers** the task (attempts+1) — the next watcher pass
  relaunches it.
- The watcher never touches the task itself: it only launches the agent.
  Claim/publish authority stays on the bus.
