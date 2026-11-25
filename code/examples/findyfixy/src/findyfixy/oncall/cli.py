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
from findyfixy.oncall.decide import Decision, ModelDecider
from findyfixy.oncall.env import SimprodEnv
from findyfixy.settings import MissingEnvironment, load_env, read_models
from simprod import controls, loadtest
from simprod.faults import Storm, storms
from wyld3rness.loop.run import LoopConfig, Outcome, run_incident

JSON = dict[str, Any]
ARM = "loop"
EXPLORE_SEED, EXPLORE_STORMS = 3, 3  # one storm of each kind


def outcome_json(outcome: Outcome) -> JSON:
    """The loop's outcome as plain JSON, with `resolved` spelled out. Pure."""
    return {**asdict(outcome), "resolved": outcome.resolved}


def line(r: JSON) -> str:
    """One storm's result as a printed line."""
    s, loop = r["storm"], r["loop"]
    tries = "; ".join(
        f"{t['number']}: {' + '.join(a['name'] for a in t['actions'])} -> {t['verdict']}" for t in loop["tries"]
    )
    return (
        f"{s['id']:<8} {s['kind']:<11} {loop['ended']:<10} ({loop['reason']})  tries [{tries or '-'}]  "
        f"driver recovered {r['recovered']!s:<5} minutes {r['customer_minutes']:<6} "
        f"model calls {len(r['model_calls'])}"
        + (f"  NOT JUDGED: {'; '.join(r['invalid'])}" if r.get("invalid") else "")
    )


def run_one(storm: Storm, model: str, config: LoopConfig) -> JSON:
    """One storm through the loop; the record the controls keep, plus the loop, the model calls and the reasons."""
    with controls.live_storm(storm, ARM, controls.RATE, config.wait_s, config.hold_s) as live:
        env = SimprodEnv(live.scraper.series, live.now)
        decider = ModelDecider(ask_decision, model, config.wait_s)
        outcome = run_incident(env, decider, config)
        if outcome.tries:
            live.judge_from = outcome.tries[-1].t_act
        live.record.update(
            model=model,
            loop=outcome_json(outcome),
            sent=env.sent,
            model_calls=[asdict(c) for c in decider.calls],
            reasons=decider.reasons,
            settings_after=controls.read_settings(),
        )
        live.record["restored"] = live.record["settings_after"] == live.before
        live.cover((live.judge_from or live.t_act) + config.wait_s + config.hold_s + 2)
    live.record["agree"] = live.record["recovered"] == outcome.resolved
    return live.record


def ask_decision(model: str, prompt: str) -> str:
    """The real model call for the decider: constrained to `Decision`, validated by the decider."""
    return ollama_reply(model, prompt, Decision)


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
    report: JSON = {
        "started_utc": datetime.now(UTC).isoformat(timespec="seconds"),
        "rate": controls.RATE,
        "seed": args.seed,
        "model": model,
        "loop": asdict(config),
        "conditions": loadtest.conditions(),
        "runs": [],
    }
    out = controls.results_path("oncall")
    try:
        for storm in storms(args.count, controls.RATE, args.seed):
            r = run_one(storm, model, config)
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
