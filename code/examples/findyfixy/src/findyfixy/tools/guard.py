"""Every agent tool behind the harness's credential broker: a token per call, verified before the tool reads.

What it does
    `guard(tool, broker, agent)` returns the same tool (name, description,
    argument schema) with two steps around its body:
    1. the agent side asks the broker for a token for this agent, this tool
       and the resource the arguments name (`resource_of`);
    2. the tool side verifies that token before the body runs.
    A refusal (a token for another agent, tool or resource, an expired one)
    comes back to the agent as an error result, and the tool body never runs.
    Any other tool error is raised, as in `make_tools_node`: an outage stops
    the run instead of reaching the model as text. An agent wired with a tool
    it was not granted fails when it is built.

Why two steps in one process
    Today both halves run in the agent's process, so they share the broker's
    key and the same arguments: in this wiring a verify cannot fail, and the
    guard proves the contract, not isolation (see the broker's docstring).
    The split is the seam: when a tool moves behind a service (Day 9), the
    verify step moves with it and checks a token it received, not one it
    just watched being made.

Grants
    `GRANTS` maps each agent identity to its tools. Today there is one agent
    and it holds every tool. Day 8's specialists each get only their own, so
    a fooled specialist cannot reach another's tools even if its model asks.
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from langchain_core.tools import BaseTool, StructuredTool, ToolException

from findyfixy.tools.changes import CHANGES_TOOL
from findyfixy.tools.deploys import DEPLOYS_TOOL
from findyfixy.tools.memory import SEARCH_TOOL
from findyfixy.tools.services import SERVICES_TOOL
from findyfixy.tools.submit import SUBMIT_TOOL
from wyld3rness.identity.broker import CredentialBroker, TokenRefused

AGENT = "agent"  # the single agent's identity; Day 8 adds the coordinator's and each specialist's
TOOL_STEP = "tool-step"  # the Day 2 tool step the console still offers: one model, one tool

GRANTS: Mapping[str, frozenset[str]] = {
    AGENT: frozenset({SEARCH_TOOL, DEPLOYS_TOOL, CHANGES_TOOL, SERVICES_TOOL, SUBMIT_TOOL}),
    TOOL_STEP: frozenset({DEPLOYS_TOOL}),
}


def resource_of(tool: str, args: Mapping[str, Any]) -> str:
    """The resource a call reads, from its arguments: a service's changes or map entry, the incidents, the decision.

    Pure, and the same on both sides, so the token's scope is exactly what the
    tool is about to read.
    """
    service = args.get("service")
    if tool in (DEPLOYS_TOOL, CHANGES_TOOL):
        return f"changes:{service}"
    if tool == SERVICES_TOOL:
        return f"services:{service}"
    if tool == SEARCH_TOOL:
        return "incidents"
    if tool == SUBMIT_TOOL:
        return "decision"
    return f"{tool}:unknown"  # an unmapped tool still gets a scope; it holds no grant, so no token is issued


class CredentialRefused(ToolException):
    """A call refused for its credential. The one tool error the guard returns to the agent instead of raising."""


def _refusals_only(err: ToolException) -> str:
    """Return a credential refusal to the agent as text; re-raise any other tool error so an outage stops the run."""
    if isinstance(err, CredentialRefused):
        return str(err)
    raise err


def guard(tool: BaseTool, broker: CredentialBroker, agent: str) -> BaseTool:
    """`tool` with a token issued for `agent` and verified before every call. Same name, schema and description.

    Raises ValueError at wiring time when `agent` holds no grant for the tool:
    a forgotten grant would otherwise refuse every call, quietly, at run time.
    Sync and async tools alike (an MCP tool is async only).
    """
    if not broker.granted(agent, tool.name):
        raise ValueError(f"{agent} holds no grant for {tool.name}: add it to GRANTS or do not give the tool")

    def token_for(args: dict[str, Any]) -> str:
        """The agent's half: a token for this call, verified by the tool's half before anything runs."""
        resource = resource_of(tool.name, args)
        try:
            token = broker.issue(agent, tool.name, resource)
            broker.verify(token, agent=agent, tool=tool.name, resource=resource)
        except TokenRefused as err:
            raise CredentialRefused(f"credential refused: {err}") from err
        return token

    def run(**args: Any) -> Any:
        """Obtain and verify a token, then run the real body."""
        token_for(args)
        return tool.invoke(args)

    async def arun(**args: Any) -> Any:
        """The same, for a tool whose body is async (an MCP tool)."""
        token_for(args)
        return await tool.ainvoke(args)

    return StructuredTool.from_function(
        func=run,
        coroutine=arun,
        name=tool.name,
        description=tool.description,
        args_schema=tool.args_schema,
        handle_tool_error=_refusals_only,
    )
