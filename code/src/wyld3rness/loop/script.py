"""A decider with no model: fixed sets of actions, tried in a fixed order. The baseline a model must beat.

    try 1 ──► plan[0](observation)      e.g. {backoff on}
    try 2 ──► plan[1](observation)      e.g. {cap the busiest key}
    try 3 ──► plan[2](observation)      e.g. {cap the busiest key, backoff on}

Why a model needs this baseline: when every kind of incident has a fix among a
few sets that can be undone, the loop alone reaches it by trying each in turn.
A model then earns its place only by choosing the right set sooner, or by
handling what no set covers. Comparing the model loop with a single model try
would credit the model for what the retries did.

Each step of the plan reads the observation, so a set can name what it acts on
(the busiest key now) without knowing the answer. The app writes the steps; the
order is the caller's (an experiment draws it per incident, so no order is
favoured). It runs through the same loop, policy and signer as a model
decider, so the two differ only in who proposes. It makes no model call, so it
has no budget. Standard library only.
"""

from __future__ import annotations

from collections.abc import Callable, Sequence

from wyld3rness.loop.env import Action
from wyld3rness.loop.run import Try

Step = Callable[[str], Sequence[Action]]


class Scripted:
    """A `Decider` that proposes `plan[k](observation)` on try k + 1, whatever happened before."""

    def __init__(self, plan: Sequence[Step]) -> None:
        """`plan` is one step per try, in the order to try them."""
        if not plan:
            raise ValueError("a script needs at least one step")
        self.plan = tuple(plan)

    def decide(self, observation: str, history: Sequence[Try]) -> list[Action]:
        """The next step's actions. Past the last step it raises, which the loop ends as a decider failure."""
        if len(history) >= len(self.plan):
            raise IndexError(f"the script has {len(self.plan)} steps and try {len(history) + 1} was asked")
        return list(self.plan[len(history)](observation))
