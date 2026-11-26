"""`make oncall`: FindyFixy runs seeded simprod storms through the harness's loop, with no person in it.

    for each storm: simprod.controls.live_storm (reset, warm, inject, DETECT_S)
        ──► wyld3rness.loop.run_incident(SimprodEnv, ModelDecider) ──► resolved or the safe state
        ──► the storm driver's own verdict, from the last try's action ──► one line, and the results file

The storm driver is the one the controls use, so a loop run differs from a
control run only in who acts. The driver's verdict (`recovered`) is computed
again from the same scraped series, independently of the loop's, and the two
must agree. Seed 3 by default: the pilot's storms, for exploratory runs; the
held-out comparison (step 6) uses a seed never run here.

Results go to code/examples/simprod/results/oncall-<UTC>.json (git-ignored),
rewritten after every storm.
"""

from __future__ import annotations

import argparse
from dataclasses import asdict
from datetime import UTC, datetime
import sys
from typing import Any

from findyfixy.llm import ollama_reply
from findyfixy.oncall.decide import MAX_OUTPUT_TOKENS, MAX_RETRIES, PROMPT_BYTES_BOUND, Decision, ModelDecider
from findyfixy.oncall.env import SimprodEnv
from findyfixy.oncall.policy import rules
from findyfixy.paths import PRICES
from findyfixy.settings import MissingEnvironment, load_env, read_models
from simprod import controls, loadtest
from simprod.faults import Storm, storms
from wyld3rness.loop.budget import Budget, BudgetExceeded
from wyld3rness.loop.policy import Policy
from wyld3rness.loop.run import LoopConfig, Outcome, run_incident
from wyld3rness.trace.prices import ModelPrice, parse_prices

JSON = dict[str, Any]
ARM = "loop"
EXPLORE_SEED, EXPLORE_STORMS = 3, 3  # one storm of each kind
# Per incident, at the committed reference prices: about twice the worst case of 12
# small-model calls of 2,000 input and 300 output tokens ($0.00264), from the Day 7 lesson.
# The loop makes at most 6 (3 tries, 1 retry each); their worst case is about $0.002.
LIMIT_USD = 0.005


def outcome_json(outcome: Outcome) -> JSON:
    """The loop's outcome as plain JSON, with `resolved` spelled out. Pure."""
    return {**asdict(outcome), "resolved": outcome.resolved}


def last_action_at(outcome: Outcome) -> float | None:
    """When the last try that changed something applied its actions; None when no try did. Pure.

    The storm driver's own verdict is measured from it. A try the policy
    refused applied nothing, so a time taken from it would anchor on no action.
    """
    return next((t.t_act for t in reversed(outcome.tries) if t.applied), None)


def line(r: JSON) -> str:
    """One storm's result as a printed line."""
    s, loop = r["storm"], r["loop"]
    tries = "; ".join(
        f"{t['number']}: {' + '.join(a['name'] for a in t['actions'])} -> {t['verdict']}" for t in loop["tries"]
    )
    return (
        f"{s['id']:<8} {s['kind']:<11} {loop['ended']:<10} ({loop['reason']})  tries [{tries or '-'}]  "
        f"driver recovered {r['recovered']!s:<5} minutes {r['customer_minutes']:<6} "
        f"model calls {len(r['model_calls'])} spent ${r['budget']['spent_usd']:.6f} of ${r['budget']['limit_usd']}"
        + (f"  NOT JUDGED: {'; '.join(r['invalid'])}" if r.get("invalid") else "")
    )


