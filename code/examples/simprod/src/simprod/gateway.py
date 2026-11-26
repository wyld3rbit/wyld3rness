"""The gateway: takes a customer's payment, writes it to the ledger, notifies.

    customer ──► POST /pay {key, amount_cents}
                   │ 1. per-key rate cap (off until an action sets one) ──► 429
                   │ 2. ledger write, timeout TIMEOUT_MS, up to RETRIES retries
                   │       immediate (as shipped) or backoff with jitter + retry budget
                   │ 3. notification, one try, best effort
                   ▼
                 200 {id, shard, attempts} | 503 shed | 504 timed out

As shipped (GATEWAY_BACKOFF=0), retries fire at once: the misconfiguration
behind the retry storm.
A slow ledger makes a write time out, the gateway sends it again at once, and
each customer request becomes up to 1 + RETRIES ledger writes exactly when the
ledger has the least room. The ledger also finishes every abandoned write
(ledger.py), so the overload outlives the slowdown that started it.

Two typed actions, each with an undo:

| Action | Request | Undo |
|---|---|---|
| Backoff | POST /actions/backoff {enabled: true} | {enabled: false} |
| Rate cap | POST /actions/rate-cap {key, rps} | {key, rps: null} |

Backoff waits a random time up to base x 2^attempt between tries ("full jitter",
which spreads retries out instead of synchronising them) and spends from a retry
budget: each request earns BUDGET_RATIO of a retry, so retries can add at most
about 10% to the ledger's load however slow it gets. A rate cap is a token bucket
per key: requests over it get 429 at once and never reach the ledger.
"""

from __future__ import annotations

from collections import Counter
from collections.abc import Callable, Mapping
from dataclasses import dataclass
import random
import threading
import time
import uuid

from simprod.credentials import Guard, guard_from_env
from simprod.service import (
    JSON,
    Caller,
    Labels,
    Metrics,
    Request,
    Response,
    Route,
    Service,
    Upstream,
    count,
    env_addr,
    env_flag,
    env_float,
    is_key,
    log,
    number,
    restart_route,
    run,
)

NAME = "gateway"
BACKOFF_BASE_S = 0.05
BACKOFF_CAP_S = 1.0
BUDGET_RATIO = 0.1  # retries earned per request
BUDGET_MAX = 10.0  # retries that can be saved up for a burst
NOTIFY_TIMEOUT_S = 0.5
TOP_WINDOW_S = 10  # the top-keys gauge covers the last this many seconds
TOP_N = 5


class TopKeys:
    """Requests per key over the last TOP_WINDOW_S whole seconds, in one-second buckets. Thread-safe.

    The second in progress is left out, since it is still filling and would read
    low. For the first TOP_WINDOW_S seconds after a restart it also reads low.

    The signal an operator uses to find a noisy tenant: without it, the right key
    to cap in a hot shard could only be guessed. Memory grows with the distinct
    keys seen in the window: bounded here, where customers use KEYS keys and the
    runaway one, but a client sending a fresh key on every request would grow it
    with traffic, and each scrape sums it under the lock `pay` takes. A
    production gateway keeps a fixed-size summary instead (a count-min sketch or
    space-saving counters); simprod does not need one at its rates.
    """

    def __init__(self, clock: Callable[[], float], window_s: int = TOP_WINDOW_S) -> None:
        self.clock, self.window_s = clock, window_s
        self.buckets: dict[int, Counter[str]] = {}
        self.lock = threading.Lock()

    def record(self, key: str) -> None:
        """Count one request for `key` in the current second."""
        now = int(self.clock())
        with self.lock:
            self.buckets.setdefault(now, Counter())[key] += 1
            for old in [s for s in self.buckets if s < now - self.window_s]:
                del self.buckets[old]

    def top(self, n: int = TOP_N) -> list[tuple[str, float]]:
        """The `n` busiest keys and their requests a second over the window."""
        now = int(self.clock())
        total: Counter[str] = Counter()
        with self.lock:
            for second, counts in self.buckets.items():
                if now - self.window_s <= second < now:  # whole seconds only: the current one is still filling
                    total.update(counts)
        return [(k, round(c / self.window_s, 2)) for k, c in total.most_common(n)]


class TokenBucket:
    """`rate` tokens a second, at most `burst` saved. Thread-safe."""

    def __init__(self, rate: float, burst: float, clock: Callable[[], float]) -> None:
        self.rate, self.burst, self.clock = rate, burst, clock
        self.tokens = burst
        self.at = clock()
        self.lock = threading.Lock()

    def take(self) -> bool:
        """Spend one token if there is one."""
        with self.lock:
            now = self.clock()
            self.tokens = min(self.burst, self.tokens + (now - self.at) * self.rate)
            self.at = now
            if self.tokens >= 1:
                self.tokens -= 1
                return True
            return False


