"""`make experiment`: does the model earn its place against a script with no model? (research log, entry 019)

    for each held-out storm, the four arms in an order drawn for that storm:
        nobody       does nothing
        script       {backoff}, {cap the busiest key}, {both}, in an order drawn for that storm, no model
        model-1      the model, one try
        model-loop   the model, up to three tries
    every arm: same storm driver, same loop container, policy, signer, budget; measured over HORIZON_S
    ──► results file after every run ──► `analyze`: primary interval, secondaries, guardrails, the bar

The primary metric is customer minutes lost in the HORIZON_S after the fault,
model loop minus script, paired by storm, with a seeded bootstrap 95% interval.
The bar: that interval entirely below zero, with 0 harm and 0 incidents over
budget. Everything else is secondary and corrected for multiple comparisons, or descriptive.

Why one fixed window: an arm that stops early (the safe state after one try)
would otherwise not be charged for the failures it left running. HORIZON_S is
longer than any arm can act: three tries of WAIT, HOLD and the margin, plus
time to decide.

Why the order is drawn per storm (a string seed, so it is the same on every
machine and Python version): a fixed order would let a slow drift in the VM
favour whichever arm runs first.

`power` prints the power table the protocol's N comes from: a model of one
storm built from the pilot's measurements (entries 012 to 016), repeated
through the harness's simulation (`wyld3rness.loop.experiment.power`).
`analyze FILE` recomputes every number from a saved results file.
"""

from __future__ import annotations

import argparse
from collections.abc import Callable, Mapping, Sequence
from datetime import UTC, datetime
from itertools import combinations, permutations
import json
from pathlib import Path
import random
import statistics
import sys
from typing import Any

from findyfixy.oncall.cli import LIMIT_USD, preflight, run_one
from findyfixy.oncall.script import STEPS
from simprod import controls, loadtest
from simprod.faults import KINDS, Storm, storms
from wyld3rness.eval.stats import bootstrap_ci
from wyld3rness.loop.experiment import exact_paired_p, paired_interval, power, step_down, zero_events_upper
from wyld3rness.loop.run import HOLD_MARGIN_S, TRIES, LoopConfig
from wyld3rness.loop.verdict import Second, customer_minutes

JSON = dict[str, Any]

HELD_OUT_SEED = 13  # never run before entry 019: seeds 3 (pilot, exploration), 7 and 11 (controls) were
STORMS = 24  # 8 of each kind: `power` and the correction's requirement below, entry 019
CONSISTENCY_STORMS = 6  # the first 6 (2 of each kind) run the model loop twice more
REPEATS = 3
HORIZON_S = 240.0  # after the fault: DETECT_S + 3 tries x (WAIT + HOLD + margin) = 201 s, plus time to decide
ARMS = ("nobody", "script", "model-1", "model-loop")
PRIMARY = ("model-loop", "script")
# A run is harm when it loses more than nobody on the same storm by more than this share: the
# widest relative spread of customer minutes within one kind and arm in the controls (entry 012:
# hot shard under nobody, 0.312 to 0.330 around 0.317, 5.7%). A share, not a fixed amount, because
# the window here is 240 s, not the controls' 75 s, and a storm's loss grows with the window.
HARM_SHARE = 0.06

# ---- Pilot model, for the power table (entries 012 to 016) -------------------------------------------

# Customer minutes when the correct fix lands at detection (entry 012, scripted arm, medians).
FIXED = {"retry_storm": 0.281, "hot_shard": 0.074, "compound": 0.080}
# Customer minutes a second while nothing fixes it (entry 012, nobody arm, medians over 75 s).
UNFIXED = {"retry_storm": 1.248 / 75, "hot_shard": 0.317 / 75, "compound": 0.320 / 75}
WRONG_TRY_S = 31.0  # a set applied that does not fix it: WAIT, then its undo (entries 012 and 014)
# The steps that end each kind; a step with a cap is refused in a retry storm (no key sends 5% of R),
# which spends a try but no time (entry 014).
ENDS = {"retry_storm": {"backoff"}, "hot_shard": {"cap", "both"}, "compound": {"both"}}
NOISE_SD = 0.014  # one storm's paired difference: two runs each spread by about 0.01 (entry 012)
PAIRS = len(list(combinations(ARMS, 2)))


