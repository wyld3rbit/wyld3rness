"""Tests for simprod/checker.py: good seconds, recovery that holds, false recoveries, customer minutes."""

from simprod.checker import (
    Scraper,
    Second,
    customer_counts,
    customer_minutes,
    judged,
    late_sends,
    late_share,
    longest_good_stretch,
    max_gap,
    recovered_at,
    resets,
    seconds_from,
)

METRICS = """# HELP customers_requests_total Customer payments
# TYPE customers_requests_total counter
customers_requests_total{outcome="ok"} 990
customers_requests_total{outcome="timeout"} 7
customers_requests_total{outcome="shed"} 3
customers_runaway_requests_total{outcome="throttled"} 500
"""


def series(goods: str, start: float = 1.0) -> list[Second]:
    """'g' a good second (200 ok), 'b' a bad one (100 ok, 100 failed), one per second from `start`."""
    return [Second(start + i, 200, 0) if c == "g" else Second(start + i, 100, 100) for i, c in enumerate(goods)]


def test_counts_customers_only() -> None:
    assert customer_counts(METRICS) == (990.0, 10.0)  # the runaway's requests are not customers


def test_a_good_second_has_completions_and_at_most_one_percent_failed() -> None:
    assert Second(1, 99, 1).good and not Second(1, 98, 2).good and not Second(1, 0, 0).good


def test_seconds_from_cumulative_scrapes() -> None:
    assert seconds_from([(0, 0, 0), (1, 200, 0), (2, 350, 50)]) == [Second(1, 200, 0), Second(2, 150, 50)]


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


def test_a_failed_scrape_leaves_a_gap_not_a_value() -> None:
    texts = iter([METRICS, OSError("down"), METRICS.replace("990", "1190")])
    now = [0.0]

    def fetch(url: str) -> str:
        """The next scripted answer, or raise it."""
        now[0] += 1
        item = next(texts)
        if isinstance(item, Exception):
            raise item
        return item

    sc = Scraper(fetch=fetch, clock=lambda: now[0])
    for _ in range(3):
        sc.scrape()
    assert sc.errors == 1 and len(sc.scrapes) == 2
    assert sc.series()[0].ok == 200


def test_a_counter_reset_is_a_gap_not_negative_data() -> None:
    scrapes = [(0, 0, 0), (1, 200, 0), (2, 5, 0), (3, 205, 1)]  # the customers service restarted after t=1
    assert seconds_from(scrapes) == [Second(1, 200, 0), Second(3, 200, 1)]
    assert resets(scrapes) == 1


def test_a_response_cut_short_is_a_counted_gap() -> None:
    import http.client

    def fetch(url: str) -> str:
        """A response that ends mid-body."""
        raise http.client.IncompleteRead(b"partial")

    sc = Scraper(fetch=fetch)
    sc.scrape()
    assert sc.errors == 1 and sc.scrapes == []


def test_late_sends_are_read_and_shared_out_over_what_completed() -> None:
    assert late_sends("customers_late_sends_total 12\n") == 12.0 and late_sends("") == 0.0
    scrapes = [(1.0, 100, 0, 0), (2.0, 200, 0, 1), (3.0, 300, 50, 21), (4.0, 400, 100, 31)]
    # From t = 2: 200 ok and 100 failed completed, 30 of them sent late.
    assert late_share(scrapes, from_t=2.0) == 0.1
    assert late_share(scrapes[:1], from_t=2.0) == 0.0  # nothing after: nothing late


def test_the_longest_scrape_gap_after_a_time() -> None:
    scrapes = [(0.0, 0, 0), (1.0, 0, 0), (2.0, 0, 0), (5.5, 0, 0), (6.5, 0, 0)]
    assert max_gap(scrapes, from_t=0.0) == 3.5
    assert max_gap(scrapes, from_t=5.5) == 1.0
    assert max_gap([], from_t=0.0) == 0.0


def test_the_scraper_keeps_late_sends_beside_each_scrape() -> None:
    sc = Scraper(fetch=lambda u: METRICS + "customers_late_sends_total 7\n", clock=lambda: 0.0)
    sc.scrape()
    assert sc.late[0][3] == 7.0 and sc.late[0][:3] == sc.scrapes[0]


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
