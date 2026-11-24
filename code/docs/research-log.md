# Research log

Every measurement behind a decision in this repository, in the order it was made. Entries are never edited after they are committed: a correction is a new entry that names the one it corrects. A protocol entry is committed before the run it describes, so the order of commits shows the prediction came first.

Each entry holds:

- **Question**: what we want to know, and the decision it feeds.
- **Protocol**: hypothesis, what is measured, the bar, written before the run. Exploratory runs say so and have none.
- **Ran**: the commit, the command, and the report or output it wrote.
- **Result**: the numbers, with their spread, and failures included.
- **Deviations**: anything done differently from the protocol, and why.
- **Meaning**: what it shows in production, and what it cannot show.

---

## 001. Does FindyFixy give the same answer twice? (exploratory)

**Question.** The eval gate is to run at every commit that changes an answer-affecting file. Judging all 30 golden cases takes about 14 minutes. If an unchanged commit gives byte-identical answers, a judge score can be reused for every case whose judge inputs did not change, and a commit costs only the answer pass plus the changed cases. This decides whether the gate at commit is affordable.

**Protocol.** None: an exploratory run, before the design was fixed.

**Ran.** Commit `cd06e51`, with `qwen3:4b-instruct` answering and greedy decoding (temperature 0). A throwaway script, not committed, opened the live session the eval uses and answered all 30 golden cases twice in a row, comparing each case's answer text, recalled contexts, proposed action and labels. Tracing off.

**Result.**

| Compared across the two passes | Identical |
|---|---|
| Answer text | 27 of 30 |
| Recalled contexts | 30 of 30 |
| Proposed action | 27 of 30 |
| Labels (severity) | 30 of 30 |
| Everything the judge reads | 27 of 30 |

The three that differed were g17, g26 and g28, the same three for answer and action. The passes took 84 s and 65 s; the first includes loading the model.

**Deviations.** None.

**Meaning.** Retrieval is deterministic here, and the answering model nearly is: greedy decoding on a local server still varied on 3 of 30. Reusing scores where the judge inputs are byte-identical would judge about 3 cases per commit instead of 30. It cannot show the rate across a server restart, a model reload, other models, or under parallel requests, and one pair of runs gives no spread. The gate at commit records how many scores it reused on every run, which measures this rate from then on.

---

## 002. The gate at commit (protocol)

**Question.** Does the gate at commit refuse a commit that makes answers worse, and cost little for a commit that does not?

**Protocol.** Four live runs, each a real `git commit` through the pre-commit hook, with the same models as entry 001 and the judge `qwen2.5:7b`. Predictions, written before any of them ran:

| Run | Change | Prediction |
|---|---|---|
| A | This commit: the gate at commit itself, a harness change | The gate runs; no score is reused, because the attested report predates judge keys; all 30 judged; it passes and the attestation joins the commit |
| B | Comment-only edits to six attested files | The gate runs; at least 24 of 30 scores reused (entry 001 predicts 27); it passes |
| C | A deliberately worse answer prompt | The gate fails and the commit is refused, naming the failing scores |
| D | A comment in the judge's code | Every judge key changes: 0 reused, 30 judged; it passes |

The bar: each run behaves as predicted. A run that does not is reported as a miss, with its output.

**Ran.** Not yet.

---

## 003. The gate at commit (results of 002)

**Question.** As entry 002.

**Ran.** Four real commits through the pre-commit hook on 2026-11-23, `qwen3:4b-instruct` answering, `qwen2.5:7b` judging, one worker. Reports are in the reports folder on the machine that ran them, named in each commit's attestation.

