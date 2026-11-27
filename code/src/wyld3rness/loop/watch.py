"""The loop, watched: each step `run_incident` takes becomes an event a viewer can show as it happens.

    try_started   try n begins: the decider is asked
    decided       what it proposed, its reason, its model calls, the incident's spend so far
    checked       the policy's answer: allowed, or every reason it was refused
    applied       one action carried out or not, its detail; `undo_of` when it puts an earlier one back
    waiting       the loop waits n seconds (WAIT, then HOLD and its margin)
    verdict       how try n ended: resolved, not verified, did not hold, denied, action failed, no undo
    incident_ended  ended (resolved or safe state), the reason, what is still in force

A driver adds its own kinds around these (`storm_started`, `storm_phase`,
`customers`, `incident_sent`, `storm_finished`, `storm_failed`), so one
vocabulary covers a storm from reset to verdict. Every value is JSON-safe and
every time is on the incident's clock (`env.now()`), so a viewer that knows
when the incident started puts the loop's events on its own clock by adding
that.

Why wrappers and not hooks in `run_incident`: the same reason as tracing
(`traced.py`). The loop stays one readable function, and anything that takes
the four ports can be watched. A wrapper passes each call through and records
it; it never changes an answer.

When a verdict is known: the loop records a try once it is over, after its
undos. The decider wrapper sees every finished try in the history of the next
decision, and `Watch.finish` sees the last ones in the outcome; so a verdict
event follows the undos of its try, the order the record keeps.

What an event never holds: a credential. The signer's wrapper records nothing,
and `apply` drops its credential argument. Model output (a reason) is untrusted text: a
viewer must show it as text.

Thread safety: `EventLog` is written by the loop's thread and read by a
server's; one lock guards it. At scale an incident's events would go to a
stream with retention (a queue or the trace backend); here only the latest
incidents are kept, in memory.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping, Sequence
import threading
from typing import Any, Literal

from wyld3rness.loop.credential import Signer
from wyld3rness.loop.env import Action, Applied, Environment
from wyld3rness.loop.policy import Policy
from wyld3rness.loop.run import Decider, Outcome, Try
from wyld3rness.loop.verdict import Second

LoopEventKind = Literal[
    "storm_started",
    "storm_phase",
    "customers",
    "incident_sent",
    "try_started",
    "decided",
    "checked",
    "applied",
    "waiting",
    "verdict",
    "handoff",
    "incident_ended",
    "storm_finished",
    "storm_failed",
]

LoopEmit = Callable[[str, float, dict[str, Any]], None]  # kind, time on the incident's clock, data


class EventLog:
    """Events of one incident, in order, as plain JSON objects. Thread-safe; written by one loop."""

    def __init__(self) -> None:
        """An empty log, not done."""
        self._lock = threading.Lock()
        self._events: list[dict[str, Any]] = []
        self.done = False

    def emit(self, kind: str, t: float, data: dict[str, Any]) -> None:
        """Append one event: its kind, its time on the incident's clock, its data."""
        with self._lock:
            self._events.append({"kind": kind, "t": round(t, 3), "data": data})

    def close(self) -> None:
        """Mark the incident over: no event follows."""
        with self._lock:
            self.done = True

    def since(self, index: int) -> tuple[list[dict[str, Any]], bool]:
        """The events from `index` on, and whether the incident is over (read under the same lock)."""
        with self._lock:
            return self._events[index:], self.done


def _label(action: object) -> str:
    """An action's label, or its repr when it is not an Action: watching never changes what the loop sees."""
    return action.label() if isinstance(action, Action) else repr(action)


def try_json(t: Try) -> dict[str, Any]:
    """The data of one try's `verdict` event. Pure."""
    return {
        "try": t.number,
        "verdict": t.verdict,
        "recovered_at": t.recovered_at,
        "undone": [_label(u.action) for u in t.undone],
        "reasons": list(t.reasons),
    }


class Watch:
    """Wraps the loop's four ports so each step is an event, and closes the incident's events at the end."""

    def __init__(self, emit: LoopEmit) -> None:
        """`emit(kind, t, data)` receives every event; it must not raise and must return quickly."""
        self._emit = emit
        self._clock: Callable[[], float] = lambda: 0.0
        self.number = 0  # the try in flight, 0 before the first decision
        self._said = 0  # tries whose verdict was emitted
        self._undos: list[tuple[Action, Action]] = []  # (undo, the action it puts back), for actions in force
        self._to_apply = 0  # actions of the allowed decision not yet applied; any other apply is an undo

    def emit(self, kind: LoopEventKind, **data: Any) -> None:
        """One event, stamped with the environment's clock."""
        self._emit(kind, self._clock(), data)

    def verdicts(self, tries: Sequence[Try]) -> None:
        """A `verdict` event for each finished try not yet reported."""
        for t in tries[self._said :]:
            self.emit("verdict", **try_json(t))
        self._said = max(self._said, len(tries))

    def finish(self, outcome: Outcome, handoff: dict[str, Any] | None = None) -> None:
        """The last verdicts, the hand-off when there was one, then `incident_ended`. Call once, after `run_incident`.

        `handoff` is what a viewer shows of it (`wyld3rness.loop.handoff.summary`).
        """
        self._to_apply = 0
        self.verdicts(outcome.tries)
        if handoff is not None:
            self.emit("handoff", **handoff)
        self.emit(
            "incident_ended",
            ended=outcome.ended,
            reason=outcome.reason,
            kept=[_label(a) for a in outcome.kept],
            safe_undos=[_label(a.action) for a in outcome.safe_undos],
            tries_used=len(outcome.tries),
        )

    def ports(
        self, env: Environment, decider: Decider, policy: Policy, signer: Signer
    ) -> tuple[Environment, Decider, Policy, Signer]:
        """The four ports, watched. The signer's credential is never an event: its wrapper only notes a failure."""
        self._clock = env.now
        return (
            WatchedEnvironment(env, self),
            WatchedDecider(decider, self),
            WatchedPolicy(policy, self),
            WatchedSigner(signer, self),
        )


