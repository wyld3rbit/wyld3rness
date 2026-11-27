"""The statistics of a paired experiment on incidents: every storm through every arm, compared storm by storm.

    per storm: arm A's value - arm B's value ──► paired differences ──► bootstrap 95% interval
    per storm: A resolved and B not (a win), B and not A (a loss) ──► exact paired test ──► step-down correction
    0 harms in n storms ──► an upper bound on the harm rate
    pilot model of one storm ──► simulated experiments ──► power at n, and the n to pre-register

Why paired: storms differ far more from each other (a retry storm loses about
four times the customer minutes of a hot shard) than arms differ on one storm,
so comparing each storm with itself removes the storm's own size from the noise.

Why a bootstrap interval and not a t-test: the differences are lumpy (a script
pays for one wrong try or two, or none), not normal. The interval comes from the
harness's eval statistics (`wyld3rness.eval.stats.bootstrap_ci`), seeded, so a
result file always gives the same interval. Why an exact test for wins and
losses: with a few dozen storms the normal approximation is poor, and the exact
test needs only the count of storms where the arms disagree.

Why a step-down correction: several secondary comparisons are made, and it
keeps the chance of any false finding at 5% without assuming they are
independent, while rejecting at least as much as dividing 5% by their number.

Why power by simulation: the differences are a mixture (which kind of storm,
which order the script tried), so no closed formula fits. The app supplies a
function that draws one storm's difference under the effect it wants to
detect; this module repeats whole experiments and counts how often the interval
excludes zero. Standard library only.
"""

from __future__ import annotations

from collections.abc import Callable, Sequence
import math
import random

from wyld3rness.eval.stats import BOOTSTRAP_SEED, bootstrap_ci

ALPHA = 0.05


def paired_interval(
    a: Sequence[float], b: Sequence[float], *, seed: int = BOOTSTRAP_SEED, resamples: int = 10_000
) -> tuple[float, tuple[float, float] | None]:
    """The mean of `a[i] - b[i]` and its seeded bootstrap 95% interval. Pure. Raises on unequal lengths."""
    if len(a) != len(b) or not a:
        raise ValueError(f"paired values need equal, non-zero lengths, got {len(a)} and {len(b)}")
    diffs = [x - y for x, y in zip(a, b, strict=True)]
    return sum(diffs) / len(diffs), bootstrap_ci(diffs, seed=seed, resamples=resamples)


def exact_paired_p(wins: int, losses: int) -> float:
    """Two-sided exact p for `wins` against `losses` among the storms where two arms disagree. Pure.

    Under no difference, each disagreement is a fair coin: the p is twice the
    smaller binomial tail, capped at 1 (the exact paired sign test).
    """
    n = wins + losses
    if n == 0:
        return 1.0
    tail = sum(math.comb(n, k) for k in range(min(wins, losses) + 1)) / 2.0**n
    return min(1.0, 2 * tail)


def step_down(pvalues: dict[str, float], alpha: float = ALPHA) -> dict[str, bool]:
    """Which comparisons are rejected under the step-down correction at `alpha`. Pure.

    Sorted from the smallest p, the k-th (from 0) is compared with alpha / (m - k);
    the first that fails stops it, and every later one is kept too.
    """
    m = len(pvalues)
    rejected = dict.fromkeys(pvalues, False)
    for k, (name, p) in enumerate(sorted(pvalues.items(), key=lambda kv: kv[1])):
        if p > alpha / (m - k):
            break
        rejected[name] = True
    return rejected


def zero_events_upper(n: int, alpha: float = ALPHA) -> float:
    """The exact one-sided upper bound on a rate after 0 events in `n` trials: 1 - alpha^(1/n). Pure.

    About 3/n for large n (the rule of three).
    """
    if n <= 0:
        raise ValueError("no trials, no bound")
    return 1 - float(alpha ** (1 / n))


def power(
    draw: Callable[[int, random.Random], float],
    n: int,
    *,
    sims: int = 2000,
    seed: int = 1,
    resamples: int = 1000,
) -> float:
    """The share of `sims` simulated experiments of `n` storms whose 95% interval lies entirely below zero.

    `draw(i, rng)` is one storm's difference (the arm under test minus its
    baseline) for storm i, under the effect to detect; i lets the app give each
    storm its kind, as the real experiment does. Seeded, so it is a pure
    function of its arguments.
    """
    rng = random.Random(seed)
    hits = 0
    for _ in range(sims):
        diffs = [draw(i, rng) for i in range(n)]
        interval = bootstrap_ci(diffs, seed=rng.randrange(2**32), resamples=resamples)
        hits += interval is not None and interval[1] < 0
    return hits / sims
