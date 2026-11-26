"""Tests for findyfixy/oncall: the environment over simprod, the model decider, and both inside the harness's loop."""

import json

import pytest

from findyfixy.oncall.cli import budget_refusal, last_action_at, line, outcome_json
from findyfixy.oncall.decide import Decision, DecisionError, ModelDecider, build_prompt, format_history
from findyfixy.oncall.env import SimprodEnv, observation, to_request, undo_for
from findyfixy.oncall.policy import cap_refusal, customer_rate, rules, top_keys
from wyld3rness.loop.budget import Budget, BudgetExceeded
from wyld3rness.loop.env import Action
from wyld3rness.loop.policy import Policy
from wyld3rness.loop.run import LoopConfig, Try, run_incident
from wyld3rness.testing import FakeDecider, FakeSigner, FakeWorld, allow
from wyld3rness.trace.prices import ModelPrice

PRICES = [ModelPrice("m", 0.00000005, 0.0000004, "test")]


def budget(limit: float = 0.005) -> Budget:
    """A budget for model `m` at the small model's reference price."""
    return Budget(limit, PRICES)


GATEWAY = """# HELP gateway_backoff_enabled 1 when retries back off
gateway_backoff_enabled 0
gateway_top_keys_rps{key="acct-0042"} 130
process_cpu_seconds_total 12
"""
BEFORE = {"backoff": False, "caps": {"acct-0007": 5.0}}


def decision(*actions: dict, reason: str = "the ledger is retrying") -> str:
    """A model reply as JSON."""
    return json.dumps({"reason": reason, "actions": list(actions)})


# ---- The environment ---------------------------------------------------------------------


def test_the_observation_keeps_the_operator_metrics_and_drops_the_rest() -> None:
    text = observation({"gateway": GATEWAY, "ledger": ""})
    assert 'gateway_top_keys_rps{key="acct-0042"} 130' in text and "gateway_backoff_enabled 0" in text
    assert "process_cpu" not in text and "# HELP" not in text
    assert "[ledger]\n(no samples)" in text and "[customers]\n(no samples)" in text


@pytest.mark.parametrize(
    ("action", "path"),
    [
        (Action("backoff", {"enabled": True}), "/gateway/actions/backoff"),
        (Action("rate_cap", {"key": "acct-0042", "rps": 10.0}), "/gateway/actions/rate-cap"),
        (Action("rate_cap", {"key": "acct-0042", "rps": None}), "/gateway/actions/rate-cap"),
        (Action("restart", {"service": "ledger"}), "/ledger/actions/restart"),
    ],
)
def test_each_action_maps_to_its_edge_request(action: Action, path: str) -> None:
    assert to_request(action)[0] == path


@pytest.mark.parametrize(
    "action",
    [
        Action("backoff", {"enabled": "yes"}),
        Action("backoff", {"enabled": True, "key": "acct-0042"}),
        Action("rate_cap", {"key": "acct-42", "rps": 10}),
        Action("rate_cap", {"key": "acct-0042\n", "rps": 10}),
        Action("rate_cap", {"key": "acct-0042", "rps": True}),
        Action("rate_cap", {"key": "acct-0042", "rps": -1}),
        Action("restart", {"service": "edge"}),
        Action("shutdown", {}),
    ],
)
def test_an_invalid_action_is_refused(action: Action) -> None:
    with pytest.raises(ValueError, match="not a valid action"):
        to_request(action)


def test_the_undo_puts_back_the_value_before() -> None:
    assert undo_for(Action("backoff", {"enabled": True}), BEFORE) == Action("backoff", {"enabled": False})
    assert undo_for(Action("rate_cap", {"key": "acct-0007", "rps": 1}), BEFORE) == Action(
        "rate_cap", {"key": "acct-0007", "rps": 5.0}
    )
    assert undo_for(Action("rate_cap", {"key": "acct-0042", "rps": 1}), BEFORE) == Action(
        "rate_cap", {"key": "acct-0042", "rps": None}
    )
    on = {**BEFORE, "backoff": True}
    assert undo_for(Action("backoff", {"enabled": True}), on) == Action("backoff", {"enabled": True})  # stays on
    assert undo_for(Action("restart", {"service": "ledger"}), BEFORE) is None
    assert undo_for(Action("backoff", {"enabled": True}), {"error": "down"}) is None  # unknown is no undo


