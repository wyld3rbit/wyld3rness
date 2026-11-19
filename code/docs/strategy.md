# Engineering strategy

This is the strategy for wyld3rness. The decision records in `adr/` are its design documents, and this page draws out the decisions that recur across them. A vision written at the end of the build projects it forward. It has three parts: a diagnosis of how things work today, the guiding policies, and the actions that enforce, escalate and change them. [`design.md`](design.md) is the target shape the transitions move toward.

## Diagnosis

The harness wraps a model whose output varies from run to run and whose statements about itself have not held up. The evidence is consistent across three phases, and it is what the strategy rests on.

The routing design assumed the small model would flag its own weak answers, so the large model would take the hard cases. In the first five runs the model's confidence did not even keep one scale: two answers used 0 to 1 and three used 0 to 100. The highest rating went to one of the worst answers (ADR 0001). Validation fixed the scale but not the judgement. A tool call passed every check in all of its runs and gave the right answer in none of them, because validation catches shape and not meaning (ADR 0002). In the Day 6 runs the escalation rate was 0%, and the weakest case still reported a confidence above the threshold (ADR 0007). On the hard cases of Day 7, the smaller model's unneeded rollbacks were confident ones too. A routing rule that never fires is not a control, so both tiers now run one model, and the case for two models moves from escalation on confidence to roles: cheap specialists for narrow questions, one large coordinator for the decision (transition 3).

Three controls work today: the eval gate, the CI gate that refuses an unmeasured answer-affecting change, and a trace for every run (ADRs 0005 to 0007). What they share is that each choice was made against a number set in advance, including the choices to say no. An evaluation library was turned down although it would have saved writing the metrics, because it brought 31 new packages and a vulnerability with no fix (ADR 0005). Batching the judge's verdicts would have cut about a third of the judge's calls per case, derived from the per-case counts in ADR 0006. It was rejected, because its agreement came in 6 points under a 0.9 bar set before the run. The cheaper path was available both times, and the measurement said no. Where the measurement said yes, the gain was taken: running every answer before any judging cut the gate's wall time by 22% with no score changed (ADR 0007).

Four gaps remain. First, the agent only proposes: it submits a decision from a closed set (read, page a human, roll back to a named version), and no step in the graph acts on it. That is safe today only because nothing is connected, so there is no policy yet between a proposal and a system, and nothing that would keep two agents' actions from colliding. Second, model clients are created in several modules of both the harness (the judge) and the app (the answer call, the agent's builder, the console, the tool demos). So there is no single place that could enforce a budget, a role or a model pin. Third, the evaluation data is small and synthetic: the frozen set is 30 cases, two thirds of them built around a near-duplicate in memory, and the hard sets added on Day 7 (v2, v3) are 30 cases each, so a per-group difference is within noise. Fourth, every control trusts one developer: the attestation is a process control, and the model names come from an untracked `.env` file (ADR 0006).

## Guiding policies

Each policy names its trade-off and the decision records it comes from.

1. **Never act on what the model says about itself.** Validate every output against a contract, and compute escalation and action decisions from facts the model cannot write. Trade-off: a second signal costs compute and design time, and early answers route more slowly. From ADRs 0001, 0002 and 0007.
2. **Set the bar before the run, and record a rejection as a result.** A change is adopted on a labelled measurement, with its bar written down first; one run is an anecdote, and the spread is reported. Trade-off: every decision waits for a labelled set and a measured run. From ADRs 0004 to 0007.
3. **Turn every rule into a check that fails.** A rule that matters gets a test, a hook or a CI job that breaks when it is broken, and that check is proven to fail once. Trade-off: each check is code to maintain, and a process check still trusts whoever runs it. From ADRs 0006 and 0007.
4. **The harness never depends on the app.** Each control is a port the app implements or calls, so a second app needs an adapter and no change to the harness. Trade-off: every control needs an interface and an adapter, which is more code than wiring it into one app. From ADRs 0006 and 0007.
5. **Open source, local and pinned, with an established blueprint first.** Every dependency carries an OSI license, a pinned version or digest and an audit; custom code covers only what the blueprint lacks. Trade-off: smaller local models, no GPU in CI, and slower adoption of new releases. From ADRs 0003, 0005, 0006 and 0007.
6. **Unknown is not zero.** A failed judge call is excluded and counted, a missing token count is left out, and a negative control can never become a baseline. Trade-off: reports carry gaps and counts that a single number would hide. From ADRs 0005 and 0007.

