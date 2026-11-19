"""Data v3 (Day 7): v2's cases on a real timeline, with a change log that tells h23 from h26.

Fakes only: no database, no model.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from findyfixy.evals.adapter import to_case
from findyfixy.evals.attacks import build_attacks, write_attacks
from findyfixy.evals.golden import read_incidents
from findyfixy.evals.golden_v2 import build_golden_v2, load_golden_v2, write_cases
from findyfixy.evals.golden_v3 import alert_time, build_changes_v3, build_golden_v3, load_golden_v3, write_changes
from findyfixy.paths import data_dir
from findyfixy.retrieval.corpus import SERVICES, write_jsonl
from findyfixy.retrieval.corpus_v2 import build_corpus_v2
from findyfixy.tools.changes import changes_before, changes_path, load_changes

V1, V2, V3 = data_dir("v1"), data_dir("v2"), data_dir("v3")


@pytest.fixture(scope="module")
def cases():
    return load_golden_v3()


@pytest.fixture(scope="module")
def changes():
    return load_changes(changes_path("v3"))


def window(case, changes, hours: float = 2):
    """The changes to the case's service in the `hours` before its alert."""
    (service,) = [s for s in SERVICES if s in case.question]
    return changes_before(changes, service, case.alert_at, hours)


def test_the_committed_v3_files_are_what_the_builder_writes(tmp_path: Path) -> None:
    """Rebuild data/v3 from committed v1 and v2 into a temp folder: every byte must match."""
    v1 = read_incidents(V1 / "incidents.jsonl")
    built = build_golden_v3(build_golden_v2(v1))
    write_jsonl(build_corpus_v2(v1), tmp_path / "incidents.jsonl")
    write_cases(built, tmp_path / "golden.jsonl")
    write_changes(build_changes_v3(json.loads((V2 / "deploys.json").read_text()), built), tmp_path / "changes.json")
    write_attacks(build_attacks(built), tmp_path / "attacks.jsonl")
    for name in ("incidents.jsonl", "golden.jsonl", "changes.json", "attacks.jsonl"):
        assert (tmp_path / name).read_bytes() == (V3 / name).read_bytes(), f"data/v3/{name} is stale: make data-v3"


def test_v3_is_v2_plus_a_clock(cases) -> None:
    """Same corpus, same cases field for field; only the alert's facts (time, service) are new."""
    assert (V3 / "incidents.jsonl").read_bytes() == (V2 / "incidents.jsonl").read_bytes()
    for v2, v3 in zip(load_golden_v2(), cases, strict=True):
        assert v3.model_dump(exclude={"alert_at", "alert_service"}) == v2.model_dump()


def test_each_case_fires_on_its_own_day_at_the_time_its_alert_names(cases) -> None:
    """hNN on 2026-10-NN; h26's alert reads 13:05, and a noisy case's time is its alert's, not the noise's."""
    by_id = {c.id: c for c in cases}
    assert [c.alert_at.day for c in cases] == list(range(1, 31))
    assert by_id["h26"].alert_at.isoformat() == "2026-10-26T13:05:00+00:00"
    assert by_id["h16"].alert_at.strftime("%H:%M") == "04:19"


def test_a_case_with_no_time_is_refused(cases) -> None:
    """A new case without HH:MM must not land silently at midnight."""
    with pytest.raises(ValueError, match="names no HH:MM"):
        alert_time(cases[0].model_copy(update={"question": "Alert: something broke."}))


def test_each_rollback_case_sees_one_change_a_safe_deploy_of_the_version_it_rolls_back_from(cases, changes) -> None:
    """What C2 allows: the deploy is the only change in two hours, and it is safe to roll back."""
    for case in (c for c in cases if c.group == "destructive"):
        (only,) = window(case, changes)
        assert only.kind == "deploy" and only.rollback_safe is True and only.ref in case.reference


def test_h23_sees_a_safe_deploy_and_a_later_key_rotation(cases, changes) -> None:
    """Every fact about the deploy holds, as on h26; only the second change shows a rollback cannot help."""
    h23 = next(c for c in cases if c.id == "h23")
    rotation, deploy = window(h23, changes)
    assert rotation.kind == "key_rotation" and deploy.kind == "deploy" and deploy.rollback_safe is True
    assert deploy.at < rotation.at < h23.alert_at


def test_no_other_case_has_a_change_in_its_two_hour_window(cases, changes) -> None:
    """On v2 every case saw the deploys made for other cases. On v3 a case sees only its own story."""
    busy = {c.id for c in cases for s in SERVICES if s in c.question and changes_before(changes, s, c.alert_at, 2)}
    assert busy == {"h23", "h26", "h27", "h28", "h29", "h30"}


def test_the_case_record_carries_the_alert_time_as_text(cases) -> None:
    """Meta is saved in the JSON report, so the time travels as ISO text and the adapter parses it back."""
    assert to_case(cases[25]).meta["alert_at"] == "2026-10-26T13:05:00+00:00"
    assert to_case(cases[25]).meta["alert_service"] == "auth"
    assert "alert_at" not in to_case(load_golden_v2()[25]).meta


def test_the_adapter_hands_each_case_its_own_alert_time() -> None:
    """The graph for h26 gets h26's alert (time and service); a case without one gets None (v1, v2)."""
    from findyfixy.evals.adapter import FindyFixy
    from findyfixy.triage.record import Triage

    seen = []

    class Graph:
        def invoke(self, state):
            return {"triage": Triage(severity="sev1", component="auth", next_action="page", confidence=0.9)}

    def make_graph(recall, at):
        seen.append(at)
        return Graph()

    sut = FindyFixy(make_graph=make_graph, candidates=lambda q, n: [], small_model="s", large_model="l")
    sut.answer(to_case(load_golden_v3()[25]))
    sut.answer(to_case(load_golden_v2()[25]))
    assert [(a.at.isoformat(), a.service) if a else None for a in seen] == [("2026-10-26T13:05:00+00:00", "auth"), None]


def test_longer_windows_reach_earlier_history(cases, changes) -> None:
    """Three days back, h26 also sees h23's deploy and rotation: history, as a real feed shows. Policy reads 2 hours."""
    h26 = next(c for c in cases if c.id == "h26")
    kinds = [c.kind for c in window(h26, changes, hours=72)]
    assert "key_rotation" in kinds and len(window(h26, changes)) == 1


def test_each_case_names_the_service_its_alert_fires_on(cases) -> None:
    """h11's alert is on search, with billing and gateway rising too; a noisy case's service is in its pager text."""
    by_id = {c.id: c.alert_service for c in cases}
    assert by_id["h11"] == "search" and by_id["h16"] == "gateway" and by_id["h26"] == "auth"
