"""Tests for the agent (Day 7, step 2): investigate with tools, then decide.

What these tests cover
    - The `Decision` record: a rollback must name a version, the version is a
      bare version number (no injected flags), and no other action carries one.
    - The three read-only tools: incident search over an injected recall, the
      deploy log per data version, the service map (and that every v2 cascade's
      cause sits upstream of the service that alerted).
    - The loop's budget: the turn cap and the wall-clock deadline each stop the
      loop and say which one did; an answer leaves `stopped` unset.
    - Agent mode in the graph: recall is a tool, not a step; the decision is a
      `Decision`; a stop on the budget hands the incident to a person, in code.
    - The eval adapter in agent mode: the action label, the tool results as
      the judge's contexts, every search's incidents recorded.
    - The command line: `--agent` refuses the gate's flags.

Why fakes
    The tool-calling model and `ask` are scripted; the tools, the ToolNode, the
    graph and the adapter are the real ones. The clock is a counter, so the
    deadline is tested without sleeping. A scripted reply can move the clock
    on, the way a slow model call spends real time.
"""

from collections.abc import Callable, Iterator
from datetime import datetime
import json
from pathlib import Path
from typing import Any

from langchain_core.messages import AIMessage, AIMessageChunk
from pydantic import ValidationError
import pytest

from findyfixy.console.fakes import as_chunk
from findyfixy.evals import answers as eval_answers
from findyfixy.evals.adapter import ACTION, ACTION_FOR, FindyFixy, to_case
from findyfixy.evals.answers import asks_for, take_fixed_flag
from findyfixy.evals.golden import load_golden
from findyfixy.evals.golden_v2 import load_golden_v2
from findyfixy.events import Event, make_emit
from findyfixy.paths import DATA
from findyfixy.tools.deploys import deploys_path, make_deploys_tool
from findyfixy.tools.investigate import agent_prompt, build_investigator
from findyfixy.tools.memory import NO_MATCH, make_search_tool
from findyfixy.tools.services import get_dependencies, load_map, make_services_tool
from findyfixy.tools.submit import SUBMIT_TOOL, make_submit_tool
from findyfixy.triage.graph import build_graph, hand_over
from findyfixy.triage.record import DECISION_PROMPT, Decision, Prior, ask_validated

# ---- helpers -----------------------------------------------------------------


def prior(incident_id: str, service: str = "auth", severity: str = "sev2") -> Prior:
    """A past incident with this id."""
    return Prior(incident_id, service, severity, "summary", "cause", "fix")  # type: ignore[arg-type]


def decision_json(action: str = "read", version: str | None = None, confidence: float = 0.9) -> str:
    """A valid Decision reply."""
    return json.dumps(
        {
            "severity": "sev1",
            "component": "auth",
            "next_action": "check the key cache",
            "confidence": confidence,
            "action": action,
            "rollback_version": version,
        }
    )


def call(name: str, args: dict[str, Any], id: str) -> dict[str, Any]:
    """One tool call as a model emits it."""
    return {"name": name, "args": args, "id": id, "type": "tool_call"}


def ai(content: str = "", calls: list[dict[str, Any]] | None = None) -> AIMessage:
    """A model reply with token usage."""
    usage = {"input_tokens": 10, "output_tokens": 5, "total_tokens": 15}
    return AIMessage(content=content, tool_calls=calls or [], usage_metadata=usage)  # type: ignore[arg-type]


class Scripted:
    """Plays prepared replies in order and counts the calls.

    With a clock, each reply moves it on by `seconds`: the time the call took.
    """

    def __init__(self, *replies: AIMessage, clock: "Clock | None" = None, seconds: float = 0.0) -> None:
        """Hold the replies."""
        self._replies = iter(replies)
        self.calls = 0
        self._clock, self._seconds = clock, seconds

    def invoke(self, messages: list[Any]) -> AIMessage:
        """The next reply."""
        self.calls += 1
        return next(self._replies)

    def stream(self, messages: list[Any]) -> Iterator[AIMessageChunk]:
        """The next reply as one chunk, arriving `seconds` later."""
        reply = self.invoke(messages)
        if self._clock is not None:
            self._clock.now += self._seconds
        yield as_chunk(reply)


class Clock:
    """A clock that moves `step` seconds every time it is read."""

    def __init__(self, step: float = 0.0) -> None:
        """Start at zero."""
        self.now, self.step = 0.0, step

    def __call__(self) -> float:
        """Read the time, then move it on."""
        value = self.now
        self.now += self.step
        return value


