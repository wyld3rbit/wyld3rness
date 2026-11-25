"""The shared base of every simulated service: HTTP, JSON logs, metrics, OpenAPI.

Each service is a table of routes. From that one table this module serves the
requests, counts them in Prometheus-text metrics and generates the OpenAPI spec
that Postman imports, so the spec can never drift from what the service does.

Why the standard library and not a web framework: the services are a test bed
that must be readable end to end by an inspector, and must not change under us.
`ThreadingHTTPServer` gives one thread per connection, which is enough for a few
hundred requests a second. At real scale this would be an async server; the
trade-off here is readability and zero dependencies over throughput, and the load
test (research log, entry 006) measures whether the throughput is enough.

Keep-alive matters. Every client in this package holds one connection per thread
(`Upstream`). Without it, a few hundred requests a second would open and close as
many TCP connections, and the sockets left in TIME_WAIT would exhaust the
ephemeral ports within minutes, a failure of the test bed, not of the system.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
import http.client
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import math
import os
import re
import sys
import threading
import time
from typing import Any, TypeGuard

JSON = dict[str, Any]
Labels = tuple[tuple[str, str], ...]

# Latency buckets in milliseconds: fine below the gateway's timeout (500 ms),
# coarse above it, where only "slow" versus "very slow" matters.
BUCKETS_MS = (5.0, 10.0, 25.0, 50.0, 100.0, 250.0, 500.0, 1000.0, 2500.0, 5000.0)

_log_lock = threading.Lock()
_KEY = re.compile(r"[A-Za-z0-9_.-]{1,64}")


# ---- Input checks, shared by every handler ---------------------------------------------


def number(value: Any) -> float | None:
    """`value` as a finite number, or None. Booleans are refused: in Python `True` is the integer 1."""
    if isinstance(value, bool) or not isinstance(value, int | float) or not math.isfinite(value):
        return None
    return float(value)


def count(value: Any) -> int | None:
    """`value` as an integer that is not a boolean, or None."""
    return value if isinstance(value, int) and not isinstance(value, bool) else None


def _not_json(token: str) -> Any:
    """Refuse NaN, Infinity and -Infinity, which Python's json accepts and standard JSON does not."""
    raise ValueError(f"{token} is not JSON")


def is_key(value: Any) -> TypeGuard[str]:
    """A customer key: 1 to 64 letters, digits, `_`, `.` or `-`. Keys become metric labels and log fields."""
    return isinstance(value, str) and _KEY.fullmatch(value) is not None


def log(service: str, event: str, **fields: Any) -> None:
    """Write one JSON log line to stdout, which `docker compose logs` shows.

    One line per event, flushed at once, under a lock so lines from concurrent
    threads never interleave. Successful requests are not logged one by one (the
    metrics count them); failures, actions and faults are.
    """
    line = json.dumps({"ts": round(time.time(), 3), "service": service, "event": event, **fields}, sort_keys=False)
    with _log_lock:
        sys.stdout.write(line + "\n")
        sys.stdout.flush()


# ---- Metrics ------------------------------------------------------------------------


def _labels(labels: Mapping[str, str] | None) -> Labels:
    """A label set as a hashable, sorted key."""
    return tuple(sorted((labels or {}).items()))


def _escape(value: str) -> str:
    """A label value escaped as the text format requires: backslash, double quote, newline."""
    return value.replace("\\", "\\\\").replace('"', '\\"').replace("\n", "\\n")


def _value(value: float) -> str:
    """A sample value, exact: a counter past 1e6 must not print as 1.00001e+06,
    or a scraper's per-second deltas round to 10 and then to 100."""
    v = float(value)
    if math.isinf(v):
        return "+Inf" if v > 0 else "-Inf"
    return str(int(v)) if v.is_integer() else repr(v)


def _fmt(name: str, labels: Labels, value: float) -> str:
    """One sample line in the Prometheus text format."""
    if labels:
        inner = ",".join(f'{k}="{_escape(v)}"' for k, v in labels)
        return f"{name}{{{inner}}} {_value(value)}"
    return f"{name} {_value(value)}"


@dataclass
class _Family:
    """One metric name: its help text, type and samples."""

    name: str
    help: str
    kind: str  # counter, gauge or histogram
    values: dict[Labels, float] = field(default_factory=dict)
    buckets: dict[Labels, list[int]] = field(default_factory=dict)
    sums: dict[Labels, float] = field(default_factory=dict)
    read: Callable[[], Mapping[Labels, float]] | None = None  # a gauge computed at scrape time


