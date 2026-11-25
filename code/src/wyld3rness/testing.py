"""Test doubles for the harness: a judge, an embedder, a system under test, and a world for the loop.

Shipped with the package, not kept in a test folder, so an app's own tests
can drive the harness without a model: FindyFixy's adapter tests use the
fake judge and embedder. They use harness types only. The fake system under
test has one label, `kind`, so the harness's own tests prove it works for an
app that is not FindyFixy. Nothing on an eval path imports this module.

`FakeWorld` and `FakeDecider` drive the loop (`wyld3rness.loop.run`) on a
clock that moves only when the loop waits, so a 3-try incident with 30 s
windows runs in milliseconds and every path is deterministic.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping, Sequence
from datetime import UTC, datetime
import threading
from typing import Any

from pydantic import BaseModel

from wyld3rness.eval.case import Case
from wyld3rness.eval.judge import JudgeError
from wyld3rness.eval.metrics import ActionRating, Claims, Questions, Verdict
from wyld3rness.eval.runner import run_eval
from wyld3rness.eval.sut import Answer
from wyld3rness.loop.env import Action, Applied
from wyld3rness.loop.run import Try
from wyld3rness.loop.verdict import Second

NOW = datetime(2026, 10, 26, 12, 30, 5, tzinfo=UTC)
# A clean tree: the traceability rules refuse an unknown git state (None).
PROV = {
    "judge_digest": "d1",
    "prompt_versions": {"m": "h"},
    "golden_sha256": "g1",
    "git_sha": "abc1234",
    "git_dirty": False,
}


def case(cid: str, group: str = "a", kind: str | None = "x", **meta: Any) -> Case:
    """A case expecting label `kind` (none when `kind` is None)."""
    return Case(
        id=cid,
        group=group,
        question=f"question {cid}",
        reference="Root cause: a. Remediation: b.",
        expected={} if kind is None else {"kind": kind},
        meta=meta,
    )


class FakeJudge:
    """Answers every question. Records prompts. Fails when told to."""

    def __init__(self, supported: bool = True, fail: bool = False) -> None:
        """`supported` sets every verdict; `fail` makes every call raise `JudgeError`."""
        self.supported, self.fail = supported, fail
        self.prompts: list[str] = []

    def ask(self, prompt: str, schema: type[BaseModel]) -> Any:
        """A canned reply of the right schema."""
        self.prompts.append(prompt)
        if self.fail:
            raise JudgeError("fake failure")
        if schema is Claims:
            return Claims(claims=["it is auth"])
        if schema is Questions:
            return Questions(questions=["q1", "q2", "q3"])
        if schema is ActionRating:
            return ActionRating(rating="helpful" if self.supported else "unhelpful", reason="fake")
        return Verdict(supported=self.supported)


def embed(texts: Any) -> list[list[float]]:
    """Every text maps to the same unit vector: cosine 1.0."""
    return [[1.0, 0.0] for _ in texts]


class FakeSUT:
    """A system that always answers `kind=label`, with knobs for every failure path."""

    def __init__(
        self,
        label: str = "x",
        *,
        fail_on: Sequence[str] = (),
        raise_on: Sequence[str] = (),
        action: str | None = "roll back",
        meta: Mapping[str, Any] | None = None,
        barrier: threading.Barrier | None = None,
    ) -> None:
        """`fail_on` ids return `Answer.failed`; `raise_on` ids raise; `barrier` waits."""
        self.label, self.action, self.barrier = label, action, barrier
        self.fail_on, self.raise_on = set(fail_on), set(raise_on)
        self.meta = dict(meta) if meta is not None else {"model_used": "small"}

    def describe(self) -> dict[str, Any]:
        """The fake's settings."""
        return {"fake_model": "m1", "fake_threshold": 0.5}

    def answer(self, case: Case) -> Answer:
        """A canned answer whose context names the case, so a leak across cases shows."""
        if self.barrier is not None:
            self.barrier.wait()
        if case.id in self.raise_on:
            raise RuntimeError("model never produced a valid record")
        if case.id in self.fail_on:
            return Answer.failed("TriageError: no valid record", {**self.meta, "model_used": None})
        return Answer(
            text=f"Kind {self.label}. Next action: {self.action}",
            contexts=[f"context for {case.id}"],
            action=self.action,
            labels={"kind": self.label},
            meta=self.meta,
        )

    def summarise(self, records: Sequence[Mapping[str, Any]]) -> dict[str, Any]:
        """One extra aggregate: the share of records that ran."""
        ran = [r for r in records if r["run_error"] is None]
        return {"ran_rate": len(ran) / len(records) if records else None}