def _env(status: int = 200) -> tuple[SimprodEnv, list[tuple]]:
    """An environment whose edge is a list: every post is recorded and answered `status`."""
    posts: list[tuple] = []

    def post(path: str, body: dict, actor: str, credential: str) -> dict:
        """Record the request; answer as `controls.act` does."""
        posts.append((path, body, actor, credential))
        return {"path": path, "body": body, "status": status, "answer": {"ok": status == 200}}

    env = SimprodEnv(lambda: [], lambda: 0.0, post=post, get=lambda url: "", settings=lambda: BEFORE)
    return env, posts


def test_apply_sends_as_findyfixy_and_returns_the_undo() -> None:
    env, posts = _env()
    applied = env.apply(Action("backoff", {"enabled": True}), "cred")
    assert applied.ok and applied.undo == Action("backoff", {"enabled": False})
    assert posts == [("/gateway/actions/backoff", {"enabled": True}, "findyfixy", "cred")] and len(env.sent) == 1


def test_apply_refuses_an_invalid_action_before_sending_anything() -> None:
    env, posts = _env()
    applied = env.apply(Action("restart", {"service": "edge"}), "cred")
    assert not applied.ok and applied.detail.startswith("refused") and posts == []


def test_a_restart_answered_202_is_carried_out() -> None:
    """Exploratory storm s3-01 (research log, entry 013): only 200 counted, so an accepted restart ended the try."""
    env, _ = _env(status=202)
    applied = env.apply(Action("restart", {"service": "gateway"}), "cred")
    assert applied.ok and applied.undo is None


def test_a_reply_lost_after_sending_is_not_ok_but_keeps_its_undo() -> None:
    def post(path: str, body: dict, actor: str, credential: str) -> dict:
        """The request went out; the reply was cut short."""
        raise ValueError("reply was not JSON")

    env = SimprodEnv(lambda: [], lambda: 0.0, post=post, get=lambda url: "", settings=lambda: BEFORE)
    applied = env.apply(Action("backoff", {"enabled": True}), "cred")
    assert not applied.ok and applied.undo == Action("backoff", {"enabled": False}) and env.sent[0]["status"] == 0


def test_a_refused_request_is_not_ok_but_keeps_its_undo() -> None:
    env, _ = _env(status=0)
    applied = env.apply(Action("backoff", {"enabled": True}), "cred")
    assert not applied.ok and applied.undo is not None  # it may have happened


# ---- The decider ---------------------------------------------------------------------------


def test_a_valid_reply_becomes_typed_actions() -> None:
    reply = decision({"name": "rate_cap", "key": "acct-0042", "rps": 10}, {"name": "backoff", "enabled": True})
    d = ModelDecider(lambda m, p, n: reply, "m", 30, budget())
    assert d.decide("metrics", []) == [
        Action("rate_cap", {"key": "acct-0042", "rps": 10.0}),
        Action("backoff", {"enabled": True}),
    ]
    assert len(d.calls) == 1 and d.calls[0].valid and d.reasons == ["the ledger is retrying"]


@pytest.mark.parametrize(
    "bad",
    [
        decision({"name": "backoff", "enabled": True, "key": "acct-0042"}),  # an argument it does not take
        decision({"name": "rate_cap", "key": "acct-0042"}),  # a missing argument
        decision({"name": "rate_cap", "key": "acct-0042", "rps": 0}),  # not a positive rate
        decision(*[{"name": "backoff", "enabled": True}] * 3),  # more than 2 actions
        decision(),  # none
        decision({"name": "restart", "service": "edge"}),  # not a service it may restart
        "not json",
    ],
)
def test_an_invalid_reply_is_retried_once_with_the_error(bad: str) -> None:
    prompts: list[str] = []
    replies = iter([bad, decision({"name": "backoff", "enabled": True})])

    def ask(model: str, prompt: str, cap: int) -> str:
        """The scripted replies, recording each prompt."""
        prompts.append(prompt)
        return next(replies)

    d = ModelDecider(ask, "m", 30, budget())
    assert d.decide("metrics", []) == [Action("backoff", {"enabled": True})]
    assert [c.valid for c in d.calls] == [False, True] and "not valid" in prompts[1]