class RetryBudget:
    """Each request earns `ratio` of a retry; a retry spends one. Thread-safe."""

    def __init__(self, ratio: float = BUDGET_RATIO, cap: float = BUDGET_MAX) -> None:
        self.ratio, self.cap = ratio, cap
        self.tokens = cap
        self.lock = threading.Lock()

    def earn(self) -> None:
        """Called once per customer request."""
        with self.lock:
            self.tokens = min(self.cap, self.tokens + self.ratio)

    def spend(self) -> bool:
        """Take one retry if the budget allows it."""
        with self.lock:
            if self.tokens >= 1:
                self.tokens -= 1
                return True
            return False


@dataclass
class Policy:
    """The settings the actions change."""

    timeout_s: float
    retries: int
    backoff: bool = False


class Gateway:
    """The gateway's state and handlers. Callers, clock, sleep and randomness are injected for tests."""

    def __init__(
        self,
        ledger: Caller,
        notify: Caller,
        policy: Policy,
        metrics: Metrics,
        clock: Callable[[], float] = time.monotonic,
        sleep: Callable[[float], None] = time.sleep,
        rng: random.Random | None = None,
    ) -> None:
        self.ledger, self.notify = ledger, notify
        self.policy = policy
        self.metrics = metrics
        self.clock, self.sleep = clock, sleep
        self.rng = rng or random.Random()
        self.budget = RetryBudget()
        self.caps: dict[str, TokenBucket] = {}
        self.top = TopKeys(clock)
        self.lock = threading.Lock()
        metrics.counter("gateway_payments_total", "Customer payments, by outcome (ok, throttled, shed, timeout, bad)")
        metrics.counter("gateway_ledger_attempts_total", "Ledger writes tried, by outcome (ok, shed, timeout, error)")
        metrics.counter("gateway_retries_total", "Ledger writes that were retries")
        metrics.counter("gateway_retries_denied_total", "Retries the budget refused (backoff on)")
        metrics.counter("gateway_notify_total", "Notifications, by outcome (ok, failed)")
        metrics.histogram("gateway_payment_ms", "Time to answer a payment, by outcome")
        metrics.gauge("gateway_backoff_enabled", "1 when retries back off with jitter and a budget", self._backoff)
        metrics.gauge("gateway_rate_caps", "Requests a second allowed per capped key", self._caps)
        metrics.gauge(
            "gateway_top_keys_rps", f"The {TOP_N} busiest keys, requests a second over {TOP_WINDOW_S} s", self._top
        )

    def _backoff(self) -> Mapping[Labels, float]:
        """1 when backoff is on, read at scrape time."""
        return {(): 1.0 if self.policy.backoff else 0.0}

    def _caps(self) -> Mapping[Labels, float]:
        """The rate allowed per capped key, read at scrape time."""
        with self.lock:
            return {(("key", k),): b.rate for k, b in self.caps.items()}

    def _top(self) -> Mapping[Labels, float]:
        """The busiest keys, read at scrape time."""
        return {(("key", k),): rps for k, rps in self.top.top()}

    def pay(self, req: Request) -> Response:
        """POST /pay: cap, write with retries, notify."""
        started = self.clock()
        key, amount = req.body.get("key"), req.body.get("amount_cents")
        cents = count(amount)
        if not is_key(key) or cents is None or cents <= 0:
            return self._done(started, "bad", Response(400, {"error": "need key and a positive amount_cents"}))
        self.top.record(key)  # counted before the cap: a capped key still shows how hard it pushes
        with self.lock:
            cap = self.caps.get(key)
        if cap is not None and not cap.take():
            return self._done(started, "throttled", Response(429, {"error": "rate capped", "key": key}))
        payment_id = uuid.uuid4().hex
        body: JSON = {"key": key, "amount_cents": amount, "id": payment_id}
        with self.lock:
            policy = Policy(self.policy.timeout_s, self.policy.retries, self.policy.backoff)
        self.budget.earn()
        last = "timeout"
        for attempt in range(1 + policy.retries):
            if attempt:
                if policy.backoff:
                    if not self.budget.spend():
                        self.metrics.inc("gateway_retries_denied_total")
                        break
                    self.sleep(self.rng.uniform(0, min(BACKOFF_CAP_S, BACKOFF_BASE_S * 2**attempt)))
                self.metrics.inc("gateway_retries_total")
            try:
                status, answer = self.ledger("POST", "/entries", body, policy.timeout_s)
            except OSError:
                last = "timeout"
                self.metrics.inc("gateway_ledger_attempts_total", {"outcome": "timeout"})
                continue
            if status == 200:
                self.metrics.inc("gateway_ledger_attempts_total", {"outcome": "ok"})
                self._notify(key, payment_id)
                return self._done(
                    started,
                    "ok",
                    Response(200, {"id": payment_id, "shard": answer.get("shard"), "attempts": attempt + 1}),
                )
            if 400 <= status < 500:
                self.metrics.inc("gateway_ledger_attempts_total", {"outcome": "error"})
                return self._done(started, "bad", Response(status, answer))
            last = "shed" if status == 503 else "error"
            self.metrics.inc("gateway_ledger_attempts_total", {"outcome": last})
        if last == "shed":
            return self._done(started, "shed", Response(503, {"error": "ledger overloaded"}))
        return self._done(started, "timeout", Response(504, {"error": "ledger did not answer in time"}))

    def _notify(self, key: str, payment_id: str) -> None:
        """One try, best effort: a failure is counted and the payment still succeeds."""
        try:
            status, _ = self.notify("POST", "/notify", {"key": key, "id": payment_id}, NOTIFY_TIMEOUT_S)
            ok = status == 200
        except OSError:
            ok = False
        self.metrics.inc("gateway_notify_total", {"outcome": "ok" if ok else "failed"})

    def _done(self, started: float, outcome: str, resp: Response) -> Response:
        """Count the payment's outcome and time, and pass the response through."""
        self.metrics.inc("gateway_payments_total", {"outcome": outcome})
        self.metrics.observe("gateway_payment_ms", (self.clock() - started) * 1000, {"outcome": outcome})
        return resp

    # ---- Actions ----

    def set_backoff(self, req: Request) -> Response:
        """POST /actions/backoff {enabled}: turn backoff with jitter and a retry budget on or off."""
        enabled = req.body.get("enabled")
        if not isinstance(enabled, bool):
            return Response(400, {"error": "need enabled (boolean)"})
        with self.lock:
            before = self.policy.backoff
            self.policy.backoff = enabled
        return Response(200, {"backoff": enabled, "was": before})

    def set_rate_cap(self, req: Request) -> Response:
        """POST /actions/rate-cap {key, rps}: cap one key's requests a second; rps null removes the cap."""
        key, raw = req.body.get("key"), req.body.get("rps")
        rps = number(raw)
        if not is_key(key) or not (raw is None or (rps is not None and rps > 0)):
            return Response(400, {"error": "need key (1-64 of A-Z a-z 0-9 _ . -) and rps (positive number, or null)"})
        with self.lock:
            before = self.caps.get(key)
            if rps is None:
                self.caps.pop(key, None)
            else:
                self.caps[key] = TokenBucket(rps, max(1.0, rps), self.clock)
        return Response(200, {"key": key, "rps": rps, "was": before.rate if before else None})


