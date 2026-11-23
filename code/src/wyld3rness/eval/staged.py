"""Run the eval gate on exactly what a commit will contain, and check what a push carries.

What it does
    commit   From the pre-commit hook. Exports the staged tree (not the
             working copy) to a folder of its own. If the committed
             attestation still covers that tree, nothing else runs. Otherwise
             it builds the tree its own environment, checks that every
             first-party import resolves inside the export, runs the app's
             gate command there, checks the attestation it wrote, and adds
             that attestation to the commit being made. Exit 1 refuses the
             commit.
    push     From the pre-push hook. Exports every commit being pushed and
             checks its attestation, because a merge, a rebase or a
             cherry-pick makes commits without running pre-commit.

        python -m wyld3rness.eval.staged commit --workspace code \
            --attestation examples/app/evals/attestation.json \
            [--link examples/app/.env ...] -- <gate command>
        python -m wyld3rness.eval.staged push --workspace code --attestation ... < refs

Why an export, and why its own environment
    Running the gate on the working copy would attest files that are not in
    the commit (an unstaged edit) or miss files that are (a staged edit undone
    in the working copy). The export is `git archive` of the tree
    `git write-tree` makes from the index, so it is byte for byte what the
    commit will hold, before its attestation is added. The project's own
    environment holds editable installs that point at the working copy from
    any folder, so the export gets a second environment (`uv sync --frozen`)
    whose installs point at the export, and the gate refuses to run if any
    first-party package still imports from elsewhere.

What the export cannot carry
    Git-ignored inputs such as `.env` files and the reports folder are not in
    the tree. `--link` makes a symbolic link to each from the working copy, so
    the run reads the real settings and writes its report where the next run's
    score reuse finds it (wyld3rness/eval/reuse.py). Their values are never
    read or printed here. A setting that changes answers but lives in an
    ignored file (the model names in `.env`) is outside the attestation, as it
    was before; the attestation records the model names that ran.

Refusals, with the reason
    - `git commit <path>`: git commits a temporary index built from those
      paths, and an attestation added to it would not be in the real index
      afterwards. Detected by the name of `GIT_INDEX_FILE`.
    - An import that resolves outside the export.
    - A gate command that exits non-zero, or exits 0 without writing an
      attestation that covers the export (for example a stale database).
    - Unstaged edits to the attestation file: the gate writes that file and
      stages it, which would discard them. A commit aborted after the hook
      (an empty message) leaves the new attestation staged; it covers the
      staged tree, so the next commit of that tree needs no run.

Failure model and cost
    The export and its environment live under `<workspace>/.gate/`, which is
    git-ignored and rebuilt on every gated commit; `uv sync` reuses its
    cache, so a rebuild with an unchanged lock takes seconds. A gated commit
    costs the app's answer pass plus judging of the cases whose judge inputs
    changed: minutes, not the full run, when the judge is unchanged.

What changes at scale
    A team runs the same gate in CI on a runner with a GPU, and the commit
    hook becomes advisory. One developer on one machine has no other place to
    run it before the commit exists.
"""

from __future__ import annotations

import argparse
from collections.abc import Callable, Mapping, Sequence
import json
import os
from pathlib import Path, PurePosixPath
import shutil
import subprocess
import sys
import tarfile
import tempfile
from typing import TextIO

from wyld3rness.eval.attest import (
    AttestationInvalid,
    FingerprintError,
    attested_inputs,
    check,
    fingerprint,
    first_party,
    load_attestation,
)
from wyld3rness.eval.provenance import STAGED_TREE_ENV

GATE_DIR = ".gate"  # under the workspace, git-ignored
# The index names git uses for an ordinary commit and for `commit -a`/`--amend`.
# `git commit <path>` uses a temporary `next-index-*.lock`: refused.
ORDINARY_INDEX_NAMES = frozenset({"index", "index.lock"})

Runner = Callable[..., subprocess.CompletedProcess[str]]


class Refused(Exception):
    """The commit or push must not go ahead. The message says why and what to do."""


def _git(repo: Path, *args: str, run: Runner = subprocess.run) -> str:
    """One git command in `repo`, its stdout stripped. Raises `Refused` on failure."""
    done = run(["git", *args], cwd=repo, capture_output=True, text=True, check=False)
    if done.returncode != 0:
        raise Refused(f"git {' '.join(args)} failed: {done.stderr.strip()}")
    return done.stdout.strip()


def check_index(environ: Mapping[str, str]) -> None:
    """Refuse a commit whose index is a temporary one (`git commit <path>`)."""
    index = environ.get("GIT_INDEX_FILE")
    if index and Path(index).name not in ORDINARY_INDEX_NAMES:
        raise Refused(
            "`git commit <path>` commits a temporary index, so the attestation cannot join it. "
            "Stage the files with `git add`, then run `git commit` with no paths."
        )


