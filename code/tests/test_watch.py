"""Tests for wyld3rness/loop/watch.py: an incident through the watched loop is one event per step, in order."""

from __future__ import annotations

from collections.abc import Sequence
import json
from typing import Any, get_args

from wyld3rness.loop.env import Action
from wyld3rness.loop.run import LoopConfig, Try, run_incident
from wyld3rness.loop.traced import traced
from wyld3rness.loop.watch import EventLog, LoopEventKind, Watch
from wyld3rness.testing import FakeDecider, FakeSigner, FakeWorld, allow

CONFIG = LoopConfig(wait_s=30, hold_s=30)
CREDENTIAL = "credential-placeholder-0002"  # stands for a signed credential: it must never reach an event


class Reporting(FakeDecider):
    """A fake decider that reports each decision's calls and the spend so far, as the model decider does."""

    def decide(self, observation: str, history: Sequence[Try]) -> list[Action]:
        self.tries = len(history) + 1
        return super().decide(observation, history)

    def last_report(self) -> dict:
        return {
            "reason": f"try {self.tries}",
            "calls": [{"model": "m", "input_tokens": 600, "output_tokens": 150, "valid": True}],
            "spent_usd": 0.0001 * self.tries,
        }


class Signing(FakeSigner):
    """A signer whose credential looks real."""

    def sign(self, action: Action) -> str:
        super().sign(action)
        return CREDENTIAL


def watched(world: FakeWorld, decider: Any, policy: Any, signer: Any = None) -> tuple[list[dict], Any]:
    """Run one incident through the watched loop; its events and outcome."""
    log = EventLog()
    watch = Watch(log.emit)
    out = run_incident(*_order(watch.ports(world, decider, policy, signer or FakeSigner()), CONFIG))
    watch.finish(out)
    log.close()
    events, done = log.since(0)
    assert done
    return events, out


def _order(ports: tuple, config: LoopConfig) -> tuple:
    """`ports` in `run_incident`'s argument order."""
    env, decider, policy, signer = ports
    return env, decider, config, policy, signer


def kinds(events: list[dict]) -> list[str]:
    """The kinds, in order."""
    return [e["kind"] for e in events]


def test_a_try_undone_then_a_try_that_resolves_is_every_step_in_order() -> None:
    events, out = watched(FakeWorld(fix=["c"]), Reporting([["a"], ["c"]]), allow("a", "c"), Signing())
    assert out.resolved
    assert kinds(events) == [
        "try_started", "decided", "checked", "applied", "waiting", "applied", "verdict",
        "try_started", "decided", "checked", "applied", "waiting", "waiting", "verdict",
        "incident_ended",
    ]  # fmt: skip
    applied = [e["data"] for e in events if e["kind"] == "applied"]
    assert [(a["action"], a["undo_of"], a["try"]) for a in applied] == [
        ("a()", None, 1),
        ("undo:a()", "a()", 1),
        ("c()", None, 2),
    ]
    verdicts = [e["data"] for e in events if e["kind"] == "verdict"]
    assert [(v["try"], v["verdict"], v["undone"]) for v in verdicts] == [
        (1, "not verified", ["undo:a()"]),
        (2, "resolved", []),
    ]
    decided = [e["data"] for e in events if e["kind"] == "decided"]
    assert decided[1]["actions"] == ["c()"] and decided[1]["reason"] == "try 2" and decided[1]["spent_usd"] == 0.0002
    assert decided[0]["calls"][0]["input_tokens"] == 600
    end = events[-1]["data"]
    assert end["ended"] == "resolved" and end["kept"] == ["c()"] and end["tries_used"] == 2
    times = [e["t"] for e in events]
    assert times == sorted(times) and times[-1] > 60  # on the environment's clock, in order


def test_a_refused_decision_is_checked_false_with_its_reasons_and_applies_nothing() -> None:
    events, _ = watched(FakeWorld(fix=["c"]), FakeDecider([["x"], ["c"]]), allow("c"))
    checked = [e["data"] for e in events if e["kind"] == "checked"]
    assert checked[0]["allowed"] is False and checked[0]["reasons"] and checked[1]["allowed"] is True
    first_verdict = next(e["data"] for e in events if e["kind"] == "verdict")
    assert first_verdict["verdict"] == "denied" and first_verdict["reasons"] == checked[0]["reasons"]
    assert [e["data"]["action"] for e in events if e["kind"] == "applied"] == ["c()"]


def test_the_safe_state_undos_are_marked_and_named_at_the_end() -> None:
    events, out = watched(FakeWorld(fix=["z"], refuse=["b"]), FakeDecider([["a", "b"]]), allow("a", "b"))
    assert out.reason.startswith("action failed")
    applied = [(e["data"]["action"], e["data"]["ok"], e["data"]["undo_of"]) for e in events if e["kind"] == "applied"]
    # b may have happened (not ok), so it is in force too: both are put back, last first
    assert applied == [("a()", True, None), ("b()", False, None), ("undo:b()", True, "b()"), ("undo:a()", True, "a()")]
    assert events[-1]["data"]["ended"] == "safe state"
    assert events[-1]["data"]["safe_undos"] == ["undo:b()", "undo:a()"]