@pytest.fixture
def deploys(tmp_path: Path) -> Path:
    """A deploy log with one recent auth deploy."""
    path = tmp_path / "deploys.json"
    path.write_text(json.dumps([{"service": "auth", "version": "2.15.0", "hours_ago": 1}]))
    return path


def tools(deploys: Path, recall: Callable[[str], list[Prior]] | None = None) -> list[Any]:
    """The agent's three tools over test data."""
    return [make_search_tool(recall or (lambda q: [prior("INC-1")])), make_deploys_tool(deploys), make_services_tool()]


# ---- the Decision record -----------------------------------------------------


def test_a_rollback_must_name_a_version() -> None:
    """A rollback with no version is refused."""
    with pytest.raises(ValidationError, match="must name rollback_version"):
        Decision.model_validate_json(decision_json("rollback", None))


def test_the_rollback_version_cannot_carry_injected_text() -> None:
    """Only a bare version number passes: an injected flag or command does not."""
    for bad in ("2.15.0 --all --force", "all", "2.15.0; rm -rf /"):
        with pytest.raises(ValidationError):
            Decision.model_validate_json(decision_json("rollback", bad))
    assert Decision.model_validate_json(decision_json("rollback", "2.15.0")).rollback_version == "2.15.0"


def test_only_a_rollback_carries_a_version() -> None:
    """A page or a read that names a version is refused, so the record says one thing."""
    with pytest.raises(ValidationError, match="only for action rollback"):
        Decision.model_validate_json(decision_json("page_human", "2.15.0"))


def test_an_unknown_action_is_refused() -> None:
    """The action is a closed set."""
    with pytest.raises(ValidationError):
        Decision.model_validate_json(decision_json("restart_everything"))


def test_ask_validated_asks_for_a_decision_with_the_decision_prompt() -> None:
    """With `record=Decision` the prompt states the action rule and the result is a Decision."""
    prompts: list[str] = []

    def ask(model: str, prompt: str) -> str:
        prompts.append(prompt)
        return decision_json("page_human")

    result = ask_validated(ask, "m", "auth is down", record=Decision, template=DECISION_PROMPT)
    assert isinstance(result, Decision) and result.action == "page_human"
    assert "rollback_version" in prompts[0] and "Report: auth is down" in prompts[0]


def test_a_decision_missing_its_action_is_retried_with_the_error() -> None:
    """A reply without the action fails validation and is re-asked once, like any invalid record."""
    replies = iter(
        [json.dumps({"severity": "sev1", "component": "a", "next_action": "b", "confidence": 0.5}), decision_json()]
    )
    prompts: list[str] = []

    def ask(model: str, prompt: str) -> str:
        prompts.append(prompt)
        return next(replies)

    assert ask_validated(ask, "m", "q", record=Decision, template=DECISION_PROMPT).action == "read"
    assert "Validation error" in prompts[1] and "action" in prompts[1]


# ---- the tools ---------------------------------------------------------------


def test_search_runs_the_injected_recall_on_the_models_query() -> None:
    """The tool searches with the model's words and returns the prompt's own format."""
    asked: list[str] = []

    def recall(query: str) -> list[Prior]:
        asked.append(query)
        return [prior("INC-7")]

    out = make_search_tool(recall).invoke({"query": "auth key cache expired"})
    assert asked == ["auth key cache expired"]
    assert out.startswith("- INC-7 (auth, sev2): summary Cause: cause Fix: fix")


def test_search_says_so_when_nothing_matches() -> None:
    """An empty result is explicit text, not an empty string."""
    assert make_search_tool(lambda q: []).invoke({"query": "nothing like it"}) == NO_MATCH


def test_search_refuses_an_overlong_query() -> None:
    """A runaway reply cannot become the search."""
    with pytest.raises(ValidationError):
        make_search_tool(lambda q: []).invoke({"query": "x" * 301})


def test_the_deploy_tool_knows_every_service_and_reads_the_runs_data_version() -> None:
    """A service with no deploys answers [] (not an error), and v2 has the rollback cases' deploys."""
    assert make_deploys_tool(deploys_path("v1")).invoke({"service": "ledger", "hours": 24}) == "[]"
    v2 = json.loads(make_deploys_tool(deploys_path("v2")).invoke({"service": "auth", "hours": 24}))
    v1 = json.loads(make_deploys_tool(deploys_path("v1")).invoke({"service": "auth", "hours": 24}))
    assert {d["version"] for d in v1} < {d["version"] for d in v2}


