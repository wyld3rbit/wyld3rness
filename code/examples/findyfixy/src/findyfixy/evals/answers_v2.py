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
from pathlib import Path
import sys
from typing import TYPE_CHECKING

from findyfixy.evals.answers import LEGACY_CONTROL, NO_RECALL_HELP, open_live_session
from findyfixy.evals.golden_v2 import golden_v2_path, load_golden_v2
from findyfixy.events import Emit, no_emit
from findyfixy.paths import EVALS, REPORTS, ROOT
from wyld3rness.eval import cli
from wyld3rness.eval.cli import Session, SessionFactory

if TYPE_CHECKING:
    from findyfixy.wiring import SharedRetrieval
    from wyld3rness.trace.tracing import Tracing

PROG = "findyfixy-eval-v2"
# The flags that belong to the v1 gate alone.
V1_ONLY = ("--gate", "--write-baseline", "--baseline-from")


def asks_for_v1_only(args: Sequence[str]) -> bool:
    """True when an argument is, or abbreviates, one of `V1_ONLY`. Pure.

    argparse accepts any unambiguous prefix (`--ga` for `--gate`), so an exact
    match would let an abbreviation through. `--flag=value` counts too.
    """
    for arg in args:
        name = arg.split("=", 1)[0]
        if name.startswith("--") and len(name) > 2 and any(flag.startswith(name) for flag in V1_ONLY):
            return True
    return False


def open_v2_session(
    negative_control: bool,
    *,
    emit: Emit = no_emit,
    retrieval: SharedRetrieval | None = None,
    tracing: Tracing | None = None,
) -> AbstractContextManager[Session]:
    """The live session over v2: its cases and its table. The one place a v2 session is wired.

    The command line and the console both use it. The cases are read here, as
    the session opens, so a run always scores the file whose hash its report
    records. A shared `retrieval` (the console's) is used through its v2
    sibling, so no second copy of the models loads.
    """
    return open_live_session(
        negative_control,
        emit=emit,
        retrieval=retrieval.with_version("v2") if retrieval is not None else None,
        tracing=tracing,
        version="v2",
        goldens=load_golden_v2(),
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
    args = list(argv) if argv is not None else sys.argv[1:]
    if asks_for_v1_only(args):
        print(f"{PROG}: the gate and the baseline cover v1 only; run v2 without them", file=sys.stderr)
        return 2
    session = open_session or open_v2_session
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
