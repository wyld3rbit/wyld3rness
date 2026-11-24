"""The whole path on real sockets, in one process: loadgen ─► gateway ─► ledger and notifications, edge in front.

No Docker: every service listens on 127.0.0.1 with a port the OS picks. This
proves the services speak HTTP to each other as deployed (keep-alive, JSON,
statuses), which the fakes in the other tests cannot.
"""

import json
import urllib.error
import urllib.request

import pytest

from simprod import edge as edge_mod
from simprod import gateway as gw_mod
from simprod import ledger as ledger_mod
from simprod import loadgen
from simprod import notifications as notify_mod
from simprod.service import Metrics, Upstream, start


@pytest.fixture
def stack():
    """Start ledger, notifications, gateway and edge; yield the gateway and edge ports; shut down."""
    lg = ledger_mod.Ledger(shards=4, workers=4, service_s=0.001, queue_limit=100, metrics=Metrics())
    public, admin = ledger_mod.build(lg, exit=lambda code: None)
    servers = [start(public, 0, "127.0.0.1"), start(admin, 0, "127.0.0.1")]
    n = notify_mod.Notifications(0.0, Metrics())
    servers.append(start(notify_mod.build(n, exit=lambda code: None), 0, "127.0.0.1"))
    ledger_port, notify_port = servers[0].server_address[1], servers[2].server_address[1]
    gw = gw_mod.Gateway(
        Upstream("127.0.0.1", ledger_port).call,
        Upstream("127.0.0.1", notify_port).call,
        gw_mod.Policy(0.5, 3),
        Metrics(),
    )
    servers.append(start(gw_mod.build(gw, exit=lambda code: None), 0, "127.0.0.1"))
    gw_port = servers[-1].server_address[1]
    ups = {
        "gateway": Upstream("127.0.0.1", gw_port),
        "ledger": Upstream("127.0.0.1", ledger_port),
        "notifications": Upstream("127.0.0.1", notify_port),
    }
    servers.append(start(edge_mod.build(edge_mod.Edge(ups, Metrics())), 0, "127.0.0.1"))
    yield {"gateway": gw_port, "edge": servers[-1].server_address[1], "ledger": lg, "gw": gw}
    for s in servers:
        s.shutdown()
        s.server_close()


def get(port: int, path: str) -> tuple[int, bytes]:
    try:
        with urllib.request.urlopen(f"http://127.0.0.1:{port}{path}", timeout=5) as r:
            return r.status, r.read()
    except urllib.error.HTTPError as e:
        return e.code, e.read()


def test_open_loop_load_through_the_real_services(stack) -> None:
    client = Upstream("127.0.0.1", stack["gateway"])

    def send(key: str, timeout_s: float) -> int:
        return client.call("POST", "/pay", {"key": key, "amount_cents": 100}, timeout_s)[0]

    out = loadgen.run(send, rate=100, seconds=1, seed=1, workers=16)
    assert out["sent"] > 50 and out["outcomes"] == {"ok": out["sent"]}
    writes = sum(
        stack["ledger"].metrics.value("ledger_writes_total", {"shard": str(s), "outcome": "ok"}) for s in range(4)
    )
    assert writes == out["sent"]


def test_inspection_through_the_edge(stack) -> None:
    status, body = get(stack["edge"], "/ledger/metrics")
    assert status == 200 and b"# TYPE ledger_writes_total counter" in body
    status, body = get(stack["edge"], "/gateway/openapi.json")
    assert status == 200 and "/pay" in json.loads(body)["paths"]
    assert get(stack["edge"], "/ledger/fault")[0] == 404


def test_an_action_through_the_edge_changes_the_gateway(stack) -> None:
    req = urllib.request.Request(
        f"http://127.0.0.1:{stack['edge']}/gateway/actions/backoff",
        data=b'{"enabled": true}',
        method="POST",
        headers={"Content-Type": "application/json", "X-Actor": "test"},
    )
    with urllib.request.urlopen(req, timeout=5) as r:
        assert json.loads(r.read()) == {"backoff": True, "was": False}
    assert stack["gw"].policy.backoff