def test_the_service_map_lists_upstreams_and_downstreams() -> None:
    """auth breaks search, billing and gateway; billing depends on auth, ledger and search."""
    path = DATA / "services.json"
    assert json.loads(get_dependencies("auth", path=path)) == {
        "service": "auth",
        "upstream": [],
        "downstream": ["billing", "gateway", "search"],
    }
    assert json.loads(get_dependencies("billing", path=path))["upstream"] == ["auth", "ledger", "search"]


def test_the_service_map_refuses_an_unknown_service() -> None:
    """The argument type is the closed set of services."""
    with pytest.raises(ValidationError):
        make_services_tool().invoke({"service": "mainframe"})


def test_a_map_that_misses_a_service_is_refused(tmp_path: Path) -> None:
    """An incomplete map is an operator fault, raised when read."""
    path = tmp_path / "services.json"
    path.write_text(json.dumps({"auth": []}))
    with pytest.raises(ValueError, match="every service once"):
        load_map(path)


# The service each v2 cascade alerted on, and the service its written cause is in.
CASCADE_CAUSE = {
    "h11": ("search", "auth"),
    "h12": ("billing", "ledger"),
    "h13": ("notifications", "scheduler"),
    "h14": ("voice-verification", "gateway"),
    "h15": ("billing", "search"),
}


def test_every_v2_cascade_cause_is_upstream_of_the_service_that_alerted() -> None:
    """Following the map from the alert reaches the cause, so the cases are answerable with the tools."""
    graph = load_map(DATA / "services.json")
    cases = {c.id: c for c in load_golden_v2()}
    for case_id, (alerted, cause) in CASCADE_CAUSE.items():
        assert cases[case_id].group == "cascade"
        assert alerted in cases[case_id].question and cause in cases[case_id].reference
        assert cause in graph[alerted], f"{case_id}: {cause} must be upstream of {alerted}"


# ---- the budget --------------------------------------------------------------


def run_loop(model: Scripted, deploys: Path, **kw: Any) -> dict[str, Any]:
    """Run the investigation loop to the end and return its final state."""
    loop = build_investigator(model, tools(deploys), **kw)
    return dict(loop.invoke({"messages": [("user", agent_prompt("auth logins failing"))]}))


def test_an_answer_ends_the_loop_with_no_stop_reason(deploys: Path) -> None:
    """Lookups, then a finding: the loop ends on the answer."""
    model = Scripted(ai(calls=[call("get_recent_deploys", {"service": "auth", "hours": 24}, "c1")]), ai("auth 2.15.0"))
    state = run_loop(model, deploys, max_turns=6, deadline_s=30.0, clock=Clock(1.0))
    assert "stopped" not in state and model.calls == 2


def test_the_turn_cap_stops_the_loop_and_says_so(deploys: Path) -> None:
    """A model that keeps asking for tools stops at the cap, with stopped = turns."""
    looping = [ai(calls=[call("get_dependencies", {"service": "auth"}, f"c{i}")]) for i in range(3)]
    state = run_loop(Scripted(*looping), deploys, max_turns=2, deadline_s=None)
    assert state["stopped"] == "turns" and state["turns"] == 2


def test_the_deadline_stops_the_loop_instead_of_running_more_tools(deploys: Path) -> None:
    """Past the deadline, requested tools do not run and the loop stops on the deadline."""
    clock = Clock()
    model = Scripted(
        ai(calls=[call("get_dependencies", {"service": "auth"}, "c1")]),
        ai(calls=[call("get_dependencies", {"service": "search"}, "c2")]),
        clock=clock,
        seconds=20.0,
    )
    # Each reply takes 20 s: the first arrives at 20 (< 30, its tool runs),
    # the second at 40, past the deadline, so it is cut off.
    state = run_loop(model, deploys, max_turns=6, deadline_s=30.0, clock=clock)
    assert state["stopped"] == "deadline" and model.calls == 2
    ran = [m for m in state["messages"] if m.type == "tool"]
    assert len(ran) == 1  # the second request was never executed


