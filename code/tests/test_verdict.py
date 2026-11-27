"""Tests for wyld3rness/loop/verdict.py: good seconds, verification, recovery that holds, false recoveries.

Moved with the code from simprod's checker tests, unchanged in substance.
"""

from wyld3rness.loop.verdict import (
    Second,
    customer_minutes,
    judged,
    longest_good_stretch,
    recovered_at,
    resets,
    seconds_from,
    verified,
)


def series(goods: str, start: float = 1.0) -> list[Second]:
    """'g' a good second (200 ok), 'b' a bad one (100 ok, 100 failed), one per second from `start`."""
    return [Second(start + i, 200, 0) if c == "g" else Second(start + i, 100, 100) for i, c in enumerate(goods)]


def test_a_good_second_has_completions_and_at_most_one_percent_failed() -> None:
    assert Second(1, 99, 1).good and not Second(1, 98, 2).good and not Second(1, 0, 0).good


def test_seconds_from_cumulative_scrapes() -> None:
    assert seconds_from([(0, 0, 0), (1, 200, 0), (2, 350, 50)]) == [Second(1, 200, 0), Second(2, 150, 50)]


def test_a_counter_reset_is_a_gap_not_negative_data() -> None:
    scrapes = [(0, 0, 0), (1, 200, 0), (2, 5, 0), (3, 205, 1)]  # the measured service restarted after t=1
    assert seconds_from(scrapes) == [Second(1, 200, 0), Second(3, 200, 1)]
    assert resets(scrapes) == 1


def test_verified_needs_the_last_seconds_all_good() -> None:
    good = [Second(float(t), 200, 0) for t in range(1, 11)]
    assert verified(good, at=10)
    assert not verified([*good[:-1], Second(10.0, 100, 100)], at=10)
    assert not verified(good[:2], at=10)  # nothing measured is not verified


def test_recovery_must_hold_for_the_whole_window() -> None:
    s = series("bbbbb" + "gggggggggg")  # good from t=6 to t=15
    assert recovered_at(s, action_t=3, wait_s=5, hold_s=5) == 6
    assert recovered_at(s, action_t=3, wait_s=5, hold_s=20) is None  # the series ends before the hold does


def test_a_false_recovery_is_not_counted() -> None:
    s = series("bb" + "ggg" + "bbbb" + "gg")  # 3 good seconds, then bad again
    assert recovered_at(s, action_t=1, wait_s=10, hold_s=5) is None
    assert longest_good_stretch(s, after_t=1) == 3.0


def test_recovery_after_the_wait_does_not_count() -> None:
    s = series("bbbbbbbbbb" + "gggggggggggggggggggg")  # good only from t=11
    assert recovered_at(s, action_t=0, wait_s=5, hold_s=5) is None
    assert recovered_at(s, action_t=0, wait_s=15, hold_s=5) == 11


def test_customer_minutes_count_failures_after_the_fault() -> None:
    s = series("gbbg")  # 200 failed after t=1
    assert customer_minutes(s, from_t=1, rate=200) == round(200 / (200 * 60), 3)


def test_customer_minutes_stop_at_the_window_end() -> None:
    s = series("gbbb")  # failures at t=2, 3, 4
    assert customer_minutes(s, from_t=1, rate=200, to_t=3) == round(200 / (200 * 60), 3)
    assert customer_minutes(s, from_t=1, rate=200, to_t=4) == round(300 / (200 * 60), 3)


def test_seconds_are_judged_over_the_last_five() -> None:
    """Entry 010: 2 failed of 188 broke a real recovery. Over 5 s it is 2 of about 1,000."""
    s = [Second(float(t), 200, 0) for t in range(1, 11)]
    s[6] = Second(7.0, 186, 2)
    assert not s[6].good and all(x.good for x in judged(s))
    assert judged(s)[6] == Second(7.0, 986, 2)
    assert judged(s)[1] == Second(2.0, 400, 0)  # the first seconds have a shorter window


def test_a_scrape_gap_shortens_the_window_instead_of_reaching_back() -> None:
    s = [Second(1.0, 100, 100), Second(7.0, 200, 0)]
    assert judged(s)[1] == Second(7.0, 200, 0)
