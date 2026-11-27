"""Score reuse (wyld3rness/eval/reuse.py): which scores may be copied, and the run that copies them.

Fakes only: the fake judge records every prompt it is asked, so a test can
prove a reused case cost no judge call.
"""

from __future__ import annotations

import copy
from dataclasses import replace
import json
from pathlib import Path
from typing import Any

import pytest

from wyld3rness.eval.attest import file_sha256
from wyld3rness.eval.reuse import PriorScores, judge_code_digest, judge_identity, judge_key, load_prior
from wyld3rness.eval.sut import Answer
from wyld3rness.testing import FakeJudge, FakeSUT, case, run

IDENTITY = "i" * 64


def identity(**changes: Any) -> str | None:
    """A judge identity with one field changed."""
    fields: dict[str, Any] = {
        "judge_model": "judge",
        "judge_digest": "d1",
        "prompt_versions": {"m": "h"},
        "judge_code": "c",
        "extra": {"embedder": "e@1"},
    }
    return judge_identity(**{**fields, **changes})


def test_every_judge_input_changes_the_key() -> None:
    """Question, reference, answer, contexts and action each change the key; the labels do not."""
    c = case("g1")
    base = Answer(text="t", contexts=["c1"], action="a", labels={"kind": "x"})
    key = judge_key(IDENTITY, c, base)
    variants = [
        judge_key(IDENTITY, replace(c, question="other"), base),
        judge_key(IDENTITY, replace(c, reference="other"), base),
        judge_key(IDENTITY, c, Answer(text="t2", contexts=["c1"], action="a", labels={"kind": "x"})),
        judge_key(IDENTITY, c, Answer(text="t", contexts=["c2"], action="a", labels={"kind": "x"})),
        judge_key(IDENTITY, c, Answer(text="t", contexts=["c1"], action="b", labels={"kind": "x"})),
        judge_key("j" * 64, c, base),
    ]
    assert len({key, *variants}) == 7
    # Labels are matched exactly by the runner, not judged, so they are not judge inputs.
    assert judge_key(IDENTITY, c, Answer(text="t", contexts=["c1"], action="a", labels={"kind": "y"})) == key


def test_every_part_of_the_judge_changes_its_identity() -> None:
    """Model, digest, prompts, code and the app's extras each change the identity."""
    base = identity()
    others = [
        identity(judge_model="other"),
        identity(judge_digest="d2"),
        identity(prompt_versions={"m": "h2"}),
        identity(judge_code="c2"),
        identity(extra={"embedder": "e@2"}),
    ]
    assert base is not None and len({base, *others}) == 6


def test_an_unknown_judge_digest_turns_reuse_off() -> None:
    """Two runs cannot be shown to share a judge whose weights are unknown."""
    assert identity(judge_digest="unknown") is None
    assert identity(judge_digest="") is None


def test_the_judge_code_digest_follows_the_loaded_files(tmp_path: Path) -> None:
    """A changed byte in a judge module changes the digest."""
    import types

    path = tmp_path / "m.py"
    path.write_text("x = 1\n")
    module = types.ModuleType("m")
    module.__file__ = str(path)
    first = judge_code_digest([module])
    path.write_text("x = 2\n")
    assert judge_code_digest([module]) != first
    assert len(judge_code_digest()) == 64


# ---- The run -----------------------------------------------------------------


def first_run() -> dict[str, Any]:
    """A judged two-case report whose records carry judge keys."""
    return run([case("g1"), case("g2")], judge_identity=IDENTITY)


def prior_of(report: dict[str, Any], name: str = "eval-1.json") -> PriorScores:
    """The prior scores a gate run would load from `report`."""
    return PriorScores(
        run=name, scores={c["id"]: (c["judge_key"], c["metrics"], c["scores_from"] or name) for c in report["cases"]}
    )


def test_identical_inputs_reuse_the_scores_and_call_no_judge() -> None:
    """The second run copies both cases' scores and asks the judge nothing."""
    report = first_run()
    judge = FakeJudge()
    again = run([case("g1"), case("g2")], judge=judge, judge_identity=IDENTITY, prior=prior_of(report))
    assert judge.prompts == []
    assert again["reuse"] == {"from": "eval-1.json", "reused": 2, "judged": 0}
    for old, new in zip(report["cases"], again["cases"], strict=True):
        assert new["metrics"] == old["metrics"] and new["scores_from"] == "eval-1.json"
        assert new["answer"] == old["answer"] and new["contexts"] == old["contexts"]
    assert again["aggregates"]["overall"]["scores"] == report["aggregates"]["overall"]["scores"]


def test_a_changed_answer_is_judged_again() -> None:
    """Only the case whose action changed costs judge calls."""
    report = first_run()
    judge = FakeJudge()
    changed = run(
        [case("g1"), case("g2")],
        sut=FakeSUT(action="restart"),
        judge=judge,
        judge_identity=IDENTITY,
        prior=prior_of({**report, "cases": [report["cases"][0]]}),
    )
    assert judge.prompts, "the changed cases were judged"
    assert changed["reuse"] == {"from": None, "reused": 0, "judged": 2}  # nothing was taken from it
    assert all(c["scores_from"] is None for c in changed["cases"])


def test_a_run_without_identity_records_no_key_and_reuses_nothing() -> None:
    """The console and any app that declares no reuse identity judge as before."""
    report = run([case("g1")])
    assert report["cases"][0]["judge_key"] is None
    assert report["reuse"] == {"from": None, "reused": 0, "judged": 1}


