"""Day 1 router, Day 2 contract: small-model-first, with validated triage output.

What it does
    1. The small model returns a `Triage` record (findyfixy/triage/record.py). An
       invalid reply is re-asked once with the error text.
    2. If the record is valid and confidence is at or above the threshold,
       it is final.
    3. If the record is still invalid, or confidence is low, the large model
       answers instead. That answer is final.

Why this shape
    Most triage questions are routine. A small model answers them fast and
    cheap. The large model is the expensive tail. The routing rule is the
    cost lever: raise the threshold and more traffic goes to the large model
    (more cost, usually better answers). Lower it and the reverse.

Why a graph, and not an if statement
    Two nodes would fit in an if statement. The graph pays off as the system
    grows. Each step is a named node with its own inputs and outputs. That
    makes it easy to trace, test, retry, and pause for a human later (Day 9).
    Day 4 adds a recall node before `small`. Day 7 adds a policy node.
    With a graph, those are new nodes. With an if statement, they are a rewrite.

Agent mode (Day 7, `agent=True`)
    The agent investigates before it decides:

        question -> investigate (tool loop: search_incidents, get_recent_deploys,
                    get_dependencies; turn cap and deadline) -> small -> route -> large?

    Recall is no longer a fixed first step: it is one of the agent's tools,
    so the model chooses the query and whether to search at all. `small` and
    `large` ask for a `Decision` (a `Triage` plus an action: read, page_human
    or rollback with a version) through `ask`, which must constrain replies
    to `Decision` (findyfixy/llm.py, `ollama_decision_ask`). If the loop stopped
    on its budget, the decision's action is replaced by page_human in code
    and a `handover` event says why: an agent out of time or turns hands the
    incident to a person instead of acting on half an investigation, and the
    run ends there (no escalation). The agent's last plain-text finding goes
    into the decision prompt with the tool results. Otherwise the route on
    confidence is unchanged; step 2 measures it, it does not tune it.

Why the graph is built by a function that takes `ask`
    `build_graph` does not know how a model is called. The caller passes in
    `ask`, a function (model name, prompt) -> raw reply text. Production passes
    `ollama_ask`. Tests pass a fake that returns a chosen confidence.
    This is dependency injection. It buys three things:
      - Routing can be tested in milliseconds with no model and no network.
      - Day 8 can swap in a gateway client without touching the graph.
      - Importing this module does nothing. No client is built, no graph is
        compiled, no environment is read until `main()` or a test asks.
    The alternative, patching a module-level function in tests, couples tests
    to private names and breaks silently when the code is renamed.

Known weaknesses (read these before trusting the numbers)
    - Confidence is still self-reported. Validation now rejects values outside
      0 to 1, so the Day 1 "75" case retries and then escalates. It does not
      make the number meaningful: a small model can write 0.9 and be wrong.
    - The large model gets the same retry. If it is still invalid, the run
      raises `TriageError`. There is no third tier and no human yet.
    - No timeout. If Ollama is down, the run raises. Retries cover bad
      output, not infrastructure faults.

Run it:   uv run findyfixy-triage
Test it:  uv run pytest
"""

from collections.abc import Callable, Mapping, Sequence
from itertools import pairwise
import time
from typing import TYPE_CHECKING, Any, Literal, Protocol, TypedDict, cast

from langchain_core.messages import AIMessage, BaseMessage, HumanMessage, ToolMessage
from langgraph.graph import END, StateGraph
from langgraph.graph.state import CompiledStateGraph

from findyfixy.events import Emit, no_emit

if TYPE_CHECKING:
    from contextlib import AbstractContextManager

    from findyfixy.observability.adapter import EventSpans
from findyfixy.llm import ollama_ask
from findyfixy.settings import MissingEnvironment, load_env, read_models
from findyfixy.tools.investigate import (
    MAX_RESULT_CHARS,
    Investigator,
    Stopped,
    ToolCallRecord,
    agent_prompt,
    analyse,
    content_text,
    format_evidence,
    investigation_prompt,
    submission,
)
from findyfixy.tools.submit import SUBMIT_TOOL
from findyfixy.triage.record import (
    DECISION_PROMPT,
    PROMPT,
    Ask,
    Decision,
    Prior,
    Triage,
    TriageError,
    ask_validated,
    format_prior,
)

