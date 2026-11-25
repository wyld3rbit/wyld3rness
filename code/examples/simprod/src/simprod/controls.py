"""The controls: before any agent runs, prove that the storms are real and the checker is fair.

    make controls-pilot     a few storms, nobody and the scripted fix, long windows ──► WAIT and HOLD
    make controls           21 storms x 3 arms, with WAIT and HOLD fixed           ──► the bars below

One storm under one arm:

    reset services, gateway deployed as the storm says ──► warm WARM_S (every second must be good)
        ──► inject the fault (chaos, sealed) ──► DETECT_S later, the arm acts
        ──► wait WAIT ──► verify ──► hold HOLD ──► clear, verdict

nobody    acts with nothing. Bar: 21 of 21 not recovered (the storm lasts by itself).
scripted  acts with the storm's fix, a set of actions chosen from the seed (an
          oracle). Bar: 21 of 21 recovered and held.
wrong     acts with the storm's wrong fix (faults.py), undone if not verified
          after WAIT. Bar: 21 of 21 not verified, undone, and the gateway back
          to its starting settings.

Every arm also needs every action and undo it sent answered 200: a refused
action would make a "fix" or an "undo" that never happened look like a result.
And a run counts only if it can be judged (`invalid`): the customer stream
measured whole and the load really offered. A run that cannot is a miss.
The storms cycle through the three kinds, so 21 is 7 of each.

If nobody's storms end by themselves, a later "fix" would be credited for what
time did. If the scripted fix fails, the environment cannot be won and any
agent result is noise. If a wrong fix is not caught and undone, the checker
cannot be trusted. Each arm is the control for one of those.

The pilot sets WAIT and HOLD from data, before the controls: WAIT is twice the
slowest recovery after the scripted fix, HOLD twice the longest stretch of good
seconds in a storm nobody fixed (a false recovery must not outlast it), each
rounded up to 5 s, HOLD at least 30 s. Both are written under the lesson's
Results and into WAIT_S and HOLD_S before `make controls` runs; it refuses to run
without them.
"""

from __future__ import annotations

import argparse
from collections.abc import Mapping
from dataclasses import asdict
from datetime import UTC, datetime
import json
import math
from pathlib import Path
import re
import sys
import threading
import time
from typing import Any
import urllib.error
import urllib.request

from simprod import loadtest
from simprod.checker import (
    Scraper,
    Second,
    customer_minutes,
    late_share,
    longest_good_stretch,
    max_gap,
    recovered_at,
    resets,
)
from simprod.faults import FAULTS_VERSION, Storm, storms

JSON = dict[str, Any]

RATE = 200  # R, set by research log entry 007 (C = 800/s, R = C / 4); the compose defaults match (tests check)
WARM_S = 20
DETECT_S = 15
VERIFY_S = 5  # at the end of WAIT, the last this many seconds must all be good to call it verified
PILOT_SEED, PILOT_STORMS, PILOT_OBSERVE_S = 3, 9, 90  # 3 of each kind
CONTROLS_SEED, CONTROLS_STORMS = 7, 21  # 7 of each kind
# From the pilot (entry 008, re-run after entry 009 on 2026-11-25), written before the
# controls run. Slowest scripted recovery 8.6 s, so WAIT = 2 x 8.6 rounded up to 5;
# no good second under nobody, so HOLD is its 30 s floor. None: `make controls` refuses.
WAIT_S: int | None = 20
HOLD_S: int | None = 30
MAX_GAP_S = 2.5  # longest allowed time between two scrapes after the fault: one fast-failing scrape at most
# Every compose setting the storm arithmetic rests on (faults.py), pinned so a value
# exported in the shell cannot change the storms. The compose defaults match (tests check).
STORM_FIXED = {"LEDGER_SHARDS": "4", "LEDGER_QUEUE_LIMIT": "100", "NOTIFY_SERVICE_MS": "5"}
EDGE = "http://127.0.0.1:8100"
ACTOR = "controls"


# ---- Talking to simprod ------------------------------------------------------------------


def storm_env(storm: Storm, rate: int) -> dict[str, str]:
    """The settings a storm runs with: pinned gateway deployed as the storm says, ledger at 2 x R, customers at R."""
    return {
        **loadtest.PINNED,
        **STORM_FIXED,
        "GATEWAY_BACKOFF": "1" if storm.gateway_backoff else "0",
        **loadtest.soak_ledger(rate),
        "CUSTOMER_RATE": str(rate),
        "CUSTOMER_SEED": str(storm.seed),
    }


