"""Tests for simprod/controls.py: settings, pilot windows, bars, and one storm with Docker faked."""

from pathlib import Path
import re
import time

import pytest

from simprod import controls, loadtest
from simprod.faults import storms
from wyld3rness.loop.verdict import Second, seconds_from

GATEWAY_METRICS = """gateway_backoff_enabled 1
gateway_rate_caps{key="acct-0042"} 10
gateway_top_keys_rps{key="acct-0042"} 120
"""


def test_gateway_settings_are_read_from_its_metrics() -> None:
    assert controls.gateway_settings(GATEWAY_METRICS) == {"backoff": True, "caps": {"acct-0042": 10.0}}
    assert controls.gateway_settings("gateway_backoff_enabled 0\n") == {"backoff": False, "caps": {}}


def test_pilot_windows_double_the_slowest_recovery_and_the_longest_false_one() -> None:
    def run(arm: str, goods: str, t_act: float = 0.0) -> dict:
        """A pilot run record whose series follows `goods` (g good, b bad)."""
        return {
            "arm": arm,
            "t_act": t_act,
            "series": [[float(i + 1), 200, 0] if c == "g" else [float(i + 1), 100, 100] for i, c in enumerate(goods)],
        }

    runs = [
        run("scripted", "bbb" + "g" * 20),
        run("scripted", "bbbbbbb" + "g" * 20),
        run("nobody", "b" * 5 + "g" * 20 + "b" * 10),
        run("nobody", "b" * 20),
    ]
    w = controls.pilot_windows(runs)
    # Judged over 5 s (wyld3rness.loop.verdict.judged), a second turns good once the bad ones leave its window:
    # 4 s after the series does.
    assert w["recovery_s"] == [8.0, 12.0] and w["nobody_longest_good_s"] == [16.0, 0.0]
    assert w["wait_s"] == 25 and w["hold_s"] == 35  # 2 x 12 → 25; 2 x 16 = 32 → 35
    with pytest.raises(ValueError):
        controls.pilot_windows([run("scripted", "b" * 30)])


def test_bars() -> None:
    def r(arm: str, recovered: bool, verified: bool = False, restored: bool | None = None, status: int = 200) -> dict:
        """A control run record with only the fields `bars` reads."""
        sent = {"nobody": 0, "scripted": 2, "wrong": 1}[arm]
        out = {
            "arm": arm,
            "recovered": recovered,
            "verified": verified,
            "actions": [{"status": status}] * sent,
            "undos": [{"status": 200}] if arm == "wrong" else [],
            "warm_ok": True,
            "invalid": [],
        }
        if restored is not None:
            out["restored"] = restored
        return out

    runs = [r("nobody", False), r("scripted", True, True), r("wrong", False, False, True)]
    assert controls.bars(runs)["pass"]
    assert not controls.bars([*runs, r("nobody", True)])["pass"]  # a storm that healed by itself
    assert not controls.bars([*runs, r("wrong", False, False, False)])["pass"]  # undo did not restore
    assert not controls.bars([*runs, r("scripted", True, True, status=400)])["pass"]  # a refused fix is not a fix
    assert not controls.bars([*runs, r("wrong", False, False, True, status=503)])["pass"]
    unjudged = {**r("nobody", False), "invalid": ["1 counter resets"]}
    b = controls.bars([*runs, unjudged])
    assert not b["pass"] and b["invalid"] == 1 and b["nobody"] == {"met": 1, "of": 2}  # a miss, not a pass


def test_a_run_is_judged_only_when_measured_whole() -> None:
    ok = {"counter_resets": 0, "max_gap_s": 1.1, "late_share": 0.0}
    assert controls.invalid(ok) == []
    assert controls.invalid({**ok, "counter_resets": 1}) == ["1 counter resets"]
    assert controls.invalid({**ok, "max_gap_s": 4.0}) == ["scrape gap 4.0 s"]
    assert controls.invalid({**ok, "late_share": loadtest.LATE_BAR + 0.001})[0].startswith("late sends")
    assert controls.invalid({**ok, "clear_error": "x"}) == ["faults not cleared"]


def test_an_action_that_cannot_reach_the_edge_is_recorded_not_raised(monkeypatch) -> None:
    def down(*a, **k):
        """The edge does not answer."""
        raise TimeoutError("timed out")

    monkeypatch.setattr(controls.urllib.request, "urlopen", down)
    a = controls.act("/gateway/actions/backoff", {"enabled": True})
    assert a["status"] == 0 and "TimeoutError" in a["answer"]
    assert "error" in controls.read_settings()


def test_controls_refuse_to_run_without_windows(monkeypatch) -> None:
    """The refusal touches nothing: no compose call, no restore."""
    monkeypatch.setattr(controls, "WAIT_S", None)

    def no_docker(*a, **k):
        """Fail the test if anything reaches Docker."""
        raise AssertionError("refusal must not call docker compose")

    monkeypatch.setattr(controls.loadtest, "compose", no_docker)
    monkeypatch.setattr(controls.loadtest, "restore", no_docker)
    assert controls.main(["run"]) == 2


