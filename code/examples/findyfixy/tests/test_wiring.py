"""Tests for findyfixy/wiring.py: the composition root, with fakes instead of a database."""

import threading
import time
from typing import ClassVar

import pytest

from findyfixy.triage.record import Prior
import findyfixy.wiring as wiring


class FakeFound:
    """A retrieval hit: only the incident id is read."""

    def __init__(self, incident_id: str) -> None:
        self.incident_id = incident_id


class FakeRetriever:
    """Records what it was asked and returns two fixed hits."""

    def __init__(self) -> None:
        self.asked: list[tuple[str, int]] = []

    def hybrid_rerank(self, question: str, k: int):
        self.asked.append((question, k))
        return [FakeFound("INC-1"), FakeFound("INC-2")]

    def brief(self, incident_id: str) -> Prior:
        return Prior(incident_id, "auth", "sev2", "summary", "cause", "fix")


class FakeConnection:
    """Counts closes, so a failed build can be shown to clean up. `broken` is set by a dying query."""

    closes = 0
    made: ClassVar[list["FakeConnection"]] = []

    def __init__(self) -> None:
        self.closed = False
        self.broken = False
        FakeConnection.made.append(self)

    def close(self) -> None:
        """Record the close."""
        self.closed = True
        FakeConnection.closes += 1


def install(monkeypatch, builds: list[int], fail_first: bool = False, delay: float = 0.0) -> FakeRetriever:
    """Replace the database and the retriever build with fakes. Returns the fake retriever."""
    retriever = FakeRetriever()
    FakeConnection.closes = 0
    FakeConnection.made = []

    def fake_connect():
        return FakeConnection()

    def fake_build(conn, embedder=None, stage=None):
        builds.append(1)
        time.sleep(delay)
        if fail_first and len(builds) == 1:
            raise OSError("database is down")
        return retriever

    monkeypatch.setattr(wiring, "connect", fake_connect)
    monkeypatch.setattr(wiring, "build_retriever", fake_build)
    return retriever


def shared() -> wiring.SharedRetrieval:
    """A shared retrieval whose embedder is a fake: no model is loaded."""
    return wiring.SharedRetrieval(make_embedder=lambda: lambda texts: [[float(len(t))] for t in texts])


def test_recall_builds_nothing_until_it_is_first_used(monkeypatch) -> None:
    builds: list[int] = []
    install(monkeypatch, builds)
    recall = shared().recall(3)
    assert builds == []
    recall("q")
    assert builds == [1]


def test_recall_asks_for_k_and_returns_briefs(monkeypatch) -> None:
    retriever = install(monkeypatch, [])
    result = shared().recall(5)("what broke?")
    assert retriever.asked == [("what broke?", 5)]
    assert [p.incident_id for p in result] == ["INC-1", "INC-2"]


