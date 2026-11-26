# Tools

Scripts that check the repository. Run them through `make check`, which supplies the packages they need.

- `tools/check_docs.py`: fails when a README names a file or make target that does not exist.
- `tools/scan_private.py`: blocks PII and secrets in commits, pushes and the working tree. The hooks in `.githooks/` call it.
- `tools/adr_snapshot.py`: draws the architecture at the close of each phase from `code/examples/findyfixy/src/findyfixy/console/static/architecture.html`, for the ADRs (`make adr-snapshot`). Pictures drawn from an older page layout are frozen and checked by hash.
- `tools/diagram.py`: draws each decision diagram from its spec in `code/docs/adr/fig/`, with the live page's icons, into `code/docs/adr/img/` and into the live page's region for it (`make adr-snapshot` runs it too).
- `tools/test_check_docs.py`, `tools/test_scan_private.py`, `tools/test_adr_snapshot.py` and `tools/test_diagram.py`: tests for the docs checker, the privacy scanner, the snapshot generator and the diagram tool. The last fails when a picture, or its copy in the live page, differs from a fresh render of its spec.