def test_past_the_deadline_after_tools_the_model_is_not_asked_again(deploys: Path) -> None:
    """Time spent by the tools counts: once it is gone, no new model call starts."""
    model = Scripted(ai(calls=[call("get_dependencies", {"service": "auth"}, "c1")]), ai("never asked"))
    # Reads: start 0, the reply's chunk 10, the check before tools 20 (< 30),
    # the check after tools 30: the deadline has passed.
    state = run_loop(model, deploys, max_turns=6, deadline_s=30.0, clock=Clock(10.0))
    assert state["stopped"] == "deadline" and model.calls == 1


class Slow:
    """A model whose one reply arrives in three chunks, `seconds` apart, and records whether its stream was closed."""

    def __init__(self, clock: Clock, seconds: float) -> None:
        """Hold the clock the chunks move on."""
        self.clock, self.seconds, self.closed, self.sent = clock, seconds, False, 0

    def invoke(self, messages: list[Any]) -> AIMessage:
        """Not used: a loop with a deadline streams."""
        raise AssertionError("a loop with a deadline must stream")

    def stream(self, messages: list[Any]) -> Iterator[AIMessageChunk]:
        """Three chunks of a finding; marks `closed` when the caller stops reading."""
        try:
            for word in ("auth ", "is the ", "cause"):
                self.clock.now += self.seconds
                self.sent += 1
                yield AIMessageChunk(content=word)
        finally:
            self.closed = True


def test_a_reply_still_arriving_at_the_deadline_is_cut_off_and_its_stream_closed(deploys: Path) -> None:
    """A single slow call cannot run past the deadline: the reply is dropped and the connection closed."""
    clock = Clock()
    model = Slow(clock, 15.0)
    state = run_loop(model, deploys, max_turns=6, deadline_s=30.0, clock=clock)
    assert state["stopped"] == "deadline" and state["turns"] == 1
    assert model.sent == 2 and model.closed  # cut at the chunk that arrived at 30 s
    assert not [m for m in state["messages"] if m.type == "ai"]


def test_a_reply_in_chunks_within_the_deadline_is_joined_whole(deploys: Path) -> None:
    """Streaming changes nothing for a reply that arrives in time."""
    clock = Clock()
    state = run_loop(Slow(clock, 5.0), deploys, max_turns=6, deadline_s=30.0, clock=clock)
    assert "stopped" not in state and state["messages"][-1].content == "auth is the cause"


def test_a_tool_that_fails_to_run_stops_the_run_instead_of_reaching_the_model(deploys: Path) -> None:
    """A database fault in search raises; only invalid arguments go back to the model as text."""

    def down(query: str) -> list[Prior]:
        raise ConnectionError("database down")

    model = Scripted(ai(calls=[call("search_incidents", {"query": "auth logins"}, "c1")]), ai("never reached"))
    loop = build_investigator(model, tools(deploys, recall=down), max_turns=6)
    with pytest.raises(ConnectionError, match="database down"):
        loop.invoke({"messages": [("user", "q")]})


def test_an_invalid_argument_goes_back_to_the_model(deploys: Path) -> None:
    """An unknown service is a tool invocation error: the model reads it and can correct itself."""
    model = Scripted(ai(calls=[call("get_dependencies", {"service": "mainframe"}, "c1")]), ai("ok"))
    state = run_loop(model, deploys, max_turns=6)
    errors = [m for m in state["messages"] if m.type == "tool" and m.status == "error"]
    assert len(errors) == 1 and model.calls == 2


# ---- the graph in agent mode -------------------------------------------------


def agent_graph(
    model: Scripted, deploys: Path, ask_replies: list[str], **kw: Any
) -> tuple[Any, list[Event], list[str]]:
    """The real graph in agent mode over a scripted loop and a scripted decision `ask`."""
    events: list[Event] = []
    prompts: list[str] = []
    replies = iter(ask_replies)

    def ask(model_name: str, prompt: str) -> str:
        prompts.append(prompt)
        return next(replies)

    loop = build_investigator(model, tools(deploys), **kw)
    graph = build_graph(
        ask,
        small_model="small",
        large_model="large",
        emit=make_emit(events.append, lambda: 0.0),
        investigator=loop,
        investigate_model="small",
        agent=True,
    )
    return graph, events, prompts