def run(cases: Sequence[Case], sut: Any = None, judge: Any = None, **kw: Any) -> dict[str, Any]:
    """`run_eval` with the fakes and a fixed provenance."""
    return run_eval(
        cases,
        sut=sut or FakeSUT(),
        judge=judge or FakeJudge(),
        embed=kw.pop("embed", embed),
        provenance=kw.pop("provenance", PROV),
        **kw,
    )


class FakeWorld:
    """An environment whose users recover while the actions named in `fix` are all in force.

    Each action is undone by `undo:<name>`, except those named in `no_undo`.
    Every waited second adds one Second: 200 ok when fixed, 100 ok and 100
    failed when not. `delay_s` is how long a fix is in force before users
    recover; `relapse_after_s` turns them bad again after that many good
    seconds (a recovery that does not hold). `refuse` names actions (or
    undos) answered not ok; `raise_on` names actions whose apply raises after
    the change is made (a reply cut short); `apply_s` is how long each apply takes.
    """

    def __init__(
        self,
        fix: Sequence[str] = ("a",),
        *,
        no_undo: Sequence[str] = (),
        refuse: Sequence[str] = (),
        raise_on: Sequence[str] = (),
        delay_s: float = 0.0,
        relapse_after_s: float | None = None,
        apply_s: float = 0.0,
    ) -> None:
        """A world that starts in an incident, at t = 0."""
        self.fix, self.no_undo = set(fix), set(no_undo)
        self.refuse, self.raise_on = set(refuse), set(raise_on)
        self.delay_s, self.relapse_after_s, self.apply_s = delay_s, relapse_after_s, apply_s
        self.t = 0.0
        self.fixed_s = 0.0
        self.in_force: set[str] = set()
        self.good_s = 0.0
        self.seconds: list[Second] = []
        self.log: list[str] = []  # every action and undo applied, in order

    def observe(self) -> str:
        """What is in force, sorted."""
        return f"in force: {sorted(self.in_force)}"

    def apply(self, action: Action) -> Applied:
        """Apply one action or undo; an undo of `x` is `undo:x`."""
        self.log.append(action.name)
        self.wait(self.apply_s)
        if action.name in self.raise_on:
            self.in_force.add(action.name)  # the change went through; only the reply was lost
            raise RuntimeError(f"{action.name} exploded")
        if action.name in self.refuse:
            return Applied(action, False, "refused", self._undo_of(action))
        if action.name.startswith("undo:"):
            self.in_force.discard(action.name.removeprefix("undo:"))
            return Applied(action, True, "undone", None)
        self.in_force.add(action.name)
        return Applied(action, True, "done", self._undo_of(action))

    def _undo_of(self, action: Action) -> Action | None:
        """`undo:<name>`, or None for an undo or an action named in `no_undo`."""
        if action.name.startswith("undo:") or action.name in self.no_undo:
            return None
        return Action(f"undo:{action.name}")

    def series(self) -> list[Second]:
        """Every second so far."""
        return list(self.seconds)

    def now(self) -> float:
        """The fake clock."""
        return self.t

    def wait(self, seconds: float) -> None:
        """Advance the clock one second at a time, recording each second's users."""
        for _ in range(int(seconds)):
            self.t += 1.0
            fixed = bool(self.fix) and self.fix <= self.in_force
            self.fixed_s = self.fixed_s + 1 if fixed else 0.0
            recovered = fixed and self.fixed_s > self.delay_s
            self.good_s = self.good_s + 1 if recovered else 0.0
            relapsed = self.relapse_after_s is not None and self.good_s > self.relapse_after_s
            good = recovered and not relapsed
            self.seconds.append(Second(self.t, 200, 0) if good else Second(self.t, 100, 100))


class FakeDecider:
    """Proposes the scripted decisions in order and records what it was shown. `fail` raises instead."""

    def __init__(self, decisions: Sequence[Sequence[str]], fail: Callable[[int], bool] = lambda n: False) -> None:
        """`decisions[i]` are the action names for try i + 1; `fail(n)` makes try n raise."""
        self.decisions = [[Action(n) for n in d] for d in decisions]
        self.fail = fail
        self.seen: list[tuple[str, list[Try]]] = []

    def decide(self, observation: str, history: Sequence[Try]) -> list[Action]:
        """The next scripted decision."""
        self.seen.append((observation, list(history)))
        n = len(self.seen)
        if self.fail(n):
            raise ValueError(f"decision {n} invalid after its retry")
        return self.decisions[n - 1]
