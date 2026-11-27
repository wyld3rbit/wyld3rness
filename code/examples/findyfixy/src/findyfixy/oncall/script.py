"""FindyFixy's script with no model: the three fix sets a person would try first, in a given order.

    backoff  {backoff on}
    cap      {cap the busiest key at R / 20}            the key read from the observation now
    both     {cap the busiest key at R / 20, backoff on}

These are the sets that end simprod's three storm kinds (simprod/faults.py), so a
script that tries all three reaches the fix of every storm within its three
tries. It reads only what the model reads (the observation), never the storm's
answer: the busiest key comes from the gateway's busiest-keys gauge and R from the
customers' rate gauge. The policy then judges it like any proposal; in a retry
storm no key sends 5% of R, so both sets with a cap are refused and cost a try
each, but no time.

Runs in the loop container, which builds it from the order the experiment sends
(`plan`). Standard library and the harness only.
"""

from __future__ import annotations

from collections.abc import Callable, Sequence

from findyfixy.oncall.policy import customer_rate, top_keys
from wyld3rness.loop.env import Action
from wyld3rness.loop.script import Scripted

BACKOFF_ON = Action("backoff", {"enabled": True})
CAP_SHARE = 1 / 20  # the cap that ends a runaway (simprod.faults.cap_rps): R / 20
STEPS = ("backoff", "cap", "both")


def busiest_cap(observation: str) -> list[Action]:
    """A cap at R / 20 on the busiest key in the observation; nothing when the key or R is not there. Pure.

    An empty proposal is a decider failure in the loop, so a missing gauge ends
    the incident in the safe state instead of capping a guessed key.
    """
    top, rate = top_keys(observation), customer_rate(observation)
    if not top or not rate:
        return []
    key = max(top, key=lambda k: top[k])
    return [Action("rate_cap", {"key": key, "rps": round(rate * CAP_SHARE, 2)})]


def _step(name: str) -> Callable[[str], list[Action]]:
    """The step called `name`. Raises ValueError for an unknown name."""
    match name:
        case "backoff":
            return lambda observation: [BACKOFF_ON]
        case "cap":
            return busiest_cap
        case "both":  # never shrinks to backoff alone: that would be another step
            return lambda observation: [*cap, BACKOFF_ON] if (cap := busiest_cap(observation)) else []
    raise ValueError(f"unknown script step {name!r}, expected one of {STEPS}")


def plan(order: Sequence[str]) -> Scripted:
    """The script that tries the named steps in `order`, one per try."""
    return Scripted([_step(name) for name in order])
