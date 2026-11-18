"""Day 7: run FindyFixy on golden v2, the hard cases. Scores only, never the gate.

What it does
    The same live wiring as findyfixy/evals/answers.py (the same graph,
    models, judge and tracing), with two differences: the cases come from
    data/v2/golden.jsonl, and recall reads the v2 table (`chunks_v2`), which
    holds v1's corpus plus the poisoned and cascade incidents.

Why a separate entry point
    The v1 gate's attestation fingerprints the import closure of answers.py.
    This module imports answers.py; answers.py never imports this one. So the
    v2 code and data stay outside the v1 attestation, and editing a hard case
    cannot void it, which is right: a hard case cannot change a v1 answer.

Why no gate
    v1 is the regression check, with its baseline and attestation. A v2 run is
    a measurement. Compared with v1's baseline it would report every score
    change as a regression, so `--gate`, `--write-baseline` and
    `--baseline-from` are refused, in any abbreviation argparse would accept.

Failure model
    A stale or missing v2 table stops the run before any model loads (the
    session's freshness check), with "run make ingest".
"""

from __future__ import annotations

from collections.abc import Sequence
from contextlib import AbstractContextManager
from functools import partial
from pathlib import Path
import sys
from typing import TYPE_CHECKING

from findyfixy.evals.answers import (
    GATE_FLAGS,
    LEGACY_CONTROL,
    NO_RECALL_HELP,
    asks_for,
    open_live_session,
    take_fixed_flag,
)
from findyfixy.evals.golden_v2 import golden_v2_path, load_golden_v2
from findyfixy.events import Emit, no_emit
from findyfixy.paths import EVALS, REPORTS, ROOT
from wyld3rness.eval import cli
from wyld3rness.eval.cli import Session, SessionFactory

if TYPE_CHECKING:
    from findyfixy.wiring import SharedRetrieval
    from wyld3rness.trace.tracing import Tracing

PROG = "findyfixy-eval-v2"


def open_v2_session(
    negative_control: bool,
    *,
    emit: Emit = no_emit,
    retrieval: SharedRetrieval | None = None,
    tracing: Tracing | None = None,
    agent: bool = True,
) -> AbstractContextManager[Session]:
    """The live session over v2: its cases and its table. The one place a v2 session is wired.

    The command line and the console both use it. The cases are read here, as
    the session opens, so a run always scores the file whose hash its report
    records. A shared `retrieval` (the console's) is used through its v2
    sibling, so no second copy of the models loads. `agent` (the default) runs
    the agent with v2's deploy log; False runs the earlier fixed path.
    """
    return open_live_session(
        negative_control,
        emit=emit,
        retrieval=retrieval.with_version("v2") if retrieval is not None else None,
        tracing=tracing,
        version="v2",
        goldens=load_golden_v2(),
        agent=agent,
    )


def main(
    argv: Sequence[str] | None = None,
    *,
    open_session: SessionFactory | None = None,
    reports_dir: Path = REPORTS,
) -> int:
    """Run the harness command line on golden v2. Returns the exit code.

    `open_session` is injected in tests; the default is the live session bound
    to v2's cases and table. No attestation is ever written.
    """
    fixed, args = take_fixed_flag(list(argv) if argv is not None else sys.argv[1:])
    if asks_for(args, GATE_FLAGS):
        print(f"{PROG}: the gate and the baseline cover v1 only; run v2 without them", file=sys.stderr)
        return 2
    session = open_session or (partial(open_v2_session, agent=False) if fixed else open_v2_session)
    return cli.main(
        args,
        prog=PROG,
        run=cli.live_runner(session, golden_path=golden_v2_path(), repo_dir=ROOT),
        reports_dir=reports_dir,
        baseline_path=EVALS / "baseline.json",  # read by --compare only; the gate is refused above
        display_root=ROOT,
        control_flag="--no-recall",
        control_help=NO_RECALL_HELP,
        legacy_control=LEGACY_CONTROL,
    )


if __name__ == "__main__":
    sys.exit(main())