class Metrics:
    """A thread-safe registry rendered in the Prometheus text exposition format.

    Counters only go up, gauges are read at scrape time from a callback, and
    histograms count observations into the fixed `BUCKETS_MS`. A scraper (curl, a
    Prometheus server, the checker) computes rates from two scrapes.
    """

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._families: dict[str, _Family] = {}

    def counter(self, name: str, help: str) -> None:
        """Declare a counter."""
        self._families[name] = _Family(name, help, "counter")

    def histogram(self, name: str, help: str) -> None:
        """Declare a latency histogram in milliseconds."""
        self._families[name] = _Family(name, help, "histogram")

    def gauge(self, name: str, help: str, read: Callable[[], Mapping[Labels, float]]) -> None:
        """Declare a gauge whose samples `read` returns at scrape time."""
        self._families[name] = _Family(name, help, "gauge", read=read)

    def inc(self, name: str, labels: Mapping[str, str] | None = None, by: float = 1.0) -> None:
        """Add `by` to a counter sample."""
        key = _labels(labels)
        with self._lock:
            fam = self._families[name]
            fam.values[key] = fam.values.get(key, 0.0) + by

    def observe(self, name: str, ms: float, labels: Mapping[str, str] | None = None) -> None:
        """Count one observation of `ms` milliseconds into a histogram."""
        key = _labels(labels)
        with self._lock:
            fam = self._families[name]
            counts = fam.buckets.setdefault(key, [0] * (len(BUCKETS_MS) + 1))
            for i, bound in enumerate(BUCKETS_MS):
                if ms <= bound:
                    counts[i] += 1
            counts[-1] += 1  # +Inf
            fam.sums[key] = fam.sums.get(key, 0.0) + ms

    def value(self, name: str, labels: Mapping[str, str] | None = None) -> float:
        """A counter's current value, 0 if never incremented. For tests and summaries."""
        with self._lock:
            return self._families[name].values.get(_labels(labels), 0.0)

    def render(self) -> str:
        """Every family in the Prometheus text format."""
        out: list[str] = []
        with self._lock:
            families = list(self._families.values())
            for fam in families:
                out += [f"# HELP {fam.name} {fam.help}", f"# TYPE {fam.name} {fam.kind}"]
                if fam.kind == "counter":
                    out += [_fmt(fam.name, k, v) for k, v in sorted(fam.values.items())]
                elif fam.kind == "histogram":
                    for k, counts in sorted(fam.buckets.items()):
                        for bound, c in zip((*BUCKETS_MS, None), counts, strict=True):
                            le = "+Inf" if bound is None else f"{bound:g}"
                            out.append(_fmt(f"{fam.name}_bucket", (*k, ("le", le)), c))
                        out.append(_fmt(f"{fam.name}_sum", k, fam.sums[k]))
                        out.append(_fmt(f"{fam.name}_count", k, counts[-1]))
        for fam in families:  # gauges read outside the lock: their callbacks take their own locks
            if fam.kind == "gauge" and fam.read is not None:
                out += [_fmt(fam.name, k, v) for k, v in sorted(fam.read().items())]
        return "\n".join(out) + "\n"


# ---- Routes and dispatch ------------------------------------------------------------


@dataclass(frozen=True)
class Request:
    """What a handler sees: the parsed JSON body and the request headers."""

    body: JSON
    headers: Mapping[str, str]

    @property
    def actor(self) -> str:
        """Who asked, from the X-Actor header. Step 4 replaces it with a verified credential."""
        return self.headers.get("X-Actor") or self.headers.get("x-actor") or "unknown"


@dataclass(frozen=True)
class Response:
    """A status, a body and its content type. A dict body is sent as JSON."""

    status: int
    body: JSON | str | bytes
    content_type: str = "application/json"


Handler = Callable[[Request], Response]
Fallback = Callable[[str, str, bytes, Mapping[str, str]], Response | None]


@dataclass(frozen=True)
class Route:
    """One endpoint. The same record serves the request and documents it in the spec."""

    method: str
    path: str
    handler: Handler
    summary: str
    body: JSON | None = None  # JSON Schema of the request body, when there is one
    responses: Mapping[int, str] = field(default_factory=lambda: {200: "OK"})
    action: bool = False  # an action: logged with its actor, documented with the X-Actor header