def reset(storm: Storm, rate: int) -> None:
    """Fresh services for every storm: no queue, no setting, no fault carried over."""
    loadtest.compose(
        "--profile",
        "storm",
        "up",
        "-d",
        "--wait",
        "--force-recreate",
        "ledger",
        "notifications",
        "gateway",
        "customers",
        env=storm_env(storm, rate),
    )


def chaos(*args: str) -> JSON:
    """Run the fault driver inside the sealed network; returns its JSON line."""
    out = loadtest.compose(
        "--profile", "chaos", "run", "--rm", "-T", "chaos", "python", "-m", "simprod.faults", *args, capture=True
    )
    line = out.strip().splitlines()[-1]
    result: JSON = json.loads(line)
    return result


def act(path: str, body: Mapping[str, object]) -> JSON:
    """One typed action through the edge, as the agent will send it. Never raises: a failure is status 0."""
    req = urllib.request.Request(
        EDGE + path,
        data=json.dumps(body).encode(),
        method="POST",
        headers={"Content-Type": "application/json", "X-Actor": ACTOR},
    )
    try:
        with urllib.request.urlopen(req, timeout=10) as r:
            return {"path": path, "body": dict(body), "status": r.status, "answer": json.loads(r.read())}
    except urllib.error.HTTPError as e:
        return {"path": path, "body": dict(body), "status": e.code, "answer": e.read().decode(errors="replace")}
    except OSError as e:  # refused, reset or timed out: the action may or may not have happened
        return {"path": path, "body": dict(body), "status": 0, "answer": f"{type(e).__name__}: {e}"}


_GAUGE = re.compile(r'^gateway_(backoff_enabled|rate_caps)(?:\{key="([^"]+)"\})?\s+(\S+)$', re.M)


def gateway_settings(metrics_text: str) -> JSON:
    """The settings the actions change, as the gateway reports them. Pure."""
    state: JSON = {"backoff": False, "caps": {}}
    for name, key, value in _GAUGE.findall(metrics_text):
        if name == "backoff_enabled":
            state["backoff"] = float(value) == 1.0
        else:
            state["caps"][key] = float(value)
    return state


def read_settings() -> JSON:
    """The gateway's settings now, through the edge. A failed read is an error record, never equal to settings."""
    try:
        with urllib.request.urlopen(EDGE + "/gateway/metrics", timeout=5) as r:
            return gateway_settings(r.read().decode())
    except OSError as e:
        return {"error": f"{type(e).__name__}: {e}"}


def verified(series: list[Second], at: float) -> bool:
    """Every second in the VERIFY_S before `at` was good. Pure."""
    window = [s for s in series if at - VERIFY_S < s.t <= at]
    return len(window) >= VERIFY_S - 1 and all(s.good for s in window)


# ---- One storm ------------------------------------------------------------------------------


def run_storm(storm: Storm, arm: str, rate: int, wait_s: float, hold_s: float) -> JSON:
    """Run `storm` under `arm` (nobody, scripted or wrong) and return everything that happened."""
    reset(storm, rate)
    scraper = Scraper(EDGE + "/customers/metrics")
    stop = threading.Event()

    def loop() -> None:
        """Scrape once a second until stopped. Nothing ends this loop early: a lost series would lose the verdict."""
        while not stop.wait(1.0):
            try:
                scraper.scrape()
            except Exception:  # scrape already counts the failures it expects; this counts the rest
                scraper.errors += 1

    threading.Thread(target=loop, daemon=True).start()
    record: JSON = {
        "storm": asdict(storm),
        "arm": arm,
        "rate": rate,
        "env": storm_env(storm, rate),
        "wait_s": wait_s,
        "hold_s": hold_s,
        "actions": [],
        "undos": [],
        "faults_version": FAULTS_VERSION,
    }
    try:
        before = read_settings()
        time.sleep(WARM_S)
        warm = scraper.series()
        record["warm_ok"] = bool(warm) and all(s.good for s in warm)
        t_fault = time.monotonic() - scraper.start
        record["injected"] = chaos("inject", json.dumps(asdict(storm)))
        time.sleep(DETECT_S)
        t_act = time.monotonic() - scraper.start
        chosen = storm.fix(rate) if arm == "scripted" else storm.wrong(rate) if arm == "wrong" else []
        record["actions"] = [act(*a) for a in chosen]
        time.sleep(wait_s)
        t_verify = time.monotonic() - scraper.start
        record["verified"] = verified(scraper.series(), t_verify)
        if arm == "wrong" and not record["verified"]:
            record["undos"] = [act(*a) for a in storm.undo_of_wrong()]
            record["restored"] = read_settings() == before
        end = t_act + wait_s + hold_s + 2  # scrape until the hold window is covered, however late recovery came
        while (
            scraper.scrapes[-1][0] if scraper.scrapes else 0.0
        ) < end and time.monotonic() - scraper.start < end + 30:
            time.sleep(0.5)
    finally:
        stop.set()
        try:
            chaos("clear")
        except Exception as exc:  # a storm whose faults cannot be cleared must not poison the next: reset does
            record["clear_error"] = f"{type(exc).__name__}: {exc}"
    series = scraper.series()
    record.update(
        t_fault=round(t_fault, 2),
        t_act=round(t_act, 2),
        t_verify=round(t_verify, 2),
        recovered_at=recovered_at(series, t_act, wait_s, hold_s),
        longest_good_after_act_s=longest_good_stretch(series, t_act),
        customer_minutes=customer_minutes(series, t_fault, rate),
        scrape_errors=scraper.errors,
        counter_resets=resets(scraper.scrapes),
        late_share=late_share(scraper.late, t_fault),
        max_gap_s=max_gap(scraper.scrapes, t_fault),
        series=[[s.t, s.ok, s.failed] for s in series],
    )
    record["recovered"] = record["recovered_at"] is not None
    record["invalid"] = invalid(record)
    return record