def run_one(storm: Storm, model: str, config: LoopConfig, prices: list[ModelPrice]) -> JSON:
    """One storm through the loop; the record the controls keep, plus the loop, the model calls, the budget."""
    with controls.live_storm(storm, ARM, controls.RATE, config.wait_s, config.hold_s) as live:
        env = SimprodEnv(live.scraper.series, live.now)
        policy = Policy(rules())
        budget = Budget(LIMIT_USD, prices)
        decider = ModelDecider(ask_decision, model, config.wait_s, budget, notes=policy.notes())
        outcome = run_incident(env, decider, config, policy)
        live.judge_from = last_action_at(outcome)
        live.record.update(
            model=model,
            loop=outcome_json(outcome),
            sent=env.sent,
            model_calls=[asdict(c) for c in decider.calls],
            reasons=decider.reasons,
            budget={
                "limit_usd": LIMIT_USD,
                "spent_usd": budget.spent_usd,
                "charges": [asdict(c) for c in budget.charges],
            },
            settings_after=controls.read_settings(),
        )
        live.record["restored"] = live.record["settings_after"] == live.before
        live.cover((live.judge_from or live.t_act) + config.wait_s + config.hold_s + 2)
    live.record["agree"] = live.record["recovered"] == outcome.resolved
    return live.record


def budget_refusal(model: str, prices: list[ModelPrice], config: LoopConfig) -> str | None:
    """Why `model` cannot run the loop inside LIMIT_USD, checked before any storm starts; None when it can. Pure.

    The loop's most calls (each try and its one retry) at the largest prompt a
    call can carry (the prompt bound plus the echoed invalid reply of a retry)
    must fit, or a run would end on the budget instead of on the model's decisions.
    """
    budget = Budget(LIMIT_USD, prices)
    try:
        worst = budget.worst_case(model, "x" * PROMPT_BYTES_BOUND, MAX_OUTPUT_TOKENS)
    except BudgetExceeded as exc:
        return str(exc)
    calls = config.tries * (1 + MAX_RETRIES)
    if calls * worst > LIMIT_USD:
        return (
            f"{model}: {calls} calls at a worst case of ${worst:.6f} each exceed the ${LIMIT_USD} incident budget, "
            "which is sized for the small model's price"
        )
    return None


def ask_decision(model: str, prompt: str, max_output_tokens: int) -> str:
    """The real model call for the decider: constrained to `Decision`, capped, validated by the decider."""
    return ollama_reply(model, prompt, Decision, max_output_tokens)


def main(argv: list[str] | None = None) -> int:
    """Run `--count` storms from `--seed` through the loop; print a line each and save the results."""
    parser = argparse.ArgumentParser(prog="findyfixy-oncall", description=__doc__.splitlines()[0])
    parser.add_argument("--seed", type=int, default=EXPLORE_SEED)
    parser.add_argument("--count", type=int, default=EXPLORE_STORMS)
    parser.add_argument("--model", help="the model that decides (default: SMALL_MODEL)")
    args = parser.parse_args(argv)
    load_env()
    try:
        model = args.model or read_models().small
    except MissingEnvironment as exc:
        print(f"set {exc} in code/examples/findyfixy/.env, or pass --model", file=sys.stderr)
        return 2
    if controls.WAIT_S is None or controls.HOLD_S is None:
        print("WAIT_S and HOLD_S are not set in simprod/controls.py", file=sys.stderr)
        return 2
    config = LoopConfig(wait_s=controls.WAIT_S, hold_s=controls.HOLD_S)
    prices = parse_prices(PRICES.read_text())
    refusal = budget_refusal(model, prices, config)
    if refusal:
        print(refusal, file=sys.stderr)
        return 2
    report: JSON = {
        "started_utc": datetime.now(UTC).isoformat(timespec="seconds"),
        "rate": controls.RATE,
        "seed": args.seed,
        "model": model,
        "loop": asdict(config),
        "budget_limit_usd": LIMIT_USD,
        "conditions": loadtest.conditions(),
        "runs": [],
    }
    out = controls.results_path("oncall")
    try:
        for storm in storms(args.count, controls.RATE, args.seed):
            r = run_one(storm, model, config, prices)
            report["runs"].append(r)
            print(line(r), flush=True)
            controls.save(report, out)
    finally:
        try:
            loadtest.compose("--profile", "storm", "stop", "customers")  # no load left running afterwards
        except Exception as exc:
            print(f"could not stop the customers service ({exc})", file=sys.stderr)
        loadtest.restore()
    report["complete"] = True
    controls.save(report, out)
    print(f"\nresults: {out}", flush=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())
