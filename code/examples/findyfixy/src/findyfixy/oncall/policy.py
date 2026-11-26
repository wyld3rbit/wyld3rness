"""FindyFixy's policy over simprod: the harness's `Policy` table, with one precondition read from live metrics.

    kind      rule                                                   why (research log, entry 013)
    backoff   allowed
    rate_cap  only on a key sending ≥ SHARE of the customers'        the model capped an innocent key in
              rate now (gateway's busiest-keys gauge, 10 s)          3 of 3 retry storms, and the loop kept it
    restart   only alone in its decision                             a gateway restart cancelled the cap beside
                                                                     it in 3 of 3 hot shards

Why 5%: the threshold is compared with the customers' configured rate R (the
`customers_rate` gauge), which leaves out a runaway client, so it does not move
when a runaway adds load. At R = 200 a second it is 10 a second: about two
orders of magnitude below the runaways in entry 013 and above the innocent keys
the model capped there, whose rates are in that entry. A key outside the
gateway's 5 busiest is refused, because its rate is not published and an
unknown rate is not a pass.

The metrics come through the edge, as the decider's do. They are written by
services the agent's actions affect, so the precondition reads them at the last
moment, after the decision is made and before any action of it is applied.
"""

from __future__ import annotations

from collections.abc import Callable
import re

from findyfixy.oncall.env import fetch_text
from simprod.controls import EDGE
from wyld3rness.loop.env import Action
from wyld3rness.loop.policy import Rule

SHARE = 0.05  # a cap only on a key sending at least this share of the customers' rate R now
_TOP = re.compile(r'^gateway_top_keys_rps\{key="([^"]+)"\}\s+(\S+)$', re.M)
_RATE = re.compile(r"^customers_rate\s+(\S+)$", re.M)


def top_keys(metrics_text: str) -> dict[str, float]:
    """The gateway's busiest keys and their requests a second, from its /metrics. Pure."""
    return {key: float(value) for key, value in _TOP.findall(metrics_text)}


def customer_rate(metrics_text: str) -> float | None:
    """The customers' mean requests a second, from the customers service's /metrics. Pure. None when absent."""
    found = _RATE.search(metrics_text)
    return float(found.group(1)) if found else None


def cap_refusal(key: str, top: dict[str, float], rate: float | None, share: float = SHARE) -> str | None:
    """Why a cap on `key` is refused now, and what would pass; None when the key sends at least `share`. Pure.

    The reason names the keys that qualify now, or says that none does, because
    the next try reads it: in research log entry 014 a small model told only
    that its key was not among the busiest proposed another unlisted key, three
    times, and lost the correct action beside it each time.
    """
    if rate is None or rate <= 0:
        return "the customers' request rate could not be read"
    floor = share * rate
    sent = top.get(key)
    if sent is None:
        why = f"{key} is not among the {len(top)} busiest keys"
    elif sent < floor:
        why = f"{key} sends {sent:.1f}/s, under {share:.0%} of the {rate:.0f}/s customers send"
    else:
        return None
    qualifying = [f"{k} ({v:.1f}/s)" for k, v in sorted(top.items(), key=lambda kv: -kv[1]) if v >= floor]
    if qualifying:
        return f"{why}; keys that qualify now: {', '.join(qualifying)}"
    return f"{why}; no key sends {floor:.0f}/s or more now, so no rate_cap can be allowed"


def rules(get: Callable[[str], str] = fetch_text, edge: str = EDGE) -> list[Rule]:
    """FindyFixy's three rules. `get` reads a URL at `edge`, simprod's edge as seen from here; tests pass a fake."""

    def cap_on_the_cause(action: Action) -> str | None:
        """The cap's precondition: its key must be a cause of the load now."""
        top = top_keys(get(f"{edge}/gateway/metrics"))
        rate = customer_rate(get(f"{edge}/customers/metrics"))
        return cap_refusal(str(action.args.get("key")), top, rate)

    return [
        Rule("backoff"),
        Rule(
            "rate_cap",
            precondition=cap_on_the_cause,
            note=f"A rate_cap is refused unless its key sends at least {SHARE:.0%} of the customers' request rate now.",
        ),
        Rule("restart", alone=True, note="A restart is refused unless it is the only action in its decision."),
    ]
