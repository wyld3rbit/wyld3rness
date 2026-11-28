"""Reuse a judge score when everything the judge would read is identical to an attested run.

What it does
    A gate run answers every case, then asks a judge model for four scores per
    case, which is most of a run's time. Most commits change nothing the judge
    reads: repeated answer passes on one commit are nearly always identical
    (research log, entry 001). This module lets a gate run copy those cases'
    scores from the last attested run and judge only the rest.

        judge_code_digest() -> str             the judge's own loaded source, hashed
        judge_identity(...) -> str             who judges, and how
        judge_key(identity, case, answer)      everything one case's scores depend on
        load_prior(attestation, reports_dir)   the attested run's keys and scores

Why the key holds what it holds
    The four metrics read the question, the reference, the recalled contexts,
    the answer text and the proposed action (runner._judge). The score also
    depends on who judges: the judge model and its digest, the prompt versions,
    the judge's code, and what the app adds (FindyFixy: the embedder used by
    response relevancy, and the lock file that pins the libraries). All of it
    goes into one SHA-256. Change any part and the key changes, so the case is
    judged again. A change to the judge itself changes every key, so it
    forces a full run with no special case.

Trust model
    - Only the attested run is a source. Its report is found by the name in
      the committed attestation and must match the hash recorded there, so an
      edited or swapped report is not reused.
    - A metric that failed in that run (a judge timeout, a parse error) is not
      reused: the failure may have been transient, and judging again is cheap.
      The one exception is action quality with no proposed action: the metric
      refuses an empty action, so that failure is set by the inputs.
    - The judge is close to repeatable, not guaranteed (temperature 0 on a
      local server). A reused score names the run that judged it
      (`scores_from` in the record, carried through any chain of reuse), and
      `--no-reuse` judges every case, which is how a phase closes on one full run.

What changes at scale
    With a remote judge and many cases, this becomes a content-addressed score
    cache keyed the same way, shared by CI and every developer, instead of one
    attested report on one machine.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
import hashlib
import inspect
import json
from pathlib import Path
from types import ModuleType
from typing import Any

from wyld3rness.eval import judge, metrics
from wyld3rness.eval.attest import AttestationInvalid, file_sha256, load_attestation
from wyld3rness.eval.case import Case
from wyld3rness.eval.report import ReportInvalid, load_report
from wyld3rness.eval.stats import METRIC_NAMES
from wyld3rness.eval.sut import Answer

# The code that turns judge replies into scores: the metrics (their prompts
# and arithmetic) and the judge client (its call options and parsing). Their
# only other first-party import, the tracing helper judge.py loads lazily,
# records spans and cannot change a score, so it is left out.
JUDGE_MODULES: tuple[ModuleType, ...] = (judge, metrics)


def _digest(payload: Mapping[str, Any]) -> str:
    """SHA-256 of canonical JSON: sorted keys, no whitespace choices left to chance."""
    text = json.dumps(payload, sort_keys=True, ensure_ascii=False, separators=(",", ":"))
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def judge_code_digest(modules: Sequence[ModuleType] = JUDGE_MODULES) -> str:
    """SHA-256 over the source files of the judge modules as loaded in this process.

    The files of the modules that actually run, so a gate in an exported
    checkout hashes that checkout's judge. No repository path is computed.
    """
    return _digest({m.__name__: file_sha256(Path(inspect.getfile(m))) for m in modules})


def judge_identity(
    *,
    judge_model: str,
    judge_digest: str,
    prompt_versions: Mapping[str, str],
    judge_code: str,
    extra: Mapping[str, str],
) -> str | None:
    """One digest for who judges and how, or None when the judge's digest is unknown.

    An unknown digest means Ollama could not say which weights answer to the
    model name, so two runs cannot be shown to share a judge: no reuse.
    `extra` is what the app adds (see the module docstring).
    """
    if judge_digest in ("", "unknown"):
        return None
    return _digest(
        {
            "judge_model": judge_model,
            "judge_digest": judge_digest,
            "prompt_versions": dict(prompt_versions),
            "judge_code": judge_code,
            "extra": dict(extra),
        }
    )


def judge_key(identity: str, case: Case, answer: Answer) -> str:
    """Everything one case's four scores depend on, as one SHA-256."""
    return _digest(
        {
            "identity": identity,
            "question": case.question,
            "reference": case.reference,
            "answer": answer.text,
            "contexts": list(answer.contexts),
            "action": answer.action,
        }
    )


def _reusable(record: Mapping[str, Any]) -> bool:
    """True when the record holds all four metrics, each scored, or failed only because no action was proposed."""
    metrics = record.get("metrics")
    if not isinstance(metrics, dict) or set(metrics) != set(METRIC_NAMES):
        return False
    for name, result in metrics.items():
        if not isinstance(result, dict):
            return False
        if result.get("failed") is True and name == "action_quality" and not record.get("proposed_action"):
            continue  # set by the inputs: the metric refuses an empty action (runner._judge)
        score = result.get("score")
        if result.get("failed") is not False or isinstance(score, bool) or not isinstance(score, int | float):
            return False
    return True


@dataclass(frozen=True)
class PriorScores:
    """The attested run's judge keys and scores, by case id.

    `run` is that run's report file name. Each entry is (judge key, metrics,
    the report that judged them): the attested run itself, or, when it had
    reused the score too, the run it named, so a chain of reuse still names
    the run whose judge produced the score.
    """

    run: str
    scores: Mapping[str, tuple[str, Mapping[str, Any], str]]

    def match(self, case_id: str, key: str) -> tuple[Mapping[str, Any], str] | None:
        """(metrics, the report that judged them) for this case when its key is identical, else None."""
        found = self.scores.get(case_id)
        if found is None or found[0] != key:
            return None
        return found[1], found[2]


def load_prior(attestation_path: Path, reports_dir: Path) -> tuple[PriorScores | None, str]:
    """The attested run's scores, or None, with one line saying which and why.

    Never raises for a missing or unusable source: no reuse is always safe,
    it only costs judge time. The line goes to the run's progress output.
    """
    try:
        doc = load_attestation(attestation_path)
    except AttestationInvalid as err:
        return None, f"no score reuse: {err}"
    name = doc["report"]["name"]
    if not name or Path(name).name != name:
        return None, "no score reuse: the attestation names its report with a path, not a file name"
    path = reports_dir / name
    if not path.is_file():
        return None, f"no score reuse: the attested report {name} is not on this machine"
    if file_sha256(path) != doc["report"]["sha256"]:
        return None, f"no score reuse: {name} does not match the hash in the attestation"
    try:
        report = load_report(path)
    except ReportInvalid as err:
        return None, f"no score reuse: {err}"
    scores = {
        c["id"]: (c["judge_key"], c["metrics"], c.get("scores_from") or name)
        for c in report["cases"]
        if isinstance(c.get("judge_key"), str) and _reusable(c)
    }
    if not scores:
        return None, f"no score reuse: {name} has no judge keys (written before score reuse existed)"
    return PriorScores(run=name, scores=scores), f"score reuse: from {name}, {len(scores)} case(s) eligible"
