"""Day 7: the service-map tool. Which services can break this one, and which does it break.

What it does
    `get_dependencies(service)` returns the service's upstreams (what it calls,
    or where its work comes from) and its downstreams (who calls it), from the
    synthetic map in `data/services.json`. `make_services_tool` wraps it as a
    tool the agent can call.

Why the agent needs it
    In a cascade the alert fires where the pain shows, not where the fault is.
    On golden v2 every cascade case blamed the alerting service (search slow,
    when auth's key cache had expired). Following the map upstream is how an
    on-call engineer finds the real cause, and the agent needs the same fact.
    A model cannot infer a company's topology from an alert; it has to read it.

Why the map is not versioned
    It describes the system, not a data set. v1 and v2 are two sets of
    incidents about the same eight services, so they share one map. A test
    checks that every v2 cascade's cause sits upstream of the service that
    alerted, so the map and the cases cannot drift apart.

Failure modes
    - An unknown service is refused by the argument type (`Service`), and the
      tool node returns the error to the model, as for the deploy tool.
    - A map that names a service outside `Service`, or misses one, raises when
      the file is read: an operator fault, not something the model can fix.
"""

from functools import cache
import json
from pathlib import Path
from typing import get_args

from langchain_core.tools import StructuredTool
from pydantic import BaseModel, Field

from findyfixy.paths import DATA
from findyfixy.tools.deploys import Service

DEFAULT_SERVICES_PATH = DATA / "services.json"


SERVICES_TOOL = "get_dependencies"


class DependenciesArgs(BaseModel):
    """The tool's argument contract. Also the schema the model sees."""

    service: Service = Field(description="service whose dependencies to list")


@cache
def load_map(path: Path) -> dict[str, tuple[str, ...]]:
    """Read and check the map: every service exactly once, every upstream a known service.

    Cached per path: the file is small and fixed for the life of the process.
    Keys starting with `_` are comments.
    """
    raw = json.loads(path.read_text(encoding="utf-8"))
    entries = {k: v for k, v in raw.items() if not k.startswith("_")}
    known = set(get_args(Service))
    if set(entries) != known:
        missing, unknown = known - set(entries), set(entries) - known
        raise ValueError(f"{path.name} must list every service once: missing {missing}, unknown {unknown}")
    for name, upstream in entries.items():
        if not set(upstream) <= known:
            raise ValueError(f"{path.name}: {name} has unknown upstream {set(upstream) - known}")
    return {name: tuple(upstream) for name, upstream in entries.items()}


def get_dependencies(service: str, *, path: Path) -> str:
    """JSON text: the service, its upstreams and its downstreams, each sorted.

    Validated here too, so a direct call is as safe as one from the model.
    """
    args = DependenciesArgs.model_validate({"service": service})
    graph = load_map(path)
    downstream = sorted(name for name, upstream in graph.items() if args.service in upstream)
    return json.dumps({"service": args.service, "upstream": sorted(graph[args.service]), "downstream": downstream})


def make_services_tool(path: Path = DEFAULT_SERVICES_PATH) -> StructuredTool:
    """Wrap `get_dependencies` as a tool. `path` is injected for tests."""

    def run(service: str) -> str:
        """Look up one service in the map at `path`, as JSON for the model."""
        return get_dependencies(service, path=path)

    return StructuredTool.from_function(
        func=run,
        name=SERVICES_TOOL,
        description="List what one service depends on (upstream) and what depends on it (downstream).",
        args_schema=DependenciesArgs,
    )
