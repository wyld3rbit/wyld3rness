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
from dataclasses import dataclass
from functools import cache, partial
import threading
from typing import TYPE_CHECKING

from findyfixy.triage.record import RECALL_K, Prior

if TYPE_CHECKING:
    from contextlib import AbstractContextManager

    from langchain_core.tools import BaseTool
    from psycopg import Connection

    from findyfixy.paths import DataVersion
    from findyfixy.retrieval.ingest import Embedder
    from findyfixy.retrieval.retrieve import Rerank, Retriever, Stage
    from findyfixy.tools.changes import Change
    from findyfixy.tools.investigate import Investigator
    from findyfixy.triage.graph import Recall
    from findyfixy.triage.record import AlertFacts, Decision
    from wyld3rness.action.ledger import Verdict
    from wyld3rness.identity.broker import CredentialBroker
    from wyld3rness.trace.tracing import Tracing


def connect() -> "Connection":
    """Open the project database with the vector type registered.

    Autocommit, because the app only reads here. The caller closes it (use it as
    a context manager).
    """
    from pgvector.psycopg import register_vector
    import psycopg

    from findyfixy.retrieval.ingest import database_url

    conn = psycopg.connect(database_url(), autocommit=True)
    register_vector(conn)
    return conn


# Data versions whose cases carry what their alert states (time and service)
# and a change log (tools/changes.py). Earlier versions have a deploy log
# against one shared moment and no alert facts.
CHANGE_LOG_VERSIONS: frozenset["DataVersion"] = frozenset({"v3"})


def load_facts(version: "DataVersion", alert: "AlertFacts | None") -> "list[Change] | None":
    """The change log a version's agent and ledger share, loaded once; None for a version without one.

    A change-log version needs the alert's facts, because its log is absolute:
    without the alert's time nothing can be called recent. Raises ValueError
    when they are missing, or given to a version that cannot use them.
    """
    from findyfixy.tools.changes import changes_path, load_changes

    if version in CHANGE_LOG_VERSIONS:
        if alert is None:
            raise ValueError(f"data {version} looks back from each case's alert: pass its AlertFacts")
        return load_changes(changes_path(version))
    if alert is not None:
        raise ValueError(f"data {version} has a deploy log with no clock: the alert facts would be ignored")
    return None


def changes_tool(version: "DataVersion", changes: "list[Change] | None", alert: "AlertFacts | None") -> "BaseTool":
    """The tool that lists what changed: the version's deploy log, or the loaded change log as of the alert."""
    from findyfixy.tools.changes import make_changes_tool
    from findyfixy.tools.deploys import deploys_path, make_deploys_tool

    if changes is not None and alert is not None:
        return make_changes_tool(changes, alert.at)
    return make_deploys_tool(deploys_path(version))


def agent_instructions(version: "DataVersion") -> Callable[[str], str]:
    """The agent's instructions, naming the change tool `build_agent` gives it for `version`."""
    from findyfixy.tools.changes import CHANGES_TOOL
    from findyfixy.tools.deploys import DEPLOYS_TOOL
    from findyfixy.tools.investigate import agent_prompt

    return partial(agent_prompt, changes_tool=CHANGES_TOOL if version in CHANGE_LOG_VERSIONS else DEPLOYS_TOOL)


Judge = Callable[["Decision"], "Verdict"]


@dataclass(frozen=True)
class Agent:
    """The agent's loop, the instructions that name its tools, and how to judge its decision. Built together.

    The graph's investigate step sends `instructions(question)` to the loop.
    Each graph build calls `new_judge()` for its policy step: a fresh action
    ledger per incident, so one run's held rollback never queues another's.
    Kept in one object because they must agree: instructions naming a tool the
    loop does not have send the model calling a tool that fails, and a judge
    reading other facts than the agent's tools would decide on evidence the
    agent never saw.
    """

    investigator: "Investigator"
    instructions: Callable[[str], str]
    new_judge: Callable[[], Judge]


def judge_factory(changes: "list[Change] | None", alert: "AlertFacts | None") -> Callable[[], Judge]:
    """Build a fresh judge (one action ledger) per incident, over the same loaded facts the agent's tool reads.

    With no change log (v1, v2) every rollback fails closed.
    """
    from findyfixy.policy import FindyFixyActions, make_judge

    incident = f"{alert.service} alert at {alert.at.isoformat()}" if alert is not None else "incident"
    return lambda: make_judge(incident, FindyFixyActions(changes, alert))


