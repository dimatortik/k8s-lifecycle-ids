#!/usr/bin/env python3
"""Statistics for the campaign CSVs.

Modes:
  --per-condition-fpr  paired one-sided Wilcoxon (Holm) on rolled-pod and warmup FPR
  --per-rolled-pod     per-rolled-pod FPR table across conditions
  --per-attack-h3      window-pooled TPR per attack, Wilson CI, Fisher exact
  (default)            aggregate H1/H2 tests for the earlier single-app layout

Example:
    python3 scripts/utils/calculate_statistics.py --per-attack-h3 \\
        --results-root experiments/results/main_campaign_phase2_20260527-094911 \\
        --trials 1,2,3,4,5,6,7,8,9,10,11,12,13,14,15
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

from idsanalysis.inference import (  # noqa: F401
    ALPHA,
    DELTA_EQUIV,
    DELTA_NONINF,
    TostResult,
    _bootstrap_median_ci,
    _holm_correction,
    _mannwhitney,
    _mean_var,
    _rank_biserial,
    _tost_paired,
    _wilcoxon_one_sample,
    _wilcoxon_paired,
)
from idsanalysis.metrics import (  # noqa: F401
    compute_per_rolled_pod_fpr,
    rolled_pod_uids,
)
from idsanalysis.modes import per_attack_h3, per_condition_fpr, per_rolled_pod, primary
from idsanalysis.modes.primary import (  # noqa: F401
    TestResult,
    TrialMetrics,
    _collect_all,
    _run_all_primary_tests,
    render_markdown,
    write_per_trial_csv,
)


_REPO_ROOT = Path(__file__).resolve().parents[2]
_DEFAULT_TRIALS_ROOT = _REPO_ROOT / "experiments" / "results" / "trials"
_DEFAULT_PER_TRIAL_OUT = _REPO_ROOT / "experiments" / "results" / "n10_per_trial_metrics.csv"


def _parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(description=__doc__,
                                formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--trials-root", type=Path, default=_DEFAULT_TRIALS_ROOT,
                   help=f"Directory containing trial_NN/ subdirs (default: {_DEFAULT_TRIALS_ROOT}).")
    p.add_argument("--output", type=Path, default=None,
                   help="Optional Markdown output path. Always also printed to stdout.")
    p.add_argument("--per-trial-csv", type=Path, default=_DEFAULT_PER_TRIAL_OUT,
                   help=f"Per-trial metrics CSV (default: {_DEFAULT_PER_TRIAL_OUT}).")

    p.add_argument("--per-rolled-pod", action="store_true",
                   help="Run Phase-2 per-rolled-pod FPR aggregation instead of "
                        "the default Phase-1 aggregate pipeline.")
    p.add_argument("--per-attack-h3", action="store_true",
                   help="Run Phase-2 per-attack-class H3 TPR aggregation "
                        "(window-pooled TPR + Wilson 95%% CI + Fisher exact).")
    p.add_argument("--attack-classes", type=str, default="EXFIL,TOKEN,RSHELL",
                   help="Comma-separated H3 attack classes for per-attack-h3 mode "
                        "(default: EXFIL,TOKEN,RSHELL).  Each generates "
                        "H3_<CLASS>_{NAIVE,ADV}_raw_data.csv lookups.")
    p.add_argument("--per-condition-fpr", action="store_true",
                   help="Run Phase-1 per-condition FPR formal tests (paired "
                        "one-sided Wilcoxon BLIND vs PHASE/PHASECTRL on rolled + "
                        "warmup FPR, Holm-corrected).  Reproduces RESULTS.md §2.2.")
    p.add_argument("--baseline-condition", type=str, default="BLIND_ROLLOUT",
                   help="Uncontrolled baseline condition for --per-condition-fpr "
                        "(default: BLIND_ROLLOUT).")
    p.add_argument("--treatment-conditions", type=str,
                   default="PHASE_ROLLOUT,PHASECTRL_ROLLOUT",
                   help="Comma-separated treatment conditions for "
                        "--per-condition-fpr (default: "
                        "PHASE_ROLLOUT,PHASECTRL_ROLLOUT).")
    p.add_argument("--trials", type=str, default=None,
                   help="Comma-separated trial ids to include in per-rolled-pod / "
                        "per-attack-h3 / per-condition-fpr analysis "
                        "(e.g. '1,2,3,4,5').  Required for those modes.")
    p.add_argument("--conditions", type=str, default=None,
                   help="Comma-separated Phase-2 condition names for per-rolled-pod "
                        "analysis (e.g. 'BLIND_STEADY,BLIND_ROLLOUT,PHASE_ROLLOUT,"
                        "PHASECTRL_ROLLOUT').  Required with --per-rolled-pod.")
    p.add_argument("--results-root", type=Path, default=None,
                   help="Root containing trial_NN/ subdirs with Phase-2 condition "
                        "CSVs.  Required with --per-rolled-pod / --per-attack-h3 / "
                        "--per-condition-fpr (distinct from --trials-root so the "
                        "same script can serve Phase-1 and Phase-2 layouts).")
    return p.parse_args()


def main() -> None:
    args = _parse_args()

    if args.per_rolled_pod:
        if not args.trials or not args.conditions or args.results_root is None:
            sys.exit("ERROR: --per-rolled-pod requires --trials, --conditions, "
                     "and --results-root.")
        trial_ids = [int(t.strip()) for t in args.trials.split(",") if t.strip()]
        conditions = [c.strip() for c in args.conditions.split(",") if c.strip()]
        per_rolled_pod.run(args.results_root, trial_ids, conditions, args.output)
        return

    if args.per_attack_h3:
        if not args.trials or args.results_root is None:
            sys.exit("ERROR: --per-attack-h3 requires --trials and --results-root.")
        trial_ids = [int(t.strip()) for t in args.trials.split(",") if t.strip()]
        attack_classes = [c.strip().upper() for c in args.attack_classes.split(",") if c.strip()]
        per_attack_h3.run(args.results_root, trial_ids, attack_classes, args.output)
        return

    if args.per_condition_fpr:
        if not args.trials or args.results_root is None:
            sys.exit("ERROR: --per-condition-fpr requires --trials and --results-root.")
        trial_ids = [int(t.strip()) for t in args.trials.split(",") if t.strip()]
        treatments = [c.strip() for c in args.treatment_conditions.split(",") if c.strip()]
        per_condition_fpr.run(args.results_root, trial_ids,
                              args.baseline_condition, treatments, args.output)
        return

    primary.run(args.trials_root, args.output, args.per_trial_csv)


if __name__ == "__main__":
    main()
