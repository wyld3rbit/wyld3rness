"""Tests for findyfixy/oncall/script.py and experiment.py: the script's steps, the pilot model, and the analysis."""

from itertools import permutations
import json
from pathlib import Path
from typing import Any

import pytest

from findyfixy.oncall import cli
from findyfixy.oncall.experiment import (
    ARMS,
    HORIZON_S,
    acted_past,
    analyze,
    draw_orders,
    harm,
    mark_counted,
    mean_saving,
    needless,
    script_minutes,
    storms_per_kind_for_correction,
    unjudged,
    window_minutes,
)
from findyfixy.oncall.script import STEPS, busiest_cap, plan
from findyfixy.oncall.service import choose_decider
from wyld3rness.loop.env import Action
from wyld3rness.loop.run import LoopConfig

OBS = """[gateway]
gateway_top_keys_rps{key="acct-0042"} 130
gateway_top_keys_rps{key="acct-0007"} 4

[customers]
customers_rate 200
"""
CAP = Action("rate_cap", {"key": "acct-0042", "rps": 10.0})
BACKOFF = Action("backoff", {"enabled": True})


def test_the_script_caps_the_busiest_key_at_a_twentieth_of_the_customers_rate() -> None:
    assert busiest_cap(OBS) == [CAP]
    assert busiest_cap("[gateway]\n(no samples)") == []
    assert busiest_cap('gateway_top_keys_rps{key="k"} 3\n') == []  # no customers' rate: no guess


def test_the_script_tries_its_steps_in_the_order_given() -> None:
    script = plan(["both", "backoff", "cap"])
    assert script.decide(OBS, []) == [CAP, BACKOFF]
    assert plan(["backoff"]).decide(OBS, []) == [BACKOFF]
    assert plan(["both"]).decide("nothing", []) == []  # never backoff alone in place of both
    with pytest.raises(ValueError, match="unknown script step"):
        plan(["restart"])


def test_the_loop_service_builds_the_script_with_a_measured_zero_cost() -> None:
    body = {"decider": "script", "order": ["cap", "backoff", "both"]}
    decider, record = choose_decider(body, "http://agent", "i1", {"budget_usd": 0.005})
    assert decider.decide(OBS, []) == [CAP]
    assert record() == {
        "decider": "script",
        "order": ["cap", "backoff", "both"],
        "model_calls": [],
        "budget": {"limit_usd": 0.005, "spent_usd": 0.0},
    }
    with pytest.raises(ValueError, match="decider must be"):
        choose_decider({"decider": "oracle"}, "http://agent", "i1", {"budget_usd": 0.005})


