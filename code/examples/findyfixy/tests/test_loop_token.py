"""Only the host may start an incident; the agent sits behind its edge; the probe covers every port (ADR 0009, step 0b).

The loop container shares a network with the agent's container, so its incident
routes need the loop's token, which only the host holds. These tests start the
real loop handler on a free loopback port and ask it as the agent would (no
token, a wrong one) and as the host does. No services, no model: the handler
answers 401 before it reads a body, and a body with no incident id, sent with the
token, is a 400.
"""

from __future__ import annotations

from pathlib import Path
import re
import threading
import urllib.error
import urllib.request

import pytest

from findyfixy.oncall import cli, probe
from findyfixy.oncall.service import AGENT_DOOR, MODEL_DOOR, edge_doors, loop_server
from findyfixy.paths import ROOT, WORKSPACE
from wyld3rness.loop.credential import authorized

TOKEN = "test-tok"  # a placeholder: the loop compares whatever it is given


@pytest.fixture
def token_file(tmp_path: Path) -> Path:
    """The loop's token file, holding TOKEN."""
    path = tmp_path / "loop-token"
    path.write_text(TOKEN + "\n")
    return path


@pytest.fixture
def loop_url(tmp_path: Path, token_file: Path):
    """The real loop handler on a free loopback port, reading `token_file`; stopped after the test."""
    (tmp_path / "pass").write_text("a sign pass")  # read before the body, so a body with no id is the 400
    server = loop_server(
        "http://127.0.0.1:1", "http://127.0.0.1:1", "http://127.0.0.1:1", tmp_path / "pass", token_file, "127.0.0.1", 0
    )
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    yield f"http://127.0.0.1:{server.server_address[1]}"
    server.shutdown()
    server.server_close()


def status(url: str, method: str = "GET", body: bytes | None = None, token: str | None = None) -> int:
    """The HTTP status the loop answers with."""
    headers = {"Authorization": f"Bearer {token}"} if token is not None else {}
    req = urllib.request.Request(url, body, headers, method=method)
    try:
        with urllib.request.urlopen(req, timeout=5) as r:
            return int(r.status)
    except urllib.error.HTTPError as e:
        return int(e.code)


@pytest.mark.parametrize("token, expected", [(None, 401), ("wrong", 401), ("", 401), (TOKEN, 400)])
def test_only_the_loops_token_reaches_the_incident_handler(loop_url, token, expected) -> None:
    """No token, a wrong or an empty one: 401 before the body is read. The right one reaches the handler (400)."""
    assert status(f"{loop_url}/incident", "POST", b"{}", token) == expected


@pytest.mark.parametrize("token, expected", [(None, 401), ("wrong", 401), (TOKEN, 404)])
def test_the_events_need_the_token_too(loop_url, token, expected) -> None:
    """An incident's events are the loop's view of it: 401 without the token, 404 for an incident it never opened."""
    assert status(f"{loop_url}/incident/events?incident=none&from=0", token=token) == expected


def test_a_rewritten_token_is_used_at_once_and_a_missing_file_refuses_everyone(loop_url, token_file: Path) -> None:
    """The file is read per request: a new token works with no restart; with no file, even the old token is refused."""
    token_file.write_text("a-new-one\n")
    assert status(f"{loop_url}/incident", "POST", b"{}", TOKEN) == 401
    assert status(f"{loop_url}/incident", "POST", b"{}", "a-new-one") == 400
    token_file.unlink()
    assert status(f"{loop_url}/incident", "POST", b"{}", "a-new-one") == 401


def test_the_health_check_stays_open(loop_url) -> None:
    """The container's health check has no token and tells nothing."""
    assert status(f"{loop_url}/healthz") == 200


def test_an_empty_token_never_authorizes() -> None:
    """A loop started with an empty token refuses every caller instead of accepting any."""
    assert not authorized("Bearer ", "")
    assert not authorized(None, "")
    assert not authorized("Basic " + TOKEN, TOKEN)
    assert authorized("Bearer " + TOKEN, TOKEN)


def test_the_host_sends_its_token_and_names_the_fix_without_one(tmp_path: Path, monkeypatch) -> None:
    """The runner and the console read the token file; with none, the error says to run make oncall-up."""
    token_file = tmp_path / "token"
    monkeypatch.setattr(cli, "LOOP_TOKEN", token_file)
    with pytest.raises(FileNotFoundError, match="make oncall-up"):
        cli.loop_request("/incident", b"{}")
    token_file.write_text(TOKEN + "\n")
    req = cli.loop_request("/incident", b"{}")
    assert req.get_header("Authorization") == f"Bearer {TOKEN}"
    assert req.get_header("Content-type") == "application/json"


COMPOSE_FILES = (
    ROOT / "docker-compose.yml",
    WORKSPACE / "examples/simprod/docker-compose.yml",
    WORKSPACE / "infra/langfuse/docker-compose.yml",
)
# A `ports` entry: an optional address, the host port, the container port.
PORT_LINE = re.compile(r'^\s*-\s*"?(?:([\d.]+):)?(\d+):\d+"?\s*$', re.MULTILINE)


def test_the_probe_tries_every_port_the_stack_publishes() -> None:
    """A port published by any compose file must be in the probe, so a new one cannot go unchecked."""
    published = {int(port) for f in COMPOSE_FILES for _, port in PORT_LINE.findall(f.read_text())}
    probed = {p.port for p in probe.HOST_PORTS}
    assert published and published <= probed, f"published ports the probe does not try: {sorted(published - probed)}"


def test_nothing_is_published_beyond_this_machine() -> None:
    """Every published port is bound to 127.0.0.1. Without the address Docker binds every interface."""
    entries = [(f.name, addr, port) for f in COMPOSE_FILES for addr, port in PORT_LINE.findall(f.read_text())]
    assert entries and all(addr == "127.0.0.1" for _, addr, _ in entries), entries


def service_block(compose: str, name: str) -> str:
    """One service's lines from a compose file: from `  name:` to the next two-space key."""
    match = re.search(rf"^  {re.escape(name)}:\n((?:    .*\n|\s*\n)*)", compose, re.MULTILINE)
    assert match, f"no service {name!r}"
    return match.group(1)


def test_the_agent_sits_on_an_internal_network_with_no_way_to_the_host() -> None:
    """No host gateway, no published port, one network, and that network is internal."""
    compose = (ROOT / "docker-compose.yml").read_text()
    agent = service_block(compose, "agent")
    assert "networks: [agent]" in agent
    assert "extra_hosts" not in agent and "ports:" not in agent
    assert re.search(r"^  agent:\n    internal: true$", compose.split("\nnetworks:\n", 1)[1], re.MULTILINE)
    assert "OLLAMA_HOST: http://agent-edge:11434" in agent


def test_the_edge_forwards_one_model_route_out_and_the_agents_own_routes_in() -> None:
    """The model door lists exactly the route the model client calls; the agent door, the agent's three."""
    doors = edge_doors("http://ollama", "http://agent")
    assert [(d.method, d.path, d.upstream) for d in doors[MODEL_DOOR]] == [("POST", "/api/chat", "http://ollama")]
    agent_routes = {(d.method, d.path) for d in doors[AGENT_DOOR]}
    assert agent_routes == {("POST", "/decide"), ("GET", "/record"), ("GET", "/healthz")}
    assert all(d.upstream == "http://agent" for d in doors[AGENT_DOOR])
