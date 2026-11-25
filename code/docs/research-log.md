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

---

## 007. Does the simulated production fit in the container VM, and at what rate does it run? (results of 006)

**Ran.** On 2026-11-24 from 02:08 to 02:32 UTC, `make load-test` at commit `07574fc` (the code of `83a4d1f`; the image's files were checked equal to that commit's before the run), with Langfuse, Postgres and Adminer running. Results file SHA-256 `f91681778a33c042…`.

| Phase | Rate/s | Requests | Failed | Late sends | p50 ms | p99 ms | VM CPU mean (max) | VM memory available, lowest | Bar |
|---|---|---|---|---|---|---|---|---|---|
| Ramp | 25 | 1,476 | 0 | 0 | 8.7 | 89.5 | 21.6% (33.7%) | 2,573 MiB | pass |
| Ramp | 50 | 2,998 | 0 | 0 | 8.9 | 90.4 | 21.8% (37.9%) | 2,533 MiB | pass |
| Ramp | 100 | 5,981 | 0 | 0 | 8.5 | 89.8 | 26.4% (42.3%) | 2,537 MiB | pass |
| Ramp | 200 | 12,071 | 0 | 0 | 7.7 | 88.4 | 27.1% (40.7%) | 2,587 MiB | pass |
| Ramp | 400 | 23,815 | 0 | 0 | 7.7 | 55.1 | 37.5% (59.7%) | 2,545 MiB | pass |
| Ramp | 800 | 48,298 | 0 | 0 | 7.4 | 49.7 | 44.3% (55.2%) | 2,542 MiB | pass |
| Ramp | 1600 | 95,689 | 12,604 (11,081 timeouts, 1,523 connection errors) | 745 | 10.0 | 546.2 | 81.6% (89.8%) | 2,199 MiB | fail: failures, p99, CPU |
| Soak 1 | 200 | 59,587 | 0 | 0 | 19.1 | 57.8 | 29.6% (45.6%) | 2,430 MiB | pass |
| Soak 2 | 200 | 59,933 | 0 | 0 | 19.6 | 58.4 | 30.8% (49.2%) | 2,392 MiB | pass |
| Soak 3 | 200 | 59,719 | 0 | 0 | 19.4 | 57.5 | 31.2% (46.1%) | 2,412 MiB | pass |

C = 800/s, measured, not a lower bound: 1600/s failed. R = 200/s, since 4 × 200 = 800 ≤ C. All three soaks pass, so the bar is met. No phase lost a VM sample (27 to 28 per ramp phase, 144 per soak). The 200/s prediction of the exploratory runs held, and the expectation in 006 (every rate to at least 400 passes) was exceeded: 800 passed.

**What saturated.** Per-container CPU from `docker stats` (100% is one core): at 800/s the gateway used 29%, the generator 14%, notifications 13%, the ledger 12%; at 1600/s the gateway used 81%, the generator 23%. A Python process runs one thread at a time, so one process tops out near one core: the knee is the gateway's, not the generator's (745 late sends, 0.8%, stayed under the 1% bar). The VM itself idles at about 21% with Langfuse and Postgres up, the floor under every phase.

**Deviations.** None from the protocol. `make load-test` reinstalled the simprod package in the worktree's environment first (uv's editable install of a changed member); the image, which serves the requests, was unchanged.

**Meaning.** Storms run at 200 customer requests a second, the ledger at one slot per shard holding each write 10 ms (capacity 400/s, half busy), and a storm that quadruples the gateway's load to the ledger still sits at the rate the VM served without a failure. A retry storm's numbers will therefore measure the storm, not the VM. Customer minutes in step 6 convert at 12,000 requests a minute. Unexplained and left so: below 400/s p99 is about 90 ms, above it about 50 ms; one guess is CPU frequency scaling at low load, not tested. What this cannot show: behaviour while Langfuse ingests traces (it was idle), a gateway with more than one process, or anything over hours.

---

## 008. Are the storms real, fixable, and judged fairly? (protocol)

**Question.** Steps 3 to 6 put an agent and a no-model script on seeded storms in the simulated production (entry 006), judged by a checker that reads only what customers saw. Three things must hold first. A storm must last by itself, or a later "fix" would be credited for what time did. The correct fix must end it, or no agent can win and every result is noise. A wrong fix must be caught and undone, or the checker cannot be trusted. Each of three arms is the control for one of these. The pilot before them sets the two windows every later verdict uses, and the controls give the spread of customer minutes from which step 6 sizes N.

**Storms.** Faults version 2 (`simprod/faults.py`), at R = 200 requests a second (entry 007). A storm is a fault plus the gateway it meets, deployed either as shipped (retries fire at once) or with backoff (`GATEWAY_BACKOFF=1`: full jitter and a retry budget). The storms cycle through three kinds; each has one correct fix, a set of actions applied together. The decoy is another customer's key, drawn from the seed.

