"""Day 7: incident memory as a tool. The agent searches past incidents when it decides to.

What it does
    `make_search_tool(recall)` wraps a recall function (question in, similar
    past incidents out) as the `search_incidents` tool. The result is the same
    text the fixed path puts in its prompt (`format_prior`): each incident's
    id, service, severity, summary, cause and fix, each line capped.

Why recall becomes a tool
    On the fixed path, recall runs once, on the alert text, before the model
    sees anything. The agent can instead search with its own words, search
    again after a deploy or a dependency points somewhere else (the upstream
    service in a cascade), or not search at all. Same retriever, different
    control: the model chooses the query and when to stop.

Why the recall is injected
    The eval passes a recall bound to one case with that case's own incident
    held out (findyfixy/evals/adapter.py), so the tool can never hand the
    model the answer. The console passes the plain shared recall. The tool
    does not know which, and does not need to.

Failure modes
    - An empty or over-long query is refused by the argument type, and the tool
      node returns the error to the model.
    - A database fault propagates, like the fixed path's recall: retrying here
      would hide an outage.
"""

from collections.abc import Callable, Sequence

from langchain_core.tools import StructuredTool
from pydantic import BaseModel, Field

from findyfixy.triage.record import Prior, format_prior

# Bounds the query the model writes: long enough for an alert line plus a
# hypothesis, short enough that a runaway reply cannot become the search.
MAX_QUERY_CHARS = 300

# What a search returns when the archive has nothing. Explicit text, so the
# model reads "nothing similar" rather than an empty string it may ignore.
NO_MATCH = "no similar past incidents"


class SearchArgs(BaseModel):
    """The tool's argument contract. Also the schema the model sees."""

    query: str = Field(min_length=3, max_length=MAX_QUERY_CHARS, description="what to look for in past incidents")


SEARCH_TOOL = "search_incidents"


def make_search_tool(recall: Callable[[str], Sequence[Prior]]) -> StructuredTool:
    """Wrap `recall` as the `search_incidents` tool."""

    def run(query: str) -> str:
        """Validate the query, search, and say so plainly when nothing matches.

        Validated here as well as by the tool node, so a direct call is as
        safe as one from the model.
        """
        args = SearchArgs.model_validate({"query": query})
        return format_prior(recall(args.query)) or NO_MATCH

    return StructuredTool.from_function(
        func=run,
        name=SEARCH_TOOL,
        description="Search past incident postmortems. Returns the closest ones with severity, cause and fix.",
        args_schema=SearchArgs,
    )