def export_tree(repo: Path, tree: str, workspace: str, dest: Path) -> Path:
    """Extract `workspace` from git object `tree` (a tree or a commit) into `dest`. Returns the exported workspace.

    `dest` is emptied first. `git archive` writes the objects' bytes, so line
    endings and modes are what the commit holds. The tar is extracted with
    the "data" filter, which refuses links out of the folder and absolute paths.
    """
    if dest.exists():
        shutil.rmtree(dest)
    dest.mkdir(parents=True)
    with tempfile.TemporaryFile() as tar:
        done = subprocess.run(["git", "archive", "--format=tar", tree, workspace], cwd=repo, stdout=tar, check=False)
        if done.returncode != 0:
            raise Refused(f"git archive {tree} {workspace} failed")
        tar.seek(0)
        with tarfile.open(fileobj=tar) as archive:
            archive.extractall(dest, filter="data")
    exported = dest / workspace
    if not exported.is_dir():
        raise Refused(f"the tree holds no {workspace}/ folder")
    return exported


def attestation_problems(workspace: Path, attestation: str) -> list[str]:
    """Every reason the attestation in `workspace` does not cover its files. Empty means covered."""
    try:
        doc = load_attestation(workspace / attestation)
        return check(doc, fingerprint(workspace, *attested_inputs(doc)))
    except (AttestationInvalid, FingerprintError) as err:
        return [str(err)]


def link_ignored(source: Path, exported: Path, links: Sequence[str]) -> None:
    """Link each git-ignored input from the working copy's workspace into the export.

    A missing source is skipped: a fresh clone may have no reports yet, and a
    missing `.env` is the app's own error to report. A path that exists in
    the export is refused, because it is tracked and the link would hide it.
    """
    for rel in links:
        pure = PurePosixPath(rel)
        if pure.is_absolute() or ".." in pure.parts:
            raise Refused(f"--link {rel!r} must be a plain path inside the workspace")
        target, link = source.joinpath(*pure.parts), exported.joinpath(*pure.parts)
        if not target.exists():
            continue
        if link.exists() or link.is_symlink():
            raise Refused(f"--link {rel}: the path is tracked in the commit, so it is not a git-ignored input")
        link.parent.mkdir(parents=True, exist_ok=True)
        link.symlink_to(target.resolve(), target_is_directory=target.is_dir())


def escaped_imports(found: Mapping[str, str | None], exported: Path) -> list[str]:
    """First-party packages whose import resolves outside the export (or not at all). Pure."""
    root = exported.resolve()
    problems = []
    for name, origin in sorted(found.items()):
        if origin is None:
            problems.append(f"{name} does not import in the gate environment")
        elif not Path(origin).resolve().is_relative_to(root):
            problems.append(f"{name} imports from outside the exported tree")
    return problems


# Run inside the gate environment: where each first-party package imports from.
_PROBE = (
    "import importlib.util, json, sys\n"
    "out = {}\n"
    "for name in sys.argv[1:]:\n"
    "    spec = importlib.util.find_spec(name)\n"
    "    out[name] = spec.origin if spec else None\n"
    "print(json.dumps(out))\n"
)


def _uv(exported: Path, env_dir: Path, args: Sequence[str], environ: Mapping[str, str], run: Runner) -> str:
    """One `uv` command in the export, against the gate environment. Returns stdout; raises `Refused`."""
    env = {**environ, "UV_PROJECT_ENVIRONMENT": str(env_dir)}
    done = run(["uv", *args], cwd=exported, env=env, capture_output=True, text=True, check=False)
    if done.returncode != 0:
        raise Refused(f"uv {' '.join(args[:2])} failed in the gate environment: {done.stderr.strip()[-500:]}")
    return done.stdout


def gate_commit(
    repo: Path,
    workspace: str,
    attestation: str,
    links: Sequence[str],
    command: Sequence[str],
    *,
    environ: Mapping[str, str] = os.environ,
    run: Runner = subprocess.run,
    say: Callable[[str], None] = print,
) -> None:
    """The commit flow (module docstring). Returns when the commit may go ahead; raises `Refused` otherwise."""
    check_index(environ)
    staged_path = f"{workspace}/{attestation}"
    if _git(repo, "diff", "--name-only", "--", staged_path, run=run):
        raise Refused(f"{staged_path} has unstaged edits, which the gate would overwrite. Stage or discard them first.")
    tree = _git(repo, "write-tree", run=run)
    source = repo / workspace
    gate_dir = source / GATE_DIR
    exported = export_tree(repo, tree, workspace, gate_dir / "tree")
    before = attestation_problems(exported, attestation)
    if not before:
        say("gate at commit: the committed attestation covers the staged tree; no run needed")
        return
    say(f"gate at commit: the staged tree needs a run ({len(before)} difference(s), first: {before[0]})")
    if not command:
        raise Refused("no gate command given after --")
    link_ignored(source, exported, links)
    env_dir = gate_dir / "env"
    _uv(exported, env_dir, ["sync", "--frozen", "--all-packages", "--quiet"], environ, run)
    try:
        roots = attested_inputs(load_attestation(exported / attestation)).source_roots
    except AttestationInvalid as err:
        raise Refused(f"cannot tell where the first-party packages live: {err}") from err
    names = sorted(first_party(exported, roots))
    found = json.loads(
        _uv(exported, env_dir, ["run", "--frozen", "--no-sync", "python", "-I", "-c", _PROBE, *names], environ, run)
    )
    escaped = escaped_imports(found, exported)
    if escaped:
        raise Refused("the gate environment does not run the staged code: " + "; ".join(escaped))
    env = {**environ, "UV_PROJECT_ENVIRONMENT": str(env_dir), STAGED_TREE_ENV: tree}
    done = run(list(command), cwd=exported, env=env, check=False)
    if done.returncode != 0:
        raise Refused(f"the gate failed on the staged tree (exit {done.returncode}); the commit is not made")
    after = attestation_problems(exported, attestation)
    if after:
        raise Refused("the gate exited 0 but wrote no attestation that covers the staged tree: " + after[0])
    shutil.copyfile(exported / attestation, source / attestation)
    _git(repo, "add", "--", staged_path, run=run)
    say(f"gate at commit: attestation for tree {tree[:12]} added to this commit")


