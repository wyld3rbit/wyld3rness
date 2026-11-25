"""Tests for simprod/gateway.py: retries as shipped, backoff with a budget, rate caps, the actions."""

import random

from simprod.gateway import Gateway, Policy, RetryBudget, TokenBucket, build
from simprod.service import Metrics, Request


class FakeLedger:
    """Answers from a script: a status, or 'timeout' to raise OSError. Records every call."""

    def __init__(self, script: list) -> None:
        self.script = list(script)
        self.calls = 0

    def __call__(self, method, path, body, timeout_s):
        self.calls += 1
        step = self.script.pop(0) if self.script else 200
        if step == "timeout":
            raise OSError("timed out")
        return step, {"shard": 1}


def notify_ok(method, path, body, timeout_s):
    return 200, {}


def gateway(script: list, backoff: bool = False, retries: int = 3, now=None) -> tuple[Gateway, FakeLedger, list]:
    fake = FakeLedger(script)
    slept: list[float] = []
    clock = now or (lambda: 0.0)
    gw = Gateway(
        fake, notify_ok, Policy(0.5, retries, backoff), Metrics(), clock=clock, sleep=slept.append, rng=random.Random(1)
    )
    return gw, fake, slept


def pay(gw: Gateway, key: str = "acct-0001"):
    return gw.pay(Request({"key": key, "amount_cents": 100}, {}))


def test_as_shipped_a_timeout_is_retried_at_once() -> None:
    """The misconfiguration: no wait between tries, so one request becomes up to four writes."""
    gw, fake, slept = gateway(["timeout", "timeout", "timeout", "timeout"])
    resp = pay(gw)
    assert resp.status == 504 and fake.calls == 4 and slept == []
    assert gw.metrics.value("gateway_retries_total") == 3


def test_a_retry_that_succeeds_returns_the_attempt_count() -> None:
    gw, fake, _ = gateway(["timeout", 200])
    resp = pay(gw)
    assert resp.status == 200 and resp.body["attempts"] == 2 and fake.calls == 2


def test_a_shed_write_ends_in_503_and_a_client_error_is_not_retried() -> None:
    gw, _, _ = gateway([503, 503, 503, 503])
    assert pay(gw).status == 503
    gw, fake, _ = gateway([400])
    assert pay(gw).status == 400 and fake.calls == 1


def test_backoff_waits_with_jitter_and_the_budget_caps_retries() -> None:
    """With backoff on, sustained failure costs at most the budget: about 10% extra writes."""
    gw, fake, slept = gateway(["timeout"] * 10_000, backoff=True)
    for _ in range(200):
        pay(gw)
    retries = gw.metrics.value("gateway_retries_total")
    assert slept and all(0 <= s <= 1.0 for s in slept)
    assert retries <= 10 + 0.1 * 200 + 1  # the saved-up burst plus what 200 requests earned
    assert gw.metrics.value("gateway_retries_denied_total") > 0
    assert fake.calls == 200 + retries


def test_the_backoff_action_and_its_undo() -> None:
    gw, _, _ = gateway([])
    assert gw.set_backoff(Request({"enabled": True}, {})).body == {"backoff": True, "was": False}
    assert gw.policy.backoff
    assert gw.set_backoff(Request({"enabled": False}, {})).body == {"backoff": False, "was": True}
    assert gw.set_backoff(Request({"enabled": "yes"}, {})).status == 400


def test_a_rate_cap_throttles_one_key_and_its_undo_removes_it() -> None:
    now = [0.0]
    gw, fake, _ = gateway([], now=lambda: now[0])
    gw.set_rate_cap(Request({"key": "acct-hot", "rps": 2}, {}))
    statuses = [pay(gw, "acct-hot").status for _ in range(5)]
    assert statuses.count(200) == 2 and statuses.count(429) == 3  # a burst of 2, then capped
    assert pay(gw, "acct-cold").status == 200  # other keys untouched
    assert fake.calls == 3  # capped requests never reach the ledger
    now[0] = 1.0
    assert pay(gw, "acct-hot").status == 200  # refilled at 2 a second
    undo = gw.set_rate_cap(Request({"key": "acct-hot", "rps": None}, {}))
    assert undo.body == {"key": "acct-hot", "rps": None, "was": 2.0}
    assert all(pay(gw, "acct-hot").status == 200 for _ in range(5))


def test_a_bad_rate_cap_is_refused() -> None:
    gw, _, _ = gateway([])
    assert gw.set_rate_cap(Request({"key": "k", "rps": 0}, {})).status == 400
    assert gw.set_rate_cap(Request({"rps": 1}, {})).status == 400


def test_token_bucket_and_retry_budget() -> None:
    now = [0.0]
    b = TokenBucket(1.0, 1.0, lambda: now[0])
    assert b.take() and not b.take()
    now[0] = 1.0
    assert b.take()
    budget = RetryBudget(ratio=0.5, cap=1.0)
    assert budget.spend() and not budget.spend()
    budget.earn()
    budget.earn()
    assert budget.spend()


def test_the_actions_are_routes_marked_as_actions() -> None:
    s = build(gateway([])[0], exit=lambda code: None)
    actions = {path for (method, path), r in s.routes.items() if r.action}
    assert actions == {"/actions/backoff", "/actions/rate-cap", "/actions/restart"}


def test_booleans_infinity_and_odd_keys_are_refused() -> None:
    gw, fake, _ = gateway([])
    assert gw.set_rate_cap(Request({"key": "k", "rps": True}, {})).status == 400
    assert gw.set_rate_cap(Request({"key": "k", "rps": float("inf")}, {})).status == 400
    assert gw.set_rate_cap(Request({"key": 'a"b', "rps": 1}, {})).status == 400
    assert gw.pay(Request({"key": "k", "amount_cents": True}, {})).status == 400
    assert fake.calls == 0


def test_top_keys_show_the_noisy_tenant_and_forget_old_traffic() -> None:
    now = [100.0]
    gw, _, _ = gateway([], now=lambda: now[0])
    for _ in range(50):
        pay(gw, "acct-noisy")
    for i in range(10):
        pay(gw, f"acct-{i:04d}")
    assert gw.top.top() == []  # the second in progress is not counted yet
    now[0] = 101.0
    top = gw.top.top()
    assert top[0] == ("acct-noisy", 5.0)  # 50 requests over a 10 s window
    assert 'gateway_top_keys_rps{key="acct-noisy"} 5' in gw.metrics.render()
    now[0] = 111.0
    assert gw.top.top() == []  # the window moved past them


def test_a_capped_key_still_shows_in_top_keys() -> None:
    now = [0.0]
    gw, _, _ = gateway([], now=lambda: now[0])
    gw.set_rate_cap(Request({"key": "acct-hot", "rps": 1}, {}))
    for _ in range(20):
        pay(gw, "acct-hot")
    now[0] = 1.0
    assert gw.top.top()[0][0] == "acct-hot"
