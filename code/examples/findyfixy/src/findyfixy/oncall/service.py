"""The on-call agent and loop as services, each in its own container (ADR 0008, step 4).

    host: experiment runner ── POST /incident ──► loop container (findyfixy-loop)
                                                  │ scraper of what customers saw, policy, signer, edge
                                                  └─ POST /decide ──► agent container (findyfixy-agent)
                                                                       │ the model call and its budget
                                                                       └─► Ollama on the host

    agent: no Docker socket, no keys, no route to simprod; it gets the metrics as text, returns a proposal
    loop:  no Docker socket; a pass that may only sign; reaches the edge and the vault

Why the loop runs one incident per request: the host keeps the storm (reset,
inject, clear) because that needs the Docker socket, and asks the loop to handle
the incident from the moment the fault is detected. The loop scrapes what
customers saw itself, so its verdict comes from its own measurement; the host
scrapes too, and the two verdicts are compared (`agree`).

Times in the loop's answer are on the loop's clock, from the start of the
request; the host adds the time it sent the request.

Each incident's steps are also events (`wyld3rness.loop.watch`), kept for the
latest incidents and served at `GET /incident/events?incident=<id>&from=<n>`,
so a viewer (the console) can show a try as it happens. Only the latest
`KEEP_INCIDENTS` are kept, in memory: this is a view, the answer is the record.

Configuration comes from the environment, read in `main`: AGENT_URL, SIMPROD_EDGE,
BAO_ADDR and BAO_SIGN_FILE for the loop; OLLAMA_HOST (read by the model client)
for the agent. The model, the budget and the wait come with each incident from
the host, so the agent decides with what this run asked for.
"""

from __future__ import annotations

import argparse
from collections.abc import Callable
from dataclasses import asdict
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import os
from pathlib import Path
import sys
import threading
import time
from typing import Any
from urllib.parse import parse_qs, urlparse

from findyfixy.llm import ollama_reply
from findyfixy.oncall.decide import MAX_OUTPUT_TOKENS, Decision, ModelDecider
from findyfixy.oncall.env import SimprodEnv
from findyfixy.oncall.policy import rules
from findyfixy.oncall.script import plan
from findyfixy.paths import PRICES
from findyfixy.settings import read_tracing
from simprod.checker import Scraper
from wyld3rness.loop.budget import Budget
from wyld3rness.loop.credential import TransitSigner
from wyld3rness.loop.policy import Policy
from wyld3rness.loop.remote import RemoteDecider, serve_decider
from wyld3rness.loop.run import TRIES, Decider, LoopConfig, Outcome, run_incident
from wyld3rness.loop.traced import traced
from wyld3rness.loop.watch import EventLog, Watch
from wyld3rness.trace.command import CommandTrace
from wyld3rness.trace.prices import parse_prices
from wyld3rness.trace.tracing import Tracing

JSON = dict[str, Any]
PORT = 8000
KEEP_INCIDENTS = 8  # events kept for this many latest incidents; a viewer reads them while they run


class Incidents:
    """The event logs of the latest incidents, by id. Thread-safe: the loop writes, a viewer reads."""

    def __init__(self, keep: int = KEEP_INCIDENTS) -> None:
        """Keep at most `keep` incidents; the oldest is dropped first."""
        self._lock = threading.Lock()
        self._logs: dict[str, EventLog] = {}
        self.keep = keep

    def open(self, incident: str) -> EventLog:
        """A new log for `incident`, replacing any with the same id."""
        with self._lock:
            self._logs.pop(incident, None)
            self._logs[incident] = log = EventLog()
            while len(self._logs) > self.keep:
                del self._logs[next(iter(self._logs))]
            return log

    def get(self, incident: str) -> EventLog | None:
        """The log of `incident`, or None when it is unknown or was dropped."""
        with self._lock:
            return self._logs.get(incident)


def ask_decision(model: str, prompt: str, max_output_tokens: int) -> str:
    """The real model call for the decider: constrained to `Decision`, capped, validated by the decider."""
    return ollama_reply(model, prompt, Decision, max_output_tokens)


def outcome_json(outcome: Outcome) -> JSON:
    """The loop's outcome as plain JSON, with `resolved` spelled out. Pure."""
    return {**asdict(outcome), "resolved": outcome.resolved}


def make_agent(model: str, budget_usd: float, wait_s: float) -> ModelDecider:
    """One incident's decider: the model, a fresh budget, the policy's rules for its prompt."""
    if not model or budget_usd <= 0 or wait_s <= 0:
        raise ValueError(f"an incident needs a model, a budget and a wait, got {model!r}, {budget_usd}, {wait_s}")
    notes = Policy(rules()).notes()  # the notes are static text; the rules' live checks run in the loop
    budget = Budget(budget_usd, parse_prices(PRICES.read_text()))
    return ModelDecider(ask_decision, model, wait_s, budget, notes=notes, max_output_tokens=MAX_OUTPUT_TOKENS)


