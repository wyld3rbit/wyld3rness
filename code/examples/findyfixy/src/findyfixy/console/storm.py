"""The console's storm: drive one storm and show every step as it happens, or replay a saved one.

    live    POST /storm  {"kind", "decider"}
              storm driver (cli.run_one): reset, warm-up, fault, detection ──► storm_phase, customers
              loop container handles the incident ──► GET /incident/events, polled ──► try_started ... incident_ended
              host's verdict ──► storm_finished
    replay  POST /replay {"file", "run", "speed"}
              a saved run's record ──► the same events, in order, at `speed` times real time

Both paths emit one vocabulary (`wyld3rness.loop.watch.LoopEventKind`), so the
page has one way to draw a storm. Every event carries `at`, its time on the
storm's clock (seconds since the storm's scraper started), so the page can put
the loop's steps on the same axis as what customers saw; a verdict's
`recovered_at` is moved to that clock too.

Why the console polls the loop container instead of the loop pushing: the loop
container has no route to the console and needs none; a viewer that reads is
one more reader of a log, never a dependency of the incident. A step shows up
at most one poll interval (`POLL_S`) after it happened.

Why replay rebuilds events from the record and not from a recording of the
stream: the record is what the experiment kept and checked, so a replayed run
is the measured run. The record has no time for the moment a decision started,
so in a replay `try_started` is placed when the previous try ended, and
`decided` just before the try's first action. A try that ended "no undo" does
not record whether its hold ran, so its verdict is placed after WAIT. Everything
else is at its recorded time. A replay says it is one (`storm_started.replay`).

One storm at a time, like every console run: a storm takes about 4 minutes and
the loop container takes one incident at a time anyway. A live storm cannot be
stopped from the console: an incident in flight has changes in force, and only
the loop may put them back. A replay can be stopped.
"""

from __future__ import annotations

from collections.abc import Callable, Iterator, Mapping, Sequence
from dataclasses import dataclass, replace
import json
from pathlib import Path
import random
import re
import threading
import time
from typing import Any, Literal
import urllib.error
import urllib.request

from pydantic import BaseModel, ConfigDict, Field, StrictInt

from findyfixy.oncall import cli
from findyfixy.oncall.experiment import HORIZON_S
from findyfixy.oncall.script import STEPS
from simprod import controls, loadtest
from simprod.faults import KINDS, Storm, storms
from wyld3rness.loop.env import Action
from wyld3rness.loop.handoff import summary as handoff_summary
from wyld3rness.loop.run import HOLD_MARGIN_S, TRIES, LoopConfig
from wyld3rness.loop.verdict import Second, customer_minutes
from wyld3rness.loop.watch import LoopEventKind

JSON = dict[str, Any]
StormKind = Literal["retry_storm", "hot_shard", "compound"]
DeciderArm = Literal["model-loop", "model-1", "script"]
POLL_S = 1.0  # how often the loop container's events are read during a live storm
RESULTS = loadtest.RESULTS  # where the experiment and the on-call runs save their records
_SAVED = re.compile(r"^(experiment|oncall)-\d{8}T\d{6}Z\.json$")  # files a replay may read; nothing else


@dataclass(frozen=True)
class LoopEvent:
    """One storm event on its way to the stream. The app's `Event` and the harness's `HarnessEvent` have this shape."""

    kind: LoopEventKind
    t: float
    data: dict[str, Any]


class StormRequest(BaseModel):
    """The body of POST /storm, validated before anything starts. Unknown fields are refused."""

    model_config = ConfigDict(extra="forbid")

    kind: StormKind
    decider: DeciderArm


class ReplayRequest(BaseModel):
    """The body of POST /replay: a saved results file (by name only), a run's index in it, and the speed."""

    model_config = ConfigDict(extra="forbid")

    file: str = Field(pattern=_SAVED.pattern)
    run: StrictInt = Field(ge=0)
    speed: StrictInt = Field(default=10, ge=1, le=60)


Emit = Callable[..., None]  # emit(kind, **data), stamped by the console


def pick_storm(kind: str, seed: int) -> Storm:
    """A storm of `kind` drawn from `seed` by the experiment's own generator, so it is one of its kind. Pure."""
    index = KINDS.index(kind)
    storm = storms(index + 1, controls.RATE, seed)[index]
    return replace(storm, id=f"console-{seed}")


def loop_config(base: LoopConfig, decider: str) -> LoopConfig:
    """The loop's windows with the arm's number of tries: one for `model-1`, the loop's for the others. Pure."""
    return LoopConfig(wait_s=base.wait_s, hold_s=base.hold_s, tries=1 if decider == "model-1" else TRIES)


