"""Golden v3 (Day 7): the v2 hard cases on a real timeline, with a change log instead of a deploy log.

Why v3 exists
    v1 and v2 store each deploy as "hours ago" against one shared moment, so
    every case sees the same log. h23 (auth logins fail minutes after a key
    rotation; the right action is to page) and h26 (auth logins fail after a
    bad deploy; the right action is to roll back) then look identical to any
    rule: same service, same deploy an hour before. The rollback policy (step
    3) needs to tell them apart, so v3 gives each case its own clock and records
    every kind of change, not only deploys. v2 stays as it was.

What changes from v2
    - Each case carries `alert_at`, an absolute UTC time. Case hNN happens on
      2026-10-NN at the time its alert names.
    - data/v3/changes.json replaces deploys.json. Every v2 deploy becomes a
      change at an absolute time: its v2 "hours ago" measured back from the
      rollback case of its own service (h26 to h30), so in its 2-hour window
      each rollback case sees exactly the deploy it saw on v2. Deploys carry
      `rollback_safe`.
    - h23 gets the two changes its story names: an auth deploy at 21:00 and the
      key rotation at 21:55, both before its 22:03 alert.
    - The corpus is v2's, rebuilt by the same function, in its own table.

What does not change, and what does
    The questions, references, groups and expected actions are v2's, byte for
    byte. Three things differ, so a v2 to v3 score difference can come from
    any of them: the clock (each case looks back from its own alert), the log
    (every change kind, and only changes before the alert), and the tool's
    shape (it is named get_recent_changes, names a version `ref`, and gives
    `minutes_ago` instead of `hours_ago`).

    A case still sees history longer windows reach: h26 looking back three
    days or more sees h23's deploy and key rotation, as a real feed would show
    an earlier incident's changes. The rollback policy reads 2 hours.

Side effect worth knowing
    On v2 every case also saw the deploys made "an hour ago" for other cases,
    so a case with no deploy in its story could still see one (a decoy, by
    accident). On v3 a case sees only changes before its own alert. Fewer
    accidental decoys can lower the number of unneeded rollbacks; that is a
    data change, not an agent change, and is reported as one.
"""

from __future__ import annotations

from collections.abc import Sequence
from datetime import UTC, datetime, timedelta
import json
from pathlib import Path
import re
import sys

from pydantic import AwareDatetime

from findyfixy.evals.golden import read_incidents
from findyfixy.evals.golden_v2 import HardCase, build_golden_v2, write_cases
from findyfixy.paths import data_dir
from findyfixy.retrieval.corpus import write_jsonl
from findyfixy.retrieval.corpus_v2 import build_corpus_v2
from findyfixy.tools.changes import Change

# The month the cases happen in: case hNN on day NN.
_YEAR, _MONTH = 2026, 10

# The rollback case of each service that has deploys. A v2 deploy's "hours ago"
# is measured back from this case's alert, which keeps that case's view as on v2.
_ANCHOR = {
    "auth": "h26",
    "billing": "h27",
    "gateway": "h28",
    "notifications": "h29",
    "voice-verification": "h30",
}

_CLOCK = re.compile(r"\b([01]\d|2[0-3]):([0-5]\d)\b")


class TimedCase(HardCase):
    """A v3 case: a v2 case plus the moment its alert fired. Tools look back from `alert_at`."""

    alert_at: AwareDatetime


def alert_time(case: HardCase) -> datetime:
    """When the case's alert fired: day NN of the month for case hNN, at the first HH:MM in its text. Pure.

    Every v2 question names its time first: "13:05 Alert: ...", or a pager
    wrapper whose first time is the alert's. Raises if a case names none, so a
    new case cannot silently land at midnight.
    """
    match = _CLOCK.search(case.question)
    if match is None:
        raise ValueError(f"{case.id} names no HH:MM time")
    day = int(case.id[1:])
    return datetime(_YEAR, _MONTH, day, int(match[1]), int(match[2]), tzinfo=UTC)


