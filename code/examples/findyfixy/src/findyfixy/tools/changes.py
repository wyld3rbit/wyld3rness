"""Day 7: the change-log tool. Every kind of change to a service, measured back from the alert.

What it does
    `get_recent_changes(service, hours, now=...)` returns the changes to one
    service in the `hours` before `now`, newest first: deploys, config pushes,
    feature flags and key rotations. `make_changes_tool` wraps it as a tool the
    agent can call. It replaces the deploy tool (tools/deploys.py) from data v3 on.

Why a change log, not a deploy log
    Most outages follow a change, and not only a deploy: config pushes, flags
    and key rotations break systems too. A deploy log cannot see a key rotation, so
    a rollback rule that reads it approves a rollback that cannot help. The
    poisoned case h23 is that shape: auth deployed, then a key rotation broke
    logins. With every change kind in one log, "the deploy is the only change
    in the window" becomes a fact the action ledger checks (findyfixy/policy.py).

Why `now` is injected
    v1 and v2 stored "hours ago" against one shared moment, so every case saw
    the same log, and h23 and h26 could not be told apart. Here each change has
    an absolute time and the caller passes the alert's time as `now`. That is
    how a real change feed works: changes are timestamped once, and each
    incident looks back from its own start. Changes after `now` are never
    returned: a case must not see the future.

Failure modes
    - Bad arguments fail the argument schema; `make_tools_node`
      (tools/deploys.py) returns that to the model as text it can correct.
    - A malformed log raises a validation error. That is an operator fault,
      not something the model can fix, so it is not caught.
    - Changes nobody recorded are invisible. A manual hotfix or a rotation done
      by hand is the known blind spot of every change feed.

At scale
    The log is a query against a change-event service, filtered by service and
    time on the server, with a timeout. Here it is a small file read per call.
"""

from collections.abc import Sequence
from datetime import datetime, timedelta
import json
from pathlib import Path
from typing import Literal, Self

from langchain_core.tools import StructuredTool
from pydantic import AwareDatetime, BaseModel, ConfigDict, Field, TypeAdapter, model_validator

from findyfixy.paths import DataVersion, data_dir
from findyfixy.tools.deploys import MAX_HOURS, Service

ChangeKind = Literal["deploy", "config", "flag", "key_rotation"]

CHANGES_TOOL = "get_recent_changes"


class Change(BaseModel):
    """One recorded change to one service.

    `ref` names what changed: the version for a deploy, the key, flag or
    config set otherwise. `rollback_safe` is set on deploys only: True when the
    previous version can run on the data this one wrote (no one-way schema or
    format change). The action ledger checks it before a rollback, so a
    deploy without it, or another kind with it, is refused at load time.
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    at: AwareDatetime
    service: Service
    kind: ChangeKind
    ref: str = Field(min_length=1)
    author: str = Field(min_length=1)
    summary: str = Field(min_length=1)
    rollback_safe: bool | None = None

    @model_validator(mode="after")
    def _rollback_safe_on_deploys_only(self) -> Self:
        """A deploy must say whether it is safe to roll back; nothing else may."""
        if (self.kind == "deploy") != (self.rollback_safe is not None):
            raise ValueError("rollback_safe is required on a deploy and not allowed on any other change")
        return self


_LOG = TypeAdapter(list[Change])


def changes_path(version: DataVersion) -> Path:
    """The change log of one data version (v3 on)."""
    return data_dir(version) / "changes.json"


def load_changes(path: Path) -> list[Change]:
    """Read and validate a change log. Raises on a malformed file (an operator fault)."""
    return _LOG.validate_json(path.read_bytes())


def changes_before(changes: Sequence[Change], service: Service, now: datetime, hours: float) -> list[Change]:
    """The changes to `service` in the `hours` before `now`, newest first. Pure.

    The window is [now - hours, now], both ends included: a deploy exactly two
    hours before the alert is "within two hours", as v2 counted it. A change
    after `now` never counts. The action ledger uses this same function, so the
    agent and the policy cannot read different windows.
    """
    start = now - timedelta(hours=hours)
    hits = [c for c in changes if c.service == service and start <= c.at <= now]
    return sorted(hits, key=lambda c: c.at, reverse=True)


def minutes_before(change: Change, now: datetime) -> int:
    """How many whole minutes before `now` the change happened. The one measure the agent and the ledger share."""
    return int((now - change.at).total_seconds() // 60)


class ChangesArgs(BaseModel):
    """The tool's argument contract. Also the schema the model sees."""

    service: Service = Field(description="service whose changes to list")
    hours: int = Field(ge=1, le=MAX_HOURS, description="look-back window in hours (1 to 168)")


def get_recent_changes(service: str, hours: int, *, changes: Sequence[Change], now: datetime) -> str:
    """Return JSON text: this service's changes in the `hours` before `now`, newest first.

    Each change is shown with `minutes_ago` (relative to the alert) instead of
    its absolute time, so the model never has to do date arithmetic. Arguments
    are validated here too, so a direct call is as safe as one from a model.
    """
    args = ChangesArgs.model_validate({"service": service, "hours": hours})
    shown = []
    for change in changes_before(changes, args.service, now, args.hours):
        record = change.model_dump(mode="json", exclude={"at", "service"}, exclude_none=True)
        record["minutes_ago"] = minutes_before(change, now)
        shown.append(record)
    return json.dumps(shown)


def make_changes_tool(changes: Sequence[Change], now: datetime) -> StructuredTool:
    """Wrap `get_recent_changes` as a tool, bound to one loaded log and one alert time.

    The caller loads the log once (`load_changes`) and hands the same list to
    the action ledger, so the agent and the ledger read the same facts, and a
    malformed file fails when the agent is built, not halfway through a case.
    """

    def run(service: str, hours: int) -> str:
        """The tool body: this service's recent changes, as of the alert."""
        return get_recent_changes(service, hours, changes=changes, now=now)

    return StructuredTool.from_function(
        func=run,
        name=CHANGES_TOOL,
        description="List recent changes to one service (deploys, config, flags, key rotations), newest first.",
        args_schema=ChangesArgs,
    )
