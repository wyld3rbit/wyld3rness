"""Notifications: tells the customer a payment went through.

The gateway calls it once per committed payment, best effort: a failed
notification is counted, never retried, and does not fail the payment. It has no
fault of its own; it is here as the third service of the incident corpus and as
a target for the restart action, whose only effect on it is a short outage.
"""

from __future__ import annotations

from collections.abc import Callable
import time

from simprod.service import Metrics, Request, Response, Route, Service, env_float, restart_route, run

NAME = "notifications"


class Notifications:
    """Sends (sleeps for) one notification per request."""

    def __init__(self, service_s: float, metrics: Metrics, sleep: Callable[[float], None] = time.sleep) -> None:
        self.service_s = service_s
        self.metrics = metrics
        self.sleep = sleep
        metrics.counter("notifications_sent_total", "Notifications sent")

    def notify(self, req: Request) -> Response:
        """POST /notify {id, key}: one notification."""
        if not isinstance(req.body.get("key"), str):
            return Response(400, {"error": "need key (string)"})
        self.sleep(self.service_s)
        self.metrics.inc("notifications_sent_total")
        return Response(200, {"sent": req.body.get("id")})


def build(n: Notifications, exit: Callable[[int], None] | None = None) -> Service:
    """The public service."""
    restart = restart_route(NAME) if exit is None else restart_route(NAME, exit=exit)
    return Service(
        NAME,
        [
            Route(
                "POST",
                "/notify",
                n.notify,
                "Send one notification",
                body={
                    "type": "object",
                    "required": ["key"],
                    "properties": {"key": {"type": "string"}, "id": {"type": "string"}},
                },
            ),
            restart,
        ],
        metrics=n.metrics,
    )


def main() -> None:
    """Container entry point."""
    n = Notifications(env_float("NOTIFY_SERVICE_MS", 5) / 1000, Metrics())
    run((build(n), 8000))


if __name__ == "__main__":
    main()
