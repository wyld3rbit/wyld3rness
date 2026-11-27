# wyld3rness

The harness. It gates releases on measured answers, traces every run, and runs each incident through a boundary the agent cannot get around. It knows nothing about any one app; FindyFixy is its first.

![The harness: what people set before runtime; the eval gate, the CI check and tracing at every commit and model call; and for every incident, the policy, a one-time pass, the action, the recovery check, keep or undo, the safe state, and the hand-off to a person. Proposers, a script or the app's model behind a budget, feed the policy; the app's environment port applies actions and reports what customers saw.](../../docs/img/harness.svg)

## Plug in an app

An app implements these ports. FindyFixy's implementation of each is linked.

| Port | What the app supplies | FindyFixy |
|---|---|---|
| `SystemUnderTest` (`code/src/wyld3rness/eval/sut.py`) | `describe`, `answer(case)`, `summarise`: the eval gate runs the app through it | `code/examples/findyfixy/src/findyfixy/evals/adapter.py` |
| Event adapter (`code/src/wyld3rness/trace/spans.py`) | opens and closes spans as the app's steps happen | `code/examples/findyfixy/src/findyfixy/observability/adapter.py` |
| `Environment` (`code/src/wyld3rness/loop/env.py`) | `observe`, `apply(action, credential)` returning its undo, the customer series, the clock | `code/examples/findyfixy/src/findyfixy/oncall/env.py` |
| `Decider` (`code/src/wyld3rness/loop/run.py`) | `decide(observation, history)`: 1 or 2 typed actions | `code/examples/findyfixy/src/findyfixy/oncall/decide.py` |
| `Policy` of `Rule`s (`code/src/wyld3rness/loop/policy.py`) | each allowed kind: alone or not, a precondition read from live state | `code/examples/findyfixy/src/findyfixy/oncall/policy.py` |

The harness supplies the rest: the loop (`run_incident`), the one-time pass (`Signer`, signed in OpenBao), the hand-off to a person (`Handoff`, an Alertmanager webhook, with an optional bearer token), the agent edge (`code/src/wyld3rness/loop/edge.py`: an allow-list forwarder for an agent on an internal network; the app lists its `Door`s), the boundary probe (`code/src/wyld3rness/loop/probe.py`: what an agent's container can reach; the app lists its routes and ports), the budget, the recovery verdict, the script baseline, the attack suite, the experiment's statistics, and wrappers that trace and stream every step. `code/src/wyld3rness/testing.py` has fakes for all of it, so an app's tests run with no model.

## The loop in one picture

```
observe ─► decide ─► policy ─► pass ─► apply ─► wait ─► customers recovered and held? ─► keep
                                                          no ─► undo ─► next try, told what failed
out of tries, budget, an error, or an action with no undo ─► safe state: undo all, stop ─► hand off to a person
```

## Add a control

A new control is a port in this package, never code inside an app. Give it a fake in `code/src/wyld3rness/testing.py`, a test that fails when the control is bypassed, and a box on the console's architecture page.

## Rules the tests enforce

- Nothing here imports an app, and no module works out a path from its own location (`code/tests/test_harness_rules.py`).
- Only the dependencies in `code/pyproject.toml` are imported.
- A change to the eval path needs a passing eval run and its attestation in the same commit (`code/src/wyld3rness/eval/attest.py`).

## Modules

| Folder | Holds |
|---|---|
| `code/src/wyld3rness/eval/` | runner, judge and metrics, report and gate, attestation, gate at commit, score reuse |
| `code/src/wyld3rness/trace/` | OpenTelemetry tracing, spans, scores, prices |
| `code/src/wyld3rness/loop/` | the incident loop, policy, pass, budget, verdict, remote decider, agent edge, boundary probe, script, attack suite, experiment statistics, traced and watched wrappers |

Each module's docstring gives its trade-offs and failure modes. Tests: `code/tests/` (`code/tests/README.md`).
