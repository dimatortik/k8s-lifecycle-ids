"""Default mode (aggregate H1 / H2a-c tests) for the earlier single-app trial layout; not used for the published campaigns."""

from __future__ import annotations

import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional

import numpy as np
import pandas as pd

from ..formatting import (fmt_ci as _fmt_ci, fmt_metric as _fmt_metric,
                          fmt_p as _fmt_p, fmt_rb as _fmt_rb)
from ..inference import (ALPHA, DELTA_EQUIV, DELTA_NONINF, _bootstrap_median_ci,
                         _holm_correction, _mannwhitney, _mean_var,
                         _rank_biserial, _tost_paired, _wilcoxon_one_sample,
                         _wilcoxon_paired)
from ..io import load_csv as _load_csv
from ..metrics import fpr as _fpr, sep as _sep, tpr as _tpr

_FILE_BASELINE = "baseline_idle_raw_data.csv"
_FILE_H1 = "h1_raw_data.csv"
_FILE_H2 = "h2_raw_data.csv"
_FILE_H3 = "h3_raw_data.csv"


@dataclass
class TrialMetrics:
    trial_id: int
    fpr_b: Optional[float]
    fpr_u: Optional[float]
    fpr_s: Optional[float]
    tpr_s: Optional[float]
    sep: Optional[float]
    n_baseline_windows: int
    n_h1_windows: int
    n_h2_windows: int
    n_h3_malicious_windows: int


def _collect_trial(trial_dir: Path) -> TrialMetrics:
    trial_id = int(trial_dir.name.removeprefix("trial_"))
    base = _load_csv(trial_dir / _FILE_BASELINE)
    h1 = _load_csv(trial_dir / _FILE_H1)
    h2 = _load_csv(trial_dir / _FILE_H2)
    h3 = _load_csv(trial_dir / _FILE_H3)

    fpr_b, n_b = _fpr(base)
    fpr_u, n_u = _fpr(h1)
    fpr_s, n_s = _fpr(h2)
    tpr_s, n_m = _tpr(h3)
    sep      = _sep(h3)

    return TrialMetrics(
        trial_id=trial_id,
        fpr_b=fpr_b, fpr_u=fpr_u, fpr_s=fpr_s, tpr_s=tpr_s, sep=sep,
        n_baseline_windows=n_b,
        n_h1_windows=n_u,
        n_h2_windows=n_s,
        n_h3_malicious_windows=n_m,
    )


def _collect_all(trials_root: Path) -> list[TrialMetrics]:
    if not trials_root.exists():
        sys.exit(f"ERROR: trials root not found: {trials_root}\n"
                 "Run scripts/experiments/run_n10_trials.sh first.")
    trial_dirs = sorted(p for p in trials_root.iterdir()
                        if p.is_dir() and p.name.startswith("trial_"))
    if not trial_dirs:
        sys.exit(f"ERROR: no trial_* directories under {trials_root}")
    return [_collect_trial(d) for d in trial_dirs]


@dataclass
class TestResult:
    label: str
    family: str
    description: str
    test_name: str
    statistic_str: str
    raw_p: float
    effect_size: Optional[float] = None
    ci_low: Optional[float] = None
    ci_high: Optional[float] = None
    extras: dict = field(default_factory=dict)
    note: str = ""


