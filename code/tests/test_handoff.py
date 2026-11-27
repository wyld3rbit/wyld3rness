"""Tests for the hand-off: an unresolved incident reaches a person, and a failed send never raises."""

from __future__ import annotations

from datetime import UTC, datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import threading
import urllib.error

import pytest

from wyld3rness.loop.env import Action, Applied
from wyld3rness.loop.handoff import WebhookHandoff, alert, hand_off, summary
from wyld3rness.loop.run import Outcome, Try
from wyld3rness.testing import FakeHandoff

BACKOFF = Action("backoff", {"enabled": True})
UNDO = Action("backoff", {"enabled": False})


def safe_state() -> Outcome:
    """Two tries, both undone, then out of tries."""
    applied = (Applied(BACKOFF, True, "200", UNDO),)
    tries = [
        Try(1, (BACKOFF,), applied, 5.0, "not verified", undone=applied),
        Try(2, (Action("rate_cap", {"key": "acct-0042", "rps": 5}),), (), 40.0, "denied", reasons=("not hot",)),
    ]
    return Outcome("safe state", "out of tries: 2 tried, none resolved", tries=tries)


def test_a_resolved_incident_is_not_handed_off() -> None:
    handoff = FakeHandoff()
    assert hand_off(handoff, "i1", Outcome("resolved", "resolved on try 1")) is None
    assert handoff.sent == []
    with pytest.raises(ValueError, match="nothing to hand off"):
        alert("i1", Outcome("resolved", "resolved on try 1"))


def test_the_alert_says_what_was_tried_undone_and_why_it_stopped() -> None:
    payload = alert("i7", safe_state(), at=datetime(2026, 11, 26, 12, 0, tzinfo=UTC), trace_url="http://t/7")
    assert payload["version"] == "4" and payload["status"] == "firing"
    one = payload["alerts"][0]
    assert one["labels"] == {"alertname": "IncidentNeedsAPerson", "incident": "i7", "severity": "page"}
    assert one["startsAt"] == "2026-11-26T12:00:00Z" and one["generatorURL"] == "http://t/7"
    notes = one["annotations"]
    assert notes["summary"] == "Incident i7 ended in the safe state: out of tries: 2 tried, none resolved"
    assert notes["tried"].splitlines() == [
        "try 1: backoff(enabled=True): not verified (undone)",
        "try 2: rate_cap(key=acct-0042, rps=5): denied: not hot",
    ]
    assert notes["in_force"] == "nothing" and notes["trace"] == "http://t/7"


def test_a_safe_state_is_sent_and_the_record_keeps_the_alert_as_sent() -> None:
    handoff = FakeHandoff()
    record = hand_off(handoff, "i7", safe_state())
    assert record is not None and record["delivered"] is True and record["to"] == "fake://on-call"
    assert handoff.sent == [record["alert"]]
    assert summary(record)["summary"].startswith("Incident i7 ended in the safe state")


def test_a_refused_hand_off_is_recorded_as_not_delivered() -> None:
    record = hand_off(FakeHandoff(delivered=False), "i7", safe_state())
    assert record is not None and record["delivered"] is False and record["detail"] == "refused"


@pytest.mark.parametrize(
    ("post", "detail"),
    [
        (lambda url, body, t: (500, "boom"), "HTTP 500 boom"),
        (lambda url, body, t: (_ for _ in ()).throw(ConnectionRefusedError("refused")), "ConnectionRefusedError"),
        (lambda url, body, t: (_ for _ in ()).throw(TimeoutError("timed out")), "TimeoutError"),
        (
            lambda url, body, t: (_ for _ in ()).throw(urllib.error.HTTPError(url, 403, "no", {}, None)),  # type: ignore[arg-type]
            "HTTP 403",
        ),
    ],
)
def test_every_failed_send_is_not_delivered_and_never_raises(post, detail) -> None:
    delivery = WebhookHandoff("http://on-call", post=post).send({"alerts": []})
    assert delivery.delivered is False and delivery.detail.startswith(detail)


def test_the_webhook_reaches_a_real_receiver() -> None:
    got: list[dict[str, object]] = []

    class Receiver(BaseHTTPRequestHandler):
        def do_POST(self) -> None:
            got.append(json.loads(self.rfile.read(int(self.headers["Content-Length"]))))
            self.send_response(200)
            self.send_header("Content-Length", "2")
            self.end_headers()
            self.wfile.write(b"ok")

        def log_message(self, format: str, *args: object) -> None:
            pass

    server = ThreadingHTTPServer(("127.0.0.1", 0), Receiver)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    try:
        url = f"http://127.0.0.1:{server.server_port}/handoff"
        record = hand_off(WebhookHandoff(url), "i7", safe_state())
    finally:
        server.shutdown()
    assert record is not None and record["delivered"] is True and record["detail"] == "HTTP 200 ok"
    assert got == [record["alert"]]
