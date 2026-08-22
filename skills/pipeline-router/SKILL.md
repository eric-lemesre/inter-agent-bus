---
name: pipeline-router
description: Break a request down into subtasks and route them to the agents declared in the project's roster, using universal cost/capability rules — use when orchestrating several AI agents through the shared bus (push_task/claim_task/publish_result).
---

# Pipeline Router

## Role

Analyze a request or an engineering ticket, split it into subtasks, and
assign each subtask to the best-placed agent **from the project's roster**.
This skill knows **no agent in advance**: the cast is a policy of the
consuming project, never of the plugin.

## The roster

The project declares its agents in a roster file (path in the
`ORCHESTRATOR_ROSTER` env var, otherwise `roster.json` at the project root).
Each entry describes: `name`, `provider`, `cost_model` (`flat` =
subscription, `credits` = per token, `local` = free), an optional
`budget_cap_usd`, `specialties` (capability keywords), `context_window`,
`notes`. Full example:
[`references/roster.example.json`](references/roster.example.json).

Before any routing: read the roster, then call
`register_agent(name, description)` for each agent to create the queues.

## Universal routing rules

1. **Volume goes to flat-rate agents** (`cost_model: flat`): their marginal
   cost is zero — bulk generation, large refactoring, test suites.
2. **Cheap bulk goes to the lowest per-token cost** (`credits` at low price,
   or `local`): parsing, formatting, triage, mocks.
3. **Critical work goes to the strongest reasoner**: architecture, security
   code, final review — regardless of cost.
4. **Never self-review**: the agent reviewing a piece of work is always
   different from the one that produced it.
5. **Respect budget caps**: an agent close to exhausting its
   `budget_cap_usd` hands its tasks over to its declared `fallback` agent.
6. **Very long contexts go to large windows** (`context_window`): ingesting
   whole repositories, bulky logs, long specifications.

## Execution workflow

1. Read the roster; `register_agent(...)` for each agent.
2. Split the request into **closed-contract** subtasks (inputs, outputs,
   acceptance criteria).
3. For each subtask: pick the agent using the rules above, then
   `push_task(target_agent=..., task_id=..., payload=<contract>, priority=...)`.
4. Each agent loops: `claim_task(agent_name=...)` → execution →
   `publish_result(...)` (publishing settles the task; an expired lease
   re-offers it automatically).
5. Dependencies resolve through `read_result(task_id)` — a downstream
   subtask references in its payload the upstream `task_id`s it needs.
6. Supervise with `get_system_state()`; route cross-reviews by applying
   rule 4.

## Routing helper outside a session

[`scripts/router.py`](scripts/router.py) offers heuristic routing from the
roster: `python3 scripts/router.py <roster.json> "<description>"`. It is a
fallback for scripts and hooks — in session, the orchestrating agent routes
by itself, using the rules above.
