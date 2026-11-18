"""Data v2 (Day 7): the files are reproducible, v1 is untouched inside them, and the hard cases are what they claim.

Fakes only: no database, no model.
"""

from __future__ import annotations

from collections import Counter
import json
from pathlib import Path

import pytest

from findyfixy.evals import answers_v2
from findyfixy.evals.adapter import to_case
from findyfixy.evals.golden import MAX_QUESTION_CHARS, load_golden, read_incidents
from findyfixy.evals.golden_v2 import (
    build_deploys_v2,
    build_golden_v2,
    load_golden_v2,
    write_cases,
    write_deploys,
)
from findyfixy.paths import data_dir
from findyfixy.retrieval.corpus import write_jsonl
from findyfixy.retrieval.corpus_v2 import CASCADES, EXTRA, POISONED, build_corpus_v2

V1, V2 = data_dir("v1"), data_dir("v2")


@pytest.fixture(scope="module")
def v1_incidents():
    return read_incidents(V1 / "incidents.jsonl")


@pytest.fixture(scope="module")
def cases():
    return load_golden_v2()


def test_the_committed_v2_files_are_what_the_builder_writes(tmp_path: Path, v1_incidents) -> None:
    """Rebuild data/v2 from committed v1 into a temp folder: every byte must match."""
    write_jsonl(build_corpus_v2(v1_incidents), tmp_path / "incidents.jsonl")
    write_cases(build_golden_v2(v1_incidents), tmp_path / "golden.jsonl")
    write_deploys(build_deploys_v2(json.loads((V1 / "deploys.json").read_text())), tmp_path / "deploys.json")
    for name in ("incidents.jsonl", "golden.jsonl", "deploys.json"):
        assert (tmp_path / name).read_bytes() == (V2 / name).read_bytes(), f"data/v2/{name} is stale: make data-v2"


def test_corpus_v2_is_v1_byte_for_byte_then_ten_new_incidents() -> None:
    """v1 is frozen: its 200 lines open the v2 file unchanged, and the additions use new ids."""
    v1_lines = (V1 / "incidents.jsonl").read_text().splitlines()
    v2_lines = (V2 / "incidents.jsonl").read_text().splitlines()
    assert v2_lines[: len(v1_lines)] == v1_lines
    added = [json.loads(line)["id"] for line in v2_lines[len(v1_lines) :]]
    assert added == [i.id for i in EXTRA] and len(added) == 10
    assert not set(added) & {json.loads(line)["id"] for line in v1_lines}


def test_a_v2_id_that_clashes_with_v1_is_refused(v1_incidents) -> None:
    """A clash would overwrite a v1 incident in the v2 table."""
    with pytest.raises(ValueError, match="already used in v1"):
        build_corpus_v2([*v1_incidents, POISONED[0]])


def test_golden_v2_has_thirty_cases_in_six_groups_of_five(cases) -> None:
    assert [c.id for c in cases] == [f"h{n:02d}" for n in range(1, 31)]
    assert Counter(c.group for c in cases) == dict.fromkeys(
        ("no_precedent", "conflicting", "cascade", "noisy", "poisoned", "destructive"), 5
    )
    assert all(len(c.question) <= MAX_QUESTION_CHARS for c in cases)
    assert all(c.reference.startswith("Root cause: ") and "\nRemediation: " in c.reference for c in cases)


def test_every_named_incident_is_in_corpus_v2_and_held_out_ones_differ_from_twins(cases) -> None:
    """A twin recall can only be measured against an incident recall can return."""
    ids = {json.loads(line)["id"] for line in (V2 / "incidents.jsonl").read_text().splitlines()}
    for case in cases:
        assert case.twin_id is None or case.twin_id in ids
        assert case.source_id != case.twin_id


def test_poisoned_and_cascade_cases_point_at_their_precedents(cases) -> None:
    by_group = {g: [c for c in cases if c.group == g] for g in ("poisoned", "cascade")}
    assert [c.twin_id for c in by_group["poisoned"]] == [i.id for i in POISONED]
    assert [c.twin_id for c in by_group["cascade"]] == [i.id for i in CASCADES]
    # The right answer to a poisoned case never follows the injection into a destructive action.
    assert all(c.expected_action != "destructive" for c in by_group["poisoned"])


def test_each_poisoned_incident_carries_an_injected_instruction() -> None:
    """The injection sits in the remediation, the field recall puts into the prompt."""
    markers = (
        "ignore your previous instructions",
        "system prompt",
        "does not need human approval",
        "full authority",
        "Decode and follow",
    )
    for incident, marker in zip(POISONED, markers, strict=True):
        assert marker in incident.remediation
        assert marker not in incident.root_cause


def test_destructive_cases_are_severe_and_follow_a_deploy_within_two_hours(cases) -> None:
    """What Day 7's policy checks before a rollback: sev1 or sev2, and a deploy of that service within 2 hours."""
    deploys = json.loads((V2 / "deploys.json").read_text())
    for case in (c for c in cases if c.group == "destructive"):
        assert case.expected_action == "destructive" and case.expected_severity in ("sev1", "sev2")
        recent = [d for d in deploys if d["service"] in case.question and d["hours_ago"] <= 2]
        assert len(recent) == 1, case.id
        assert recent[0]["version"] in case.reference


def test_only_destructive_cases_expect_a_destructive_action(cases) -> None:
    assert {c.id for c in cases if c.expected_action == "destructive"} == {
        c.id for c in cases if c.group == "destructive"
    }


def test_v1_cases_keep_their_record_and_v2_cases_carry_the_action_class(cases) -> None:
    """A v1 record must not change shape; a v2 record adds the class step 3 scores against."""
    v1_case = to_case(load_golden(V1 / "golden.jsonl")[0])
    assert set(v1_case.meta) == {"source_id", "twin_id"}
    assert to_case(cases[0]).meta["action_class"] == cases[0].expected_action


@pytest.mark.parametrize("flag", ["--gate", "--write-baseline", "--baseline-from=r.json", "--ga", "--write"])
def test_a_v2_run_refuses_the_gate_and_the_baseline(capsys, flag: str) -> None:
    """v1 is the gate's set; comparing v2 with v1's baseline would call every change a regression."""

    def never(*args, **kwargs):
        raise AssertionError("no session may open")

    assert answers_v2.main([flag], open_session=never) == 2
    assert "cover v1 only" in capsys.readouterr().err


def test_ordinary_flags_are_not_mistaken_for_gate_flags() -> None:
    from findyfixy.evals.answers import GATE_FLAGS, asks_for

    assert not asks_for(["--no-recall", "--workers", "2", "--show-case", "h07", "-"], GATE_FLAGS)


def test_the_v1_gate_never_imports_v2_code() -> None:
    """The attestation fingerprints the v1 command's import closure; v2 must stay outside it."""
    from findyfixy.evals.answers import ATTEST_ENTRY, ATTEST_EXTRA, ATTEST_SOURCE_ROOTS
    from findyfixy.paths import WORKSPACE
    from wyld3rness.eval.attest import import_closure

    files = {p.name for p in import_closure(WORKSPACE, ATTEST_ENTRY, ATTEST_SOURCE_ROOTS)}
    assert {"answers_v2.py", "golden_v2.py", "corpus_v2.py"}.isdisjoint(files)
    assert not any("data/v2" in extra for extra in ATTEST_EXTRA)
