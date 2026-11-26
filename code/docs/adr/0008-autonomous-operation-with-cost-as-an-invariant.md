# ADR 0008: Autonomous operation, with cost as an invariant

Status: accepted as direction. Written 2026-11-21. Nothing in this record is built yet.
Phase: Day 7, direction. Code at the decision: the commit that adds this record.

## Decision

The harness and its agentic loops will let an agent run on-call without a human approving each action. Before runtime, people set the goal, the invariants, the allowed kinds of action, the budget and the gate. At runtime code decides what may act, checks real signals, undoes what did not work and stops in a safe state, a containment approved in advance.

Cost becomes an invariant beside safety. Each incident will have a budget that is enforced before the money is spent, and every result will be reported as cost per resolved incident.

All of it is tested on one simulated production: small real services under load, with faults the agent cannot see. This is Level 3 as the project first defined it: people govern from on the loop, by setting and reviewing the rules from failure patterns seen before production. The simulated production is where those patterns are seen. Human approval stays, and moves from each action to each kind of action.

![The architecture when this was decided: what is built is solid, what this record plans is dashed](img/0008-architecture.svg)

## Diagnosis

The agent can only propose text. It cannot act, and it has no signal to observe, so no test so far could show whether a wrong answer would have been noticed and corrected.

Its self-reported confidence cannot stand in for that signal. On the 30 golden cases the small model rated itself 0.90 to 0.98, including on all 7 severities it got wrong. In the case with the lowest faithfulness (case g28, a retry storm) the right cause was ranked first and third in recall, and the model copied the incident with the same service name. The larger model made the same mistake.

The judge is not that signal either. It rejected "the component is ledger" on that case while its context named ledger, and rejected 4 other claims, most of them stated in that context. By a hand count the 0.17 faithfulness reported in ADR 0007 should be at least 0.83. A judge also costs about 33 times the agent run it scores.

Nothing bounds cost today. Every call is priced after the fact on its trace, and no code can refuse one. A loop that retries turns that gap into an open-ended bill.

## Guiding policies

1. Code owns the loop, the stop, the undo and the definition of fixed. The model owns one decision at a time.
2. Approval is per kind of action and before runtime. It is never per action. A kind is allowed on evidence and removed on evidence.
3. A model call reserves its worst-case cost before it runs. An unknown cost counts as over budget, never as zero. An exhausted budget ends in the safe state.
4. Safety first, then resolution, then cost: among the options that pass the safety and resolution bars, the one with the lowest cost per resolved incident is used. A runbook with no model is always one of the options.
5. Cost per resolved incident counts every attempt, including failed tries and runs that ended in a safe state. Customer minutes lost and the cost of proving safety are reported beside it and never blended into it.
6. An action that did not work is undone and retried. One that cannot be undone is never retried: the incident goes to the safe state.
7. Prove the test before testing the agent: doing nothing must fail, a scripted fix must pass, a wrong fix must be undone.

## Coherent actions

The planned order of the four controls is kept. Each now runs on the simulated production, with its bar written down before its runs:

- The trust boundary: policy between a proposed action and any system, short-lived credentials per action, a versioned set of faults, an injection suite through the real prompt path, and one outage through the whole loop, metered and inside a budget.
- Cost and one gateway: gateways compared, the budget enforced on every model call, and no responder, a runbook and the agent compared on cost per resolved incident.
- Operation and approval: roll back and roll forward, scheduled evaluation, each kind of action approved once on the evidence of a game day, and removed when the evidence falls.
- Autonomy as policy: an unattended night, its postmortems, and an operating review that includes the full cost.

What this replaces in the earlier plan and in ADR 0007's open action:

- The computed escalation signal is replaced. A recovery check decides after acting, and a second opinion before acting is adopted only if a trigger clears its bar and is worth its cost.
- The harder static golden set is replaced by versioned faults on the running system. The existing golden set stays as the regression check for the decision step.
- Human approval of each destructive action is replaced by human approval of each kind of action.

## Build, buy or adopt

Adopt the parts with open-source standards: OpenTelemetry for traces, container Compose for the environment, and established components for metrics, alerts and fault injection after the usual license and image checks. Build the loop, the budget and the recovery check thin, as harness ports. The published live-fault benchmarks need more machine than this project runs on, so they are not adopted now.

## Staffing