def script_minutes(kind: str, order: Sequence[str]) -> float:
    """The pilot's customer minutes for the script trying `order` on a storm of `kind`. Pure."""
    lost = 0.0
    for step in order:
        if step in ENDS[kind]:
            return FIXED[kind] + lost
        if not (kind == "retry_storm" and step != "backoff"):  # refused, so nothing applied and no time
            lost += WRONG_TRY_S * UNFIXED[kind]
    raise ValueError(f"{order} never ends a {kind}")


def chooser_minutes(kind: str, decide_s: float) -> float:
    """The pilot's customer minutes for an arm that picks the right set first, deciding in `decide_s`. Pure."""
    return FIXED[kind] + decide_s * UNFIXED[kind]


def mean_saving(decide_s: float) -> float:
    """The mean saving per incident of a right-first chooser against the script, over kinds and orders. Pure."""
    orders = list(permutations(STEPS))
    return statistics.fmean(script_minutes(k, o) - chooser_minutes(k, decide_s) for k in KINDS for o in orders)


def draw_for(decide_s: float) -> Callable[[int, random.Random], float]:
    """One storm's paired difference (chooser minus script) under the pilot model, for `power`."""

    def draw(i: int, rng: random.Random) -> float:
        """Storm i: its kind in turn as `storms` gives them, the script's order drawn, plus run noise."""
        kind = KINDS[i % len(KINDS)]
        order = rng.sample(STEPS, len(STEPS))
        return chooser_minutes(kind, decide_s) - script_minutes(kind, order) + rng.gauss(0, NOISE_SD)

    return draw


def storms_per_kind_for_correction(alpha: float = 0.05, pairs: int = PAIRS) -> int:
    """The fewest storms of one kind that, all won by one arm, pass the step-down correction's strictest step. Pure."""
    k = 1
    while exact_paired_p(k, 0) > alpha / pairs:
        k += 1
    return k


def power_table(sims: int = 2000) -> JSON:
    """Power at the largest effect for several N, and the smallest effect STORMS detect with 80%."""
    largest = {n: power(draw_for(0.0), n, sims=sims) for n in (12, 18, 24, 30)}
    scan = {d: power(draw_for(d), STORMS, sims=sims) for d in (0.5, 1.0, 1.5, 2.0)}
    detectable = min((mean_saving(d) for d, p in scan.items() if p >= 0.8), default=None)
    return {
        "largest_saving": round(mean_saving(0.0), 4),
        "power_at_largest": largest,
        "storms": STORMS,
        "power_at_storms_by_decide_s": {d: {"saving": round(mean_saving(d), 4), "power": p} for d, p in scan.items()},
        "smallest_saving_80": None if detectable is None else round(detectable, 4),
        "storms_per_kind_for_correction": storms_per_kind_for_correction(),
        "pilot_decide_s_saving": round(mean_saving(6.5), 4),
        "sims": sims,
    }


# ---- Running the arms ----------------------------------------------------------------------------------


def draw_orders(seed: int, storm_id: str) -> tuple[list[str], list[str]]:
    """The order of the arms and of the script's steps for one storm. Pure: a string seed is stable everywhere."""
    rng = random.Random(f"{seed}:{storm_id}")
    return rng.sample(ARMS, len(ARMS)), rng.sample(STEPS, len(STEPS))


def run_arm(storm: Storm, arm: str, model: str, config: LoopConfig, script: Sequence[str]) -> JSON:
    """One storm under one arm, measured to HORIZON_S after the fault."""
    if arm == "nobody":
        with controls.live_storm(storm, arm, controls.RATE, config.wait_s, config.hold_s) as live:
            live.cover(live.t_fault + HORIZON_S)
        return live.record
    tries = 1 if arm == "model-1" else TRIES
    loop = LoopConfig(wait_s=config.wait_s, hold_s=config.hold_s, tries=tries)
    return run_one(storm, model, loop, arm=arm, script=script if arm == "script" else (), until_s=HORIZON_S)


def window_minutes(r: Mapping[str, Any]) -> float:
    """Customer minutes lost in the HORIZON_S after the fault, from the run's saved series. Pure."""
    series = [Second(*s) for s in r["series"]]
    return customer_minutes(series, r["t_fault"], r["rate"], r["t_fault"] + HORIZON_S)