def test_agent_mode_needs_an_investigator_and_refuses_a_recall_step(deploys: Path) -> None:
    """Recall is the agent's tool; a fixed recall step or no tools at all is a wiring mistake."""
    with pytest.raises(ValueError, match="needs an investigator"):
        build_graph(lambda m, p: "", small_model="s", large_model="l", agent=True)
    loop = build_investigator(Scripted(), tools(deploys))
    with pytest.raises(ValueError, match="takes no recall step"):
        build_graph(
            lambda m, p: "", small_model="s", large_model="l", agent=True, investigator=loop, recall=lambda q: []
        )


def test_the_agent_decides_on_the_evidence_it_gathered(deploys: Path) -> None:
    """The deploy it found and its own finding reach the decision prompt, and the result is a Decision."""
    model = Scripted(
        ai(calls=[call("get_recent_deploys", {"service": "auth", "hours": 24}, "c1")]), ai("2.15.0 broke it")
    )
    graph, events, prompts = agent_graph(model, deploys, [decision_json("rollback", "2.15.0")], max_turns=6)
    state = graph.invoke({"question": "auth logins failing"})
    assert isinstance(state["triage"], Decision) and state["triage"].rollback_version == "2.15.0"
    assert "rollback_version" in prompts[0] and '"version": "2.15.0"' in prompts[0]
    assert "finding of the investigation: 2.15.0 broke it" in prompts[0]
    assert [c.name for c in state["calls"]] == ["get_recent_deploys"] and state["stopped"] is None
    assert not [e for e in events if e.kind == "handover"]


def test_a_budget_stop_hands_the_incident_to_a_person_whatever_was_proposed(deploys: Path) -> None:
    """Out of turns, a proposed rollback becomes page_human, and a handover event says why."""
    looping = [ai(calls=[call("get_dependencies", {"service": "auth"}, f"c{i}")]) for i in range(2)]
    graph, events, _ = agent_graph(Scripted(*looping), deploys, [decision_json("rollback", "2.15.0")], max_turns=2)
    state = graph.invoke({"question": "auth logins failing"})
    assert state["triage"].action == "page_human" and state["triage"].rollback_version is None
    handover = [e for e in events if e.kind == "handover"]
    assert len(handover) == 1 and handover[0].data == {
        "reason": "turns",
        "proposed": "rollback",
        "rollback_version": "2.15.0",
    }


def test_a_handed_over_incident_is_not_escalated(deploys: Path) -> None:
    """Low confidence after a hand-over does not call the large model: the incident is already with a person."""
    looping = [ai(calls=[call("get_dependencies", {"service": "auth"}, f"c{i}")]) for i in range(2)]
    graph, events, prompts = agent_graph(
        Scripted(*looping), deploys, [decision_json("read", confidence=0.1)], max_turns=2
    )
    state = graph.invoke({"question": "auth logins failing"})
    assert state["model_used"] == "small" and len(prompts) == 1
    assert len([e for e in events if e.kind == "handover"]) == 1


def test_a_deadline_stop_is_not_reported_as_the_turn_cap(deploys: Path) -> None:
    """node_exited says which budget stopped the loop."""
    model = Scripted(
        ai(calls=[call("get_dependencies", {"service": "auth"}, "c1")]),
        ai(calls=[call("get_dependencies", {"service": "search"}, "c2")]),
    )
    graph, events, _ = agent_graph(
        model, deploys, [decision_json("read")], max_turns=6, deadline_s=30.0, clock=Clock(20.0)
    )
    graph.invoke({"question": "auth logins failing"})
    exited = next(e for e in events if e.kind == "node_exited" and e.data["node"] == "investigate")
    assert exited.data["stopped"] == "deadline" and exited.data["hit_turn_cap"] is False


def test_a_cut_off_reply_is_reported_and_the_incident_handed_over(deploys: Path) -> None:
    """The viewer and the trace see the cut-off request end, and a person gets the incident."""
    clock = Clock()
    graph, events, _ = agent_graph(
        Slow(clock, 15.0), deploys, [decision_json("rollback", "2.15.0")], max_turns=6, deadline_s=30.0, clock=clock
    )
    state = graph.invoke({"question": "auth logins failing"})
    replies = [e for e in events if e.kind == "model_reply" and e.data.get("model") == "small"]
    assert replies[0].data["cut"] is True and replies[0].data["reply"] == ""
    assert state["triage"].action == "page_human"
    assert [e.data["reason"] for e in events if e.kind == "handover"] == ["deadline"]


