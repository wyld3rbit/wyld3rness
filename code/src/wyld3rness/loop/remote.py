"""The decider in another process: the loop asks it over HTTP, so the agent can run with nothing it could act with.

    loop process                                     agent process (no socket, no keys, no route to the system)
    RemoteDecider.decide(observation, history) ──► POST /decide {"incident", "params", "observation", "history"}
                                               ◄── {"actions": [{"name", "args"}]}
                                                   or 409 {"kind": "budget"} / 422 {"kind": "decider"}
    GET /record?incident=<id>                  ──► what the agent's decider kept (its calls, its budget)

Why a process boundary and not an import: the part that talks to the model is
the part to contain. Behind this boundary it holds no credential, no signer and
no network path to the system it would change, so even a fully fooled model can
only return a proposal, which the loop checks against the policy and signs or not.

The wire format is the loop's own types as plain JSON (`encode_try`,
`decode_try`), so the remote decider sees exactly the history an in-process one
would. A refusal by the agent's budget comes back as `BudgetExceeded`, so the loop
names it as the budget, as it does in process. Standard library only.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping, Sequence
from dataclasses import asdict
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import threading
from typing import Any, Protocol
import urllib.error
import urllib.parse
import urllib.request

from wyld3rness.loop.budget import BudgetExceeded
from wyld3rness.loop.env import Action, Applied
from wyld3rness.loop.run import Decider, Try

JSON = dict[str, Any]
TIMEOUT_S = 300.0  # a decision is a model call and its retry; a cold model load can take minutes


class RemoteDecisionError(Exception):
    """The agent could not decide (invalid after its retries, a model error, an unreachable agent)."""


def encode_action(action: Action) -> JSON:
    """An action as plain JSON. Pure."""
    return {"name": action.name, "args": dict(action.args)}


def decode_action(data: Mapping[str, Any]) -> Action:
    """An action from plain JSON; raises ValueError when it is not one. Pure."""
    name, args = data.get("name"), data.get("args", {})
    if not isinstance(name, str) or not name or not isinstance(args, dict):
        raise ValueError(f"not an action: {data!r}")
    return Action(name, args)


def encode_try(t: Try) -> JSON:
    """One try as plain JSON, the loop's own fields. Pure."""
    return asdict(t)


def _applied(data: Mapping[str, Any]) -> Applied:
    """An `Applied` from plain JSON. Pure."""
    undo = data.get("undo")
    return Applied(
        decode_action(data["action"]), bool(data["ok"]), str(data["detail"]), decode_action(undo) if undo else None
    )


def decode_try(data: Mapping[str, Any]) -> Try:
    """One try from plain JSON. Raises KeyError or ValueError when malformed. Pure."""
    return Try(
        number=int(data["number"]),
        actions=tuple(decode_action(a) for a in data["actions"]),
        applied=tuple(_applied(a) for a in data["applied"]),
        t_act=float(data["t_act"]),
        verdict=data["verdict"],
        recovered_at=data.get("recovered_at"),
        undone=tuple(_applied(a) for a in data.get("undone", ())),
        reasons=tuple(str(r) for r in data.get("reasons", ())),
    )


class RemoteDecider:
    """A `Decider` that asks the agent's service. One per incident: the agent keeps its budget by incident id.

    `params` travel with every decision (the model, the budget, the wait), so
    the agent decides with what this run asked for, not with what its image
    was started with.
    """

    def __init__(
        self, url: str, incident: str, params: Mapping[str, Any] | None = None, timeout_s: float = TIMEOUT_S
    ) -> None:
        """`url` is the agent's base URL, such as http://agent:8000; `incident` must be unique."""
        self.url, self.incident, self.timeout_s = url.rstrip("/"), incident, timeout_s
        self.params = dict(params or {})

    def decide(self, observation: str, history: Sequence[Try]) -> list[Action]:
        """POST the observation and the history; the agent's actions, or its refusal raised as the loop expects."""
        body = {
            "incident": self.incident,
            "params": self.params,
            "observation": observation,
            "history": [encode_try(t) for t in history],
        }
        req = urllib.request.Request(
            f"{self.url}/decide", json.dumps(body).encode(), {"Content-Type": "application/json"}
        )
        try:
            with urllib.request.urlopen(req, timeout=self.timeout_s) as r:
                answer = json.load(r)
        except urllib.error.HTTPError as e:
            try:
                error = json.loads(e.read() or b"{}")
            except ValueError:  # not our server's JSON: a proxy's page, the HTTP server's own error page
                raise RemoteDecisionError(f"agent answered HTTP {e.code}") from None
            if error.get("kind") == "budget":
                raise BudgetExceeded(str(error.get("error"))) from None
            raise RemoteDecisionError(str(error.get("error", e))) from None
        except OSError as exc:  # refused, reset, timed out: the agent is not there
            raise RemoteDecisionError(f"agent unreachable: {type(exc).__name__}: {exc}") from None
        return [decode_action(a) for a in answer["actions"]]

    def record(self) -> JSON:
        """What the agent kept for this incident (its calls, reasons and budget), read once at the end.

        When it cannot be read, the answer says so (`record_error`): an unknown
        cost is never stored as a measured zero.
        """
        query = urllib.parse.urlencode({"incident": self.incident})
        try:
            with urllib.request.urlopen(f"{self.url}/record?{query}", timeout=10) as r:
                answer: JSON = json.load(r)
                return answer
        except (OSError, ValueError) as exc:
            return {"record_error": f"{type(exc).__name__}: {exc}"}


