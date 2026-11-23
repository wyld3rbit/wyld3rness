"""Tests for the provenance stamp (wyld3rness/eval/provenance.py). Clock, git and Ollama faked.

What these tests cover
    Every harness key, the app's `describe()` merged flat, the refusal of a
    clashing key, the negative-control flag, and that git runs in the
    directory the caller names.
"""

from pathlib import Path
import subprocess
from typing import Any

import pytest

from wyld3rness.eval import provenance as pv
from wyld3rness.eval.metrics import PROMPT_VERSIONS
from wyld3rness.eval.provenance import CORE_KEYS, build_provenance, git_info
from wyld3rness.testing import NOW


def provenance(**over: Any) -> dict[str, Any]:
    """`build_provenance` with fakes; keyword arguments override them."""
    args: dict[str, Any] = dict(
        judge_model="qwen2.5:7b",
        digest=lambda m: "dig-" + m,
        golden_sha256="gold",
        describe={"small_model": "s", "threshold": 0.7},
        git=lambda: ("abc1234", False),
        now=lambda: NOW,
    )
    args.update(over)
    return build_provenance(**args)


def test_provenance_has_every_harness_key_and_the_apps_keys_flat() -> None:
    """Every core key, plus the app's `describe()` keys at the top level."""
    p = provenance()
    assert set(p) == {*CORE_KEYS, "small_model", "threshold"}
    assert p["judge_digest"] == "dig-qwen2.5:7b"
    assert p["prompt_versions"] == PROMPT_VERSIONS
    assert p["timestamp_utc"] == "2026-10-26T12:30:05+00:00"
    assert p["git_sha"] == "abc1234" and p["git_dirty"] is False
    assert p["small_model"] == "s" and p["threshold"] == 0.7
    assert p["negative_control"] is False


def test_the_negative_control_is_recorded() -> None:
    """A control run says so in its stamp."""
    assert provenance(negative_control=True)["negative_control"] is True


@pytest.mark.parametrize("key", ["judge_digest", "git_sha", "negative_control"])
def test_describe_may_not_overwrite_a_harness_key(key: str) -> None:
    """An app key may not forge the judge, git or control stamp."""
    with pytest.raises(ValueError, match=key):
        provenance(describe={key: "forged"})


def test_git_info_outside_a_checkout_is_unknown(tmp_path: Path) -> None:
    """Without a repository the stamp is unknown, not a crash."""
    assert git_info(tmp_path) == ("unknown", None)


def test_git_info_runs_in_the_directory_it_is_given(tmp_path: Path, monkeypatch) -> None:
    """git runs in the caller's directory, never one the harness works out."""
    calls: list[tuple[list[str], Path]] = []

    def fake_run(args, cwd, **kwargs):
        """Record the call and answer like git would."""
        calls.append((args, cwd))
        out = "abc1234\n" if "rev-parse" in args else " M some/file\n"
        return subprocess.CompletedProcess(args, 0, stdout=out)

    monkeypatch.setattr(pv.subprocess, "run", fake_run)
    assert git_info(tmp_path) == ("abc1234", True)
    assert [cwd for _, cwd in calls] == [tmp_path, tmp_path]


def test_the_staged_tree_stands_for_the_commit_being_made(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """At commit the gate runs outside any checkout; the tree id it was given is what ran, and it is clean."""
    monkeypatch.setenv(pv.STAGED_TREE_ENV, "ab" * 20)
    assert pv.source_info(tmp_path) == ("tree-" + "ab" * 6, False)


@pytest.mark.parametrize("value", ["abc", "z" * 40, "AB" * 20])
def test_a_malformed_staged_tree_id_is_refused(monkeypatch: pytest.MonkeyPatch, tmp_path: Path, value: str) -> None:
    """A value that is not a full lowercase object id is a mistake, not a tree."""
    monkeypatch.setenv(pv.STAGED_TREE_ENV, value)
    with pytest.raises(ValueError, match="full git object id"):
        pv.source_info(tmp_path)


def test_without_a_staged_tree_the_commit_is_used(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """Outside the gate at commit, `source_info` is `git_info`."""
    monkeypatch.delenv(pv.STAGED_TREE_ENV, raising=False)
    monkeypatch.setattr(pv, "git_info", lambda repo: ("abc1234", False))
    assert pv.source_info(tmp_path) == ("abc1234", False)
