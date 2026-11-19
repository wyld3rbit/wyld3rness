"""The change-log tool (Day 7): windows look back from the alert, never forward, and every change kind counts.

Fakes only: a small log written to a temp file.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
import json
from pathlib import Path

from pydantic import ValidationError
import pytest

from findyfixy.tools.changes import CHANGES_TOOL, Change, changes_before, load_changes, make_changes_tool
from findyfixy.tools.deploys import DEPLOYS_TOOL
from findyfixy.tools.investigate import agent_prompt
from findyfixy.triage.record import AlertFacts
from findyfixy.wiring import agent_instructions, changes_tool, load_facts

ALERT = datetime(2026, 10, 23, 22, 3, tzinfo=UTC)


def change(minutes_before: float, kind: str = "deploy", service: str = "auth", **extra: object) -> Change:
    """One change `minutes_before` the alert (negative: after it)."""
    return Change(
        at=ALERT - timedelta(minutes=minutes_before),
        service=service,  # type: ignore[arg-type]
        kind=kind,  # type: ignore[arg-type]
        ref="r",
        author="team",
        summary="s",
        **extra,  # type: ignore[arg-type]
    )


@pytest.fixture
def log(tmp_path: Path) -> Path:
    """A log with a deploy, a rotation, an older deploy, another service's deploy and one after the alert."""
    rows = [
        change(63, rollback_safe=True),
        change(8, "key_rotation"),
        change(200, rollback_safe=True),
        change(30, service="gateway", rollback_safe=True),
        change(-5, rollback_safe=True),
    ]
    path = tmp_path / "changes.json"
    path.write_text(json.dumps([r.model_dump(mode="json", exclude_none=True) for r in rows]))
    return path


def test_the_window_holds_every_kind_newest_first_and_never_the_future(log: Path) -> None:
    """Two hours before 22:03: the rotation (8 min) and the deploy (63 min). Not the older one or the later one."""
    shown = json.loads(make_changes_tool(load_changes(log), ALERT).invoke({"service": "auth", "hours": 2}))
    assert [(c["kind"], c["minutes_ago"]) for c in shown] == [("key_rotation", 8), ("deploy", 63)]
    assert shown[1]["rollback_safe"] is True and "rollback_safe" not in shown[0]
    assert all("at" not in c and "service" not in c for c in shown)  # relative time only: no date arithmetic


def test_the_window_includes_its_start(log: Path) -> None:
    """A change exactly `hours` before the alert is inside: "within two hours" includes two hours."""
    changes = [change(120, rollback_safe=True)]
    assert changes_before(changes, "auth", ALERT, 2) == changes
    assert changes_before(changes, "auth", ALERT, 1) == []


def test_the_same_log_reads_differently_from_a_different_alert(log: Path) -> None:
    """The clock is the case's: an hour later the rotation is 68 minutes old and the deploy has left a 1-hour window."""
    later = make_changes_tool(load_changes(log), ALERT + timedelta(hours=1)).invoke({"service": "auth", "hours": 1})
    assert [c["minutes_ago"] for c in json.loads(later)] == [55]  # the change that was 5 minutes in the future


def test_bad_arguments_and_a_malformed_log_are_refused(log: Path, tmp_path: Path) -> None:
    """Arguments fail their schema (the model can correct them); a bad file raises (an operator fault)."""
    tool = make_changes_tool(load_changes(log), ALERT)
    with pytest.raises(ValidationError):
        tool.invoke({"service": "auth", "hours": 0})
    with pytest.raises(ValidationError):
        tool.invoke({"service": "payroll", "hours": 2})
    bad = tmp_path / "bad.json"
    row = {"at": "2026-10-23T21:00:00", "service": "auth", "kind": "deploy", "ref": "r", "author": "a", "summary": "s"}
    bad.write_text(json.dumps([row]))
    with pytest.raises(ValidationError):
        load_changes(bad)  # a time with no zone: "21:00" in which zone?


def test_a_change_log_version_needs_the_alert_and_a_deploy_log_version_refuses_one() -> None:
    """v3 looks back from each case's alert; v1 and v2 have no clock, so alert facts given to them would be ignored."""
    alert = AlertFacts(ALERT, "auth")
    assert changes_tool("v3", load_facts("v3", alert), alert).name == CHANGES_TOOL
    assert changes_tool("v2", load_facts("v2", None), None).name == DEPLOYS_TOOL
    with pytest.raises(ValueError, match="AlertFacts"):
        load_facts("v3", None)
    with pytest.raises(ValueError, match="no clock"):
        load_facts("v1", alert)


def test_the_instructions_name_the_tool_the_agent_has_and_v1_reads_as_before() -> None:
    """v1 and v2 name the deploy tool, exactly the text the v1 baseline ran; v3 names the change tool."""
    assert agent_instructions("v1")("q") == agent_prompt("q")
    assert "get_recent_deploys lists what changed in a service" in agent_prompt("q")
    assert agent_instructions("v2")("q") == agent_prompt("q")
    v3 = agent_instructions("v3")("q")
    assert f"{CHANGES_TOOL} lists what changed" in v3 and DEPLOYS_TOOL not in v3


def test_rollback_safe_is_required_on_a_deploy_and_refused_elsewhere() -> None:
    """The policy reads rollback_safe before a rollback, so the log must state it for every deploy and only there."""
    with pytest.raises(ValidationError, match="rollback_safe"):
        change(10)  # a deploy that does not say
    with pytest.raises(ValidationError, match="rollback_safe"):
        change(10, "key_rotation", rollback_safe=True)
