"""The gate at commit and at push (wyld3rness/eval/staged.py), on throwaway git repositories.

Git runs for real; `uv` and the gate command are faked, so no environment is
built and no model is called. The fake gate writes an attestation over the
folder it runs in, which is what a passing real gate does.
"""

from __future__ import annotations

from collections.abc import Iterator, Sequence
import json
import os
from pathlib import Path
import subprocess
from typing import Any

import pytest

from wyld3rness.eval.attest import fingerprint
from wyld3rness.eval.provenance import STAGED_TREE_ENV
from wyld3rness.eval.staged import (
    Refused,
    check_index,
    escaped_imports,
    export_tree,
    gate_commit,
    gate_push,
    link_ignored,
    pushed_commits,
    toplevel,
)

WS = "ws"
ATTESTATION = "evals/attestation.json"
IDENTITY = ("-c", "user.name=T", "-c", "user.email=0+t@users.noreply.github.com")


@pytest.fixture(autouse=True)
def no_outer_git(monkeypatch: pytest.MonkeyPatch) -> None:
    """Drop every GIT_* variable: inside a pre-commit hook they point at the real repository's index."""
    for name in [n for n in os.environ if n.startswith("GIT_")]:
        monkeypatch.delenv(name)


def git(repo: Path, *args: str) -> str:
    """One git command in `repo`, with a synthetic identity."""
    return subprocess.run(
        ["git", *IDENTITY, *args], cwd=repo, check=True, capture_output=True, text=True
    ).stdout.strip()


def attest(ws: Path) -> None:
    """Write an attestation that covers `ws` as it is now."""
    current = fingerprint(ws, ["pkg.core"], [], ["src"])
    doc = {
        "version": 2,
        "verdict": "pass",
        "created_utc": "2026-11-23T00:00:00+00:00",
        "fingerprint": current.digest,
        "inputs": {"entry_modules": ["pkg.core"], "extra_files": [], "source_roots": ["src"]},
        "files": dict(current.files),
        "report": {"name": "eval-1.json", "sha256": "0" * 64},
        "run": {},
    }
    (ws / ATTESTATION).parent.mkdir(parents=True, exist_ok=True)
    (ws / ATTESTATION).write_text(json.dumps(doc, indent=2, sort_keys=True) + "\n")


@pytest.fixture
def repo(tmp_path: Path) -> Path:
    """A repository with one attested package and one commit."""
    root = tmp_path / "repo"
    ws = root / WS
    (ws / "src" / "pkg").mkdir(parents=True)
    (ws / "src" / "pkg" / "__init__.py").write_text("")
    (ws / "src" / "pkg" / "core.py").write_text("X = 1\n")
    (ws / "README.md").write_text("not attested\n")
    (root / ".gitignore").write_text(f"{WS}/.gate/\n{WS}/settings.env\n")
    attest(ws)
    git(root, "init", "-q", "-b", "main")
    git(root, "add", "-A")
    git(root, "commit", "-q", "-m", "first")
    return root


class FakeTools:
    """A `subprocess.run` stand-in: git for real, `uv` and the gate faked and recorded."""

    def __init__(
        self, gate_exit: int = 0, writes: bool = True, escape: bool = False, during: Path | None = None
    ) -> None:
        """`gate_exit`: the gate's exit code. `writes`: it writes an attestation. `escape`: a stray import.

        `during`: a repository whose branch is switched while the fake gate runs.
        """
        self.gate_exit, self.writes, self.escape, self.during = gate_exit, writes, escape, during
        self.calls: list[tuple[list[str], dict[str, str] | None]] = []

    def __call__(self, cmd: Sequence[str], **kw: Any) -> subprocess.CompletedProcess[str]:
        """Dispatch one command."""
        cmd = list(cmd)
        cwd = Path(kw.get("cwd") or ".")
        if cmd[0] == "git":
            return subprocess.run(cmd, **kw)
        self.calls.append((cmd, kw.get("env")))
        if cmd[:2] == ["uv", "sync"]:
            return subprocess.CompletedProcess(cmd, 0, "", "")
        if cmd[:2] == ["uv", "run"]:
            names = cmd[cmd.index("-c") + 2 :]
            base = Path("/elsewhere") if self.escape else cwd / "src"
            found = {n: str(base / n / "__init__.py") for n in names}
            return subprocess.CompletedProcess(cmd, 0, json.dumps(found), "")
        if cmd[0] == "fake-gate":
            if self.writes:
                attest(cwd)
            if self.during is not None:
                git(self.during, "switch", "-q", "-c", "elsewhere")
            return subprocess.CompletedProcess(cmd, self.gate_exit, "", "")
        raise AssertionError(f"unexpected command {cmd}")