class WatchedDecider:
    """A decider whose every decision is two events: `try_started`, then `decided` (with its report)."""

    def __init__(self, inner: Decider, watch: Watch) -> None:
        """Wrap `inner`."""
        self.inner, self.watch = inner, watch

    def last_report(self) -> Mapping[str, Any]:
        """The inner decider's report, so a wrapper around this one (tracing) still sees the model calls."""
        method = getattr(self.inner, "last_report", None)  # a decider that reports its calls has this method
        report: Mapping[str, Any] = method() if callable(method) else {}
        return report

    def decide(self, observation: str, history: Sequence[Try]) -> Sequence[Action]:
        """The inner decision; the tries finished before it are reported first."""
        self.watch.verdicts(history)
        self.watch.number = len(history) + 1
        self.watch.emit("try_started", **{"try": self.watch.number})
        error = None
        actions: Sequence[Action] = ()
        try:
            actions = self.inner.decide(observation, history)
            self.watch._to_apply = len(actions)
            return actions
        except Exception as exc:
            error = f"{type(exc).__name__}: {exc}"
            raise
        finally:
            report = self.last_report()
            self.watch.emit(
                "decided",
                **{"try": self.watch.number},
                actions=[_label(a) for a in actions] if error is None else [],
                reason=report.get("reason"),
                calls=list(report.get("calls", [])),
                spent_usd=report.get("spent_usd"),
                error=error,
            )


class WatchedPolicy(Policy):
    """A policy whose every check is a `checked` event."""

    def __init__(self, inner: Policy, watch: Watch) -> None:
        """Wrap `inner`: the same rules, checked by it."""
        self.inner, self.watch = inner, watch

    def kinds(self) -> list[str]:
        """The inner policy's kinds."""
        return self.inner.kinds()

    def notes(self) -> list[str]:
        """The inner policy's notes."""
        return self.inner.notes()

    def check(self, decision: Sequence[Action]) -> list[str]:
        """The inner check, reported: allowed or not, and why not."""
        refused = self.inner.check(decision)
        if refused:
            self.watch._to_apply = 0
        self.watch.emit("checked", **{"try": self.watch.number}, allowed=not refused, reasons=list(refused))
        return refused


class WatchedEnvironment:
    """An environment whose actions and waits are events; observation and measurement pass through."""

    def __init__(self, inner: Environment, watch: Watch) -> None:
        """Wrap `inner`."""
        self.inner, self.watch = inner, watch

    def observe(self) -> str:
        """The inner observation (the decider's `decided` event says what came of it)."""
        return self.inner.observe()

    def apply(self, action: Action, credential: str) -> Applied:
        """The inner apply, reported; an action that puts an earlier one back is marked `undo_of` it.

        The loop applies a decision's actions first, then only undos (of a try
        that did not resolve it, or in the safe state), so counting the
        decision's actions tells the two apart, even when an undo looks like a
        proposal. The count ends early when the decision stops early: an action
        that fails or raises, or one that cannot be signed.
        """
        watch, undo_of = self.watch, None
        proposed = watch._to_apply > 0
        if proposed:
            watch._to_apply -= 1
        else:
            undo_of = next((done for undo, done in watch._undos if undo == action), None)
        try:
            result = self.inner.apply(action, credential)
        except Exception as exc:  # the loop records it as failed; the viewer sees it too, then the loop stops
            watch._to_apply = 0
            watch.emit(
                "applied",
                **{"try": watch.number},
                action=action.label(),
                ok=False,
                detail=f"apply raised: {type(exc).__name__}: {exc}",
                undo_of=undo_of.label() if undo_of is not None else None,
            )
            raise
        if not result.ok:  # the loop stops on a failed action: whatever is applied next is an undo
            watch._to_apply = 0
        if undo_of is not None:
            watch._undos.remove((action, undo_of))
        elif proposed and result.undo is not None:
            watch._undos.append((result.undo, action))
        self.watch.emit(
            "applied",
            **{"try": self.watch.number},
            action=action.label(),
            ok=result.ok,
            detail=result.detail,
            undo_of=undo_of.label() if undo_of is not None else None,
        )
        return result

    def series(self) -> Sequence[Second]:
        """The inner measurement: the verdict's, read often, never an event."""
        return self.inner.series()

    def now(self) -> float:
        """The inner clock."""
        return self.inner.now()

    def wait(self, seconds: float) -> None:
        """The inner wait, announced as it starts. Every action of the decision is applied by now."""
        self.watch._to_apply = 0
        self.watch.emit("waiting", seconds=seconds)
        self.inner.wait(seconds)


class WatchedSigner:
    """A signer passed through; a failure to sign is noted, because the loop then stops and only undoes."""

    def __init__(self, inner: Signer, watch: Watch) -> None:
        """Wrap `inner`."""
        self.inner, self.watch = inner, watch

    def sign(self, action: Action) -> str:
        """The inner credential, never recorded. On a failure, whatever is applied next is an undo."""
        try:
            return self.inner.sign(action)
        except Exception:
            self.watch._to_apply = 0
            raise
