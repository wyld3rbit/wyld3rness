"""The composition root: where the real database, models and retriever are built.

Why: the retriever was assembled by hand in three places (the console, the
answer evaluation and the retrieval evaluation), each with its own copy of
"connect, register the vector type, load every passage, build the embedder and
the reranker". A change to how recall is built had to be made three times.
Everything that needs the real thing now builds it here.

The rest of the code takes its dependencies as arguments (a `Recall`, an `Ask`,
a `Judge`) and never imports this module. Only entry points do. That keeps the
domain code testable with fakes.

Heavy imports (psycopg, the embedding model, the reranker) happen inside the
functions, so importing this module costs nothing.
"""

from collections.abc import Callable, Sequence
import contextlib
import logging
import threading
import time
from typing import TYPE_CHECKING, TypeVar

from findyfixy.triage.record import RECALL_K, Prior

if TYPE_CHECKING:
    from contextlib import AbstractContextManager

    from psycopg import Connection

    from findyfixy.retrieval.ingest import Embedder
    from findyfixy.retrieval.retrieve import Retriever, Stage
    from findyfixy.triage.graph import Recall
    from wyld3rness.trace.tracing import Tracing


_log = logging.getLogger(__name__)
_T = TypeVar("_T")

# How long a query waits for a lost database to come back. A restart of the
# local Postgres takes seconds; the longest seen here took 39.5 s (research
# log, entry 004). Past the deadline the call fails and the caller records
# that case; the next call tries once more.
RECONNECT_DEADLINE_S = 60.0
# Connection settings that turn a hang into an error the reconnect can act on.
# A connect that gets no answer gives up after CONNECT_TIMEOUT_S. TCP
# keepalives probe a silent connection after 10 s, every 5 s, 3 times, so a
# dead network path is detected in about 25 s instead of the system's hours.
CONNECT_TIMEOUT_S = 5
KEEPALIVE_IDLE_S, KEEPALIVE_INTERVAL_S, KEEPALIVE_COUNT = 10, 5, 3
RECONNECT_FIRST_WAIT_S = 1.0
RECONNECT_MAX_WAIT_S = 8.0


def connect() -> "Connection":
    """Open the project database with the vector type registered.

    Autocommit, because the app only reads here. The caller closes it (use it as
    a context manager).
    """
    from pgvector.psycopg import register_vector
    import psycopg

    from findyfixy.retrieval.ingest import database_url

    conn = psycopg.connect(
        database_url(),
        autocommit=True,
        connect_timeout=CONNECT_TIMEOUT_S,
        keepalives=1,
        keepalives_idle=KEEPALIVE_IDLE_S,
        keepalives_interval=KEEPALIVE_INTERVAL_S,
        keepalives_count=KEEPALIVE_COUNT,
    )
    register_vector(conn)
    return conn


def build_retriever(
    conn: "Connection", embedder: "Embedder | None" = None, stage: "Stage | None" = None
) -> "Retriever":
    """Hybrid retriever over every passage in the database.

    Loads the embedding model and the reranker (about 1.3 GB of weights). Pass an
    `embedder` you already have to share one loaded model. `stage` wraps each
    retrieval stage (`stage_spans` makes them tracing spans).
    """
    from findyfixy.retrieval.ingest import make_embedder
    from findyfixy.retrieval.retrieve import Retriever, load_passages, make_reranker, no_stages, postgres_vector_search

    embedder = embedder or make_embedder()
    return Retriever(
        load_passages(conn), postgres_vector_search(conn, embedder), make_reranker(), stage=stage or no_stages
    )


def stage_spans(tracing: "Tracing | None") -> "Stage | None":
    """A retriever `Stage` that makes each stage a span under the current one, or None without tracing.

    The recall node's span is current while recall runs (findyfixy/observability/adapter.py),
    so the stages appear inside it. Outside any span they would each start a
    trace of their own, which is why this is only built when tracing is on.
    """
    if tracing is None:
        return None
    from wyld3rness.trace.spans import LF_TYPE

    tracer = tracing.tracer()

    def stage(name: str) -> "AbstractContextManager[object]":
        """One span named for the stage, current while it runs."""
        return tracer.start_as_current_span(f"recall {name}", attributes={LF_TYPE: "span"})

    return stage


