"""Tests for simprod/checker.py: reading what customers saw, the scraper, and whether a run was measured whole.

The verdict rules it feeds are the harness's (code/tests/test_verdict.py)."""

from simprod.checker import Scraper, customer_counts, late_sends, late_share, max_gap

METRICS = """# HELP customers_requests_total Customer payments
# TYPE customers_requests_total counter
customers_requests_total{outcome="ok"} 990
customers_requests_total{outcome="timeout"} 7
customers_requests_total{outcome="shed"} 3
customers_runaway_requests_total{outcome="throttled"} 500
"""


def test_counts_customers_only() -> None:
    assert customer_counts(METRICS) == (990.0, 10.0)  # the runaway's requests are not customers


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
