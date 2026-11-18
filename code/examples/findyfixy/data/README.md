# Data

Synthetic data only. Nothing here comes from a real incident, customer or employer.

The corpus, the golden set and the deploy log are versioned. A version is a folder, and a folder is never edited in place: a harder or larger set is a new version.

| | v1 (frozen) | v2 (hard cases) |
|---|---|---|
| Corpus | `code/examples/findyfixy/data/v1/incidents.jsonl`: 200 incidents, from `code/examples/findyfixy/src/findyfixy/retrieval/corpus.py` | `code/examples/findyfixy/data/v2/incidents.jsonl`: v1's 200 unchanged, plus 5 poisoned and 5 cascade incidents from `code/examples/findyfixy/src/findyfixy/retrieval/corpus_v2.py` |
| Golden set | `code/examples/findyfixy/data/v1/golden.jsonl`: 30 cases (20 twin, 10 no twin) | `code/examples/findyfixy/data/v2/golden.jsonl`: 30 cases, 5 each of no precedent, conflicting, cascade, noisy, poisoned and destructive, from `code/examples/findyfixy/src/findyfixy/evals/golden_v2.py` |
| Deploy log | `code/examples/findyfixy/data/v1/deploys.json` | `code/examples/findyfixy/data/v2/deploys.json`: v1's records plus the deploys the destructive cases roll back |
| Used by | the eval gate, the baseline and the attestation | `make eval DATA=v2`: scores only, never the gate |

v1 stays the regression check: a v1 gate run must match the last attested run case by case. `make data-v2` rebuilds v2, and a test fails if the committed files differ from what it writes. `make ingest` loads each version into its own table, so a v1 query can never return a v2 incident.

Not versioned:

- `code/examples/findyfixy/data/incidents.json` is a small hand-written bank of incident excerpts with answer keys, used by the Day 2 measurements.
- `code/examples/findyfixy/data/retrieval_eval.jsonl` holds the labelled queries that score retrieval, written against corpus v1.
