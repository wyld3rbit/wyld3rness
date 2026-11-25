"""The verdict: did users recover, and did it hold? Judged from what users saw, nothing else.

    a stream of (t, ok, failed) completions, one interval a second
                      │
    a good second: over the JUDGE_S seconds ending then, failed ≤ SLO_FAILED_SHARE of the
                   completions (and some completed)
                      │
    verified at t:  every second in the VERIFY_S before t was good
    recovered at t: t lies within WAIT of the action, and every second from t to t + HOLD is good

Why users and not the services' own metrics: a fix can make a service's
numbers look healthy (a cap sheds load, a restart zeroes its counters) while
users still fail. The user stream is the ground truth an agent is judged on,
and it must come from a process the agent cannot write: the environment port
supplies it (`code/src/wyld3rness/loop/env.py`), never the decider.

Moved here unchanged from simprod's checker, where the storm controls proved
these rules fair (research log, entries 011 and 012: 63 of 63), so the loop
keeps or undoes a fix by the same rule the controls were scored on. Parsing a
service's metrics stays with the app that knows its format. Standard library
only: a rule that judges an agent should not depend on what the agent uses.
WAIT and HOLD are not here: they come from an environment's pilot and are
passed in.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
import itertools

SLO_FAILED_SHARE = 0.01  # a second with more than 1% of user requests failing is a bad second
# Each second is judged on the JUDGE_S seconds ending at it (research log, entry 011). At
# R = 200 one second holds about 200 requests, so 1% is 2 failures and a stray pair breaks
# a real recovery (entry 010); 5 s holds about 1,000, so 1% is 10. The cost: a recovery
# shows up to JUDGE_S - 1 s late, which the pilot's WAIT absorbs.
JUDGE_S = 5
VERIFY_S = 5  # at the end of WAIT, the last this many seconds must all be good to call it verified


@dataclass(frozen=True)
class Second:
    """One second of the user stream: when it ended (s from the incident's start) and its completions."""

    t: float
    ok: float
    failed: float

    @property
    def good(self) -> bool:
        """Some requests completed, and at most SLO_FAILED_SHARE of them failed."""
        total = self.ok + self.failed
        return total > 0 and self.failed <= SLO_FAILED_SHARE * total


def seconds_from(scrapes: Sequence[tuple[float, float, float]]) -> list[Second]:
    """Per-interval completions from cumulative (t, ok, failed) scrapes. Pure.

    A counter that went down means the measured service restarted: that
    interval is dropped as a gap (see `resets`), never turned into negative data.
    """
    return [
        Second(t1, ok1 - ok0, f1 - f0)
        for (_, ok0, f0), (t1, ok1, f1) in itertools.pairwise(scrapes)
        if ok1 >= ok0 and f1 >= f0
    ]


def judged(series: Sequence[Second], window_s: float = JUDGE_S) -> list[Second]:
    """Each second with the completions of the `window_s` seconds ending at it, for verdicts. Pure.

    The window is by time, not by count, so a scrape gap shortens it instead of
    reaching further back.
    """
    out = []
    for i, s in enumerate(series):
        window = [x for x in series[: i + 1] if x.t > s.t - window_s]
        out.append(Second(s.t, sum(x.ok for x in window), sum(x.failed for x in window)))
    return out


def resets(scrapes: Sequence[tuple[float, float, float]]) -> int:
    """How many intervals a counter went down in: restarts of the measured service. Pure."""
    return sum(ok1 < ok0 or f1 < f0 for (_, ok0, f0), (_, ok1, f1) in itertools.pairwise(scrapes))


def verified(series: Sequence[Second], at: float, verify_s: float = VERIFY_S) -> bool:
    """Every second in the `verify_s` before `at` was good, and at most one is missing. Pure.

    Nothing measured is not verified: a scraper that stopped cannot pass a fix.
    """
    window = [s for s in series if at - verify_s < s.t <= at]
    return len(window) >= verify_s - 1 and all(s.good for s in window)


def recovered_at(series: Sequence[Second], action_t: float, wait_s: float, hold_s: float) -> float | None:
    """The first time within `wait_s` after the action from which every second for `hold_s` is good. Pure.

    None when there is no such time: not recovered, or recovered only briefly.
    A series that ends before the hold window does is not counted as held.
    """
    for i, s in enumerate(series):
        if s.t <= action_t or not s.good:
            continue
        if s.t > action_t + wait_s:
            return None
        window = [x for x in series[i:] if x.t <= s.t + hold_s]
        if series[-1].t >= s.t + hold_s and all(x.good for x in window):
            return s.t
    return None


def longest_good_stretch(series: Sequence[Second], after_t: float) -> float:
    """The longest run of consecutive good seconds after `after_t`, in seconds. Pure.

    In an incident nobody fixes, this is how long a false recovery can look real:
    the hold window must be longer.
    """
    best = run = 0.0
    prev_t: float | None = None
    for s in series:
        if s.t <= after_t:
            continue
        step = s.t - prev_t if prev_t is not None else 1.0
        run = run + step if s.good else 0.0
        best = max(best, run)
        prev_t = s.t
    return round(best, 1)


def customer_minutes(series: Sequence[Second], from_t: float, rate: float) -> float:
    """Failed user requests after `from_t`, as minutes of all users failing at `rate` a second. Pure."""
    return round(sum(s.failed for s in series if s.t > from_t) / (rate * 60), 3)