def invalid(r: JSON) -> list[str]:
    """Why a run cannot be judged, empty when it can. Pure.

    A verdict counts only if the customer stream was measured whole and the
    load was really offered: no counter reset (a restart drops its failures), no
    scrape gap past MAX_GAP_S (merged seconds thin the verification window), no
    late sends past the load test's bar (the open loop had closed), and the
    faults cleared. A run that fails this is a miss, never a rerun (entry 008).
    """
    why = []
    if r["counter_resets"]:
        why.append(f"{r['counter_resets']} counter resets")
    if r["max_gap_s"] > MAX_GAP_S:
        why.append(f"scrape gap {r['max_gap_s']} s")
    if r["late_share"] > loadtest.LATE_BAR:
        why.append(f"late sends {r['late_share']:.1%}")
    if "clear_error" in r:
        why.append("faults not cleared")
    return why


def first_good_run(series: list[list[float]], after_t: float, length_s: float) -> float | None:
    """Seconds after `after_t` until a run of `length_s` good seconds begins. Pure. For the pilot."""
    secs = [Second(t, ok, f) for t, ok, f in series]
    for i, s in enumerate(secs):
        if s.t <= after_t or not s.good:
            continue
        window = [x for x in secs[i:] if x.t <= s.t + length_s]
        if secs[-1].t >= s.t + length_s and all(x.good for x in window):
            return round(s.t - after_t, 1)
    return None


def round_up_5(x: float) -> int:
    """Round up to the next multiple of 5 s."""
    return int(math.ceil(x / 5) * 5)


def pilot_windows(runs: list[JSON]) -> JSON:
    """WAIT and HOLD from pilot runs. Pure. Fails loudly if a scripted fix never recovered."""
    recoveries = [first_good_run(r["series"], r["t_act"], 10) for r in runs if r["arm"] == "scripted"]
    if any(x is None for x in recoveries):
        raise ValueError(f"a scripted fix did not recover in the pilot: {recoveries}")
    stretches = [
        longest_good_stretch([Second(*s) for s in r["series"]], r["t_act"]) for r in runs if r["arm"] == "nobody"
    ]
    slowest = max(x for x in recoveries if x is not None)
    return {
        "recovery_s": recoveries,
        "nobody_longest_good_s": stretches,
        "wait_s": round_up_5(2 * slowest),
        "hold_s": max(30, round_up_5(2 * max(stretches, default=0))),
    }


# ---- Commands -------------------------------------------------------------------------------


def results_path(name: str) -> Path:
    """A new results file under results/, named by the UTC start time."""
    out = loadtest.RESULTS / f"{name}-{datetime.now(UTC):%Y%m%dT%H%M%SZ}.json"
    out.parent.mkdir(parents=True, exist_ok=True)
    return out


def save(report: JSON, out: Path) -> None:
    """Rewrite the results file: after every storm, so a failure never loses the storms before it."""
    out.write_text(json.dumps(report, indent=1) + "\n")


def line(r: JSON) -> str:
    """One storm's result as a printed line."""
    s = r["storm"]
    acts = ", ".join(f"{a['path'].rsplit('/', 1)[-1]}={a['status']}" for a in r["actions"] + r["undos"]) or "-"
    return (
        f"{s['id']:<8} {s['kind']:<11} {r['arm']:<8} warm {'ok' if r['warm_ok'] else 'BAD'}  "
        f"verified {r['verified']!s:<5} recovered {r['recovered']!s:<5} at {r['recovered_at']}  "
        f"minutes {r['customer_minutes']:<6} actions {acts}"
        + (f"  restored {r['restored']}" if "restored" in r else "")
        + (f"  NOT JUDGED: {'; '.join(r['invalid'])}" if r.get("invalid") else "")
    )