No hire. People own the goal, the invariants, the allowed actions, the budget and the gate. A person is needed when a safe state has to be resolved afterwards, or when a new kind of action is to be allowed.

## What would change this

A recovery signal that cannot be made honest: then verification cannot replace approval. An agent that does not beat a runbook: then the runbook keeps the pager. A cost per resolved incident above the alternative it replaces: then the cheaper option is used. Real customers or data: then identity, retention and a human escalation path are decided again.

## Evidence

- Kinds of action: the resolving step of each of the 200 synthetic incidents falls into 17 kinds. 120 incidents end in an action that can be undone, 30 in one that cannot be undone but has limited harm, 50 in one that can do lasting damage and needs a precondition.
- Second opinion, an exploratory replay of the 30 saved prompts on 2026-11-21, two runs per model: severity was right on 23 for Qwen3 4B, 25 for Qwen3 8B, and 21 and 20 for Qwen2.5 7B. The bar, set before the run, was a net gain (fixed minus broken) of at least 3 with at most 1 broken. The best trigger that calls a second model (the severity differs from the top recalled incident, then ask Qwen2.5 7B) came close: it fixed 3 and broke 1 in the first run and fixed 2 and broke 1 in the second, a net of 2 and 1. It missed the bar, and 36 combinations of trigger and target were tried, so escalation is not switched on.
- Record of the classification, the replay outputs, the confidences and the script: report `replay-20261122T043131Z`, a local report like the eval runs cited by earlier records.
- Cost, from ADR 0007: $0.0000431 for an agent run at the reference price and $0.00144 for judging it.

## Limits

This is a direction and not a result. The evidence is synthetic, small and from local models. The 17 kinds come from tidy incidents that each end in one known fix. Reference prices cover tokens and leave out the machine. Zero failures in N runs supports only a failure rate below about 3/N, and only for what was tested.

## Day 7 decisions

Everything above is the direction as accepted before Day 7's build, with its picture; it is not edited. From here this record holds Day 7's design decisions, one section per step. Steps 0 to 3 were written after they were built, from the decision log, and say so; from step 4 on, each section is written in its step's plan, before any code. The Outcome at close says what was built and where it differed. What was measured is in the [research log](../research-log.md), what changed in the [changelog](../../CHANGELOG.md).

### Step 0: the gate at commit (written after the build)

The eval gate runs in the pre-commit hook, on exactly the staged tree in its own environment, and reuses a judge score only where every judge input is identical to the attested run's. Rejected: checking the attestation only at push (a regression is found after work is built on it), a full run on every commit (about 14 minutes). Research log entries 002 and 003.

### Step 2: a production small enough to trust (written after the build)

Three standard-library services on a Docker network with no route out; one edge proxy publishes an allow-list and never forwards a fault control. Ledger capacity is a parameter (slots times a sleep per write), not CPU speed, so a fault reproduces on any machine. Customers are an open loop: seeded arrivals sent on time whatever the gateway does, and a phase with more than 1% late sends is invalid. Three storm kinds from two faults, each with one fix that is a set of actions. Rejected: publishing each service's port (gives up "no route out"), CPU-bound work (faults would depend on the machine), a closed-loop load generator (it sends less exactly when the system struggles). Research log entries 006 to 012.

### Step 3: the loop (written after the build)

**Decision.** Code owns the loop, in the harness: at most 3 tries; each try is 1 or 2 typed actions applied together, then wait 30 s, verify on what customers saw, hold 30 s; then keep, or undo last first and try again with what failed, or the safe state. The verdict is the rule the storm controls were scored on, moved into the harness unchanged. An app supplies two ports: an environment (observe, apply an action and return its undo, the customer stream, the clock) and a decider (the model). The picture is the live page: Act, Verify, Undo and Safe state are built ([target design](../target-design.svg)).

**Blueprints searched (after the build, on 2026-11-25).**

| Pattern | Where it is established | What we take | Why not adopt the software |
|---|---|---|---|
| Metric analysis with automatic rollback | Argo Rollouts' AnalysisRun: a success condition per measurement, failure, inconclusive and error limits, abort and roll back | Verify after a wait on a measured condition, roll back on failure, and a hard bound so a run cannot hang | It rolls out Kubernetes workloads; simprod has no Kubernetes, and the actions here are runtime settings, not revisions |
| Remediation sequence | Keptn v1: an action, a delayed evaluation, then resolved or escalated | Act, wait, evaluate, then the next action or stop | End of life since December 2023, repository archived |
| Compensation in reverse order | The saga pattern, as Temporal documents it | Undo last first, each undo registered when its action is applied | It needs a workflow server; its gain, durability across a crash, is a gap below |

