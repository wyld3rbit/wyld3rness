# Testing

## The harness's tests

The tests of the `wyld3rness` harness. They drive it with the fakes in `code/src/wyld3rness/testing.py`, a judge, an embedder and a system under test that is not FindyFixy, so they need no model, database or network. `make test` runs them with the example app's tests. `make test-harness` runs them alone, in a throwaway environment that has no app installed, which proves the harness stands on its own.

- `code/tests/test_harness_rules.py` enforces the harness's architecture: it imports only the standard library, its declared dependencies and itself, has no import cycles or relative imports, and never works out a path from its own location.
- `code/tests/test_attest.py` tests the CI gate on temp trees, including a workspace with two source roots and the command line run alone with no site-packages.
- `code/tests/test_runner.py`, `code/tests/test_metrics.py`, `code/tests/test_judge.py`, `code/tests/test_report.py`, `code/tests/test_stats.py`, `code/tests/test_provenance.py` and `code/tests/test_cli.py` cover the eval runner, the metrics and their judge, the report and baseline rules, the statistics, the run's provenance and the command line.
- `code/tests/test_trace.py` tests the span tree and provider with an in-memory exporter, including that a hung or refusing backend does not slow the code that makes spans. `code/tests/test_eval_tracing.py` covers eval cases as traces with their scores.
- `code/tests/test_prices.py` covers the price table parser and its sync to Langfuse, and `code/tests/test_langfuse_env.py` checks the Langfuse secrets writer against its compose file.

## FindyFixy's tests

The example app's tests, next to the behaviour they cover. They use fakes, so they need no model, database or network, and they run with `make test`. The harness's own tests are in `code/tests/`.

- `code/examples/findyfixy/tests/test_architecture.py` enforces the dependency rule: domain modules do not import vendor clients or the composition root, and only `code/examples/findyfixy/src/findyfixy/paths.py` works out where files live.
- `code/examples/findyfixy/tests/test_eval_adapter.py` tests FindyFixy as a system under test, with the harness's fakes from `code/src/wyld3rness/testing.py`, and `code/examples/findyfixy/tests/test_eval_legacy_reports.py` its pre-harness reports.
- `code/examples/findyfixy/tests/test_eval_attestation.py` checks what FindyFixy declares to the CI gate: the eval path holds no console module, every data file it reads is declared, and the Makefile and the workflow verify the file the gate writes.
- `code/examples/findyfixy/tests/test_trace_adapter.py` runs every fake console scenario through a traced console and checks the tree. `code/examples/findyfixy/tests/test_observe_everything.py` covers command traces, `user.id` on top spans, hunt sessions, the console's request traces, a tool path run and an MCP tool call.
- `code/examples/findyfixy/tests/test_page_truth.py` keeps the architecture page honest: each box's `data-code` must match the code (built means it exists, planned means it does not), and every event kind the code can emit must have a handler on the page.
- `code/examples/findyfixy/tests/js/replay.js` plays events through the page's own script, for `code/examples/findyfixy/tests/test_page_lag.py` and `code/examples/findyfixy/tests/test_page_truth.py`.
- `code/examples/findyfixy/tests/integration/` needs a live model and database, so `make test` leaves it out.