| Kind | Gateway | Injected | Fix | Wrong fix |
|---|---|---|---|---|
| Retry storm | As shipped | Every ledger write slower by 2 to 5 service times, for 5 to 15 s | Backoff on | Cap the decoy |
| Hot shard | Backs off | One key sent at 0.5 R to 0.75 R on top of its customers, until cleared | Cap that key at R / 20 | Cap the decoy |
| Compound | As shipped | The same runaway | Cap that key and backoff on | Backoff on and cap the decoy |

**Protocol.** At commit `633a288`. One storm under one arm: fresh containers, the gateway deployed as the kind says; 20 s of warm-up in which every second must be good; the fault injected from inside the network; 15 s later the arm acts through the edge, as an agent will; WAIT seconds later, verification; then the hold. A good second has at least one completed customer request and at most 1% failed. Recovered means a run of HOLD good seconds that starts within WAIT of the action. Verified means the last 5 s of WAIT are all good. Customer minutes are the failed customer requests from the fault on, divided by R × 60. A run is judged only if the customer stream was measured whole and the load was really offered: no counter reset, no more than 2.5 s between two scrapes after the fault, at most 1% of customer requests sent over 100 ms late (the load test's bar), and the faults cleared at the end. A run that fails this is a miss for its arm. Every setting the storm arithmetic rests on (shards, queue limit, service times, retries, timeout, backoff, rate) is pinned per storm and saved in its record.

| Part | What runs | Rule or bar |
|---|---|---|
| Pilot | `make controls-pilot`: 9 storms (seed 3), 3 of each kind, under nobody and the scripted fix, observed for 90 s after the action | WAIT = 2 × the slowest scripted recovery (the first run of 10 good seconds), HOLD = 2 × the longest good stretch under nobody, at least 30 s, both rounded up to 5 s. A scripted storm that never recovers fails the pilot: stop and report |
| Windows | WAIT and HOLD are committed into `simprod/controls.py` before the controls run; `make controls` refuses to run without them | - |
| Controls | `make controls`: 21 storms (seed 7), 7 of each kind, each under nobody, scripted, wrong, in that order | nobody: 21 of 21 not recovered. scripted: 21 of 21 recovered, every action answered 200. wrong: 21 of 21 not verified, undone with every undo answered 200, the gateway back to its starting settings. Every warm-up good |

The bar: all three arms meet theirs over all 21 storms. No storm is rerun: a storm that hits an infrastructure error, or cannot be judged, counts as a miss and is reported as one with its reason. Reported beside the bar: customer minutes per kind and arm (median, minimum, maximum), the time to recovery after the scripted fix, scrape errors, the longest scrape gap and the late-send share.

Expected from the exploratory runs below: nobody loses about 1.25 customer minutes in a retry storm and about 0.32 in a runaway storm; the scripted fix recovers within about 2 s (retry storm, hot shard) to 6 s (compound) of acting; WAIT near 20 s and HOLD at its 30 s floor.

**Exploratory runs before this entry** (2026-11-24, faults version 1 on the uncommitted step code, one storm per arm, evidence kept, no bar):

| Run | Storm and gateway | Arm | Recovered | Customer minutes |
|---|---|---|---|---|
| 02:36 UTC | Retry storm, as shipped | nobody | no | 1.253 |
| | | backoff | yes | 0.279 |
| | Hot shard, as shipped | nobody | no | 0.324 |
| | | cap | no | 0.315 |
| 02:46 UTC | Hot shard, backoff deployed before the fault | nobody | no | 0.324 |
| | | cap | yes, failures near 0 about 2 s after acting | 0.072 |
| | | cap on another key | no | 0.323 |
| 18:49 UTC | Hot shard, as shipped (same storm as 02:46) | cap | no | 0.317 |
| | | backoff | no | 0.324 |
| | | cap and backoff together | yes, about 6 s after acting | 0.079 |
| | | cap, then backoff 15 s later | yes, about 7 s after the backoff | 0.147 |

They changed the design. In version 1 a hot shard was fixed by a cap alone, and the 02:36 run showed it was not: with retries firing at once, the shard's own customers (about R / 4 a second, each sent up to 4 times) keep a shard that serves R / 2 over capacity after the runaway is capped, so the outage is a retry storm confined to one shard. The 02:46 run isolated the cause: with backoff deployed, the cap alone ends it. The 18:49 run showed that on the gateway as shipped each action alone is indistinguishable from doing nothing, and only the pair recovers. Version 2 keeps both systems as two kinds, the wrong fixes aim at a decoy key so they test the target as well as the action, and a try in step 3 became a set of at most 2 actions verified together.

**Limits.** The same project writes the faults, the fixes and the checker. The scripted fix is an oracle that reads the storm's seed, so the controls show the environment can be won, not that anything can find the fix. Arms run in a fixed order per storm; containers are recreated between runs, so only the VM carries over. One seed set per part, one VM, synthetic customers, and windows measured on this rate and capacity only.

**Ran.** Not yet.