def build_golden_v3(v2: Sequence[HardCase]) -> list[TimedCase]:
    """The v2 cases, each with its alert time. Pure."""
    return [TimedCase(**case.model_dump(), alert_at=alert_time(case)) for case in v2]


def _h23_changes(alert: datetime) -> list[Change]:
    """The changes h23's story names: an auth deploy, then the key rotation that broke logins.

    The deploy is safe to roll back, so every fact a rollback rule can check
    about the deploy holds. Only the later rotation shows the rollback is the
    wrong move: the deploy is not the only change in the window.
    """
    day = alert.replace(hour=0, minute=0)
    return [
        Change(
            at=day.replace(hour=21, minute=0),
            service="auth",
            kind="deploy",
            ref="2.13.5",
            author="team-identity",
            summary="Log token issuer on each login",
            rollback_safe=True,
        ),
        Change(
            at=day.replace(hour=21, minute=55),
            service="auth",
            kind="key_rotation",
            ref="token-signing-key",
            author="team-identity",
            summary="Rotate the token signing key and retire the old key",
        ),
    ]


def build_changes_v3(deploys_v2: Sequence[dict[str, object]], cases: Sequence[TimedCase]) -> list[Change]:
    """v2's deploys at absolute times, plus h23's changes, oldest first. Pure.

    Every v2 deploy is safe to roll back: none of them changes stored data in
    a way the previous version cannot read.
    """
    alerts = {c.id: c.alert_at for c in cases}
    changes = [
        Change(
            at=alerts[_ANCHOR[str(d["service"])]] - timedelta(hours=float(str(d["hours_ago"]))),
            service=str(d["service"]),  # type: ignore[arg-type]  # validated by Change
            kind="deploy",
            ref=str(d["version"]),
            author=str(d["author"]),
            summary=str(d["summary"]),
            rollback_safe=True,
        )
        for d in deploys_v2
    ]
    changes += _h23_changes(alerts["h23"])
    return sorted(changes, key=lambda c: (c.at, c.service))


def write_changes(changes: Sequence[Change], path: Path) -> None:
    """The change log: one record per line inside a JSON array, stable bytes."""
    rows = ["  " + json.dumps(c.model_dump(mode="json", exclude_none=True)) for c in changes]
    path.write_text("[\n" + ",\n".join(rows) + "\n]\n", encoding="utf-8", newline="\n")


def golden_v3_path() -> Path:
    """Where the v3 golden set lives."""
    return data_dir("v3") / "golden.jsonl"


def load_golden_v3(path: Path | None = None) -> list[TimedCase]:
    """Read the v3 cases back (data/v3/golden.jsonl unless `path` is given)."""
    with (path or golden_v3_path()).open(encoding="utf-8") as handle:
        return [TimedCase.model_validate_json(line) for line in handle if line.strip()]


def main(argv: Sequence[str] | None = None) -> None:
    """Write data/v3: the corpus, the golden set and the change log, from v1 plus the v2 and v3 additions."""
    if list(argv if argv is not None else sys.argv[1:]):
        raise SystemExit("usage: findyfixy-data-v3")
    v1 = read_incidents(data_dir("v1") / "incidents.jsonl")
    data_dir("v3").mkdir(exist_ok=True)
    corpus = build_corpus_v2(v1)
    write_jsonl(corpus, data_dir("v3") / "incidents.jsonl")
    cases = build_golden_v3(build_golden_v2(v1))
    write_cases(cases, golden_v3_path())
    deploys = json.loads((data_dir("v2") / "deploys.json").read_text(encoding="utf-8"))
    changes = build_changes_v3(deploys, cases)
    write_changes(changes, data_dir("v3") / "changes.json")
    print(f"wrote data/v3: {len(corpus)} incidents, {len(cases)} golden cases, {len(changes)} changes")


if __name__ == "__main__":
    main()
