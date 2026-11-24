# Changelog

Notable changes to wyld3rness and its example app. The format follows [Keep a Changelog](https://keepachangelog.com/en/1.1.0/), and versions will follow [Semantic Versioning](https://semver.org/) once releases begin. Each entry links the decision record that explains it.

## Unreleased

### Direction

- **Autonomous operation, with cost as an invariant.** The target is on-call without a human approving each action: an act, verify and undo loop on a simulated production, a budget per incident and cost per resolved incident. The order of the four planned controls is kept, human approval moves from each action to each kind of action, and the planned computed escalation signal and harder static golden set are replaced by a recovery check and versioned faults. Nothing of it is built yet; the design, the visualizer and the README show it as planned. ([ADR 0008](docs/adr/0008-autonomous-operation-with-cost-as-an-invariant.md))

### Harness

- **Eval gate.** Runs an app on golden cases with known answers. Labels such as severity are matched exactly, and a judge model scores faithfulness, response relevancy, context recall and action quality. Each run is compared with a frozen baseline, with seeded bootstrap intervals, and fails on a measured regression or a judge failure rate above its cap. A negative control can never become a baseline. ([ADR 0005](docs/adr/0005-gate-releases-on-measured-answer-quality.md))
- **One interface for any app.** `SystemUnderTest` (`describe`, `answer`, `summarise`) separates the harness from the app. A test fails if the harness imports anything but the standard library, its declared dependencies and itself. ([ADR 0006](docs/adr/0006-eval-gate-as-harness-component-attested-in-ci.md))
- **Attested CI gate.** A full, clean, passing run writes an attestation: the hash of every answer-affecting file, found from the eval's import closure plus declared data, lock and compose files. CI recomputes the hashes in its own job and fails a change no passing run covers. ([ADR 0006](docs/adr/0006-eval-gate-as-harness-component-attested-in-ci.md))
- **Eval gate at every commit.** The pre-commit hook exports exactly the staged tree, gives it its own environment, refuses an import that escapes it, runs the eval gate there and adds the attestation to the same commit; a failed gate refuses the commit. A case whose judge inputs (question, reference, contexts, answer, proposed action, judge model and digest, prompts, judge code, embedder, lock) are identical to the attested run's keeps that run's score, which it names; `--no-reuse` judges every case. The pre-push hook checks each pushed commit's attestation, which merges and rebases would otherwise skip. `git commit <path>` is refused. This ends the old deadlock: an attested file could not be committed, because the gate refused an uncommitted tree. (`code/src/wyld3rness/eval/staged.py`, `reuse.py`)
- **Tracing.** Every run, eval case and command is an OpenTelemetry trace in a self-hosted Langfuse, with a span per step, eval scores on their case, and a cost on each model call from a committed reference price table. Export is batched on its own thread, so a slow or stopped backend does not slow a run. ([ADR 0007](docs/adr/0007-every-run-is-a-trace-in-a-self-hosted-backend.md))

### Example app: FindyFixy

- **Small model first.** A small model answers, and a larger one takes the case when the answer is invalid after one retry or its confidence is low. ([ADR 0001](docs/adr/0001-route-small-model-first.md))
- **Output contract and tools.** Every answer is a validated record with one retry that carries the validation error. Tool arguments are validated inside the tool, and the deploy tool runs both directly and over MCP. ([ADR 0002](docs/adr/0002-validate-outputs-and-tool-arguments.md))
- **Incident memory.** Synthetic past incidents are chunked, embedded and stored in Postgres with pgvector by an ingestion job that is safe to re-run. ([ADR 0003](docs/adr/0003-incident-memory-in-postgres.md))
- **Hybrid retrieval.** Keyword and vector search fused by reciprocal rank, then a cross-encoder reranker, returning incidents rather than chunks. ([ADR 0004](docs/adr/0004-hybrid-retrieval-with-reranker.md))
- **Console.** A local page that runs a triage or an eval and shows each step on the live architecture diagram.
- **Nothing that kills an unattended run.** With one model configured, an answer still invalid after its retry is a typed `TriageError`, recorded by the eval, the console and `make triage`, which goes on to the next question. A query that loses its database connection reconnects with backoff for up to 60 s and retries, and logs both events. The database container runs an init process as PID 1, so an orphaned health check can no longer make Postgres restart itself. (research log, entry 004)

### Simulated production: simprod

- **Three services under real load, open to inspection.** A gateway writes each payment to a 4-shard ledger with fixed capacity per shard, with retries that fire at once as shipped, then notifies; an open-loop generator sends Poisson arrivals and counts latency from the scheduled time. Every service serves health, Prometheus-text metrics, an OpenAPI spec and JSON logs; the service that executes an action logs it with its actor. The services sit on a Docker network with no route out; an edge proxy publishes an allow-list on 127.0.0.1:8100 and never forwards the fault controls. Three typed actions: retry backoff with jitter and a retry budget, a per-key rate cap, each with an undo, and a restart with none. Standard library only, read-only and unprivileged containers. `make sim-up`, `make load-test`, inspection guide in `code/examples/simprod/README.md`. (research log, entry 006)

### Tooling and security

- Locked dependencies with a 7-day release cooldown, `make audit` for known vulnerabilities and unexpected startup hooks, and container images pinned by digest and scanned with Grype before first use.
- simprod's base image, `python:3.13.18-slim-trixie` pinned by digest, scanned with Grype v0.120.1: no Critical, 84 High, of which 5 had a Debian fix (CVE-2026-85091 in zlib1g, CVE-2026-19499 and CVE-2026-5435 in libc6 and libc-bin). The image installs those fixes at pinned versions, whose changelogs are dated 2026-10-10 and 2026-11-01; the built image scans with 0 fixable High. The other 79 High matches (25 bugs: 8 in OpenSSL not yet fixed, 17 Debian will not fix) sit in code the services do not run: no TLS, no mount tools, no Perl.
- A privacy scanner in the pre-commit and pre-push hooks blocks personal data and secrets in files and commit metadata.
- Ruff and mypy in the hooks and in CI. CI runs with a read-only token and no secrets, with actions pinned by commit.
- `make check` covers documentation drift, the repository tools' tests and the privacy scan.
- The local Langfuse's ClickHouse no longer keeps its own profiler and internal logs (`code/infra/langfuse/clickhouse-logs.xml`); the query and part logs expire after a day. Left on, they used about 15 GB and kept the machine busy with nothing running.
- The design and the engineering strategy are one document, `code/docs/design.md`, and the engineering rules for contributors are in `CONTRIBUTING.md`.

### Known issues

- Linux installs pull PyTorch's GPU libraries although nothing uses a GPU, which slows CI. CPU-only wheels are planned with the service image.
- The model names come from an untracked `.env` file, so a model swap changes answers without failing CI. Only the judge is pinned, through the baseline.
- The stale-database check catches a changed embedding model name, not a new revision of the same model.
- The golden set is small and synthetic, and it has not yet exercised escalation.
- The judge can reject a claim its own context supports (ADR 0008), so a faithfulness score alone does not show a wrong answer.
- On some golden cases the judge writes four questions where response relevancy asks for three, on every run, so that metric fails there (research log, entry 003).
- The direct and MCP tool runs record tokens without a model name, so Langfuse puts no cost on those calls.
- Nothing enforces a cost limit. Cost is computed after the fact, on the trace.

## History

The step-by-step build log up to 2026-11-13, including the image scans and exceptions that earlier records cite, is preserved at [this commit](https://github.com/wyld3rbit/wyld3rness/blob/19f5c7a73ff58bfb98ad0afcb98374b1e8c360bd/code/CHANGELOG.md).
