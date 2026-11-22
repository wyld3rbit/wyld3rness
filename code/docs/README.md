# Docs

The interactive architecture page lives with the console that serves it, `code/examples/findyfixy/src/findyfixy/console/static/architecture.html`. From the top it shows what a human sets before runtime, the harness, FindyFixy's decision, the act-verify-undo loop and the simulated production. What is built is solid, what is planned is dashed, and it follows a live run from the console's event stream: a triage run of one question, or an eval run of the first N golden cases, where each case moves through the app lane and its scores land on the eval gate. Start the console with `make console`.

`code/docs/design.md` is the design and the engineering strategy in one page: the target shape, the diagnosis and policies drawn from the decision records, and the invariants with the check for each.

`code/docs/adr/` holds one architecture decision record per phase, each with a diagram, keyed by ADR number. The diagrams for ADRs 0001 to 0008 are frozen files checked by hash. `make adr-snapshot` draws the diagram of each later record from the architecture page, and `code/docs/target-design.svg` with it.

`code/docs/demo.gif` and `code/docs/space-needle.png` are the images used by the top-level README.
