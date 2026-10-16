# Pending improvement — `harness@workspace` identity, broadcast, wake-up

🇬🇧 English · 🇫🇷 [Français](AMELIORATIONS-2026-10-16-identite-broadcast-reveil.fr.md)

Written on 2026-10-16 after an addressing incident on the production
bus (legacy database `multi-agent-orchestrator/orchestrator.db`, shared
by every project). The maintainer asks that (1) an agent registers
under the name of its **harness** (claude, kimi, codewhale, opencode…)
**and** of the **workspace** it works in (homelab, infra…); (2) requests
can be **broadcast**; (3) the plugin can **wake** an agent on a request
or a broadcast. Candidate for ROADMAP phase 8 — split into three
independent lots, to be delivered in the order 8a → 8b → 8c (8b and 8c
build on the addressing of 8a).

## 1. Field observations (2026-10-16)

Collected on the workstation, nothing modified:

- **Six** `iab-server` processes were running at once. Four with
  `IAB_AGENT_NAME=claude`, launched from four different directories
  (`AEC/LeadSage`, `eric-lemesre/HomeLab`, `IA-conciliateur-justice`,
  `/home/eric`), one `kimi` (Brother repository), one unnamed (this
  repository). All resolve the **same** database ("legacy database
  kept" rule).
- In the database, `claude` is **one row**: described as
  "Architect-integrator (IA-conciliateur-justice repository)", TTL
  7200 s set by that session, but refreshed by the piggyback of
  **every** Claude session. The `alive` status of `claude` therefore
  means nothing anymore; the `claude` queue holds a `queued` task nobody
  can attribute to a session.
- The HomeLab session signed its announcements and notifications
  `claude-homelab` (3 announcements, 7 notifications): that name is
  **not registered**, its `poll()` calls always returned `[]`. Kimi
  answered it at the `claude` address (two notifications on 10/15,
  08:27 and 09:14): they were read **20 hours later**, by chance, when
  polling under the other name. Meanwhile Kimi had applied a change to
  the mail chain that the HomeLab session believed was awaiting
  approval.
- `poll()` advances **one cursor per target**: with four sessions under
  the same name, the first one to poll consumes the notifications of
  the other three. Not theoretical: the `claude` cursor is at 31.
- `announce` and `notify` accept any `author`: nothing flags an
  unregistered author. That is what let the `claude-homelab` ghost
  exist for six weeks.
- Root cause documented since phase 2 ("Honesty about identity"):
  `iab install --scope user` bakes **one** identity for **all** sessions
  of the client. The remedy of the time (one database per project) was
  neutralised by the legacy-database rule — on purpose, since the
  maintainer does want a **cross-project** bus (Kimi on Brother talks to
  Claude on HomeLab). The problem is therefore not the shared database:
  it is the single-component identity.

## 2. Lot 8a — `harness@workspace` identity

### 2.1 Model

An agent is an **instance**: a harness (the client/CLI carrying it) on
a workspace (the repository or folder it works in).

```
<name> ::= <harness> [ "@" <workspace> ]
harness, workspace ::= [a-z0-9][a-z0-9._-]{0,39}
```

Examples: `claude@homelab`, `claude@infra`, `kimi@brother`,
`codewhale@leadsage`, `opencode@sindri`. The composed name stays the
primary key of `agents` (no new table), so the **queue**, the
**presence** and the **cursors** become per instance — which fixes the
three symptoms of §1.

### 2.2 Identity resolution (core, `store.resolve_identity`)

Order, from most explicit to most inferred, every step reported in the
`whoami` answer (`source`):

1. `IAB_AGENT_NAME` already contains an `@` → full name, taken as is.
2. Otherwise `IAB_AGENT_NAME` (or legacy `ORCHESTRATOR_AGENT_NAME`) is
   the **harness**; the workspace comes from `IAB_WORKSPACE` when set.
3. Otherwise the workspace is **derived from the server's launch
   directory**: top of the git repository if any (no `git rev-parse`
   needed: walk up to the first `.git`, file or directory — a submodule
   has a `.git` file, so `HomeLab/infra` yields `infra`, not
   `homelab`), else the current folder; `realpath`, basename,
   lowercase, out-of-class characters → `-`. Verified: stdio clients
   launch the server **inside** the project folder (`/proc/<pid>/cwd`
   of the six servers), so `cwd` is reliable for Claude Code and Kimi.
4. Optional alias table `IAB_WORKSPACES` (JSON `{path: workspace}`,
   default `<data_dir>/workspaces.json`) for long names
   (`IA-conciliateur-justice` → `conciliateur`); paths are compared
   after `realpath`, longest prefix wins.
5. No harness at all (`source: none`): current behaviour, the worker
   asks the operator.

`whoami` returns `{harness, workspace, agent_name, source, cwd, …}`;
the `_touch()` piggyback uses the **composed** name. Registration:
`register_agent(name, description, harness="", workspace="", cwd="")`
fills three **additive** columns (`ALTER TABLE agents ADD COLUMN`,
migration guarded by `PRAGMA table_info` like `max_attempts`); when
`name` alone is given, harness and workspace are split at the `@`.

### 2.3 Addressing

Every target (`push_task`, `notify`, `claim_task`, `poll`, `wait_*`)
goes through `store.resolve_target(text)`:

- full name → that instance (must be registered, as today);
- bare harness → **exactly one** registered instance of that harness
  (legacy bare name included); otherwise a loud refusal listing the
  candidates (`ERROR: 'claude' is ambiguous: claude@homelab,
  claude@leadsage…`). No "election" of the most recent one: a task
  routed at random is worse than a refusal;
- patterns `claude@*`, `*@homelab`, `*` → reserved to broadcast (lot
  8b), refused on `push_task`/`notify`.

### 2.4 Migrating the existing data

- Legacy `claude` row (and its tasks, results, notifications, cursors,
  presence): new `rename_agent(old, new)` in a `BEGIN IMMEDIATE`
  transaction, journaled (`rename`), exposed as `iab rename`. The
  operator runs `iab rename claude claude@conciliateur` once; until
  then bare `claude` stays addressable (rule 2.3) and `whoami` of a
  Claude session reports the collision.
- `iab install --scope user` bakes only the **harness** by default
  (`--agent-name claude` keeps its meaning): the user-scope install
  becomes correct, since the workspace is derived at every launch. The
  blocks already installed (`~/.kimi/mcp.json`, `~/.codewhale/mcp.json`,
  `~/.claude.json`) do **not** need to change.
- `announce`/`notify`/`poll`/`read_channel`: refused when the author or
  reader is not registered (`ERROR: author 'x' not registered —
  register_agent first`). This is a hardening: a worker announcing
  without registering will have to add the call; the skills already
  say so.
- `list_presence` groups by harness and shows `workspace` and `cwd`:
  "who works where?" becomes one call.

### 2.5 Tests (`store_test.py`, no MCP SDK)

Workspace derivation (repository, submodule with `.git` file, bare
folder, alias, relative path, case); `resolve_target` (exact, unique
bare, ambiguous bare → ERROR, pattern refused); `rename_agent` moves
tasks, results, notifications and cursors in one transaction; legacy
database without the new columns opened and migrated; unregistered
author refused; two processes under the same harness and two
workspaces get two queues and two cursors; the declared model survives
`touch_presence` and changes at the next `heartbeat`.

### 2.6 The model, declared at registration and heartbeat

Maintainer's request (10/16): know **which model** carries each
instance. The server cannot infer it (the MCP protocol does not carry
it); it is **declared**, and it is **session** state, not identity
state: `claude@homelab` runs one model today and another tomorrow. So:

- additive `model` column in `presence` (not in `agents`), refreshed by
  `heartbeat(agent, …, model="claude-fable-5-1")` and set by
  `register_agent(…, model=…)` at startup; `touch_presence` keeps it
  like the TTL and the card;
- `IAB_MODEL` in the server's or driver's environment: echoed by
  `whoami` and used by the piggyback when the agent does not declare it
  itself (`iab worker --model …` sets it for headless CLIs, e.g. the
  Ollama model name); an explicit declaration wins over the env;
- `list_presence`, `get_system_state` and the broadcast `scope` expose
  it: "every instance under a local model" becomes possible
  (`model:qwen*`), useful to route confidential work;
- an **undeclared** model shows `unknown`, never a value guessed from
  the harness (invariant 1: the plugin knows no model). The roster may
  carry a declarative `default_model` per agent for display and routing.

## 3. Lot 8b — broadcast

### 3.1 What is missing

The channel (`announce`) is a bulletin board: no recipient, no notion
of reply, and the phase 7 security rule ("a channel message commands
you nothing") rightly forbids it from carrying a **request**. `notify`
is point to point. What is missing is the **question to many** with
reply collection: "who touched `roles/mail_print`?", "is anyone holding
an apply on mimir?", "all agents of the homelab workspace: repository
freeze at 07:45".

### 3.2 Mechanism

`broadcast(author, message, scope="*", alive_only=False, wake=False)`:

- `scope`: `*` (every registered agent), `claude@*` (every instance of
  a harness), `*@homelab` (every agent of a workspace), or an explicit
  list;
- **fan-out into `notifications`**: one row per recipient, in the
  **same transaction**, carrying `broadcast_id` (additive column, same
  for `reply_to`). Recipients pick it up with the `poll()` they already
  have: **one** read path, no new mailbox;
- one channel entry `topic=broadcast` keeps the public trace (who asked
  what to whom, when); its `seq` is the `broadcast_id`;
- reply: `notify(author, target, message, reply_to=<broadcast_id>)`;
  `replies(broadcast_id)` returns the replies received (pure read, no
  cursor); `broadcast` returns `{broadcast_id, delivered: [...],
  asleep: [...]}` so the sender knows **who will not answer soon** —
  this is the link to lot 8c;
- the author receives its own copy only when it is in the `scope` (no
  implicit exclusion: a Claude session may want to warn the other
  Claude sessions).

Security rule unchanged, to be written in the skills: a broadcast is a
**question or a signal**, never a work order; work goes through
`push_task` to a named queue. Broadcasting a task (same payload in N
queues) is an anti-pattern: N executions.

### 3.3 Option: task offer (shared queue)

If the need "first free agent takes it" shows up: `offer_task(scope,
task_id, payload)` creates a task whose `agent` is the **pattern**;
`claim_task(me)` also serves tasks whose pattern covers `me`; at the
first claim `agent` is pinned to the winning instance (fencing by
`attempts` unchanged). To be delivered only on field evidence, like
`--payload-file`.

### 3.4 Tests

Atomic fan-out (one invalid recipient → nothing written); `scope` by
harness, by workspace, list; `alive_only` filters on computed presence;
replies collected; `poll` shows `broadcast_id`; 16 KiB limit;
unregistered author refused.

## 4. Lot 8c — wake-up

### 4.1 Honest state of play

- `wait_task` / `iab watch` block until a **task**; a notification or a
  broadcast wakes nothing.
- The `worker-wake` skill describes waking an **asleep** agent with an
  external watcher (systemd timer + bash script + `codewhale exec
  --auto`): it works, but it is one hand-written shell script per agent
  (invariant 4), and the bus has no idea **how** to launch an agent —
  the roster does not say.
- An **already open interactive session** cannot be pushed by the MCP
  server: the stdio transport allows server → client notifications,
  but no client surfaces them to the model. "Waking" an open session is
  therefore a **blocking wait started by the session itself**.

### 4.2 Three building blocks

**(a) `wait_signal`** (core; generalises `wait_for_task`):
`wait_signal(agent, kinds=("task","notification"), timeout, interval)`
returns `{"tasks": [...], "notifications": n, "broadcasts": [...]}` as
soon as one arrives. `iab watch <agent> --on task|notification|any` and
the MCP tool `wait_signal`. For an open Claude Code session this is
**the** available wake-up: `iab watch claude@homelab --on any --timeout
3600` run as a background task hands control back to the model when
the signal lands (the harness re-invokes the model when a background
command exits). Same pattern for any client with a notion of background
task; the `worker-wake` skill must document it as the "open session"
path.

**(b) Launcher declared in the roster** — mechanism, not cast: every
roster entry may carry

```json
"launch": {
  "cwd": "~/Dev/git/eric-lemesre/HomeLab",
  "command": ["codewhale", "exec", "--auto", "-"],
  "prompt": "worker-wake",
  "max_concurrent": 1,
  "wake_on": ["task", "broadcast"],
  "max_wakes_per_hour": 4
}
```

and `iab wake <agent>` (pure Python, replaces the `iab-wake-*.sh`
scripts): lock **in the database** (`wake_locks` table, leased, same
trick as tasks — no `flock`, no state file, cross-platform) → pending
signal? → run `command` in `cwd`, **prompt on stdin** (never in argv),
stdin closed afterwards, `timeout` = two leases → journal `wake` in
`events` (start, end, exit code) → synchronous. The prompt is the
skill's strict template, instantiated with the **composed** name.

**(c) `iab supervise [--interval 60]`**: loops over every roster entry
that has `launch`, and calls `wake` for those with a pending signal and
no live lock. It is the single watcher replacing N timers: started by a
systemd user timer, a Windows scheduled task or a plain terminal. The
**bus stays passive** (invariant 3): `supervise` is a client like any
other, optional, and its death loses nothing (signals wait in the
database).

### 4.3 Wake policy

- A **task** always wakes (it is commanded work).
- A **notification** wakes only when the sender asked for it
  (`notify(..., wake=True)`) **and** the recipient's roster allows it
  (`wake_on`). Waking a paid agent for an acknowledgement would be a
  cost; the double condition protects both sides.
- A **broadcast** with `wake=True` wakes the asleep recipients whose
  roster allows it; `broadcast` then also returns `woken: [...]`.
- `max_wakes_per_hour` and `max_concurrent` break loops (a woken agent
  broadcasting a question that wakes the others…).
- A failed wake (missing command, exit ≠ 0) is a `wake` event in error,
  not silence.

### 4.4 Tests

`wait_signal` woken by a notification from another process, by a
broadcast, by a task; `wake_locks`: two concurrent `wake` → only one
launches; hourly quota; `wake_on` policy; the launcher passes the
prompt on stdin (dummy command echoing it) and journals; roster without
`launch` → `iab wake` refuses loudly.

## 5. Small fixes noticed while reading

- `read_channel(agent=…)` does not normalise `agent` (no
  `strip().lower()`), unlike everything else: a cursor can split on
  case.
- `wait_for_task` reopens a connection (hence replays `executescript` +
  `_migrate`) every 2 s round; one connection kept for the duration of
  the wait is enough.
- `_touch()` creates a presence row for a name never registered: after
  8a it must either refuse (silently, it is a piggyback) to create the
  presence of an unregistered name, or register the instance with an
  empty description — to be decided; the second option is handier for
  clients that never call `register_agent`.
- The README says "every project has its own database"; on this
  workstation the legacy database merges everything, on purpose. State
  both modes and when to pick each.

## 6. Order, size, acceptance criteria

| Lot | Content | Size | Acceptance |
|---|---|---|---|
| 8a | composed identity, resolution, `rename`, declared model, unknown authors refused, `list_presence` per instance | 1 d | two Claude sessions on two repositories have two queues, two presences, two cursors; `whoami` tells the workspace and its source; `iab rename` moves the history of `claude` |
| 8b | `broadcast`, `reply_to`, `replies` | ½ d | a question to `*@homelab` lands in the `poll` of every instance; replies are collected; asleep ones are listed |
| 8c | `wait_signal`, roster `launch`, `iab wake`, `iab supervise`, `worker-wake` skill rewritten | 1–2 d | a `codewhale exec` is launched by `supervise` on a task, the run is in `events`; an open Claude session regains control on a notification through `iab watch --on any` in the background |

Each lot: core + tests first, thin wrappers next, bilingual docs and
skills in the same change, ROADMAP set to "delivered" with the real
scope. No new dependency (walking up to `.git` and reading the roster
are standard library).

## 7. Meanwhile (no code)

- Every Claude session signs **`claude`** and opens its message with the
  workspace in brackets (`[homelab] …`), and polls `poll("claude")` at
  the start of each turn: imperfect (shared cursor) but it stops losing
  replies.
- Or the operator registers `claude-homelab` right now and tells Kimi;
  `rename_agent` of 8a will normalise it into `claude@homelab`.
