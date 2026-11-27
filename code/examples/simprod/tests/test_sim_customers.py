"""Tests for simprod/customers.py: the continuous stream, the runaway client and its sealed control."""

import threading
import time

from simprod.customers import Customers, attempt, build, gaps
from simprod.service import Metrics, Request


def test_gaps_are_seeded_exponential() -> None:
    import itertools

    a = list(itertools.islice(gaps(100, 1), 2000))
    assert a == list(itertools.islice(gaps(100, 1), 2000))
    assert abs(sum(a) / len(a) - 0.01) < 0.001


def test_attempt_classifies_like_the_one_shot_generator() -> None:
    assert attempt(lambda k, t: 200, "k", due=0.0, clock=lambda: 0.0)[0] == "ok"
    assert attempt(lambda k, t: 429, "k", due=0.0, clock=lambda: 0.0)[0] == "throttled"
    assert attempt(lambda k, t: 200, "k", due=0.0, clock=lambda: 5.0)[0] == "timeout"  # too late to send


def run_for(c: Customers, seconds: float) -> None:
    stop = threading.Event()
    threads = [
        threading.Thread(target=c.customers, args=(stop,)),
        threading.Thread(target=c.runaway_loop, args=(stop,)),
    ]
    for t in threads:
        t.start()
    time.sleep(seconds)
    stop.set()
    c.wake.set()
    for t in threads:
        t.join(timeout=3)


def test_customers_and_the_runaway_are_counted_apart() -> None:
    sent: list[str] = []
    lock = threading.Lock()

    def send(key: str, timeout_s: float) -> int:
        with lock:
            sent.append(key)
        return 429 if key == "acct-hot" else 200

    c = Customers(send, rate=200, seed=1, metrics=Metrics())
    c.start_runaway(Request({"kind": "runaway", "key": "acct-hot", "rps": 100, "seconds": 10}, {}))
    run_for(c, 0.5)
    m = c.metrics
    assert m.value("customers_requests_total", {"outcome": "ok"}) > 50
    assert c.admin_metrics.value("customers_runaway_requests_total", {"outcome": "throttled"}) > 20
    assert m.value("customers_requests_total", {"outcome": "throttled"}) == 0  # the runaway is not a customer


def test_the_runaway_stops_when_cleared_and_by_itself() -> None:
    now = [0.0]
    c = Customers(lambda k, t: 200, rate=1, seed=1, metrics=Metrics(), clock=lambda: now[0])
    c.start_runaway(Request({"kind": "runaway", "key": "acct-hot", "rps": 10, "seconds": 5}, {}))
    assert c.fault_state()["active"] and c.fault_state()["key"] == "acct-hot"
    now[0] = 6.0
    assert not c.fault_state()["active"]
    c.start_runaway(Request({"kind": "runaway", "key": "acct-hot", "rps": 10, "seconds": 5}, {}))
    c.clear(Request({}, {}))
    assert not c.fault_state()["active"]


def test_a_bad_runaway_is_refused() -> None:
    c = Customers(lambda k, t: 200, rate=1, seed=1, metrics=Metrics())
    for bad in (
        {"key": "a b", "rps": 1, "seconds": 1},
        {"key": "k", "rps": 0, "seconds": 1},
        {"key": "k", "rps": 1, "seconds": True},
    ):
        assert c.start_runaway(Request({"kind": "runaway", **bad}, {})).status == 400


def test_the_runaway_control_is_on_the_admin_service_only() -> None:
    public, admin = build(Customers(lambda k, t: 200, rate=1, seed=1, metrics=Metrics()))
    assert ("POST", "/fault") in admin.routes and ("POST", "/fault") not in public.routes
    public_text = public.metrics.render()
    assert 'key="' not in public_text and "runaway" not in public_text  # names neither the key nor the fault
    assert "customers_runaway_requests_total" in admin.metrics.render()
