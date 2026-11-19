# Design: the harness as a control plane

This document is the target design of wyld3rness: the shape the transitions in [`strategy.md`](strategy.md) move toward. The ADRs in `adr/` record what each phase decided and built, and the strategy holds the diagnosis, the policies and the invariants. This page holds only the shape.

## The question

An LLM agent is a non-deterministic component inside a system that must behave deterministically where it matters. The same alert can produce a different answer on a different day, a retrieved document can carry instructions, and a confident answer can be wrong. The question the harness answers is how much an agent may do on its own, and on what evidence.

The answer this design gives: the agent never decides its own authority. Every request crosses a fixed set of checkpoints, each checkpoint enforces one invariant in code, and the authority each kind of action runs with is set by measured evidence, also in code.

## Two planes

The **data plane** is the app. FindyFixy, the example app, takes an incident alert and investigates it with read-only tools: past incidents, what changed, and what each service depends on. In the target shape, specialists investigate in parallel, each answering one question, and a coordinator merges their findings and proposes a triage record with one next action. The data plane is where the models' judgement is used.

The **control plane** is the harness. It sits on every path in and out of the data plane: requests, model calls, tool calls, proposed actions, releases. It never relies on the model behaving. Each of its checkpoints is deterministic code that holds the same way for every request.

![The two planes: the agent as the data plane, the harness as the control plane around it. Solid is built, dashed is planned.](target-design.svg)

## Invariants

Each checkpoint enforces one invariant. The invariants, the policy behind each and the check that proves it are listed once, in the enforcements of [`strategy.md`](strategy.md#enforcements).

## One request, end to end

An engineer pastes an alert, or the alert arrives on its own. The coordinator starts the specialists in parallel: what changed in the service and what it depends on, whether this has happened before, and whether the service is the cause or a victim. Each specialist reads only through its own tools, and every tool call presents a token the broker issued to that agent for that one tool and resource, which expires in five minutes. Every model call goes through the gateway, which checks the agent's and the team's budget, picks the model for the role, and records tokens and cost. Retrieved text is marked as untrusted data in every prompt.

The coordinator merges the findings and proposes a triage record and one action; schema validation rejects a malformed record. Every proposal, from the coordinator or any specialist, goes to the action ledger, which checks it against facts the model cannot forge: the action is one the app declared, the severity comes from the validated record, and a rollback is allowed only when the deploy is the only change to that service in the two hours before the alert, read from the change log, and is marked safe to roll back. Otherwise it becomes a page to a person, with the changes in that window listed. The ledger also holds a lock per blast radius (the service and everything that depends on it), so two actions that could interfere never run together, and it allows one destructive action in flight per incident.

If the action is allowed, its class decides what happens next. A read action runs. A destructive action runs at the level the autonomy policy grants its class: it waits for a human at Level 2, and runs with a human on the loop only at Level 3. Actions execute one at a time: the next one starts only after a check that the alert's signal recovered, and a failed check stops the run and pages. Every step lands in the trace, with the agent that took it, and eval scores attach to the same trace.

## Autonomy as policy

There are three levels. At Level 1 the agent advises and humans act. At Level 2 the agent acts after a human approves. At Level 3 the agent acts, and humans govern from on the loop, with the power to undo.

Autonomy is set per action class, not per agent:

| Class | Examples | Starts at |
|---|---|---|
| Read | recall, read logs, read metrics, list deploys | Level 3: runs freely |
| Reversible | page a human, open a ticket | Level 3: acts, human on the loop |
| Destructive | roll back a deploy | Level 2: acts after a human approves |

A class moves up one level only when its evidence passes thresholds stored in code: the eval gate on the current golden set, 0 unauthorized actions in the injection suite, and an approval record in which humans agreed with the agent's proposals often enough over enough cases. A regression in any of them moves the class down without a human decision, as far as Level 1, where the action is only proposed. The thresholds are set from measured data in Day 10, not guessed in advance.

## How an app plugs in

The harness never imports the app. Each control is a port the app implements or calls:

- `SystemUnderTest` (built): describe, answer, summarise. The eval gate runs the app through it.
- The event adapter (built): the app reports its steps, and the harness turns them into spans.
- The action port and ledger (Day 7): the app declares its actions, their classes and the facts policy checks; any of its agents proposes through it.
- The model port (Day 8): each of the app's agents asks the gateway for a completion under its role's budget and never holds a model client.
- The approval port (Day 9): the harness pauses a run and resumes it on an approval record.

Dependencies point inward: the harness defines the ports, the app depends on them, and one composition root in the app wires the real implementations.

## Two harnesses

This design is the runtime harness: what ships around the app. The build harness is how the coding agent is constrained while it writes the code: written rules for the agent, the pre-commit hooks, the privacy scan, plan-before-code, and a fresh-agent review before each substantial commit. Both are named in every write-up, because the word harness means the second one to many readers.

## What this design does not cover

It is one machine, one engineer and synthetic data. Multi-tenant isolation, high availability, data retention and a real identity provider are named where they would change a decision, and are not built. Specialists per infrastructure layer (Kubernetes, network, ML serving) fit the same ledger, broker and budgets, and need telemetry per layer first; they are left to the vision. The judge is a model too, so the eval gate measures agreement with a judge, which is itself checked by hand on sampled cases.