def test_a_decider_that_raises_is_a_decided_event_with_its_error() -> None:
    class Down:
        def decide(self, observation: str, history: Sequence[Try]) -> list[Action]:
            raise ConnectionError("agent unreachable")

    events, out = watched(FakeWorld(), Down(), allow("a"))
    assert out.reason.startswith("decider failed")
    decided = next(e["data"] for e in events if e["kind"] == "decided")
    assert decided["error"] == "ConnectionError: agent unreachable" and decided["actions"] == []
    assert kinds(events) == ["try_started", "decided", "incident_ended"]


def test_an_undo_that_looks_like_a_proposal_is_told_apart_by_order() -> None:
    """Try 2 proposes `undo:a`, the very action that undid try 1: it is a proposal, not an undo."""
    events, _ = watched(FakeWorld(fix=["undo:a"]), FakeDecider([["a"], ["undo:a"]]), allow("a", "undo:a"))
    applied = [(e["data"]["action"], e["data"]["undo_of"]) for e in events if e["kind"] == "applied"]
    assert applied[:3] == [("a()", None), ("undo:a()", "a()"), ("undo:a()", None)]


def test_events_are_json_and_never_hold_a_credential_and_the_answers_are_unchanged() -> None:
    plain = run_incident(FakeWorld(fix=["c"]), FakeDecider([["a"], ["c"]]), CONFIG, allow("a", "c"), FakeSigner())
    events, out = watched(FakeWorld(fix=["c"]), FakeDecider([["a"], ["c"]]), allow("a", "c"), Signing())
    text = json.dumps(events)
    assert CREDENTIAL not in text
    assert (out.ended, out.reason, [t.verdict for t in out.tries]) == (
        plain.ended,
        plain.reason,
        [t.verdict for t in plain.tries],
    )
    assert set(kinds(events)) <= set(get_args(LoopEventKind))


def test_watch_and_tracing_compose() -> None:
    log = EventLog()
    watch = Watch(log.emit)
    ports = traced(None, *watch.ports(FakeWorld(fix=["a"]), FakeDecider([["a"]]), allow("a"), FakeSigner()))
    out = run_incident(*_order(ports, CONFIG))
    watch.finish(out)
    assert out.resolved and log.since(0)[0][-1]["data"]["ended"] == "resolved"


def test_the_log_reads_from_an_index_and_says_when_it_is_done() -> None:
    log = EventLog()
    log.emit("try_started", 1.23456, {"try": 1})
    log.emit("decided", 2.0, {"try": 1})
    assert log.since(1) == ([{"kind": "decided", "t": 2.0, "data": {"try": 1}}], False)
    log.close()
    assert log.since(0)[0][0]["t"] == 1.235 and log.since(2) == ([], True)


def test_tracing_around_watching_still_records_the_model_calls() -> None:
    """The order the loop container uses: traced(watch.ports(...)). The trace must keep its generations."""
    from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter

    from wyld3rness.trace.command import CommandTrace
    from wyld3rness.trace.tracing import TraceConfig, Tracing

    exporter = InMemorySpanExporter()
    tracing = Tracing.start(
        TraceConfig(host="http://127.0.0.1:1", public_key="pk-lf-t", secret_key="sk-lf-t", service_name="t"),
        exporter=exporter,
    )
    try:
        watch = Watch(EventLog().emit)
        with CommandTrace(tracing, "incident") as trace:
            ports = traced(
                trace.tracer, *watch.ports(FakeWorld(fix=["c"]), Reporting([["c"]]), allow("c"), FakeSigner())
            )
            out = run_incident(*_order(ports, CONFIG))
        assert out.resolved and tracing.flush()
        names = [s.name for s in exporter.get_finished_spans()]
        assert names.count("model m") == 1
        decide = next(s for s in exporter.get_finished_spans() if s.name == "decide")
        assert decide.attributes["loop.reason"] == "try 1"
    finally:
        tracing.close()


def test_after_a_decision_fails_partway_the_safe_state_undos_are_marked_undos() -> None:
    """Action a fails (not ok) before b is reached: the next apply is a's undo, not a proposal."""
    events, _ = watched(FakeWorld(fix=["z"], refuse=["a"]), FakeDecider([["a", "b"]]), allow("a", "b"))
    applied = [(e["data"]["action"], e["data"]["undo_of"]) for e in events if e["kind"] == "applied"]
    assert applied == [("a()", None), ("undo:a()", "a()")]


def test_after_a_signing_failure_the_safe_state_undos_are_marked_undos() -> None:
    """a is signed and applied, b cannot be signed: the loop stops, and a's undo is marked as one."""
    events, out = watched(FakeWorld(fix=["z"]), FakeDecider([["a", "b"]]), allow("a", "b"), FakeSigner(refuse=["b"]))
    assert out.reason.startswith("action failed")
    applied = [(e["data"]["action"], e["data"]["undo_of"]) for e in events if e["kind"] == "applied"]
    assert applied == [("a()", None), ("undo:a()", "a()")]


def test_an_apply_that_raises_is_an_applied_event_that_failed() -> None:
    events, _ = watched(FakeWorld(fix=["z"], raise_on=["a"]), FakeDecider([["a"]]), allow("a"))
    first = next(e["data"] for e in events if e["kind"] == "applied")
    assert first["action"] == "a()" and first["ok"] is False and first["detail"].startswith("apply raised:")
    later = [e["data"] for e in events if e["kind"] == "applied"][1:]
    assert all(a["undo_of"] == "a()" for a in later)  # the safe state puts a back, and says so
