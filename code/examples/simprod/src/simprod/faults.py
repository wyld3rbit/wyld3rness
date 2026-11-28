"""The three faults, versioned and seeded, and the driver that injects them from inside the network.

    host ──► docker compose run --rm chaos python -m simprod.faults inject '<storm json>'
                                   │ prod network only
                                   ├─► ledger:9000/fault     retry storm: a slowdown that ends by itself
                                   └─► customers:9000/fault  hot shard, compound: a runaway client on one key

A storm is a fault plus the gateway it meets. The gateway is deployed either as
shipped (immediate retries) or well configured (GATEWAY_BACKOFF=1: backoff with
jitter and a retry budget). Each kind has one correct fix, a set of actions
applied together:

    kind         gateway      injected                         fix                  wrong fix
    retry_storm  as shipped   every ledger write slower,       backoff on           cap the decoy
                              EXTRA ms for TRIGGER s
    hot_shard    backs off    one key at RPS on top of its     cap that key         cap the decoy
                              customers, until cleared
    compound     as shipped   the same runaway                 cap that key and     backoff on and
                                                               backoff on           cap the decoy

Why each lasts: a retry storm, because immediate retries keep the ledger over
capacity after the slowdown ends; a hot shard, because the runaway never stops
by itself; a compound, because of the runaway and that shard's own customers
retrying at once.

Why hot_shard and compound are two kinds: the same fault, a different system,
a different fix. With retries firing at once, a shard's own customers (about
R / 4 a second, each sent up to 4 times) keep a shard that serves R / 2 over
capacity after the runaway is capped, so the outage becomes a retry storm
confined to one shard, and neither action alone ends it. On a gateway that backs
off, the cap alone does. The exploratory runs that showed this are in research
log entry 008.

The decoy is another customer's key, drawn from the seed. Every wrong fix caps
it, so a wrong fix has a real target, as an operator who misread the busiest
keys would pick, and its undo can be checked.

Restart is neither a fix nor a wrong fix here. Restarting the ledger empties its
queue, which can end a retry storm whose trigger has passed, but it cannot be
undone and leaves the cause (immediate retries) in place. It cannot end a
runaway, whose cause is outside the ledger. A gateway restart returns it to its
deployed settings.

Storms are drawn from a seed, so a storm set is reproducible and a held-out set
(step 6) is a different seed never used in tuning. FAULTS_VERSION changes when
any formula or kind below changes, and every result records it. Version 1 had
two kinds and single-action fixes.

The numbers are relative to the operating rate R (research log, entries 006
and 007). At R the ledger has 4 shards, each serving R / 2 writes a second (one
slot, 2000 / R ms a write), and each shard gets about R / 4 of the customers:

- Retry storm: EXTRA is 2 to 5 service times, so a shard serves R / 6 to R / 12
  writes a second while the slowdown lasts, under the R / 4 arriving. The line
  fills to the queue limit, every wait passes the gateway's 500 ms timeout, and
  each customer becomes up to 4 writes: R a second against R / 2 of capacity.
- Hot shard and compound: RPS is 0.5 R to 0.75 R on one key, so its shard gets
  0.75 R to R against R / 2. A cap of R / 20 brings it to 0.3 R.
"""

from __future__ import annotations

import argparse
from dataclasses import asdict, dataclass
import json
import random
import sys
from typing import Literal

from simprod.loadgen import KEYS
from simprod.service import Upstream

FAULTS_VERSION = 2
HOT_SHARD_SECONDS = 3600.0  # longer than any storm: the runaway does not stop by itself
Kind = Literal["retry_storm", "hot_shard", "compound"]
KINDS: tuple[Kind, ...] = ("retry_storm", "hot_shard", "compound")
Action = tuple[str, dict[str, object]]  # (path on the edge, body)

BACKOFF_ON: Action = ("/gateway/actions/backoff", {"enabled": True})
BACKOFF_OFF: Action = ("/gateway/actions/backoff", {"enabled": False})


def cap(key: str, rps: float | None) -> Action:
    """A rate cap on `key`, or its removal (rps None)."""
    return "/gateway/actions/rate-cap", {"key": key, "rps": rps}


