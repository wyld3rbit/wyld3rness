# FindyFixy

The agent the wyld3rness harness is tested on: an event detection, determination and mitigation agent, meant to run on-call without a human approving each action. Today it diagnoses, and acts only on the simulated production, through the harness's loop (`make oncall`). It is a uv workspace member with its own `code/examples/findyfixy/pyproject.toml`, which declares its libraries (the on-call core, and an optional `triage` extra with retrieval, the router graph and MCP, which every local environment installs), its dependency on the harness and its commands (`findyfixy-console`, `findyfixy-eval` and the rest; the Makefile wraps the common ones).

Beside the package: `code/examples/findyfixy/tests/` (its tests), `code/examples/findyfixy/data/` (the synthetic corpus, golden set and deploy history), `code/examples/findyfixy/evals/` (the frozen baseline and the attestation CI checks), `code/examples/findyfixy/config/prices.toml` (reference prices per token) and `code/examples/findyfixy/docker-compose.yml` (Postgres with pgvector). Settings go in `code/examples/findyfixy/.env`, copied from `code/examples/findyfixy/.env.example`.

The package is `code/examples/findyfixy/src/findyfixy/`. Dependencies point inward: only entry points and `code/examples/findyfixy/src/findyfixy/observability/tracing.py` import the composition root `code/examples/findyfixy/src/findyfixy/wiring.py` or `code/examples/findyfixy/src/findyfixy/settings.py`, and `code/examples/findyfixy/tests/test_architecture.py` enforces it.

**Router and triage**
- `code/examples/findyfixy/src/findyfixy/triage/graph.py`: the small-model-first router graph.
- `code/examples/findyfixy/src/findyfixy/triage/record.py`: the triage record and validate-retry for model output.
- `code/examples/findyfixy/src/findyfixy/tools/investigate.py` and `code/examples/findyfixy/src/findyfixy/tools/deploys.py`: the tool-calling loop and the deploy-history tool.
- `code/examples/findyfixy/src/findyfixy/events.py`: the run events the graph reports.
- `code/examples/findyfixy/src/findyfixy/observability/adapter.py`: those events as OpenTelemetry spans, through the harness's `SpanTree`. The console turns it on when tracing is configured (`open_tracing` in `code/examples/findyfixy/src/findyfixy/observability/tracing.py`); `RunTracer` does the same for eval cases, which share one emitter across threads. `traced_sink` gives the console's event log a `run_traced` event per run, with the link to its trace.
- `code/examples/findyfixy/src/findyfixy/llm.py`: the one place that calls the answering model.

**Memory**
- `code/examples/findyfixy/src/findyfixy/retrieval/corpus.py` and `code/examples/findyfixy/src/findyfixy/retrieval/ingest.py`: build and ingest the synthetic incident corpus.
- `code/examples/findyfixy/src/findyfixy/retrieval/search.py` and `code/examples/findyfixy/src/findyfixy/retrieval/retrieve.py`: nearest-chunk search and the three retrievers.
- `code/examples/findyfixy/src/findyfixy/retrieval/incidents.py`: the small hand-written incident bank.
- `code/examples/findyfixy/src/findyfixy/model_cache.py`: loads the pinned embedder and reranker from the local cache, and downloads only when the cache does not hold them.

**Evaluation**
- `code/examples/findyfixy/src/findyfixy/evals/golden.py`: the golden set, built from the corpus.
- `code/examples/findyfixy/src/findyfixy/evals/adapter.py`: FindyFixy as a system under test for the harness in `code/src/wyld3rness/`: holdout recall, the severity label and the twin data.
- `code/examples/findyfixy/src/findyfixy/evals/answers.py`: the answer evaluation's entry point. It builds the live adapter and hands it to the harness command line. It also declares what the CI gate attests: the entry module whose imports make up the eval path, the data directory, the baseline, the lock file and the compose file, and the database check that stops a gate run on a stale ingest.
- `code/examples/findyfixy/src/findyfixy/evals/queries.py` and `code/examples/findyfixy/src/findyfixy/evals/retrieval.py`: the labelled queries and the retrieval evaluation.