class Service:
    """A named set of routes plus the three every service has: /healthz, /metrics, /openapi.json.

    `dispatch` is pure apart from the handlers, so tests call it without sockets.
    `fallback` lets the edge proxy route by prefix instead of by exact path.
    """

    def __init__(
        self,
        name: str,
        routes: list[Route],
        metrics: Metrics | None = None,
        fallback: Fallback | None = None,
        public_url: str | None = None,
    ) -> None:
        self.name = name
        self.metrics = metrics or Metrics()
        self.fallback = fallback
        self.public_url = public_url or os.environ.get("PUBLIC_URL", f"http://127.0.0.1:8100/{name}")
        builtin = [
            Route("GET", "/healthz", lambda r: Response(200, {"status": "ok", "service": name}), "Liveness"),
            Route(
                "GET",
                "/metrics",
                lambda r: Response(200, self.metrics.render(), "text/plain; version=0.0.4"),
                "Metrics in the Prometheus text format",
            ),
            Route("GET", "/openapi.json", lambda r: Response(200, self.openapi()), "This OpenAPI spec"),
        ]
        self.routes = {(r.method, r.path): r for r in [*builtin, *routes]}
        self.metrics.counter("http_requests_total", "Requests served, by route and status")
        self.metrics.histogram("http_request_duration_ms", "Time to serve a request, by route")

    def openapi(self) -> JSON:
        """The OpenAPI 3.1 spec, generated from the route table."""
        paths: dict[str, JSON] = {}
        for r in self.routes.values():
            op: JSON = {
                "summary": r.summary,
                "responses": {str(code): {"description": text} for code, text in r.responses.items()},
            }
            if r.body is not None:
                op["requestBody"] = {"required": True, "content": {"application/json": {"schema": r.body}}}
            if r.action:
                op["parameters"] = [
                    {
                        "name": "X-Actor",
                        "in": "header",
                        "required": False,
                        "schema": {"type": "string"},
                        "description": "Who is acting, for the action log",
                    }
                ]
            paths.setdefault(r.path, {})[r.method.lower()] = op
        return {
            "openapi": "3.1.0",
            "info": {"title": f"simprod {self.name}", "version": "1"},
            "servers": [{"url": self.public_url}],
            "paths": paths,
        }

    def dispatch(self, method: str, path: str, raw: bytes, headers: Mapping[str, str]) -> Response:
        """Serve one request: route, parse, call, count. Never raises."""
        path = path.split("?", 1)[0]
        started = time.monotonic()
        route = self.routes.get((method, path))
        label = path if route else "other"
        if route is None:
            found = self.fallback(method, path, raw, headers) if self.fallback else None
            resp = found or Response(404, {"error": "not found", "path": path})
        else:
            resp = self._call(route, raw, headers)
        ms = (time.monotonic() - started) * 1000
        self.metrics.inc("http_requests_total", {"route": label, "status": str(resp.status)})
        self.metrics.observe("http_request_duration_ms", ms, {"route": label})
        if resp.status >= 400 and route is not None:
            log(self.name, "request_failed", method=method, path=path, status=resp.status, ms=round(ms, 1))
        return resp

    def _call(self, route: Route, raw: bytes, headers: Mapping[str, str]) -> Response:
        """Parse the body and call the handler, mapping bad input to 400 and a bug to 500."""
        try:
            body = json.loads(raw, parse_constant=_not_json) if raw else {}
        except ValueError:  # JSONDecodeError is a ValueError, and so is NaN or Infinity
            return Response(400, {"error": "body is not standard JSON"})
        if not isinstance(body, dict):
            return Response(400, {"error": "body must be a JSON object"})
        try:
            resp = route.handler(Request(body, headers))
        except Exception as exc:  # a bug in a handler must not kill the server thread
            log(self.name, "handler_error", path=route.path, error=f"{type(exc).__name__}: {exc}")
            return Response(500, {"error": "internal error"})
        if route.action:
            log(
                self.name,
                "action",
                path=route.path,
                actor=Request(body, headers).actor,
                params=body,
                status=resp.status,
                result=resp.body if isinstance(resp.body, dict) else None,
            )
        return resp


# ---- Serving ------------------------------------------------------------------------


class _Server(ThreadingHTTPServer):
    """A threading server with a deeper accept backlog and daemon threads."""

    daemon_threads = True
    allow_reuse_address = True
    request_queue_size = 256  # the default of 5 refuses connections in a burst


def _handler_for(service: Service) -> type[BaseHTTPRequestHandler]:
    """A request handler class bound to `service`."""

    class _Handler(BaseHTTPRequestHandler):
        """One connection's requests, served by `service`."""

        protocol_version = "HTTP/1.1"  # keep-alive: every response carries Content-Length

        def do_GET(self) -> None:
            """Serve a GET through the service's dispatch."""
            self._serve()

        def do_POST(self) -> None:
            """Serve a POST through the service's dispatch."""
            self._serve()

        def _serve(self) -> None:
            """Read the body, dispatch, write the response; a client that hung up is not an error."""
            length = int(self.headers.get("Content-Length") or 0)
            raw = self.rfile.read(length) if length else b""
            resp = service.dispatch(self.command, self.path, raw, dict(self.headers.items()))
            body = resp.body
            data = body if isinstance(body, bytes) else (body if isinstance(body, str) else json.dumps(body)).encode()
            try:
                self.send_response(resp.status)
                self.send_header("Content-Type", resp.content_type)
                self.send_header("Content-Length", str(len(data)))
                self.end_headers()
                self.wfile.write(data)
            except (BrokenPipeError, ConnectionResetError):
                # The client gave up (a gateway timeout): the work was done for nobody.
                self.close_connection = True

        def log_message(self, format: str, *args: Any) -> None:
            """Silence the default per-request stderr line; `log` and the metrics cover it."""

    return _Handler


