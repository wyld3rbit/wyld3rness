"""The checker: what customers saw, read from the outside, and whether a run was measured whole.

    every second: GET /customers/metrics ──► cumulative (ok, failed) customer requests
                      │
    the harness's verdict (code/src/wyld3rness/loop/verdict.py): good seconds, verified,
    recovered within WAIT and held for HOLD

Why customers and not the services' own metrics: a fix can make a service's
numbers look healthy (a cap sheds load, a restart zeroes its counters) while
customers still fail. The customer stream is the ground truth the agent is
judged on. A run is judged only if that stream was measured whole: no counter
reset, no scrape gap, and customers sent on time (`invalid` in controls.py).
The runaway client's requests are not customers and are left out.

The agent sees the customers' counters only in the observation the loop sends
it, as an operator reads synthetic monitoring. It cannot change them, and it
cannot change the verdict:
the scraper runs in the host process that drives the storm, and a second one in
FindyFixy's loop container for the loop's own verdict; never in the agent's
container, which has no route to simprod.

The verdict rules lived here first and moved to the harness unchanged
(ADR 0008, step 3), so the loop keeps or undoes a fix by the rule the controls were
scored on. This module keeps what is simprod's: the metrics format, the
scraper, and the measurement checks (late sends, scrape gaps). The services'
image copies this file but never imports it; the host and the loop container do.
"""

from __future__ import annotations

from collections.abc import Callable, Sequence
import http.client
import itertools
import re
import time
import urllib.request

from wyld3rness.loop.verdict import Second, seconds_from

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
