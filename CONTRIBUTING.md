# Contributing

Pull requests are welcome. Changes to `main` go through a pull request, and `main` accepts a change only when both jobs of the `check` workflow pass: `attestation` and `check`. The maintainer's own pull requests merge automatically once they pass; others are merged by the maintainer.

A change to an answer-affecting file (the eval path's code, `code/examples/findyfixy/data/`, the baseline, the lock file or the compose file) also needs a passing eval gate run and the `code/examples/findyfixy/evals/attestation.json` it writes, in the same commit. With the hooks installed (the `hooks` target in `code/Makefile`), the pre-commit hook does this on exactly the staged files and refuses the commit if the gate fails; without them, run `make eval-gate` on a clean tree and commit the attestation. Either way the run needs the local models.

## Before you open one

- Run `make check`. Run `make test` too when `code/` changed.
- Use synthetic data only. Do not add personal data, secrets or local paths.
- Add a dependency only if it has an OSI-approved license, and say so in the pull request.
- Keep the change small and say how you checked it.

## License

Contributions are licensed under Apache-2.0, the same terms as the project. See `LICENSE`.