def fetch_events(incident: str, start: int) -> tuple[list[JSON], bool] | None:
    """The loop container's events for `incident` from `start` on, and whether it is over; None when not kept yet."""
    url = f"{cli.LOOP_URL}/incident/events?incident={incident}&from={start}"
    try:
        with urllib.request.urlopen(url, timeout=5) as r:
            body = json.load(r)
    except urllib.error.HTTPError as e:
        if e.code == 404:  # the incident is not open yet, or was dropped: the host's answer has the record
            return None
        raise
    return list(body["events"]), bool(body["done"])


def on_storm_clock(data: Mapping[str, Any], sent_at: float) -> JSON:
    """An event's data with its recovery time moved from the incident's clock to the storm's. Pure."""
    if data.get("recovered_at") is None:
        return dict(data)
    return {**data, "recovered_at": round(sent_at + float(data["recovered_at"]), 2)}


class Poller:
    """Reads one incident's events from the loop container until it is over, re-stamped on the storm's clock."""

    def __init__(
        self,
        emit: Emit,
        fetch: Callable[[str, int], tuple[list[JSON], bool] | None] = fetch_events,
        sleep: Callable[[float], None] = time.sleep,
    ) -> None:
        """`emit` receives each event; `fetch` reads the loop container."""
        self.emit, self.fetch, self.sleep = emit, fetch, sleep
        self.seen = 0
        self.errors = 0
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None

    def start(self, incident: str, sent_at: float) -> None:
        """Poll in a thread from now on; `sent_at` is when the incident was sent, on the storm's clock."""
        self._thread = threading.Thread(target=self._run, args=(incident, sent_at), daemon=True)
        self._thread.start()

    def _run(self, incident: str, sent_at: float) -> None:
        """Until the incident is over or `finish` is called: read what is new, emit it, wait a poll."""
        while True:
            done = self.drain(incident, sent_at)
            if done or self._stop.is_set():
                return
            self.sleep(POLL_S)

    def drain(self, incident: str, sent_at: float) -> bool:
        """Emit every event not yet emitted; True when the loop says the incident is over. Errors are counted."""
        try:
            got = self.fetch(incident, self.seen)
        except Exception:  # the view is not the record: a failed read is counted, the storm goes on
            self.errors += 1
            return False
        if got is None:
            return False
        events, done = got
        for e in events:
            self.emit(e["kind"], **on_storm_clock(e["data"], sent_at), at=round(sent_at + float(e["t"]), 2))
        self.seen += len(events)
        return done

    def finish(self, incident: str, sent_at: float) -> None:
        """After the host has the loop's answer: stop polling and read whatever is left, once."""
        self._stop.set()
        if self._thread is not None:
            self._thread.join(timeout=10)
        self.drain(incident, sent_at)


def drive(
    req: StormRequest,
    emit: Emit,
    *,
    seed: int | None = None,
    prepare: Callable[[], tuple[str, LoopConfig] | str] = lambda: cli.preflight(None),
    run: Callable[..., JSON] = cli.run_one,
    poller: Poller | None = None,
) -> None:
    """One live storm, every step emitted. Always ends with exactly one `storm_finished` or `storm_failed`.

    `prepare` is the experiment's own preflight (model, windows, budget, the
    loop container, the vault), so the console starts a storm only when a
    `make experiment` could. The script's order is drawn per storm, as in the
    experiment.
    """
    try:
        ready = prepare()
        if isinstance(ready, str):
            emit("storm_failed", error=ready)
            return
        model, base = ready
        seed = random.randrange(2**31) if seed is None else seed
        storm = pick_storm(req.kind, seed)
        order = random.Random(seed).sample(STEPS, len(STEPS)) if req.decider == "script" else []
        config = loop_config(base, req.decider)
        emit(
            "storm_started",
            storm=storm.id,
            kind=storm.kind,
            decider=req.decider,
            model=None if req.decider == "script" else model,
            order=order,
            tries=config.tries,
            wait_s=config.wait_s,
            hold_s=config.hold_s,
            replay=False,
        )
        poll = poller or Poller(emit)
        sent: JSON = {}

        def notify(kind: str, **data: Any) -> None:
            """The storm driver's events, passed on; `incident_sent` starts reading the loop's."""
            emit(kind, **data)
            if kind == "incident_sent":
                sent.update(data)
                poll.start(data["incident"], data["at"])

        try:
            record = run(storm, model, config, arm=req.decider, script=order, notify=notify, until_s=HORIZON_S)
        finally:  # whatever ended the storm, its reader stops here and reads what is left once
            if sent:
                poll.finish(sent["incident"], sent["at"])
        emit("storm_finished", **finished(record))
    except Exception as exc:  # anything the driver raises ends the stream with its reason, never silently
        emit("storm_failed", error=f"{type(exc).__name__}: {exc}")


