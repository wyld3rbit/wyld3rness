# Design and strategy

**wyld3rness is a harness for LLM agents that run on-call with no person approving each action, inside a budget.** It is tested on FindyFixy, which acts only on a simulated production. Each phase's choices, with the options rejected, are in the [decision records](adr/README.md); every measurement is in the [research log](research-log.md).

![The target design: what people set before runtime, the harness, the triage agent, the two proposers (a model that reads the metrics and a script with no model), the act, verify and undo loop, and the simulated production. Solid is built, dashed is planned.](target-design.svg)

## The idea

An agent will sometimes be wrong with full confidence, and the text it reads can be hostile. So the design makes it **safe and affordable to be wrong**: a wrong action is limited before it runs, detected on what customers saw, undone, and paid for from a budget that cannot be exceeded.

| Who | Decides | When |
|---|---|---|
| People | The goal, the allowed kinds of action, the budget, the eval gate | Before runtime |
| Harness, in code | Whether an action may run, whether it worked, undo, stop | Every step |
| A proposer (a model or a script) | Which typed action to try | Once per try |
| The system | Whether customers recovered | After every action |

A proposer can change none of the first two rows.

## The harness

![The harness: what people set before runtime; the eval gate, the CI check and tracing at every commit and model call; and for every incident, the policy, a one-time pass, the action, the recovery check, keep or undo, the safe state, and the hand-off to a person. Proposers, a script or the app's model behind a budget, feed the policy; the app's environment port applies actions and reports what customers saw.](img/harness.svg)

The harness never imports the app: each control is a port the app implements or calls. The ports and how to plug in an app are in the [harness README](../src/wyld3rness/README.md). Where each part runs, and what each container cannot reach, is drawn in [ADR 0008](adr/0008-autonomous-operation-with-cost-as-an-invariant.md#step-4-the-trust-boundary-and-the-budget-written-in-the-plan-before-the-code).

## Guiding policies

1. **Never act on what the model says about itself.** Actions, retries and stops are decided from facts the model cannot write.
2. **Set the bar before the run.** A change is adopted on a measurement whose bar was written first; a rejection is a result.
3. **Every rule is a check that fails.** A test, hook or CI job breaks when the rule is broken.
4. **The harness never depends on the app.** A second app needs an adapter, not a harness change.
5. **Open source, local and pinned, blueprint first.** Custom code covers only what an established blueprint lacks.
6. **Unknown is not zero.** A failed measurement is excluded and counted, never scored as 0.
7. **Cost is an invariant.** Cost per resolved incident counts every attempt, failed ones included. Among options that pass the safety and resolution bars, the cheapest wins, and a script with no model is always one of them.
8. **Code owns the loop.** The proposer makes one decision; code decides whether it may act, whether it worked and when to stop.

## Invariants

A control counts as built only when a check fails if someone goes around it.

| Invariant | Status |
|---|---|
| No change that can alter an answer ships without a passing, attested eval | built |
| Every run, model call and decision is a trace, with its cost | built |
| The harness never imports the app | built |
| No action runs unless its kind is allowed and its preconditions hold when it runs | built |
| No credential outlives one action | built |
| Outside text is data, never instructions | built |
| Nothing is called fixed until customers recover and stay recovered | built |
| Every incident ends resolved or in a safe state | built |
| No incident spends past its budget | partly built: the on-call proposer's calls only |
| A safe state hands the incident to a person | built |
| No model call bypasses one gateway | planned |
| Autonomy of each kind of action follows its evidence | planned |

## How a proposer earns its place

![The experiment: a protocol committed first; held-out storms, each through four options in random order (nobody, the script, the model with one try, the model with up to three); every option that acts goes through the same boundary; customers are measured by the host; customer minutes lost per run feed a paired analysis with repeats for noise; the decision rule written in advance is applied.](img/experiment.svg)

Every proposer meets the same storms, unseen while it was built, in random order, through the same boundary. What counts is the customer minutes lost in a fixed window after the fault, measured outside the loop. The prediction and the decision rule are committed before the run. If the model does not beat the script, the script keeps those incidents. Protocol and results: [research log](research-log.md), entries 019 and 020.

## Autonomy as policy

The target is Level 3: the agent acts, rolls back and writes the postmortem, and people set and review the rules rather than approve each action. A person approves each kind of action once, and evidence from the simulated production keeps or removes it.

| Kind | Example | Level 3 requires |
|---|---|---|
| Can be undone | revert config, add capacity | a tested undo and a recovery check |
| Cannot be undone, limited harm | flush a cache | a scope and rate limit, and a recovery check |
| Can do lasting damage | delete logs | a precondition code can prove, or the safe state |

## The release path

```
git commit ─► export the staged tree ─► eval run (unchanged answers keep their scores)
           ─► gate fails on a regression ─► attestation added to the commit
git push   ─► every pushed commit's attestation checked ─► CI recomputes it, refuses an uncovered change
```

Why at commit and not in CI: [ADR 0008, step 0](adr/0008-autonomous-operation-with-cost-as-an-invariant.md#step-0-the-gate-at-commit-written-after-the-build). Why CI checks an attestation instead of running the eval: [ADR 0006](adr/0006-eval-gate-as-harness-component-attested-in-ci.md).

## The repository

```
code/
├── src/wyld3rness/          the harness: never imports an app
│   ├── eval/                eval gate, judge, attestation
│   ├── trace/               spans, scores, cost
│   └── loop/                the incident loop, policy, pass, budget, verdict, script, experiment
├── examples/findyfixy/      the agent under test: triage, on-call proposer, console
├── examples/simprod/        the simulated production it acts on
├── infra/                   self-hosted services
└── docs/                    this page, the decision records, the research log
tools/                       public checks: docs drift, privacy scan, pictures
```

| Concern | Choice | License |
|---|---|---|
| Models | Qwen3 and Qwen2.5, served by Ollama | Apache 2.0, MIT |
| Agent graph | LangGraph | MIT |
| Validated output | Pydantic | MIT |
| Incident memory | Postgres with pgvector | PostgreSQL |
| Tracing | OpenTelemetry into self-hosted Langfuse | Apache 2.0, MIT |
| Signing | OpenBao Transit | MPL 2.0 |
| Simulated production | Python standard library, Docker Compose | PSF |

## Next

1. **Stop the bleeding fast:** a runbook acts at once on known signatures while the agent diagnoses; fixes go to a small slice first; the model has a decision deadline; check windows are sized to traffic; every model call goes through one gateway that enforces the budget.
2. **Every human fix recorded:** the agent reads everything and changes only through typed actions; people act through the same recorded tools; each resolved incident becomes a replayable storm and an incident-memory case.
3. **Autonomy earned by evidence:** a new kind of change, found by a person or proposed by the agent, is tested on its replayed storm, approved once, and kept or removed by evidence; faults outside the runbook's catalogue test whether the model earns its place.

## Limits

One machine, one engineer, synthetic data and a simulated production. Nothing here shows safety on real systems.
