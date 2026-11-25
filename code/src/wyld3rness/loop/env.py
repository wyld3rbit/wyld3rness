"""The environment port: what the loop may see and do in one system, and how it is judged.

    loop ──observe()──────► text the decider reads (metrics, settings)
         ──apply(action)──► Applied: carried out or not, and its undo (or None: no undo)
         ──series()───────► the user stream, per second, the verdict reads
         ──now(), wait(s)─► the incident's clock

An app implements this for the system it operates: FindyFixy's over simprod is
`code/examples/findyfixy/src/findyfixy/oncall/env.py`. The harness never
imports it.

Why the undo comes back from `apply` and is not looked up later: the right
undo depends on the state before the action (backoff on is undone by backoff
off only if it was off), and only the moment of applying sees that state. An
action whose undo cannot be known is reported with `undo=None`, the same as an
action that has none, so the loop never retries after it.

Why `series` is on the port and not computed by the decider: the measurement
must come from a process the agent cannot write (`verdict.py`). In simprod it
is a scraper in the host process that drives the storm.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from typing import Protocol

from wyld3rness.loop.verdict import Second


@dataclass(frozen=True)
class Action:
    """One typed action: its name and arguments, as the environment defines them."""

    name: str
    args: Mapping[str, object] = field(default_factory=dict)

    def label(self) -> str:
        """`name(k=v, ...)`, for logs and prompts."""
        return f"{self.name}({', '.join(f'{k}={v}' for k, v in self.args.items())})"


@dataclass(frozen=True)
class Applied:
    """What became of one action: carried out (`ok`), the environment's answer, and its undo.

    `ok` False means the action may or may not have happened (refused, timed
    out): the loop treats it as a failure that ends in the safe state.
    """

    action: Action
    ok: bool
    detail: str
    undo: Action | None


class Environment(Protocol):
    """One system the loop operates. Every method may raise; the loop turns that into the safe state."""

    def observe(self) -> str:
        """What the decider may read now, as text."""
        ...

    def apply(self, action: Action) -> Applied:
        """Carry out one action and report it, with the action that undoes it."""
        ...

    def series(self) -> Sequence[Second]:
        """Every second of the user stream measured so far, oldest first, on the `now()` clock."""
        ...

    def now(self) -> float:
        """Seconds since the incident's clock started."""
        ...

    def wait(self, seconds: float) -> None:
        """Let `seconds` pass. Real time in production, a step of the clock in a fake."""
        ...