def _run_all_primary_tests(trials: list[TrialMetrics],
                           *,
                           delta_equiv: float = DELTA_EQUIV,
                           delta_noninf: float = DELTA_NONINF,
                           ) -> list[TestResult]:
    """Build all primary-family tests for the H1 + H2a/b/c set."""
    results: list[TestResult] = []

    fpr_b = [t.fpr_b for t in trials if t.fpr_b is not None]
    fpr_u = [t.fpr_u for t in trials if t.fpr_u is not None]

    paired_u, paired_s_a, paired_b, paired_s_bc = [], [], [], []
    for t in trials:
        if t.fpr_u is not None and t.fpr_s is not None:
            paired_u.append(t.fpr_u)
            paired_s_a.append(t.fpr_s)
        if t.fpr_b is not None and t.fpr_s is not None:
            paired_b.append(t.fpr_b)
            paired_s_bc.append(t.fpr_s)

    mw_U, mw_p, mw_note = _mannwhitney(fpr_u, fpr_b)
    results.append(TestResult(
        label="H1",
        family="primary",
        description="rollout inflates FPR over idle (FPR_u vs FPR_b)",
        test_name="Mann–Whitney U (two-sided)",
        statistic_str=f"U = {mw_U:.2f}",
        raw_p=mw_p,
        note=mw_note,
    ))

    wx_W, wx_p, wx_note = _wilcoxon_paired(paired_u, paired_s_a, alternative="greater")
    d_su = [u - s for u, s in zip(paired_u, paired_s_a)]
    r_rb_a = _rank_biserial(d_su)
    results.append(TestResult(
        label="H2a",
        family="primary",
        description="controller reduces rollout FPR (d_su = FPR_u − FPR_s > 0)",
        test_name="Wilcoxon signed-rank (one-sided greater)",
        statistic_str=f"W = {wx_W:.2f}",
        raw_p=wx_p,
        effect_size=r_rb_a,
        note=wx_note,
    ))

    d_sb = [s - b for b, s in zip(paired_b, paired_s_bc)]
    tost = _tost_paired(d_sb, delta_equiv)
    ci_low_b, ci_high_b = _bootstrap_median_ci(d_sb)
    results.append(TestResult(
        label="H2b",
        family="primary",
        description=f"controlled-rollout FPR equivalent to baseline within ±δ = {delta_equiv:.2%}",
        test_name=f"TOST via 2× one-sided Wilcoxon (δ = {delta_equiv:.2%})",
        statistic_str=f"p_TOST = max(p_lo, p_hi)",
        raw_p=tost.p_tost,
        effect_size=None,
        ci_low=ci_low_b,
        ci_high=ci_high_b,
        extras={
            "p_lower": tost.p_lower,
            "p_upper": tost.p_upper,
            "r_rb_lower": tost.r_rb_lower,
            "r_rb_upper": tost.r_rb_upper,
            "median_diff": float(np.median(d_sb)) if len(d_sb) > 0 else float("nan"),
            "delta": delta_equiv,
        },
        note=tost.note,
    ))

    shifted = [d + delta_noninf for d in d_sb]
    _, p_c, note_c = _wilcoxon_one_sample(shifted, "greater")
    r_rb_c = _rank_biserial(shifted)
    ci_low_c, ci_high_c = _bootstrap_median_ci(d_sb)
    results.append(TestResult(
        label="H2c",
        family="primary",
        description=f"no over-suppression: median(FPR_s − FPR_b) > −δ_lower = −{delta_noninf:.2%}",
        test_name=f"Wilcoxon signed-rank one-sided (NI, δ_lower = {delta_noninf:.2%})",
        statistic_str=f"shifted W",
        raw_p=p_c,
        effect_size=r_rb_c,
        ci_low=ci_low_c,
        ci_high=ci_high_c,
        extras={
            "median_diff": float(np.median(d_sb)) if len(d_sb) > 0 else float("nan"),
            "delta_lower": delta_noninf,
        },
        note=note_c,
    ))

    return results