# The recall step's seam: a question in, similar past incidents out. Production
# passes a function built on `findyfixy.retrieval.retrieve.Retriever`. Tests pass a fake.
Recall = Callable[[str], Sequence[Prior]]

# The routing policy in one number. Treat it as a product decision, not a
# constant of nature: it trades cost against quality. Day 8 tunes it
# (0.6, 0.7, 0.8) and measures the effect on cost, latency and faithfulness.
THRESHOLD = 0.7

# The five questions from the lesson, word for word. They are fixed so that
# runs are comparable over time. Changing them would break that comparison.
QUESTIONS = [
    "A pod is in CrashLoopBackOff after a config change. First three checks?",
    "p95 latency on auth doubled at 02:00 UTC with no deploy. Likely causes?",
    "What does HTTP 429 from our vendor API mean and what do we do?",
    "Two regions disagree on a feature flag value. How do we find which is right?",
    "Customer reports intermittent voice-verification failures only on one carrier. Plan?",
]


class S(TypedDict, total=False):
    """The graph state: the shared notebook every node reads and writes.

    A node returns only the keys it changes. LangGraph merges them in.
    With no custom reducer, a new value for a key replaces the old one.
    That is why `large` overwrites `small`'s result and `model_used`.

    `triage` is None when the small model never produced a valid record. That
    is a real state, not an error: `route` sends it to `large`.

    `total=False` makes every key optional. That lets us start a run with
    just {"question": ...}. The cost: the type checker no longer guarantees
    that `triage` exists when `route` reads it. It exists at runtime only
    because `small` always runs first and always sets it.
    """

    question: str
    triage: Triage | None
    model_used: str
    evidence: str
    prior: str
    # Agent mode: why the tool loop stopped early, or None when it answered.
    stopped: Stopped | None
    # Agent mode: every lookup the agent made, for the eval record and the judge's contexts.
    calls: list[ToolCallRecord]
    # Agent mode: the decision the agent submitted in its loop, if it did.
    proposed: Decision


class Node(Protocol):
    """A graph node: state in, state update out. LangGraph matches nodes by the parameter name `state`."""

    def __call__(self, state: S) -> S: ...


# The compiled router, typed by its state.
TriageGraph = CompiledStateGraph[S, None, S, S]


