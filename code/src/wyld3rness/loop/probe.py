"""The boundary probe: from inside an agent's container, what can it reach? (ADR 0009, step 0b)

    app's routes + host ports ──► run() inside the agent's container
                                    │ a default route out? (/proc/net/route)
                                    │ each route: peers by name, the host by name and by the
                                    │ bridge's address, an outside name, the internet
                                    └─► one line per route, then a verdict; exit 1 if any fails

Why a probe and not a claim: an agent on a Docker network marked internal has
no default route and reaches only its peers. A compose change can undo that
silently, so the boundary is measured every time the agent starts.

What each answer means. "closed": nothing answered (no route, no name). For a
peer, the agent's own neighbours (its loop, its edge), "refused": a 401 or 403,
a credential the agent does not hold or a route not on the edge's list. For
everything else, the host and the outside, any answer at all is "open": a 401
from a host port still proves the agent got there, and a guard such as a Host
check can be talked past. "needed": the agent's job uses it, and the answer
came from the upstream itself; a needed route the edge refuses (401, 403) is
"broken", because every decision would then fail. A 502 there is "no upstream
yet": the edge is right but the model server is not answering (it is started
by hand, after the stack), which passes with that line in the output.
"open" and "broken" fail the probe.

Standard library only: it runs in the agent's image. The app supplies the
routes and its published ports (findyfixy/oncall/probe.py).
"""

from __future__ import annotations

from collections.abc import Callable, Sequence
from dataclasses import dataclass
import json
from pathlib import Path
import socket
import urllib.error
import urllib.request

TIMEOUT_S = 3.0
ROUTE_TABLE = Path("/proc/net/route")
DNS = "DNS"  # a Route's method for "can this name be resolved"
FAILING = ("open", "broken")


@dataclass(frozen=True)
class Route:
    """One thing the agent could try.

    An HTTP request; a bare TCP connection when `method` is empty (url is
    host:port); a name lookup when `method` is DNS (url is the name).
    """

    name: str
    url: str
    method: str = "GET"
    body: bytes | None = None
    peer: bool = False  # one of the agent's neighbours: 401 and 403 are refusals, not a way out
    needed: bool = False  # the agent's job uses it


@dataclass(frozen=True)
class HostPort:
    """A port the stack publishes on the host's loopback, and a harmless request to it (method "" for TCP only)."""

    port: int
    what: str
    path: str = "/"
    method: str = "GET"
    body: bytes | None = None


def host_routes(ports: Sequence[HostPort], address: str, label: str) -> tuple[Route, ...]:
    """Every published port at `address`, as routes. Pure. None is a peer or needed: any answer is a way out."""
    return tuple(
        Route(
            f"{label}: {p.what}",
            f"{address}:{p.port}" if not p.method else f"http://{address}:{p.port}{p.path}",
            p.method,
            p.body,
        )
        for p in ports
    )


def verdict_for(route: Route, status: int | None) -> str:
    """What one answer means: closed, needed, no upstream yet, broken, refused or open. Pure.

    `status` is the HTTP status, 0 for a TCP connection that opened or a name
    that resolved, and None when nothing answered.
    """
    if status is None:
        return "closed"
    if route.needed:
        return "broken" if status in (401, 403) else "no upstream yet" if status == 502 else "needed"
    if route.peer and status in (401, 403):
        return "refused"
    return "open"


def _hex_ip(field: str) -> list[int]:
    """The four bytes of an address as /proc/net/route writes it: hex, little-endian."""
    return list(int(field, 16).to_bytes(4, "little"))


def routes_out(table: str) -> tuple[bool, str | None]:
    """From the text of /proc/net/route: is there a default route, and the bridge's address. Pure.

    The bridge's address is the first host of the container's own subnet, where
    Docker puts the network's gateway even on an internal network; a service
    the host's VM listens on there would be reachable with no default route.
    """
    default, bridge = False, None
    for line in table.splitlines()[1:]:
        cols = line.split()
        if len(cols) < 8:
            continue
        if cols[1] == "00000000":
            default = True
        elif bridge is None:
            net = _hex_ip(cols[1])
            mask = _hex_ip(cols[7])
            first = [n & m for n, m in zip(net, mask, strict=True)]
            first[3] += 1
            bridge = ".".join(map(str, first))
    return default, bridge


def attempt(route: Route) -> int | None:
    """Try one route once. The HTTP status, 0 for an open connection or a resolved name, None for nothing."""
    if route.method == DNS:
        try:
            socket.getaddrinfo(route.url, 443)
            return 0
        except OSError:
            return None
    if not route.method:
        host, port = route.url.rsplit(":", 1)
        try:
            with socket.create_connection((host, int(port)), timeout=TIMEOUT_S):
                return 0
        except OSError:
            return None
    headers = {"Content-Type": "application/json"} if route.body is not None else {}
    req = urllib.request.Request(route.url, route.body, headers, method=route.method)
    try:
        with urllib.request.urlopen(req, timeout=TIMEOUT_S) as r:
            return int(r.status)
    except urllib.error.HTTPError as e:
        return int(e.code)
    except OSError:  # name not found, no route, refused, timed out
        return None


def run(
    routes: Sequence[Route],
    ports: Sequence[HostPort],
    table: str | None = None,
    try_: Callable[[Route], int | None] = attempt,
) -> list[dict[str, object]]:
    """Every result, in order: the default route, each route, then every published port on the bridge's address.

    `table` is the route table's text (read from /proc when None); `try_` is injectable so tests send nothing.
    """
    if table is None:
        table = ROUTE_TABLE.read_text() if ROUTE_TABLE.exists() else ""
    default, bridge = routes_out(table)
    out: list[dict[str, object]] = [
        {"route": "a default route out", "status": None, "verdict": "open" if default else "closed"}
    ]
    for route in (*routes, *(host_routes(ports, bridge, f"bridge {bridge}") if bridge else ())):
        status = try_(route)
        out.append({"route": route.name, "status": status, "verdict": verdict_for(route, status)})
    return out


def report(results: Sequence[dict[str, object]]) -> int:
    """Print one JSON line per result and a summary; 1 if any result is open or broken."""
    for r in results:
        print(json.dumps(r), flush=True)
    failed = [str(r["route"]) for r in results if r["verdict"] in FAILING]
    print(json.dumps({"probe": "fail" if failed else "pass", "failed": failed}), flush=True)
    return 1 if failed else 0
