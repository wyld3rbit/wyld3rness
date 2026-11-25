"""Customers, all the time: the open-loop load that storms run under, plus a runaway client on demand.

    customers ──► POST gateway/pay at RATE a second, Poisson, seeded   (counted: what customers saw)
    runaway   ──► POST gateway/pay for ONE key at RPS a second         (counted apart: not a customer)

The one-shot generator (loadgen.py) measures a phase and exits. Storms need load
that is already flowing when the fault starts and keeps flowing through the
fix, so this runs as a service. Each customer request follows the same rules
as loadgen.py: sent at its scheduled time whatever earlier ones did, latency and
the 3 s customer timeout counted from that time.

The runaway client is the hot-shard fault. Its rate stays under R (faults.py), so
a pool sized for R customers is enough for it too. One tenant's integration goes wrong
and hammers a single key, so the shard holding it saturates and every customer
on that shard waits. Its requests are counted apart from customers: capping that
key throttles the runaway, not customers, so the fix is not charged as harm.
The rest of that tenant's real traffic still shares the key and its cap.

Fault control (sealed): the runaway is started on ADMIN_PORT, which only the
internal network reaches and the edge never forwards. /metrics is public: it is
the customer-experience signal, what synthetic monitoring shows in production.
It carries no key and no trace of the runaway, so it names neither the key nor
the kind of fault; the runaway's own counter is on the sealed admin /metrics.
"""

from __future__ import annotations

from collections.abc import Callable, Iterator
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
import random
import threading
import time

from simprod.loadgen import KEYS, Send, attempt, pool_size
from simprod.service import (
    JSON,
    Metrics,
    Request,
    Response,
    Route,
    Service,
    Upstream,
    env_addr,
    env_float,
    is_key,
    log,
    number,
    run,
)

NAME = "customers"


def gaps(rate: float, seed: int) -> Iterator[float]:
    """Seconds between arrivals, forever: a seeded Poisson process. Pure."""
    rng = random.Random(seed)
    while True:
        yield rng.expovariate(rate)


@dataclass
class Runaway:
    """The runaway client in force: `rps` requests a second on `key` until `until` (monotonic)."""

    key: str = ""
    rps: float = 0.0
    until: float = 0.0


