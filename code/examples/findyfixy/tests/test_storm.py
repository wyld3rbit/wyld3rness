"""Tests for findyfixy/console/storm.py and the console's storm routes: a storm is shown step by step, live or replayed.

No storm runs here: the storm driver, the loop container and the saved runs are
fakes. One saved record (`RECORD`) has the shape the experiment writes: a script
storm whose first try is not verified and undone, and whose second resolves.
"""

from __future__ import annotations

import json
import os
from pathlib import Path
import shutil
import subprocess
import time
from typing import Any

from pydantic import ValidationError
import pytest

from findyfixy.console.server import STORM_PAGE, RunHub, wire
from findyfixy.console.storm import (
    Poller,
    ReplayRequest,
    StormRequest,
    drive,
    finished,
    load_run,
    loop_config,
    pick_storm,
    play,
    replay_events,
    saved_runs,
)
from findyfixy.paths import ROOT
from wyld3rness.loop.run import LoopConfig

REPLAY = ROOT / "tests" / "js" / "replay.js"
BACKOFF = {"name": "backoff", "args": {"enabled": True}}
BACKOFF_OFF = {"name": "backoff", "args": {"enabled": False}}
CAP = {"name": "rate_cap", "args": {"key": "acct-0001", "rps": 10.0}}
CAP_OFF = {"name": "rate_cap", "args": {"key": "acct-0001", "rps": None}}


def _series(t_fault: float, fixed_at: float, end: float) -> list[list[float]]:
    """One second each: good before the fault, half failing until `fixed_at`, good after."""
    return [[float(t), 40.0, 0.0 if t < t_fault or t >= fixed_at else 40.0] for t in range(1, int(end) + 1)]


RECORD: dict[str, Any] = {
    "storm": {"id": "s9-01", "kind": "hot_shard"},
    "arm": "script",
    "model": "m",
    "script_order": ["backoff", "cap", "both"],
    "incident": "s9-01-abc",
    "rate": 80,
    "wait_s": 30.0,
    "hold_s": 30.0,
    "t_fault": 20.0,
    "t_act": 35.0,
    "loop_clock_offset_s": 35.5,
    "series": _series(20.0, 80.0, 160.0),
    "loop": {
        "ended": "resolved",
        "reason": "resolved on try 2",
        "resolved": True,
        "tries": [
            {
                "number": 1,
                "actions": [BACKOFF],
                "applied": [{"action": BACKOFF, "ok": True, "detail": "200", "undo": BACKOFF_OFF}],
                "t_act": 0.1,
                "verdict": "not verified",
                "recovered_at": None,
                "undone": [{"action": BACKOFF_OFF, "ok": True, "detail": "200", "undo": None}],
                "reasons": [],
            },
            {
                "number": 2,
                "actions": [CAP],
                "applied": [{"action": CAP, "ok": True, "detail": "200", "undo": CAP_OFF}],
                "t_act": 30.3,
                "verdict": "resolved",
                "recovered_at": 70.0,
                "undone": [],
                "reasons": [],
            },
        ],
        "kept": [CAP],
        "safe_undos": [],
    },
    "budget": None,
    "reasons": None,
    "recovered": True,
    "agree": True,
    "restored": False,
    "invalid": [],
    "trace_id": "abc123",
}


def record_to(events: list) -> Any:
    """An emitter that keeps (kind, data). `kind` is positional-only, as the console's is: data has a `kind` too."""

    def emit(kind: str, /, **data: Any) -> None:
        events.append((kind, data))

    return emit


def names_to(out: list) -> Any:
    """An emitter that keeps only the kinds."""

    def emit(kind: str, /, **data: Any) -> None:
        out.append(kind)

    return emit


