"""Every agent tool behind the credential broker: same tool to the model, a verified token on every call.

Fakes only: the committed data files, no model.
"""

from __future__ import annotations

import json

import pytest

from findyfixy.tools.changes import CHANGES_TOOL
from findyfixy.tools.deploys import DEPLOYS_TOOL, make_deploys_tool
from findyfixy.tools.guard import AGENT, GRANTS, guard, resource_of
from findyfixy.tools.memory import SEARCH_TOOL, make_search_tool
from findyfixy.tools.services import SERVICES_TOOL, make_services_tool
from findyfixy.tools.submit import SUBMIT_TOOL
from wyld3rness.identity.broker import CredentialBroker


def test_a_guarded_tool_looks_and_answers_the_same() -> None:
    """The model sees the same name, description and schema; a granted call returns the body's own answer."""
    plain = make_services_tool()
    guarded = guard(plain, CredentialBroker(GRANTS), AGENT)
    assert (guarded.name, guarded.description) == (plain.name, plain.description)
    assert guarded.args_schema == plain.args_schema
    assert guarded.invoke({"service": "auth"}) == plain.invoke({"service": "auth"})


def test_a_refused_call_never_runs_the_tool_and_returns_an_error_to_the_agent() -> None:
    """A grant revoked after wiring: the broker refuses the token, the body is not called, and the agent reads why."""
    ran: list[str] = []
    broker = CredentialBroker({"memory": frozenset({SEARCH_TOOL})})
    guarded = guard(make_search_tool(lambda q: ran.append(q) or []), broker, "memory")
    broker._grants = {}  # revoke: the broker's own table, as an operator would
    reply = guarded.invoke({"name": SEARCH_TOOL, "args": {"query": "auth"}, "id": "c1", "type": "tool_call"})
    assert ran == []
    assert reply.status == "error" and "credential refused: memory is not granted search_incidents" in reply.content


def test_each_call_is_scoped_to_the_resource_it_reads() -> None:
    assert resource_of(CHANGES_TOOL, {"service": "auth", "hours": 2}) == "changes:auth"
    assert resource_of(DEPLOYS_TOOL, {"service": "billing", "hours": 24}) == "changes:billing"
    assert resource_of(SERVICES_TOOL, {"service": "gateway"}) == "services:gateway"
    assert resource_of(SEARCH_TOOL, {"query": "x"}) == "incidents"
    assert resource_of(SUBMIT_TOOL, {}) == "decision"


def test_the_real_agent_wires_only_granted_tools() -> None:
    """build_agent guards every tool it gives, and guard refuses an ungranted one at wiring time, so this builds."""
    from datetime import UTC, datetime

    from findyfixy.triage.record import AlertFacts
    from findyfixy.wiring import build_agent

    build_agent("m", lambda q: [])
    build_agent("m", lambda q: [], "v3", AlertFacts(datetime(2026, 10, 26, 13, 5, tzinfo=UTC), "auth"))


def test_a_tool_without_a_grant_fails_when_wired_not_at_every_call() -> None:
    with pytest.raises(ValueError, match="holds no grant for search_incidents"):
        guard(make_search_tool(lambda q: []), CredentialBroker({"memory": frozenset()}), "memory")


def test_only_a_credential_refusal_reaches_the_agent_as_text() -> None:
    """Any other tool error is raised, as make_tools_node requires: an outage stops the run."""
    from langchain_core.tools import StructuredTool, ToolException

    def down(service: str) -> str:
        raise ToolException("change service unavailable")

    broken = StructuredTool.from_function(func=down, name=SERVICES_TOOL, description="d")
    guarded = guard(broken, CredentialBroker(GRANTS), AGENT)
    with pytest.raises(ToolException, match="unavailable"):
        guarded.invoke({"service": "auth"})


def test_an_async_tool_is_guarded_too() -> None:
    """MCP tools are async only; the guard awaits them."""
    import asyncio

    from langchain_core.tools import StructuredTool

    async def lookup(service: str) -> str:
        return f"async {service}"

    tool = StructuredTool.from_function(coroutine=lookup, name=SERVICES_TOOL, description="d")
    guarded = guard(tool, CredentialBroker(GRANTS), AGENT)
    assert asyncio.run(guarded.ainvoke({"service": "auth"})) == "async auth"


def test_a_guarded_deploy_lookup_reads_the_same_log() -> None:
    plain = make_deploys_tool()
    guarded = guard(plain, CredentialBroker(GRANTS), AGENT)
    args = {"service": "auth", "hours": 24}
    assert json.loads(guarded.invoke(args)) == json.loads(plain.invoke(args))
