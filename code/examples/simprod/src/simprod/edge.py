"""The edge: the only door from the host into the simulated production.

    host 127.0.0.1:8100 ──► edge ──► GET  /<service>/healthz | /metrics | /openapi.json
                                     GET  /customers/healthz | /metrics | /openapi.json
                                     POST /gateway/actions/backoff | /gateway/actions/rate-cap
                                     POST /<service>/actions/restart
                                     anything else: 404, logged

Why a proxy exists at all: the services sit on a Docker network marked
internal, which has no route out. Docker cannot publish a port from such a
container (measured on 2026-11-23: the host got no answer), so one container
joins both that network and an ordinary one and forwards. That container could
reach the internet; it runs only this file.

Why an allow-list: it is how the fault controls stay sealed. They listen on each
service's admin port (9000), and no entry below names that port or a /fault
path, so nothing on the host can reach them over HTTP, the agent included. The
table is the single source for both the routing and this proxy's OpenAPI spec.

No payments through here. Customers and the load generator pay from inside the
internal network; nothing on the host needs `POST /gateway/pay`, and forwarding
it would let anything on the host spend with a key read from the metrics
(ADR 0009, step 0b).
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass

from simprod.credentials import HEADER
from simprod.service import (
    JSON,
    Metrics,
    Response,
    Route,
    Service,
    Upstream,
    env_float,
    log,
    run,
)

NAME = "edge"
SERVICES = ("gateway", "ledger", "notifications")
# Read-only: what customers saw, the signal synthetic monitoring gives an operator.
# Its fault port (the runaway client) is sealed like the ledger's.
CUSTOMERS = "customers"
FORWARD_TIMEOUT_S = 10.0
# Only these request headers pass through: the body's type, who is acting, and the action's credential.
FORWARDED_HEADERS = ("Content-Type", "X-Actor", HEADER)


@dataclass(frozen=True)
class Allowed:
    """One forwarded endpoint: method, service and the path on that service."""

    method: str
    service: str
    path: str
    summary: str


ALLOW: tuple[Allowed, ...] = (
    *(
        Allowed("GET", s, p, f"{s}: {what}")
        for s in SERVICES
        for p, what in (("/healthz", "liveness"), ("/metrics", "Prometheus metrics"), ("/openapi.json", "OpenAPI spec"))
    ),
    *(
        Allowed("GET", CUSTOMERS, p, f"customers: {what}")
        for p, what in (("/healthz", "liveness"), ("/metrics", "what customers saw"), ("/openapi.json", "OpenAPI spec"))
    ),
    Allowed("POST", "gateway", "/actions/backoff", "gateway: turn retry backoff on or off"),
    Allowed("POST", "gateway", "/actions/rate-cap", "gateway: cap one key's rate"),
    *(Allowed("POST", s, "/actions/restart", f"{s}: restart (no undo)") for s in SERVICES),
)
_INDEX = {(a.method, f"/{a.service}{a.path}"): a for a in ALLOW}


def route(method: str, path: str) -> Allowed | None:
    """The allow-list entry for a request, or None. Exact match only: no prefixes, no `..`."""
    return _INDEX.get((method, path.split("?", 1)[0]))


class Edge:
    """Forwards allowed requests to the services on the internal network."""

    def __init__(self, upstreams: Mapping[str, Upstream], metrics: Metrics) -> None:
        self.upstreams = upstreams
        self.metrics = metrics
        metrics.counter("edge_forwarded_total", "Requests forwarded, by service and upstream status")
        metrics.counter("edge_denied_total", "Requests refused because no allow-list entry matched")

    def forward(self, method: str, path: str, raw: bytes, headers: Mapping[str, str]) -> Response | None:
        """The fallback for every path the edge itself does not serve."""
        allowed = route(method, path)
        if allowed is None:
            self.metrics.inc("edge_denied_total")
            log(NAME, "denied", method=method, path=path)
            return Response(404, {"error": "not found", "path": path})
        lowered = {k.lower(): v for k, v in headers.items()}
        passed = {h: lowered[h.lower()] for h in FORWARDED_HEADERS if h.lower() in lowered}
        try:
            status, ctype, body = self.upstreams[allowed.service].raw(
                method, allowed.path, raw or None, FORWARD_TIMEOUT_S, passed
            )
        except OSError as exc:
            self.metrics.inc("edge_forwarded_total", {"service": allowed.service, "status": "502"})
            return Response(502, {"error": "upstream unreachable", "service": allowed.service, "detail": str(exc)})
        self.metrics.inc("edge_forwarded_total", {"service": allowed.service, "status": str(status)})
        return Response(status, body, ctype or "application/json")


def openapi(public_url: str) -> JSON:
    """The edge's own spec: every allowed endpoint, generated from ALLOW. One Postman import covers all."""
    paths: dict[str, JSON] = {}
    for a in ALLOW:
        op: JSON = {"summary": a.summary, "tags": [a.service], "responses": {"200": {"description": "OK"}}}
        if a.method == "POST":
            op["requestBody"] = {"required": True, "content": {"application/json": {"schema": {"type": "object"}}}}
            if "/actions/" in a.path:
                op["parameters"] = [
                    {"name": HEADER, "in": "header", "required": True, "schema": {"type": "string"}},
                    {"name": "X-Actor", "in": "header", "required": False, "schema": {"type": "string"}},
                ]
                op["responses"] = {**op["responses"], "403": {"description": "No valid credential for this action"}}
        paths.setdefault(f"/{a.service}{a.path}", {})[a.method.lower()] = op
    return {
        "openapi": "3.1.0",
        "info": {"title": "simprod edge", "version": "1"},
        "servers": [{"url": public_url}],
        "paths": paths,
    }


def build(edge: Edge, public_url: str = "http://127.0.0.1:8100") -> Service:
    """The edge service: its own health and metrics, a combined spec, and forwarding for the rest."""
    return Service(
        NAME,
        [
            Route("GET", "/openapi.json", lambda r: Response(200, openapi(public_url)), "Every allowed endpoint"),
        ],
        metrics=edge.metrics,
        fallback=edge.forward,
        public_url=public_url,
    )


def main() -> None:
    """Container entry point."""
    port = int(env_float("UPSTREAM_PORT", 8000))
    edge = Edge({s: Upstream(s, port) for s in (*SERVICES, CUSTOMERS)}, Metrics())
    log(NAME, "allow_list", entries=[f"{a.method} /{a.service}{a.path}" for a in ALLOW])
    run((build(edge), 8000))


if __name__ == "__main__":
    main()
