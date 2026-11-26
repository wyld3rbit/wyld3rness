"""Tests for wyld3rness/loop/run.py: every way an incident can end, on a fake world and a fake clock."""

from wyld3rness.loop.budget import BudgetExceeded
from wyld3rness.loop.env import Action
from wyld3rness.loop.policy import Policy, Rule
from wyld3rness.loop.run import LoopConfig, run_incident
from wyld3rness.testing import FakeDecider, FakeWorld, allow

CONFIG = LoopConfig(wait_s=30, hold_s=30)
POLICY = allow("a", "b", "c", "restart")  # every kind these tests propose


def test_the_right_fix_is_kept_on_the_first_try() -> None:
    world = FakeWorld(fix=["a"])
    out = run_incident(world, FakeDecider([["a"]]), CONFIG, POLICY)
    assert out.resolved and out.reason == "resolved on try 1"
    assert [t.verdict for t in out.tries] == ["resolved"]
    assert out.tries[0].recovered_at == 1.0
    assert out.kept == [Action("a")] and world.log == ["a"]


def test_a_wrong_fix_is_undone_and_the_next_try_is_told_what_failed() -> None:
    world = FakeWorld(fix=["c"])
    decider = FakeDecider([["a", "b"], ["c"]])
    out = run_incident(world, decider, CONFIG, POLICY)
    assert out.resolved and [t.verdict for t in out.tries] == ["not verified", "resolved"]
    assert world.log == ["a", "b", "undo:b", "undo:a", "c"]  # undone last first, before the next try
    _, history = decider.seen[1]
    assert [a.name for a in history[0].actions] == ["a", "b"] and history[0].verdict == "not verified"
    assert decider.seen[1][0] == "in force: []"  # the next decision sees the state put back
    assert out.kept == [Action("c")]


def test_a_combined_fix_needs_both_actions_together() -> None:
    world = FakeWorld(fix=["a", "b"])
    out = run_incident(world, FakeDecider([["a"], ["a", "b"]]), CONFIG, POLICY)
    assert out.resolved and len(out.tries) == 2
    assert world.log == ["a", "undo:a", "a", "b"]


def test_out_of_tries_ends_in_the_safe_state_with_nothing_kept() -> None:
    world = FakeWorld(fix=["z"])
    out = run_incident(world, FakeDecider([["a"], ["b"], ["c"]]), CONFIG, POLICY)
    assert not out.resolved and out.ended == "safe state"
    assert out.reason == "out of tries: 3 tried, none resolved"
    assert len(out.tries) == 3 and out.kept == [] and world.in_force == set()


def test_a_recovery_that_does_not_hold_is_undone_and_retried() -> None:
    # Users recover 20 s after the fix, pass verification at 30 s, and break again at 35 s.
    world = FakeWorld(fix=["a"], delay_s=20, relapse_after_s=15)
    out = run_incident(world, FakeDecider([["a"], ["b"], ["c"]]), CONFIG, POLICY)
    assert out.tries[0].verdict == "did not hold" and out.tries[0].undone
    assert not out.resolved


def test_a_failed_action_ends_in_the_safe_state_with_no_retry() -> None:
    world = FakeWorld(fix=["a"], refuse=["b"])
    decider = FakeDecider([["a", "b"], ["a"]])
    out = run_incident(world, decider, CONFIG, POLICY)
    assert out.reason.startswith("action failed: b()") and len(decider.seen) == 1
    assert out.tries[0].verdict == "action failed"
    # The refused action may have happened, so its undo is sent too, then the one before it.
    assert world.log == ["a", "b", "undo:b", "undo:a"] and out.kept == []


def test_an_action_without_undo_that_does_not_resolve_ends_with_no_retry() -> None:
    world = FakeWorld(fix=["a"], no_undo=["restart"])
    decider = FakeDecider([["restart"], ["a"]])
    out = run_incident(world, decider, CONFIG, POLICY)
    assert out.ended == "safe state" and "has no undo: no retry" in out.reason
    assert len(decider.seen) == 1 and out.tries[0].verdict == "no undo"
    assert out.kept == [Action("restart")]  # it cannot be put back, and the record says so


def test_an_action_without_undo_that_resolves_is_kept() -> None:
    world = FakeWorld(fix=["restart"], no_undo=["restart"])
    out = run_incident(world, FakeDecider([["restart"]]), CONFIG, POLICY)
    assert out.resolved and out.kept == [Action("restart")]


def test_a_decider_failure_ends_in_the_safe_state() -> None:
    world = FakeWorld(fix=["z"])
    out = run_incident(world, FakeDecider([["a"], ["b"]], fail=lambda n: n == 2), CONFIG, POLICY)
    assert out.reason.startswith("decider failed: ValueError") and len(out.tries) == 1
    assert world.log == ["a", "undo:a"] and out.kept == []