def build(gw: Gateway, exit: Callable[[int], None] | None = None, guard: Guard | None = None) -> Service:
    """The public service; `guard` checks each action's credential."""
    restart = restart_route(NAME) if exit is None else restart_route(NAME, exit=exit)
    return Service(
        NAME,
        [
            Route(
                "POST",
                "/pay",
                gw.pay,
                "Take one payment",
                body={
                    "type": "object",
                    "required": ["key", "amount_cents"],
                    "properties": {"key": {"type": "string"}, "amount_cents": {"type": "integer", "minimum": 1}},
                },
                responses={
                    200: "Paid",
                    400: "Bad request",
                    429: "Rate capped",
                    503: "Ledger overloaded",
                    504: "Ledger timed out",
                },
            ),
            Route(
                "POST",
                "/actions/backoff",
                gw.set_backoff,
                "Turn retry backoff on or off (undo: the opposite)",
                body={"type": "object", "required": ["enabled"], "properties": {"enabled": {"type": "boolean"}}},
                responses={200: "Set", 400: "Bad request"},
                action=True,
            ),
            Route(
                "POST",
                "/actions/rate-cap",
                gw.set_rate_cap,
                "Cap one key's rate (undo: rps null)",
                body={
                    "type": "object",
                    "required": ["key", "rps"],
                    "properties": {"key": {"type": "string"}, "rps": {"type": ["number", "null"]}},
                },
                responses={200: "Set", 400: "Bad request"},
                action=True,
            ),
            restart,
        ],
        metrics=gw.metrics,
        guard=guard,
    )


def main() -> None:
    """Container entry point."""
    ledger = Upstream(*env_addr("LEDGER_ADDR", "ledger:8000"))
    notify = Upstream(*env_addr("NOTIFY_ADDR", "notifications:8000"))
    # GATEWAY_BACKOFF is how the gateway is deployed: off as shipped (the
    # retry-storm misconfiguration), on for a well-configured deployment (the
    # hot-shard storms start from it, src/simprod/faults.py). The backoff action
    # changes it at run time; a restart returns to the deployed value.
    policy = Policy(
        timeout_s=env_float("GATEWAY_TIMEOUT_MS", 500) / 1000,
        retries=int(env_float("GATEWAY_RETRIES", 3)),
        backoff=env_flag("GATEWAY_BACKOFF"),
    )
    gw = Gateway(ledger.call, notify.call, policy, Metrics())
    log(NAME, "config", timeout_ms=policy.timeout_s * 1000, retries=policy.retries, backoff=policy.backoff)
    guard = guard_from_env()
    log(NAME, "guard", on=guard is not None)
    run((build(gw, guard=guard), 8000))


if __name__ == "__main__":
    main()
