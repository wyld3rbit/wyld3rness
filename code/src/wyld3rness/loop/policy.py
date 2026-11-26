"""The policy: which kinds of action may run, alone or together, and whether their preconditions hold now.

    decision ─► every action's kind in the table? ─► a kind that must be alone, alone?
             ─► each precondition, read from live state ─► allowed, or the reasons it is not

An app declares its policy as data: one `Rule` per kind it allows. Anything not
in the table is refused. A precondition is a function the app supplies that
reads the system now (FindyFixy's reads the gateway's busiest keys) and returns
a reason to refuse, or None. A precondition that raises refuses too: a check
that cannot run is not a pass.

The whole decision is checked before its first action is applied, so a refused
decision changes nothing. The window between this check and the apply is the
time to send the first request, milliseconds here; the credential (Day 7,
step 4b) is checked again by the service that executes the action. Undos are
not checked: they put back the state from before an action this policy allowed.

Why data and plain Python and not a policy engine (Cedar, OPA): three rules, all
of them tested with fakes, do not justify a new language or a container. The
table can move to an engine behind the same `check` when policies need review
by people who do not read Python (ADR 0008, Day 7 decisions).
"""

from __future__ import annotations

from collections.abc import Callable, Sequence
from dataclasses import dataclass

from wyld3rness.loop.env import Action

Precondition = Callable[[Action], str | None]  # the reason to refuse, or None


@dataclass(frozen=True)
class Rule:
    """One allowed kind of action: whether it must be alone in its decision, its precondition, and a note.

    The note says the rule in a sentence, so a prompt can state it from the
    same table the check enforces.
    """

    kind: str
    alone: bool = False
    precondition: Precondition | None = None
    note: str = ""


class Policy:
    """The table of allowed kinds, checked against a whole decision. Refuses whatever it does not name."""

    def __init__(self, rules: Sequence[Rule]) -> None:
        """One rule per kind; a kind named twice is a mistake in the table and fails loudly."""
        kinds = [r.kind for r in rules]
        if len(set(kinds)) != len(kinds):
            raise ValueError(f"a kind is named twice in the policy: {sorted(kinds)}")
        self._rules = {r.kind: r for r in rules}

    def kinds(self) -> list[str]:
        """The allowed kinds, sorted, for prompts and records."""
        return sorted(self._rules)

    def notes(self) -> list[str]:
        """Each rule's note, in kind order, skipping rules without one."""
        return [self._rules[k].note for k in self.kinds() if self._rules[k].note]

    def check(self, decision: Sequence[Action]) -> list[str]:
        """Every reason the decision is refused; empty when it is allowed.

        All reasons are collected, not only the first, so the next try is told
        everything that was wrong with this one.
        """
        reasons = []
        for action in decision:
            rule = self._rules.get(action.name)
            if rule is None:
                reasons.append(f"{action.name}: not an allowed kind of action")
                continue
            if rule.alone and len(decision) > 1:
                reasons.append(f"{action.name}: must be the only action in its decision")
            if rule.precondition is not None:
                try:
                    refusal = rule.precondition(action)
                except Exception as exc:  # a precondition that cannot be checked is not met
                    refusal = f"its precondition could not be checked: {type(exc).__name__}: {exc}"
                if refusal:
                    reasons.append(f"{action.label()}: {refusal}")
        return reasons