def test_a_decision_of_the_wrong_size_is_refused_before_anything_is_applied() -> None:
    for names in ([], ["a", "b", "c"]):
        world = FakeWorld(fix=["a"])
        out = run_incident(world, FakeDecider([names]), CONFIG, POLICY)
        assert out.ended == "safe state" and f"{len(names)} actions" in out.reason
        assert world.log == [] and out.tries == []


def test_an_apply_that_raises_is_kept_as_possibly_in_force() -> None:
    """The review's case: the change went through, then the reply was lost."""
    world = FakeWorld(fix=["a"], raise_on=["b"])
    out = run_incident(world, FakeDecider([["a", "b"]]), CONFIG, POLICY)
    assert out.reason == "action failed: b(): apply raised: RuntimeError: b exploded"
    assert world.log == ["a", "b", "undo:a"]  # a is put back; b's undo is unknown
    assert out.kept == [Action("b")] and world.in_force == {"b"}  # the record matches the world


def test_an_environment_that_raises_outside_an_apply_ends_in_the_safe_state() -> None:
    class Blind(FakeWorld):
        """A world whose metrics cannot be read on the second try."""

        def observe(self) -> str:
            if self.log:
                raise ConnectionError("metrics unreachable")
            return super().observe()

    world = Blind(fix=["z"])
    out = run_incident(world, FakeDecider([["a"], ["b"]]), CONFIG, POLICY)
    assert out.reason == "environment failed: ConnectionError: metrics unreachable"
    assert world.log == ["a", "undo:a"] and out.kept == []


def test_a_slow_apply_does_not_shorten_the_recovery_window() -> None:
    """The review's case: 8 s to apply, recovery 2 s later. WAIT counts from the last apply."""
    world = FakeWorld(fix=["a"], delay_s=1, apply_s=8)
    out = run_incident(world, FakeDecider([["a"]]), LoopConfig(wait_s=10, hold_s=10), POLICY)
    assert out.resolved and out.tries[0].t_act == 8.0


def test_a_decider_that_returns_no_sequence_is_a_decider_failure() -> None:
    class Broken:
        """Returns None instead of actions."""

        def decide(self, observation: str, history: object) -> None:
            return None

    out = run_incident(FakeWorld(), Broken(), CONFIG, POLICY)  # type: ignore[arg-type]
    assert out.reason.startswith("decider failed: TypeError")


def test_a_failed_undo_stops_the_loop_and_the_safe_state_tries_it_once_more() -> None:
    world = FakeWorld(fix=["z"], refuse=["undo:b"])
    decider = FakeDecider([["a", "b"], ["c"]])
    out = run_incident(world, decider, CONFIG, POLICY)
    assert out.reason.startswith("undo failed: undo:b()") and len(decider.seen) == 1
    assert world.log == ["a", "b", "undo:b", "undo:b", "undo:a"]
    assert out.kept == [Action("b")]  # still in force: the record names it


def test_verification_uses_the_environment_clock_not_the_decision() -> None:
    world = FakeWorld(fix=["a"])
    run_incident(world, FakeDecider([["a"]]), CONFIG, POLICY)
    assert world.t == CONFIG.wait_s + CONFIG.hold_s + CONFIG.hold_margin_s


def test_a_refused_decision_applies_nothing_and_spends_a_try() -> None:
    world = FakeWorld(fix=["c"])
    decider = FakeDecider([["a", "b"], ["c"]])
    out = run_incident(world, decider, CONFIG, allow("a", "c"))
    assert out.resolved and [t.verdict for t in out.tries] == ["denied", "resolved"]
    assert out.tries[0].reasons == ("b: not an allowed kind of action",) and out.tries[0].applied == ()
    assert world.log == ["c"]  # nothing of the refused decision was applied, so nothing needed undoing
    assert decider.seen[1][1][0].verdict == "denied"  # the next decision is told


def test_a_restart_beside_another_action_is_refused() -> None:
    world = FakeWorld(fix=["restart"], no_undo=["restart"])
    policy = Policy([Rule("a"), Rule("restart", alone=True)])
    out = run_incident(world, FakeDecider([["a", "restart"], ["restart"]]), CONFIG, policy)
    assert [t.verdict for t in out.tries] == ["denied", "resolved"] and world.log == ["restart"]


def test_a_decider_that_keeps_proposing_refused_actions_runs_out_of_tries() -> None:
    world = FakeWorld(fix=["a"])
    out = run_incident(world, FakeDecider([["b"], ["b"], ["b"]]), CONFIG, allow("a"))
    assert out.reason == "out of tries: 3 tried, none resolved" and world.log == []


def test_a_budget_refusal_ends_in_the_safe_state_named_as_the_budget() -> None:
    class Thrifty(FakeDecider):
        """Its second model call does not fit the budget."""

        def decide(self, observation: str, history: list) -> list[Action]:
            if history:
                raise BudgetExceeded("worst case $0.0003 does not fit the $0.0001 left")
            return super().decide(observation, history)

    world = FakeWorld(fix=["z"])
    out = run_incident(world, Thrifty([["a"]]), CONFIG, POLICY)
    assert out.reason.startswith("budget: worst case") and world.log == ["a", "undo:a"]