def start(service: Service, port: int, host: str = "0.0.0.0") -> ThreadingHTTPServer:
    """Serve `service` on `port` in a background thread. Returns the server (its port may be 0 → chosen)."""
    server = _Server((host, port), _handler_for(service))
    threading.Thread(target=server.serve_forever, name=f"{service.name}:{port}", daemon=True).start()
    return server


def run(*listeners: tuple[Service, int]) -> None:
    """Serve each (service, port) pair and block forever. The entry point of a container."""
    for service, port in listeners:
        start(service, port)
        log(service.name, "listening", port=port)
    threading.Event().wait()


def restart_route(service_name: str, exit: Callable[[int], None] = os._exit, delay_s: float = 0.2) -> Route:
    """The restart action: answer 202, then exit the process so Docker's restart policy starts it fresh.

    No Docker socket is involved: the service ends itself. Everything in memory
    (queues, connections, settings changed by other actions) is lost, which is
    why this action has no undo.
    """

    def handle(req: Request) -> Response:
        threading.Timer(delay_s, exit, args=(0,)).start()
        return Response(202, {"restarting": service_name})

    return Route(
        "POST",
        "/actions/restart",
        handle,
        "Restart this service (no undo)",
        body={"type": "object"},
        responses={202: "Restarting"},
        action=True,
    )


# ---- Clients ------------------------------------------------------------------------


class Upstream:
    """A keep-alive HTTP client to one service, with one connection per calling thread.

    A failed call closes its connection, so the next one opens a new socket:
    after a timeout the old one may still deliver a late response. http.client
    reopens a closed connection by itself on the next request.
    """

    def __init__(self, host: str, port: int) -> None:
        self.host, self.port = host, port
        self._local = threading.local()

    def raw(
        self, method: str, path: str, body: bytes | None, timeout_s: float, headers: Mapping[str, str] | None = None
    ) -> tuple[int, str, bytes]:
        """Send one request. Returns status, content type and body bytes; raises OSError on failure."""
        conn: http.client.HTTPConnection | None = getattr(self._local, "conn", None)
        if conn is None:
            conn = http.client.HTTPConnection(self.host, self.port, timeout=timeout_s)
            self._local.conn = conn
        conn.timeout = timeout_s
        if conn.sock is not None:
            conn.sock.settimeout(timeout_s)
        try:
            conn.request(method, path, body=body, headers=dict(headers or {}))
            resp = conn.getresponse()
            data = resp.read()
        except (OSError, http.client.HTTPException) as exc:
            conn.close()
            raise OSError(f"{self.host}:{self.port}{path}: {type(exc).__name__}: {exc}") from exc
        return resp.status, resp.getheader("Content-Type") or "", data

    def call(
        self, method: str, path: str, body: JSON | None, timeout_s: float, headers: Mapping[str, str] | None = None
    ) -> tuple[int, JSON]:
        """Send JSON, receive JSON. Raises OSError on a failed connection or a timeout."""
        data = json.dumps(body).encode() if body is not None else None
        hdrs = {"Content-Type": "application/json", **(headers or {})}
        status, _, raw = self.raw(method, path, data, timeout_s, hdrs)
        try:
            parsed = json.loads(raw) if raw else {}
        except json.JSONDecodeError:
            parsed = {"raw": raw[:200].decode(errors="replace")}
        return status, parsed if isinstance(parsed, dict) else {"value": parsed}


Caller = Callable[[str, str, JSON | None, float], tuple[int, JSON]]


def env_addr(name: str, default: str) -> tuple[str, int]:
    """A host:port from the environment."""
    host, _, port = os.environ.get(name, default).rpartition(":")
    return host, int(port)


def env_float(name: str, default: float) -> float:
    """A number from the environment."""
    return float(os.environ.get(name, default))


def env_flag(name: str, default: bool = False) -> bool:
    """A switch from the environment: "1" or "0", anything else refused at start, not guessed."""
    value = os.environ.get(name, "1" if default else "0")
    if value not in ("0", "1"):
        raise ValueError(f"{name} must be 0 or 1, not {value!r}")
    return value == "1"
