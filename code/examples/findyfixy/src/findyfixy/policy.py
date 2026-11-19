"""FindyFixy's side of the action ledger: its actions, the facts a rollback needs, and each service's blast radius.

What it does
    `FindyFixyActions` implements the harness's `ActionPort`
    (wyld3rness/action/ledger.py). `make_judge` builds, per incident, the
    function the graph's policy step calls: it turns the agent's validated
    `Decision` into a `Proposal`, asks the incident's ledger, and returns the
    `Verdict`.

The declared actions
    read        look further (logs, metrics, changes); changes nothing.
    page_human  hand the incident to a person; reversible, changes nothing.
                Also the fallback for every downgrade and rejection.
    rollback    undo a deploy; destructive, changes a running system.

The rollback rule (decision C2, with rollback safety)
    A rollback passes only when every fact below holds. None is read from the
    model's prose:
    - the target is the service the alert fired on, or one it depends on
      (data/services.json): a fooled model cannot roll back an unrelated
      service;
    - exactly one change to the target in the 2 hours before the alert
      (the same window function as the agent's change tool, tools/changes.py);
    - that change is a deploy, marked safe to roll back;
    - the version to roll back to is the target's deploy before that one;
    - the validated record's severity is sev1 or sev2.
    The alert (`AlertFacts`) and the change log are facts the model cannot
    write. The target and the severity are the model's validated fields, so
    the rule bounds them by those facts rather than trusting them: within the
    alert's dependency chain, a fooled model can still name the wrong service
    or overstate severity, and a rollback it gets through still waits for a
    person's approval (Day 9). A rollback that fails any check becomes a page
    to a person, with every change in the window listed.

Failing closed
    Data v1 and v2 have no change log and no alert facts, so no rollback can be
    verified and every one is downgraded. No facts, no destructive action.

Blast radius
    A service and every service that depends on it, directly or through
    others, from data/services.json. A rollback of auth holds auth, gateway,
    search, billing and voice-verification.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping, Sequence
from pathlib import Path
from typing import cast

from findyfixy.tools.changes import Change, changes_before, minutes_before
from findyfixy.tools.deploys import Service
from findyfixy.tools.services import DEFAULT_SERVICES_PATH, load_map
from findyfixy.triage.record import AlertFacts, Decision
from wyld3rness.action.ledger import ActionLedger, ActionSpec, Evidence, Proposal, Verdict

# The window a rollback's deploy must sit in, before the alert.
ROLLBACK_WINDOW_HOURS = 2.0
SEVERE = frozenset({"sev1", "sev2"})
FALLBACK = "page_human"

_SPECS: Mapping[str, ActionSpec] = {
    "read": ActionSpec("read", changes_system=False),
    "page_human": ActionSpec("reversible", changes_system=False),
    "rollback": ActionSpec("destructive", changes_system=True),
}


def dependents(upstream: Mapping[str, Sequence[str]]) -> dict[str, frozenset[str]]:
    """For each service, every service that depends on it, directly or not. Pure.

    The map lists what each service depends on; a blast radius needs the
    reverse, followed to the end.
    """
    direct: dict[str, set[str]] = {s: set() for s in upstream}
    for service, needs in upstream.items():
        for need in needs:
            direct[need].add(service)
    out: dict[str, frozenset[str]] = {}
    for service in upstream:
        seen: set[str] = set()
        todo = list(direct[service])
        while todo:
            nxt = todo.pop()
            if nxt not in seen:
                seen.add(nxt)
                todo.extend(direct[nxt])
        out[service] = frozenset(seen)
    return out


def upstream_of(upstream: Mapping[str, Sequence[str]], service: str) -> frozenset[str]:
    """Every service `service` depends on, directly or through others. Pure."""
    seen: set[str] = set()
    todo = list(upstream.get(service, ()))
    while todo:
        nxt = todo.pop()
        if nxt not in seen:
            seen.add(nxt)
            todo.extend(upstream.get(nxt, ()))
    return frozenset(seen)


def describe(change: Change, alert: AlertFacts) -> str:
    """One change as a person reads it: kind, what, when, and whether it is safe to undo."""
    minutes = minutes_before(change, alert.at)
    safe = ""
    if change.rollback_safe is not None:
        safe = ", safe to roll back" if change.rollback_safe else ", NOT safe to roll back"
    return f"{change.kind} {change.ref} on {change.service}, {minutes} min before the alert ({change.summary}){safe}"


class FindyFixyActions:
    """The port. `changes` and `alert` are the incident's facts; None for data without them."""

    fallback = FALLBACK

    def __init__(
        self,
        changes: Sequence[Change] | None,
        alert: AlertFacts | None,
        services_path: Path = DEFAULT_SERVICES_PATH,
    ) -> None:
        """Hold the incident's facts and the service map, both ways."""
        self._changes = changes
        self._alert = alert
        self._upstream = load_map(services_path)
        self._dependents = dependents(self._upstream)

    def spec(self, action: str) -> ActionSpec | None:
        """The declared action's spec; None for anything else."""
        return _SPECS.get(action)

    def blast_radius(self, target: str) -> frozenset[str]:
        """The service and every service that depends on it. An unknown target holds only itself."""
        return frozenset({target}) | self._dependents.get(target, frozenset())

    def evidence(self, proposal: Proposal) -> Evidence:
        """Check the rollback rule. Every failure is stated; the window's changes are always listed."""
        failures: list[str] = []
        severity = proposal.params.get("severity", "")
        if severity not in SEVERE:
            failures.append(f"severity {severity or 'missing'}: a rollback needs sev1 or sev2")
        if proposal.target not in self._dependents:
            return Evidence((*failures, f"{proposal.target!r} is not a known service"))
        if self._changes is None or self._alert is None:
            return Evidence((*failures, "no change log for this incident: the rollback cannot be verified"))
        alerting = self._alert.service
        if proposal.target != alerting and proposal.target not in upstream_of(self._upstream, alerting):
            failures.append(f"{proposal.target} is neither the alerting service ({alerting}) nor one it depends on")
        service = cast("Service", proposal.target)  # checked above: a key of the service map
        window = changes_before(self._changes, service, self._alert.at, ROLLBACK_WINDOW_HOURS)
        facts = tuple(describe(c, self._alert) for c in window)
        if len(window) != 1:
            failures.append(
                f"{len(window)} changes to {proposal.target} in the {ROLLBACK_WINDOW_HOURS:g} hours before the alert: "
                "a rollback needs exactly one"
            )
        else:
            (change,) = window
            if change.kind != "deploy":
                failures.append(f"the only change is a {change.kind}, not a deploy")
            elif not change.rollback_safe:
                failures.append(f"deploy {change.ref} is not marked safe to roll back")
            else:
                before = [
                    c for c in self._changes if c.service == proposal.target and c.kind == "deploy" and c.at < change.at
                ]
                previous = max(before, key=lambda c: c.at).ref if before else None
                wanted = proposal.params.get("version")
                if wanted != previous:
                    failures.append(f"rollback to {wanted}, but the version before {change.ref} is {previous}")
        return Evidence(tuple(failures), facts)


def make_judge(
    incident: str,
    port: FindyFixyActions,
    on_verdict: Callable[[Verdict], None] | None = None,
    proposer: str = "agent",
) -> Callable[[Decision], Verdict]:
    """The policy step's function for one incident: a decision in, the ledger's verdict out.

    One ledger per incident, so every proposal on it, from any agent, meets
    the same locks. `proposer` names the agent; Day 8's team passes each
    specialist's and the coordinator's own name.
    """
    ledger = ActionLedger(port, on_verdict)

    def judge(decision: Decision) -> Verdict:
        """Turn the decision's fields into a proposal and ask the ledger. The prose is never read."""
        params: dict[str, str] = {"severity": decision.severity}
        if decision.rollback_version is not None:
            params["version"] = decision.rollback_version
        # The record's component is free text; a service name is matched case-blind.
        target = decision.component.strip().lower()
        return ledger.propose(Proposal(incident, proposer, decision.action, target, params))

    return judge
