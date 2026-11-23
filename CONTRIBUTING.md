# Contributing

Pull requests are welcome. Changes to `main` go through a pull request, and `main` accepts a change only when both jobs of the `check` workflow pass: `attestation` and `check`. The maintainer's own pull requests merge automatically once they pass; others are merged by the maintainer.

A change to an answer-affecting file (the eval path's code, `code/examples/findyfixy/data/`, the baseline, the lock file or the compose file) also needs a passing eval gate run and the `code/examples/findyfixy/evals/attestation.json` it writes, in the same commit. With the hooks installed (the `hooks` target in `code/Makefile`), the pre-commit hook does this on exactly the staged files and refuses the commit if the gate fails; without them, run `make eval-gate` on a clean tree and commit the attestation. Either way the run needs the local models.

## Before you open one

- Run `make check`. Run `make test` too when `code/` changed.
- Use synthetic data only. Do not add personal data, secrets or local paths.
- Add a dependency only if it has an OSI-approved license, and say so in the pull request.
- Keep the change small and say how you checked it.

## Engineering rules

- Dependencies point inward: domain logic never imports the HTTP layer or a vendor client, and the harness never imports an app.
- One composition root builds the real wiring. Configuration is read at the edge. No wiring is duplicated. Each module has one reason to change.
- A new control is a harness component with a port the app implements or calls.
- Validate at system boundaries. Treat corpus text, model output and request data as untrusted. Fail loudly.
- Resolve files from the project root, never from a hardcoded path or the current directory.
- Inject dependencies, use precise types, write small pure functions and do no work at import time.
- Test logic with fakes next to the code, and show that each new test can fail: break the code on purpose, see it fail, restore it.
- Document each module, class and function: what it does, why, the trade-off and the failure mode.
- Pin versions and commit the lock file. Pin container images by digest.
- Least privilege: services bind to loopback and no secret is in code. Keep it simple: no speculative abstraction, no clever code.
- Workflows use `pull_request`, read-only permissions, no secrets and actions pinned by commit.

## License

Contributions are licensed under Apache-2.0, the same terms as the project. See `LICENSE`.