class Recorded(Decider, Protocol):
    """A decider that can also say what it kept: its calls, its reasons, its budget."""

    def record(self) -> JSON:
        """Plain JSON about this decider's work so far."""
        ...


def serve_decider(make: Callable[[str, JSON], Recorded], host: str, port: int) -> ThreadingHTTPServer:
    """An HTTP server for `make`'s deciders, one per incident id, made on the incident's first decision.

    `make(incident, params)` gets the parameters the first decision carried. A
    decision that fails is answered 422 with kind "decider", a budget refusal 409
    with kind "budget"; the caller turns them back into the loop's errors. A
    decision with a history for an incident this server has not seen is refused:
    the server restarted and lost that incident's budget, and starting a new one
    would let the incident spend its limit twice. Reading an incident's record
    forgets it.
    """
    deciders: dict[str, Recorded] = {}
    lock = threading.Lock()

    class Handler(BaseHTTPRequestHandler):
        """POST /decide, GET /record, GET /healthz."""

        def _send(self, status: int, body: JSON) -> None:
            """Answer with `status` and `body` as JSON."""
            data = json.dumps(body).encode()
            self.send_response(status)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)

        def do_GET(self) -> None:
            """GET /healthz, or GET /record?incident=<id>, which also forgets the incident."""
            url = urllib.parse.urlparse(self.path)
            if url.path == "/healthz":
                return self._send(200, {"status": "ok"})
            if url.path == "/record":
                incident = urllib.parse.parse_qs(url.query).get("incident", [""])[0]
                with lock:
                    decider = deciders.pop(incident, None)
                return self._send(200, decider.record() if decider else {"record_error": "unknown incident"})
            return self._send(404, {"error": "not found"})

        def do_POST(self) -> None:
            """POST /decide: one decision for one incident."""
            if self.path != "/decide":
                return self._send(404, {"error": "not found"})
            try:
                body = json.loads(self.rfile.read(int(self.headers.get("Content-Length") or 0)))
                incident, observation = str(body["incident"]), str(body["observation"])
                params = dict(body.get("params") or {})
                history = [decode_try(t) for t in body.get("history", [])]
            except (KeyError, TypeError, ValueError) as exc:
                return self._send(400, {"error": f"bad request: {exc}", "kind": "request"})
            with lock:  # one decider, and one budget, per incident: made on its first decision only
                if incident not in deciders:
                    if history:
                        return self._send(
                            422, {"error": "unknown incident mid-way: its budget was lost", "kind": "decider"}
                        )
                    deciders[incident] = make(incident, params)
                decider = deciders[incident]
            try:
                actions = decider.decide(observation, history)
            except BudgetExceeded as exc:
                return self._send(409, {"error": str(exc), "kind": "budget"})
            except Exception as exc:  # the decider's own failure: invalid after its retries, the model down
                return self._send(422, {"error": f"{type(exc).__name__}: {exc}", "kind": "decider"})
            return self._send(200, {"actions": [encode_action(a) for a in actions]})

        def log_message(self, format: str, *args: object) -> None:
            """Quiet: the decider keeps its own record."""

    return ThreadingHTTPServer((host, port), Handler)
