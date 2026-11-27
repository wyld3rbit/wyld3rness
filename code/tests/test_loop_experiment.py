"""Tests for wyld3rness/loop/experiment.py and script.py: paired statistics, correction, bounds, power, script."""

import math
import random

import pytest

from wyld3rness.loop.env import Action
from wyld3rness.loop.experiment import exact_paired_p, paired_interval, power, step_down, zero_events_upper
from wyld3rness.loop.run import LoopConfig, run_incident
from wyld3rness.loop.script import Scripted
from wyld3rness.testing import FakeSigner, FakeWorld, allow


def test_paired_interval_is_the_mean_difference_and_seeded() -> None:
    a, b = [1.0, 2.0, 3.0, 4.0], [0.5, 1.5, 2.0, 4.0]
    mean, ci = paired_interval(a, b)
    assert mean == pytest.approx(0.5)
    assert ci is not None and ci[0] <= 0.5 <= ci[1]
    assert paired_interval(a, b) == (mean, ci)


def test_paired_interval_refuses_unpaired_values() -> None:
    with pytest.raises(ValueError, match="equal"):
        paired_interval([1.0], [1.0, 2.0])


def test_exact_paired_p_is_the_two_sided_sign_test() -> None:
    assert exact_paired_p(0, 0) == 1.0
    assert exact_paired_p(8, 0) == pytest.approx(2 / 2**8)
    assert exact_paired_p(5, 3) == pytest.approx(2 * sum(math.comb(8, k) for k in range(4)) / 2**8)
    assert exact_paired_p(3, 5) == exact_paired_p(5, 3)
    assert exact_paired_p(4, 4) == 1.0


def test_the_correction_steps_down_and_stops_at_the_first_kept() -> None:
    # m = 3: 0.01 <= 0.05/3, 0.02 <= 0.05/2, 0.04 <= 0.05/1: all rejected.
    assert step_down({"a": 0.01, "b": 0.02, "c": 0.04}) == {"a": True, "b": True, "c": True}
    # 0.03 > 0.05/2 stops it, so 0.04 is kept although it is below 0.05.
    assert step_down({"a": 0.01, "b": 0.03, "c": 0.04}) == {"a": True, "b": False, "c": False}
    assert step_down({"a": 0.02, "b": 0.01}) == {"a": True, "b": True}
    assert step_down({"a": 0.03, "b": 0.026}) == {"a": False, "b": False}  # 0.026 > 0.05/2 stops it at once


def test_zero_events_upper_is_exact_and_near_the_rule_of_three() -> None:
    assert zero_events_upper(1) == pytest.approx(0.95)
    assert zero_events_upper(24) == pytest.approx(1 - 0.05 ** (1 / 24))
    assert zero_events_upper(300) == pytest.approx(3 / 300, rel=0.01)
    with pytest.raises(ValueError):
        zero_events_upper(0)


def test_power_counts_intervals_entirely_below_zero() -> None:
    def clear(i: int, rng: random.Random) -> float:
        return -1.0 + rng.gauss(0, 0.1)

    def none(i: int, rng: random.Random) -> float:
        return rng.gauss(0, 0.1)

    assert power(clear, 10, sims=50) == 1.0
    assert power(none, 10, sims=200) < 0.1
    assert power(none, 10, sims=50) == power(none, 10, sims=50)


def test_power_passes_the_storm_index() -> None:
    seen: list[int] = []

    def draw(i: int, rng: random.Random) -> float:
        seen.append(i)
        return -1.0

    power(draw, 3, sims=2, resamples=10)
    assert seen == [0, 1, 2, 0, 1, 2]


def test_the_script_proposes_each_step_in_turn_from_the_observation() -> None:
    script = Scripted([lambda o: [Action("a", {"seen": o})], lambda o: [Action("b")]])
    assert script.decide("obs", []) == [Action("a", {"seen": "obs"})]
    world = FakeWorld(fix=("b",))
    out = run_incident(world, script, LoopConfig(wait_s=10, hold_s=10), allow("a", "b"), FakeSigner())
    assert out.resolved and [t.actions[0].name for t in out.tries] == ["a", "b"]


def test_the_script_past_its_last_step_is_a_decider_failure() -> None:
    script = Scripted([lambda o: [Action("a")]])
    world = FakeWorld(fix=("b",))
    out = run_incident(world, script, LoopConfig(wait_s=10, hold_s=10), allow("a"), FakeSigner())
    assert not out.resolved and "the script has 1 steps and try 2 was asked" in out.reason
    with pytest.raises(ValueError):
        Scripted([])
