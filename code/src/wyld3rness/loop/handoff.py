"""The hand-off: an incident that ends in the safe state is handed to a person.

    run_incident ──► Outcome: safe state ──► alert(...) ──► Handoff.send ──► the on-call URL
                                                                   └──► Delivery: delivered or not, into the record

Why: the safe state undoes every change and stops. Those are exactly the
incidents the agent could not fix, so a person must hear of them without
looking. A resolved incident is not handed off.

Why the Alertmanager webhook payload: it is the format on-call tools already
take from monitoring, so a real pager can replace whatever receives it today
without a change here. Only the fields a receiver reads are filled.

Why delivery never raises: telling a person is a second duty after making the
system safe, and a failure to notify must never undo the first. A failed send
is a `Delivery` with `delivered` False and the reason, which the caller writes
into the incident's record. What changes at scale: a receiver that must
acknowledge, and a resend of an unacknowledged hand-off (ADR 0009).
"""

from __future__ import annotations

from collections.abc import Callable, Mapping
from dataclasses import dataclass
from datetime import UTC, datetime
import http.client
import json
from typing import Any, Protocol
import urllib.error
import urllib.request

from wyld3rness.loop.run import Outcome, Try

JSON = dict[str, Any]
TIMEOUT_S = 5.0  # long enough for a receiver on the same machine; a slow one is "not delivered", never a stall
NEVER = "0001-01-01T00:00:00Z"  # Alertmanager's end time for an alert still firing


@dataclass(frozen=True)
class Delivery:
    """What became of one hand-off: delivered or not, where to, and the receiver's answer or the error."""

    delivered: bool
    to: str
    detail: str


class Handoff(Protocol):
    """Sends one alert to a person. Never raises: a failure is a `Delivery` that says so."""

    def send(self, alert: JSON) -> Delivery:
        """Send `alert` (an Alertmanager webhook payload) and say whether it arrived."""
        ...


def _try_line(t: Try) -> str:
    """One try as a person reads it: its actions, the verdict, and whether they were undone. Pure."""
    acts = ", ".join(a.label() for a in t.actions) or "nothing"
    undone = " (undone)" if t.undone else ""
    reasons = f": {'; '.join(t.reasons)}" if t.reasons else ""
    return f"try {t.number}: {acts}: {t.verdict}{undone}{reasons}"


def alert(incident: str, outcome: Outcome, *, at: datetime | None = None, trace_url: str | None = None) -> JSON:
    """The Alertmanager webhook payload for an incident that ended in the safe state. Pure.

    Raises ValueError for a resolved incident: there is nothing to hand off.
    """
    if outcome.resolved:
        raise ValueError(f"incident {incident} was resolved: there is nothing to hand off")
    started = (at or datetime.now(UTC)).strftime("%Y-%m-%dT%H:%M:%SZ")
    labels = {"alertname": "IncidentNeedsAPerson", "incident": incident, "severity": "page"}
    annotations = {
        "summary": f"Incident {incident} ended in the safe state: {outcome.reason}",
        "tried": "\n".join(_try_line(t) for t in outcome.tries) or "no try was made",
        "undone": ", ".join(a.action.label() for a in outcome.safe_undos) or "nothing was left to undo",
        "in_force": ", ".join(a.label() for a in outcome.kept) or "nothing",
    }
    if trace_url:
        annotations["trace"] = trace_url
    one = {
        "status": "firing",
        "labels": labels,
        "annotations": annotations,
        "startsAt": started,
        "endsAt": NEVER,
        "generatorURL": trace_url or "",
        "fingerprint": incident,
    }
    return {
        "version": "4",
        "groupKey": f"incident:{incident}",
        "truncatedAlerts": 0,
        "status": "firing",
        "receiver": "on-call",
        "groupLabels": {"incident": incident},
        "commonLabels": labels,
        "commonAnnotations": annotations,
        "externalURL": "",
        "alerts": [one],
    }


Post = Callable[[str, bytes, float], tuple[int, str]]


def _post(url: str, body: bytes, timeout_s: float) -> tuple[int, str]:
    """POST `body` as JSON to `url` and return the status and the start of the answer. Raises on a network error."""
    request = urllib.request.Request(url, body, {"Content-Type": "application/json"}, method="POST")
    with urllib.request.urlopen(request, timeout=timeout_s) as response:
        return response.status, response.read(200).decode(errors="replace")


class WebhookHandoff:
    """Posts the alert to one URL, as Alertmanager's webhook receiver would. Delivered means a 2xx answer."""

    def __init__(self, url: str, *, timeout_s: float = TIMEOUT_S, post: Post = _post) -> None:
        """`post` is injectable so tests need no server."""
        self.url = url
        self.timeout_s = timeout_s
        self._post = post

    def send(self, alert: JSON) -> Delivery:
        """Send `alert`. Every failure (refused, timed out, a non-2xx answer) is `delivered` False with the reason."""
        try:
            status, answer = self._post(self.url, json.dumps(alert).encode(), self.timeout_s)
        except urllib.error.HTTPError as exc:
            return Delivery(False, self.url, f"HTTP {exc.code}")
        # URLError and timeouts are OSError, a malformed URL is ValueError, and a receiver that is not
        # speaking HTTP (a bad status line, a cut chunk) raises http.client.HTTPException.
        except (OSError, ValueError, http.client.HTTPException) as exc:
            return Delivery(False, self.url, f"{type(exc).__name__}: {exc}")
        return Delivery(200 <= status < 300, self.url, f"HTTP {status} {answer}".strip())


def hand_off(handoff: Handoff, incident: str, outcome: Outcome, *, trace_url: str | None = None) -> JSON | None:
    """Hand an unresolved incident to a person and return what goes into its record; None when it was resolved.

    The record keeps the alert as sent, so a replay shows exactly what the person received.
    """
    if outcome.resolved:
        return None
    payload = alert(incident, outcome, trace_url=trace_url)
    delivery = handoff.send(payload)
    return {"delivered": delivery.delivered, "to": delivery.to, "detail": delivery.detail, "alert": payload}


def summary(record: Mapping[str, Any]) -> JSON:
    """The fields a viewer shows from a hand-off record: delivered, to, and the alert's annotations. Pure."""
    first = (record.get("alert") or {}).get("alerts", [{}])[0]
    return {"delivered": bool(record.get("delivered")), "to": record.get("to"), **(first.get("annotations") or {})}
