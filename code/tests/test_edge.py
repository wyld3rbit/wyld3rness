"""The allow-list edge forwards exactly its doors, streams, and refuses the rest (wyld3rness.loop.edge).

A fake upstream on a free loopback port records what reached it and streams
its answer in two parts, the second only after the test has read the first:
so a passing test shows the edge did not buffer.
"""

from __future__ import annotations

from collections.abc import Iterator
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import threading
import urllib.error
import urllib.request

import pytest

from wyld3rness.loop.edge import Door, edge_server, route


class Upstream:
    """A fake upstream: what it received, and a gate the streamed answer waits on."""

    def __init__(self) -> None:
        self.seen: list[dict[str, object]] = []
        self.release = threading.Event()
        upstream = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, format: str, *args: object) -> None:
                pass

            def do_POST(self) -> None:
                body = self.rfile.read(int(self.headers.get("Content-Length", "0")))
                upstream.seen.append({"path": self.path, "headers": dict(self.headers), "body": body})
                self.send_response(200)
                self.send_header("Content-Type", "application/x-ndjson")
                self.end_headers()
                self.wfile.write(b'{"part": 1}\n')
                self.wfile.flush()
                upstream.release.wait(5)
                self.wfile.write(b'{"part": 2}\n')

            do_GET = do_POST

        self.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.url = f"http://127.0.0.1:{self.server.server_address[1]}"


@pytest.fixture
def stack() -> Iterator[tuple[Upstream, str, list[dict[str, object]]]]:
    """The fake upstream and an edge with two doors to it; yields both and the edge's log."""
    up = Upstream()
    threading.Thread(target=up.server.serve_forever, args=(0.05,), daemon=True).start()
    log: list[dict[str, object]] = []
    doors = (Door("POST", "/api/chat", up.url), Door("GET", "/record", up.url))
    edge = edge_server(doors, "127.0.0.1", 0, log=log.append)
    threading.Thread(target=edge.serve_forever, args=(0.05,), daemon=True).start()
    yield up, f"http://127.0.0.1:{edge.server_address[1]}", log
    up.release.set()
    for s in (edge, up.server):
        s.shutdown()
        s.server_close()


def status(url: str, method: str = "GET", body: bytes | None = None) -> int:
    """The status the edge answers with."""
    try:
        with urllib.request.urlopen(urllib.request.Request(url, body, method=method), timeout=5) as r:
            return int(r.status)
    except urllib.error.HTTPError as e:
        return int(e.code)


def test_route_is_exact_on_method_and_path_and_ignores_the_query() -> None:
    doors = (Door("POST", "/api/chat", "http://up"),)
    assert route(doors, "POST", "/api/chat") == doors[0]
    assert route(doors, "POST", "/api/chat?x=1") == doors[0]
    for method, path in [("GET", "/api/chat"), ("POST", "/api/chat/"), ("POST", "/api/chat/../pull"), ("POST", "/")]:
        assert route(doors, method, path) is None


def test_a_listed_route_streams_through_with_only_its_content_type(stack) -> None:
    """The first part arrives while the upstream still holds the second; cookies and credentials stay at the edge."""
    up, edge, _ = stack
    req = urllib.request.Request(
        f"{edge}/api/chat",
        b'{"model": "m"}',
        {"Content-Type": "application/json", "Authorization": "Bearer stolen", "Cookie": "s=1"},
        method="POST",
    )
    with urllib.request.urlopen(req, timeout=5) as r:
        assert json.loads(r.readline()) == {"part": 1}  # read before the upstream is released
        up.release.set()
        assert json.loads(r.readline()) == {"part": 2}
    headers = {k.lower(): v for k, v in up.seen[0]["headers"].items()}  # type: ignore[union-attr]
    assert headers["content-type"] == "application/json"
    assert "authorization" not in headers and "cookie" not in headers
    assert up.seen[0]["body"] == b'{"model": "m"}'


def test_the_query_passes_through(stack) -> None:
    up, edge, _ = stack
    up.release.set()
    assert status(f"{edge}/record?incident=i7") == 200
    assert up.seen[0]["path"] == "/record?incident=i7"


@pytest.mark.parametrize(
    "method, path",
    [("GET", "/api/tags"), ("POST", "/api/pull"), ("GET", "/api/chat"), ("POST", "/api/chat/../pull"), ("DELETE", "/")],
)
def test_anything_not_listed_is_refused_and_logged_and_never_reaches_the_upstream(stack, method, path) -> None:
    up, edge, log = stack
    assert status(f"{edge}{path}", method, b"{}" if method == "POST" else None) == 403
    assert not up.seen and log[-1]["edge"] == "refused"


def test_the_health_check_is_answered_by_the_edge_itself(stack) -> None:
    up, edge, _ = stack
    assert status(f"{edge}/healthz") == 200 and not up.seen


def test_an_upstream_that_is_down_is_a_502(stack) -> None:
    _, _, _ = stack
    log: list[dict[str, object]] = []
    edge = edge_server((Door("POST", "/api/chat", "http://127.0.0.1:1"),), "127.0.0.1", 0, log=log.append)
    threading.Thread(target=edge.serve_forever, args=(0.05,), daemon=True).start()
    try:
        assert status(f"http://127.0.0.1:{edge.server_address[1]}/api/chat", "POST", b"{}") == 502
        assert log[-1]["edge"] == "upstream failed"
    finally:
        edge.shutdown()
        edge.server_close()


def test_a_chunked_body_is_refused_not_forwarded_empty(stack) -> None:
    """Without this, the upstream would get no body and answer with an error that looks like its own."""
    import http.client

    up, edge, _ = stack
    conn = http.client.HTTPConnection(edge.removeprefix("http://"), timeout=5)
    conn.request("POST", "/api/chat", body=iter([b'{"model": "m"}']), headers={"Content-Type": "application/json"})
    assert conn.getresponse().status == 411 and not up.seen
    conn.close()
