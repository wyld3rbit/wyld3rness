# AGENTS.md

Instructions for coding agents working in this repository. People start at `README.md`.

## Layout

- `code/src/wyld3rness/`: the harness package. It never imports an application.
- `code/examples/findyfixy/`: the example agent. `code/examples/simprod/`: the simulated production it is tested against.
- `code/infra/`: services the harness talks to, each with its own compose file.
- `docs/`: every guide, the design and the decision records (`docs/adr/`).
- `tools/`: repository checks. `.githooks/`: the commit and push hooks.

## Commands

Run from the repository root. Python is 3.13, managed by uv in `code/.venv`.

```bash
make setup     # once per clone
make test      # harness and app tests, fakes only, about a minute
make lint      # Ruff and mypy
make check     # docs drift, repository tools' tests, privacy scan
make audit     # known vulnerabilities and startup hooks; required after any change to code/uv.lock
make eval-gate # the answer-quality gate on live models, many minutes
```

Install the hooks once with the `hooks` target in `code/Makefile`. The pre-commit hook runs the checks above, and the eval gate on exactly the staged files when an answer-affecting file changed.

## Rules the checks enforce

- The harness imports only the standard library, its declared dependencies and itself (`code/tests/test_harness_rules.py`).
- A change to an answer-affecting file needs a passing eval gate run and its attestation in the same commit (`docs/evals.md`).
- Docs may not name a file or make target that does not exist, or a count that goes stale (`tools/check_docs.py`).
- No personal data, secrets, local paths or host names anywhere (`tools/scan_private.py`).

## Rules to follow

- Never bypass a hook or a check.
- Synthetic data only. Every dependency has an OSI-approved license and a pinned version; uv ignores releases newer than `exclude-newer` in `code/pyproject.toml`.
- Container images are pinned by digest.
- Resolve files from the project root in code, never from the current directory.
- Keep `README.md` true in the same commit as the change it describes.
