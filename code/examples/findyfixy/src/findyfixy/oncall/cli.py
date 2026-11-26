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
from collections.abc import Sequence
from contextlib import suppress
from dataclasses import asdict
from datetime import UTC, datetime
import json
import sys
from typing import Any
import urllib.request
import uuid

from findyfixy.llm import ANSWER_TIMEOUT_S
from findyfixy.oncall.decide import MAX_OUTPUT_TOKENS, MAX_RETRIES, PROMPT_BYTES_BOUND
from findyfixy.paths import PRICES
from findyfixy.settings import MissingEnvironment, load_env, read_models
from simprod import controls, loadtest, vault
from simprod.faults import Storm, storms
from wyld3rness.loop.budget import Budget, BudgetExceeded
from wyld3rness.loop.remote import decode_try
from wyld3rness.loop.run import LoopConfig, Try
from wyld3rness.trace.prices import ModelPrice, parse_prices

JSON = dict[str, Any]
ARM = "loop"
EXPLORE_SEED, EXPLORE_STORMS = 3, 3  # one storm of each kind
# Per incident, at the committed reference prices: about twice the worst case of 12
# small-model calls of 2,000 input and 300 output tokens ($0.00264), from the Day 7 lesson.
# The loop makes at most 6 (3 tries, 1 retry each); their worst case is about $0.002.
LIMIT_USD = 0.005  # also the agent container's BUDGET_USD (examples/findyfixy/docker-compose.yml)
LOOP_URL = "http://127.0.0.1:8300"  # the loop container, published on loopback only


def last_action_at(tries: Sequence[Try]) -> float | None:
    """When the last try that changed something applied its actions; None when no try did. Pure.

    The storm driver's own verdict is measured from it. A try the policy
    refused applied nothing, so a time taken from it would anchor on no action.
    """
    return next((t.t_act for t in reversed(tries) if t.applied), None)


def on_storm_clock(sent_at: float, tries: Sequence[Try]) -> float | None:
    """The last applied action's time on the storm's clock, from the loop's clock that started at `sent_at`. Pure."""
    last = last_action_at(tries)
    return None if last is None else sent_at + last


def line(r: JSON) -> str:
    """One storm's result as a printed line."""
    s, loop = r["storm"], r["loop"]
    tries = "; ".join(
        f"{t['number']}: {' + '.join(a['name'] for a in t['actions'])} -> {t['verdict']}" for t in loop["tries"]
    )
    return (
        f"{s['id']:<8} {s['kind']:<11} {loop['ended']:<10} ({loop['reason']})  tries [{tries or '-'}]  "
        f"driver recovered {r['recovered']!s:<5} minutes {r['customer_minutes']:<6} "
        + (
            f"model calls {len(r['model_calls'])} spent ${r['budget']['spent_usd']:.6f} of ${r['budget']['limit_usd']}"
            if r.get("budget")
            else f"agent record unknown: {r.get('agent_record_error')}"
        )
        + (f"  NOT JUDGED: {'; '.join(r['invalid'])}" if r.get("invalid") else "")
    )


def run_one(
    storm: Storm,
    model: str,
    config: LoopConfig,
    *,
    arm: str = ARM,
    script: Sequence[str] = (),
    until_s: float = 0.0,
    notify: controls.Notify = controls.quiet,
) -> JSON:
    """One storm: the host runs it and keeps its own verdict; the loop container handles the incident.

    The loop's times are on its own clock from the request; adding the time the
    request was sent puts them on the storm's clock, where the host's verdict is.
    `script` (an order of FindyFixy's script steps) makes the script decide instead
    of the model. The series is measured at least `until_s` after the fault, so
    an experiment can give every arm the same window. `notify` hears the storm's
    phases and seconds (`controls.live_storm`) and `incident_sent` with the
    incident's id and when it was sent on the storm's clock, so a viewer can put
    the loop's own events (`GET /incident/events`) on the storm's clock.
    """
    incident = f"{storm.id}-{uuid.uuid4().hex[:12]}"  # unique, so no agent can mistake it for another
    with controls.live_storm(storm, arm, controls.RATE, config.wait_s, config.hold_s, notify) as live:
        sent_at = live.now()
        with suppress(Exception):  # a viewer never costs a storm its verdict
            notify("incident_sent", incident=incident, at=round(sent_at, 2))
        answer = post_incident(incident, config, model, script, arm)
        live.judge_from = on_storm_clock(sent_at, [decode_try(t) for t in answer["loop"]["tries"]])
        agent = answer.get("agent") or {"record_error": "the loop sent no agent record"}
        live.record.update(
            model=model,
            incident=incident,
            loop_clock_offset_s=round(sent_at, 2),
            loop=answer["loop"],
            sent=answer["sent"],
            # Unknown is stored as unknown (None and the reason), never as zero spent or no calls.
            model_calls=agent.get("model_calls"),
            reasons=agent.get("reasons"),
            budget=agent.get("budget"),
            agent_record_error=agent.get("record_error"),
            loop_scrape_errors=answer.get("scrape_errors"),
            trace_id=answer.get("trace_id"),  # the incident's trace in Langfuse, None with tracing off
            settings_after=controls.read_settings(),
        )
        live.record["restored"] = live.record["settings_after"] == live.before
        live.cover(max((live.judge_from or live.t_act) + config.wait_s + config.hold_s + 2, live.t_fault + until_s))
    live.record["agree"] = live.record["recovered"] == answer["loop"]["resolved"]
    return live.record