def build_graph(
    ask: Ask,
    *,
    small_model: str,
    large_model: str,
    threshold: float = THRESHOLD,
    emit: Emit = no_emit,
    investigator: Investigator | None = None,
    investigate_model: str | None = None,
    recall: Recall | None = None,
    agent: bool = False,
    agent_instructions: Callable[[str], str] = agent_prompt,
) -> TriageGraph:
    """Wire the router and return a compiled graph, ready for `.invoke()`.

    Nothing runs here. Compiling validates the wiring (every edge points at a
    real node) and returns a runnable. A model is called only on `.invoke()`.

    Model names are plain arguments, not environment lookups. Reading the
    environment is the caller's job (`main` does it). That keeps this function
    pure and keeps a missing variable from surfacing deep inside a graph run.

    `emit` reports node entry and exit, model calls and the route decision
    (findyfixy/events.py). The default reports nothing.

    `investigator` (findyfixy/tools/investigate.py) is an optional tool-calling step that
    runs once, before `small`. Its tool results become `evidence` in state and
    in both models' prompts. If `large` takes over, it reuses that evidence and
    the tools are not called again. With no investigator the graph is exactly
    the two-node router it was before: same nodes, same prompts.

    `investigate_model` is the name to show for the investigator's model in
    events. The investigator is injected with its model already bound, so the
    graph cannot know the name on its own. `None` means unnamed.

    `recall` (findyfixy/retrieval/retrieve.py) is an optional step that runs first. It looks up
    similar past incidents for the question and puts them in both models' prompts
    as `prior`. Retrieval before generation: if the right incident never reaches
    the prompt, no prompt recovers it. With no `recall` the graph is unchanged.

    `agent` turns on agent mode (see the module docstring). It needs an
    `investigator` whose tools include incident search, and no `recall` step:
    recall is one of the agent's tools, and a second fixed recall would put
    incidents in the prompt that the agent did not choose. Raises ValueError
    on either mistake, at build time.

    `agent_instructions` wraps the question in the agent's instructions. It
    must name the tools the investigator really has; `wiring.agent_instructions`
    builds the one that matches a data version's change tool.
    """
    if agent and investigator is None:
        raise ValueError("agent mode needs an investigator: the agent's tools are its only evidence")
    if agent and recall is not None:
        raise ValueError("agent mode takes no recall step: incident search is one of the agent's tools")
    record: type[Triage] = Decision if agent else Triage
    template = DECISION_PROMPT if agent else PROMPT

    def recall_node(state: S) -> S:
        """Look up similar past incidents and store them as prompt text.

        An infrastructure fault (database down) propagates, like Ollama being
        down: a retry here would hide it. The exit event lists the incident IDs
        so the page and the log show what the model was given.
        """
        assert recall is not None  # wired only when recall is set; this narrows the type
        emit("node_entered", node="recall", model=None)
        items = list(recall(state["question"]))
        emit(
            "node_exited",
            node="recall",
            model=None,
            incidents=[item.incident_id for item in items],
        )
        return {"prior": format_prior(items)}

    def investigate(state: S) -> S:
        """Run the tool loop once, reporting each step as it finishes.

        The loop is a nested graph. Streaming it with the "updates" and
        "tasks" modes hands this node each step as it starts and as it
        finishes, so events are emitted while the loop runs, not after it, and
        a model request is reported when the loop starts one. That matters for a live viewer: each model
        call here is a real request to Ollama, and the viewer should show it
        when it happens. `tool_call` is emitted BEFORE the tool runs (the loop
        is paused until this body returns to the generator) and `tool_result`
        after it. Failures from infrastructure (Ollama down) propagate like any
        other.

        The node's exit event carries the whole step's cost, because this step
        adds model calls to every run, even one that ends with no lookup.
        """
        assert investigator is not None  # wired only when an investigator is set
        emit("node_entered", node="investigate", model=investigate_model)
        prompt = agent_instructions if agent else investigation_prompt
        messages: list[BaseMessage] = [HumanMessage(prompt(state["question"]))]
        stopped: Stopped | None = None
        turn = 0
        # "tasks" reports a node as it starts, so a model request is reported
        # exactly when the loop really makes one, never ahead of a call the
        # loop then skips (a finished submission, a budget stop).
        for mode, raw in investigator.stream({"messages": list(messages)}, stream_mode=["updates", "tasks"]):
            chunk = cast("Mapping[str, Any]", raw)  # both modes yield a mapping
            if mode == "tasks":
                if chunk.get("name") == "agent" and "input" in chunk:
                    turn += 1
                    emit("model_request", model=investigate_model, attempt=turn)
                continue
            for step, delta in chunk.items():
                new = delta.get("messages", [])
                messages.extend(new)
                if step == "agent":
                    if not new:
                        # The reply was cut off at the deadline: the request was
                        # real, so it gets a reply event that says it was cut.
                        emit("model_reply", model=investigate_model, attempt=turn, reply="", cut=True)
                    for reply in new:
                        _emit_agent_reply(reply, turn)
                elif step == "tools":
                    for result in new:
                        if isinstance(result, ToolMessage):
                            emit(
                                "tool_result",
                                name=result.name,
                                result=content_text(result.content)[:MAX_RESULT_CHARS],
                                error=result.status == "error",
                            )
                elif step == "budget":
                    stopped = delta["stopped"]
        inv = analyse(messages)
        emit(
            "node_exited",
            node="investigate",
            model=investigate_model,
            calls=len(inv.calls),
            turns=inv.turns,
            input_tokens=inv.input_tokens,
            output_tokens=inv.output_tokens,
            # The loop's own reason, not "the last reply asked for tools", which
            # is also true when the deadline stopped it or the agent submitted.
            hit_turn_cap=stopped == "turns",
            served_by=list(inv.served_by),
            stopped=stopped,
        )
        # The submission is the agent's decision, not evidence: it stays out of
        # the evidence text and the judge's contexts.
        looked = [c for c in inv.calls if c.name != SUBMIT_TOOL]
        evidence = format_evidence(tuple(looked))
        if agent and inv.answer:
            # The agent's own conclusion, so a fallback decision does not redo
            # the reasoning (a cascade traced upstream). Clipped like a tool
            # result, and quoted under the same "data, not instructions" heading.
            evidence += f"\nfinding of the investigation: {inv.answer[:MAX_RESULT_CHARS]}"
        out: S = {"evidence": evidence, "stopped": stopped, "calls": looked}
        submitted = submission(messages, SUBMIT_TOOL) if agent else None
        if submitted is not None:
            # Already validated by the tool node against the same schema.
            out["proposed"] = Decision.model_validate(submitted)
        return out

    def _emit_agent_reply(reply: BaseMessage, turn: int) -> None:
        """Report one investigator model reply, then any tool calls in it."""
        if not isinstance(reply, AIMessage):
            return
        served_by = (reply.response_metadata or {}).get("model")
        usage: Mapping[str, object] = reply.usage_metadata or {}
        emit(
            "model_reply",
            model=investigate_model,
            attempt=turn,
            reply=content_text(reply.content),
            served_by=served_by,
            mismatch=bool(served_by and investigate_model and served_by != investigate_model),
            input_tokens=usage.get("input_tokens"),
            output_tokens=usage.get("output_tokens"),
        )
        for call in reply.tool_calls:
            emit("tool_call", name=call["name"], args=call["args"])

    def make_node(name: str, model: str, *, tolerate_invalid: bool) -> Node:
        """Build a node that asks `model` and writes the result into state.

        `small` and `large` differ only in which model they call and in what
        happens when the model stays invalid after its retry. `small` records
        `triage=None` so the router can escalate. `large` has nowhere to
        escalate to, so it lets `TriageError` propagate. One factory means the
        two cannot drift apart. The node returns a partial state update;
        LangGraph merges it. See `S` for the merge rule.
        """

        def node(state: S) -> S:
            emit("node_entered", node=name, model=model)
            proposed = state.get("proposed") if name == "small" else None
            # Who wrote the decision. The agent's in-loop submission was written
            # by the investigator's model, which need not be the small model
            # (a cheap investigator, a different decider). Unnamed falls back to
            # this node's model, the only name the graph has.
            author = (investigate_model or model) if proposed is not None else model
            triage: Triage | None
            try:
                if proposed is not None:
                    # The agent submitted a valid decision in its loop: no
                    # second model call. Reported as a valid first attempt.
                    emit("validation", model=author, attempt=1, ok=True, error=None)
                    triage = proposed
                else:
                    triage = ask_validated(
                        ask,
                        model,
                        state["question"],
                        emit=emit,
                        evidence=state.get("evidence", ""),
                        prior=state.get("prior", ""),
                        record=record,
                        template=template,
                    )
            except TriageError:
                if not tolerate_invalid:
                    raise
                triage = None
            stopped = state.get("stopped")
            if isinstance(triage, Decision) and stopped is not None:
                triage = hand_over(triage, stopped, emit)
            emit(
                "node_exited",
                node=name,
                model=author,
                triage=triage.model_dump() if triage else None,
            )
            return {"triage": triage, "model_used": author}

        return node

    def route(state: S) -> Literal["large", "__end__"]:
        """The routing policy. Returns the name of the next node, or END.

        This is a pure function of the state: no model call, no network, no
        clock. That makes it deterministic and testable on its own.

        A missing record always escalates. Otherwise the comparison is `>=`,
        so exactly 0.7 stays on the small model.

        One model, no second tier: when `small_model` and `large_model` are the
        same, escalating would ask the same model the same question again and
        double the latency for no new information. The run ends instead. This
        is how a single answering model is configured (set both names alike).

        The return annotation is load-bearing. LangGraph reads it to learn
        where this edge can go. With a bare `str` it cannot, and its diagram
        drops the `small -> large` edge. Runtime routing would still work,
        because routing uses the returned value. The annotation exists for
        tooling and for the reader. `tests/test_routing.py` pins it.
        """
        if small_model == large_model:
            return "__end__"  # langgraph's END, spelled out so the Literal holds
        triage = state["triage"]
        if triage is None:
            return "large"  # no decision at all: the large model must produce one
        if agent and state.get("stopped") is not None:
            # Out of budget: the decision was handed to a person, and a large-model
            # decision would be handed over too. Escalating would only add
            # latency to an incident a person already has.
            return "__end__"
        if triage.confidence < threshold:
            return "large"
        return "__end__"

    def decide(state: S) -> Literal["large", "__end__"]:
        """Run `route` and report the decision. Kept apart so `route` stays pure.

        The `Literal` annotation is repeated on purpose: LangGraph reads the
        one on the function it is given, which is this one.
        """
        target = route(state)
        triage = state["triage"]
        emit(
            "route",
            confidence=triage.confidence if triage else None,
            threshold=threshold,
            target=target,
        )
        return target

    g: StateGraph[S] = StateGraph(S)
    g.add_node("small", make_node("small", small_model, tolerate_invalid=True))
    g.add_node("large", make_node("large", large_model, tolerate_invalid=False))
    # Steps before the cheap model, in order: recall, then investigate. Each is
    # optional, and the chain ends at `small`.
    before = []
    if recall is not None:
        g.add_node("recall", recall_node)
        before.append("recall")
    if investigator is not None:
        g.add_node("investigate", investigate)
        before.append("investigate")
    chain = [*before, "small"]
    g.set_entry_point(chain[0])  # without optional steps, the cheap model
    for here, there in pairwise(chain):
        g.add_edge(here, there)
    g.add_conditional_edges("small", decide)  # the decision point
    g.add_edge("large", END)  # a plain edge: after `large`, always stop
    return g.compile()