def pilot() -> int:
    """Pilot storms, nobody and scripted, with a long window; prints and saves WAIT and HOLD."""
    report: JSON = {
        "started_utc": datetime.now(UTC).isoformat(timespec="seconds"),
        "rate": RATE,
        "seed": PILOT_SEED,
        "conditions": loadtest.conditions(),
        "runs": [],
    }
    out = results_path("pilot")
    for storm in storms(PILOT_STORMS, RATE, PILOT_SEED):
        for arm in ("nobody", "scripted"):
            r = run_storm(storm, arm, RATE, wait_s=PILOT_OBSERVE_S - 10, hold_s=10)
            report["runs"].append(r)
            print(line(r), flush=True)
            save(report, out)
    try:
        report["windows"] = pilot_windows(report["runs"])
    except ValueError as exc:
        report["windows_error"] = str(exc)
    report["complete"] = True
    save(report, out)
    print(f"\nwindows: {report.get('windows') or report.get('windows_error')}\nresults: {out}", flush=True)
    return 0 if "windows" in report else 1


def bars(runs: list[JSON]) -> JSON:
    """Each arm's bar over the storms. Pure. A run that cannot be judged (`invalid`) never meets a bar."""
    by = {arm: [r for r in runs if r["arm"] == arm] for arm in ("nobody", "scripted", "wrong")}

    def answered(r: JSON) -> bool:
        """Every action and undo the run sent was carried out."""
        return all(a["status"] == 200 for a in r["actions"] + r["undos"])

    def meets(r: JSON) -> bool:
        """This run meets its arm's bar."""
        if r["invalid"] or not r["warm_ok"]:
            return False
        if r["arm"] == "nobody":
            return not r["recovered"] and not r["actions"]
        if r["arm"] == "scripted":
            return r["recovered"] and bool(r["actions"]) and answered(r)
        return (not r["verified"]) and bool(r["undos"]) and answered(r) and r.get("restored", False)

    out: JSON = {arm: {"met": sum(meets(r) for r in by[arm]), "of": len(by[arm])} for arm in by}
    out["warm_bad"] = sum(not r["warm_ok"] for r in runs)
    out["invalid"] = sum(bool(r["invalid"]) for r in runs)
    out["pass"] = all(out[a]["met"] == out[a]["of"] > 0 for a in by)
    return out


def controls() -> int:
    """21 storms x 3 arms with the pilot's WAIT and HOLD; prints and saves the bars."""
    if WAIT_S is None or HOLD_S is None:
        print("WAIT_S and HOLD_S are not set: run `make controls-pilot` and record them first", file=sys.stderr)
        return 2
    report: JSON = {
        "started_utc": datetime.now(UTC).isoformat(timespec="seconds"),
        "rate": RATE,
        "seed": CONTROLS_SEED,
        "wait_s": WAIT_S,
        "hold_s": HOLD_S,
        "conditions": loadtest.conditions(),
        "runs": [],
    }
    out = results_path("controls")
    for storm in storms(CONTROLS_STORMS, RATE, CONTROLS_SEED):
        for arm in ("nobody", "scripted", "wrong"):
            r = run_storm(storm, arm, RATE, WAIT_S, HOLD_S)
            report["runs"].append(r)
            print(line(r), flush=True)
            save(report, out)
    report["bars"] = bars(report["runs"])
    report["complete"] = True
    save(report, out)
    print(f"\nbars: {report['bars']}\nresults: {out}", flush=True)
    return 0 if report["bars"]["pass"] else 1


def main(argv: list[str] | None = None) -> int:
    """`pilot` or `run`."""
    parser = argparse.ArgumentParser(prog="simprod-controls", description=__doc__.splitlines()[0])
    parser.add_argument("cmd", choices=["pilot", "run"])
    args = parser.parse_args(argv)
    if args.cmd == "run" and (WAIT_S is None or HOLD_S is None):
        return controls()  # refuses with its message before anything is started or restored
    try:
        return pilot() if args.cmd == "pilot" else controls()
    finally:
        try:
            loadtest.compose("--profile", "storm", "stop", "customers")  # no load left running afterwards
        except Exception as exc:
            print(f"could not stop the customers service ({exc})", file=sys.stderr)
        loadtest.restore()


if __name__ == "__main__":
    sys.exit(main())