def test_the_host_sends_the_script_order_and_the_tries(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    sent: dict[str, Any] = {}
    token = tmp_path / "loop-token"
    token.write_text("placeholder")
    monkeypatch.setattr(cli, "LOOP_TOKEN", token)  # every request to the loop carries the host's token

    class Answer:
        def __enter__(self) -> "Answer":
            return self

        def __exit__(self, *exc: object) -> None:
            return None

        def read(self) -> bytes:
            return b"{}"

    def urlopen(req: Any, timeout: float) -> Answer:
        sent.update(json.loads(req.data))
        return Answer()

    monkeypatch.setattr(cli.urllib.request, "urlopen", urlopen)
    cli.post_incident("i1", LoopConfig(wait_s=30, hold_s=30, tries=1), "m", ["both", "cap", "backoff"])
    assert sent["tries"] == 1 and sent["decider"] == "script" and sent["order"] == ["both", "cap", "backoff"]
    sent.clear()
    cli.post_incident("i2", LoopConfig(wait_s=30, hold_s=30), "m", arm="model-loop")
    assert "decider" not in sent and sent["tries"] == 3 and sent["arm"] == "model-loop"


def test_the_pilot_script_pays_one_wrong_try_per_applied_miss() -> None:
    wrong = 31.0 * 0.317 / 75
    assert script_minutes("hot_shard", ["cap", "backoff", "both"]) == pytest.approx(0.074)
    assert script_minutes("hot_shard", ["backoff", "both", "cap"]) == pytest.approx(0.074 + wrong)
    assert script_minutes("compound", ["backoff", "cap", "both"]) == pytest.approx(0.080 + 2 * 31.0 * 0.320 / 75)
    # In a retry storm both steps with a cap are refused: no time lost.
    assert script_minutes("retry_storm", ["cap", "both", "backoff"]) == pytest.approx(0.281)


def test_the_largest_saving_and_the_correction_requirement() -> None:
    assert mean_saving(0.0) == pytest.approx(0.058, abs=0.001)
    assert abs(mean_saving(6.5)) < 0.005  # the pilot's decision time costs what the script's wrong tries do
    assert storms_per_kind_for_correction() == 8  # 2 / 2**8 < 0.05 / 6 < 2 / 2**7


def test_the_orders_are_drawn_per_storm_and_stable() -> None:
    arms, steps = draw_orders(13, "s13-00")
    assert sorted(arms) == sorted(ARMS) and sorted(steps) == sorted(STEPS)
    assert draw_orders(13, "s13-00") == (arms, steps)
    seen = {tuple(draw_orders(13, f"s13-{i:02}")[1]) for i in range(24)}
    assert len(seen) > 1 and seen <= set(permutations(STEPS))


# ---- The analysis, on made-up runs ----------------------------------------------------------------------


def run(
    sid: str,
    kind: str,
    arm: str,
    minutes: float,
    *,
    resolved: bool = True,
    kept: list[dict[str, Any]] | None = None,
    spent: float = 0.0,
    reason: str = "resolved on try 1",
    restored: bool = True,
) -> dict[str, Any]:
    """One saved run: `minutes` of customers lost in the window, spread evenly over it."""
    failed = minutes * 200 * 60 / HORIZON_S
    r: dict[str, Any] = {
        "storm": {"id": sid, "kind": kind, "key": "acct-0042"},
        "arm": arm,
        "rate": 200,
        "t_fault": 10.0,
        "env": {"GATEWAY_BACKOFF": "1" if kind == "hot_shard" else "0"},
        "series": [[10.0 + i, 200.0, failed] for i in range(1, int(HORIZON_S) + 6)],
        "invalid": [],
        "recovered": False,
        "wait_s": 30,
        "hold_s": 30,
    }
    if arm != "nobody":
        r |= {
            "loop": {
                "resolved": resolved,
                "reason": reason,
                "kept": kept or [],
                "tries": [{"t_act": 20.0, "verdict": "resolved" if resolved else "not verified"}],
            },
            "budget": {"limit_usd": 0.005, "spent_usd": spent},
            "restored": restored,
            "agree": True,
            "loop_clock_offset_s": 25.0,
        }
    r["unjudged"] = unjudged(r)
    return r


def storm(sid: str, kind: str, loop_minutes: float, script_minutes: float) -> list[dict[str, Any]]:
    """Four arms of one storm."""
    return [
        run(sid, kind, "nobody", 1.0),
        run(sid, kind, "script", script_minutes),
        run(sid, kind, "model-1", loop_minutes, spent=0.0002),
        run(sid, kind, "model-loop", loop_minutes, spent=0.0003),
    ]


def test_window_minutes_and_a_short_window_cannot_be_judged() -> None:
    r = run("s", "hot_shard", "script", 0.5)
    assert window_minutes(r) == pytest.approx(0.5, abs=0.002)
    assert r["unjudged"] == []
    r["series"] = r["series"][:100]
    assert unjudged(r) == ["window not measured to its end"]


def test_needless_counts_only_kept_changes_the_storm_did_not_need() -> None:
    cap = {"name": "rate_cap", "args": {"key": "acct-0042", "rps": 10}}
    other = {"name": "rate_cap", "args": {"key": "acct-0007", "rps": 1}}
    on = {"name": "backoff", "args": {"enabled": True}}
    assert needless(run("s", "compound", "script", 0.1, kept=[cap, on])) == []
    assert needless(run("s", "hot_shard", "script", 0.1, kept=[cap, on])) == []  # already backing off
    assert needless(run("s", "retry_storm", "script", 0.1, kept=[on, other])) == ["cap acct-0007"]
    assert needless(run("s", "retry_storm", "script", 0.1, kept=[cap])) == ["cap acct-0042"]


def test_harm_is_losing_more_than_nobody_or_leaving_an_unrestored_change() -> None:
    nobody = run("s", "compound", "nobody", 1.0)
    assert harm(run("s", "compound", "script", 1.05), nobody) == []  # within 6% of nobody
    assert harm(run("s", "compound", "script", 1.07), nobody) == ["lost more than nobody"]
    left = run("s", "compound", "model-loop", 0.5, resolved=False, reason="out of tries", restored=False)
    assert harm(left, nobody) == ["ended in the safe state with the gateway's settings not restored"]


def test_acted_past_the_window() -> None:
    r = run("s", "compound", "model-loop", 0.5)
    assert not acted_past(r)  # 25 + 20 + 60 = 105 < 10 + 240
    r["loop"]["tries"] = [{"t_act": 200.0, "verdict": "resolved"}]
    assert acted_past(r)  # 25 + 200 + 62 > 250
    r["loop"]["tries"] = [{"t_act": 200.0, "verdict": "denied"}]
    assert not acted_past(r)  # a refusal waits for nothing
    r["loop"]["tries"] = [{"t_act": 200.0, "verdict": "not verified"}]
    assert acted_past(r)  # 25 + 200 + 30 > 250


def test_analyze_pairs_each_storm_with_itself() -> None:
    runs = [
        *storm("a", "retry_storm", 0.30, 0.28),
        *storm("b", "hot_shard", 0.09, 0.20),
        *storm("c", "compound", 1.00, 0.34),
    ]
    runs[11] = run("c", "compound", "model-loop", 1.0, resolved=False, reason="out of tries", spent=0.0004)
    out = analyze({"runs": runs})
    assert out["storms"] == 3 and out["excluded"] == []
    assert out["primary"]["mean"] == pytest.approx((0.02 - 0.11 + 0.66) / 3, abs=0.003)
    assert out["resolved"] == {"nobody": 0, "script": 3, "model-1": 3, "model-loop": 2}
    assert out["resolution_pairs"]["script vs model-loop"]["won"] == 1
    assert out["resolution_pairs"]["nobody vs script"] == {"won": 0, "lost": 3, "p": 0.25, "rejected": False}
    assert out["cost"]["script"]["per_resolved_usd"] == 0
    assert out["cost"]["model-loop"]["per_resolved_usd"] == pytest.approx((0.0003 + 0.0003 + 0.0004) / 2)
    assert out["harm"]["model-loop"] == 0 and out["over_budget"] == 0
    assert out["bar_met"] is False  # the loop lost more than the script


def test_analyze_meets_the_bar_only_below_zero_and_without_harm() -> None:
    runs = [r for i in range(6) for r in storm(f"s{i}", "hot_shard", 0.08, 0.20 + 0.01 * i)]
    assert analyze({"runs": runs})["bar_met"] is True
    runs[3] = run("s0", "hot_shard", "model-loop", 0.08, reason="budget: refused", resolved=False)
    out = analyze({"runs": runs})
    assert out["over_budget"] == 1 and out["bar_met"] is False


def test_a_storm_with_an_arm_not_judged_is_left_out_and_named() -> None:
    runs = [*storm("a", "hot_shard", 0.1, 0.2), *storm("b", "hot_shard", 0.1, 0.2), *storm("c", "hot_shard", 0.1, 0.2)]
    runs[5]["unjudged"] = ["late sends 3.0%"]
    out = analyze({"runs": runs})
    assert out["storms"] == 2 and out["excluded"] == ["b"]


def test_only_the_second_try_of_a_run_counts() -> None:
    first, second = run("a", "hot_shard", "script", 0.9), run("a", "hot_shard", "script", 0.2)
    first["unjudged"], second["second_try"] = ["late sends 3.0%"], True
    runs = [first, second, run("a", "hot_shard", "nobody", 1.0)]
    mark_counted(runs)
    assert [r["counts"] for r in runs] == [False, True, True]


def test_consistency_counts_storms_where_every_repeat_resolved() -> None:
    runs = [*storm("a", "hot_shard", 0.1, 0.2), *storm("b", "hot_shard", 0.1, 0.2), *storm("c", "hot_shard", 0.1, 0.2)]
    for sid, ok in (("a", True), ("a", True), ("b", True), ("b", False)):
        r = run(sid, "hot_shard", "model-loop", 0.1, resolved=ok)
        r["repeat"] = 2
        runs.append(r)
    out = analyze({"runs": runs})
    assert out["consistency"] == {"storms": 2, "all_resolved": 1}
    runs[0]["unjudged"] = ["late sends 3.0%"]  # storm a leaves the main comparison, and so the consistency figure
    assert analyze({"runs": runs})["consistency"] == {"storms": 1, "all_resolved": 0}
