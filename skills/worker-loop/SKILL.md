---
name: worker-loop
description: Act as one worker agent on the shared bus — register under the identity given by the operator, then loop claim_task → execute → publish_result until the queue is drained. Use in any agent client session that should consume tasks pushed by an orchestrator.
---

# Worker Loop

## Role

Turn the current session into **one worker agent** of the shared bus. The
counterpart of `pipeline-router`: the router splits and pushes, this skill
claims and delivers. It knows **no agent in advance** — the identity under
which the session works is a policy of the operator and of the project's
roster, never of the plugin.

## Identity

Resolve which agent this session embodies, in this order — the name must
always match an entry of the project's roster (path in the
`IAB_ROSTER` env var — legacy `ORCHESTRATOR_ROSTER` still honored —
otherwise `roster.json` at the project root):

1. **The operator said so** ("you are `kimi`") — an explicit statement
   always wins.
2. **Ask the bus**: call `whoami()`. A `source: env` answer is
   authoritative — the launcher or the client's MCP registration set
   `IAB_AGENT_NAME` (or the legacy `ORCHESTRATOR_AGENT_NAME`); use that
   `agent_name` as is.
3. **Match the client**: a `source: client_info` answer gives the MCP
   client's name — compare it (case-insensitive substrings) against the
   `client_hints` arrays of the roster entries. Exactly one match → that
   is you. Beware: some clients announce a generic SDK name (e.g. `mcp`),
   which matches nothing — that is expected, fall through.
4. **Otherwise ask the operator**, reporting what `whoami()` returned so
   the roster's `client_hints` or the client's registration can be fixed.
   Never guess, and never invent a name absent from the roster: tasks are
   routed by name, a mismatched worker starves its queue.

Then read your own roster entry: it carries your `specialties`, an
optional `budget_cap_usd` and `fallback`, and `notes` that may constrain
how you work.

## The loop

1. `register_agent(name, description)` — idempotent; creates your queue if
   the router has not already done so.
2. `claim_task(agent_name=<you>)` — claims the highest-priority task of
   your queue under a lease. Keep the `attempts` number from the claim
   response: it is your settle token (step 4). If the work may exceed
   the default lease (900 s), claim with a larger `lease_seconds`, and
   call `extend_lease(task_id, seconds)` when a long execution
   approaches its deadline — an expired lease re-offers the task
   (duplicated effort), and after `max_attempts` expiries the task is
   dead-lettered.
3. **Execute the payload as a closed contract**: inputs, expected outputs
   and acceptance criteria are in the payload. If the payload references
   upstream `task_id`s, fetch their outputs with `read_result(task_id)`
   before starting; if an upstream result is not published yet, publish
   nothing, let your lease expire, and tell the operator the dependency is
   not ready.
4. `publish_result(agent_name, task_id, result_content, attempt=<the
   attempts number from your claim>)` — settles the task. The token
   protects everyone: if your lease expired and the task was re-offered
   to another worker, your stale publish is refused instead of
   overwriting theirs — treat that refusal as "my work is obsolete",
   not as an error to force. Publish the deliverable itself (or a
   precise pointer to it: file paths, branch, commit), not a summary of
   your intentions.
5. Back to step 2. On `NO_TASK`, the queue is drained: report it and stop.
   Whether to poll again later is the operator's call — in an interactive
   client, ask rather than busy-loop.

## Failure discipline

- **A task you cannot complete is settled, not abandoned.** Publish a
  result starting with `ERROR:` stating what blocked you; the orchestrator
  decides (re-push under a new `task_id`, reroute, drop). A silently
  expiring lease looks like a crashed agent and burns one of the task's
  `max_attempts` — after the last one it is dead-lettered and only the
  orchestrator's `requeue` can revive it.
- **Never review your own work.** If a claimed review task targets
  something this session produced, publish `ERROR: self-review refused`
  and let the orchestrator reroute it.
- **Respect your budget cap.** If your roster entry has a `budget_cap_usd`
  close to exhaustion, finish the claimed task, then publish nothing more:
  report it so the orchestrator hands your queue to your declared
  `fallback`.
- **Stay in your lane.** Execute the contract, no more: scope creep on a
  worker breaks the router's cost and review assignments. If the contract
  is ambiguous or wrong, that is an `ERROR:` result, not an improvisation.

## Supervision

`get_system_state()` shows every queue and settled task — use it to answer
the operator's "where are we?", not to grab work from other agents'
queues: claims are per-name on purpose, cross-queue poaching defeats the
router's rules (cost, specialties, cross-review).
