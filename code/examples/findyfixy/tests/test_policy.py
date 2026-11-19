"""FindyFixy's side of the action ledger: the rollback rule on v3's real facts, failing closed, and the policy step.

Fakes only: the committed v3 change log, no model.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

import pytest

from findyfixy.evals.golden_v3 import load_golden_v3
from findyfixy.events import Event, make_emit
from findyfixy.policy import FindyFixyActions, dependents, make_judge
from findyfixy.tools.changes import changes_path, load_changes
from findyfixy.tools.services import DEFAULT_SERVICES_PATH, load_map
from findyfixy.triage.graph import make_policy
from findyfixy.triage.record import AlertFacts, Decision
from findyfixy.wiring import build_judge

ALERTS = {c.id: AlertFacts(c.alert_at, c.alert_service) for c in load_golden_v3()}
CHANGES = load_changes(changes_path("v3"))


def decision(action: str = "rollback", version: str | None = "2.14.1", severity: str = "sev1", component: str = "auth"):
    """A validated decision as the agent would submit it."""
    return Decision(
        severity=severity,  # type: ignore[arg-type]
        component=component,
        next_action="roll back",
        confidence=0.9,
        action=action,  # type: ignore[arg-type]
        rollback_version=version if action == "rollback" else None,
    )


def judge_at(case: str):
    """The judge for one v3 case: its alert time, the committed change log."""
    return make_judge(case, FindyFixyActions(CHANGES, ALERTS[case]))


def test_h26_rollback_passes_every_fact() -> None:
    """One change in two hours, a deploy, safe to roll back, and 2.14.1 is the version before it."""
    v = judge_at("h26")(decision())
    assert (v.outcome, v.action) == ("allowed", "rollback")
    assert v.facts == (
        "deploy 2.15.0 on auth, 60 min before the alert (Switch password hashing library), safe to roll back",
    )


def test_h23_rollback_is_downgraded_and_the_key_rotation_is_listed() -> None:
    """Every fact about the deploy holds, as on h26; the key rotation 8 minutes before the alert is what stops it."""
    v = judge_at("h23")(decision(version="2.13.4"))
    assert (v.outcome, v.action) == ("downgraded", "page_human")
    assert v.reasons == ("2 changes to auth in the 2 hours before the alert: a rollback needs exactly one",)
    assert v.facts[0].startswith("key_rotation token-signing-key on auth, 8 min before the alert")


@pytest.mark.parametrize(
    ("case", "version", "service"),
    [
        ("h27", "5.2.0", "billing"),
        ("h28", "1.40.1", "gateway"),
        ("h29", "3.3.0", "notifications"),
        ("h30", "0.9.3", "voice-verification"),
    ],
)
def test_every_other_rollback_case_passes(case: str, version: str, service: str) -> None:
    """The rule keeps all five correct rollbacks of v3."""
    assert judge_at(case)(decision(version=version, severity="sev2", component=service)).outcome == "allowed"


def test_rolling_back_to_the_wrong_version_is_downgraded() -> None:
    """The version must be the deploy before the one in the window, from the log, not from the model."""
    v = judge_at("h26")(decision(version="2.13.4"))
    assert v.outcome == "downgraded" and v.reasons == ("rollback to 2.13.4, but the version before 2.15.0 is 2.14.1",)


def test_a_rollback_with_no_change_in_the_window_is_downgraded() -> None:
    """h14 on v3: the agent proposed a rollback the case did not call for; nothing changed in two hours."""
    v = judge_at("h14")(decision(component="voice-verification", version="0.9.2"))
    assert v.outcome == "downgraded" and v.reasons == (
        "0 changes to voice-verification in the 2 hours before the alert: a rollback needs exactly one",
    )


def test_a_rollback_of_a_service_the_alert_does_not_depend_on_is_downgraded() -> None:
    """A fooled model naming an unrelated service: h29 alerts on notifications, billing is not in its chain."""
    v = judge_at("h29")(decision(component="billing", version="5.2.0", severity="sev2"))
    assert v.outcome == "downgraded"
    assert "billing is neither the alerting service (notifications) nor one it depends on" in v.reasons


def test_a_rollback_upstream_of_the_alerting_service_is_within_bounds() -> None:
    """A cascade: voice-verification depends on gateway, so the bound allows gateway (other facts still apply)."""
    port = FindyFixyActions(CHANGES, ALERTS["h30"])
    v = make_judge("x", port)(decision(component="gateway", version="1.40.1", severity="sev2"))
    assert not any("neither the alerting service" in r for r in v.reasons)


def test_low_severity_and_unknown_services_are_downgraded_with_every_reason() -> None:
    v = judge_at("h26")(decision(severity="sev3"))
    assert v.outcome == "downgraded" and v.reasons == ("severity sev3: a rollback needs sev1 or sev2",)
    unknown = judge_at("h26")(decision(component="the token signer"))
    assert unknown.outcome == "downgraded" and "not a known service" in unknown.reasons[-1]


def test_the_component_is_matched_case_blind() -> None:
    assert judge_at("h26")(decision(component=" Auth ")).outcome == "allowed"


def test_without_a_change_log_every_rollback_fails_closed() -> None:
    """v1 and v2 have no change log and no clock: a rollback cannot be verified, so it is never allowed."""
    for version in ("v1", "v2"):
        v = build_judge(version)(decision())
        assert (v.outcome, v.action) == ("downgraded", "page_human")
        assert "no change log" in v.reasons[-1]


def test_reading_and_paging_always_pass() -> None:
    judge = build_judge("v1")
    assert judge(decision(action="read")).outcome == "allowed"
    assert judge(decision(action="page_human")).outcome == "allowed"


def test_a_rollback_holds_the_service_and_everything_that_depends_on_it() -> None:
    """auth breaks gateway, search and billing; gateway breaks voice-verification."""
    port = FindyFixyActions(CHANGES, ALERTS["h26"])
    assert port.blast_radius("auth") == {"auth", "gateway", "search", "billing", "voice-verification"}
    assert port.blast_radius("ledger") == {"ledger", "billing"}
    assert dependents(load_map(DEFAULT_SERVICES_PATH))["voice-verification"] == frozenset()


def test_a_second_rollback_in_the_same_incident_waits() -> None:
    """One ledger per incident: the agent proposing twice gets the second queued, not run twice."""
    judge = judge_at("h26")
    assert judge(decision()).outcome == "allowed"
    second = judge(decision())
    assert second.outcome == "queued" and "auth" in second.reasons[0]


def test_the_policy_step_reports_the_verdict_and_leaves_the_decision_as_written() -> None:
    """The decision (what is scored) is unchanged; the verdict and a `policy` event say what may proceed."""
    events: list[Event] = []
    step = make_policy(judge_at("h23"), make_emit(events.append, lambda: 0.0))
    written = decision(version="2.13.4")
    out: dict[str, Any] = step({"question": "q", "triage": written})  # type: ignore[arg-type]
    assert out["verdict"].action == "page_human" and written.action == "rollback"
    (event,) = events
    assert event.kind == "policy"
    assert {k: event.data[k] for k in ("outcome", "proposed", "action", "proposer", "target")} == {
        "outcome": "downgraded",
        "proposed": "rollback",
        "action": "page_human",
        "proposer": "agent",
        "target": "auth",
    }
    assert event.data["facts"][0].startswith("key_rotation")
    assert step({"question": "q", "triage": None}) == {}  # no decision, nothing to judge


def test_the_judges_clock_is_the_cases_own() -> None:
    """The same rollback judged as of a later moment sees nothing in its window."""
    later = make_judge("x", FindyFixyActions(CHANGES, AlertFacts(datetime(2026, 10, 26, 18, 0, tzinfo=UTC), "auth")))
    assert later(decision()).outcome == "downgraded"


def test_the_report_counts_rollbacks_the_ledger_let_through() -> None:
    """Allowed rollbacks are split by whether the case called for one; v1 (no action class) reports None."""
    from findyfixy.evals.adapter import rollbacks_allowed

    records = [
        {"verdict": "allowed", "final_action": "rollback", "action_class": "destructive"},
        {"verdict": "allowed", "final_action": "rollback", "action_class": "reversible"},
        {"verdict": "downgraded", "final_action": "page_human", "action_class": "read"},
        {"verdict": None, "final_action": None, "action_class": "destructive"},  # a failed run
    ]
    assert rollbacks_allowed(records) == {"rollbacks_allowed_rightly": 1, "rollbacks_allowed_wrongly": 1}
    v1 = [{"verdict": "allowed", "final_action": "rollback", "action_class": None}]
    assert rollbacks_allowed(v1) == {"rollbacks_allowed_rightly": None, "rollbacks_allowed_wrongly": None}


def test_each_graph_build_gets_a_fresh_ledger() -> None:
    """A hunt or an eval builds a graph per run; one run's held rollback must not queue the next."""
    from findyfixy.wiring import judge_factory

    new_judge = judge_factory(CHANGES, ALERTS["h26"])
    assert new_judge()(decision()).outcome == "allowed"
    assert new_judge()(decision()).outcome == "allowed"