def commit_gate(repo: Path, tools: FakeTools, links: Sequence[str] = ()) -> list[str]:
    """Run the commit flow with the fakes; return what it said."""
    said: list[str] = []
    gate_commit(repo, WS, ATTESTATION, links, ["fake-gate"], environ=dict(os.environ), run=tools, say=said.append)
    return said


def staged_names(repo: Path) -> list[str]:
    """Paths staged for the next commit."""
    return git(repo, "diff", "--cached", "--name-only").splitlines()


# ---- Pure pieces ----------------------------------------------------------------


def test_the_root_is_found_from_a_subfolder_inside_a_worktree_hook(repo: Path, monkeypatch) -> None:
    """A worktree's hook sets GIT_DIR; git then mistakes the current folder for the root."""
    monkeypatch.setenv("GIT_DIR", str(repo / ".git"))
    assert toplevel(repo / WS) == repo.resolve()


@pytest.mark.parametrize("index", [None, "/r/.git/index", "/r/.git/index.lock"])
def test_an_ordinary_commit_index_is_accepted(index: str | None) -> None:
    """`git commit`, `commit -a` and `--amend` use the real index or its lock."""
    check_index({} if index is None else {"GIT_INDEX_FILE": index})


def test_a_commit_of_named_paths_is_refused() -> None:
    """`git commit <path>` builds a temporary index the attestation cannot join."""
    with pytest.raises(Refused, match="temporary index"):
        check_index({"GIT_INDEX_FILE": "/r/.git/next-index-123.lock"})


def test_an_import_from_outside_the_export_is_named(tmp_path: Path) -> None:
    """Inside is fine; outside or missing is a problem."""
    inside = str(tmp_path / "src" / "a" / "__init__.py")
    found = {"a": inside, "b": "/elsewhere/b/__init__.py", "c": None}
    assert escaped_imports(found, tmp_path) == [
        "b imports from outside the exported tree",
        "c does not import in the gate environment",
    ]


def test_ignored_inputs_are_linked_and_tracked_ones_refused(tmp_path: Path) -> None:
    """A link for each ignored input that exists; a tracked path is never hidden."""
    source, exported = tmp_path / "src", tmp_path / "exp"
    (source / "r").mkdir(parents=True)
    (source / "settings.env").write_text("A=1\n")
    exported.mkdir()
    link_ignored(source, exported, ["settings.env", "r", "missing.env"])
    assert (exported / "settings.env").is_symlink() and (exported / "r").is_symlink()
    assert not (exported / "missing.env").exists()
    (exported / "tracked.txt").write_text("x")
    (source / "tracked.txt").write_text("y")
    with pytest.raises(Refused, match="tracked"):
        link_ignored(source, exported, ["tracked.txt"])
    with pytest.raises(Refused, match="plain path"):
        link_ignored(source, exported, ["../x"])


# ---- The commit flow -----------------------------------------------------------


def test_the_export_holds_the_staged_tree_not_the_working_copy(repo: Path, tmp_path: Path) -> None:
    """A staged edit is exported; an unstaged edit on top of it is not."""
    core = repo / WS / "src" / "pkg" / "core.py"
    core.write_text("X = 2\n")
    git(repo, "add", "-A")
    core.write_text("X = 3\n")
    exported = export_tree(repo, git(repo, "write-tree"), WS, tmp_path / "out")
    assert (exported / "src" / "pkg" / "core.py").read_text() == "X = 2\n"


def test_a_commit_the_attestation_still_covers_runs_nothing(repo: Path) -> None:
    """A change outside the attested files needs no environment and no gate."""
    (repo / WS / "README.md").write_text("edited\n")
    git(repo, "add", "-A")
    tools = FakeTools()
    said = commit_gate(repo, tools)
    assert tools.calls == [] and "no run needed" in said[0]


def test_a_changed_attested_file_runs_the_gate_and_stages_its_attestation(repo: Path) -> None:
    """The gate runs in the export with the staged tree's id, and its attestation joins the commit."""
    (repo / WS / "src" / "pkg" / "core.py").write_text("X = 2\n")
    git(repo, "add", "-A")
    tree = git(repo, "write-tree")
    tools = FakeTools()
    said = commit_gate(repo, tools)
    gate_env = next(env for cmd, env in tools.calls if cmd[0] == "fake-gate")
    assert gate_env is not None and gate_env[STAGED_TREE_ENV] == tree
    assert gate_env["UV_PROJECT_ENVIRONMENT"].endswith(f"{WS}/.gate/env")
    assert f"{WS}/{ATTESTATION}" in staged_names(repo)
    assert "added to this commit" in said[-1]
    git(repo, "commit", "-q", "-m", "second")
    gate_push(repo, WS, ATTESTATION, f"refs/heads/main {git(repo, 'rev-parse', 'HEAD')} refs/heads/main {'0' * 40}")