class Customers:
    """The continuous customer stream and the runaway client. Clock and sleep are injected for tests."""

    def __init__(
        self,
        send: Send,
        rate: float,
        seed: int,
        metrics: Metrics,
        clock: Callable[[], float] = time.monotonic,
        sleep: Callable[[float], None] = time.sleep,
    ) -> None:
        self.send, self.rate, self.seed = send, rate, seed
        self.metrics = metrics
        # The runaway is counted on the sealed admin service only. On the public
        # /metrics its counter would exist only during a hot shard, naming the
        # fault kind to anyone who reads it, the agent included.
        self.admin_metrics = Metrics()
        self.clock, self.sleep = clock, sleep
        self.runaway = Runaway()
        self.lock = threading.Lock()
        self.wake = threading.Event()  # set when a runaway starts, so its loop does not wait out a poll
        metrics.counter("customers_requests_total", "Customer payments, by what the customer saw")
        metrics.counter("customers_late_sends_total", "Customer requests sent over 100 ms behind schedule")
        metrics.histogram("customers_latency_ms", "Customer latency from the scheduled time, ok answers")
        self.admin_metrics.counter("customers_runaway_requests_total", "Runaway-client requests, by outcome")
        metrics.gauge("customers_rate", "Mean customer requests a second", lambda: {(): self.rate})

    def _record(self, result: tuple[str, float, bool]) -> None:
        """Count one customer request."""
        outcome, ms, late = result
        self.metrics.inc("customers_requests_total", {"outcome": outcome})
        if late:
            self.metrics.inc("customers_late_sends_total")
        if outcome == "ok":
            self.metrics.observe("customers_latency_ms", ms)

    def customers(self, stop: threading.Event) -> None:
        """Send customer requests on their Poisson schedule until `stop` is set."""
        rng = random.Random(self.seed + 1)
        start = self.clock()
        due = start
        with ThreadPoolExecutor(max_workers=pool_size(self.rate), thread_name_prefix="customer") as pool:
            for gap in gaps(self.rate, self.seed):
                if stop.is_set():
                    return
                due += gap
                wait = due - self.clock()
                if wait > 0:
                    self.sleep(wait)
                key = f"acct-{rng.randrange(KEYS):04d}"
                pool.submit(self._customer_one, key, due)

    def _customer_one(self, key: str, due: float) -> None:
        """One customer request, counted."""
        self._record(attempt(self.send, key, due, self.clock))

    def runaway_loop(self, stop: threading.Event) -> None:
        """While a runaway is in force, send its requests at a fixed interval; otherwise wait to be woken."""
        with ThreadPoolExecutor(max_workers=pool_size(self.rate), thread_name_prefix="runaway") as pool:
            due = self.clock()
            while not stop.is_set():
                with self.lock:
                    r = Runaway(self.runaway.key, self.runaway.rps, self.runaway.until)
                now = self.clock()
                if r.rps <= 0 or now >= r.until:
                    self.wake.wait(1.0)
                    self.wake.clear()
                    due = self.clock()
                    continue
                due = max(due + 1 / r.rps, now - 1.0)  # never more than 1 s of catch-up after a stall
                wait = due - self.clock()
                if wait > 0:
                    self.sleep(wait)
                pool.submit(self._runaway_one, r.key, due)

    def _runaway_one(self, key: str, due: float) -> None:
        """One runaway request, counted apart from customers."""
        outcome, _, _ = attempt(self.send, key, due, self.clock)
        self.admin_metrics.inc("customers_runaway_requests_total", {"outcome": outcome})

    # ---- Fault control (admin port only) ----

    def start_runaway(self, req: Request) -> Response:
        """POST /fault {kind: runaway, key, rps, seconds}: one key hammered at `rps` for `seconds`."""
        b = req.body
        key, rps, seconds = b.get("key"), number(b.get("rps")), number(b.get("seconds"))
        if b.get("kind") != "runaway" or not is_key(key) or rps is None or rps <= 0 or seconds is None or seconds <= 0:
            return Response(400, {"error": "need kind=runaway, key, rps > 0 and seconds > 0"})
        with self.lock:
            self.runaway = Runaway(key, rps, self.clock() + seconds)
        self.wake.set()
        log(NAME, "fault_set", kind="runaway", key=key, rps=rps, seconds=seconds, actor=req.actor)
        return Response(200, self.fault_state())

    def clear(self, req: Request) -> Response:
        """POST /fault/clear: stop the runaway now."""
        with self.lock:
            self.runaway = Runaway()
        log(NAME, "fault_cleared", actor=req.actor)
        return Response(200, self.fault_state())

    def fault_state(self) -> JSON:
        """The runaway in force, for the inspector."""
        with self.lock:
            r = self.runaway
            left = max(0.0, r.until - self.clock())
        active = r.rps > 0 and left > 0
        return {
            "active": active,
            "kind": "runaway" if active else None,
            "key": r.key if active else None,
            "rps": r.rps if active else 0,
            "seconds_left": round(left, 1),
        }


def build(c: Customers) -> tuple[Service, Service]:
    """The public service (health, metrics, spec) and the sealed admin service."""
    public = Service(NAME, [], metrics=c.metrics)
    admin = Service(
        f"{NAME}-admin",
        [
            Route("GET", "/fault", lambda r: Response(200, c.fault_state()), "The runaway in force"),
            Route(
                "POST",
                "/fault",
                c.start_runaway,
                "Start a runaway client on one key",
                body={"type": "object", "required": ["kind", "key", "rps", "seconds"]},
            ),
            Route("POST", "/fault/clear", c.clear, "Stop the runaway"),
        ],
        metrics=c.admin_metrics,
        public_url="http://customers:9000",
    )
    return public, admin


def main() -> None:
    """Container entry point: serve health and metrics, run customers and the runaway loop forever."""
    gateway = Upstream(*env_addr("GATEWAY_ADDR", "gateway:8000"))

    def send(key: str, timeout_s: float) -> int:
        """POST one payment to the gateway; returns the status."""
        return gateway.call("POST", "/pay", {"key": key, "amount_cents": 1999}, timeout_s)[0]

    c = Customers(send, env_float("CUSTOMER_RATE", 200), int(env_float("CUSTOMER_SEED", 1)), Metrics())
    stop = threading.Event()
    threading.Thread(target=c.customers, args=(stop,), name="customers", daemon=True).start()
    threading.Thread(target=c.runaway_loop, args=(stop,), name="runaway", daemon=True).start()
    log(NAME, "config", rate=c.rate, seed=c.seed)
    public, admin = build(c)
    run((public, 8000), (admin, int(env_float("ADMIN_PORT", 9000))))


if __name__ == "__main__":
    main()
