"""--per-condition-fpr mode — Phase-1 H2 formal tests (paired Wilcoxon, Holm)."""

from __future__ import annotations

import sys
from pathlib import Path
from typing import Optional

import numpy as np

from ..formatting import fmt_metric as _fmt_metric, fmt_p as _fmt_p, fmt_rb as _fmt_rb
from ..inference import ALPHA, _holm_correction, _rank_biserial, _wilcoxon_paired
from ..metrics import compute_per_rolled_pod_fpr

_FPR_VARIANTS = (("all", "fpr_rolled_all"), ("warmup", "fpr_rolled_warmup"))


def compute_per_condition_fpr_tests(results_root: Path,
                                    trial_ids: list[int],
                                    baseline: str,
                                    treatments: list[str],
                                    ) -> dict:
    """Per-condition rolled-pod FPR summary + paired Wilcoxon tests."""
    conditions = [baseline] + [t for t in treatments if t != baseline]

    fpr: dict[tuple[str, int], dict] = {}
    per_condition: dict[str, dict] = {}
    for c in conditions:
        all_vals: list[float] = []
        warm_vals: list[float] = []
        for t in trial_ids:
            csv_path = results_root / f"trial_{t:02d}" / f"{c}_raw_data.csv"
            entry = compute_per_rolled_pod_fpr(csv_path)
            fpr[(c, t)] = {"all": entry["fpr_rolled_all"],
                           "warmup": entry["fpr_rolled_warmup"]}
            if entry["fpr_rolled_all"] is not None:
                all_vals.append(entry["fpr_rolled_all"])
            if entry["fpr_rolled_warmup"] is not None:
                warm_vals.append(entry["fpr_rolled_warmup"])
        per_condition[c] = {
            "all_mean":     float(np.mean(all_vals)) if all_vals else None,
            "all_range":    (min(all_vals), max(all_vals)) if all_vals else None,
            "all_n":        len(all_vals),
            "warmup_mean":  float(np.mean(warm_vals)) if warm_vals else None,
            "warmup_range": (min(warm_vals), max(warm_vals)) if warm_vals else None,
            "warmup_n":     len(warm_vals),
        }

    tests: list[dict] = []
    for variant_key, _field in _FPR_VARIANTS:
        for treat in treatments:
            base_vec: list[float] = []
            treat_vec: list[float] = []
            for t in trial_ids:
                bv = fpr[(baseline, t)][variant_key]
                tv = fpr[(treat, t)][variant_key]
                if bv is None or tv is None:
                    continue
                base_vec.append(bv)
                treat_vec.append(tv)
            diffs = [b - s for b, s in zip(base_vec, treat_vec)]
            W, p, note = _wilcoxon_paired(base_vec, treat_vec, alternative="greater")
            tests.append({
                "label":        f"{variant_key}:{treat}",
                "variant":      variant_key,
                "treatment":    treat,
                "n_pairs":      len(diffs),
                "W":            W,
                "p_raw":        p,
                "note":         note,
                "r_rb":         _rank_biserial(diffs) if diffs else float("nan"),
                "median_delta": float(np.median(diffs)) if diffs else float("nan"),
            })

    holm = _holm_correction([(r["label"], r["p_raw"]) for r in tests])
    holm_map = {lbl: (pholm, verdict) for lbl, _praw, pholm, verdict in holm}
    for r in tests:
        r["p_holm"], r["verdict"] = holm_map[r["label"]]

    return {"per_condition": per_condition, "tests": tests,
            "baseline": baseline, "treatments": treatments, "trial_ids": trial_ids}


def render_per_condition_fpr_markdown(block: dict) -> str:
    pc = block["per_condition"]
    baseline = block["baseline"]
    n = len(block["trial_ids"])
    lines: list[str] = []
    lines.append(f"## Per-condition rolled-pod FPR (N={n} trials)")
    lines.append("")
    lines.append("| Condition | FPR_rolled_all mean | range | "
                 "FPR_rolled_warmup mean | range |")
    lines.append("|---|---:|---:|---:|---:|")
    for c, s in pc.items():
        ar = (f"[{s['all_range'][0]*100:.2f}%, {s['all_range'][1]*100:.2f}%]"
              if s["all_range"] else "—")
        wr = (f"[{s['warmup_range'][0]*100:.2f}%, {s['warmup_range'][1]*100:.2f}%]"
              if s["warmup_range"] else "—")
        lines.append(f"| {c} | {_fmt_metric(s['all_mean'])} | {ar} | "
                     f"{_fmt_metric(s['warmup_mean'])} | {wr} |")
    lines.append("")
    lines.append(f"## H2 formal tests — paired one-sided Wilcoxon "
                 f"(baseline {baseline} > treatment), Holm-corrected α={ALPHA}")
    lines.append("")
    lines.append("| Test | N | W | p (raw) | p (Holm) | r_rb | median Δ | Verdict |")
    lines.append("|---|---:|---:|---:|---:|---:|---:|---|")
    variant_name = {"all": "rolled FPR", "warmup": "warmup FPR"}
    for r in block["tests"]:
        treat_short = r["treatment"].replace("_ROLLOUT", "")
        name = f"H2 — {variant_name[r['variant']]}, {treat_short}"
        md = r["median_delta"]
        md_str = f"{md*100:+.2f} pp" if md == md else "n/a"
        lines.append(
            f"| {name} | {r['n_pairs']} | {r['W']:.2f} | {_fmt_p(r['p_raw'])} | "
            f"{_fmt_p(r['p_holm'])} | {_fmt_rb(r['r_rb'])} | {md_str} | "
            f"{r['verdict']} |")
    lines.append("")
    lines.append("> Pairing is by trial; pairs where either condition has no "
                 "rolled pods (FPR undefined) are dropped. Wilcoxon method is "
                 "pinned (exact for N≤50). Reproduces RESULTS.md §2.2.")
    return "\n".join(lines)


def run(results_root: Path, trial_ids: list[int], baseline: str,
        treatments: list[str], output: Optional[Path]) -> None:
    """--per-condition-fpr entry point."""
    block = compute_per_condition_fpr_tests(results_root, trial_ids, baseline, treatments)
    md = render_per_condition_fpr_markdown(block)
    print(md)
    if output is not None:
        output.parent.mkdir(parents=True, exist_ok=True)
        output.write_text(md)
        print(f"\n[wrote] {output}", file=sys.stderr)