@dataclass(frozen=True)
class Storm:
    """One seeded storm. Fields a kind does not use stay at zero or empty."""

    id: str
    kind: Kind
    seed: int
    decoy: str  # another customer's key: what every wrong fix caps
    extra_ms: float = 0.0  # retry storm: added to every write
    trigger_s: float = 0.0  # retry storm: how long the slowdown lasts
    key: str = ""  # hot shard, compound: the runaway key
    rps: float = 0.0  # hot shard, compound: the runaway's rate

    @property
    def gateway_backoff(self) -> bool:
        """How the gateway is deployed for this storm: True only for a hot shard."""
        return self.kind == "hot_shard"

    def fix(self, rate: float) -> list[Action]:
        """The actions that end this storm, applied together."""
        if self.kind == "retry_storm":
            return [BACKOFF_ON]
        if self.kind == "hot_shard":
            return [cap(self.key, cap_rps(rate))]
        return [cap(self.key, cap_rps(rate)), BACKOFF_ON]

    def undo_of_fix(self) -> list[Action]:
        """The undo of `fix`, last action first."""
        if self.kind == "retry_storm":
            return [BACKOFF_OFF]
        if self.kind == "hot_shard":
            return [cap(self.key, None)]
        return [BACKOFF_OFF, cap(self.key, None)]

    def wrong(self, rate: float) -> list[Action]:
        """Undoable actions that do not end this storm. For compound, the right
        set aimed at the wrong key: backoff alone does not end it (entry 008)."""
        if self.kind == "compound":
            return [BACKOFF_ON, cap(self.decoy, cap_rps(rate))]
        return [cap(self.decoy, cap_rps(rate))]

    def undo_of_wrong(self) -> list[Action]:
        """The undo of `wrong`, last action first."""
        if self.kind == "compound":
            return [cap(self.decoy, None), BACKOFF_OFF]
        return [cap(self.decoy, None)]


def cap_rps(rate: float) -> float:
    """The rate cap that ends a runaway: R / 20."""
    return round(rate / 20, 2)


def storms(n: int, rate: float, seed: int) -> list[Storm]:
    """`n` storms, the three kinds in turn, parameters drawn from `seed`. Pure."""
    rng = random.Random(seed)
    service_ms = 2000 / rate
    out = []
    for i in range(n):
        kind = KINDS[i % len(KINDS)]
        key, decoy = (f"acct-{k:04d}" for k in rng.sample(range(KEYS), 2))
        sid, sseed = f"s{seed}-{i:02d}", rng.randrange(2**31)
        if kind == "retry_storm":
            s = Storm(
                sid,
                kind,
                sseed,
                decoy,
                extra_ms=round(rng.uniform(2, 5) * service_ms, 1),
                trigger_s=round(rng.uniform(5, 15), 1),
            )
        else:
            s = Storm(sid, kind, sseed, decoy, key=key, rps=round(rng.uniform(0.5, 0.75) * rate, 1))
        out.append(s)
    return out


def inject(storm: Storm, ledger: Upstream, customers: Upstream) -> dict[str, object]:
    """Start the storm's fault. Returns the service's answer: the fault now in force."""
    if storm.kind == "retry_storm":
        status, body = ledger.call(
            "POST",
            "/fault",
            {"kind": "slowdown", "extra_ms": storm.extra_ms, "seconds": storm.trigger_s},
            5.0,
            {"X-Actor": "chaos"},
        )
    else:
        status, body = customers.call(
            "POST",
            "/fault",
            {"kind": "runaway", "key": storm.key, "rps": storm.rps, "seconds": HOT_SHARD_SECONDS},
            5.0,
            {"X-Actor": "chaos"},
        )
    if status != 200:
        raise RuntimeError(f"injecting {storm.id} failed: {status} {body}")
    return body


def clear(ledger: Upstream, customers: Upstream) -> None:
    """End every fault now."""
    for up in (ledger, customers):
        status, body = up.call("POST", "/fault/clear", {}, 5.0, {"X-Actor": "chaos"})
        if status != 200:
            raise RuntimeError(f"clearing faults failed: {status} {body}")


def main(argv: list[str] | None = None) -> int:
    """`inject '<storm json>'` or `clear`, run inside the chaos container on the prod network."""
    parser = argparse.ArgumentParser(prog="faults", description=__doc__.splitlines()[0])
    sub = parser.add_subparsers(dest="cmd", required=True)
    sub.add_parser("inject").add_argument("storm", help="a Storm as JSON")
    sub.add_parser("clear")
    args = parser.parse_args(argv)
    ledger, customers = Upstream("ledger", 9000), Upstream("customers", 9000)
    if args.cmd == "inject":
        storm = Storm(**json.loads(args.storm))
        state = inject(storm, ledger, customers)
        print(json.dumps({"injected": asdict(storm), "state": state, "faults_version": FAULTS_VERSION}))
    else:
        clear(ledger, customers)
        print(json.dumps({"cleared": True}))
    return 0


if __name__ == "__main__":
    sys.exit(main())
