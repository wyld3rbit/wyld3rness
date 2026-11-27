"""FindyFixy's boundary: what its agent's container must not reach (ADR 0009, step 0b).

    make oncall-up ──► docker compose exec agent python -m findyfixy.oncall.probe
                        └─► wyld3rness.loop.probe.run(ROUTES, HOST_PORTS): exit 1 if any way out is open

The harness does the probing (`wyld3rness.loop.probe`); this file is the app's
part: the agent's peers, the names it must not resolve, and every port the
stack publishes. `test_the_probe_tries_every_port_the_stack_publishes` keeps
HOST_PORTS whole, so a new published port cannot go unchecked.

Every request is chosen so that nothing changes if a guard is missing: the loop
gets an empty incident, simprod's edge gets backoff off (its default), the
agent door gets a route it does not list, the rest get reads.
"""

from __future__ import annotations

import sys

from wyld3rness.loop.probe import DNS, HostPort, Route, host_routes, report, run

HOST = "host.docker.internal"  # Docker's name for the host; the agent's container should not resolve it
EDGE = "agent-edge"

HOST_PORTS: tuple[HostPort, ...] = (
    HostPort(8300, "loop, start an incident", "/incident", "POST", b"{}"),
    HostPort(8301, "agent edge, the host's door", "/healthz"),
    HostPort(8100, "simprod edge, an action", "/gateway/actions/backoff", "POST", b'{"enabled": false}'),
    HostPort(8200, "vault", "/v1/sys/mounts"),
    HostPort(8765, "console, the on-call inbox", "/handoffs"),
    HostPort(3000, "Langfuse API", "/api/public/traces"),
    HostPort(9090, "Langfuse object store"),
    HostPort(5432, "database", "", ""),
    HostPort(8080, "database console (Adminer)", "", ""),
    HostPort(11434, "Ollama", "/api/version"),
)

ROUTES: tuple[Route, ...] = (
    Route("loop: start an incident", "http://loop:8000/incident", "POST", b"{}", peer=True),
    Route("loop: read events", "http://loop:8000/incident/events?incident=probe&from=0", peer=True),
    # Ollama answers an empty chat request 400 (no model named): reached, nothing generated.
    Route("agent edge: the model route", f"http://{EDGE}:11434/api/chat", "POST", b"{}", needed=True),
    Route("agent edge: another model route", f"http://{EDGE}:11434/api/tags", peer=True),
    Route("agent edge: pull a model", f"http://{EDGE}:11434/api/pull", "POST", b"{}", peer=True),
    # The host's door forwards the agent's own three routes; anything else must be refused.
    Route("agent edge, host's door: an unlisted route", f"http://{EDGE}:8001/incident", "POST", b"{}", peer=True),
    Route("simprod edge, by name", "http://edge:8000/gateway/actions/backoff", "POST", b'{"enabled": false}'),
    Route("vault, by name", "http://openbao:8200/v1/sys/mounts"),
    *host_routes(HOST_PORTS, HOST, "host by name"),
    Route("an outside name", "example.com", DNS),  # a resolver that forwards out can carry data out
    Route("internet", "1.1.1.1:443", ""),
)


def main() -> int:
    """Probe FindyFixy's boundary from inside the agent's container; 1 if any way out is open."""
    return report(run(ROUTES, HOST_PORTS))


if __name__ == "__main__":
    sys.exit(main())