def test_hand_over_keeps_the_triage_for_the_person_to_read() -> None:
    """Only the action changes; severity, component and next action stay as written."""
    original = Decision.model_validate_json(decision_json("rollback", "2.15.0"))
    handed = hand_over(original, "deadline")
    assert handed.action == "page_human" and handed.rollback_version is None
    assert (handed.severity, handed.component, handed.next_action) == (
        original.severity,
        original.component,
        original.next_action,
    )


# ---- submitting the decision -------------------------------------------------


SUBMITTED = {
    "severity": "sev1",
    "component": "auth",
    "next_action": "roll auth back to 2.14.1",
    "confidence": 0.8,
    "action": "rollback",
    "rollback_version": "2.14.1",
}


def submit(args: dict[str, Any], id: str) -> dict[str, Any]:
    """A submit_decision call."""
    return call(SUBMIT_TOOL, args, id)


def test_a_valid_submission_ends_the_loop_without_another_model_call(deploys: Path) -> None:
    """Look up, submit, done: two model calls, and the decision comes from the submission."""
    model = Scripted(
        ai(calls=[call("get_recent_deploys", {"service": "auth", "hours": 24}, "c1")]),
        ai(calls=[submit(SUBMITTED, "s1")]),
    )
    loop = build_investigator(model, [*tools(deploys), make_submit_tool()], max_turns=6, finish_tool=SUBMIT_TOOL)
    state = loop.invoke({"messages": [("user", "q")]})
    assert model.calls == 2 and "stopped" not in state


def test_an_invalid_submission_goes_back_to_the_model_to_correct(deploys: Path) -> None:
    """A rollback with no version is refused by the schema; the corrected one ends the loop."""
    bad = {**SUBMITTED, "rollback_version": None}
    model = Scripted(ai(calls=[submit(bad, "s1")]), ai(calls=[submit(SUBMITTED, "s2")]))
    loop = build_investigator(model, [*tools(deploys), make_submit_tool()], max_turns=6, finish_tool=SUBMIT_TOOL)
    state = loop.invoke({"messages": [("user", "q")]})
    errors = [m for m in state["messages"] if m.type == "tool" and m.status == "error"]
    assert model.calls == 2 and len(errors) == 1


def test_the_graph_takes_the_submitted_decision_without_a_decision_call(deploys: Path) -> None:
    """No ask call; the submission is the decision, and it stays out of the evidence and the calls."""
    model = Scripted(
        ai(calls=[call("get_recent_deploys", {"service": "auth", "hours": 24}, "c1")]),
        ai(calls=[submit(SUBMITTED, "s1")]),
    )
    events: list[Event] = []
    loop = build_investigator(model, [*tools(deploys), make_submit_tool()], max_turns=6, finish_tool=SUBMIT_TOOL)

    def never(model_name: str, prompt: str) -> str:
        raise AssertionError("no decision call when the agent submitted")

    graph = build_graph(
        never,
        small_model="s",
        large_model="l",
        emit=make_emit(events.append, lambda: 0.0),
        investigator=loop,
        investigate_model="s",
        agent=True,
    )
    state = graph.invoke({"question": "auth logins failing"})
    assert isinstance(state["triage"], Decision) and state["triage"].rollback_version == "2.14.1"
    assert [c.name for c in state["calls"]] == ["get_recent_deploys"]
    assert SUBMIT_TOOL not in state["evidence"]
    exited = next(e for e in events if e.kind == "node_exited" and e.data["node"] == "investigate")
    assert exited.data["hit_turn_cap"] is False


def test_a_submission_on_the_last_turn_is_kept(deploys: Path) -> None:
    """A decision submitted on the turn cap runs and ends the loop; it is not thrown away as a budget stop."""
    looks = [ai(calls=[call("get_dependencies", {"service": "auth"}, f"c{i}")]) for i in range(2)]
    model = Scripted(*looks, ai(calls=[submit(SUBMITTED, "s1")]))
    loop = build_investigator(model, [*tools(deploys), make_submit_tool()], max_turns=3, finish_tool=SUBMIT_TOOL)
    state = loop.invoke({"messages": [("user", "q")]})
    assert "stopped" not in state and state["turns"] == 3


