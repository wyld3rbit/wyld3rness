"""The action ledger: one deterministic door between every proposed action and any system.

What it does
    An app's agents propose actions. The ledger decides each one, in code,
    and says why: ALLOWED, DOWNGRADED to the app's safe fallback (paging a
    person), REJECTED (an action the app never declared, also handed to the
    fallback) or QUEUED (allowed, but it must wait for an action already
    holding what it would touch). It never executes anything; execution, and
    releasing what an action holds, come with approval (Day 9).

Why the harness owns it, and the app supplies the facts
    Whether an action may run is the same question for every app: is it
    declared, what class is it, do its preconditions hold, does it collide with
    an action already in flight. Only the facts are the app's: which actions
    exist, what a rollback needs to be safe, what a service's failure can
    break. So the ledger is here and the app implements `ActionPort`. A second
    app writes a port and changes nothing in the harness.

What the model can and cannot influence
    The ledger reads the proposal's fields, never its prose, and the app checks
    a destructive proposal against facts from its own systems (for FindyFixy,
    the alert and the change log). The fields themselves are the model's: a
    fooled model chooses the action, its target and its parameters. So the
    app's preconditions must bound each field by a fact the model cannot
    write (FindyFixy ties a rollback's target to the alerting service and what
    it depends on), and whatever still passes waits for a person's approval
    until its class earns more (Day 9, Day 10).

Many proposers
    One ledger per incident, shared by every agent working on it, and safe to
    call from several threads. An allowed action that changes a system holds
    its blast radius (its target and everything that depends on it) until it
    is released, so two actions that could interfere never run together. At
    most one destructive action is in flight per incident, whatever its
    radius: a second one waits even when the two look independent, because
    two concurrent fixes make it impossible to tell which one worked.

What changes at scale
    The locks live in this process. Several workers on one incident would need
    them in a shared store with leases (a row lock, or a lease in a
    coordination service), so a crashed worker cannot hold a radius forever.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping
from dataclasses import dataclass, field, replace
import threading
from types import MappingProxyType
from typing import Literal, Protocol

ActionClass = Literal["read", "reversible", "destructive"]
Outcome = Literal["allowed", "downgraded", "rejected", "queued"]


@dataclass(frozen=True)
class ActionSpec:
    """What the app says about one of its actions.

    `changes_system` is False for an action that only reads or notifies (look
    at logs, page a person), so it takes no lock; True for one that changes a
    running system (a rollback, a restart), which holds its blast radius.
    """

    action_class: ActionClass
    changes_system: bool


@dataclass(frozen=True, eq=False)
class Proposal:
    """One action an agent proposes, as fields the ledger can check.

    Compared by identity, not by value: two agents proposing the same rollback
    are two proposals, and `release` frees exactly the one that was allowed.

    `proposer` names the agent, so a trace shows who asked for what when
    several agents work one incident. `target` is what the action acts on (a
    service). `params` are the action's own fields (for a rollback, the
    version and the severity the agent's validated record gave), read by the
    app's preconditions.
    """

    incident: str
    proposer: str
    action: str
    target: str
    params: Mapping[str, str] = field(default_factory=dict)

    def __post_init__(self) -> None:
        """Freeze `params`, so a proposal cannot change after it was judged."""
        object.__setattr__(self, "params", MappingProxyType(dict(self.params)))


@dataclass(frozen=True)
class Evidence:
    """The app's check of a proposal's preconditions.

    `failures` are the preconditions that did not hold, in words a person can
    act on; empty means they all held. `facts` are what the person should see
    either way (for a rollback, the changes to the service before the alert).
    """

    failures: tuple[str, ...] = ()
    facts: tuple[str, ...] = ()


@dataclass(frozen=True)
class Verdict:
    """What the ledger decided, and why. `action` is what may proceed: the proposal's, or the fallback.

    `seq` numbers the incident's verdicts in the order they were decided,
    taken under the ledger's lock, so a trace can order verdicts from
    concurrent proposers even when their reports arrive out of order.
    """

    proposal: Proposal
    outcome: Outcome
    action: str
    reasons: tuple[str, ...]
    facts: tuple[str, ...] = ()
    seq: int = 0


class ActionPort(Protocol):
    """What an app tells the ledger. Implemented by the app, called only by the ledger."""

    fallback: str  # the declared action a downgrade or rejection becomes (for example, paging a person)

    def spec(self, action: str) -> ActionSpec | None:
        """The action's spec, or None if the app never declared it."""
        ...

    def evidence(self, proposal: Proposal) -> Evidence:
        """Check a destructive proposal's preconditions against the app's own facts."""
        ...

    def blast_radius(self, target: str) -> frozenset[str]:
        """The target and everything whose failure it could cause."""
        ...


