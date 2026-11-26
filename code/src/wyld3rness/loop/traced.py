"""The loop, traced: each part `run_incident` calls gets a span, inside the incident's span, as it happens.

    incident (the caller's span)
      ├─ observe                 the observation's size
      ├─ decide                  try n: the observation in, the actions out; the decider's reason
      │    └─ model <name>       one per model call the decider reports: tokens, valid (a generation)
      ├─ policy                  the decision in, every refusal out
      ├─ sign                    the action signed (never the credential)
      ├─ apply                   the action, carried out or not, its detail, whether it has an undo
      ├─ wait                    how long the loop waited (WAIT, then HOLD)
      └─ ... next try, undo (an apply), the safe state

Why wrappers and not hooks in `run_incident`: the loop stays one readable
function with no tracing in it, and anything that takes the four ports gets
traced the same way. Each wrapper only passes the call through and records it;
a wrapper never changes an answer, and with tracing off nothing here is built.

Model calls happen in the decider, which may run in another process with no
route to the trace backend (`remote.py`): it reports each decision's calls
(model, tokens, valid) and reason with its answer, failed decisions included,
through its `last_report()` method, and the
decide span adds one generation per call. They are timed as the whole decision,
because the agent's own clock is not on this trace; the backend prices them
from the model and tokens.

What a span never holds: a credential. The signer's token is a bearer secret
for 60 s, so `sign` records the action only, and `apply` drops its argument.
"""

from __future__ import annotations

from collections.abc import Iterator, Mapping, Sequence
from contextlib import contextmanager
import time
from typing import TYPE_CHECKING, Any

from wyld3rness.loop.credential import Signer
from wyld3rness.loop.env import Action, Applied, Environment
from wyld3rness.loop.policy import Policy
from wyld3rness.loop.run import Decider, Try
from wyld3rness.loop.verdict import Second
from wyld3rness.trace.spans import (
    GEN_AI_INPUT_TOKENS,
    GEN_AI_OPERATION,
    GEN_AI_OUTPUT_TOKENS,
    GEN_AI_PROVIDER,
    GEN_AI_REQUEST_MODEL,
    LF_INPUT,
    LF_OUTPUT,
    LF_TYPE,
    as_text,
    attribute_value,
    mark_failed,
)

if TYPE_CHECKING:
    from opentelemetry.trace import Span, Tracer


@contextmanager
def _span(tracer: Tracer, name: str, input: Any = None, **attributes: Any) -> Iterator[Span]:
    """A current child span; an exception marks it failed and is re-raised. None values are left out."""
    with tracer.start_as_current_span(name, record_exception=False, set_status_on_exception=False) as span:
        values = {k: attribute_value(v) for k, v in attributes.items() if v is not None}
        if input is not None:
            values[LF_INPUT] = as_text(input)
        span.set_attributes({k: v for k, v in values.items() if v is not None})
        try:
            yield span
        except BaseException as err:
            mark_failed(span, f"{type(err).__name__}: {err}")
            raise


def _labels(actions: Sequence[Action]) -> list[str]:
    """Each action as its label, or its repr when it is not an Action: recording never changes what the loop sees."""
    return [a.label() if isinstance(a, Action) else repr(a) for a in actions]


