"""--per-rolled-pod mode — per-rolled-pod FPR table + directional ordering."""

from __future__ import annotations

import sys
from pathlib import Path
from typing import Optional

from ..metrics import compute_per_rolled_pod_fpr


def render_per_rolled_pod_markdown(results: list[dict],
                                   conditions: list[str],
                                   trials: list[int]) -> str:
    """Render the per-rolled-pod analysis as a markdown report."""
    def _pct(x):
        return "N/A" if x is None else f"{x*100:.2f}%"

    lines: list[str] = []
    lines.append(f"## Per-rolled-pod FPR (N={len(trials)} trials per condition)")
    lines.append("")
    lines.append("| Condition | Trial | rolled pods | FPR_rolled_all | FPR_rolled_warmup | n_rows_all / n_rows_warmup |")
    lines.append("|---|---:|---:|---:|---:|---:|")
    for r in results:
        lines.append(
            f"| {r['condition']} | {r['trial']} | {r['rolled_pod_count']} | "
            f"{_pct(r['fpr_rolled_all'])} | {_pct(r['fpr_rolled_warmup'])} | "
            f"{r['n_rows_rolled_all']} / {r['n_rows_rolled_warmup']} |"
        )
    lines.append("")

    lines.append(f"### Per-condition summary (mean ± range across {len(trials)} trials)")
    lines.append("")
    lines.append("| Condition | FPR_rolled_all mean | range | FPR_rolled_warmup mean | range |")
    lines.append("|---|---:|---:|---:|---:|")
    summary: dict[str, dict[str, list[float]]] = {}
    for c in conditions:
        all_vals = [r["fpr_rolled_all"] for r in results
                    if r["condition"] == c and r["fpr_rolled_all"] is not None]
        warmup_vals = [r["fpr_rolled_warmup"] for r in results
                       if r["condition"] == c and r["fpr_rolled_warmup"] is not None]
        summary[c] = {"all": all_vals, "warmup": warmup_vals}

        def _stats(vals):
            if not vals:
                return "N/A", "—"
            mean = sum(vals) / len(vals)
            if len(vals) == 1:
                return f"{mean*100:.2f}%", f"[{mean*100:.2f}%]"
            return f"{mean*100:.2f}%", f"[{min(vals)*100:.2f}%, {max(vals)*100:.2f}%]"

        m_all, r_all = _stats(all_vals)
        m_warm, r_warm = _stats(warmup_vals)
        lines.append(f"| {c} | {m_all} | {r_all} | {m_warm} | {r_warm} |")
    lines.append("")

    lines.append(f"### H2 directional ordering check (informational, N={len(trials)})")
    lines.append("")
    lines.append("Expected: BLIND_ROLLOUT > PHASE_ROLLOUT > PHASECTRL_ROLLOUT on both FPR variants.")
    lines.append("")

    def _means_chain(metric_key: str):
        vals = []
        for c in ["BLIND_ROLLOUT", "PHASE_ROLLOUT", "PHASECTRL_ROLLOUT"]:
            vs = summary.get(c, {}).get(metric_key, [])
            vals.append((c, sum(vs) / len(vs) if vs else None))
        return vals

    def _verdict(chain):
        if any(m is None for _, m in chain):
            return "INSUFFICIENT DATA"
        a, b, c_ = chain[0][1], chain[1][1], chain[2][1]
        if a > b > c_:
            return "**HOLDS**"
        if a > b and b > c_:
            return "**HOLDS**"
        return "VIOLATED or NOISY"

    chain_all = _means_chain("all")
    chain_warm = _means_chain("warmup")

    def _fmt_chain(chain):
        parts = []
        for c, m in chain:
            parts.append(f"{c}={_pct(m)}")
        return " → ".join(parts)

    lines.append(f"Per-rolled-pod-all ordering:    {_fmt_chain(chain_all)}  → {_verdict(chain_all)}")
    lines.append(f"Per-rolled-pod-warmup ordering: {_fmt_chain(chain_warm)}  → {_verdict(chain_warm)}")
    lines.append("")
    lines.append("Note: no statistical test at this N — N≥10 + paired Wilcoxon "
                 "signed-rank planned for the N=120 analysis pipeline.")
    lines.append("")
    return "\n".join(lines)


def run(results_root: Path, trial_ids: list[int], conditions: list[str],
        output: Optional[Path]) -> None:
    """--per-rolled-pod entry point."""
    results: list[dict] = []
    for t in trial_ids:
        trial_dir = results_root / f"trial_{t:02d}"
        for c in conditions:
            csv_path = trial_dir / f"{c}_raw_data.csv"
            entry = compute_per_rolled_pod_fpr(csv_path)
            entry["trial"] = t
            entry["condition"] = c
            results.append(entry)

    md = render_per_rolled_pod_markdown(results, conditions, trial_ids)
    print(md)
    if output is not None:
        output.parent.mkdir(parents=True, exist_ok=True)
        output.write_text(md)
        print(f"\n[wrote] {output}", file=sys.stderr)
