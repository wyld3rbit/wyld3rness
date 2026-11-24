"""Tests for simprod/service.py: dispatch, metrics text, the generated spec, the restart action."""

import json

from simprod.service import BUCKETS_MS, Metrics, Request, Response, Route, Service, restart_route


def echo(req: Request) -> Response:
    return Response(200, {"got": req.body, "actor": req.actor})


def svc(**kw) -> Service:
    return Service("demo", [Route("POST", "/echo", echo, "Echo", body={"type": "object"})], **kw)


def test_dispatch_routes_parses_and_counts() -> None:
    s = svc()
    resp = s.dispatch("POST", "/echo?x=1", b'{"a": 1}', {"X-Actor": "tester"})
    assert resp.status == 200 and resp.body == {"got": {"a": 1}, "actor": "tester"}
    assert s.metrics.value("http_requests_total", {"route": "/echo", "status": "200"}) == 1


def test_bad_json_is_400_and_an_unknown_path_is_404() -> None:
    s = svc()
    assert s.dispatch("POST", "/echo", b"{not json", {}).status == 400
    assert s.dispatch("POST", "/echo", b"[1]", {}).status == 400
    assert s.dispatch("GET", "/nowhere", b"", {}).status == 404
    assert s.metrics.value("http_requests_total", {"route": "other", "status": "404"}) == 1


def test_a_handler_bug_is_a_500_not_a_dead_thread(capsys) -> None:
    def boom(req: Request) -> Response:
        raise RuntimeError("bug")

    s = Service("demo", [Route("POST", "/boom", boom, "Boom")])
    assert s.dispatch("POST", "/boom", b"", {}).status == 500
    assert "handler_error" in capsys.readouterr().out


def test_an_action_is_logged_as_json_with_its_actor(capsys) -> None:
    s = Service("demo", [Route("POST", "/actions/x", echo, "X", action=True)])
    s.dispatch("POST", "/actions/x", b'{"v": 2}', {"X-Actor": "agent-7"})
    line = json.loads(capsys.readouterr().out.strip().splitlines()[-1])
    assert line["event"] == "action" and line["actor"] == "agent-7" and line["params"] == {"v": 2}


def test_metrics_render_in_the_prometheus_text_format() -> None:
    m = Metrics()
    m.counter("c_total", "a counter")
    m.histogram("h_ms", "a histogram")
    m.gauge("g", "a gauge", lambda: {(("shard", "0"),): 3.0})
    m.inc("c_total", {"outcome": "ok"}, 2)
    m.observe("h_ms", 7.0)
    text = m.render()
    assert "# TYPE c_total counter" in text and 'c_total{outcome="ok"} 2' in text
    assert 'h_ms_bucket{le="5"} 0' in text and 'h_ms_bucket{le="10"} 1' in text  # cumulative
    assert 'h_ms_bucket{le="+Inf"} 1' in text and "h_ms_count 1" in text and "h_ms_sum 7" in text
    assert 'g{shard="0"} 3' in text
    assert len([ln for ln in text.splitlines() if ln.startswith("h_ms_bucket")]) == len(BUCKETS_MS) + 1


def test_the_spec_is_generated_from_the_routes() -> None:
    spec = svc(public_url="http://x/demo").openapi()
    assert spec["openapi"] == "3.1.0" and spec["servers"] == [{"url": "http://x/demo"}]
    assert set(spec["paths"]) == {"/healthz", "/metrics", "/openapi.json", "/echo"}
    assert "requestBody" in spec["paths"]["/echo"]["post"]


def test_restart_answers_then_exits_the_process() -> None:
    exited: list[int] = []
    route = restart_route("demo", exit=exited.append, delay_s=0.0)
    resp = route.handler(Request({}, {}))
    import time

    time.sleep(0.05)
    assert resp.status == 202 and exited == [0] and route.action


def test_label_values_are_escaped() -> None:
    m = Metrics()
    m.counter("c_total", "a counter")
    m.inc("c_total", {"key": 'a"b\\c\nd'})
    assert 'c_total{key="a\\"b\\\\c\\nd"} 1' in m.render()


def test_nan_and_infinity_are_not_json() -> None:
    s = svc()
    assert s.dispatch("POST", "/echo", b'{"a": NaN}', {}).status == 400
    assert s.dispatch("POST", "/echo", b'{"a": Infinity}', {}).status == 400


def test_numbers_counts_and_keys() -> None:
    from simprod.service import count, is_key, number

    assert number(True) is None and number(float("inf")) is None and number(2) == 2.0
    assert count(True) is None and count(3) == 3 and count(3.0) is None
    assert is_key("acct-0001") and not is_key('a"b') and not is_key("") and not is_key("x" * 65)