def unjudged(r: Mapping[str, Any]) -> list[str]:
    """Why a run cannot be compared: the controls' reasons, or the window not measured to its end. Pure."""
    why = list(r.get("invalid") or [])
    if not r["series"] or r["series"][-1][0] < r["t_fault"] + HORIZON_S - 1:
        why.append("window not measured to its end")
    return why


def judged_run(storm: Storm, arm: str, model: str, config: LoopConfig, script: Sequence[str]) -> list[JSON]:
    """The run, and once more if it cannot be judged (entry 011's rule): every try kept, the last one counts."""
    first = run_arm(storm, arm, model, config, script)
    first["unjudged"] = unjudged(first)
    if not first["unjudged"]:
        return [first]
    second = run_arm(storm, arm, model, config, script)
    second["unjudged"], second["second_try"] = unjudged(second), True
    return [first, second]


# ---- Analysis ------------------------------------------------------------------------------------------


def resolved(r: Mapping[str, Any]) -> bool:
    """The incident ended resolved: the loop's own verdict, or for nobody the storm driver's. Pure."""
    loop = r.get("loop")
    return bool(loop["resolved"]) if loop else bool(r["recovered"])


def needless(r: Mapping[str, Any]) -> list[str]:
    """Actions still in force at the end that the storm's fix does not need and that changed something. Pure.

    Backoff turned on where the gateway already backed off changed nothing, so
    it is not counted; any other kept action outside the fix is.
    """
    storm = r["storm"]
    kind, key = storm["kind"], storm.get("key", "")
    out = []
    for a in (r.get("loop") or {}).get("kept", []):
        if a["name"] == "backoff":
            needed = kind in ("retry_storm", "compound")
            already = r["env"]["GATEWAY_BACKOFF"] == "1"  # deployed backing off, as in a hot shard
            if not needed and not already:
                out.append("backoff on")
        elif a["name"] == "rate_cap":
            if not (kind in ("hot_shard", "compound") and a["args"].get("key") == key):
                out.append(f"cap {a['args'].get('key')}")
        else:
            out.append(a["name"])
    return out


def harm(r: Mapping[str, Any], nobody: Mapping[str, Any]) -> list[str]:
    """Why a run left the system worse than nobody did on the same storm; empty when it did not. Pure."""
    why = []
    if window_minutes(r) > window_minutes(nobody) * (1 + HARM_SHARE):
        why.append("lost more than nobody")
    loop = r.get("loop")
    if loop and not loop["resolved"] and not r.get("restored", False):
        why.append("ended in the safe state with the gateway's settings not restored")
    return why


def spent(r: Mapping[str, Any]) -> float | None:
    """What the incident's decider spent, or None when its record is unknown. Pure."""
    budget = r.get("budget")
    return None if budget is None else float(budget["spent_usd"])