So the pattern is adopted and the code is ours: three modules, 468 lines with their documentation, standard library only. Rejected: a graph framework (no branching needed yet, and every path is tested with fakes as plain code); computing an undo after the action (only the moment of applying sees the state before).

**Gaps the search shows.** First, Argo Rollouts separates a failed measurement from a missing one (inconclusive). This loop does not: a second it could not measure counts against verification, so a scraper outage undoes a working fix and spends a try. The safer reading of "cannot measure" is the safe state, not a retry. Second, the loop's state is in memory: a crash mid-incident leaves actions in force that no record names. A durable record of each action before it is applied would close it; the action ledger planned for Day 8 is where it belongs.

**What would change this.** The loop needing branches a person must follow (parallel tries, a human step), then a framework earns its cost. A measured case where undo-and-retry does worse than the safe state after one failed try.

### Step 4: the trust boundary and the budget (written in the plan, before the code)

![Where each part runs after step 4: the experiment runner and Ollama on the host; the harness loop and the agent in their own containers in the VM; OpenBao holding the key; simprod with no route out](img/0008-step4-where-it-runs.svg)

Built in two parts: 4a the policy, the budget and the diagram tool; 4b OpenBao, the credential and the containers.

**Policy: a table as data, checked by plain Python in the harness.** The app declares one rule per allowed kind: whether it must be alone in its decision, and a precondition read from live state. The harness checks the whole decision before its first action is applied, so a refused decision changes nothing; it spends a try, and the next try is told every reason. FindyFixy's first rules come from entry 013: a rate cap only on a key sending at least 5% of the customers' request rate now, and a restart only alone. Rejected: Cedar through its Python binding (unofficial, license not found), OPA as a container (another service and a new language for three rules), no policy (the needless cap was kept 3 of 3 times).

**Budget: reserve the worst case before each model call.** Input tokens are bounded by the prompt's UTF-8 bytes plus a template allowance, because a byte-level tokenizer never makes more tokens than bytes; output by a cap on every call. An unpriced model or a call that does not fit what is left is refused, and the incident ends in the safe state. Rejected: counting with the model's tokenizer (it must match the one inside the model server exactly), pricing after the call (a report, not a precondition). The model server has no tokenize endpoint.

**Signing key: OpenBao's Transit engine, never exported.** The harness asks OpenBao to sign each one-action token; the gateway asks OpenBao to verify, so simprod stays standard library only (Python's standard library has no Ed25519). The image is OpenBao 2.7.2 with zlib and OpenSSL patched at pinned versions, as simprod's base was. Measured in dev mode on the unpatched 2.7.2 image: 17 to 21 MiB, and 1.2 ms a signature at the median. Rejected: an HMAC key file with a small signer (recommended first: no new image, but no rotation or audit log), a software HSM (SoftHSMv2 driven from Python: viable, with rotation and audit written by hand), a key in the agent's process.

**Containers for the acting path only.** The agent becomes a small decider service with no Docker socket, no keys and no route to simprod: it gets the metrics as text and returns a proposal. The loop, its scraper and its policy run in their own container. The experiment runner stays on the host because it needs the Docker socket, and Ollama stays native for the GPU. Torch stays native too: the acting path does not use it, and a full app image in the VM measured 416 MB, about 1.4 GiB, and 7 to 9 times slower embedding and reranking on the VM's 2 CPUs. Rejected: every part in containers (the inner loop is slower and every timing changes), only OpenBao in Docker (the boundary stays a convention).

**Decision diagrams: one spec file per picture.** A standard-library tool draws each picture from its spec with the live page's icons; the ADR shows it and the console's page embeds a copy; `make check` fails if either differs from a fresh render. Rejected: growing the live page itself (it draws the logical flow, not where things run), Mermaid (no Lucide icons).

**What would change this.** Policies that people outside engineering must review: then the table moves to a policy language. Prompts large enough that the byte bound refuses calls that would fit: then an exact count. OpenBao failing its rescan or its sign-and-verify check after patching: then the software HSM.
