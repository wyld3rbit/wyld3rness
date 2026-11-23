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
