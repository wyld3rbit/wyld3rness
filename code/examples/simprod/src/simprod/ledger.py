"""The ledger: writes payment entries into shards with a fixed capacity each.

    gateway ──► POST /entries {key, amount_cents, id}
                  │ shard = crc32(key) % SHARDS
                  ▼
             shard k: WORKERS slots, each write holds one for SERVICE_MS
             waiting line capped at QUEUE_LIMIT, beyond it: 503 at once

Capacity is a parameter, not an accident of CPU speed: a write holds its slot
for a fixed service time (a sleep, standing in for a database commit), so one
shard serves WORKERS / SERVICE_MS writes a second whatever the machine. That
makes the faults reproducible: the same load gives the same queueing on any
laptop, and the load test (entry 006) checks the VM can actually deliver it.

Why a shard can stay broken after its cause ends: the ledger does not know a
caller gave up. A write that waited past the gateway's timeout is still done in
full, for nobody. If retries keep arrivals above capacity, the line never
drains, every write waits past the timeout, and goodput stays near zero. That is
the retry storm. Shedding at QUEUE_LIMIT bounds memory and threads, not the storm.

Fault control (sealed): a second listener on ADMIN_PORT, reachable only on the
internal network, adds latency for a while. The edge proxy never forwards it.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping
from dataclasses import dataclass
import threading
import time
import zlib

from simprod.service import (
    JSON,
    Labels,
    Metrics,
    Request,
    Response,
    Route,
    Service,
    count,
    env_float,
    is_key,
    log,
    number,
    restart_route,
    run,
)

NAME = "ledger"


def shard_of(key: str, shards: int) -> int:
    """The shard a key lives on. crc32, not hash(): Python salts hash() per process."""
    return zlib.crc32(key.encode()) % shards


@dataclass
class Slowdown:
    """An injected fault: every write takes `extra_s` longer until `until` (monotonic seconds)."""

    extra_s: float = 0.0
    until: float = 0.0


class Shard:
    """WORKERS slots and a capped waiting line."""

    def __init__(self, workers: int, queue_limit: int) -> None:
        self.slots = threading.BoundedSemaphore(workers)
        self.queue_limit = queue_limit
        self.waiting = 0
        self.busy = 0
        self.lock = threading.Lock()


class Ledger:
    """The ledger's state and its handlers. Clock and sleep are injected for tests."""

    def __init__(
        self,
        shards: int,
        workers: int,
        service_s: float,
        queue_limit: int,
        metrics: Metrics,
        sleep: Callable[[float], None] = time.sleep,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self.shards = [Shard(workers, queue_limit) for _ in range(shards)]
        self.service_s = service_s
        self.metrics = metrics
        self.sleep, self.clock = sleep, clock
        self.fault = Slowdown()
        self._fault_lock = threading.Lock()
        metrics.counter("ledger_writes_total", "Write requests, by shard and outcome (ok, shed)")
        metrics.histogram("ledger_write_ms", "Time from arrival to a finished write, by shard")
        metrics.gauge("ledger_queue_depth", "Writes waiting for a slot, by shard", self._depths)
        metrics.gauge("ledger_busy_slots", "Slots holding a write, by shard", self._busy)

    def _depths(self) -> Mapping[Labels, float]:
        """Writes waiting per shard, read at scrape time."""
        return {(("shard", str(i)),): float(s.waiting) for i, s in enumerate(self.shards)}

    def _busy(self) -> Mapping[Labels, float]:
        """Slots in use per shard, read at scrape time."""
        return {(("shard", str(i)),): float(s.busy) for i, s in enumerate(self.shards)}

    def extra_s(self) -> float:
        """The injected delay in force now, 0 when no fault is active."""
        with self._fault_lock:
            return self.fault.extra_s if self.clock() < self.fault.until else 0.0

    def write(self, req: Request) -> Response:
        """POST /entries: wait for a slot on the key's shard, hold it for the service time."""
        key, amount = req.body.get("key"), req.body.get("amount_cents")
        if not is_key(key) or count(amount) is None:
            return Response(400, {"error": "need key (1-64 of A-Z a-z 0-9 _ . -) and amount_cents (integer)"})
        index = shard_of(key, len(self.shards))
        shard = self.shards[index]
        labels = {"shard": str(index)}
        arrived = self.clock()
        with shard.lock:
            if shard.waiting >= shard.queue_limit:
                self.metrics.inc("ledger_writes_total", {**labels, "outcome": "shed"})
                return Response(503, {"error": "shard overloaded", "shard": index})
            shard.waiting += 1
        shard.slots.acquire()
        with shard.lock:
            shard.waiting -= 1
            shard.busy += 1
        try:
            self.sleep(self.service_s + self.extra_s())
        finally:
            with shard.lock:
                shard.busy -= 1
            shard.slots.release()
        self.metrics.inc("ledger_writes_total", {**labels, "outcome": "ok"})
        self.metrics.observe("ledger_write_ms", (self.clock() - arrived) * 1000, labels)
        return Response(200, {"shard": index, "id": req.body.get("id")})

    # ---- Fault control (admin port only) ----

    def set_fault(self, req: Request) -> Response:
        """POST /fault {kind: slowdown, extra_ms, seconds}: slow every write for a while."""
        b = req.body
        extra_ms, seconds = number(b.get("extra_ms")), number(b.get("seconds"))
        if b.get("kind") != "slowdown" or extra_ms is None or extra_ms < 0 or seconds is None or seconds <= 0:
            return Response(400, {"error": "need kind=slowdown, extra_ms >= 0 and seconds > 0"})
        with self._fault_lock:
            self.fault = Slowdown(extra_ms / 1000, self.clock() + seconds)
        log(NAME, "fault_set", kind="slowdown", extra_ms=b["extra_ms"], seconds=b["seconds"], actor=req.actor)
        return Response(200, self.fault_state())

    def clear_fault(self, req: Request) -> Response:
        """POST /fault/clear: end any injected fault now."""
        with self._fault_lock:
            self.fault = Slowdown()
        log(NAME, "fault_cleared", actor=req.actor)
        return Response(200, self.fault_state())

    def fault_state(self) -> JSON:
        """The fault in force, for the inspector."""
        extra = self.extra_s()
        with self._fault_lock:
            left = max(0.0, self.fault.until - self.clock())
        return {
            "active": extra > 0,
            "kind": "slowdown" if extra > 0 else None,
            "extra_ms": round(extra * 1000),
            "seconds_left": round(left, 1),
        }


def build(ledger: Ledger, exit: Callable[[int], None] | None = None) -> tuple[Service, Service]:
    """The public service and the sealed admin service."""
    restart = restart_route(NAME) if exit is None else restart_route(NAME, exit=exit)
    public = Service(
        NAME,
        [
            Route(
                "POST",
                "/entries",
                ledger.write,
                "Write one payment entry",
                body={
                    "type": "object",
                    "required": ["key", "amount_cents"],
                    "properties": {
                        "key": {"type": "string"},
                        "amount_cents": {"type": "integer"},
                        "id": {"type": "string"},
                    },
                },
                responses={200: "Written", 400: "Bad request", 503: "Shard overloaded"},
            ),
            restart,
        ],
        metrics=ledger.metrics,
    )
    admin = Service(
        f"{NAME}-admin",
        [
            Route("GET", "/fault", lambda r: Response(200, ledger.fault_state()), "The fault in force"),
            Route(
                "POST",
                "/fault",
                ledger.set_fault,
                "Inject a slowdown",
                body={"type": "object", "required": ["kind", "extra_ms", "seconds"]},
            ),
            Route("POST", "/fault/clear", ledger.clear_fault, "End the fault"),
        ],
        public_url="http://ledger:9000",
    )
    return public, admin


def main() -> None:
    """Container entry point. Settings come from the environment (docker-compose.yml)."""
    metrics = Metrics()
    ledger = Ledger(
        shards=int(env_float("LEDGER_SHARDS", 4)),
        workers=int(env_float("LEDGER_WORKERS", 1)),
        service_s=env_float("LEDGER_SERVICE_MS", 20) / 1000,
        queue_limit=int(env_float("LEDGER_QUEUE_LIMIT", 100)),
        metrics=metrics,
    )
    log(
        NAME,
        "config",
        shards=len(ledger.shards),
        service_ms=ledger.service_s * 1000,
        workers=int(env_float("LEDGER_WORKERS", 1)),
        queue_limit=int(env_float("LEDGER_QUEUE_LIMIT", 100)),
    )
    public, admin = build(ledger)
    run((public, 8000), (admin, int(env_float("ADMIN_PORT", 9000))))


if __name__ == "__main__":
    main()
