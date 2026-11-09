"""--per-attack-h3 mode — window-pooled per-attack TPR + Wilson CI + Fisher exact."""

from __future__ import annotations

import sys
from pathlib import Path
from typing import Optional

import numpy as np
from scipy import stats

from ..io import load_csv_comment as _load_csv_comment


def _wilson_ci(count: int, nobs: int, alpha: float = 0.05) -> tuple[float, float]:
    """Wilson 95% CI on a binomial proportion (Wilson 1927)."""
    if nobs <= 0:
        return 0.0, 1.0
    z = float(stats.norm.ppf(1.0 - alpha / 2.0))
    p_hat = count / nobs
    z2_n = (z * z) / nobs
    denom = 1.0 + z2_n
    centre = (p_hat + z2_n / 2.0) / denom
    half_width = (z * np.sqrt(p_hat * (1.0 - p_hat) / nobs + z2_n / (4.0 * nobs))) / denom
    lo = max(0.0, centre - half_width)
    hi = min(1.0, centre + half_width)
    return lo, hi


def compute_per_attack_h3_stats(results_root: Path,
                                trials: list[int],
                                attack_classes: list[str],
                                ) -> dict:
    """Window-pool malicious anomalies across trials per attack-condition."""
    per_condition: dict = {}
    timings = ("NAIVE", "ADV")
    for attack in attack_classes:
        for timing in timings:
            cond_label = f"H3_{attack}_{timing}"
            n_mal_total = 0
            n_anom_total = 0
            trial_breakdown: list[tuple[int, int, int]] = []
            for tid in trials:
                csv_path = (results_root / f"trial_{tid:02d}"
                            / f"{cond_label}_raw_data.csv")
                df = _load_csv_comment(csv_path)
                if df is None or "ground_truth" not in df.columns:
                    continue
                mal = df[df["ground_truth"] == "malicious"]
                n_mal = int(len(mal))
                n_anom = int(mal["is_anomaly"].sum()) if n_mal > 0 else 0
                trial_breakdown.append((tid, n_mal, n_anom))
                n_mal_total += n_mal
                n_anom_total += n_anom

            tpr = (n_anom_total / n_mal_total) if n_mal_total > 0 else None
            ci_lo, ci_hi = _wilson_ci(n_anom_total, n_mal_total) if n_mal_total > 0 else (None, None)

            binom_p_ge_090 = None
            if n_mal_total > 0:
                bt = stats.binomtest(n_anom_total, n_mal_total, 0.90, alternative="less")
                binom_p_ge_090 = float(bt.pvalue)

            per_condition[cond_label] = {
                "n_malicious": n_mal_total,
                "n_anomaly": n_anom_total,
                "tpr": tpr,
                "wilson_ci_lo": ci_lo,
                "wilson_ci_hi": ci_hi,
                "n_trials": len(trial_breakdown),
                "trial_breakdown": trial_breakdown,
                "binom_p_ge_090": binom_p_ge_090,
            }

    per_attack: dict = {}
    for attack in attack_classes:
        naive = per_condition.get(f"H3_{attack}_NAIVE")
        adv = per_condition.get(f"H3_{attack}_ADV")
        if not naive or not adv:
            continue
        if naive["n_malicious"] == 0 or adv["n_malicious"] == 0:
            continue
        a, b = naive["n_anomaly"], naive["n_malicious"] - naive["n_anomaly"]
        c, d = adv["n_anomaly"],   adv["n_malicious"]   - adv["n_anomaly"]
        odds_ratio, fisher_p = stats.fisher_exact([[a, b], [c, d]], alternative="two-sided")
        tpr_delta = naive["tpr"] - adv["tpr"]
        l1, u1 = naive["wilson_ci_lo"], naive["wilson_ci_hi"]
        l2, u2 = adv["wilson_ci_lo"],   adv["wilson_ci_hi"]
        p1, p2 = naive["tpr"], adv["tpr"]
        lo = (p1 - p2) - np.sqrt((p1 - l1) ** 2 + (u2 - p2) ** 2)
        hi = (p1 - p2) + np.sqrt((u1 - p1) ** 2 + (p2 - l2) ** 2)
        per_attack[attack] = {
            "naive_n": naive["n_malicious"], "naive_anom": naive["n_anomaly"],
            "naive_tpr": naive["tpr"],
            "adv_n": adv["n_malicious"],     "adv_anom": adv["n_anomaly"],
            "adv_tpr": adv["tpr"],
            "fisher_exact_p": float(fisher_p),
            "odds_ratio": float(odds_ratio) if np.isfinite(odds_ratio) else None,
            "tpr_delta": tpr_delta,
            "tpr_delta_ci_lo": float(lo),
            "tpr_delta_ci_hi": float(hi),
        }

    return {"per_condition": per_condition, "per_attack_naive_vs_adv": per_attack}