def test_a_console_storm_is_one_of_its_kind_drawn_from_its_seed() -> None:
    for kind in ("retry_storm", "hot_shard", "compound"):
        storm = pick_storm(kind, 5)
        assert storm.kind == kind and storm.id == "console-5" and pick_storm(kind, 5) == storm
    assert loop_config(LoopConfig(wait_s=30, hold_s=30), "model-1").tries == 1
    assert loop_config(LoopConfig(wait_s=30, hold_s=30), "script").tries == 3


def test_requests_are_strict() -> None:
    with pytest.raises(ValidationError):
        StormRequest.model_validate({"kind": "hot_shard", "decider": "model-loop", "extra": 1})
    with pytest.raises(ValidationError):
        StormRequest.model_validate({"kind": "flood", "decider": "script"})
    for bad in ({"file": "../x.json", "run": 0}, {"file": "experiment-20261126T075824Z.json", "run": "0"}):
        with pytest.raises(ValidationError):
            ReplayRequest.model_validate(bad)


def test_a_live_storm_emits_the_drivers_events_then_the_loops_on_the_storms_clock_then_the_verdict() -> None:
    events: list[tuple[str, dict]] = []
    loop_events = [
        {"kind": "try_started", "t": 0.2, "data": {"try": 1}},
        {"kind": "decided", "t": 6.0, "data": {"try": 1, "actions": ["backoff(enabled=True)"]}},
        {"kind": "verdict", "t": 70.0, "data": {"try": 1, "verdict": "resolved", "recovered_at": 50.0}},
    ]
    reads = iter([None, (loop_events[:1], False), (loop_events[1:], True)])

    def run(storm, model, config, *, arm, script, notify, until_s):
        assert until_s == 240.0  # measured over the experiment's window
        notify("storm_phase", phase="fault", at=20.0)
        notify("incident_sent", incident="i1", at=35.0)
        time.sleep(0.05)  # the poller reads while the incident runs
        return {**RECORD, "arm": arm}

    poller = Poller(record_to(events), lambda i, n: next(reads, ([], True)), lambda s: None)
    drive(
        StormRequest(kind="hot_shard", decider="script"),
        record_to(events),
        seed=3,
        prepare=lambda: ("m", LoopConfig(wait_s=30, hold_s=30)),
        run=run,
        poller=poller,
    )
    kinds = [k for k, _ in events]
    assert kinds[0] == "storm_started" and kinds[-1] == "storm_finished"
    assert kinds.index("incident_sent") < kinds.index("try_started") < kinds.index("decided")
    started = events[0][1]
    assert started["decider"] == "script" and sorted(started["order"]) == ["backoff", "both", "cap"]
    assert started["model"] is None and started["replay"] is False
    decided = next(d for k, d in events if k == "decided")
    assert decided["at"] == 41.0  # sent at 35.0, plus 6.0 on the loop's clock
    verdict = next(d for k, d in events if k == "verdict")
    assert (verdict["at"], verdict["recovered_at"]) == (105.0, 85.0)
    assert events[-1][1]["recovered"] is True and events[-1][1]["trace_id"] == "abc123"


def test_a_storm_that_cannot_start_says_why_and_ends() -> None:
    events: list[tuple[str, dict]] = []
    drive(StormRequest(kind="compound", decider="model-loop"), record_to(events), prepare=lambda: "the vault is sealed")
    assert events == [("storm_failed", {"error": "the vault is sealed"})]

    def boom(*a, **k):
        raise ConnectionError("loop container down")

    events.clear()
    drive(
        StormRequest(kind="compound", decider="model-loop"),
        record_to(events),
        prepare=lambda: ("m", LoopConfig(wait_s=30, hold_s=30)),
        run=boom,
    )
    assert [k for k, _ in events] == ["storm_started", "storm_failed"]
    assert events[-1][1]["error"] == "ConnectionError: loop container down"


def test_a_failed_read_of_the_loop_is_counted_and_never_stops_the_storm() -> None:
    def fetch(incident, start):
        raise OSError("refused")

    poller = Poller(record_to([]), fetch, lambda s: None)
    assert poller.drain("i1", 0.0) is False and poller.errors == 1