class TracedDecider:
    """A decider whose every decision is a span, with the model calls it reports as generations."""

    def __init__(self, inner: Decider, tracer: Tracer, provider: str = "ollama") -> None:
        """Wrap `inner`; `provider` names the model server on each generation."""
        self.inner, self.tracer, self.provider = inner, tracer, provider

    def decide(self, observation: str, history: Sequence[Try]) -> Sequence[Action]:
        """The inner decision, recorded with its try number, input, output and the calls it reports."""
        start = time.time_ns()
        with _span(self.tracer, "decide", observation, **{"loop.try": len(history) + 1}) as span:
            try:
                actions = self.inner.decide(observation, history)
            finally:
                self._report(span, start)
            span.set_attribute(LF_OUTPUT, as_text(_labels(actions)))
            return actions

    def _report(self, span: Span, start: int) -> None:
        """The decider's reason and one generation per model call it reported for this decision, if any."""
        method = getattr(self.inner, "last_report", None)  # a decider that reports its calls has this method
        report: Mapping[str, Any] = method() if callable(method) else {}
        if report.get("reason"):
            span.set_attribute("loop.reason", as_text(report["reason"]))
        end = time.time_ns()
        for call in report.get("calls", []):
            model = str(call.get("model", "unknown"))
            values = {
                LF_TYPE: "generation",
                GEN_AI_OPERATION: "chat",
                GEN_AI_PROVIDER: self.provider,
                GEN_AI_REQUEST_MODEL: model,
                GEN_AI_INPUT_TOKENS: call.get("input_tokens"),
                GEN_AI_OUTPUT_TOKENS: call.get("output_tokens"),
                "loop.valid": call.get("valid"),
            }
            generation = self.tracer.start_span(
                f"model {model}", start_time=start, attributes={k: v for k, v in values.items() if v is not None}
            )
            generation.end(end_time=end)


class TracedPolicy(Policy):
    """A policy whose every check is a span: the decision, and every reason it was refused."""

    def __init__(self, inner: Policy, tracer: Tracer) -> None:
        """Wrap `inner`: the same rules, checked by it."""
        self.inner, self.tracer = inner, tracer

    def kinds(self) -> list[str]:
        """The inner policy's kinds."""
        return self.inner.kinds()

    def notes(self) -> list[str]:
        """The inner policy's notes."""
        return self.inner.notes()

    def check(self, decision: Sequence[Action]) -> list[str]:
        """The inner check, recorded: allowed or not, and why not."""
        with _span(self.tracer, "policy", _labels(decision)) as span:
            refused = self.inner.check(decision)
            span.set_attributes({"loop.allowed": not refused, LF_OUTPUT: as_text(refused or "allowed")})
            return refused


class TracedSigner:
    """A signer whose every signature is a span naming the action, never the credential."""

    def __init__(self, inner: Signer, tracer: Tracer) -> None:
        """Wrap `inner`."""
        self.inner, self.tracer = inner, tracer

    def sign(self, action: Action) -> str:
        """The inner signature; the span records the action and whether it was signed."""
        with _span(self.tracer, "sign", action.label()):
            return self.inner.sign(action)


class TracedEnvironment:
    """An environment whose observations, actions and waits are spans; its measurement passes through untraced."""

    def __init__(self, inner: Environment, tracer: Tracer) -> None:
        """Wrap `inner`."""
        self.inner, self.tracer = inner, tracer

    def observe(self) -> str:
        """The inner observation, recorded by its size (the decide span holds the text)."""
        with _span(self.tracer, "observe") as span:
            text = self.inner.observe()
            span.set_attribute("loop.observation_chars", len(text))
            return text

    def apply(self, action: Action, credential: str) -> Applied:
        """The inner apply, recorded with its result; the credential is passed on and never recorded."""
        with _span(self.tracer, "apply", action.label()) as span:
            result = self.inner.apply(action, credential)
            span.set_attributes(
                {"loop.ok": result.ok, "loop.has_undo": result.undo is not None, LF_OUTPUT: as_text(result.detail)}
            )
            if not result.ok:
                mark_failed(span, result.detail)
            return result

    def series(self) -> Sequence[Second]:
        """The inner measurement, untraced: the loop reads it often and it is the verdict's, not an event."""
        return self.inner.series()

    def now(self) -> float:
        """The inner clock."""
        return self.inner.now()

    def wait(self, seconds: float) -> None:
        """The inner wait, as a span as long as the wait."""
        with _span(self.tracer, "wait", **{"loop.wait_s": seconds}):
            self.inner.wait(seconds)


def traced(
    tracer: Tracer | None, env: Environment, decider: Decider, policy: Policy, signer: Signer
) -> tuple[Environment, Decider, Policy, Signer]:
    """The four ports, each wrapped so its calls are spans; unchanged when `tracer` is None."""
    if tracer is None:
        return env, decider, policy, signer
    return (
        TracedEnvironment(env, tracer),
        TracedDecider(decider, tracer),
        TracedPolicy(policy, tracer),
        TracedSigner(signer, tracer),
    )
