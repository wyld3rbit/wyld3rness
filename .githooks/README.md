# Git hooks

Two shell hooks that enforce the project's checks. Install them once per clone with the `hooks` target in `code/Makefile`.

- Both hooks first refuse anything under `internal/` and any `CLAUDE.md`, which are local only and never committed.
- `.githooks/pre-commit` scans the staged content for PII and secrets with `tools/scan_private.py`, then runs `make check`, and `make lint`, `make test` and `make gate-at-commit` when files under `code/` are staged.
- `make gate-at-commit` exports exactly the staged tree, not the working copy. When the committed attestation still covers it, nothing else runs. When an answer-affecting file changed, it runs the eval gate in that export with its own environment, reuses the attested run's judge score for every case whose judge inputs are identical, and adds the new attestation to the commit. A failed gate refuses the commit. `git commit <path>` is refused, because it commits a temporary index the attestation cannot join: stage with `git add`, then `git commit`.
- `.githooks/pre-push` scans every commit about to be pushed, including its metadata, runs the checks again, and checks that each pushed commit is covered by its own attestation (`make gate-at-push`). That catches a commit made with `--no-verify`, and a merge, rebase or cherry-pick, which never run pre-commit.