def build_judge(version: "DataVersion", alert: "AlertFacts | None" = None) -> Judge:
    """One judge for one incident of `version`: what a caller without an agent (a test, a replay) uses."""
    return judge_factory(load_facts(version, alert), alert)()


@cache
def process_broker() -> "CredentialBroker":
    """The credential broker for this process, built on first use with a random in-memory key.

    One per process: every agent in it shares the key, and nothing is stored.
    The grants are the app's (tools/guard.py).
    """
    from findyfixy.tools.guard import GRANTS
    from wyld3rness.identity.broker import CredentialBroker

    return CredentialBroker(GRANTS)


def build_agent(
    model: str, recall: "Recall", version: "DataVersion" = "v1", alert: "AlertFacts | None" = None
) -> Agent:
    """The agent's investigation loop (Day 7): `model` with its three read-only tools and its budget.

    The tools are incident search over `recall`, what changed in a service
    (`changes_tool`), and the service map. `recall` decides what search may
    see: the eval passes one with the case's own incident held out, the console
    the plain one. What changed follows the data version: v1 and v2 read their
    deploy log, v3 its change log as of the case's alert. The log is loaded
    once and shared with the judge. One builder for the eval and the console,
    so the two cannot run different agents.

    Every tool here only reads, and each call carries a token the process's
    credential broker issued for this agent, tool and resource, verified
    before the tool reads (tools/guard.py). Nothing the agent proposes is
    executed; the action ledger judges each proposal, and execution waits for
    approval.
    """
    from langchain_ollama import ChatOllama

    from findyfixy.llm import answer_options
    from findyfixy.tools.guard import AGENT, guard
    from findyfixy.tools.investigate import AGENT_DEADLINE_S, AGENT_MAX_TURNS, build_investigator
    from findyfixy.tools.memory import make_search_tool
    from findyfixy.tools.services import make_services_tool
    from findyfixy.tools.submit import SUBMIT_TOOL, make_submit_tool

    changes = load_facts(version, alert)
    broker = process_broker()
    tools = [
        guard(tool, broker, AGENT)
        for tool in (
            make_search_tool(recall),
            changes_tool(version, changes, alert),
            make_services_tool(),
            make_submit_tool(),
        )
    ]
    llm = ChatOllama(model=model, **answer_options()).bind_tools(tools)
    investigator = build_investigator(
        llm, tools, max_turns=AGENT_MAX_TURNS, deadline_s=AGENT_DEADLINE_S, finish_tool=SUBMIT_TOOL
    )
    return Agent(investigator, agent_instructions(version), judge_factory(changes, alert))


