"""Tests for wyld3rness/loop/policy.py: kinds, actions that must be alone, and preconditions that fail closed."""

import pytest

from wyld3rness.loop.env import Action
from wyld3rness.loop.policy import Policy, Rule


def test_a_kind_the_table_does_not_name_is_refused() -> None:
    assert Policy([Rule("a")]).check([Action("b")]) == ["b: not an allowed kind of action"]


def test_an_allowed_decision_has_no_reasons() -> None:
    assert Policy([Rule("a"), Rule("b")]).check([Action("a"), Action("b")]) == []


def test_an_action_that_must_be_alone_is_refused_beside_another() -> None:
    policy = Policy([Rule("restart", alone=True), Rule("a")])
    assert policy.check([Action("restart")]) == []
    assert policy.check([Action("a"), Action("restart")]) == ["restart: must be the only action in its decision"]


def test_a_precondition_gives_its_reason() -> None:
    policy = Policy([Rule("cap", precondition=lambda a: None if a.args["rps"] > 5 else "too small")])
    assert policy.check([Action("cap", {"rps": 10})]) == []
    assert policy.check([Action("cap", {"rps": 1})]) == ["cap(rps=1): too small"]


def test_a_precondition_that_cannot_be_checked_refuses() -> None:
    def broken(action: Action) -> str | None:
        raise ConnectionError("metrics unreachable")

    reasons = Policy([Rule("cap", precondition=broken)]).check([Action("cap")])
    assert reasons == ["cap(): its precondition could not be checked: ConnectionError: metrics unreachable"]


def test_every_reason_is_collected() -> None:
    policy = Policy([Rule("restart", alone=True)])
    assert len(policy.check([Action("x"), Action("restart")])) == 2


def test_a_kind_named_twice_is_a_mistake() -> None:
    with pytest.raises(ValueError, match="named twice"):
        Policy([Rule("a"), Rule("a", alone=True)])


def test_the_notes_come_from_the_table() -> None:
    policy = Policy([Rule("b", note="B is careful."), Rule("a"), Rule("c", note="C is alone.")])
    assert policy.kinds() == ["a", "b", "c"] and policy.notes() == ["B is careful.", "C is alone."]
