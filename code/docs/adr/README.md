# Architecture decision records

This project is a prototype harness for an LLM app, built with open-source tools. It keeps changing. Each record covers one day of the build: its design decisions, each written in its step's plan before any code, then one Outcome when the day closes, with the system as it stood then. What was measured is in the [research log](../research-log.md); what changed is in the [changelog](../../CHANGELOG.md).

A record has a decision, a diagnosis, guiding policies, coherent actions, a build-buy-adopt call, a staffing rule, what would change the decision, evidence and limits. The structure follows a standard engineering strategy shape.

Records are append-only, with one recorded exception: on 2026-11-26 the Staffing section of records 0001 to 0008 was reworded to state what people own; nothing else in them changed. A day's record grows only by its decision sections and its Outcome; a later decision that reverses one is a new record that supersedes it, and an earlier record is never edited. Records 0001 to 0005 were written on 2026-10-29, after their phases closed, from the day notes, the changelog and the decision log. Day 6's record was written in the commit that closed it. Day 7's began at the start of the day as its direction (0008), and its decisions were added to it; from Day 8 each record is begun in the plan of the day's first step.

Day 5 was rebuilt after ADR 0005 was accepted, so ADR 0006 records the rebuilt phase and 0005 keeps its own text and picture. Diagrams are keyed by ADR number. The diagrams for ADRs 0001 to 0008 are frozen files checked by hash: 0001 to 0004 were drawn from the page's earlier one-lane layout, 0005 shows the eval gate as first built inside the app, 0006 and 0007 predate the autonomous loop on the page, and 0008 records a plan as it was accepted; its Outcome has a second picture, drawn from the page at Day 7's close. `make adr-snapshot` draws the diagram of each later record from the live architecture page. A test fails if a frozen file changes, or if the page and the latest phase disagree.

| ADR | Phase | Decision | Code at close |
|---|---|---|---|
| [0001](0001-route-small-model-first.md) | Day 1 | Route on a small model first and distrust its confidence | `853532b` |
| [0002](0002-validate-outputs-and-tool-arguments.md) | Day 2 | Validate every model output and every tool argument | `5262b5b` |
| [0003](0003-incident-memory-in-postgres.md) | Day 3 | Store incident memory in Postgres with pgvector | `21b4c4a` |
| [0004](0004-hybrid-retrieval-with-reranker.md) | Day 4 | Retrieve with hybrid search and a reranker | `cd9283d` |
| [0005](0005-gate-releases-on-measured-answer-quality.md) | Day 5 | Gate releases on measured answer quality | `1e7f27a` |
| [0006](0006-eval-gate-as-harness-component-attested-in-ci.md) | Day 5, rebuilt | The eval gate is a harness component, attested in CI | `6f9e3ee` |
| [0007](0007-every-run-is-a-trace-in-a-self-hosted-backend.md) | Day 6 | Every run is an OpenTelemetry trace in a self-hosted backend | the commit that adds it |
| [0008](0008-autonomous-operation-with-cost-as-an-invariant.md) | Day 7 | Autonomous operation, with cost as an invariant | the commit that merges Day 7 (Outcome) |