def incident_timeout_s(config: LoopConfig) -> float:
    """The longest one incident can legitimately take, so the host never gives up while the loop still acts. Pure.

    Each try: every model call a decision may make, each up to the model's read
    timeout, then WAIT, HOLD and the hold margin; plus a minute of room.
    """
    per_try = (1 + MAX_RETRIES) * ANSWER_TIMEOUT_S + config.wait_s + config.hold_s + config.hold_margin_s
    return config.tries * per_try + 60


def post_incident(incident: str, config: LoopConfig, model: str, script: Sequence[str] = (), arm: str = ARM) -> JSON:
    """Ask the loop container to handle one incident now, with this run's decider, budget, tries and windows."""
    body: JSON = {
        "incident": incident,
        "wait_s": config.wait_s,
        "hold_s": config.hold_s,
        "tries": config.tries,
        "model": model,
        "budget_usd": LIMIT_USD,
        "arm": arm,
    }
    if script:
        body.update(decider="script", order=list(script))
    req = urllib.request.Request(
        f"{LOOP_URL}/incident", json.dumps(body).encode(), {"Content-Type": "application/json"}
    )
    with urllib.request.urlopen(req, timeout=incident_timeout_s(config)) as r:
        answer: JSON = json.load(r)
        return answer


def services_ready() -> str | None:
    """Why the loop container cannot take an incident, checked before any storm; None when it can."""
    try:
        with urllib.request.urlopen(f"{LOOP_URL}/healthz", timeout=5):
            return None
    except OSError as exc:
        return f"the loop container does not answer on {LOOP_URL} ({type(exc).__name__}): run `make oncall-up`"


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


def preflight(model_arg: str | None) -> tuple[str, LoopConfig] | str:
    """The model and the loop's windows, or why no storm may start: checked before any storm, in this order.

    The model is named; the windows are set; the budget carries the model's worst
    case; the loop container answers; the vault signs and its clock agrees.
    """
    load_env()
    try:
        model = model_arg or read_models().small
    except MissingEnvironment as exc:
        return f"set {exc} in code/examples/findyfixy/.env, or pass --model"
    if controls.WAIT_S is None or controls.HOLD_S is None:
        return "WAIT_S and HOLD_S are not set in simprod/controls.py"
    config = LoopConfig(wait_s=controls.WAIT_S, hold_s=controls.HOLD_S)
    refusal = budget_refusal(model, parse_prices(PRICES.read_text()), config) or services_ready()
    if refusal:
        return refusal
    try:
        vault.check()  # an expired pass, a sealed vault or a skewed clock stops the run before any storm
    except Exception as exc:
        return f"the vault is not ready ({exc}): run `make sim-up`"
    return model, config


def main(argv: list[str] | None = None) -> int:
    """Run `--count` storms from `--seed` through the loop; print a line each and save the results."""
    parser = argparse.ArgumentParser(prog="findyfixy-oncall", description=__doc__.splitlines()[0])
    parser.add_argument("--seed", type=int, default=EXPLORE_SEED)
    parser.add_argument("--count", type=int, default=EXPLORE_STORMS)
    parser.add_argument("--model", help="the model that decides (default: SMALL_MODEL)")
    args = parser.parse_args(argv)
    ready = preflight(args.model)
    if isinstance(ready, str):
        print(ready, file=sys.stderr)
        return 2
    model, config = ready
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
