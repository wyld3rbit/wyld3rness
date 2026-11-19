# Data

Synthetic data only. Nothing here comes from a real incident, customer or employer.

The corpus, the golden set and the deploy (or change) log are versioned. A version is a folder, and a folder is never edited in place: a harder or larger set is a new version.

| | v1 (frozen) | v2 (hard cases) | v3 (hard cases on a timeline) |
|---|---|---|---|
| Corpus | `code/examples/findyfixy/data/v1/incidents.jsonl`: 200 incidents, from `code/examples/findyfixy/src/findyfixy/retrieval/corpus.py` | `code/examples/findyfixy/data/v2/incidents.jsonl`: v1's 200 unchanged, plus 5 poisoned and 5 cascade incidents from `code/examples/findyfixy/src/findyfixy/retrieval/corpus_v2.py` | `code/examples/findyfixy/data/v3/incidents.jsonl`: v2's, byte for byte |
| Golden set | `code/examples/findyfixy/data/v1/golden.jsonl`: 30 cases (20 twin, 10 no twin) | `code/examples/findyfixy/data/v2/golden.jsonl`: 30 cases, 5 each of no precedent, conflicting, cascade, noisy, poisoned and destructive, from `code/examples/findyfixy/src/findyfixy/evals/golden_v2.py` | `code/examples/findyfixy/data/v3/golden.jsonl`: v2's cases, each with `alert_at`, the moment its alert fired, from `code/examples/findyfixy/src/findyfixy/evals/golden_v3.py` |
| What changed | `code/examples/findyfixy/data/v1/deploys.json`: deploys as "hours ago" against one shared moment | `code/examples/findyfixy/data/v2/deploys.json`: v1's records plus the deploys the destructive cases roll back | `code/examples/findyfixy/data/v3/changes.json`: every change kind (deploy, config, flag, key rotation) at an absolute time; deploys say whether they are safe to roll back |
| Used by | the eval gate, the baseline and the attestation | `make eval DATA=v2`: scores only, never the gate | `make eval DATA=v3`: scores only, never the gate |

v1 stays the regression check: a v1 gate run must match the last attested run case by case. `make data-v2` and `make data-v3` rebuild their versions, and a test fails if the committed files differ from what they write. `make ingest` loads each version into its own table, so a v1 query can never return a v2 incident.

Why v3: in v1 and v2 every case reads the same log against one shared moment, so two auth cases (h23, where a key rotation broke logins, and h26, where a deploy did) see the same deploy an hour before and no rule can tell them apart. In v3 each case looks back from its own alert time, and the log records every change, so h23's window shows the rotation and h26's shows the deploy alone. Each v2 deploy keeps its offset from the rollback case of its own service, so h26 to h30 see what they saw on v2.

Not versioned:

- `code/examples/findyfixy/data/services.json` is the service map the agent's `get_dependencies` tool reads: for each of the eight services, the services whose failure can break it. It describes the system, not a data set, so every version shares it. A test checks that each v2 cascade's cause sits upstream of the service that alerted.
- `code/examples/findyfixy/data/incidents.json` is a small hand-written bank of incident excerpts with answer keys, used by the Day 2 measurements and as the console's incident question set.
- `code/examples/findyfixy/data/retrieval_eval.jsonl` holds the labelled queries that score retrieval, written against corpus v1.