def pushed_commits(repo: Path, refs: str, run: Runner = subprocess.run) -> list[str]:
    """Every commit the push would send, oldest first, from the pre-push hook's stdin.

    Each line is `<local ref> <local sha> <remote ref> <remote sha>`. A
    deleted ref (local sha all zeros) sends nothing. A new branch sends what
    no remote branch already has, and so does a branch whose remote tip this
    repository has never fetched (a force push over a commit made elsewhere).
    """
    commits: list[str] = []
    for line in refs.splitlines():
        parts = line.split()
        if len(parts) != 4 or set(parts[1]) == {"0"}:
            continue
        local, remote = parts[1], parts[3]
        known = set(remote) != {"0"} and _known_commit(repo, remote, run)
        span = [f"{remote}..{local}"] if known else [local, "--not", "--remotes"]
        listed = _git(repo, "rev-list", "--reverse", *span, run=run)
        commits += [c for c in listed.splitlines() if c and c not in commits]
    return commits


def _known_commit(repo: Path, sha: str, run: Runner) -> bool:
    """True when `sha` is a commit in this repository."""
    done = run(["git", "cat-file", "-e", f"{sha}^{{commit}}"], cwd=repo, capture_output=True, text=True, check=False)
    return done.returncode == 0


def gate_push(
    repo: Path,
    workspace: str,
    attestation: str,
    refs: str,
    *,
    run: Runner = subprocess.run,
    say: Callable[[str], None] = print,
) -> None:
    """Check the attestation of every pushed commit. Raises `Refused` naming each commit that fails."""
    failures = []
    commits = pushed_commits(repo, refs, run=run)
    dest = repo / workspace / GATE_DIR / "push"
    try:
        for commit in commits:
            exported = export_tree(repo, commit, workspace, dest)
            problems = attestation_problems(exported, attestation)
            if problems:
                failures.append(f"{commit[:12]}: {problems[0]}")
    finally:
        shutil.rmtree(dest, ignore_errors=True)
    if failures:
        raise Refused("pushed commits not covered by their attestation: " + "; ".join(failures))
    say(f"gate at push: {len(commits)} commit(s) checked, each covered by its attestation")


def _parser() -> argparse.ArgumentParser:
    """`commit ... -- <gate command>` and `push ...`."""
    parser = argparse.ArgumentParser(prog="python -m wyld3rness.eval.staged", description=__doc__.splitlines()[0])
    commands = parser.add_subparsers(dest="command", required=True)
    for name in ("commit", "push"):
        sub = commands.add_parser(name)
        sub.add_argument(
            "--workspace", required=True, help="the folder, relative to the repository root, the gate runs in"
        )
        sub.add_argument("--attestation", required=True, help="the attestation file, relative to the workspace")
        if name == "commit":
            sub.add_argument("--link", action="append", default=[], help="a git-ignored input to link in (repeatable)")
            sub.add_argument("gate", nargs=argparse.REMAINDER, help="-- then the gate command, run in the export")
    return parser


def main(argv: Sequence[str] | None = None, *, stdin: TextIO = sys.stdin) -> int:
    """Exit 0 to let the commit or push go ahead, 1 to refuse it. Run from inside the repository."""
    args = _parser().parse_args(argv)
    try:
        repo = Path(_git(Path.cwd(), "rev-parse", "--show-toplevel"))
        if args.command == "commit":
            gate = args.gate[1:] if args.gate[:1] == ["--"] else args.gate
            gate_commit(repo, args.workspace, args.attestation, args.link, gate)
        else:
            gate_push(repo, args.workspace, args.attestation, stdin.read())
    except Refused as err:
        print(f"refused: {err}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