def hand_over(decision: Decision, stopped: Stopped, emit: Emit = no_emit) -> Decision:
    """Replace the action with page_human because the investigation ran out of budget.

    Code decides this, not the model: a decision made on a cut-short
    investigation may rest on evidence the agent never got to see, so it is
    handed to a person whatever it proposed. The triage fields stay as the
    model wrote them, for the person to read. `handover` records what was
    proposed and why it was replaced. A proposal that was already page_human
    is unchanged and still reported, so every budget stop is visible.
    """
    emit("handover", reason=stopped, proposed=decision.action, rollback_version=decision.rollback_version)
    return decision.model_copy(update={"action": "page_human", "rollback_version": None})


def main() -> None:
    """Run the five lesson questions and print triage, model, confidence, latency.

    Latency is wall time for the whole graph run, measured with perf_counter
    (a monotonic clock, so system clock changes cannot skew it). It includes
    model load time on a cold start, so the first question is often slower.
    Five samples is too few for a p95. It is enough to see the shape.
    Escalation rate (runs that reach `large`, divided by total runs) is the
    number to watch. Count it from the `model:` lines.

    Configuration comes from `findyfixy.settings`. A missing model name stops the run
    loudly with the variable's name, not with a silent default.
    """
    from contextlib import nullcontext

    from findyfixy.observability.adapter import RunTracer
    from findyfixy.observability.tracing import command_trace

    load_env()
    try:
        models = read_models()
    except MissingEnvironment as err:
        raise SystemExit(f"error: set the environment variable {err}") from None
    with command_trace("triage cli", input=list(QUESTIONS)) as command:
        # Each question is one FindyFixy run inside the command's trace.
        runs = RunTracer(command.tracer) if command.tracer is not None else None
        graph = build_graph(
            ollama_ask,
            small_model=models.small,
            large_model=models.large,
            emit=runs.wrap(no_emit) if runs is not None else no_emit,
        )
        for q in QUESTIONS:
            _triage_one(graph, q, runs.run(q) if runs is not None else nullcontext())


def _triage_one(graph: TriageGraph, q: str, run: "AbstractContextManager[EventSpans | None]") -> None:
    """Run one question, end its run trace (when tracing), print the triage and latency."""
    with run as spans:
        start = time.perf_counter()
        result = graph.invoke({"question": q})
        latency = time.perf_counter() - start
        if spans is not None:
            spans.finish_run(output=result["triage"].model_dump(), model_used=result["model_used"])
        print(f"Q: {q}")
        t = result["triage"]
        print(f"A: {t.severity} | {t.component} | {t.next_action}")
        print(f"model: {result['model_used']} | confidence: {t.confidence} | latency: {latency:.1f}s\n")


if __name__ == "__main__":
    main()