def test_every_reported_model_request_really_happens(deploys: Path) -> None:
    """After a submission ends the loop, no further model request is reported."""
    model = Scripted(ai(calls=[submit(SUBMITTED, "s1")]))
    events: list[Event] = []
    loop = build_investigator(model, [*tools(deploys), make_submit_tool()], max_turns=6, finish_tool=SUBMIT_TOOL)
    graph = build_graph(
        lambda m, p: "",
        small_model="s",
        large_model="l",
        emit=make_emit(events.append, lambda: 0.0),
        investigator=loop,
        investigate_model="s",
        agent=True,
    )
    graph.invoke({"question": "q"})
    requests = [e for e in events if e.kind == "model_request" and e.data["model"] == "s" and "prompt" not in e.data]
    replies = [e for e in events if e.kind == "model_reply" and "attempt" in e.data and e.data["model"] == "s"]
    assert len(requests) == model.calls == 1 and len(replies) == 1


def test_a_submitted_decision_is_credited_to_the_model_that_wrote_it(deploys: Path) -> None:
    """The investigator's model wrote an in-loop submission: the record and events name it, not the small model."""
    model = Scripted(ai(calls=[submit(SUBMITTED, "s1")]))
    events: list[Event] = []
    loop = build_investigator(model, [*tools(deploys), make_submit_tool()], max_turns=6, finish_tool=SUBMIT_TOOL)
    graph = build_graph(
        lambda m, p: "",
        small_model="s",
        large_model="s",  # one tier: no escalation can change model_used
        emit=make_emit(events.append, lambda: 0.0),
        investigator=loop,
        investigate_model="cheap",
        agent=True,
    )
    assert graph.invoke({"question": "q"})["model_used"] == "cheap"
    exited = [e for e in events if e.kind == "node_exited" and e.data["node"] == "small"]
    assert [e.data["model"] for e in exited] == ["cheap"]


def test_an_out_of_budget_agent_with_no_valid_decision_still_escalates(deploys: Path) -> None:
    """No record at all goes to the large model, which is then handed over: the run never ends empty."""
    looping = [ai(calls=[call("get_dependencies", {"service": "auth"}, f"c{i}")]) for i in range(2)]
    replies = {"small": ["not json", "still not json"], "large": [decision_json("rollback", "2.14.1")]}
    queues = {k: iter(v) for k, v in replies.items()}
    loop = build_investigator(Scripted(*looping), tools(deploys), max_turns=2)
    graph = build_graph(
        lambda m, p: next(queues[m]),
        small_model="small",
        large_model="large",
        investigator=loop,
        investigate_model="small",
        agent=True,
    )
    state = graph.invoke({"question": "q"})
    assert state["model_used"] == "large" and state["triage"].action == "page_human"


def test_the_fixed_path_tool_step_still_escalates_after_its_turn_cap(deploys: Path) -> None:
    """The hand-over rule is the agent's: the Day 2 tool step keeps its low-confidence escalation."""
    looping = [ai(calls=[call("get_recent_deploys", {"service": "auth", "hours": 24}, f"c{i}")]) for i in range(5)]
    loop = build_investigator(Scripted(*looping), [make_deploys_tool(deploys)], max_turns=2)
    low = json.dumps({"severity": "sev2", "component": "auth", "next_action": "look", "confidence": 0.2})
    high = json.dumps({"severity": "sev2", "component": "auth", "next_action": "look", "confidence": 0.9})
    queues = {"small": iter([low]), "large": iter([high])}
    graph = build_graph(lambda m, p: next(queues[m]), small_model="small", large_model="large", investigator=loop)
    assert graph.invoke({"question": "q"})["model_used"] == "large"


def test_a_rollback_names_the_version_to_roll_back_to() -> None:
    """The field's meaning, as the model and engineers say it: the version to return to."""
    assert "roll back to" in Decision.model_fields["rollback_version"].description


# ---- the eval adapter in agent mode ------------------------------------------


def test_v2_cases_expect_their_action_only_in_agent_mode() -> None:
    """The action label is added for v2 cases in agent mode; v1 and the fixed path are unchanged."""
    hard = load_golden_v2()[25]  # h26, a destructive case
    assert to_case(hard).expected == {"severity": hard.expected_severity}
    assert to_case(hard, agent=True).expected == {"severity": hard.expected_severity, ACTION: "rollback"}
    v1 = load_golden()[0]
    assert to_case(v1, agent=True).expected == to_case(v1).expected
    assert set(ACTION_FOR) == {"read", "reversible", "destructive"}