def test_the_retriever_is_built_once_even_when_runs_overlap(monkeypatch) -> None:
    builds: list[int] = []
    install(monkeypatch, builds, delay=0.05)
    recall = shared().recall(3)
    threads = [threading.Thread(target=recall, args=("q",)) for _ in range(8)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert builds == [1]  # eight runs, one build


def test_a_failed_first_build_is_retried_on_the_next_call(monkeypatch) -> None:
    builds: list[int] = []
    install(monkeypatch, builds, fail_first=True)
    recall = shared().recall(3)
    with pytest.raises(OSError):
        recall("q")
    assert FakeConnection.closes == 1  # the failed build closed its connection
    assert len(recall("q")) == 2  # the database came back
    assert builds == [1, 1]


def test_the_default_k_is_the_shared_recall_k() -> None:
    import inspect

    assert inspect.signature(wiring.SharedRetrieval.recall).parameters["k"].default == wiring.RECALL_K == 3


def test_recall_and_the_judge_share_one_embedder_and_one_retriever(monkeypatch) -> None:
    """The point of sharing: triage recall and eval candidates reuse one build."""
    builds: list[int] = []
    install(monkeypatch, builds)
    made: list[int] = []

    def make_embedder():
        made.append(1)
        return lambda texts: [[1.0] for _ in texts]

    retrieval = wiring.SharedRetrieval(make_embedder=make_embedder)
    retrieval.recall(3)("q")
    retrieval.candidates("q", 4)
    assert retrieval.embed(["a", "b"]) == [[1.0], [1.0]]
    assert builds == [1] and made == [1]


def test_close_closes_the_connection_and_a_later_use_rebuilds(monkeypatch) -> None:
    """Closing releases the database; the next call builds again."""
    builds: list[int] = []
    install(monkeypatch, builds)
    retrieval = shared()
    retrieval.candidates("q", 1)
    retrieval.close()
    retrieval.close()  # harmless twice
    assert FakeConnection.closes == 1
    retrieval.candidates("q", 1)
    assert builds == [1, 1]


# ---- A lost database -------------------------------------------------------------


class Dying(FakeRetriever):
    """Raises a connection-level error for the first `fails` queries, then answers."""

    def __init__(self, fails: int) -> None:
        super().__init__()
        self.fails = fails

    def hybrid_rerank(self, question: str, k: int):
        if self.fails:
            self.fails -= 1
            import psycopg

            FakeConnection.made[-1].broken = True  # what psycopg marks when the server goes away
            raise psycopg.OperationalError("server closed the connection unexpectedly")
        return super().hybrid_rerank(question, k)


class Clock:
    """A fake clock that only moves when the code sleeps."""

    def __init__(self) -> None:
        self.now = 0.0
        self.slept: list[float] = []

    def sleep(self, seconds: float) -> None:
        self.slept.append(seconds)
        self.now += seconds


def install_dying(monkeypatch, fails: int) -> tuple[list[int], Dying]:
    """A database whose connection dies `fails` times; returns the build counter and the retriever."""
    builds: list[int] = []
    retriever = Dying(fails)
    FakeConnection.closes = 0
    FakeConnection.made = []
    monkeypatch.setattr(wiring, "connect", FakeConnection)

    def fake_build(conn, embedder=None, stage=None):
        builds.append(1)
        return retriever

    monkeypatch.setattr(wiring, "build_retriever", fake_build)
    return builds, retriever


def shared_with(clock: Clock) -> wiring.SharedRetrieval:
    """A shared retrieval with a fake embedder and the fake clock."""
    return wiring.SharedRetrieval(
        make_embedder=lambda: lambda texts: [[1.0] for _ in texts], sleep=clock.sleep, clock=lambda: clock.now
    )


def test_a_connection_lost_once_is_replaced_and_the_query_retried(monkeypatch, caplog) -> None:
    """A Postgres restart costs one wait and one rebuild, not the rest of the run."""
    builds, _ = install_dying(monkeypatch, fails=1)
    clock = Clock()
    with caplog.at_level("WARNING"):
        found = shared_with(clock).candidates("q", 2)
    assert [p.incident_id for p in found] == ["INC-1", "INC-2"]
    assert len(builds) == 2 and FakeConnection.closes == 1 and clock.slept == [1.0]
    assert "connection lost" in caplog.text and "reconnected after 1.0 s" in caplog.text


def test_a_database_down_past_the_deadline_fails_the_call(monkeypatch) -> None:
    """Waits back off to 8 s and stop at the deadline; then the error reaches the caller."""
    import psycopg

    install_dying(monkeypatch, fails=10_000)
    clock = Clock()
    with pytest.raises(psycopg.OperationalError):
        shared_with(clock).candidates("q", 2)
    assert clock.slept[:5] == [1.0, 2.0, 4.0, 8.0, 8.0]
    assert sum(clock.slept) <= wiring.RECONNECT_DEADLINE_S


def test_an_error_on_a_live_connection_is_not_retried(monkeypatch) -> None:
    """A timeout or out-of-memory error leaves the connection up: it is the query's failure, raised at once."""
    import psycopg

    builds, retriever = install_dying(monkeypatch, fails=0)

    def timed_out(question, k):
        raise psycopg.OperationalError("canceling statement due to statement timeout")

    retriever.hybrid_rerank = timed_out  # type: ignore[method-assign]
    clock = Clock()
    with pytest.raises(psycopg.OperationalError, match="timeout"):
        shared_with(clock).candidates("q", 2)
    assert clock.slept == [] and len(builds) == 1


def test_a_database_never_reached_fails_at_once(monkeypatch) -> None:
    """A missing `make up` is reported in seconds, not after the reconnect deadline."""
    import psycopg

    def refused():
        raise psycopg.OperationalError("connection refused")

    monkeypatch.setattr(wiring, "connect", refused)
    clock = Clock()
    with pytest.raises(psycopg.OperationalError, match="refused"):
        shared_with(clock).candidates("q", 2)
    assert clock.slept == []


def test_the_lock_is_released_while_waiting(monkeypatch) -> None:
    """The judge's embedder must not wait on a reconnect it does not need."""
    install_dying(monkeypatch, fails=1)
    clock = Clock()
    retrieval = shared_with(clock)
    free: list[bool] = []

    def sleep(seconds: float) -> None:
        got = retrieval._lock.acquire(blocking=False)
        free.append(got)
        if got:
            retrieval._lock.release()
        clock.sleep(seconds)

    retrieval._sleep = sleep
    retrieval.candidates("q", 2)
    assert free == [True]


def test_a_failure_that_is_not_the_connection_is_not_retried(monkeypatch) -> None:
    """A bug or a bad build propagates at once: retrying it would only hide it."""
    builds: list[int] = []
    install(monkeypatch, builds, fail_first=True)
    clock = Clock()
    with pytest.raises(OSError):
        shared_with(clock).candidates("q", 2)
    assert clock.slept == [] and len(builds) == 1