def analyze(report: Mapping[str, Any]) -> JSON:
    """Every number of the result, from the saved runs. Pure: the same file always gives the same answer."""
    counted = [r for r in report["runs"] if not r.get("repeat") and r.get("counts", True)]
    by: dict[str, dict[str, JSON]] = {}
    for r in counted:
        by.setdefault(r["storm"]["id"], {})[r["arm"]] = r
    complete = [
        sid for sid, arms in by.items() if set(arms) == set(ARMS) and not any(a["unjudged"] for a in arms.values())
    ]
    excluded = sorted(set(by) - set(complete))
    runs = {arm: [by[sid][arm] for sid in complete] for arm in ARMS}
    minutes = {arm: [window_minutes(r) for r in runs[arm]] for arm in ARMS}
    out: JSON = {"storms": len(complete), "excluded": excluded}
    if len(complete) < 2:
        return out | {"error": "fewer than 2 storms with every arm judged"}

    mean, ci = paired_interval(minutes[PRIMARY[0]], minutes[PRIMARY[1]])
    out["primary"] = {"comparison": f"{PRIMARY[0]} - {PRIMARY[1]}", "mean": round(mean, 4), "ci95": ci}
    kinds = [k for k in KINDS if any(r["storm"]["kind"] == k for r in runs[PRIMARY[0]])]
    out["minutes"] = {arm: {k: by_kind(minutes[arm], runs[arm], k) for k in kinds} for arm in ARMS}
    res = {arm: [resolved(r) for r in runs[arm]] for arm in ARMS}
    out["resolved"] = {arm: sum(res[arm]) for arm in ARMS}
    pairs: JSON = {}
    for a, b in combinations(ARMS, 2):
        wins = sum(x and not y for x, y in zip(res[a], res[b], strict=True))
        losses = sum(y and not x for x, y in zip(res[a], res[b], strict=True))
        pairs[f"{a} vs {b}"] = {"won": wins, "lost": losses, "p": exact_paired_p(wins, losses)}
    for name, rejected in step_down({k: v["p"] for k, v in pairs.items()}).items():
        pairs[name]["rejected"] = rejected
    out["resolution_pairs"] = pairs
    out["needless_kept"] = {arm: sum(bool(needless(r)) for r in runs[arm]) for arm in ARMS[1:]}

    costs: JSON = {}
    for arm in ARMS[1:]:
        values = [spent(r) for r in runs[arm]]
        known = [v for v in values if v is not None]
        total = sum(known)
        costs[arm] = {
            "unknown": len(values) - len(known),
            "mean_usd": statistics.fmean(known) if known else None,
            "ci95": bootstrap_ci(known),
            "per_resolved_usd": total / out["resolved"][arm] if out["resolved"][arm] else None,
        }
    out["cost"] = costs

    harms = {
        arm: [h for s, r in zip(complete, runs[arm], strict=True) if (h := harm(r, by[s]["nobody"]))]
        for arm in ARMS[1:]
    }
    n_harm = sum(len(v) for v in harms.values())
    out["harm"] = {arm: len(v) for arm, v in harms.items()} | {
        "upper95_per_arm": round(zero_events_upper(len(complete)), 3) if n_harm == 0 else None
    }
    over = sum(str(r["loop"]["reason"]).startswith("budget:") for arm in ARMS[1:] for r in runs[arm])
    out["over_budget"] = over
    out["disagree"] = sum(r.get("agree") is False for arm in ARMS[1:] for r in runs[arm])
    out["acted_past_window"] = sum(acted_past(r) for arm in ARMS[1:] for r in runs[arm])

    # Only storms in the main comparison, so the consistency figure is about the same storms.
    repeats = [r for r in report["runs"] if r.get("repeat") and not r["unjudged"] and r["storm"]["id"] in complete]
    if repeats:
        sets: dict[str, list[bool]] = {}
        for r in repeats:
            sets.setdefault(r["storm"]["id"], [resolved(by[r["storm"]["id"]]["model-loop"])]).append(resolved(r))
        full = [v for v in sets.values() if len(v) == REPEATS]
        out["consistency"] = {"storms": len(full), "all_resolved": sum(all(v) for v in full)}
    out["bar_met"] = bool(ci and ci[1] < 0 and n_harm == 0 and over == 0)
    return out


def by_kind(values: Sequence[float], runs: Sequence[Mapping[str, Any]], kind: str) -> float:
    """The mean of `values` over the runs of one storm kind. Pure."""
    return round(statistics.fmean(v for v, r in zip(values, runs, strict=True) if r["storm"]["kind"] == kind), 4)


def acted_past(r: Mapping[str, Any]) -> bool:
    """The loop was still waiting or holding at the window's end, so failures after it went uncounted. Pure."""
    tries = (r.get("loop") or {}).get("tries") or []
    if not tries:
        return False
    last = tries[-1]
    # How long the loop watched after the last try's action: nothing after a refusal or a failed
    # action, WAIT before undoing what was not verified, WAIT and HOLD (and its margin) otherwise.
    watched = {"denied": 0.0, "action failed": 0.0, "not verified": r["wait_s"]}.get(
        last["verdict"], r["wait_s"] + r["hold_s"] + HOLD_MARGIN_S
    )
    return bool(r["loop_clock_offset_s"] + last["t_act"] + watched > r["t_fault"] + HORIZON_S)


def line(r: Mapping[str, Any]) -> str:
    """One run as a printed line."""
    s, loop = r["storm"], r.get("loop") or {}
    tries = "; ".join(
        " + ".join(a["name"] for a in t["actions"]) + f" -> {t['verdict']}" for t in loop.get("tries", [])
    )
    return (
        f"{s['id']:<8} {s['kind']:<11} {r['arm']:<10} {'resolved' if resolved(r) else 'not':<8} "
        f"minutes {window_minutes(r):<6} [{tries or '-'}]"
        + (f"  NOT JUDGED: {'; '.join(r['unjudged'])}" if r["unjudged"] else "")
    )


