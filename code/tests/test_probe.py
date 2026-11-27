"""The boundary probe's verdicts and its reading of the route table (wyld3rness.loop.probe). Nothing is sent."""

from __future__ import annotations

import pytest

from wyld3rness.loop.probe import HostPort, Route, report, routes_out, run, verdict_for

PEER = Route("peer", "http://loop/x", peer=True)
HOST = Route("host", "http://host/x")
NEEDED = Route("model", "http://edge/api/chat", "POST", b"{}", needed=True)


@pytest.mark.parametrize(
    "route, status, verdict",
    [
        (PEER, 401, "refused"),
        (PEER, 403, "refused"),  # the loop wants its token; the edge does not list the route
        (PEER, 200, "open"),
        (PEER, 400, "open"),  # reached a handler without a credential
        (HOST, 401, "open"),  # a host port that asks for a credential was still reached
        (HOST, 403, "open"),  # a Host check is a refusal a client can talk past
        (HOST, None, "closed"),
        (Route("tcp", "h:1", ""), 0, "open"),
        (Route("name", "example.com", "DNS"), 0, "open"),
        (NEEDED, 400, "needed"),  # the upstream itself answered
        (NEEDED, 403, "broken"),  # the edge stopped listing it: every decision would fail
        (NEEDED, 502, "no upstream yet"),  # the edge is right, the model server is not up yet: passes
        (NEEDED, None, "closed"),
    ],
)
def test_each_answer_means_one_verdict(route, status, verdict) -> None:
    assert verdict_for(route, status) == verdict


# /proc/net/route as Docker writes it: a container on an ordinary network (a default route via
# 172.18.0.1) and one on an internal network (only its own subnet, 172.19.0.0/16).
HEADER = "Iface\tDestination\tGateway \tFlags\tRefCnt\tUse\tMetric\tMask\t\tMTU\tWindow\tIRTT\n"
ORDINARY = HEADER + (
    "eth0\t00000000\t010012AC\t0003\t0\t0\t0\t00000000\t0\t0\t0\n"
    "eth0\t000012AC\t00000000\t0001\t0\t0\t0\t0000FFFF\t0\t0\t0\n"
)
INTERNAL = HEADER + "eth0\t000013AC\t00000000\t0001\t0\t0\t0\t0000FFFF\t0\t0\t0\n"


@pytest.mark.parametrize(
    "table, expected",
    [(ORDINARY, (True, "172.18.0.1")), (INTERNAL, (False, "172.19.0.1")), ("", (False, None))],
)
def test_the_route_table_gives_the_default_route_and_the_bridge(table, expected) -> None:
    assert routes_out(table) == expected


def test_a_default_route_fails_and_the_bridge_gets_every_port_by_address() -> None:
    """A missing name proves nothing, so every published port is also tried on the bridge's address."""
    ports = (HostPort(8300, "loop"), HostPort(5432, "database", "", ""))
    tried: list[str] = []
    results = run((), ports, ORDINARY, lambda r: tried.append(r.url) or None)
    assert results[0] == {"route": "a default route out", "status": None, "verdict": "open"}
    assert tried == ["http://172.18.0.1:8300/", "172.18.0.1:5432"]
    assert run((), ports, "", lambda r: None) == [{"route": "a default route out", "status": None, "verdict": "closed"}]


def test_the_report_fails_on_open_or_broken_and_passes_otherwise(capsys) -> None:
    ok = [{"route": r, "verdict": v} for r, v in (("a", "closed"), ("b", "refused"), ("c", "needed"))]
    assert report(ok) == 0
    assert report([*ok, {"route": "d", "verdict": "broken"}]) == 1
    assert report([*ok, {"route": "e", "verdict": "open"}]) == 1
    assert capsys.readouterr().out.splitlines()[-1] == '{"probe": "fail", "failed": ["e"]}'