| Run | Commit | Change | Gate | Scores reused | Judged | Wall time | Prediction met |
|---|---|---|---|---|---|---|---|
| A | `1107cb8` | The gate at commit itself | pass | 0 (the attested report had no judge keys) | 30 | 667 s (87 s answering, 580 s judging) | yes |
| B | `d364a60` | Comment-only edits to six attested files | pass | 27 | 3 | 150 s (87 s answering, 63 s judging) | yes (at least 24) |
| C | none | Answer prompt told to always say sev4 and "investigate" | fail: severity match 0.00 and action quality 0.00 against floors 0.72 and 0.87; faithfulness 0.04; commit refused, nothing staged | 0 | 30 | 448 s | yes |
| D | `c4992d7` | A comment in the judge's code | pass | 0 of 27 eligible: every judge key changed | 30 | 669 s (88 s answering, 581 s judging) | yes |

Two observations the protocol did not ask for:

- **Answers repeated exactly.** Runs A, B and D gave the same answer, contexts and proposed action for all 30 cases, including after the model server was restarted between B and D. Entry 001 saw 27 of 30 in an exploratory pair of passes.
- **The judge repeated itself exactly.** On identical inputs, run D's judge reproduced all 120 of run A's metric scores. The three cases B judged were not changed answers: the judge fails response relevancy on them in every run (it writes four questions where the metric asks for three), and a failed metric is never reused.

**Deviations.** Run D was first started and then stopped after about two minutes, before any case was answered, because the checkout was switched to another branch while the hook ran; the commit would have landed on that branch with an attestation for a different tree. The hook now refuses a commit when the branch, its commit or the staged files change during the gate (`599ede8`). Run D was then repeated from the start.

**Meaning.** A commit that leaves the answers unchanged costs about 2.5 minutes of gate time instead of about 11, and a commit that makes the answers worse cannot be made. A change to the judge forces a full run without any special case. What this cannot show: other models, parallel workers, how often answers differ over weeks of model-server use, or a machine other than this one. Every gate run records its reuse counts in its attestation, so the rate keeps being measured.

---

## 004. Does a run survive a database restart? (protocol)

**Question.** An unattended run must not lose the rest of its work when the database restarts. On 2026-11-21 a gate run lost 12 of 30 cases: Postgres restarted itself three times, for 39.5 s, 1.8 s and 6.6 s, and the one shared connection was never replaced. The cause was reproduced that day in a throwaway container: Postgres ran as process 1 and treated an orphaned `pg_isready`'s exit as a crashed backend; with an init process as process 1 the session survived. That the orphans were health checks that timed out under memory pressure is inferred, not shown. Two fixes follow: the database container gets an init process as process 1 (`init: true`), and a query that loses its connection reconnects with backoff for up to 60 s and retries.

**Protocol.** Two live checks, after the container is recreated with the new compose file:

| Check | How | Prediction |
|---|---|---|
| E | Read process 1's name inside the database container | It is the init process, not `postgres` |
| F | `make eval` (all 30 cases, no gate), with `docker restart` of the database right after the 8th case is answered | 0 run errors; the output shows "database connection lost" and then "database reconnected after N s" with N under 60; every case's scores equal those of the attested run, because the answers repeat (entry 003) |

The bar: both behave as predicted. The 2026-11-21 loss is the reference, with the caveat that its restarts had a different trigger than `docker restart`.

**Ran.** Not yet.

---

## 005. Does a run survive a database restart? (results of 004)

**Ran.** On 2026-11-23, at commit `d6c319a`, after the database container was recreated from its new compose file.

| Check | Observed | Prediction met |
|---|---|---|
| E | Process 1 in the database container is `docker-init`; Postgres runs as process 27; `HostConfig.Init` is true; all 800 stored chunks were still there | yes |
| F | `make eval`, `docker restart` of the database right after case 8 was answered. Case 9 logged "database connection lost (AdminShutdown); reconnecting", then "database reconnected after 2.9 s". 0 run errors in 30 cases. Every judge key (30 of 30) and every metric score (120 of 120) equal step 1's attested run (`eval-20261123T210734Z.json`). 680 s wall | yes |

