"""Tests for wyld3rness/loop/remote.py: the decider over HTTP, end to end on a local port."""

import threading

import pytest

from wyld3rness.loop.budget import BudgetExceeded
from wyld3rness.loop.env import Action, Applied
from wyld3rness.loop.remote import RemoteDecider, RemoteDecisionError, decode_try, encode_try, serve_decider
from wyld3rness.loop.run import LoopConfig, Try, run_incident
from wyld3rness.testing import FakeDecider, FakeSigner, FakeWorld, allow

TRY = Try(
    1,
    (Action("rate_cap", {"key": "acct-0640", "rps": 10.0}),),
    (
        Applied(
            Action("rate_cap", {"key": "acct-0640", "rps": 10.0}),
            True,
            "200",
            Action("rate_cap", {"key": "acct-0640", "rps": None}),
        ),
    ),
    42.5,
    "did not hold",
    51.5,
    (Applied(Action("rate_cap", {"key": "acct-0640", "rps": None}), True, "200", None),),
    ("a reason",),
)


def test_a_try_survives_the_wire_unchanged() -> None:
    assert decode_try(encode_try(TRY)) == TRY


class Recording(FakeDecider):
    """A fake decider that can also say what it kept, as the agent's must."""

    def record(self) -> dict:
        return {"decisions": len(self.seen)}


class Thrifty:
    """A decider whose every call is over budget."""

    def decide(self, observation, history):
        raise BudgetExceeded("worst case $0.0003 does not fit the $0.0001 left")

    def record(self) -> dict:
        return {}


class Broken(Thrifty):
    """A decider whose model never gives a valid reply."""

    def decide(self, observation, history):
        raise ValueError("no valid decision in 2 attempts")


@pytest.fixture
def serve():
    """Start a decider server on a free port; yield a function that serves a given decider factory."""
    servers = []

    def start(make):
        server = serve_decider(make, "127.0.0.1", 0)
        threading.Thread(target=server.serve_forever, daemon=True).start()
        servers.append(server)
        return f"http://127.0.0.1:{server.server_address[1]}"

    yield start
    for s in servers:
        s.shutdown()


def test_the_loop_runs_an_incident_through_a_remote_decider(serve) -> None:
    made: dict[str, Recording] = {}

    def make(incident: str, params: dict) -> Recording:
        made[incident] = Recording([["a"], ["c"]])
        return made[incident]

    url = serve(make)
    world = FakeWorld(fix=["c"])
    remote = RemoteDecider(url, "inc-1")
    out = run_incident(world, remote, LoopConfig(wait_s=30, hold_s=30), allow("a", "c"), FakeSigner())
    assert out.resolved and [t.verdict for t in out.tries] == ["not verified", "resolved"]
    assert made["inc-1"].seen[1][1][0] == out.tries[0]  # the agent saw the loop's own history, decoded
    assert remote.record() == {"decisions": 2}
    assert remote.record() == {"record_error": "unknown incident"}  # read once, then forgotten


def test_a_budget_refusal_comes_back_as_the_budget(serve) -> None:
    out = run_incident(
        FakeWorld(),
        RemoteDecider(serve(lambda i, p: Thrifty()), "inc-2"),
        LoopConfig(wait_s=30, hold_s=30),
        allow("a"),
        FakeSigner(),
    )
    assert out.reason.startswith("budget: worst case")


def test_a_decider_failure_is_named(serve) -> None:
    with pytest.raises(RemoteDecisionError, match="ValueError: no valid decision"):
        RemoteDecider(serve(lambda i, p: Broken()), "inc-3").decide("metrics", [])


def test_an_agent_that_is_not_there_is_a_decider_failure() -> None:
    with pytest.raises(RemoteDecisionError, match="agent unreachable"):
        RemoteDecider("http://127.0.0.1:9", "inc-4", timeout_s=2).decide("metrics", [])


def test_each_incident_gets_its_own_decider(serve) -> None:
    made: list[str] = []

    def make(incident: str, params: dict) -> Recording:
        made.append(incident)
        return Recording([["a"]] * 3)

    url = serve(make)
    for incident in ("x", "x", "y"):
        RemoteDecider(url, incident).decide("metrics", [])
    assert made == ["x", "y"]


def test_the_runs_parameters_reach_the_agent(serve) -> None:
    got: list[dict] = []

    def make(incident: str, params: dict) -> Recording:
        got.append(params)
        return Recording([["a"]])

    RemoteDecider(serve(make), "inc-5", {"model": "m", "budget_usd": 0.005, "wait_s": 30}).decide("metrics", [])
    assert got == [{"model": "m", "budget_usd": 0.005, "wait_s": 30}]


def test_an_incident_the_agent_lost_mid_way_is_refused_not_restarted(serve) -> None:
    """An agent that restarted would otherwise start a fresh budget for an incident already spending."""
    url = serve(lambda i, p: Recording([["a"]] * 3))
    with pytest.raises(RemoteDecisionError, match="unknown incident mid-way"):
        RemoteDecider(url, "inc-6").decide("metrics", [TRY])


def test_an_unreadable_record_says_so_and_is_never_a_zero() -> None:
    assert "record_error" in RemoteDecider("http://127.0.0.1:9", "inc-7").record()
