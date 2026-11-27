"""An allow-list edge: the only door between an agent on an internal network and anything outside it.

    agent (internal network, no route out) ──► edge door ──► one upstream per listed route
                                                  └─ anything not listed: 403, logged

Why it exists. A container on a Docker network marked internal has no default
route: it reaches its peers on that network and nothing else, not the host, not
the internet. That is the boundary, enforced by the kernel's routing rather
than by each service remembering to check a credential. An agent still needs
some things outside (its model, and a host-side test suite needs to reach it),
so one small container joins the internal network and an ordinary one and
forwards exactly the routes listed here. The same pattern guards simprod's
fault controls (simprod/edge.py).

How it matches. Method and path, exactly; the query string passes through
untouched. No prefixes and no normalization: `/api/chat/../pull` is not
`/api/chat`, so it is refused. A body must come with a Content-Length (411
otherwise). Only the request's Content-Type is forwarded;
every other header (cookies, an Authorization a client might send, a forged
Host) stays here.

Streaming. A model server streams its answer as lines over seconds. The edge
copies each chunk to the client as it arrives and closes the connection at the
end (HTTP/1.0 framing), so a client's read timeout still measures a stalled
model, not a buffered edge. A request body is capped at `MAX_BODY` bytes.

What it does not do. No authentication of its own: it is reachable only by the
peers on its networks, and what it forwards keeps its own credentials. No
rewriting of bodies. At scale an egress proxy (Envoy, or a cloud's private
endpoint) does the same job with policy files and metrics.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
import http.client
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import sys
import threading
from urllib.parse import urlsplit

MAX_BODY = 1 << 20  # 1 MiB: a prompt with its schema is a few kilobytes
TIMEOUT_S = 300.0  # per read from the upstream: a cold load of a 7B model is under two minutes
CHUNK = 64 * 1024


@dataclass(frozen=True)
class Door:
    """One forwarded route: method, exact path, and the upstream base URL (scheme, host, port) it goes to."""

    method: str
    path: str
    upstream: str


def route(doors: Sequence[Door], method: str, path: str) -> Door | None:
    """The door a request may pass through, or None. Pure. Exact method and path; the query is ignored here."""
    bare = path.split("?", 1)[0]
    return next((d for d in doors if d.method == method and d.path == bare), None)


Log = Callable[[dict[str, object]], None]


def _stderr(event: dict[str, object]) -> None:
    """One JSON line on stderr: the container's log."""
    print(json.dumps(event), file=sys.stderr, flush=True)


def edge_server(
    doors: Sequence[Door], host: str, port: int, *, timeout_s: float = TIMEOUT_S, log: Log = _stderr
) -> ThreadingHTTPServer:
    """A server that forwards `doors` and refuses everything else. Not started: call `serve_forever`.

    `GET /healthz` is answered here when no door claims it, so the container's
    health check needs no upstream.
    """

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, format: str, *args: object) -> None:
            """Quiet: refusals and upstream failures are logged as JSON instead."""

        def _json(self, status: int, payload: object) -> None:
            body = json.dumps(payload).encode()
            self.send_response(status)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def _handle(self) -> None:
            door = route(doors, self.command, self.path)
            if door is None:
                if self.command == "GET" and self.path == "/healthz":
                    return self._json(200, {"ok": True})
                log({"edge": "refused", "method": self.command, "path": self.path.split("?", 1)[0]})
                return self._json(403, {"error": "not on this edge's list"})
            # A chunked or unsized body would be forwarded empty and fail upstream with a misleading
            # error; refuse it here, where the cause is plain.
            sized = "Content-Length" in self.headers or self.command in ("GET", "HEAD", "DELETE", "OPTIONS")
            if "Transfer-Encoding" in self.headers or not sized:
                return self._json(411, {"error": "send the body with a Content-Length"})
            try:
                length = int(self.headers.get("Content-Length", "0"))
            except ValueError:
                length = -1
            if not 0 <= length <= MAX_BODY:
                return self._json(413, {"error": "bad body size"})
            body = self.rfile.read(length) if length else None
            self._forward(door, body)

        def _forward(self, door: Door, body: bytes | None) -> None:
            up = urlsplit(door.upstream)
            query = self.path.partition("?")[2]
            target = door.path + (f"?{query}" if query else "")
            headers = {"Content-Type": ct} if (ct := self.headers.get("Content-Type")) else {}
            conn = http.client.HTTPConnection(up.hostname or "", up.port or 80, timeout=timeout_s)
            try:
                conn.request(self.command, target, body, headers)
                answer = conn.getresponse()
            except OSError as exc:
                conn.close()
                log({"edge": "upstream failed", "path": door.path, "error": f"{type(exc).__name__}: {exc}"})
                return self._json(502, {"error": "upstream unreachable"})
            try:
                self.send_response(answer.status)
                if ct_up := answer.getheader("Content-Type"):
                    self.send_header("Content-Type", ct_up)
                if (n := answer.getheader("Content-Length")) is not None:
                    self.send_header("Content-Length", n)
                self.end_headers()
                # read1 returns what has arrived (up to CHUNK), so each streamed line goes on at once.
                while chunk := answer.read1(CHUNK):
                    self.wfile.write(chunk)
                    self.wfile.flush()
            except OSError as exc:  # the client left, or the upstream cut the stream: end this request only
                log({"edge": "stream cut", "path": door.path, "error": f"{type(exc).__name__}: {exc}"})
            finally:
                conn.close()

        do_GET = do_POST = do_PUT = do_PATCH = do_DELETE = do_HEAD = do_OPTIONS = _handle

    server = ThreadingHTTPServer((host, port), Handler)
    server.daemon_threads = True
    return server


def serve(doors_by_port: Mapping[int, Sequence[Door]], host: str = "0.0.0.0") -> None:
    """Serve each port's doors on its own server until the process stops. Blocks."""
    servers = [edge_server(doors, host, port) for port, doors in doors_by_port.items()]
    threads = [threading.Thread(target=s.serve_forever, daemon=True) for s in servers]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
