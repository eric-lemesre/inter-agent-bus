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

The operator states which agent this session embodies (e.g. "you are
`kimi`"). That name must match an entry of the project's roster (path in
the `ORCHESTRATOR_ROSTER` env var, otherwise `roster.json` at the project
root). If no identity was given, ask — never guess one, and never invent
a name absent from the roster: tasks are routed by name, a mismatched
worker starves its queue. Read your own roster entry: it carries your
`specialties`, an optional `budget_cap_usd` and `fallback`, and `notes` that
may constrain how you work.

## The loop

1. `register_agent(name, description)` — idempotent; creates your queue if
   the router has not already done so.
2. `claim_task(agent_name=<you>)` — claims the highest-priority task of
   your queue under a lease. Estimate the work first: if it may exceed the
   default lease (900 s), claim with a larger `lease_seconds` rather than
   letting the lease expire mid-work (an expired lease re-offers the task
   to be claimed again — possibly by you, duplicating effort).
3. **Execute the payload as a closed contract**: inputs, expected outputs
   and acceptance criteria are in the payload. If the payload references
   upstream `task_id`s, fetch their outputs with `read_result(task_id)`
   before starting; if an upstream result is not published yet, publish
   nothing, let your lease expire, and tell the operator the dependency is
   not ready.
4. `publish_result(agent_name, task_id, result_content)` — settles the
   task. Publish the deliverable itself (or a precise pointer to it: file
   paths, branch, commit), not a summary of your intentions.
5. Back to step 2. On `NO_TASK`, the queue is drained: report it and stop.
   Whether to poll again later is the operator's call — in an interactive
   client, ask rather than busy-loop.

## Failure discipline

- **A task you cannot complete is settled, not abandoned.** Publish a
  result starting with `ERROR:` stating what blocked you; the orchestrator
  decides (re-push under a new `task_id`, reroute, drop). A silently
  expiring lease looks like a crashed agent and gets the same task
  re-offered forever.
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