def handle_incident(
    body: JSON,
    *,
    agent_url: str,
    edge: str,
    signer: TransitSigner,
    tracing: Tracing | None = None,
    log: EventLog | None = None,
) -> JSON:
    """Run one incident (`_handle`); its event log is closed however it ends, a refused request included.

    A viewer reads the log until it is closed, so a log left open by a 400
    would be read for ever.
    """
    try:
        return _handle(body, agent_url=agent_url, edge=edge, signer=signer, tracing=tracing, log=log)
    finally:
        if log is not None:
            log.close()


def _handle(
    body: JSON,
    *,
    agent_url: str,
    edge: str,
    signer: TransitSigner,
    tracing: Tracing | None = None,
    log: EventLog | None = None,
) -> JSON:
    """Run one incident through the loop, measured by this process's own scraper, and report it.

    `decider` is "model" (the agent container, the default) or "script" (FindyFixy's
    script with no model, in the `order` given, built here: it needs no agent).
    `tries` defaults to the loop's own. Everything else is the same for both, so
    an experiment's arms differ only in who proposes.

    With `tracing`, the incident is one trace, named for its arm, with a span for
    every observation, decision (and its model calls), policy check, signature,
    action and wait (`wyld3rness.loop.traced`); its id goes back in the answer.
    With `log`, every step is also an event in it (`wyld3rness.loop.watch`).
    """
    incident = str(body["incident"])
    tries = int(body.get("tries", TRIES))
    if not 1 <= tries <= TRIES:
        raise ValueError(f"tries must be 1 to {TRIES}, got {tries}")
    config = LoopConfig(wait_s=float(body["wait_s"]), hold_s=float(body["hold_s"]), tries=tries)
    params = {"model": str(body["model"]), "budget_usd": float(body["budget_usd"]), "wait_s": config.wait_s}
    # Everything a bad request can fail on is read before the scraper's thread starts, so a 400 leaves no thread.
    decider, record = choose_decider(body, agent_url, incident, params)
    arm = str(body.get("arm", "loop"))
    scraper = Scraper(f"{edge}/customers/metrics")
    stop = threading.Event()

    def scrape() -> None:
        """Once a second until the incident ends. Nothing ends it early: a lost series would lose the verdict."""
        while not stop.wait(1.0):
            try:
                scraper.scrape()
            except Exception:  # scrape counts the failures it expects; this counts the rest
                scraper.errors += 1

    threading.Thread(target=scrape, daemon=True).start()
    env = SimprodEnv(scraper.series, lambda: time.monotonic() - scraper.start, edge=edge)
    about = {"incident": incident, "arm": arm, "decider": body.get("decider", "model"), "tries": tries}
    with CommandTrace(tracing, f"incident {arm}", input=about | {"order": body.get("order")}) as trace:
        try:
            watch = Watch(log.emit if log is not None else lambda kind, t, data: None)
            ports = traced(trace.tracer, *watch.ports(env, decider, Policy(rules(edge=edge)), signer))
            outcome = run_incident(ports[0], ports[1], config, ports[2], ports[3])
            watch.finish(outcome)
        finally:
            stop.set()
        agent = record()
        trace.set(
            **{f"loop.{k}": v for k, v in about.items()},
            **{"loop.ended": outcome.ended, "loop.reason": outcome.reason, "loop.tries_used": len(outcome.tries)},
            **{"loop.spent_usd": (agent.get("budget") or {}).get("spent_usd")},
        )
        trace.output({"ended": outcome.ended, "reason": outcome.reason, "kept": [a.label() for a in outcome.kept]})
    return {
        "loop": outcome_json(outcome),
        "sent": env.sent,
        "agent": agent,
        "scrape_errors": scraper.errors,
        "trace_id": trace.trace_id,
    }


def choose_decider(body: JSON, agent_url: str, incident: str, params: JSON) -> tuple[Decider, Callable[[], JSON]]:
    """The incident's decider and how to read its record afterwards. Raises ValueError for an unknown one.

    The script makes no model call, so its record says so, measured: no calls and nothing spent.
    """
    kind = body.get("decider", "model")
    if kind == "model":
        remote = RemoteDecider(agent_url, incident, params)
        return remote, remote.record
    if kind == "script":
        order = [str(step) for step in body["order"]]
        budget = {"limit_usd": params["budget_usd"], "spent_usd": 0.0}
        return plan(order), lambda: {"decider": "script", "order": order, "model_calls": [], "budget": budget}
    raise ValueError(f"decider must be 'model' or 'script', got {kind!r}")