def build_retriever(
    conn: "Connection",
    embedder: "Embedder | None" = None,
    stage: "Stage | None" = None,
    version: "DataVersion" = "v1",
    reranker: "Rerank | None" = None,
) -> "Retriever":
    """Hybrid retriever over every passage of one data version's table.

    Loads the embedding model and the reranker (about 1.3 GB of weights). Pass an
    `embedder` you already have to share one loaded model. `stage` wraps each
    retrieval stage (`stage_spans` makes them tracing spans). `version` picks
    the table (v1 by default, the frozen corpus every earlier run used).
    `reranker` shares one loaded cross-encoder between versions.
    """
    from findyfixy.retrieval.ingest import make_embedder
    from findyfixy.retrieval.retrieve import Retriever, load_passages, make_reranker, no_stages, postgres_vector_search

    embedder = embedder or make_embedder()
    return Retriever(
        load_passages(conn, version),
        postgres_vector_search(conn, embedder, version),
        reranker if reranker is not None else make_reranker(),
        stage=stage or no_stages,
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

    Versions: one instance serves one data version (`version`), and
    `with_version` hands out a sibling for another version over the same
    state. The connection, the embedder and the reranker are loaded once and
    shared; only the retriever (which holds a version's passages) is built per
    version. So the console can run a v2 eval without a second 1.3 GB copy.

    Thread safety: one lock covers every use. Neither a psycopg connection nor
    a loaded model is safe to call from two threads at once, and the lock also
    keeps two first calls from building twice. It is held only for retrieval
    and embedding (well under a second), never across a model call.
    """

    def __init__(
        self,
        *,
        make_embedder: "Callable[[], Embedder] | None" = None,
        make_reranker: "Callable[[], Rerank] | None" = None,
        stage: "Stage | None" = None,
        version: "DataVersion" = "v1",
    ) -> None:
        """Hold nothing yet. `make_embedder` and `make_reranker` are for tests; `stage` traces
        the retrieval stages; `version` picks the data version recall reads (v1 by default)."""
        self._state = _Loaded(make_embedder=make_embedder, make_reranker=make_reranker, stage=stage)
        self._version = version

    def with_version(self, version: "DataVersion") -> "SharedRetrieval":
        """A sibling that reads `version`, sharing this one's connection, models and lock."""
        sibling = SharedRetrieval.__new__(SharedRetrieval)
        sibling._state, sibling._version = self._state, version
        return sibling

    @property
    def version(self) -> "DataVersion":
        """The data version this instance's recall reads."""
        return self._version

    def _load_embedder(self) -> "Embedder":
        """The embedder, loaded once for every version. The caller holds the lock."""
        state = self._state
        if state.embedder is None:
            if state.make_embedder is None:
                from findyfixy.retrieval.ingest import make_embedder

                state.make_embedder = make_embedder
            state.embedder = state.make_embedder()
        return state.embedder

    def _load_reranker(self) -> "Rerank":
        """The cross-encoder, loaded once for every version. The caller holds the lock."""
        state = self._state
        if state.reranker is None:
            if state.make_reranker is None:
                from findyfixy.retrieval.retrieve import make_reranker

                state.make_reranker = make_reranker
            state.reranker = state.make_reranker()
        return state.reranker

    def _load_retriever(self) -> "Retriever":
        """This version's retriever over the shared models, built once. The caller holds the lock.

        A failed first build closes the connection it opened, so a retry starts clean.
        """
        state = self._state
        retriever = state.retrievers.get(self._version)
        if retriever is not None:
            return retriever
        embedder = self._load_embedder()
        reranker = self._load_reranker()
        opened = state.conn is None
        conn = state.conn or connect()
        try:
            retriever = build_retriever(conn, embedder, state.stage, self._version, reranker)
        except BaseException:
            if opened:
                conn.close()
            raise
        state.conn = conn
        state.retrievers[self._version] = retriever
        return retriever

    def candidates(self, question: str, n: int) -> list[Prior]:
        """Top `n` incidents of this version by hybrid search and rerank, as prompt-ready briefs."""
        with self._state.lock:
            retriever = self._load_retriever()
            return [retriever.brief(f.incident_id) for f in retriever.hybrid_rerank(question, n)]

    def recall(self, k: int = RECALL_K) -> "Recall":
        """The graph's recall step: the top `k` candidates for a question."""

        def recall(question: str) -> list[Prior]:
            """Similar past incidents for one question."""
            return self.candidates(question, k)

        return recall

    def embed(self, texts: "Sequence[str]") -> list[list[float]]:
        """Embed texts with the shared model (the judge's relevancy metric needs it)."""
        with self._state.lock:
            return self._load_embedder()(texts)

    def close(self) -> None:
        """Close the shared connection and drop every version's retriever. Safe to call twice.

        Closes it for the siblings too: they share the state.
        """
        state = self._state
        with state.lock:
            if state.conn is not None:
                state.conn.close()
            state.conn = None
            state.retrievers.clear()


class _Loaded:
    """What a `SharedRetrieval` and its siblings share: the lock, the connection, the models, the retrievers."""

    def __init__(
        self,
        *,
        make_embedder: "Callable[[], Embedder] | None",
        make_reranker: "Callable[[], Rerank] | None",
        stage: "Stage | None",
    ) -> None:
        """Nothing loaded yet."""
        self.make_embedder = make_embedder
        self.make_reranker = make_reranker
        self.stage = stage
        self.lock = threading.Lock()
        self.embedder: Embedder | None = None
        self.conn: Connection | None = None
        self.reranker: Rerank | None = None
        self.retrievers: dict[DataVersion, Retriever] = {}
