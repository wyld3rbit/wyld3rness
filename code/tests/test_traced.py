"""Tests for wyld3rness/loop/traced.py: an incident through the traced loop is one trace, a span per step."""

from __future__ import annotations

from collections.abc import Sequence

from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter
from opentelemetry.trace import StatusCode
import pytest

from wyld3rness.loop.env import Action
from wyld3rness.loop.run import LoopConfig, Try, run_incident
from wyld3rness.loop.traced import traced
from wyld3rness.testing import FakeDecider, FakeSigner, FakeWorld, allow
from wyld3rness.trace.command import CommandTrace
from wyld3rness.trace.spans import GEN_AI_INPUT_TOKENS, GEN_AI_REQUEST_MODEL, LF_INPUT, LF_OUTPUT, LF_TYPE
from wyld3rness.trace.tracing import TraceConfig, Tracing

CONFIG = TraceConfig(host="http://127.0.0.1:1", public_key="pk-lf-test", secret_key="sk-lf-SECRET", service_name="t")
CREDENTIAL = "credential-placeholder-0001"  # stands for a signed credential: it must never reach a span


@pytest.fixture
def tracing():
    """A Tracing that exports to memory, and the exporter."""
    exporter = InMemorySpanExporter()
    t = Tracing.start(CONFIG, exporter=exporter)
    yield t, exporter
    t.close()


class Reporting(FakeDecider):
    """A fake decider that reports each decision's reason and model calls, as the remote decider does."""

    def decide(self, observation: str, history: Sequence[Try]) -> list[Action]:
        self.tries = len(history) + 1
        return super().decide(observation, history)

    def last_report(self) -> dict:
        return {
            "reason": f"try {self.tries}",
            "calls": [{"model": "m", "input_tokens": 600, "output_tokens": 150, "valid": True}],
        }


class Signing(FakeSigner):
    """A signer whose credential looks real."""

    def sign(self, action: Action) -> str:
        super().sign(action)
        return CREDENTIAL


def test_an_incident_is_one_trace_with_a_span_per_step(tracing) -> None:
    t, exporter = tracing
    world = FakeWorld(fix=["c"])
    with CommandTrace(t, "incident") as trace:
        ports = traced(trace.tracer, world, Reporting([["a"], ["c"]]), allow("a", "c"), Signing())
        out = run_incident(ports[0], ports[1], LoopConfig(wait_s=30, hold_s=30), ports[2], ports[3])
    assert out.resolved and t.flush()
    spans = exporter.get_finished_spans()
    names = [s.name for s in spans]
    assert names.count("decide") == 2 and names.count("policy") == 2 and names.count("model m") == 2
    # try 1: apply a, then its undo after it was not verified; try 2: apply c
    assert [s.attributes[LF_INPUT] for s in spans if s.name == "apply"] == ["a()", "undo:a()", "c()"]
    assert len({s.context.trace_id for s in spans}) == 1
    root = next(s for s in spans if s.name == "incident")
    assert all(
        s.parent is not None and s.parent.span_id == root.context.span_id
        for s in spans
        if s is not root and s.name != "model m"
    )
    decide = [s for s in spans if s.name == "decide"]
    assert [d.attributes["loop.try"] for d in decide] == [1, 2]
    assert decide[1].attributes["loop.reason"] == "try 2" and decide[1].attributes[LF_OUTPUT] == '["c()"]'
    generation = next(s for s in spans if s.name == "model m")
    assert generation.attributes[LF_TYPE] == "generation" and generation.attributes[GEN_AI_REQUEST_MODEL] == "m"
    assert generation.attributes[GEN_AI_INPUT_TOKENS] == 600
    assert generation.parent is not None and generation.parent.span_id in {d.context.span_id for d in decide}
    waits = [s.attributes["loop.wait_s"] for s in spans if s.name == "wait"]
    assert waits == [30, 30, 32.0]  # try 1: WAIT; try 2: WAIT, then HOLD and its margin
    assert not any(CREDENTIAL in str(v) for s in spans for v in s.attributes.values())


def test_a_refusal_and_a_failed_action_are_recorded(tracing) -> None:
    t, exporter = tracing
    world = FakeWorld(fix=["c"], refuse=["c"])
    with CommandTrace(t, "incident") as trace:
        ports = traced(trace.tracer, world, FakeDecider([["x"], ["c"]]), allow("c"), FakeSigner())
        out = run_incident(ports[0], ports[1], LoopConfig(wait_s=30, hold_s=30), ports[2], ports[3])
    assert out.reason.startswith("action failed") and t.flush()
    spans = exporter.get_finished_spans()
    policy = [s for s in spans if s.name == "policy"]
    assert policy[0].attributes["loop.allowed"] is False and policy[1].attributes["loop.allowed"] is True
    applied = next(s for s in spans if s.name == "apply")
    assert applied.attributes["loop.ok"] is False and applied.status.status_code == StatusCode.ERROR


def test_a_decider_that_raises_is_a_failed_span_and_the_loop_still_ends(tracing) -> None:
    class Down:
        def decide(self, observation: str, history: Sequence[Try]) -> list[Action]:
            raise ConnectionError("agent unreachable")

    t, exporter = tracing
    with CommandTrace(t, "incident") as trace:
        ports = traced(trace.tracer, FakeWorld(), Down(), allow("a"), FakeSigner())
        out = run_incident(ports[0], ports[1], LoopConfig(wait_s=30, hold_s=30), ports[2], ports[3])
    assert out.reason.startswith("decider failed") and t.flush()
    decide = next(s for s in exporter.get_finished_spans() if s.name == "decide")
    assert decide.status.status_code == StatusCode.ERROR


def test_without_a_tracer_the_ports_are_unchanged() -> None:
    world, decider, policy, signer = FakeWorld(), FakeDecider([["a"]]), allow("a"), FakeSigner()
    assert traced(None, world, decider, policy, signer) == (world, decider, policy, signer)


def test_the_traced_policy_answers_as_the_policy_does(tracing) -> None:
    t, _ = tracing
    inner = allow("a")
    _, _, policy, _ = traced(t.tracer(), FakeWorld(), FakeDecider([["a"]]), inner, FakeSigner())
    assert policy.check([Action("b")]) == inner.check([Action("b")]) and policy.kinds() == inner.kinds()


def test_a_decision_that_is_not_actions_ends_as_it_would_untraced(tracing) -> None:
    class Odd:
        def decide(self, observation: str, history: Sequence[Try]) -> list[object]:
            return ["not an action"]

    t, _ = tracing
    plain = run_incident(FakeWorld(), Odd(), LoopConfig(wait_s=30, hold_s=30), allow("a"), FakeSigner())
    with CommandTrace(t, "incident") as trace:
        ports = traced(trace.tracer, FakeWorld(), Odd(), allow("a"), FakeSigner())
        out = run_incident(ports[0], ports[1], LoopConfig(wait_s=30, hold_s=30), ports[2], ports[3])
    assert out.reason == plain.reason == "decision holds something that is not an Action"