def test_a_reply_still_invalid_after_its_retry_raises() -> None:
    d = ModelDecider(lambda m, p, n: "not json", "m", 30, budget())
    with pytest.raises(DecisionError, match="no valid decision in 2 attempts"):
        d.decide("metrics", [])
    assert len(d.calls) == 2


def test_the_reason_comes_before_the_actions_in_the_schema() -> None:
    assert list(Decision.model_json_schema()["properties"]) == ["reason", "actions"]


def test_the_prompt_tells_the_model_what_was_tried() -> None:
    t = Try(1, (Action("backoff", {"enabled": True}),), (), 0.0, "not verified")
    assert format_history([t]).endswith("- Try 1: backoff(enabled=True): not verified, undone.")
    assert format_history([]) == "Tried so far: nothing."


# ---- Both inside the harness's loop ------------------------------------------------------------


def test_the_model_decider_drives_the_harness_loop_to_a_resolved_incident() -> None:
    replies = iter(
        [
            decision({"name": "backoff", "enabled": True}),
            decision({"name": "rate_cap", "key": "acct-0042", "rps": 10}),
        ]
    )
    prompts: list[str] = []

    def ask(model: str, prompt: str, cap: int) -> str:
        """Backoff first (wrong here), then the cap."""
        prompts.append(prompt)
        return next(replies)

    decider = ModelDecider(ask, "m", 30, budget())
    config, policy = LoopConfig(wait_s=30, hold_s=30), allow("backoff", "rate_cap")
    out = run_incident(FakeWorld(fix=["rate_cap"]), decider, config, policy, FakeSigner())
    assert out.resolved and [t.verdict for t in out.tries] == ["not verified", "resolved"]
    assert "Try 1: backoff(enabled=True): not verified, undone." in prompts[1]
    assert len(decider.budget.charges) == 2 and decider.budget.spent_usd > 0
    record = {
        "storm": {"id": "s3-00", "kind": "hot_shard"},
        "loop": outcome_json(out),
        "recovered": True,
        "customer_minutes": 0.1,
        "model_calls": [{}, {}],
        "budget": {"limit_usd": 0.005, "spent_usd": 0.0002},
    }
    assert "resolved" in line(record) and "1: backoff -> not verified; 2: rate_cap -> resolved" in line(record)


# ---- The policy --------------------------------------------------------------------------------

GATEWAY_TOP = 'gateway_top_keys_rps{key="acct-0640"} 130.3\ngateway_top_keys_rps{key="acct-0774"} 0.6\n'


def test_the_policy_reads_the_busiest_keys_and_the_customer_rate() -> None:
    assert top_keys(GATEWAY_TOP) == {"acct-0640": 130.3, "acct-0774": 0.6}
    assert customer_rate("customers_rate 200\n") == 200.0 and customer_rate("") is None


@pytest.mark.parametrize(
    ("key", "refused_because"),
    [
        ("acct-0640", None),  # the runaway in entry 013's hot shard: 130.3/s
        ("acct-0774", "sends 0.6/s, under 5% of the 200/s"),  # the innocent key entry 013's model capped
        ("acct-0001", "not among the 2 busiest keys"),
    ],
)
def test_a_cap_is_allowed_only_on_a_key_that_is_a_cause(key: str, refused_because: str | None) -> None:
    refusal = cap_refusal(key, top_keys(GATEWAY_TOP), 200.0)
    assert refusal is None if refused_because is None else refused_because in str(refusal)


def test_a_refused_cap_says_what_would_pass() -> None:
    """Entry 014: told only "not among the busiest", the model proposed another unlisted key three times."""
    assert cap_refusal("acct-0774", top_keys(GATEWAY_TOP), 200.0).endswith("keys that qualify now: acct-0640 (130.3/s)")
    calm = top_keys('gateway_top_keys_rps{key="acct-0673"} 0.9\n')
    assert cap_refusal("acct-0090", calm, 200.0) == (
        "acct-0090 is not among the 1 busiest keys; no key sends 10/s or more now, so no rate_cap can be allowed"
    )


