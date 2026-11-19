"""Day 7: run FindyFixy on a hard set, golden v2 or v3. Scores only, never the gate.

What it does
    The same live wiring as findyfixy/evals/answers.py (the same graph,
    models, judge and tracing), with two differences: the cases come from the
    hard set's golden file, and recall reads that version's table
    (`chunks_v2`, `chunks_v3`), which holds v1's corpus plus the poisoned and
    cascade incidents.

The two hard sets
    v2  the 30 hard cases, with a deploy log against one shared moment.
    v3  the same cases on a real timeline: each case has its alert time and
        the agent reads a change log as of that time (findyfixy/evals/golden_v3.py).

Why a separate entry point
    The v1 gate's attestation fingerprints the import closure of answers.py.
    This module imports answers.py; answers.py never imports this one. So the
    hard sets' cases, data and builders stay outside the v1 attestation, and
    editing a hard case cannot void it, which is right: a hard case cannot
    change a v1 answer. The agent's tools are inside it, including the change
    tool only v3 reads, because the v1 agent is built by the same code.

Why no gate
    v1 is the regression check, with its baseline and attestation. A hard-set
    run is a measurement. Compared with v1's baseline it would report every
    score change as a regression, so `--gate`, `--write-baseline` and
    `--baseline-from` are refused, in any abbreviation argparse would accept.

Failure model
    A stale or missing table stops the run before any model loads (the
    session's freshness check), with "run make ingest".
"""

from __future__ import annotations

from collections.abc import Callable, Sequence
from contextlib import AbstractContextManager
from functools import partial
from pathlib import Path
import sys
from typing import TYPE_CHECKING, Literal

from findyfixy.evals.answers import (
    GATE_FLAGS,
    LEGACY_CONTROL,
    NO_RECALL_HELP,
    asks_for,
    open_live_session,
    take_fixed_flag,
)
from findyfixy.evals.golden import GoldenCase
from findyfixy.evals.golden_v2 import golden_v2_path, load_golden_v2
from findyfixy.evals.golden_v3 import golden_v3_path, load_golden_v3
from findyfixy.events import Emit, no_emit
from findyfixy.paths import EVALS, REPORTS, ROOT
from wyld3rness.eval import cli
from wyld3rness.eval.cli import Session, SessionFactory

if TYPE_CHECKING:
    from findyfixy.wiring import SharedRetrieval
    from wyld3rness.trace.tracing import Tracing

HardVersion = Literal["v2", "v3"]

# Each hard set's golden file and its reader. The one table of hard sets.
HARD_SETS: dict[HardVersion, tuple[Callable[[], Path], Callable[[], Sequence[GoldenCase]]]] = {
    "v2": (golden_v2_path, load_golden_v2),
    "v3": (golden_v3_path, load_golden_v3),
}


def golden_path(version: HardVersion) -> Path:
    """Where one hard set's cases live."""
    return HARD_SETS[version][0]()


def load_hard(version: HardVersion) -> list[GoldenCase]:
    """Read one hard set's cases."""
    return list(HARD_SETS[version][1]())


def open_hard_session(
    version: HardVersion,
    negative_control: bool,
    *,
    emit: Emit = no_emit,
    retrieval: SharedRetrieval | None = None,
    tracing: Tracing | None = None,
    agent: bool = True,
) -> AbstractContextManager[Session]:
    """The live session over one hard set: its cases and its table. The one place a hard session is wired.

    The command line and the console both use it. The cases are read here, as
    the session opens, so a run always scores the file whose hash its report
    records. A shared `retrieval` (the console's) is used through its sibling
    for `version`, so no second copy of the models loads. `agent` (the
    default) runs the agent with the version's deploy or change log; False runs
    the earlier fixed path.
    """
    return open_live_session(
        negative_control,
        emit=emit,
        retrieval=retrieval.with_version(version) if retrieval is not None else None,
        tracing=tracing,
        version=version,
        goldens=load_hard(version),
        agent=agent,
    )


def main(
    argv: Sequence[str] | None = None,
    *,
    version: HardVersion = "v2",
    open_session: SessionFactory | None = None,
    reports_dir: Path = REPORTS,
) -> int:
    """Run the harness command line on one hard set. Returns the exit code.

    `open_session` is injected in tests; the default is the live session bound
    to the version's cases and table. No attestation is ever written.
    """
    prog = f"findyfixy-eval-{version}"
    fixed, args = take_fixed_flag(list(argv) if argv is not None else sys.argv[1:])
    if asks_for(args, GATE_FLAGS):
        print(f"{prog}: the gate and the baseline cover v1 only; run {version} without them", file=sys.stderr)
        return 2
    live = partial(open_hard_session, version)
    session = open_session or (partial(live, agent=False) if fixed else live)
    return cli.main(
        args,
        prog=prog,
        run=cli.live_runner(session, golden_path=golden_path(version), repo_dir=ROOT),
        reports_dir=reports_dir,
        baseline_path=EVALS / "baseline.json",  # read by --compare only; the gate is refused above
        display_root=ROOT,
        control_flag="--no-recall",
        control_help=NO_RECALL_HELP,
        legacy_control=LEGACY_CONTROL,
    )


def main_v3(argv: Sequence[str] | None = None) -> int:
    """The `findyfixy-eval-v3` command: `main` on golden v3."""
    return main(argv, version="v3")


if __name__ == "__main__":
    sys.exit(main())