Each policy is held to three tests. It is applicable, because it has already decided a real trade-off in the records above. It is enforced, by the checks below. And its effect compounds, because the same rule carries into every later control.

## Coherent actions

### Enforcements

These are the invariants the harness holds for every request. A control counts as built only when a check fails if someone goes around it.

| Invariant | Policy | Check that enforces it | Status |
|---|---|---|---|
| No change that can alter an answer ships without a passing, attested eval | 2, 3 | CI rehashes every answer-affecting file and fails a change no passing run covered | built |
| Every request, model call, tool call and decision is a trace, with its eval scores attached | 3, 6 | Tests read the spans a run emits; a run with the backend down keeps its speed | built |
| The harness never imports the app | 4 | A test fails if the harness imports anything but the standard library, its declared dependencies and itself | built |
| Retrieved and user text is data, never instructions: no model output acts until the action ledger's deterministic policy approves it, whichever agent proposed it | 1 | An injection suite of attack prompts and poisoned incidents, with every proposer fooled, with 0 unauthorized actions, in CI | planned |
| Two actions whose blast radii overlap never run together, and at most one destructive action is in flight per incident | 1, 3 | A test sends overlapping proposals from two agents and sees the second queued or refused | planned |
| Every tool call carries its own token, scoped to one agent, one tool and one resource, which expires in five minutes | 1, 3 | A token used by the wrong agent, on the wrong tool or resource, or after expiry is refused | planned |
| No model call bypasses the gateway, which enforces budgets per agent and per team, roles and the model pins | 3, 5 | An architecture test fails if any module other than the gateway imports a model client | planned |
| No destructive action runs without a human, unless autonomy for that class was earned on evidence | 1, 2 | A destructive action pauses until an approval record exists | planned |
| Quality is measured in operation, not only before release | 2 | A scheduled run below the gate raises an alert, and once autonomy exists, lowers it | planned |
| The authority of each action class is a function of evidence, in code | 1, 2 | Changing the evidence changes the level; no setting raises a level by hand | planned |

### Escalations

A policy can be challenged, and the challenge has one path. An exception needs the project owner's approval and an entry in the decision log that names the policy, the reason and what was rejected. The release cooldown of policy 5 has been overridden this way. Postgres 17.11 was taken for the tracing stack 3 days into its cooldown, as a named exception recorded on 2026-11-09. An exception that recurs is a signal to change the policy, not to keep granting it.

### Transitions

The gaps in the diagnosis close in this order, each recorded in the decision record of the phase that builds it:

1. **Harder data and an agent that investigates.** A versioned corpus and golden set with no-precedent alerts, conflicting signals, cascades and noisy alert text, with the current set frozen for regression, and a version on a real timeline whose change log records every change kind. The agent gathers its own evidence with read-only tools inside a turn and time budget and proposes a decision from a closed set; running out of budget hands the incident to a person, in code. A computed escalation signal was measured first and missed its bar. Narrows the third gap and applies policy 1.
2. **A trust boundary built for many agents.** An action ledger between every proposed action, from any agent, and any system: the policy, a lock per blast radius and one destructive action in flight per incident. Short-lived credentials per tool call, scoped to the agent that calls. Closes the first gap.
3. **One path to every model, and the team.** All model clients move behind a gateway that enforces budgets per agent and per team, roles and pins. The agent becomes a coordinator with read-only specialists that investigate in parallel, kept only if it beats the single agent on measured quality or cost. Closes the second gap.
4. **Operation.** The service, human approval for destructive actions, actions executed one at a time with a check that the signal recovered, and scheduled evaluation in operation.
5. **Autonomy as policy.** The level each action class runs at is set from measured evidence, and the vision is written.

The fourth gap, trust in one developer, stays open by design while there is one developer. It closes when a second developer or an outsider must trust a merge, and a real run on a GPU runner replaces the attestation (ADR 0006).

## Review

Each phase's decision record reports whether this strategy held: which policy it applied, and any evidence against a policy or the diagnosis. If several policies fail their tests, the diagnosis is re-examined first.
