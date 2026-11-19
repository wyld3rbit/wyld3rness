"""Day 7: the agent's way to finish. It submits its decision as a tool call, validated like any tool argument.

What it does
    `make_submit_tool()` is the `submit_decision` tool. Its argument schema is
    the `Decision` record itself, so the tool node validates a submission the
    way it validates any tool call: an invalid one (a rollback with no
    version, an injected flag in the version, an unknown action) comes back
    to the model as an error it can correct within its turn budget. A valid
    one ends the loop (findyfixy/tools/investigate.py, `finish_tool`).

Why a tool, and not a separate decision call after the loop
    A separate call costs one more full model request per run, on a path
    where a sev1 has seconds. Submitting through a tool folds the decision
    into the loop's last turn. Not every model calls a submit tool, so the
    tool is the fast path, and the separate validated decision call stays as
    the fallback for a model that ends with plain text instead.

The tool executes nothing
    It records the proposal and returns "submitted". What may act on it is
    the action ledger's decision, never the model's.
"""

from langchain_core.tools import StructuredTool

from findyfixy.triage.record import Decision

SUBMIT_TOOL = "submit_decision"


def make_submit_tool() -> StructuredTool:
    """The `submit_decision` tool, its arguments validated as a `Decision`."""

    def run(**decision: object) -> str:
        """Acknowledge a submission. The arguments are ignored on purpose.

        By the time this runs, the tool node has validated them as a
        `Decision`. The loop reads the decision from the tool call itself
        (findyfixy/tools/investigate.py), so returning it here would only
        repeat it into the model's context. Executing nothing is the point.
        """
        return "submitted"

    return StructuredTool.from_function(
        func=run,
        name=SUBMIT_TOOL,
        description=(
            "Finish the investigation: submit your decision once. action is read, page_human or rollback; "
            "rollback_version is the version to roll back to, only for rollback."
        ),
        args_schema=Decision,
    )