def render_markdown(trials: list[TrialMetrics]) -> str:
    fpr_b = [t.fpr_b for t in trials if t.fpr_b is not None]
    fpr_u = [t.fpr_u for t in trials if t.fpr_u is not None]
    fpr_s = [t.fpr_s for t in trials if t.fpr_s is not None]
    tpr_s = [t.tpr_s for t in trials if t.tpr_s is not None]

    mb, vb = _mean_var(fpr_b)
    mu, vu = _mean_var(fpr_u)
    ms, vs = _mean_var(fpr_s)
    mt, vt = _mean_var(tpr_s)

    primary_tests = _run_all_primary_tests(trials)
    holm_input = [(r.label, r.raw_p) for r in primary_tests if r.family == "primary"]
    corrected = _holm_correction(holm_input)
    corrected_by_label = {label: (raw, c, v) for label, raw, c, v in corrected}

    lines: list[str] = []
    lines.append("# N-trial Statistical Summary (H1, H2a/H2b/H2c)")
    lines.append("")
    lines.append(
        f"Trials analysed: **{len(trials)}**  •  α = {ALPHA}  •  "
        f"Holm-Bonferroni across {len(holm_input)} primary tests  •  "
        f"δ_equiv = δ_lower = {DELTA_EQUIV:.2%} (pre-registered 2026-05-13)"
    )
    lines.append("")

    lines.append("## Per-scenario metrics")
    lines.append("")
    lines.append("| Scenario | Metric | Mean | Variance | n |")
    lines.append("|---|---|---:|---:|---:|")
    lines.append(f"| Baseline (idle / steady)           | FPR_b | {_fmt_metric(mb)} | {vb:.6f} | {len(fpr_b)} |")
    lines.append(f"| H1 (rollout, no controller)        | FPR_u | {_fmt_metric(mu)} | {vu:.6f} | {len(fpr_u)} |")
    lines.append(f"| H2 (rollout + controller)          | FPR_s | {_fmt_metric(ms)} | {vs:.6f} | {len(fpr_s)} |")
    lines.append(f"| H3 (rollout + controller + exfil)  | TPR_s | {_fmt_metric(mt)} | {vt:.6f} | {len(tpr_s)} |")
    lines.append("")

    lines.append("## Primary hypothesis tests")
    lines.append("")
    lines.append(
        "| Hyp | Description | Test | Statistic | p (raw) | p (Holm) | Effect r_rb | 95% CI median(d) | Verdict |"
    )
    lines.append("|---|---|---|---|---:|---:|---:|---:|---|")
    for r in primary_tests:
        if r.family != "primary":
            continue
        raw, corr, verdict = corrected_by_label.get(r.label, (r.raw_p, float("nan"), "—"))
        eff_str = _fmt_rb(r.effect_size)
        ci_str = _fmt_ci(r.ci_low, r.ci_high)
        lines.append(
            f"| {r.label} | {r.description} | {r.test_name} | {r.statistic_str} | "
            f"{_fmt_p(raw)} | {_fmt_p(corr)} | {eff_str} | {ci_str} | {verdict} |"
        )
    lines.append("")

    h2b = next((r for r in primary_tests if r.label == "H2b"), None)
    if h2b is not None and h2b.extras:
        lines.append("### H2b TOST diagnostics")
        lines.append("")
        lines.append("Schuirmann's TOST decomposition (`max(p_lower, p_upper)` is the value submitted to Holm):")
        lines.append("")
        lines.append("| Component | Tests | p | Effect r_rb |")
        lines.append("|---|---|---:|---:|")
        lines.append(
            f"| p_lower | median(d_sb) > −δ ({-h2b.extras['delta']:.2%}) "
            f"| {_fmt_p(h2b.extras['p_lower'])} | {_fmt_rb(h2b.extras['r_rb_lower'])} |"
        )
        lines.append(
            f"| p_upper | median(d_sb) < +δ ({+h2b.extras['delta']:.2%}) "
            f"| {_fmt_p(h2b.extras['p_upper'])} | {_fmt_rb(h2b.extras['r_rb_upper'])} |"
        )
        lines.append("")
        lines.append(f"Observed median(FPR_s − FPR_b) = **{h2b.extras['median_diff']*100:+.2f}%**.")
        lines.append("")

    h2c = next((r for r in primary_tests if r.label == "H2c"), None)
    if h2c is not None:
        h2c_raw, h2c_corr, h2c_verdict = corrected_by_label.get("H2c", (float("nan"), float("nan"), "—"))
        if h2c_verdict == "fail to reject H0":
            med = h2c.extras.get("median_diff", float("nan"))
            d_lo = h2c.extras.get("delta_lower", DELTA_NONINF)
            lines.append("### ⚠ Over-suppression finding")
            lines.append("")
            lines.append(
                f"**H2c fails to reject H0 (over-suppression observed).** "
                f"median(FPR_s − FPR_b) = **{med*100:+.2f}%**, below the "
                f"non-inferiority bound −δ_lower = **{-d_lo:.2%}**.  "
                f"The controller is operating *outside* the equivalence band — "
                f"it suppresses rolling-update FPR not just to baseline level "
                f"but materially below.  Calibration of θ_high should be "
                f"reviewed; whether this also compromises H3 TPR (attack masking) "
                f"is to be evaluated by Phase-2 per-attack H3 results."
            )
            lines.append("")

    lines.append("## H3 (descriptive — no formal test in this table)")
    lines.append("")
    lines.append(
        "H3 is reported as a descriptive metric (mean TPR_s) because the "
        "pre-registered success criterion is a one-sample threshold "
        "(TPR_s ≥ 0.90), not a comparison between distributions.  "
        "Per-attack H3 tests are in the --per-attack-h3 mode."
    )
    lines.append("")

    sep_vals = [t.sep for t in trials if t.sep is not None]
    if sep_vals:
        ms_sep, vs_sep = _mean_var(sep_vals)
        lines.append("## H3 score separation")
        lines.append("")
        lines.append(
            "Sep = median(score | malicious) − median(score | benign).  "
            "More negative = more separation (IF convention: more anomalous = "
            "more negative score)."
        )
        lines.append("")
        lines.append(f"- Mean Sep over {len(sep_vals)} trials: **{ms_sep:.4f}**  "
                     f"(variance {vs_sep:.6f})")
        lines.append("")

    lines.append("## Per-trial values")
    lines.append("")
    lines.append("| trial | FPR_b | FPR_u | FPR_s | TPR_s | Sep | n_b | n_u | n_s | n_h3_mal |")
    lines.append("|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|")
    for t in trials:
        sep_str = f"{t.sep:.4f}" if t.sep is not None else "n/a"
        lines.append(
            f"| {t.trial_id:>2} | "
            f"{_fmt_metric(t.fpr_b)} | {_fmt_metric(t.fpr_u)} | "
            f"{_fmt_metric(t.fpr_s)} | {_fmt_metric(t.tpr_s)} | {sep_str} | "
            f"{t.n_baseline_windows} | {t.n_h1_windows} | "
            f"{t.n_h2_windows} | {t.n_h3_malicious_windows} |"
        )
    lines.append("")
    return "\n".join(lines)


def write_per_trial_csv(trials: list[TrialMetrics], path: Path) -> None:
    rows = [{
        "trial_id": t.trial_id,
        "fpr_b": t.fpr_b, "fpr_u": t.fpr_u, "fpr_s": t.fpr_s, "tpr_s": t.tpr_s,
        "sep": t.sep,
        "n_baseline_windows": t.n_baseline_windows,
        "n_h1_windows": t.n_h1_windows,
        "n_h2_windows": t.n_h2_windows,
        "n_h3_malicious_windows": t.n_h3_malicious_windows,
    } for t in trials]
    path.parent.mkdir(parents=True, exist_ok=True)
    pd.DataFrame(rows).to_csv(path, index=False)


def run(trials_root: Path, output: Optional[Path], per_trial_csv: Path) -> None:
    """Default-mode entry point (no --per-* flag)."""
    trials = _collect_all(trials_root)
    md = render_markdown(trials)
    print(md)
    if output is not None:
        output.parent.mkdir(parents=True, exist_ok=True)
        output.write_text(md)
        print(f"\n[wrote] {output}", file=sys.stderr)
    write_per_trial_csv(trials, per_trial_csv)
    print(f"[wrote] {per_trial_csv}", file=sys.stderr)
