"""Tests for simprod/edge.py: the allow-list is the only door, and it never opens onto a fault control."""

import pytest

from simprod.edge import ALLOW, CUSTOMERS, SERVICES, Edge, build, openapi, route
from simprod.service import Metrics


class FakeUpstream:
    def __init__(self) -> None:
        self.calls: list[tuple] = []

    def raw(self, method, path, body, timeout_s, headers=None):
        self.calls.append((method, path, body, dict(headers or {})))
        return 200, "application/json", b'{"ok": true}'


def edge() -> tuple[Edge, dict[str, FakeUpstream]]:
    ups = {s: FakeUpstream() for s in (*SERVICES, CUSTOMERS)}
    return Edge(ups, Metrics()), ups  # type: ignore[arg-type]


@pytest.mark.parametrize(
    "method,path",
    [
        ("GET", "/ledger/fault"),
        ("POST", "/ledger/fault"),
        ("POST", "/ledger/fault/clear"),
        ("GET", "/ledger/../ledger/fault"),
        ("GET", "/ledger//fault"),
        ("GET", "/ledger:9000/fault"),
        ("POST", "/ledger/entries"),
        ("POST", "/notifications/notify"),  # internal calls stay internal
        ("GET", "/gateway/pay"),
        ("POST", "/gateway/pay"),  # customers pay inside the network; the host never does
        ("DELETE", "/gateway/healthz"),
        ("GET", "/loadgen/metrics"),
        ("GET", "/customers/fault"),
        ("POST", "/customers/fault"),
        ("POST", "/customers/actions/restart"),
        ("GET", "/"),
    ],
)
def test_anything_not_listed_is_refused(method, path) -> None:
    e, ups = edge()
    assert route(method, path) is None
    resp = build(e).dispatch(method, path, b"", {})
    assert resp.status == 404 and all(not u.calls for u in ups.values())


def test_no_allowed_entry_names_a_fault_or_the_admin_port() -> None:
    for a in ALLOW:
        assert "fault" not in a.path and "9000" not in a.path


def test_an_allowed_request_reaches_the_right_service_and_path() -> None:
    e, ups = edge()
    resp = build(e).dispatch(
        "POST",
        "/gateway/actions/backoff",
        b'{"enabled": true}',
        {"x-actor": "agent", "Cookie": "dropped", "content-type": "application/json"},
    )
    assert resp.status == 200
    method, path, body, headers = ups["gateway"].calls[0]
    assert (method, path, body) == ("POST", "/actions/backoff", b'{"enabled": true}')
    assert headers == {"Content-Type": "application/json", "X-Actor": "agent"}  # only the two named headers


def test_every_service_can_be_inspected_and_restarted() -> None:
    for s in SERVICES:
        for p in ("/healthz", "/metrics", "/openapi.json"):
            assert route("GET", f"/{s}{p}") is not None
        assert route("POST", f"/{s}/actions/restart") is not None


def test_an_unreachable_service_is_a_502() -> None:
    class Down:
        def raw(self, *a, **k):
            raise OSError("refused")

    e = Edge(dict.fromkeys(SERVICES, Down()), Metrics())  # type: ignore[arg-type]
    assert build(e).dispatch("GET", "/ledger/healthz", b"", {}).status == 502


def test_the_spec_lists_exactly_the_allow_list() -> None:
    spec = openapi("http://127.0.0.1:8100")
    listed = {(m.upper(), p) for p, ops in spec["paths"].items() for m in ops}
    assert listed == {(a.method, f"/{a.service}{a.path}") for a in ALLOW}


def test_customers_can_be_read_but_not_driven() -> None:
    assert route("GET", "/customers/metrics") is not None
    assert all(a.method == "GET" for a in ALLOW if a.service == CUSTOMERS)
