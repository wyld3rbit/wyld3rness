"""The loop: one incident, from the first decision to resolved or the safe state. Code owns it, not the model.

    try 1..TRIES:
      observe ─► decide: 1 to MAX_ACTIONS typed actions, applied, verified and undone together
        ─► apply each ─► wait WAIT ─► verified? (the last VERIFY_S all good)
             no  ─► undo, last first ─► next try, told what failed
             yes ─► hold HOLD ─► recovered within WAIT and held for HOLD?
                                   yes ─► resolved: keep the actions, stop
                                   no  ─► undo, last first ─► next try
    safe state: undo every action still in force that has an undo, last first; stop, unresolved.
      reached when: out of tries; the decider raised or broke the shape; an action failed;
      an undo failed; an action without undo did not resolve it; the environment raised.

The model proposes; this module decides what happens next, from the environment's
measurement (`verdict.py`), never from the model's own account. Nothing here
waits for a person: every path ends resolved or in the safe state.

Why undo before retrying: a retry from the state a failed set left behind can be
worse than no retry, and the next decision would be judged on a mix. Why an
action without undo ends the incident when it does not resolve it: the system is
no longer in a state the loop knows how to return from, so another try would
compound an unknown. The safe state here is "put back every change that can be
put back, then change nothing"; a containment beyond that (shedding a tenant,
paging) is the environment's to add when one is approved in advance.

Plain Python, not a graph framework: every step is a line here and every path is
tested with fakes (`code/tests/test_loop.py`). The trade-off is that branching
added later (parallel tries, a person in the loop) is written by hand. The policy
check, the credential and the budget (Day 7, step 4) wrap `apply` and `decide`
without changing this sequence.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass, field
from typing import Literal, Protocol

from wyld3rness.loop.env import Action, Applied, Environment
from wyld3rness.loop.verdict import JUDGE_S, VERIFY_S, judged, recovered_at, verified

TRIES = 3
MAX_ACTIONS = 2
# After HOLD, wait this much longer for the last seconds to be measured: the scraper reports
# a second after it ends, and a series that stops short of the hold window is not held.
HOLD_MARGIN_S = 2.0

Verdict = Literal["resolved", "not verified", "did not hold", "action failed", "no undo"]


@dataclass(frozen=True)
class Try:
    """One try: the actions, what became of them, the verdict, and the undos it took.

    `t_act` is when the last action was applied (when one failed, when it failed).
    """

    number: int
    actions: tuple[Action, ...]
    applied: tuple[Applied, ...]
    t_act: float
    verdict: Verdict
    recovered_at: float | None = None
    undone: tuple[Applied, ...] = ()


class Decider(Protocol):
    """Proposes the next set of actions, from what it can read and what was tried. May raise."""

    def decide(self, observation: str, history: Sequence[Try]) -> Sequence[Action]:
        """1 to MAX_ACTIONS actions to apply together."""
        ...


@dataclass(frozen=True)
class LoopConfig:
    """The loop's limits and windows. WAIT and HOLD come from the environment's pilot, so they have no default."""

    wait_s: float
    hold_s: float
    tries: int = TRIES
    max_actions: int = MAX_ACTIONS
    verify_s: float = VERIFY_S
    judge_s: float = JUDGE_S
    hold_margin_s: float = HOLD_MARGIN_S


@dataclass
class Outcome:
    """How an incident ended, with every try. `kept` is what is still in force at the end."""

    ended: Literal["resolved", "safe state"]
    reason: str
    tries: list[Try] = field(default_factory=list)
    kept: list[Action] = field(default_factory=list)
    safe_undos: list[Applied] = field(default_factory=list)

    @property
    def resolved(self) -> bool:
        """Recovered and held, with the last try's actions kept."""
        return self.ended == "resolved"


class _Stop(Exception):
    """Ends the incident in the safe state with `reason`. Internal to `run_incident`."""

    def __init__(self, reason: str) -> None:
        super().__init__(reason)
        self.reason = reason


def _undo_try(env: Environment, in_force: list[Applied], count: int) -> tuple[list[Applied], str | None]:
    """Undo the last `count` actions in force, last first, popping each one undone. Returns the undos and any error.

    A failed action is undone too when its undo is known: it may have happened.
    An undo that fails stops here and leaves its action in force, so the safe
    state tries it once more, with everything before it.
    """
    done = []
    for _ in range(count):
        a = in_force[-1]
        if a.undo is not None:
            result = env.apply(a.undo)
            done.append(result)
            if not result.ok:
                return done, f"undo failed: {a.undo.label()}: {result.detail}"
        in_force.pop()
    return done, None