class SharedRetrieval:
    """One database connection, embedder and retriever per process, built on first use.

    Why shared: the console runs triage (recall) and, from Day 5, eval runs
    whose judge also needs the embedder. Built separately, they held two copies
    of the embedding model and the reranker (about 1.3 GB) and two database
    connections, on a 16 GB machine that also holds two answering models and a
    judge. One instance serves both.

    Why lazy: building it loads two models and opens the database. Doing that
    at startup would slow every start and fail it when the database is off,
    though fake mode needs neither. On first use a failure propagates (the
    console turns it into a failed run) and the next call tries again. What was
    built is kept.

    Thread safety: one lock covers every use. Neither a psycopg connection nor
    a loaded model is safe to call from two threads at once, and the lock also
    keeps two first calls from building twice. It is held only for retrieval
    and embedding (well under a second), never across a model call, and it is
    released while waiting for a lost database (below), so the embedder stays
    usable.

    A lost database. A restart, a failover or a dropped network path kills the
    one connection, and every later query would fail until the process
    restarted. So when a query fails and its connection is closed or broken,
    the connection and the retriever are dropped, and the query is rebuilt and
    retried with backoff (1, 2, 4, 8, 8 ... s) until `RECONNECT_DEADLINE_S`;
    past it the error propagates. Nothing else is retried: an error on a live
    connection (a timeout, out of memory) is the query's own failure, and a
    database that was never reached on first use fails at once, so a missing
    `make up` is reported in seconds. The rebuild reloads the reranker too: a
    few seconds, paid once per lost connection. The loss and the recovery are
    logged as warnings, so a run's output shows them; a loss past the deadline
    reaches the caller as a failed case or run.
    Why not a pool: queries are serialized by the lock, so a pool would hold
    connections nothing uses at once; a pool also needs the same liveness
    check. A server that answers many requests at once needs one.
    """

    def __init__(
        self,
        *,
        make_embedder: "Callable[[], Embedder] | None" = None,
        stage: "Stage | None" = None,
        sleep: Callable[[float], None] = time.sleep,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        """Hold nothing yet. `make_embedder`, `sleep` and `clock` are for tests; `stage` traces the retrieval stages."""
        self._make_embedder = make_embedder
        self._stage = stage
        self._sleep, self._clock = sleep, clock
        self._lock = threading.Lock()
        self._embedder: Embedder | None = None
        self._conn: Connection | None = None
        self._retriever: Retriever | None = None

    def _load_embedder(self) -> "Embedder":
        """The embedder, loaded once. The caller holds the lock."""
        if self._embedder is None:
            if self._make_embedder is None:
                from findyfixy.retrieval.ingest import make_embedder

                self._make_embedder = make_embedder
            self._embedder = self._make_embedder()
        return self._embedder

    def _load_retriever(self) -> "Retriever":
        """The retriever over the shared embedder, built once. The caller holds the lock.

        A failed build closes the connection it opened, so a retry starts clean.
        """
        if self._retriever is None:
            embedder = self._load_embedder()
            conn = connect()
            try:
                self._retriever = build_retriever(conn, embedder, self._stage)
            except BaseException:
                conn.close()
                raise
            self._conn = conn
        return self._retriever

    def _drop_connection(self) -> None:
        """Forget the connection and the retriever built on it. The caller holds the lock."""
        if self._conn is not None:
            with contextlib.suppress(Exception):  # it is already broken; closing is best effort
                self._conn.close()
        self._conn = self._retriever = None

    def _connection_gone(self) -> bool:
        """True when a connection was open and is now closed or broken. The caller holds the lock."""
        return self._conn is not None and bool(self._conn.closed or self._conn.broken)

    def _wait(self, seconds: float) -> None:
        """Sleep with the lock released, so the embedder and other callers are not held up."""
        self._lock.release()
        try:
            self._sleep(seconds)
        finally:
            self._lock.acquire()

    def _query(self, use: "Callable[[Retriever], _T]") -> _T:
        """`use(retriever)`, rebuilt and retried while a lost database comes back (see the class docstring)."""
        import psycopg

        lost = (psycopg.OperationalError, psycopg.InterfaceError)
        try:
            return use(self._load_retriever())
        except lost as err:
            if not self._connection_gone():
                raise  # never connected, or the connection is fine: not a lost database
            _log.warning("database connection lost (%s); reconnecting", type(err).__name__)
        self._drop_connection()
        started, wait = self._clock(), RECONNECT_FIRST_WAIT_S
        while True:
            if self._clock() + wait - started > RECONNECT_DEADLINE_S:
                _log.warning("database still unreachable after %.1f s; giving up on this call", self._clock() - started)
                return use(self._load_retriever())  # one last try; its error is the one the caller sees
            self._wait(wait)
            wait = min(wait * 2, RECONNECT_MAX_WAIT_S)
            try:
                result = use(self._load_retriever())
            except lost:
                self._drop_connection()
                continue
            _log.warning("database reconnected after %.1f s", self._clock() - started)
            return result

    def candidates(self, question: str, n: int) -> list[Prior]:
        """Top `n` incidents by hybrid search and rerank, as prompt-ready briefs."""
        with self._lock:
            return self._query(lambda r: [r.brief(f.incident_id) for f in r.hybrid_rerank(question, n)])

    def recall(self, k: int = RECALL_K) -> "Recall":
        """The graph's recall step: the top `k` candidates for a question."""

        def recall(question: str) -> list[Prior]:
            """Similar past incidents for one question."""
            return self.candidates(question, k)

        return recall

    def embed(self, texts: "Sequence[str]") -> list[list[float]]:
        """Embed texts with the shared model (the judge's relevancy metric needs it)."""
        with self._lock:
            return self._load_embedder()(texts)

    def close(self) -> None:
        """Close the database connection and drop what was built. Safe to call twice."""
        with self._lock:
            if self._conn is not None:
                self._conn.close()
            self._conn = self._retriever = None