def test_reused_cases_emit_the_same_events_as_judged_ones() -> None:
    """A viewer sees four metric scores and a scored case either way."""
    events: list[tuple[str, dict[str, Any]]] = []
    report = first_run()
    run(
        [case("g1")],
        judge_identity=IDENTITY,
        prior=prior_of(report),
        on_event=lambda kind, **data: events.append((kind, data)),
    )
    kinds = [k for k, _ in events]
    assert kinds.count("metric_scored") == 4 and kinds[-1] == "case_scored"


def test_a_chain_of_reuse_still_names_the_run_that_judged() -> None:
    """Run 2 copies run 1's scores; run 3 copies run 2's, and its records still name run 1."""
    first = first_run()
    second = run([case("g1")], judge_identity=IDENTITY, prior=prior_of(first, "eval-1.json"))
    third = run([case("g1")], judge_identity=IDENTITY, prior=prior_of(second, "eval-2.json"))
    assert third["cases"][0]["scores_from"] == "eval-1.json"
    assert third["reuse"]["from"] == "eval-2.json"


class Sink:
    """Records every score sent to the trace backend."""

    def __init__(self) -> None:
        """No scores yet."""
        self.sent: list[tuple[str, str]] = []

    def score(self, trace_id: str, name: str, value: float, kind: str) -> None:
        """Record one score's name and kind."""
        self.sent.append((name, kind))


def test_a_reused_case_still_sends_its_label_matches_but_no_metric_scores() -> None:
    """Labels were matched in this run; the metrics were not judged in it."""
    from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter

    from wyld3rness.trace.tracing import TraceConfig, Tracing

    config = TraceConfig(host="http://127.0.0.1:1", public_key="pk-lf-t", secret_key="sk-lf-t", service_name="t")
    tracing = Tracing.start(config, exporter=InMemorySpanExporter())
    sink = Sink()
    try:
        run([case("g1")], judge_identity=IDENTITY, prior=prior_of(first_run()), tracing=tracing, scores=sink)
    finally:
        tracing.close()
    assert sink.sent == [("kind_match", "BOOLEAN")]


def test_no_proposed_action_does_not_block_reuse(tmp_path: Path) -> None:
    """Action quality refuses an empty action every time, so that failure is reusable."""
    report = run([case("g1")], sut=FakeSUT(action=None), judge_identity=IDENTITY)
    assert report["cases"][0]["metrics"]["action_quality"]["failed"] is True
    prior, _ = load_prior(attested(tmp_path, report), tmp_path / "reports")
    assert prior is not None and set(prior.scores) == {"g1"}


def test_the_proposed_action_is_stored() -> None:
    """What the action metric reads is in the record, so a key can be audited."""
    assert first_run()["cases"][0]["proposed_action"] == "roll back"


# ---- Loading the attested run --------------------------------------------------


def attested(tmp_path: Path, report: dict[str, Any], *, sha: str | None = None) -> Path:
    """Save `report` and an attestation naming it; return the attestation's path."""
    reports = tmp_path / "reports"
    reports.mkdir(exist_ok=True)
    path = reports / "eval-1.json"
    path.write_text(json.dumps(report))
    doc = {
        "version": 2,
        "verdict": "pass",
        "created_utc": "2026-11-23T00:00:00+00:00",
        "fingerprint": "f",
        "inputs": {"entry_modules": ["a"], "extra_files": [], "source_roots": ["."]},
        "files": {"a.py": "h"},
        "report": {"name": path.name, "sha256": sha or file_sha256(path)},
        "run": {},
    }
    attestation = tmp_path / "attestation.json"
    attestation.write_text(json.dumps(doc))
    return attestation


def test_load_prior_reads_the_attested_report(tmp_path: Path) -> None:
    """Keys and scores by case, named by the report file."""
    report = first_run()
    prior, why = load_prior(attested(tmp_path, report), tmp_path / "reports")
    assert prior is not None and prior.run == "eval-1.json" and set(prior.scores) == {"g1", "g2"}
    assert "2 case(s) eligible" in why


@pytest.mark.parametrize(
    ("setup", "reason"),
    [
        ("no attestation", "no attestation"),
        ("no report", "not on this machine"),
        ("edited report", "does not match the hash"),
        ("no keys", "no judge keys"),
    ],
)
def test_load_prior_refuses_any_source_it_cannot_trust(tmp_path: Path, setup: str, reason: str) -> None:
    """Every refusal returns None with a reason; none raises."""
    report = first_run()
    if setup == "no keys":
        report = copy.deepcopy(report)
        for c in report["cases"]:
            c["judge_key"] = None
    attestation = attested(tmp_path, report)
    if setup == "no attestation":
        attestation.unlink()
    elif setup == "no report":
        (tmp_path / "reports" / "eval-1.json").unlink()
    elif setup == "edited report":
        (tmp_path / "reports" / "eval-1.json").write_text(json.dumps({**report, "cases": []}))
    prior, why = load_prior(attestation, tmp_path / "reports")
    assert prior is None and reason in why


def test_a_failed_metric_is_never_reused(tmp_path: Path) -> None:
    """The failure may have been transient, so that case is judged again."""
    report = run([case("g1"), case("g2")], judge_identity=IDENTITY)
    report["cases"][0]["metrics"]["faithfulness"].update(score=None, failed=True, error="timeout")
    prior, _ = load_prior(attested(tmp_path, report), tmp_path / "reports")
    assert prior is not None and set(prior.scores) == {"g2"}


def test_a_report_name_with_a_path_is_refused(tmp_path: Path) -> None:
    """The attestation is input from whoever committed it: a name, never a path."""
    attestation = attested(tmp_path, first_run())
    doc = json.loads(attestation.read_text())
    doc["report"]["name"] = "../eval-1.json"
    attestation.write_text(json.dumps(doc))
    prior, why = load_prior(attestation, tmp_path / "reports")
    assert prior is None and "file name" in why