def test_a_saved_run_replays_as_the_same_vocabulary_in_the_order_it_happened() -> None:
    events = replay_events(RECORD)
    kinds = [k for _, k, _ in events if k != "customers"]
    assert kinds == [
        "storm_started", "storm_phase", "storm_phase", "storm_phase", "incident_sent",
        "try_started", "decided", "checked", "applied", "waiting", "applied", "verdict",
        "try_started", "decided", "checked", "applied", "waiting", "waiting", "verdict", "incident_ended",
        "storm_finished",
    ]  # fmt: skip
    times = [at for at, _, _ in events]
    assert times == sorted(times)
    applied = [d for _, k, d in events if k == "applied"]
    assert [(a["action"], a["undo_of"]) for a in applied] == [
        ("backoff(enabled=True)", None),
        ("backoff(enabled=False)", "backoff(enabled=True)"),
        ("rate_cap(key=acct-0001, rps=10.0)", None),
    ]
    undo_at = next(at for at, k, d in events if k == "applied" and d["undo_of"])
    assert undo_at == pytest.approx(35.5 + 0.1 + 30)  # after its WAIT
    assert events[0][2]["replay"] is True and events[0][2]["order"] == ["backoff", "cap", "both"]
    assert events[0][2]["tries"] == 3  # allowed for the arm, as a live storm says
    resolved = next(d for _, k, d in events if k == "verdict" and d["verdict"] == "resolved")
    assert resolved["recovered_at"] == 105.5  # 70.0 on the loop's clock, sent at 35.5
    assert sum(1 for _, k, _ in events if k == "customers") == 160


def test_finished_measures_customer_minutes_from_the_record() -> None:
    done = finished(RECORD)
    # 60 seconds half failing (20 to 80) at 80 a second: 2400 failed requests, 30 customer minutes at 80 a second
    assert done["customer_minutes"] == pytest.approx(0.5, abs=0.02)
    assert (done["recovered"], done["resolved"], done["spent_usd"]) == (True, True, None)


def test_saved_runs_lists_only_replayable_runs_and_load_refuses_anything_else(tmp_path: Path) -> None:
    name = "experiment-20261126T075824Z.json"
    (tmp_path / name).write_text(json.dumps({"runs": [{"storm": RECORD["storm"], "arm": "nobody"}, RECORD]}))
    (tmp_path / "notes.json").write_text(json.dumps({"runs": [RECORD]}))
    assert saved_runs(tmp_path) == [
        {"file": name, "run": 1, "storm": "s9-01", "kind": "hot_shard", "arm": "script", "resolved": True}
    ]
    assert load_run(ReplayRequest(file=name, run=1), tmp_path)["trace_id"] == "abc123"
    for run in (0, 2):
        with pytest.raises(ValueError, match="no replayable run"):
            load_run(ReplayRequest(file=name, run=run), tmp_path)
    with pytest.raises(ValueError, match="no saved results file"):
        load_run(ReplayRequest(file="oncall-20261126T075824Z.json", run=0), tmp_path)


def test_a_replay_stops_between_events() -> None:
    out: list[str] = []
    calls = iter([False, False, True])
    play(replay_events(RECORD), names_to(out), 10, lambda: next(calls), lambda s: None)
    assert out == ["storm_started", "storm_phase", "storm_failed"]


def _hub_stream(hub: RunHub) -> list[dict]:
    """Every event of the hub's current run, as the page receives them."""
    events: list = []
    done, deadline = False, time.monotonic() + 10
    while not done and time.monotonic() < deadline:
        new, done = hub.wait_for(len(events), 1.0)
        events += new
    assert done, "the run did not end"
    return [wire(e) for e in events]


