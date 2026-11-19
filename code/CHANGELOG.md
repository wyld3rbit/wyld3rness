# Changelog

Notable changes to wyld3rness and its example app. The format follows [Keep a Changelog](https://keepachangelog.com/en/1.1.0/), and versions will follow [Semantic Versioning](https://semver.org/) once releases begin. Each entry links the decision record that explains it.

## Unreleased

### Harness

- **Eval gate.** Runs an app on golden cases with known answers. Labels such as severity are matched exactly, and a judge model scores faithfulness, response relevancy, context recall and action quality. Each run is compared with a frozen baseline, with seeded bootstrap intervals, and fails on a measured regression or a judge failure rate above its cap. A baseline frozen on a different system (the app's `system` in provenance) is refused as not comparable. A negative control can never become a baseline. ([ADR 0005](docs/adr/0005-gate-releases-on-measured-answer-quality.md))
- **One interface for any app.** `SystemUnderTest` (`describe`, `answer`, `summarise`) separates the harness from the app. A test fails if the harness imports anything but the standard library, its declared dependencies and itself. ([ADR 0006](docs/adr/0006-eval-gate-as-harness-component-attested-in-ci.md))
- **Attested CI gate.** A full, clean, passing run writes an attestation: the hash of every answer-affecting file, found from the eval's import closure plus declared data, lock and compose files. CI recomputes the hashes in its own job and fails a change no passing run covers. ([ADR 0006](docs/adr/0006-eval-gate-as-harness-component-attested-in-ci.md))
- **Tracing.** Every run, eval case and command is an OpenTelemetry trace in a self-hosted Langfuse, with a span per step, eval scores on their case, and a cost on each model call from a committed reference price table. Export is batched on its own thread, so a slow or stopped backend does not slow a run. ([ADR 0007](docs/adr/0007-every-run-is-a-trace-in-a-self-hosted-backend.md))

### Example app: FindyFixy

- **Small model first.** A small model answers, and a larger one takes the case when the answer is invalid after one retry or its confidence is low. ([ADR 0001](docs/adr/0001-route-small-model-first.md))
- **Output contract and tools.** Every answer is a validated record with one retry that carries the validation error. Tool arguments are validated inside the tool, and the deploy tool runs both directly and over MCP. ([ADR 0002](docs/adr/0002-validate-outputs-and-tool-arguments.md))
- **Incident memory.** Synthetic past incidents are chunked, embedded and stored in Postgres with pgvector by an ingestion job that is safe to re-run. ([ADR 0003](docs/adr/0003-incident-memory-in-postgres.md))
- **Hybrid retrieval.** Keyword and vector search fused by reciprocal rank, then a cross-encoder reranker, returning incidents rather than chunks. ([ADR 0004](docs/adr/0004-hybrid-retrieval-with-reranker.md))
- **Console.** A local page that runs a triage or an eval and shows each step on the live architecture diagram.
- **Versioned data.** The corpus, golden set and deploy log live in `data/v1/` (frozen, the gate's set) and `data/v2/` (30 hard cases: no precedent, conflicting signals, cascades, noisy alerts, poisoned incidents with injected instructions, and destructive rollbacks). Each version has its own database table. `make eval DATA=v2` measures v2 through its own command, which the v1 attestation does not cover, and never runs the gate. The console's Eval run panel picks any set; one loaded embedder and reranker serve them all. Each case record now carries the model's self-reported confidence.
- **Change log on a timeline (data v3).** `data/v3/` holds v2's cases, each with the moment its alert fired, and a change log that records every change kind (deploy, config, flag, key rotation) at an absolute time, with whether each deploy is safe to roll back. On v3 the agent's change tool looks back from the case's own alert time and never sees a later change. In v1 and v2 every case read one deploy log against one shared moment, so a rollback rule could not tell a bad deploy (h26) from a key rotation right after a harmless deploy (h23). `make eval DATA=v3` measures it, scores only. v1's prompt and tools are unchanged.
- **Agent.** FindyFixy is now an agent that investigates before it decides, and the agent is the system under test: the eval, the gate's baseline and the console run it. The model runs a tool loop with three read-only tools: incident search (recall as a tool, so the model chooses the query and whether to search), what changed in a service (the deploy log on v1 and v2, the change log as of the alert on v3), and a service map of what each service depends on. It states a hypothesis before each lookup and finishes by submitting a validated decision as a tool call: severity, component, next action, and an action from a closed set (read, page a human, or roll back to a named version). A separate decision call is the fallback when it ends with plain text. The loop stops at 6 turns or 25 seconds. The deadline also cuts off a reply still streaming when time runs out, closing the connection so the model stops generating; out of budget, code replaces the action with paging a human and records why. Nothing the agent proposes is executed. The agent runs on Qwen3 8B alone, in both tiers: at the same severity accuracy, Qwen3 4B proposed far more rollbacks that the cases did not call for. An eval loads each model before the first case, so a cold load never uses up case 1's deadline. `make eval FIXED=1` runs the earlier fixed path for comparison, scores only. The deploy tool now accepts every service, answering an empty list for one with no deploys.

### Tooling and security

- Locked dependencies with a 7-day release cooldown, `make audit` for known vulnerabilities and unexpected startup hooks, and container images pinned by digest and scanned with Grype before first use.
- A privacy scanner in the pre-commit and pre-push hooks blocks personal data and secrets in files and commit metadata.
- The attestation is verified at push time and in CI. It used to be checked at commit time too, which blocked committing a change to attested code: the gate can only attest a committed tree.
- Ruff and mypy in the hooks and in CI. CI runs with a read-only token and no secrets, with actions pinned by commit.
- `make check` covers documentation drift, the repository tools' tests and the privacy scan.

### Known issues

- Linux installs pull PyTorch's GPU libraries although nothing uses a GPU, which slows CI. CPU-only wheels are planned with the service image.
- The model names come from an untracked `.env` file, so a model swap changes answers without failing CI. Only the judge is pinned, through the baseline.
- The stale-database check catches a changed embedding model name, not a new revision of the same model.
- The golden sets are small and synthetic: 30 cases each, so one run's per-group differences are within noise.
- `make ingest` embeds v1's 800 chunks again for each later version's table (v2, v3), because each version is loaded on its own.
- Action quality is judged against the incident's known fix, while the agent proposes the next operational step, so the agent's baseline scores it low and its floor is low. The judge's action rubric is to be rewritten for the agent's job, then the baseline refrozen.
- The Day 2 tool step (`findyfixy-direct-run`, `findyfixy-mcp-run`) reports a turn-cap stop from the messages, not from the loop's own stop reason. The two agree on that loop, which has no deadline and no submit tool; the agent path uses the loop's reason.

## History

The step-by-step build log up to 2026-11-13, including the image scans and exceptions that earlier records cite, is preserved at [this commit](https://github.com/wyld3rbit/wyld3rness/blob/19f5c7a73ff58bfb98ad0afcb98374b1e8c360bd/code/CHANGELOG.md).