def _apply(env: Environment, action: Action) -> Applied:
    """Apply one action; an apply that raises is a failed action that may be in force, with no known undo.

    The request may have reached the system before the failure (a reply cut
    short, an unreadable body), so the record keeps it as possibly in force
    instead of dropping it, and the safe state lists it in `kept`.
    """
    try:
        return env.apply(action)
    except Exception as exc:
        return Applied(action, False, f"apply raised: {type(exc).__name__}: {exc}", None)


def _check(actions: tuple[Action, ...], config: LoopConfig) -> tuple[Action, ...]:
    """The decision, if it has the shape the loop accepts. Raises `_Stop` otherwise. Pure.

    The decider validates its own output and retries; this is the loop's own
    boundary check, so a decider that skips it cannot send ten actions.
    """
    if not 1 <= len(actions) <= config.max_actions:
        raise _Stop(f"decision has {len(actions)} actions, allowed 1 to {config.max_actions}")
    if not all(isinstance(a, Action) for a in actions):
        raise _Stop("decision holds something that is not an Action")
    return actions


def _judge(env: Environment, t_act: float, config: LoopConfig) -> tuple[Verdict, float | None]:
    """Wait, verify, hold: the verdict on the actions applied at `t_act`, from the environment's measurement."""
    env.wait(config.wait_s)
    if not verified(judged(env.series(), config.judge_s), env.now(), config.verify_s):
        return "not verified", None
    env.wait(config.hold_s + config.hold_margin_s)
    at = recovered_at(judged(env.series(), config.judge_s), t_act, config.wait_s, config.hold_s)
    return ("resolved", at) if at is not None else ("did not hold", None)


def run_incident(env: Environment, decider: Decider, config: LoopConfig) -> Outcome:
    """Run one incident through at most `config.tries` tries. Never raises: every failure is the safe state.

    Failure modes, each ending in the safe state with its reason: the decider
    raised or broke the shape; an action was not carried out, or its apply
    raised (then it is kept as possibly in force); an undo failed;
    an action with no undo did not resolve it; the environment raised; out of tries.
    """
    out = Outcome("safe state", "")
    in_force: list[Applied] = []  # carried out and not yet undone, in order
    try:
        for number in range(1, config.tries + 1):
            observation = env.observe()
            try:
                proposed = tuple(decider.decide(observation, list(out.tries)))
            except Exception as exc:  # the decider's own failure (invalid after its retries, a timeout, not a sequence)
                raise _Stop(f"decider failed: {type(exc).__name__}: {exc}") from exc
            actions = _check(proposed, config)
            applied: list[Applied] = []
            for action in actions:
                result = _apply(env, action)
                applied.append(result)
                in_force.append(result)
                if not result.ok:
                    out.tries.append(Try(number, actions, tuple(applied), env.now(), "action failed"))
                    raise _Stop(f"action failed: {action.label()}: {result.detail}")
            # After the last action, not before the first: WAIT counts from when the change took
            # effect, as the pilot measured it, so a slow apply cannot shorten the recovery window.
            t_act = env.now()
            verdict, at = _judge(env, t_act, config)
            if verdict == "resolved":
                out.tries.append(Try(number, actions, tuple(applied), t_act, verdict, at))
                out.ended, out.reason = "resolved", f"resolved on try {number}"
                out.kept = [a.action for a in in_force]
                return out
            if any(a.undo is None for a in applied):
                out.tries.append(Try(number, actions, tuple(applied), t_act, "no undo"))
                raise _Stop(f"try {number}: {verdict}, and an action in it has no undo: no retry")
            undone, error = _undo_try(env, in_force, len(applied))
            out.tries.append(Try(number, actions, tuple(applied), t_act, verdict, None, tuple(undone)))
            if error:
                raise _Stop(error)
        raise _Stop(f"out of tries: {config.tries} tried, none resolved")
    except _Stop as stop:
        out.reason = stop.reason
    except Exception as exc:  # the environment raised: the state is unknown, so put back what can be
        out.reason = f"environment failed: {type(exc).__name__}: {exc}"
    _safe_state(env, in_force, out)
    return out


def _safe_state(env: Environment, in_force: list[Applied], out: Outcome) -> None:
    """Undo every action still in force that has an undo, last first; record each; keep what could not be undone.

    Every undo is attempted even after one fails or raises: each change left in
    force is one nobody approved for the end state. None is retried here.
    """
    kept: list[Action] = []
    for a in reversed(in_force):
        if a.undo is None:
            kept.append(a.action)
            continue
        try:
            result = env.apply(a.undo)
        except Exception as exc:
            result = Applied(a.undo, False, f"{type(exc).__name__}: {exc}", None)
        out.safe_undos.append(result)
        if not result.ok:
            kept.append(a.action)
    out.kept = kept[::-1]