def render_per_attack_h3_markdown(stats_block: dict,
                                  trials: list[int],
                                  attack_classes: list[str]) -> str:
    """Render the per-attack H3 stats as a markdown report."""
    per_cond = stats_block["per_condition"]
    per_attack = stats_block["per_attack_naive_vs_adv"]

    def _pct(x):
        return "n/a" if x is None else f"{x*100:.1f}%"
    def _ci(lo, hi):
        if lo is None or hi is None: return "n/a"
        return f"[{lo*100:.1f}%, {hi*100:.1f}%]"
    def _p(p):
        if p is None: return "n/a"
        if p < 0.001: return "<0.001"
        return f"{p:.4f}"

    lines: list[str] = []
    lines.append(f"## Per-attack H3 (window-pooled TPR + Wilson 95% CI, N={len(trials)} trials)")
    lines.append("")
    lines.append(
        "Window-level pooling across trials per attack-condition.  Rationale: "
        "per-trial malicious-window denominator is 1–4 for our setup, which "
        "makes per-trial TPR an ordinal scale.  "
        "Wilson 1927 CI on pooled (anomaly, malicious) is the appropriate "
        "interval-estimator.  One-sided exact binomial p-value reports rejection "
        "of H0: TPR ≥ 0.90 (i.e. p < α ⇒ TPR materially below 0.90)."
    )
    lines.append("")
    lines.append("| Condition | n_mal | n_anom | TPR | 95% Wilson CI | p (H0: TPR ≥ 0.90) | n_trials |")
    lines.append("|---|---:|---:|---:|---:|---:|---:|")
    for attack in attack_classes:
        for timing in ("NAIVE", "ADV"):
            cond = f"H3_{attack}_{timing}"
            row = per_cond.get(cond)
            if row is None:
                lines.append(f"| {cond} | — | — | — | — | — | 0 |")
                continue
            lines.append(
                f"| {cond} | {row['n_malicious']} | {row['n_anomaly']} | "
                f"{_pct(row['tpr'])} | {_ci(row['wilson_ci_lo'], row['wilson_ci_hi'])} | "
                f"{_p(row['binom_p_ge_090'])} | {row['n_trials']} |"
            )
    lines.append("")

    lines.append("### NAIVE vs ADV per attack class (Fisher exact + Newcombe CI on TPR_naive − TPR_adv)")
    lines.append("")
    if not per_attack:
        lines.append("_No condition pairs have both NAIVE and ADV data._")
        lines.append("")
    else:
        lines.append("| Attack | TPR_naive (n) | TPR_adv (n) | Δ TPR | 95% CI (Newcombe) | Fisher p | OR |")
        lines.append("|---|---|---|---:|---:|---:|---:|")
        for attack in attack_classes:
            row = per_attack.get(attack)
            if row is None:
                lines.append(f"| {attack} | — | — | — | — | — | — |")
                continue
            or_str = "∞" if row["odds_ratio"] is None else f"{row['odds_ratio']:.2f}"
            lines.append(
                f"| {attack} | {_pct(row['naive_tpr'])} ({row['naive_anom']}/{row['naive_n']}) | "
                f"{_pct(row['adv_tpr'])} ({row['adv_anom']}/{row['adv_n']}) | "
                f"{row['tpr_delta']*100:+.1f}pp | "
                f"{_ci(row['tpr_delta_ci_lo'], row['tpr_delta_ci_hi'])} | "
                f"{_p(row['fisher_exact_p'])} | {or_str} |"
            )
        lines.append("")

    lines.append("### Per-trial breakdown")
    lines.append("")
    lines.append("| Condition | trial | n_mal | n_anom | TPR_trial |")
    lines.append("|---|---:|---:|---:|---:|")
    for attack in attack_classes:
        for timing in ("NAIVE", "ADV"):
            cond = f"H3_{attack}_{timing}"
            row = per_cond.get(cond)
            if row is None:
                continue
            for tid, n_mal, n_anom in row["trial_breakdown"]:
                tpr_t = (n_anom / n_mal) if n_mal > 0 else None
                lines.append(
                    f"| {cond} | {tid:>2} | {n_mal} | {n_anom} | {_pct(tpr_t)} |"
                )
    lines.append("")
    return "\n".join(lines)


def run(results_root: Path, trials: list[int], attack_classes: list[str],
        output: Optional[Path]) -> None:
    """--per-attack-h3 entry point."""
    stats_block = compute_per_attack_h3_stats(results_root, trials, attack_classes)
    md = render_per_attack_h3_markdown(stats_block, trials, attack_classes)
    print(md)
    if output is not None:
        output.parent.mkdir(parents=True, exist_ok=True)
        output.write_text(md)
        print(f"\n[wrote] {output}", file=sys.stderr)