def test_an_unreadable_customer_rate_refuses_the_cap() -> None:
    assert cap_refusal("acct-0640", top_keys(GATEWAY_TOP), None) == "the customers' request rate could not be read"


def test_findyfixys_policy_refuses_a_restart_beside_a_cap_and_a_cap_on_an_innocent_key() -> None:
    def get(url: str) -> str:
        """The gateway's and the customers' metrics, as the edge serves them."""
        return GATEWAY_TOP if "/gateway/" in url else "customers_rate 200\n"

    policy = Policy(rules(get))
    runaway = Action("rate_cap", {"key": "acct-0640", "rps": 10.0})
    assert policy.check([runaway, Action("backoff", {"enabled": True})]) == []
    assert policy.check([runaway, Action("restart", {"service": "gateway"})]) == [
        "restart: must be the only action in its decision"
    ]
    assert "under 5%" in policy.check([Action("rate_cap", {"key": "acct-0774", "rps": 1.0})])[0]
    assert len(policy.notes()) == 2


# ---- The budget in the decider -----------------------------------------------------------------


def test_the_decider_reserves_before_each_call_and_settles_after() -> None:
    calls: list[int] = []

    def ask(model: str, prompt: str, cap: int) -> str:
        """Record the cap the call was given."""
        calls.append(cap)
        return decision({"name": "backoff", "enabled": True})

    d = ModelDecider(ask, "m", 30, budget())
    d.decide("metrics", [])
    assert calls == [400] and len(d.budget.charges) == 1 and d.budget.held_usd == pytest.approx(0.0)


def test_a_call_that_does_not_fit_is_never_made() -> None:
    calls: list[str] = []

    def ask(model: str, prompt: str, cap: int) -> str:
        """Record any call that is made; none should be."""
        calls.append(prompt)
        return ""

    d = ModelDecider(ask, "m", 30, budget(limit=0.00001))
    with pytest.raises(BudgetExceeded):
        d.decide("metrics", [])
    assert calls == []


def test_a_call_that_raises_is_charged_its_reservation() -> None:
    def ask(model: str, prompt: str, cap: int) -> str:
        """The model server timed out after the request went out."""
        raise TimeoutError("no reply")

    d = ModelDecider(ask, "m", 30, budget())
    with pytest.raises(TimeoutError):
        d.decide("metrics", [])
    charge = d.budget.charges[0]
    assert charge.cost_usd == charge.reserved_usd > 0 and d.budget.held_usd == pytest.approx(0.0)


def test_the_prompt_states_the_rules_and_a_refused_try() -> None:
    t = Try(1, (Action("restart", {"service": "gateway"}),), (), 0.0, "denied", reasons=("restart: must be alone",))
    prompt = build_prompt("metrics", [t], 30, ["A restart is refused unless it is alone."])
    assert "Rules the code enforces:\n- A restart is refused unless it is alone." in prompt
    assert "refused by the rules, nothing applied (restart: must be alone)" in format_history([t])


# ---- Checks before any storm --------------------------------------------------------------------


def test_a_model_the_budget_cannot_carry_is_refused_before_any_storm() -> None:
    config = LoopConfig(wait_s=30, hold_s=30)
    small = ModelPrice("small", 0.00000005, 0.0000004, "test")
    large = ModelPrice("large", 0.00000025, 0.000002, "test")  # the 7B and 8B models' reference price
    assert budget_refusal("small", [small, large], config) is None
    assert "exceed the $0.005 incident budget" in str(budget_refusal("large", [small, large], config))
    assert "no reference price" in str(budget_refusal("unpriced", [small], config))


def test_the_driver_measures_from_the_last_try_that_applied_something() -> None:
    """The review's case: try 1 applied and was undone, tries 2 and 3 were refused."""
    config = LoopConfig(wait_s=30, hold_s=30)
    out = run_incident(FakeWorld(fix=["z"]), FakeDecider([["a"], ["b"], ["b"]]), config, allow("a"), FakeSigner())
    assert [t.verdict for t in out.tries] == ["not verified", "denied", "denied"]
    assert last_action_at(out) == out.tries[0].t_act != out.tries[-1].t_act
    refused_only = run_incident(FakeWorld(), FakeDecider([["b"]] * 3), config, allow("a"), FakeSigner())
    assert last_action_at(refused_only) is None