class ActionLedger:
    """The ledger of one incident. Nothing runs until `propose`.

    `on_verdict` is called with every verdict, so the caller can trace each
    decision with its reason and proposer. It runs outside the lock, so with
    several proposers the calls can arrive out of order; each verdict's `seq`
    is the order decided.

    The lock covers only the holdings: the app's facts are checked before it
    is taken, so a slow fact source (a change-event service) never stalls the
    other proposers on the incident.
    """

    def __init__(self, port: ActionPort, on_verdict: Callable[[Verdict], None] | None = None) -> None:
        """Hold the app's port. No action is held yet."""
        self._port = port
        self._on_verdict = on_verdict
        self._lock = threading.Lock()
        self._held: dict[Proposal, frozenset[str]] = {}
        self._destructive: Proposal | None = None
        self._seq = 0

    def propose(self, proposal: Proposal) -> Verdict:
        """Decide one proposal. The same proposal and holdings give the same outcome."""
        spec = self._port.spec(proposal.action)
        evidence = (
            self._port.evidence(proposal) if spec is not None and spec.action_class == "destructive" else Evidence()
        )
        with self._lock:
            self._seq += 1
            verdict = replace(self._decide(proposal, spec, evidence), seq=self._seq)
        if self._on_verdict is not None:
            self._on_verdict(verdict)
        return verdict

    def action_class(self, action: str) -> ActionClass | None:
        """The class the app declared for `action`, or None if it never declared it."""
        spec = self._port.spec(action)
        return spec.action_class if spec is not None else None

    def release(self, proposal: Proposal) -> None:
        """Free what an allowed action holds, once it has run and been checked. A no-op for one that holds nothing."""
        with self._lock:
            self._held.pop(proposal, None)
            if self._destructive == proposal:
                self._destructive = None

    def _decide(self, proposal: Proposal, spec: ActionSpec | None, evidence: Evidence) -> Verdict:
        """The rules, in order. The caller holds the lock; `evidence` was checked before it."""
        fallback = self._port.fallback
        if spec is None:
            return Verdict(proposal, "rejected", fallback, (f"{proposal.action!r} is not a declared action",))
        if evidence.failures:
            return Verdict(proposal, "downgraded", fallback, evidence.failures, evidence.facts)
        if not spec.changes_system:
            return Verdict(proposal, "allowed", proposal.action, (f"{spec.action_class} action",), evidence.facts)
        radius = self._port.blast_radius(proposal.target)
        for other, held in self._held.items():
            if held & radius:
                overlap = ", ".join(sorted(held & radius))
                reason = f"waits for {other.action} on {other.target} by {other.proposer}: both touch {overlap}"
                return Verdict(proposal, "queued", proposal.action, (reason,), evidence.facts)
        if spec.action_class == "destructive" and self._destructive is not None:
            other = self._destructive
            reason = f"one destructive action at a time: {other.action} on {other.target} is in flight"
            return Verdict(proposal, "queued", proposal.action, (reason,), evidence.facts)
        self._held[proposal] = radius
        if spec.action_class == "destructive":
            self._destructive = proposal
        reasons = ("preconditions hold",) if spec.action_class == "destructive" else (f"{spec.action_class} action",)
        return Verdict(proposal, "allowed", proposal.action, reasons, evidence.facts)
