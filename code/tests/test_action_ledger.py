"""The action ledger (harness): rules in order, locks per blast radius, one destructive action in flight.

A fake port only: no app, no model.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field
import threading

from wyld3rness.action.ledger import ActionLedger, ActionSpec, Evidence, Proposal, Verdict

SPECS = {
    "look": ActionSpec("read", changes_system=False),
    "page": ActionSpec("reversible", changes_system=False),
    "restart": ActionSpec("reversible", changes_system=True),
    "rollback": ActionSpec("destructive", changes_system=True),
}
# a <- b <- c: a failure of a can break b and c. d stands alone.
RADIUS = {"a": frozenset("abc"), "b": frozenset("bc"), "c": frozenset("c"), "d": frozenset("d")}


@dataclass
class FakePort:
    """Declares four actions; a rollback's preconditions fail when `failing` says so."""

    failing: tuple[str, ...] = ()
    fallback: str = "page"
    checked: list[str] = field(default_factory=list)

    def spec(self, action: str) -> ActionSpec | None:
        return SPECS.get(action)

    def evidence(self, proposal: Proposal) -> Evidence:
        self.checked.append(proposal.target)
        return Evidence(self.failing, ("fact about " + proposal.target,))

    def blast_radius(self, target: str) -> frozenset[str]:
        return RADIUS[target]


def p(action: str, target: str = "a", proposer: str = "agent-1", params: Mapping[str, str] | None = None) -> Proposal:
    return Proposal("inc-1", proposer, action, target, params or {})


def test_an_undeclared_action_is_rejected_and_handed_to_the_fallback() -> None:
    v = ActionLedger(FakePort()).propose(p("delete-database"))
    assert (v.outcome, v.action) == ("rejected", "page")
    assert "not a declared action" in v.reasons[0]


def test_read_and_notify_actions_pass_and_hold_nothing() -> None:
    ledger = ActionLedger(FakePort())
    assert [ledger.propose(p(a)).outcome for a in ("look", "page", "look")] == ["allowed"] * 3
    assert ledger.propose(p("rollback")).outcome == "allowed"  # nothing was held


def test_a_destructive_action_whose_preconditions_fail_is_downgraded_with_its_facts() -> None:
    v = ActionLedger(FakePort(failing=("two changes in the window",))).propose(p("rollback"))
    assert (v.outcome, v.action, v.reasons) == ("downgraded", "page", ("two changes in the window",))
    assert v.facts == ("fact about a",)


def test_preconditions_are_checked_only_for_destructive_actions() -> None:
    port = FakePort(failing=("never",))
    ledger = ActionLedger(port)
    ledger.propose(p("restart", "d"))
    assert port.checked == []


def test_overlapping_blast_radii_never_hold_together() -> None:
    """A restart of b holds b and c; a rollback of a would touch both, so it waits."""
    ledger = ActionLedger(FakePort())
    first = ledger.propose(p("restart", "b", "agent-1"))
    second = ledger.propose(p("rollback", "a", "agent-2"))
    assert first.outcome == "allowed" and second.outcome == "queued"
    assert "restart on b by agent-1" in second.reasons[0] and "b, c" in second.reasons[0]
    assert ledger.propose(p("restart", "d", "agent-3")).outcome == "allowed"  # disjoint: no wait


def test_one_destructive_action_in_flight_even_on_disjoint_radii() -> None:
    ledger = ActionLedger(FakePort())
    assert ledger.propose(p("rollback", "d", "agent-1")).outcome == "allowed"
    v = ledger.propose(p("rollback", "c", "agent-2"))
    assert v.outcome == "queued" and "one destructive action at a time" in v.reasons[0]


def test_release_frees_exactly_the_allowed_proposal() -> None:
    ledger = ActionLedger(FakePort())
    allowed = ledger.propose(p("rollback", "a")).proposal
    twin = p("rollback", "a")  # equal fields, a different proposal
    ledger.release(twin)
    assert ledger.propose(p("restart", "c")).outcome == "queued"
    ledger.release(allowed)
    assert ledger.propose(p("restart", "c")).outcome == "allowed"


def test_every_verdict_is_reported_in_order() -> None:
    seen: list[Verdict] = []
    ledger = ActionLedger(FakePort(), on_verdict=seen.append)
    ledger.propose(p("look"))
    ledger.propose(p("nope"))
    assert [(v.proposal.action, v.outcome) for v in seen] == [("look", "allowed"), ("nope", "rejected")]


def test_concurrent_proposers_never_both_get_an_overlapping_radius() -> None:
    """Twenty agents race for rollbacks on overlapping services: exactly one wins."""
    ledger = ActionLedger(FakePort())
    outcomes: list[str] = []
    start = threading.Barrier(20)

    def go(i: int) -> None:
        start.wait()
        outcomes.append(ledger.propose(p("rollback", "abc"[i % 3], f"agent-{i}")).outcome)

    threads = [threading.Thread(target=go, args=(i,)) for i in range(20)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert outcomes.count("allowed") == 1 and outcomes.count("queued") == 19


def test_a_proposal_cannot_change_after_it_was_judged() -> None:
    params = {"version": "1.0"}
    proposal = p("rollback", params=params)
    params["version"] = "9.9"
    assert proposal.params["version"] == "1.0"


def test_verdicts_are_numbered_in_the_order_decided() -> None:
    """Reports can arrive out of order across threads; `seq` is the order the ledger decided."""
    ledger = ActionLedger(FakePort())
    assert [ledger.propose(p(a)).seq for a in ("look", "page", "nope")] == [1, 2, 3]


def test_the_apps_facts_are_checked_outside_the_lock() -> None:
    """A slow fact source must not stall the incident: evidence runs before the lock is taken."""
    ledger = ActionLedger(FakePort())
    seen: list[bool] = []

    class Probe(FakePort):
        def evidence(self, proposal: Proposal) -> Evidence:
            seen.append(ledger._lock.locked())
            return Evidence()

    ledger._port = Probe()
    ledger.propose(p("rollback"))
    assert seen == [False]
