"""Tests for simprod/ledger.py: shards, capacity, shedding, the sealed slowdown fault."""

import threading
import time

from simprod.ledger import Ledger, build, shard_of
from simprod.service import Metrics, Request


def ledger(**kw) -> Ledger:
    args = {"shards": 4, "workers": 1, "service_s": 0.0, "queue_limit": 100, "metrics": Metrics()}
    return Ledger(**{**args, **kw})


def entry(key: str = "acct-0001") -> Request:
    return Request({"key": key, "amount_cents": 100, "id": "p1"}, {})


def test_a_key_always_lands_on_the_same_shard() -> None:
    assert shard_of("acct-0001", 4) == shard_of("acct-0001", 4)
    assert {shard_of(f"acct-{i:04d}", 4) for i in range(100)} == {0, 1, 2, 3}


def test_a_write_holds_a_slot_for_the_service_time_plus_any_fault() -> None:
    slept: list[float] = []
    lg = ledger(service_s=0.02, sleep=slept.append, clock=lambda: 0.0)
    assert lg.write(entry()).status == 200
    lg.set_fault(Request({"kind": "slowdown", "extra_ms": 300, "seconds": 10}, {}))
    lg.write(entry())
    assert slept == [0.02, 0.32]


def test_the_fault_expires_by_itself() -> None:
    now = [0.0]
    lg = ledger(clock=lambda: now[0])
    lg.set_fault(Request({"kind": "slowdown", "extra_ms": 100, "seconds": 5}, {}))
    assert lg.fault_state()["active"]
    now[0] = 6.0
    assert lg.extra_s() == 0.0 and not lg.fault_state()["active"]


def test_a_full_waiting_line_sheds_at_once() -> None:
    """Writes beyond the queue limit get 503 without waiting: memory and threads stay bounded."""
    gate = threading.Event()
    lg = ledger(queue_limit=1, sleep=lambda s: gate.wait(2))
    key = "acct-0001"
    holder = threading.Thread(target=lg.write, args=(entry(key),))  # takes the only slot
    waiter = threading.Thread(target=lg.write, args=(entry(key),))  # fills the line
    holder.start()
    time.sleep(0.05)
    waiter.start()
    time.sleep(0.05)
    shed = lg.write(entry(key))
    gate.set()
    holder.join()
    waiter.join()
    shard = str(shard_of(key, 4))
    assert shed.status == 503
    assert lg.metrics.value("ledger_writes_total", {"shard": shard, "outcome": "shed"}) == 1
    assert lg.metrics.value("ledger_writes_total", {"shard": shard, "outcome": "ok"}) == 2


def test_a_bad_entry_is_refused() -> None:
    assert ledger().write(Request({"key": "a"}, {})).status == 400


def test_the_fault_is_on_the_admin_service_only() -> None:
    public, admin = build(ledger(), exit=lambda code: None)
    assert ("POST", "/fault") not in public.routes and ("POST", "/fault") in admin.routes
    assert public.dispatch("POST", "/fault", b'{"kind": "slowdown", "extra_ms": 1, "seconds": 1}', {}).status == 404


def test_queue_depth_is_a_gauge_per_shard() -> None:
    text = ledger().metrics.render()
    assert 'ledger_queue_depth{shard="3"} 0' in text and 'ledger_busy_slots{shard="0"} 0' in text


def test_a_slowdown_must_be_finite_and_not_negative() -> None:
    lg = ledger()
    for bad in (
        {"extra_ms": -50, "seconds": 60},
        {"extra_ms": float("nan"), "seconds": 60},
        {"extra_ms": True, "seconds": 60},
        {"extra_ms": 100, "seconds": 0},
    ):
        assert lg.set_fault(Request({"kind": "slowdown", **bad}, {})).status == 400
    assert not lg.fault_state()["active"]