def test_the_console_replays_a_saved_run_as_loop_events_and_refuses_one_it_does_not_have() -> None:
    def load(req: ReplayRequest) -> dict:
        if req.run != 1:
            raise ValueError("no replayable run")
        return RECORD

    hub = RunHub(live_models=None, sleep=lambda s: None, load_saved=load, saved_runs=lambda: [])
    with pytest.raises(ValueError):
        hub.start_replay(ReplayRequest(file="experiment-20261126T075824Z.json", run=0))
    hub.start_replay(ReplayRequest(file="experiment-20261126T075824Z.json", run=1, speed=60))
    events = _hub_stream(hub)
    assert {e["source"] for e in events} == {"loop"}
    assert events[-1]["kind"] == "storm_finished" and events[-1]["data"]["at"] == 160.0


def test_the_console_drives_a_storm_through_its_driver() -> None:
    def driver(req: StormRequest, emit) -> None:
        emit("storm_started", storm="console-1", kind=req.kind, decider=req.decider, replay=False)
        emit("storm_finished", recovered=False)

    hub = RunHub(live_models=None, storm_driver=driver)
    hub.start_storm(StormRequest(kind="compound", decider="model-1"))
    events = _hub_stream(hub)
    assert [(e["source"], e["kind"]) for e in events] == [("loop", "storm_started"), ("loop", "storm_finished")]
    assert hub.stop() is False  # a live storm is the loop's to end


@pytest.mark.skipif(shutil.which("node") is None, reason="needs Node")
def test_a_replayed_storm_plays_through_the_page_and_ends_on_the_hosts_verdict(tmp_path: Path) -> None:
    """Every event reaches the page's animation; the result panel shows both verdicts and the trace."""
    hub = RunHub(live_models=None, sleep=lambda s: None, load_saved=lambda req: RECORD, saved_runs=lambda: [])
    hub.start_replay(ReplayRequest(file="experiment-20261126T075824Z.json", run=1, speed=60))
    events = _hub_stream(hub)
    for i, e in enumerate(events):  # the page plays by `t`; space them so the stub keeps order
        e["t"] = i * 0.002
    path = tmp_path / "events.json"
    path.write_text(json.dumps(events))
    env = {k: v for k, v in os.environ.items() if k != "NODE_OPTIONS"}  # as test_page_truth runs the page
    out = subprocess.run(
        ["node", str(REPLAY), str(STORM_PAGE), str(path), "1", "result,note,title", "reduced"],
        capture_output=True,
        text=True,
        timeout=60,
        env=env,
    )
    assert out.returncode == 0, out.stderr
    shown = json.loads(out.stdout)
    assert len(shown["rows"]) == len(events)
    result = shown["text"]["result"]
    assert result[result.index("Customers") + 1] == "recovered and held"
    assert result[result.index("Loop says") + 1] == "resolved"
    assert result[result.index("Trace") + 1] == "abc123"
    assert shown["text"]["note"].startswith("Incident resolved: resolved on try 2.")
    assert shown["text"]["title"] == "hot shard, script, no model (backoff, cap, both), replay of a saved run"


def test_a_storm_that_fails_after_the_incident_was_sent_stops_its_reader() -> None:
    events: list[tuple[str, dict]] = []
    reads: list[int] = []

    def fetch(incident, start):
        reads.append(start)
        return [], False  # the loop never says it is over

    poller = Poller(record_to(events), fetch, lambda s: time.sleep(0.01))

    def run(storm, model, config, *, arm, script, notify, until_s):
        notify("incident_sent", incident="i1", at=35.0)
        time.sleep(0.05)
        raise TimeoutError("the loop did not answer")

    drive(
        StormRequest(kind="hot_shard", decider="model-loop"),
        record_to(events),
        prepare=lambda: ("m", LoopConfig(wait_s=30, hold_s=30)),
        run=run,
        poller=poller,
    )
    assert events[-1] == ("storm_failed", {"error": "TimeoutError: the loop did not answer"})
    assert poller._thread is not None and not poller._thread.is_alive()
    count = len(reads)
    time.sleep(0.05)
    assert len(reads) == count  # nobody reads after the storm ended


