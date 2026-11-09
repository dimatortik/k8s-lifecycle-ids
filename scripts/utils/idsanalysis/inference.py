"""Statistical primitives + family constants."""

from __future__ import annotations

import statistics
from dataclasses import dataclass

import numpy as np
from scipy import stats


ALPHA = 0.05

DELTA_EQUIV = 0.01
DELTA_NONINF = 0.01

_BOOTSTRAP_RESAMPLES = 1000
_BOOTSTRAP_SEED = 0


def _mean_var(xs: list[float]) -> tuple[float, float]:
    if len(xs) == 0:
        return float("nan"), float("nan")
    if len(xs) == 1:
        return xs[0], 0.0
    return statistics.mean(xs), statistics.variance(xs)


def _rank_biserial(diffs: list[float]) -> float:
    """Signed rank-biserial correlation r_rb ∈ [−1, +1]."""
    nonzero = [d for d in diffs if d != 0]
    if len(nonzero) == 0:
        return 0.0
    abs_ranks = stats.rankdata([abs(d) for d in nonzero])
    pos_sum = float(sum(r for r, d in zip(abs_ranks, nonzero) if d > 0))
    neg_sum = float(sum(r for r, d in zip(abs_ranks, nonzero) if d < 0))
    total = pos_sum + neg_sum
    if total == 0:
        return 0.0
    return (pos_sum - neg_sum) / total


def _bootstrap_median_ci(values: list[float],
                          n_resamples: int = _BOOTSTRAP_RESAMPLES,
                          ci: float = 0.95,
                          seed: int = _BOOTSTRAP_SEED
                          ) -> tuple[float, float]:
    """Percentile-bootstrap CI for median(values)."""
    n = len(values)
    if n < 2:
        return float("nan"), float("nan")
    rng = np.random.default_rng(seed)
    arr = np.asarray(values, dtype=float)
    samples = rng.choice(arr, size=(n_resamples, n), replace=True)
    medians = np.median(samples, axis=1)
    alpha = (1 - ci) / 2
    return float(np.quantile(medians, alpha)), float(np.quantile(medians, 1 - alpha))


def _mannwhitney(a: list[float], b: list[float]) -> tuple[float, float, str]:
    """Mann–Whitney U two-sided."""
    if len(a) < 2 or len(b) < 2:
        return float("nan"), float("nan"), "insufficient samples (need ≥2 each)"
    res = stats.mannwhitneyu(a, b, alternative="two-sided")
    return float(res.statistic), float(res.pvalue), ""


def _wilcoxon_paired(a: list[float],
                     b: list[float],
                     alternative: str = "two-sided",
                     ) -> tuple[float, float, str]:
    """Wilcoxon signed-rank on differences a − b."""
    if len(a) != len(b):
        return float("nan"), float("nan"), f"length mismatch ({len(a)} vs {len(b)})"
    if len(a) < 2:
        return float("nan"), float("nan"), "insufficient pairs (need ≥2)"
    diffs = [x - y for x, y in zip(a, b)]
    if all(d == 0 for d in diffs):
        return 0.0, 1.0, "all paired differences are zero — test undefined, p set to 1.0"
    pinned_method = "exact" if len(a) <= 50 else "approx"
    try:
        res = stats.wilcoxon(a, b, alternative=alternative,
                             zero_method="wilcox", method=pinned_method)
    except TypeError:
        res = stats.wilcoxon(a, b, alternative=alternative,
                             zero_method="wilcox", mode=pinned_method)
    return float(res.statistic), float(res.pvalue), ""


def _wilcoxon_one_sample(diffs: list[float],
                         alternative: str,
                         ) -> tuple[float, float, str]:
    """Wilcoxon signed-rank one-sample (test median(diffs) vs 0)."""
    if len(diffs) < 2:
        return float("nan"), float("nan"), "insufficient pairs (need ≥2)"
    if all(d == 0 for d in diffs):
        return 0.0, 1.0, "all values zero — test undefined, p set to 1.0"
    pinned_method = "exact" if len(diffs) <= 50 else "approx"
    try:
        res = stats.wilcoxon(diffs, alternative=alternative,
                             zero_method="wilcox", method=pinned_method)
    except TypeError:
        res = stats.wilcoxon(diffs, alternative=alternative,
                             zero_method="wilcox", mode=pinned_method)
    return float(res.statistic), float(res.pvalue), ""


@dataclass
class TostResult:
    p_lower: float
    p_upper: float
    p_tost: float
    r_rb_lower: float
    r_rb_upper: float
    note: str = ""


def _tost_paired(diffs: list[float], delta: float) -> TostResult:
    """Schuirmann's TOST via two one-sided Wilcoxon signed-rank tests."""
    if len(diffs) < 2:
        return TostResult(
            p_lower=float("nan"), p_upper=float("nan"),
            p_tost=float("nan"),
            r_rb_lower=float("nan"), r_rb_upper=float("nan"),
            note="insufficient pairs (need ≥2)",
        )
    lower_shifted = [d + delta for d in diffs]
    upper_shifted = [d - delta for d in diffs]
    _, p_lower, note_l = _wilcoxon_one_sample(lower_shifted, "greater")
    _, p_upper, note_u = _wilcoxon_one_sample(upper_shifted, "less")
    note = "; ".join(s for s in (note_l, note_u) if s)
    return TostResult(
        p_lower=p_lower,
        p_upper=p_upper,
        p_tost=float(max(p_lower, p_upper)) if (p_lower == p_lower and p_upper == p_upper) else float("nan"),
        r_rb_lower=_rank_biserial(lower_shifted),
        r_rb_upper=_rank_biserial(upper_shifted),
        note=note,
    )


def _holm_correction(p_values: list[tuple[str, float]]) -> list[tuple[str, float, float, str]]:
    """Holm step-down: corrected p preserves monotonicity, verdict at α=ALPHA."""
    valid = [(lbl, p) for lbl, p in p_values if p == p]
    m = len(valid)
    if m == 0:
        return [(lbl, p, float("nan"), "—") for lbl, p in p_values]

    valid_sorted = sorted(valid, key=lambda x: x[1])
    corrected_map: dict[str, float] = {}
    prev = 0.0
    for k, (lbl, p) in enumerate(valid_sorted):
        c = min(1.0, max(prev, p * (m - k)))
        corrected_map[lbl] = c
        prev = c

    out: list[tuple[str, float, float, str]] = []
    for lbl, p in p_values:
        if p != p:
            out.append((lbl, p, float("nan"), "—"))
        else:
            c = corrected_map[lbl]
            verdict = "**reject H0**" if c < ALPHA else "fail to reject H0"
            out.append((lbl, p, c, verdict))
    return out
