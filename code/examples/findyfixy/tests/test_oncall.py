"""Tests for findyfixy/oncall: the environment over simprod, the model decider, and both inside the harness's loop."""

import json

import pytest

from findyfixy.oncall.cli import line, outcome_json
from findyfixy.oncall.decide import Decision, DecisionError, ModelDecider, format_history
from findyfixy.oncall.env import SimprodEnv, observation, to_request, undo_for
from wyld3rness.loop.env import Action
from wyld3rness.loop.run import LoopConfig, Try, run_incident
from wyld3rness.testing import FakeWorld

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

    def post(path: str, body: dict, actor: str) -> dict:
        """Record the request; answer as `controls.act` does."""
        posts.append((path, body, actor))
        return {"path": path, "body": body, "status": status, "answer": {"ok": status == 200}}

    env = SimprodEnv(lambda: [], lambda: 0.0, post=post, get=lambda url: "", settings=lambda: BEFORE)
    return env, posts


def test_apply_sends_as_findyfixy_and_returns_the_undo() -> None:
    env, posts = _env()
    applied = env.apply(Action("backoff", {"enabled": True}))
    assert applied.ok and applied.undo == Action("backoff", {"enabled": False})
    assert posts == [("/gateway/actions/backoff", {"enabled": True}, "findyfixy")] and len(env.sent) == 1


def test_apply_refuses_an_invalid_action_before_sending_anything() -> None:
    env, posts = _env()
    applied = env.apply(Action("restart", {"service": "edge"}))
    assert not applied.ok and applied.detail.startswith("refused") and posts == []


def test_a_restart_answered_202_is_carried_out() -> None:
    """Exploratory storm s3-01 (research log, entry 013): only 200 counted, so an accepted restart ended the try."""
    env, _ = _env(status=202)
    applied = env.apply(Action("restart", {"service": "gateway"}))
    assert applied.ok and applied.undo is None


def test_a_reply_lost_after_sending_is_not_ok_but_keeps_its_undo() -> None:
    def post(path: str, body: dict, actor: str) -> dict:
        """The request went out; the reply was cut short."""
        raise ValueError("reply was not JSON")

    env = SimprodEnv(lambda: [], lambda: 0.0, post=post, get=lambda url: "", settings=lambda: BEFORE)
    applied = env.apply(Action("backoff", {"enabled": True}))
    assert not applied.ok and applied.undo == Action("backoff", {"enabled": False}) and env.sent[0]["status"] == 0


def test_a_refused_request_is_not_ok_but_keeps_its_undo() -> None:
    env, _ = _env(status=0)
    applied = env.apply(Action("backoff", {"enabled": True}))
    assert not applied.ok and applied.undo is not None  # it may have happened


# ---- The decider ---------------------------------------------------------------------------


def test_a_valid_reply_becomes_typed_actions() -> None:
    reply = decision({"name": "rate_cap", "key": "acct-0042", "rps": 10}, {"name": "backoff", "enabled": True})
    d = ModelDecider(lambda m, p: reply, "m", wait_s=30)
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

    def ask(model: str, prompt: str) -> str:
        """The scripted replies, recording each prompt."""
        prompts.append(prompt)
        return next(replies)

    d = ModelDecider(ask, "m", wait_s=30)
    assert d.decide("metrics", []) == [Action("backoff", {"enabled": True})]
    assert [c.valid for c in d.calls] == [False, True] and "not valid" in prompts[1]


def test_a_reply_still_invalid_after_its_retry_raises() -> None:
    d = ModelDecider(lambda m, p: "not json", "m", wait_s=30)
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

    def ask(model: str, prompt: str) -> str:
        """Backoff first (wrong here), then the cap."""
        prompts.append(prompt)
        return next(replies)

    out = run_incident(FakeWorld(fix=["rate_cap"]), ModelDecider(ask, "m", 30), LoopConfig(wait_s=30, hold_s=30))
    assert out.resolved and [t.verdict for t in out.tries] == ["not verified", "resolved"]
    assert "Try 1: backoff(enabled=True): not verified, undone." in prompts[1]
    record = {
        "storm": {"id": "s3-00", "kind": "hot_shard"},
        "loop": outcome_json(out),
        "recovered": True,
        "customer_minutes": 0.1,
        "model_calls": [{}, {}],
    }
    assert "resolved" in line(record) and "1: backoff -> not verified; 2: rate_cap -> resolved" in line(record)
