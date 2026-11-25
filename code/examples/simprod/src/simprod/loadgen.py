"""The load generator: customers paying, at a fixed average rate, whatever the gateway does.

    python -m simprod.loadgen --rate 50 --seconds 300 --seed 1

Open loop. Arrival times are drawn up front (a Poisson process: exponential gaps
with mean 1/rate) and each request is sent at its time, whether or not earlier
ones have answered. A closed-loop generator (N users, each waiting for its answer
before sending again) slows down when the system slows down, so it sends less
load exactly when the system is struggling and reports better latency than
customers would see. Real customers do not wait for each other.

Latency is measured from the scheduled send time, not the actual one. If the
generator's own worker pool is busy and a request leaves late, the delay counts
against the system, as it would for a customer. The customer timeout runs from
the scheduled time too.

Output: a summary JSON on the last line of stdout, prefixed `RESULT `, which
the load-test driver parses. Per second: requests ok and failed, by scheduled
second. Failed customer requests are what step 6 converts into customer minutes.
"""

from __future__ import annotations

import argparse
from collections import Counter
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor
import json
import math
import random
import threading
import time

from simprod.service import JSON, Upstream, env_addr

CUSTOMER_TIMEOUT_S = 3.0
KEYS = 1000  # distinct customer accounts, chosen uniformly
LATE_S = 0.1  # a request sent this long after its scheduled time is a late send
MAX_WORKERS = 4096


def pool_size(rate: float) -> int:
    """Threads enough for every request in flight when each one waits the full customer timeout.

    rate x timeout requests can be in flight at once (Little's law); half again as
    many for bursts, since Poisson arrivals bunch. Too few and the generator sends
    late, which quietly lowers the load on a struggling system: the open loop would
    close. Threads are created only when needed, so a healthy run uses a handful.
    """
    return max(16, min(MAX_WORKERS, math.ceil(rate * CUSTOMER_TIMEOUT_S * 1.5)))


def arrivals(rate: float, seconds: float, seed: int) -> list[float]:
    """Send times in seconds from the start: a seeded Poisson process. Pure."""
    rng = random.Random(seed)
    times: list[float] = []
    t = rng.expovariate(rate)
    while t < seconds:
        times.append(t)
        t += rng.expovariate(rate)
    return times


def keys(count: int, seed: int, accounts: int = KEYS) -> list[str]:
    """The account each request pays from, uniform over `accounts`. Pure."""
    rng = random.Random(seed + 1)
    return [f"acct-{rng.randrange(accounts):04d}" for _ in range(count)]


def percentile(sorted_ms: list[float], q: float) -> float | None:
    """The q-th percentile (0-100) by nearest rank, None for no data."""
    if not sorted_ms:
        return None
    rank = max(1, min(len(sorted_ms), math.ceil(q / 100 * len(sorted_ms))))
    return round(sorted_ms[rank - 1], 1)


def outcome_of(status: int) -> str:
    """The customer's view of an HTTP status."""
    if status == 200:
        return "ok"
    if status == 429:
        return "throttled"
    if status == 503:
        return "shed"
    if status == 504:
        return "timeout"
    return f"http_{status}"


Send = Callable[[str, float], int]  # (key, timeout_s) -> status; raises OSError on failure or timeout


def attempt(send: Send, key: str, due: float, clock: Callable[[], float]) -> tuple[str, float, bool]:
    """One request scheduled at `due`: (outcome, latency ms from `due`, sent late). Never raises.

    The one classification of what a customer saw, shared by the load test and the storms.
    """
    behind = clock() - due
    left = CUSTOMER_TIMEOUT_S - behind
    if left <= 0:
        outcome = "timeout"  # the generator itself was too busy to send it in time
    else:
        try:
            outcome = outcome_of(send(key, left))
        except OSError:
            outcome = "timeout" if clock() - due >= CUSTOMER_TIMEOUT_S - 0.01 else "conn_error"
        # A socket timeout bounds each read, not the whole request, so an answer
        # can arrive after the customer gave up. It is a timeout all the same.
        if clock() - due > CUSTOMER_TIMEOUT_S:
            outcome = "timeout"
    return outcome, (clock() - due) * 1000, behind > LATE_S


def run(
    send: Send,
    rate: float,
    seconds: float,
    seed: int,
    workers: int | None = None,
    clock: Callable[[], float] = time.monotonic,
    sleep: Callable[[float], None] = time.sleep,
) -> JSON:
    """Send every scheduled request and summarise what customers saw."""
    times = arrivals(rate, seconds, seed)
    accounts = keys(len(times), seed)
    results: list[tuple[int, str, float, bool]] = []  # (scheduled second, outcome, latency ms, sent late)
    lock = threading.Lock()
    start = clock()

    def one(at: float, key: str) -> None:
        """One customer's request, classified by `attempt`."""
        outcome, ms, late = attempt(send, key, start + at, clock)
        with lock:
            results.append((int(at), outcome, ms, late))

    with ThreadPoolExecutor(max_workers=workers or pool_size(rate), thread_name_prefix="customer") as pool:
        for at, key in zip(times, accounts, strict=True):
            wait = start + at - clock()
            if wait > 0:
                sleep(wait)
            pool.submit(one, at, key)
    return summarise(results, rate, seconds, seed)


def summarise(results: list[tuple[int, str, float, bool]], rate: float, seconds: float, seed: int) -> JSON:
    """Counts, late sends, latency percentiles over answered-ok requests, the per-second series. Pure.

    `late_sends` checks the measurement itself: a share above about 1% means the
    generator, not the system, was the bottleneck, and the run is not valid.
    """
    outcomes = Counter(o for _, o, _, _ in results)
    ok_ms = sorted(ms for _, o, ms, _ in results if o == "ok")
    per: dict[int, Counter[str]] = {}
    for sec, o, _, _ in results:
        per.setdefault(sec, Counter())["ok" if o == "ok" else "failed"] += 1
    return {
        "rate": rate,
        "seconds": seconds,
        "seed": seed,
        "sent": len(results),
        "outcomes": dict(sorted(outcomes.items())),
        "failed": len(results) - outcomes.get("ok", 0),
        "late_sends": sum(late for *_, late in results),
        "latency_ms": {
            "p50": percentile(ok_ms, 50),
            "p90": percentile(ok_ms, 90),
            "p99": percentile(ok_ms, 99),
            "max": round(ok_ms[-1], 1) if ok_ms else None,
        },
        "per_second": [{"t": s, "ok": c["ok"], "failed": c["failed"]} for s, c in sorted(per.items())],
    }


def main(argv: list[str] | None = None) -> None:
    """Run once against the gateway and print the RESULT line."""
    parser = argparse.ArgumentParser(prog="loadgen", description=__doc__.splitlines()[0])
    parser.add_argument("--rate", type=float, required=True, help="mean requests a second")
    parser.add_argument("--seconds", type=float, required=True)
    parser.add_argument("--seed", type=int, default=1)
    args = parser.parse_args(argv)
    gateway = Upstream(*env_addr("GATEWAY_ADDR", "gateway:8000"))

    def send(key: str, timeout_s: float) -> int:
        """POST one payment to the gateway; returns the status."""
        status, _ = gateway.call("POST", "/pay", {"key": key, "amount_cents": 1999}, timeout_s)
        return status

    summary = run(send, args.rate, args.seconds, args.seed)
    print("RESULT " + json.dumps(summary), flush=True)


if __name__ == "__main__":
    main()