def test_storms_run_with_the_pinned_gateway_and_the_soak_ledger() -> None:
    retry, hot, compound = storms(3, 200, seed=9)
    env = controls.storm_env(retry, 200)
    assert (
        env["GATEWAY_RETRIES"] == "3" and env["LEDGER_SERVICE_MS"] == "10" and env["CUSTOMER_SEED"] == str(retry.seed)
    )
    backoff = {s.kind: controls.storm_env(s, 200)["GATEWAY_BACKOFF"] for s in (retry, hot, compound)}
    assert backoff == {"retry_storm": "0", "hot_shard": "1", "compound": "0"}


def test_the_compose_defaults_are_the_operating_point() -> None:
    """What `make sim-up` starts is what the storms run: the same R and the same ledger."""
    text = (Path(loadtest.COMPOSE)).read_text()

    def default(name: str) -> str:
        """The default compose gives `name`."""
        found = re.search(r"\$\{" + name + r":-([^}]+)\}", text)
        assert found, name
        return found.group(1)

    assert default("CUSTOMER_RATE") == str(controls.RATE)
    assert default("LEDGER_SERVICE_MS") == loadtest.soak_ledger(controls.RATE)["LEDGER_SERVICE_MS"]
    assert default("LEDGER_WORKERS") == "1"
    assert default("GATEWAY_BACKOFF") == loadtest.PINNED["GATEWAY_BACKOFF"] == "0"  # as shipped
    for name, value in controls.STORM_FIXED.items():
        assert default(name) == value, name


def test_a_run_that_cannot_be_judged_runs_once_more_and_both_are_kept(monkeypatch, tmp_path) -> None:
    """Entry 011: a second try only for a run that cannot be judged, never for a verdict."""
    tries: dict[tuple[str, str], int] = {}

    def fake_run(storm, arm, rate, wait_s, hold_s) -> dict:
        """The first scripted try of the first storm cannot be judged; everything else can."""
        n = tries[storm.id, arm] = tries.get((storm.id, arm), 0) + 1
        bad = storm.id.endswith("-00") and arm == "scripted" and n == 1
        return {
            "storm": {"id": storm.id, "kind": storm.kind},
            "arm": arm,
            "warm_ok": True,
            "verified": arm == "scripted",
            "recovered": arm == "scripted",
            "recovered_at": None,
            "customer_minutes": 0.0,
            "actions": [{"path": "/x", "status": 200}] if arm != "nobody" else [],
            "undos": [{"path": "/x", "status": 200}] if arm == "wrong" else [],
            "restored": True,
            "invalid": ["late sends 2.0%"] if bad else [],
        }

    monkeypatch.setattr(controls, "run_storm", fake_run)
    monkeypatch.setattr(controls, "CONTROLS_STORMS", 3)
    monkeypatch.setattr(controls.loadtest, "conditions", dict)
    monkeypatch.setattr(controls, "results_path", lambda name: tmp_path / "out.json")
    assert controls.controls() == 0
    report = __import__("json").loads((tmp_path / "out.json").read_text())
    assert len(report["runs"]) == 9 and len(report["not_judged_first"]) == 1
    assert report["bars"]["second_tries"] == 1 and max(tries.values()) == 2


class _FakeScraper:
    """A scraper whose series is already measured: 200 s of customers, all failing after t = 10."""

    def __init__(self, url: str) -> None:
        self.start = time.monotonic()
        self.scrapes = [(float(t), 200.0 * min(t, 10), 100.0 * max(t - 10, 0)) for t in range(201)]
        self.late = [(*s, 0.0) for s in self.scrapes]
        self.errors = 0

    def scrape(self) -> None:
        """Nothing to read: the series is fixed."""

    def series(self) -> list[Second]:
        """Every interval, as the real scraper gives it."""
        return seconds_from(self.scrapes)


def _no_docker(monkeypatch) -> list[str]:
    """Replace every call that would reach Docker or the edge; returns the chaos commands sent."""
    sent: list[str] = []

    def chaos(*args: str) -> dict:
        """Record the command instead of running the chaos container."""
        sent.append(args[0])
        return {}

    monkeypatch.setattr(controls, "reset", lambda storm, rate: None)
    monkeypatch.setattr(controls, "chaos", chaos)
    monkeypatch.setattr(controls, "read_settings", lambda: {"backoff": False, "caps": {}})
    monkeypatch.setattr(controls, "Scraper", _FakeScraper)
    monkeypatch.setattr(controls.time, "sleep", lambda s: None)
    return sent


def test_a_storm_nobody_fixes_is_measured_and_cleared(monkeypatch) -> None:
    sent = _no_docker(monkeypatch)
    r = controls.run_storm(storms(1, 200, 1)[0], "nobody", 200, 30, 30)
    assert sent == ["inject", "clear"] and r["actions"] == [] and r["undos"] == []
    assert r["recovered"] is False and r["invalid"] == [] and "t_verify" in r and len(r["series"]) == 200


def test_the_faults_are_cleared_when_an_arm_raises(monkeypatch) -> None:
    sent = _no_docker(monkeypatch)
    with pytest.raises(RuntimeError), controls.live_storm(storms(1, 200, 1)[0], "loop", 200, 30, 30) as live:
        raise RuntimeError("the arm failed")
    assert sent == ["inject", "clear"] and "recovered" not in live.record  # a run cut short has no verdict