# ---- Commands ------------------------------------------------------------------------------------------


def run(seed: int, count: int, consistency: int, model_arg: str | None) -> int:
    """Every storm through every arm, then the consistency repeats, then the analysis; saved after every run."""
    ready = preflight(model_arg)
    if isinstance(ready, str):
        print(ready, file=sys.stderr)
        return 2
    model, config = ready
    report: JSON = {
        "started_utc": datetime.now(UTC).isoformat(timespec="seconds"),
        "protocol": "research log entry 019",
        "seed": seed,
        "storms": count,
        "horizon_s": HORIZON_S,
        "arms": list(ARMS),
        "model": model,
        "budget_limit_usd": LIMIT_USD,
        "wait_s": config.wait_s,
        "hold_s": config.hold_s,
        "conditions": loadtest.conditions(),
        "runs": [],
    }
    out = controls.results_path("experiment")
    planned = count * len(ARMS) + consistency * (REPEATS - 1)
    print(f"{planned} runs, about 5 minutes each; results: {out}", flush=True)
    chosen = storms(count, controls.RATE, seed)
    try:
        for storm in chosen:
            arms, script = draw_orders(seed, storm.id)
            for arm in arms:
                for r in judged_run(storm, arm, model, config, script):
                    r["script_order"] = script if arm == "script" else None
                    r["arm_order"] = arms
                    report["runs"].append(r)
                    print(line(r), flush=True)
                controls.save(report, out)
        for storm in chosen[:consistency]:
            for k in range(2, REPEATS + 1):
                for r in judged_run(storm, "model-loop", model, config, ()):
                    r["repeat"] = k
                    report["runs"].append(r)
                    print(line(r), flush=True)
                controls.save(report, out)
    finally:
        try:
            loadtest.compose("--profile", "storm", "stop", "customers")  # no load left running afterwards
        except Exception as exc:
            print(f"could not stop the customers service ({exc})", file=sys.stderr)
        loadtest.restore()
    mark_counted(report["runs"])
    report["analysis"] = analyze(report)
    report["complete"] = True
    controls.save(report, out)
    print(json.dumps(report["analysis"], indent=1, default=str), f"\nresults: {out}", sep="\n", flush=True)
    return 0


def mark_counted(runs: list[JSON]) -> None:
    """Of a run and its second try, only the last one counts. Keeps both in the file."""
    for i, r in enumerate(runs):
        nxt = runs[i + 1] if i + 1 < len(runs) else None
        r["counts"] = not (
            nxt is not None
            and nxt.get("second_try")
            and nxt["storm"]["id"] == r["storm"]["id"]
            and nxt["arm"] == r["arm"]
            and nxt.get("repeat") == r.get("repeat")
        )


def main(argv: list[str] | None = None) -> int:
    """`run`, `power` or `analyze FILE`."""
    parser = argparse.ArgumentParser(prog="findyfixy-experiment", description=__doc__.splitlines()[0])
    sub = parser.add_subparsers(dest="cmd", required=True)
    go = sub.add_parser("run", help="the held-out storms through every arm")
    go.add_argument("--seed", type=int, default=HELD_OUT_SEED)
    go.add_argument("--count", type=int, default=STORMS)
    go.add_argument("--consistency", type=int, default=CONSISTENCY_STORMS)
    go.add_argument("--model", help="the model that decides (default: SMALL_MODEL)")
    sub.add_parser("power", help="the power table the protocol's N comes from")
    again = sub.add_parser("analyze", help="every number again, from a saved results file")
    again.add_argument("file", type=Path)
    args = parser.parse_args(argv)
    if args.cmd == "power":
        print(json.dumps(power_table(), indent=1))
        return 0
    if args.cmd == "analyze":
        report = json.loads(args.file.read_text())
        mark_counted(report["runs"])
        print(json.dumps(analyze(report), indent=1, default=str))
        return 0
    return run(args.seed, args.count, args.consistency, args.model)


if __name__ == "__main__":
    sys.exit(main())