def test_an_agent_v2_case_passes_the_harness_record_check() -> None:
    """The action label's record keys do not clash with the case's meta (the harness refuses a clash)."""
    from wyld3rness.eval.runner import new_record

    for case in load_golden_v2():
        record = new_record(to_case(case, agent=True))
        assert record["expected_action"] == ACTION_FOR[case.expected_action]
        assert record["action_class"] == case.expected_action


class FakeAgentGraph:
    """Searches twice through the injected recall, then returns a canned decision with its tool calls."""

    def __init__(self, recall: Callable[[str], list[Prior]], at: datetime | None) -> None:
        """Hold the recall the adapter bound to this case, and its alert time."""
        self.recall = recall
        self.at = at

    def invoke(self, state: dict[str, Any]) -> dict[str, Any]:
        """Two searches, one decision."""
        from findyfixy.tools.investigate import ToolCallRecord

        self.recall("first query")
        self.recall("second query")
        calls = [ToolCallRecord("search_incidents", {"query": "first query"}, "- INC-1 ...", False)]
        decision = Decision.model_validate_json(decision_json("rollback", "2.15.0"))
        return {"triage": decision, "model_used": "small", "calls": calls, "stopped": None}


def test_the_agent_answer_carries_its_action_tool_calls_and_every_searched_incident() -> None:
    """Labels, contexts and meta in agent mode; the source stays held out of every search."""
    case = to_case(load_golden_v2()[25], agent=True)  # h26: its own incident id is h26
    ranking = {"first query": [prior("h26"), prior("A"), prior("B")], "second query": [prior("B"), prior("C")]}
    sut = FindyFixy(
        make_graph=FakeAgentGraph,
        candidates=lambda q, n: ranking[q][:n],
        small_model="s",
        large_model="l",
        k=2,
        agent=True,
    )
    answer = sut.answer(case)
    assert answer.labels == {"severity": "sev1", ACTION: "rollback"}
    assert list(answer.contexts) == ["search_incidents(query=first query) result: - INC-1 ..."]
    assert answer.meta["recalled_ids"] == ["A", "B", "C"]
    assert answer.meta["tool_calls"] == ["search_incidents(query=first query)"]
    assert answer.meta["rollback_version"] == "2.15.0" and answer.meta["stopped"] is None
    assert sut.describe()["system"] == "agent"


def test_a_fixed_path_adapter_says_it_is_the_fixed_system() -> None:
    """The fixed path names itself, and carries none of the agent's budget keys."""
    sut = FindyFixy(make_graph=FakeAgentGraph, candidates=lambda q, n: [], small_model="s", large_model="l")
    assert sut.describe() == {
        "system": "fixed",
        "small_model": "s",
        "large_model": "l",
        "threshold": 0.7,
        "recall_k": 3,
    }


def test_the_gate_refuses_a_baseline_frozen_on_the_other_system() -> None:
    """A fixed-path baseline cannot gate the agent: the harness compares provenance `system`."""
    from wyld3rness.eval.report import comparability_problems

    same = {"judge_digest": "d", "prompt_versions": "p", "golden_sha256": "g"}
    agent_run = {"provenance": {**same, "system": "agent"}}
    fixed_baseline = {"provenance": {**same, "system": "fixed"}}
    old_baseline = {"provenance": same}  # frozen before systems were named
    assert "system differs from the baseline" in comparability_problems(agent_run, fixed_baseline)
    assert "system differs from the baseline" in comparability_problems(agent_run, old_baseline)
    assert comparability_problems(agent_run, {"provenance": {**same, "system": "agent"}}) == []


# ---- the command line --------------------------------------------------------


def test_the_fixed_flag_is_split_off_before_the_harness_parses() -> None:
    """`--fixed` is the app's flag; the rest goes to the harness unchanged."""
    assert take_fixed_flag(["--fixed", "--workers", "2"]) == (True, ["--workers", "2"])
    assert take_fixed_flag(["--workers", "2"]) == (False, ["--workers", "2"])


def test_a_fixed_path_run_refuses_the_gate_and_the_baseline_in_any_abbreviation(capsys: Any) -> None:
    """The baseline is the agent's, so a fixed-path run is scores only."""
    for flag in ("--gate", "--ga", "--write-baseline", "--baseline-from=x"):
        assert asks_for([flag], eval_answers.GATE_FLAGS)
        assert eval_answers.main(["--fixed", flag]) == 2
    assert "run --fixed without them" in capsys.readouterr().err
