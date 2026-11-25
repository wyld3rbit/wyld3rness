"""The checker: did customers recover, and did it hold? Read from what customers saw, nothing else.

    every second: GET /customers/metrics ──► completions since the last scrape, ok and failed
                      │
    a good second: over the JUDGE_S seconds ending then, failed ≤ SLO_FAILED_SHARE of the
                   completions (and some completed)
                      │
    recovered at t: t lies within WAIT of the action, and every second from t to t + HOLD is good

Why customers and not the services' own metrics: a fix can make a service's
numbers look healthy (a cap sheds load, a restart zeroes its counters) while
customers still fail. The customer stream is the ground truth the agent is
judged on. A run is judged only if that stream was measured whole: no counter
reset, no scrape gap, and customers sent on time (`invalid` in controls.py).
The runaway client's requests are not customers and are left out.

The agent can read the same metrics through the edge, as an operator reads
synthetic monitoring. It cannot change them, and it cannot change this verdict:
the checker runs on the host, in the process that drives the storm (the
controls now, the harness's environment port from step 3), never in the agent's.

WAIT and HOLD come from the pilot (research log, entry 008), written down
before the controls run. Customer minutes lost: failed customer requests,
divided by the requests a minute customers send (R x 60).
"""

from __future__ import annotations

from collections.abc import Callable, Sequence
from dataclasses import dataclass
import http.client
import itertools
import re
import time
import urllib.request

SLO_FAILED_SHARE = 0.01  # a second with more than 1% of customer requests failing is a bad second
# Each second is judged on the JUDGE_S seconds ending at it (research log, entry 011). At
# R = 200 one second holds about 200 requests, so 1% is 2 failures and a stray pair breaks
# a real recovery (entry 010); 5 s holds about 1,000, so 1% is 10. The cost: a recovery
# shows up to JUDGE_S - 1 s late, which the pilot's WAIT absorbs.
JUDGE_S = 5
_SAMPLE = re.compile(r'^customers_requests_total\{outcome="([^"]+)"\}\s+(\S+)$', re.M)
_LATE = re.compile(r"^customers_late_sends_total\s+(\S+)$", re.M)


def customer_counts(metrics_text: str) -> tuple[float, float]:
    """(ok, failed) customer requests so far, from the customers service's /metrics. Pure."""
    ok = failed = 0.0
    for outcome, value in _SAMPLE.findall(metrics_text):
        if outcome == "ok":
            ok += float(value)
        else:
            failed += float(value)
    return ok, failed


def late_sends(metrics_text: str) -> float:
    """Customer requests sent over 100 ms behind schedule so far. Pure. 0 before the first one."""
    found = _LATE.search(metrics_text)
    return float(found.group(1)) if found else 0.0


@dataclass(frozen=True)
class Second:
    """One second of the customer stream: when it ended (s from the storm's start) and its completions."""

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

    A counter that went down means the customers service restarted: that
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
    """How many intervals a counter went down in: restarts of the customers service. Pure."""
    return sum(ok1 < ok0 or f1 < f0 for (_, ok0, f0), (_, ok1, f1) in itertools.pairwise(scrapes))


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

    In a storm nobody fixes, this is how long a false recovery can look real:
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


def late_share(scrapes: Sequence[tuple[float, float, float, float]], from_t: float) -> float:
    """Of the customer requests completed after `from_t`, the share sent late. Pure.

    The open loop holds only while the generator sends on time. If it falls
    behind, it quietly sends less, the load on the system drops, and a storm can
    look as if it ended by itself. The load test bars this (loadtest.LATE_BAR);
    so do the controls.
    """
    after = [s for s in scrapes if s[0] > from_t]
    before = [s for s in scrapes if s[0] <= from_t]
    if not after or not before:
        return 0.0
    (_, ok0, f0, late0), (_, ok1, f1, late1) = before[-1], after[-1]
    done = (ok1 - ok0) + (f1 - f0)
    return round((late1 - late0) / done, 4) if done > 0 else 0.0


def max_gap(scrapes: Sequence[tuple[float, ...]], from_t: float) -> float:
    """The longest time between two successful scrapes after `from_t`, in seconds. Pure.

    Scrapes come once a second. A failed one leaves a longer interval, which
    merges seconds and thins the verification window, so a run with one is not
    judged on it.
    """
    times = [s[0] for s in scrapes if s[0] >= from_t]
    return round(max((b - a for a, b in itertools.pairwise(times)), default=0.0), 2)


def customer_minutes(series: Sequence[Second], from_t: float, rate: float) -> float:
    """Failed customer requests after `from_t`, as minutes of all customers failing. Pure."""
    return round(sum(s.failed for s in series if s.t > from_t) / (rate * 60), 3)


class Scraper:
    """Reads the customers service's /metrics once a second through the edge, from the harness's process."""

    def __init__(
        self,
        url: str = "http://127.0.0.1:8100/customers/metrics",
        clock: Callable[[], float] = time.monotonic,
        fetch: Callable[[str], str] | None = None,
    ) -> None:
        self.url, self.clock = url, clock
        self.fetch = fetch or (lambda u: urllib.request.urlopen(u, timeout=2).read().decode())
        self.start = clock()
        self.scrapes: list[tuple[float, float, float]] = []
        self.late: list[tuple[float, float, float, float]] = []  # (t, ok, failed, late sends)
        self.errors = 0

    def scrape(self) -> None:
        """One read; a failed read is counted and leaves a gap, never a fabricated value."""
        try:
            text = self.fetch(self.url)
            (ok, failed), late = customer_counts(text), late_sends(text)
        except (OSError, http.client.HTTPException, ValueError):  # refused, cut short, or garbled
            self.errors += 1
            return
        t = round(self.clock() - self.start, 2)
        self.scrapes.append((t, ok, failed))
        self.late.append((t, ok, failed, late))

    def series(self) -> list[Second]:
        """Every interval scraped so far."""
        return seconds_from(self.scrapes)