def test_nothing_a_storm_emits_after_its_end_reaches_the_stream() -> None:
    def driver(req: StormRequest, emit) -> None:
        emit("storm_started", storm="console-1", kind=req.kind, decider=req.decider, replay=False)
        emit("storm_finished", recovered=True)
        emit("customers", at=300.0, ok=10, failed=0)  # a scraper thread still running

    hub = RunHub(live_models=None, storm_driver=driver)
    hub.start_storm(StormRequest(kind="compound", decider="script"))
    assert [e["kind"] for e in _hub_stream(hub)] == ["storm_started", "storm_finished"]


def test_a_saved_run_of_another_shape_is_refused_not_crashed() -> None:
    broken = {**RECORD, "storm": "not a mapping"}
    hub = RunHub(live_models=None, load_saved=lambda req: broken, saved_runs=lambda: [])
    with pytest.raises(ValueError, match="cannot be replayed"):
        hub.start_replay(ReplayRequest(file="experiment-20261126T075824Z.json", run=1))
    assert not hub.has_run()


def test_saved_runs_are_read_once_until_the_file_changes(tmp_path: Path) -> None:
    name = "oncall-20261126T075824Z.json"
    path = tmp_path / name
    path.write_text(json.dumps({"runs": [RECORD]}))
    assert len(saved_runs(tmp_path)) == 1
    stat = path.stat()
    path.write_text(json.dumps({"runs": [RECORD, RECORD]}))
    os.utime(path, (stat.st_atime, stat.st_mtime))  # same modified time: the listing is kept
    assert len(saved_runs(tmp_path)) == 1
    os.utime(path, (stat.st_atime, stat.st_mtime + 5))
    assert len(saved_runs(tmp_path)) == 2
    (tmp_path / "experiment-20261126T075825Z.json").write_text(json.dumps({"runs": [{**RECORD, "wait_s": None}]}))
    assert len(saved_runs(tmp_path)) == 2  # a run missing what the replay reads is not listed


def test_customer_minutes_count_only_the_experiments_window() -> None:
    long = {**RECORD, "series": _series(20.0, 400.0, 400.0)}  # failing until 400 s
    window = finished(long)["customer_minutes"]
    assert window == pytest.approx(0.5 * 80 * 240 / 60 / 80, abs=0.03)  # half failing for 240 s, not 380 s


def test_a_safe_state_replays_its_hand_off_before_the_end_and_the_verdict_says_who_was_told() -> None:
    loop = {**RECORD["loop"], "ended": "safe state", "reason": "out of tries", "resolved": False, "kept": []}
    alert = {"alerts": [{"annotations": {"summary": "Incident s9-01-abc ended in the safe state: out of tries"}}]}
    handed = {
        **RECORD,
        "loop": loop,
        "handoff": {"delivered": True, "to": "http://inbox", "detail": "HTTP 200", "alert": alert},
    }
    kinds = [kind for _, kind, _ in replay_events(handed)]
    assert kinds.index("handoff") == kinds.index("incident_ended") - 1
    shown = next(data for _, kind, data in replay_events(handed) if kind == "handoff")
    assert shown["delivered"] is True and shown["summary"].startswith("Incident s9-01-abc")
    assert finished(handed)["handoff"] == "delivered"
    assert finished({**RECORD, "loop": loop})["handoff"] == "not recorded"  # saved before the hand-off existed
    assert finished(RECORD)["handoff"] is None  # resolved: nobody needs telling


def test_a_run_that_ends_before_the_window_is_labelled_with_what_was_measured() -> None:
    short = {**RECORD, "series": _series(20.0, 80.0, 120.0)}  # ends 100 s after the fault
    assert finished(short)["window_s"] == 100.0
    assert finished(RECORD)["window_s"] == 140.0  # the shared record ends 140 s after the fault too