def test_a_failing_gate_refuses_the_commit(repo: Path) -> None:
    """Nothing is staged and the working copy's attestation is untouched."""
    (repo / WS / "src" / "pkg" / "core.py").write_text("X = 2\n")
    git(repo, "add", "-A")
    before = (repo / WS / ATTESTATION).read_text()
    with pytest.raises(Refused, match="gate failed"):
        commit_gate(repo, FakeTools(gate_exit=1))
    assert (repo / WS / ATTESTATION).read_text() == before
    assert f"{WS}/{ATTESTATION}" not in staged_names(repo)


def test_a_gate_that_passes_without_attesting_refuses_the_commit(repo: Path) -> None:
    """Exit 0 is not enough: the attestation must cover the export (a stale database writes none)."""
    (repo / WS / "src" / "pkg" / "core.py").write_text("X = 2\n")
    git(repo, "add", "-A")
    with pytest.raises(Refused, match="wrote no attestation"):
        commit_gate(repo, FakeTools(writes=False))


def test_an_environment_that_imports_other_code_is_refused(repo: Path) -> None:
    """The gate never runs when a first-party package resolves outside the export."""
    (repo / WS / "src" / "pkg" / "core.py").write_text("X = 2\n")
    git(repo, "add", "-A")
    tools = FakeTools(escape=True)
    with pytest.raises(Refused, match="does not run the staged code"):
        commit_gate(repo, tools)
    assert not any(cmd[0] == "fake-gate" for cmd, _ in tools.calls)


def test_unstaged_edits_to_the_attestation_are_never_overwritten(repo: Path) -> None:
    """The gate writes that file; an edit nobody staged would be lost, so the commit is refused first."""
    (repo / WS / "src" / "pkg" / "core.py").write_text("X = 2\n")
    git(repo, "add", "-A")
    (repo / WS / ATTESTATION).write_text("{}\n")
    tools = FakeTools()
    with pytest.raises(Refused, match="unstaged edits"):
        commit_gate(repo, tools)
    assert tools.calls == [] and (repo / WS / ATTESTATION).read_text() == "{}\n"


def test_a_branch_switch_during_the_gate_refuses_the_commit(repo: Path) -> None:
    """The attestation was made for the tree at the start; the commit would land on another branch."""
    (repo / WS / "src" / "pkg" / "core.py").write_text("X = 2\n")
    git(repo, "add", "-A")
    with pytest.raises(Refused, match="changed while the gate ran"):
        commit_gate(repo, FakeTools(during=repo))
    assert f"{WS}/{ATTESTATION}" not in staged_names(repo)


def test_ignored_inputs_reach_the_gate(repo: Path) -> None:
    """A linked settings file is visible in the export the gate runs in."""
    (repo / WS / "settings.env").write_text("MODEL=m\n")
    (repo / WS / "src" / "pkg" / "core.py").write_text("X = 2\n")
    git(repo, "add", "-A")
    commit_gate(repo, FakeTools(), links=["settings.env"])
    assert (repo / WS / ".gate" / "tree" / WS / "settings.env").read_text() == "MODEL=m\n"


# ---- The push check -------------------------------------------------------------


@pytest.fixture
def history(repo: Path) -> Iterator[tuple[Path, str, str]]:
    """A covered commit, then one that changed an attested file without a new attestation."""
    good = git(repo, "rev-parse", "HEAD")
    (repo / WS / "src" / "pkg" / "core.py").write_text("X = 9\n")
    git(repo, "commit", "-q", "-am", "unattested")
    yield repo, good, git(repo, "rev-parse", "HEAD")


def test_push_names_the_commit_its_attestation_does_not_cover(history: tuple[Path, str, str]) -> None:
    """A commit made with --no-verify, a merge or a rebase is caught here."""
    repo, _, bad = history
    with pytest.raises(Refused, match=bad[:12]):
        gate_push(repo, WS, ATTESTATION, f"refs/heads/main {bad} refs/heads/main {'0' * 40}")


def test_push_checks_only_what_the_remote_lacks(history: tuple[Path, str, str]) -> None:
    """`<remote sha>..<local sha>` for an existing branch; a deleted ref sends nothing."""
    repo, good, bad = history
    assert pushed_commits(repo, f"refs/heads/main {bad} refs/heads/main {good}") == [bad]
    assert pushed_commits(repo, f"(delete) {'0' * 40} refs/heads/x {good}") == []
    # A remote tip this clone never fetched (a force push over work done elsewhere).
    assert pushed_commits(repo, f"refs/heads/main {bad} refs/heads/main {'1' * 40}") == [good, bad]
    said: list[str] = []
    gate_push(repo, WS, ATTESTATION, f"refs/heads/main {good} refs/heads/main {'0' * 40}", say=said.append)
    assert "1 commit(s) checked" in said[0]