**On call**
- `code/examples/findyfixy/src/findyfixy/oncall/env.py`: the harness's environment port over simprod's edge: the metrics an operator reads, the three typed actions with the undo that puts back the value from before, and the customer stream from the storm driver's scraper.
- `code/examples/findyfixy/src/findyfixy/oncall/decide.py`: the decider: the model reads the metrics, the policy's rules and earlier tries and proposes 1 or 2 actions, validated, with one retry that carries the error; each call reserves its worst case on the incident's budget first and has its reply capped at 400 tokens.
- `code/examples/findyfixy/src/findyfixy/oncall/policy.py`: FindyFixy's policy table: backoff only turned on; a rate cap only on a key sending at least 5% of the customers' request rate now, read from the gateway's busiest-keys gauge; a restart only alone in its decision and only for a known service whose health check clearly fails, twice.
- `code/examples/findyfixy/src/findyfixy/oncall/service.py`: the agent and the loop as services, each in its own container (`code/examples/findyfixy/Dockerfile`, profile `oncall` in `code/examples/findyfixy/docker-compose.yml`, `make oncall-up`): the agent reaches only Ollama, with no socket, keys or route to simprod; the loop scrapes what customers saw, checks the policy, signs through OpenBao and acts through the edge.
- `code/examples/findyfixy/src/findyfixy/oncall/attacks.py`: the 20 injection attacks, each a valid account key planted in the busiest-keys line, and `make injection`, which runs them through a fully fooled fake and the real model in the agent container.
- `code/examples/findyfixy/src/findyfixy/oncall/script.py`: the script with no model: backoff, cap the busiest key at a twentieth of the customers' rate, or both, read from the same metrics the model reads; the loop container runs it when an incident asks for it.
- `code/examples/findyfixy/src/findyfixy/oncall/experiment.py`: `make experiment`, every held-out storm through nobody, the script, the model with one try and the model loop, measured over the same 240 s, then the analysis; `make experiment-power` and `make experiment-analyze`.
- `code/examples/findyfixy/src/findyfixy/oncall/cli.py`: `make oncall`, seeded storms run by simprod's storm driver on the host, each incident handled by the loop container; one line each and a results file in `code/examples/simprod/results/`.

**Entry points and wiring**
- `code/examples/findyfixy/src/findyfixy/wiring.py`, `code/examples/findyfixy/src/findyfixy/settings.py` and `code/examples/findyfixy/src/findyfixy/paths.py`: the composition root, the configuration read from the environment, and where files live.
- `code/examples/findyfixy/src/findyfixy/observability/tracing.py`: `command_trace`, how each command (triage, retrieval eval, ingest, search, the tool paths) becomes a trace, with the tracer built from settings. The MCP server and the console build their provider at startup and use the harness's `CommandTrace` per tool call or request. `code/examples/findyfixy/src/findyfixy/wiring.py` `stage_spans` turns the retriever's stages into spans.
- `code/examples/findyfixy/src/findyfixy/observability/prices.py`: `make obs-prices`, which registers `code/examples/findyfixy/config/prices.toml` with Langfuse through the harness's `code/src/wyld3rness/trace/prices.py`.
- `code/examples/findyfixy/src/findyfixy/console/server.py`, `code/examples/findyfixy/src/findyfixy/console/fakes.py`, `code/examples/findyfixy/src/findyfixy/console/dev_reload.py` and `code/examples/findyfixy/src/findyfixy/console/stress.py`: the live console, its scripted stand-ins, hot reload and stress settings.
- `code/examples/findyfixy/src/findyfixy/console/static/architecture.html`: the page the console serves, the live picture of what runs and of the planned loop around it. Each box's tooltip says what it does, and `code/examples/findyfixy/tests/test_page_truth.py` keeps the boxes true to the code.
- `code/examples/findyfixy/src/findyfixy/console/eval_run.py`: the console's eval run. It streams the harness's events next to the graph's for each case, and applies the gate read-only. It writes no report.
- `code/examples/findyfixy/src/findyfixy/mcp/server.py`, `code/examples/findyfixy/src/findyfixy/mcp/client.py`, `code/examples/findyfixy/src/findyfixy/mcp/run.py` and `code/examples/findyfixy/src/findyfixy/tools/direct_run.py`: the tool served over MCP and the two measured paths to it.
