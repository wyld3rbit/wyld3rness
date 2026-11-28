"""Tests for simprod/loadgen.py: open-loop arrivals, latency from the scheduled time, the summary."""

import itertools
import statistics

from simprod import loadgen


def test_arrivals_are_seeded_poisson_at_the_rate() -> None:
    a = loadgen.arrivals(50, 200, seed=3)
    assert a == loadgen.arrivals(50, 200, seed=3) and a != loadgen.arrivals(50, 200, seed=4)
    assert abs(len(a) / 200 - 50) < 2  # 10,000 expected, sd 100: ±400 is 4 sd
    gaps = [b - x for x, b in itertools.pairwise(a)]
    assert abs(statistics.fmean(gaps) - 1 / 50) < 0.001
    assert abs(statistics.stdev(gaps) - 1 / 50) < 0.002  # exponential: sd equals mean


def test_keys_are_seeded_and_spread() -> None:
    ks = loadgen.keys(5000, seed=1)
    assert ks == loadgen.keys(5000, seed=1) and len(set(ks)) > 900


def test_percentile_is_nearest_rank() -> None:
    xs = [float(i) for i in range(1, 101)]
    assert loadgen.percentile(xs, 50) == 50 and loadgen.percentile(xs, 99) == 99
    assert loadgen.percentile([], 99) is None and loadgen.percentile([7.0], 99) == 7


def test_open_loop_sends_on_schedule_and_counts_delay_against_the_system() -> None:
    """A slow answer does not delay the next send; latency starts at the scheduled time."""
    now = [0.0]

    def sleep(s: float) -> None:
        now[0] += s

    seen: list[float] = []

    def send(key: str, timeout_s: float) -> int:
        seen.append(now[0])
        return 200

    out = loadgen.run(send, rate=10, seconds=2, seed=1, workers=1, clock=lambda: now[0], sleep=sleep)
    assert out["sent"] == len(loadgen.arrivals(10, 2, 1)) and out["outcomes"] == {"ok": out["sent"]}
    assert seen == sorted(seen)


def test_outcomes_map_statuses_and_failures_per_second() -> None:
    results = [
        (0, "ok", 10.0, False),
        (0, "timeout", 3000.0, True),
        (1, "throttled", 1.0, False),
        (1, "ok", 30.0, False),
    ]
    s = loadgen.summarise(results, rate=2, seconds=2, seed=1)
    assert s["failed"] == 2 and s["outcomes"] == {"ok": 2, "throttled": 1, "timeout": 1}
    assert s["late_sends"] == 1
    assert s["per_second"] == [{"t": 0, "ok": 1, "failed": 1}, {"t": 1, "ok": 1, "failed": 1}]
    assert s["latency_ms"]["max"] == 30.0  # over ok answers only
    assert [loadgen.outcome_of(c) for c in (200, 429, 503, 504, 500)] == [
        "ok",
        "throttled",
        "shed",
        "timeout",
        "http_500",
    ]


def test_a_request_already_past_the_customer_timeout_is_not_sent(monkeypatch) -> None:
    """When the generator is too late to send in time, the customer has already failed: counted, not sent."""
    monkeypatch.setattr(loadgen, "CUSTOMER_TIMEOUT_S", 0.0)
    sent: list[str] = []

    def send(key: str, timeout_s: float) -> int:
        sent.append(key)
        return 200

    now = [0.0]

    def sleep(s: float) -> None:
        now[0] += s

    out = loadgen.run(send, rate=5, seconds=1, seed=2, workers=1, clock=lambda: now[0], sleep=sleep)
    assert sent == [] and out["outcomes"] == {"timeout": out["sent"]}


def test_the_pool_holds_every_request_a_storm_keeps_in_flight() -> None:
    """At 200/s with a 3 s customer timeout, 600 can be waiting at once; 900 threads leave room for bursts."""
    assert loadgen.pool_size(200) == 900
    assert loadgen.pool_size(1) == 16 and loadgen.pool_size(10_000) == loadgen.MAX_WORKERS


def test_a_send_behind_schedule_is_counted_late(monkeypatch) -> None:
    now = [0.0]
    stalls = iter([0.5])  # the first request's worker starts half a second late

    def clock() -> float:
        return now[0]

    def sleep(s: float) -> None:
        now[0] += s

    def send(key: str, timeout_s: float) -> int:
        return 200

    original = loadgen.ThreadPoolExecutor

    class Stalling(original):  # type: ignore[misc, valid-type]
        def submit(self, fn, *args):
            now[0] += next(stalls, 0.0)
            return super().submit(fn, *args)

    monkeypatch.setattr(loadgen, "ThreadPoolExecutor", Stalling)
    out = loadgen.run(send, rate=5, seconds=1, seed=2, workers=1, clock=clock, sleep=sleep)
    assert out["late_sends"] == 1


def test_an_answer_after_the_customer_timeout_is_a_timeout() -> None:
    """A socket timeout bounds each read, so a 200 can arrive late; the customer had already given up."""
    now = [0.0]

    def sleep(s: float) -> None:
        now[0] += s

    def send(key: str, timeout_s: float) -> int:
        now[0] += loadgen.CUSTOMER_TIMEOUT_S + 1  # answered, but too late
        return 200

    out = loadgen.run(send, rate=2, seconds=1, seed=5, workers=1, clock=lambda: now[0], sleep=sleep)
    assert out["sent"] and out["outcomes"] == {"timeout": out["sent"]}