def finished(record: Mapping[str, Any]) -> JSON:
    """The `storm_finished` data from a run's record: the host's verdict, its cost, its trace. Pure."""
    loop = record.get("loop") or {}
    budget = record.get("budget") or {}
    series = [Second(*s) for s in record.get("series") or []]
    minutes = (  # over the experiment's window, so a console storm and a replay read like its analysis
        customer_minutes(series, record["t_fault"], record["rate"], record["t_fault"] + HORIZON_S)
        if series and "t_fault" in record and "rate" in record
        else None
    )
    return {
        "recovered": record.get("recovered"),
        "resolved": loop.get("resolved"),
        "agree": record.get("agree"),
        "customer_minutes": None if minutes is None else round(minutes, 3),
        "spent_usd": budget.get("spent_usd"),
        "trace_id": record.get("trace_id"),
        "unjudged": list(record.get("invalid") or []),
        "restored": record.get("restored"),
        "handoff": handoff_status(record),
        "window_s": HORIZON_S,  # the customer minutes are counted over this many seconds after the fault
    }


# ---- replay ------------------------------------------------------------------


def handoff_status(record: Mapping[str, Any]) -> str | None:
    """Whether a person was told: None when resolved, "delivered", "not delivered", or "not recorded". Pure.

    "not recorded" is a run saved before the hand-off existed, or with no on-call URL set.
    """
    if (record.get("loop") or {}).get("resolved", True):
        return None
    handed = record.get("handoff")
    if not handed:
        return "not recorded"
    return "delivered" if handed.get("delivered") else "not delivered"


def _label(data: Mapping[str, Any]) -> str:
    """An action's label from its JSON form. Pure."""
    return Action(str(data["name"]), dict(data.get("args") or {})).label()


def replay_events(record: Mapping[str, Any]) -> list[tuple[float, str, JSON]]:
    """The events a saved run would have streamed, as (time on the storm's clock, kind, data), in order. Pure.

    Built from the record alone; see the module notes for the two times a
    record does not keep.
    """
    storm, loop = record["storm"], record.get("loop") or {}
    offset = float(record.get("loop_clock_offset_s") or record.get("t_act") or 0.0)
    wait_s, hold_s = float(record["wait_s"]), float(record["hold_s"])
    charges = ((record.get("budget") or {}).get("charges")) or []
    reasons = record.get("reasons") or []
    out: list[tuple[float, str, JSON]] = [
        (
            0.0,
            "storm_started",
            {
                "storm": storm["id"],
                "kind": storm["kind"],
                "decider": record["arm"],
                "model": None if record["arm"] in ("script", "nobody") else record.get("model"),
                "order": list(record.get("script_order") or []),
                "tries": 1 if record["arm"] == "model-1" else TRIES,  # allowed, as a live storm says
                "wait_s": wait_s,
                "hold_s": hold_s,
                "replay": True,
            },
        ),
        (0.0, "storm_phase", {"phase": "warm-up", "seconds": float(record["t_fault"])}),
        (float(record["t_fault"]), "storm_phase", {"phase": "fault", "seconds": record["t_act"] - record["t_fault"]}),
        (float(record["t_act"]), "storm_phase", {"phase": "detected"}),
    ]
    out += [(float(t), "customers", {"ok": ok, "failed": failed}) for t, ok, failed in record.get("series") or []]
    if loop:
        out.append((offset, "incident_sent", {"incident": record.get("incident")}))
        steps = list(_tries(loop, offset, wait_s, hold_s, charges, reasons))
        out += steps
        end = max([at for at, _, _ in steps] or [offset])
        if record.get("handoff"):  # sent before incident_ended, as the loop emits it
            out.append((end, "handoff", handoff_summary(record["handoff"])))
        out.append(
            (
                end,
                "incident_ended",
                {
                    "ended": loop["ended"],
                    "reason": loop["reason"],
                    "kept": [_label(a) for a in loop.get("kept") or []],
                    "safe_undos": [_label(a["action"]) for a in loop.get("safe_undos") or []],
                    "tries_used": len(loop["tries"]),
                },
            )
        )
    last = float(record["series"][-1][0]) if record.get("series") else 0.0
    out.append((last, "storm_finished", finished(record)))
    out.sort(key=lambda e: e[0])  # stable: events at one time keep the order they were built in
    return out