def events_answer(incidents: Incidents, path: str) -> tuple[int, JSON]:
    """The answer to `GET /incident/events?incident=<id>&from=<n>`: the events from n on, and whether it is over.

    404 for an incident not (or no longer) kept: the viewer then waits for the
    host's answer, which holds the whole record. 400 for a missing id or a
    negative or non-integer `from`.
    """
    query = parse_qs(urlparse(path).query)
    incident = (query.get("incident") or [""])[0]
    try:
        start = int((query.get("from") or ["0"])[0])
    except ValueError:
        start = -1
    if not incident or start < 0:
        return 400, {"error": "bad request: incident=<id> and from=<n >= 0> are required"}
    log = incidents.get(incident)
    if log is None:
        return 404, {"error": f"no events kept for incident {incident!r}"}
    events, done = log.since(start)
    return 200, {"incident": incident, "events": events, "done": done}


def serve_loop(agent_url: str, edge: str, vault: str, sign_file: Path) -> None:
    """POST /incident runs one incident; GET /healthz answers ok. One incident at a time is what the runner sends.

    GET /incident/events serves the steps of the latest incidents as they happen (`events_answer`).

    The sign pass is read for every incident, so passes renewed by `make sim-up`
    need no restart of this container. Tracing starts once, from the environment
    (`read_tracing`: LANGFUSE_HOST and the project's keys); without keys it is off
    and the loop runs the same. Spans leave on a background thread, so a slow or
    stopped backend never slows an incident.
    """
    config = read_tracing()
    tracing = Tracing.start(config) if config is not None else None
    incidents = Incidents()
    print(json.dumps({"tracing": config.host if config else "off"}), flush=True)

    class Handler(BaseHTTPRequestHandler):
        """The loop's two routes."""

        def _send(self, status: int, body: JSON) -> None:
            """Answer with `status` and `body` as JSON."""
            data = json.dumps(body).encode()
            self.send_response(status)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)

        def do_GET(self) -> None:
            """GET /healthz, or GET /incident/events."""
            if self.path == "/healthz":
                self._send(200, {"status": "ok"})
            elif urlparse(self.path).path == "/incident/events":
                self._send(*events_answer(incidents, self.path))
            else:
                self._send(404, {"error": "not found"})

        def do_POST(self) -> None:
            """POST /incident: run one incident and answer with its record."""
            if self.path != "/incident":
                return self._send(404, {"error": "not found"})
            try:
                body = json.loads(self.rfile.read(int(self.headers.get("Content-Length") or 0)))
                signer = TransitSigner(vault, sign_file.read_text().strip())
                log = incidents.open(str(body["incident"]))  # a body with no id is a 400, as before
                answer = handle_incident(body, agent_url=agent_url, edge=edge, signer=signer, tracing=tracing, log=log)
                return self._send(200, answer)
            except (KeyError, TypeError, ValueError) as exc:
                return self._send(400, {"error": f"bad request: {exc}"})
            except OSError as exc:  # the sign pass cannot be read: answer with the reason, never drop the connection
                return self._send(500, {"error": f"{type(exc).__name__}: {exc}"})

        def log_message(self, format: str, *args: object) -> None:
            """Quiet: the runner keeps the record."""

    ThreadingHTTPServer(("0.0.0.0", PORT), Handler).serve_forever()


def main(argv: list[str] | None = None) -> int:
    """`agent` or `loop`, configured from the environment (see the module docstring)."""
    parser = argparse.ArgumentParser(prog="findyfixy-oncall-service", description=__doc__.splitlines()[0])
    parser.add_argument("role", choices=["agent", "loop"])
    args = parser.parse_args(argv)
    if args.role == "agent":
        print(json.dumps({"service": "agent"}), flush=True)
        serve_decider(
            lambda incident, p: make_agent(
                str(p.get("model", "")), float(p.get("budget_usd", 0)), float(p.get("wait_s", 0))
            ),
            "0.0.0.0",
            PORT,
        ).serve_forever()
    else:
        print(json.dumps({"service": "loop", "agent": os.environ["AGENT_URL"]}), flush=True)
        serve_loop(
            os.environ["AGENT_URL"],
            os.environ["SIMPROD_EDGE"],
            os.environ["BAO_ADDR"],
            Path(os.environ["BAO_SIGN_FILE"]),
        )
    return 0


if __name__ == "__main__":
    sys.exit(main())
