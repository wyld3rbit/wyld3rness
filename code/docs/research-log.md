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
