"""The budget: each model call made through it reserves its worst case first; a call that does not fit is never made.

    reserve(model, prompt, output cap)
        worst case = (UTF-8 bytes of the prompt + template tokens) x input price + output cap x output price
        ├─ spent + held + worst case ≤ limit ─► a Reservation; the call may run
        └─ over the limit, or no price for the model ─► BudgetExceeded: the loop ends in the safe state
    settle(reservation, input tokens, output tokens) ─► the real cost replaces the reservation

Why the bytes bound the tokens: byte-level tokenizers (Qwen's among them) never
make more tokens than a text has bytes, so the bound is an upper bound by
construction, with nothing to install and nothing to keep in step with the
model server. It overestimates, by about three times on the decider's prompts
(research log, entry 014). The chat template adds a few tokens of its own, and
`template_tokens` allows twice what entry 014 measured. A
tokenizer that is not byte-level breaks the bound; this one is the reason a
model needs a price entry before it can be called at all. The limit holds only
while both bounds hold: `settle` charges the tokens the reply reports, with no
cap, so a model server that ignores the output cap can spend past it.

Why a reservation and not a report: cost read from a trace is known after the
money is gone. An unknown price refuses the call, and unknown token counts in
the reply settle at the reservation, never at zero. Only the calls made
through this port are budgeted; a model gateway in front of every call, planned,
would take it over with the same rule.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass, field

from wyld3rness.trace.prices import ModelPrice

TEMPLATE_TOKENS = 16  # twice the chat template's own tokens, as measured in research log entry 014


class BudgetExceeded(Exception):
    """A call was refused before it ran: over the incident's limit, or no price for its model."""


@dataclass(frozen=True)
class Reservation:
    """The worst case held for one call until it settles."""

    model: str
    usd: float


@dataclass(frozen=True)
class Charge:
    """One settled call: what was reserved, what it cost, and the tokens it reported (None when not reported)."""

    model: str
    reserved_usd: float
    cost_usd: float
    input_tokens: int | None
    output_tokens: int | None


@dataclass
class Budget:
    """One incident's model budget, in US dollars at the reference prices. Single-threaded, like the loop."""

    limit_usd: float
    prices: Sequence[ModelPrice]
    template_tokens: int = TEMPLATE_TOKENS
    held_usd: float = 0.0
    charges: list[Charge] = field(default_factory=list)

    @property
    def spent_usd(self) -> float:
        """The settled cost so far."""
        return sum(c.cost_usd for c in self.charges)

    def _price(self, model: str) -> ModelPrice:
        """The model's price; matched case-insensitively, as the price table defines names."""
        for p in self.prices:
            if p.name.casefold() == model.casefold():
                return p
        raise BudgetExceeded(f"no reference price for {model}: an unpriced call is refused")

    def worst_case(self, model: str, prompt: str, max_output_tokens: int) -> float:
        """The most this call can cost. Pure, given the price table."""
        p = self._price(model)
        input_bound = len(prompt.encode()) + self.template_tokens
        return input_bound * p.input_per_token + max_output_tokens * p.output_per_token

    def reserve(self, model: str, prompt: str, max_output_tokens: int) -> Reservation:
        """Hold the call's worst case, or raise `BudgetExceeded` if it does not fit what is left."""
        cost = self.worst_case(model, prompt, max_output_tokens)
        left = self.limit_usd - self.spent_usd - self.held_usd
        if cost > left:
            raise BudgetExceeded(
                f"{model}: worst case ${cost:.6f} does not fit the ${left:.6f} left of ${self.limit_usd}"
            )
        self.held_usd += cost
        return Reservation(model, cost)

    def settle(self, reservation: Reservation, input_tokens: int | None, output_tokens: int | None) -> Charge:
        """Replace the reservation with the call's real cost; unknown counts are charged the reservation."""
        p = self._price(reservation.model)
        if input_tokens is None or output_tokens is None:
            cost = reservation.usd
        else:
            cost = input_tokens * p.input_per_token + output_tokens * p.output_per_token
        self.held_usd -= reservation.usd
        charge = Charge(reservation.model, reservation.usd, cost, input_tokens, output_tokens)
        self.charges.append(charge)
        return charge