def _tries(
    loop: Mapping[str, Any],
    offset: float,
    wait_s: float,
    hold_s: float,
    charges: Sequence[JSON],
    reasons: Sequence[str],
) -> Iterator[tuple[float, str, JSON]]:
    """Each try's events, from the loop's record: decision, policy, actions, waits, undos, verdict. Pure."""
    started = offset
    spent = 0.0
    for i, t in enumerate(loop["tries"]):
        n, at = int(t["number"]), offset + float(t["t_act"])
        actions = [_label(a) for a in t["actions"]]
        if len(charges) == len(loop["tries"]):  # one call a decision: each try's spend is known
            spent += float(charges[i]["cost_usd"])
        yield started, "try_started", {"try": n}
        yield (
            at - 0.01,
            "decided",
            {
                "try": n,
                "actions": actions,
                "reason": reasons[i] if len(reasons) == len(loop["tries"]) else None,
                "calls": [],
                "spent_usd": round(spent, 8) if len(charges) == len(loop["tries"]) else None,
                "error": None,
            },
        )
        denied = t["verdict"] == "denied"
        yield at, "checked", {"try": n, "allowed": not denied, "reasons": list(t.get("reasons") or [])}
        for a in t["applied"]:
            yield (
                at,
                "applied",
                {"try": n, "action": _label(a["action"]), "ok": a["ok"], "detail": a["detail"], "undo_of": None},
            )
        ended = at
        if not denied and all(a["ok"] for a in t["applied"]):
            yield at, "waiting", {"seconds": wait_s}
            ended = at + wait_s
            if t["verdict"] in ("resolved", "did not hold"):
                yield ended, "waiting", {"seconds": hold_s + HOLD_MARGIN_S}
                ended += hold_s + HOLD_MARGIN_S
        for u in t.get("undone") or []:
            undone = next((a for a in t["applied"] if a.get("undo") == u["action"]), None)
            yield (
                ended,
                "applied",
                {
                    "try": n,
                    "action": _label(u["action"]),
                    "ok": u["ok"],
                    "detail": u["detail"],
                    "undo_of": _label(undone["action"]) if undone else None,
                },
            )
        yield (
            ended,
            "verdict",
            {
                "try": n,
                "verdict": t["verdict"],
                "recovered_at": None if t.get("recovered_at") is None else round(offset + float(t["recovered_at"]), 2),
                "undone": [_label(u["action"]) for u in t.get("undone") or []],
                "reasons": list(t.get("reasons") or []),
            },
        )
        started = ended


_LISTED: dict[Path, tuple[float, list[JSON]]] = {}  # file -> (modified time, its replayable runs): read once


def _replayable(r: object) -> bool:
    """A run a replay can show: a record with the loop's outcome, the series and the fields the replay reads."""
    need = ("storm", "arm", "loop", "series", "t_fault", "t_act", "wait_s", "hold_s", "rate")
    return isinstance(r, dict) and all(r.get(k) not in (None, [], {}) for k in need)


def _runs_in(path: Path) -> list[JSON]:
    """The replayable runs listed in one results file; none when it cannot be read."""
    try:
        runs = json.loads(path.read_text()).get("runs") or []
    except (OSError, ValueError, AttributeError):
        return []
    return [
        {
            "file": path.name,
            "run": i,
            "storm": r["storm"]["id"],
            "kind": r["storm"]["kind"],
            "arm": r["arm"],
            "resolved": r["loop"].get("resolved"),
        }
        for i, r in enumerate(runs)
        if _replayable(r)
    ]


def saved_runs(results: Path = RESULTS) -> list[JSON]:
    """Every saved run a replay can show: file, index, storm, kind, arm and outcome, newest file first.

    A results file holds about 100 runs with a series each, so each file is
    parsed once and kept until it changes (its modified time), not on every
    page load.
    """
    out: list[JSON] = []
    for path in sorted(results.glob("*.json"), reverse=True):
        if not _SAVED.match(path.name):
            continue
        mtime = path.stat().st_mtime
        cached = _LISTED.get(path)
        if cached is None or cached[0] != mtime:
            _LISTED[path] = cached = (mtime, _runs_in(path))
        out += cached[1]
    return out


def load_run(req: ReplayRequest, results: Path = RESULTS) -> JSON:
    """The record a replay asked for. Raises ValueError when the file or run is not one `saved_runs` lists."""
    path = results / req.file
    if not _SAVED.match(req.file) or path.parent != results or not path.is_file():
        raise ValueError(f"no saved results file {req.file!r}")
    try:
        runs = json.loads(path.read_text()).get("runs") or []
    except (OSError, ValueError, AttributeError) as exc:
        raise ValueError(f"{req.file} cannot be read: {exc}") from exc
    if req.run >= len(runs) or not _replayable(runs[req.run]):
        raise ValueError(f"{req.file} has no replayable run {req.run}")
    run: JSON = runs[req.run]
    return run


def play(
    events: Sequence[tuple[float, str, JSON]],
    emit: Emit,
    speed: int,
    should_stop: Callable[[], bool],
    sleep: Callable[[float], None] = time.sleep,
) -> None:
    """Emit `events` at `speed` times the time between them; on stop, end at once with `storm_failed`."""
    previous = events[0][0] if events else 0.0
    for at, kind, data in events:
        if should_stop():
            emit("storm_failed", error="stopped", reason="stopped")
            return
        sleep(max(0.0, at - previous) / speed)
        previous = at
        emit(kind, **data, at=round(at, 2))