**Deviations.** The first attempt at the step 1 commit, before E and F, stopped because the container VM had shut down: its host agent received a termination signal at 12:16:36, from a cause not found. With no database, the gate failed within seconds, as the fail-fast rule in 004's change intends, and the commit was refused. It was repeated once the VM was back.

**Meaning.** A database restart now costs the case in flight a few seconds instead of costing the rest of the run, and the restarts seen on 2026-11-21 lose their trigger. What this cannot show: a restart longer than the 60 s deadline (the case in flight fails and the next one tries again, by design, and is covered by tests with fakes only), a network path that drops silently (keepalives, also tested only with fakes), or behaviour under memory pressure, where the 2026-11-21 restarts began.

---

## 006. Does the simulated production fit in the container VM, and at what rate does it run? (protocol)

**Question.** Steps 2 to 6 run a small production (gateway, ledger, notifications, behind an allow-list proxy, code in `code/examples/simprod/`) under synthetic load, in the same container VM as Langfuse and Postgres: 2 CPUs and 5.77 GiB, of which about 2.7 GiB was available before it started. If the services under load leave no room, a storm's numbers would measure the VM, not the system, so this is checked before anything is built on them. The test also fixes the load rate R that every storm runs at, which sets the ledger's capacity and converts failed requests into customer minutes in step 6.

**Protocol.** `make load-test` at commit `83a4d1f` (`simprod/loadtest.py`, whose constants are these numbers), with Langfuse and Postgres running. Customers are an open-loop generator: Poisson arrivals at the rate, each request sent at its time whether or not earlier ones answered, latency counted from the scheduled time, customer timeout 3 s. VM CPU and memory are read from `/proc/stat` and `/proc/meminfo` inside a container, which show the whole VM; the generator's own CPU counts, as it will in the storms.

| Part | What runs | Bar for each phase |
|---|---|---|
| Ramp | 25, 50, 100, 200, 400, 800, 1600 requests/s, 60 s each, ledger capacity far above the load (64 slots of 1 ms per shard), stopping at the first rate that fails | 0 failed requests, p99 ≤ 250 ms (half the gateway's 500 ms timeout), VM CPU mean ≤ 80%, late sends (more than 100 ms behind schedule) ≤ 1% |
| Rate | C = the highest ramp rate passing before the first failure (a lower bound if all pass). R = the highest ramp rate with 4 × R ≤ C: a retry storm sends each request up to 4 times, and must still fit in what the VM can serve | No R: stop and resize the VM before building on it |
| Soak | 3 runs of 300 s at R (seeds 1 to 3), the ledger as it will run in storms: 1 slot per shard, each write held 2000 / R ms, so capacity is 2 × R and the shards are half busy | The ramp's bar with VM CPU mean ≤ 60%, and VM memory available ≥ 1024 MiB at its lowest |

The bar: all three soaks pass. Expected from the exploratory runs below: every ramp rate up to at least 400 passes, so R is 100 or more.

**Exploratory runs before this entry** (2026-11-23, evidence kept, no bar): 10 s at 50/s, 499 of 499 ok, p99 133 ms, ledger at its default 20 ms per write. 15 s each at 200/s and 400/s with the ledger unconstrained: all 3,020 and 6,031 ok, p99 48 and 88 ms, VM CPU over each run 28% and 33%. They changed the design in three ways before this entry: the ramp gained 1600/s because 400/s left most of the CPU idle; the generator's thread pool is sized from the rate (rate × 3 s × 1.5, since a storm keeps up to rate × 3 s requests waiting), because the fixed 256 threads would have sent late during a storm and quietly lowered the load on the system it measures; and late sends became part of the bar.

**Limits.** One VM on one machine, with Langfuse idle rather than ingesting traces. Sleep-based service times stand in for database work, so ledger CPU is not representative of a real database. The ramp's 60 s phases show sustained throughput, not behaviour over hours.

**Ran.** Not yet.
