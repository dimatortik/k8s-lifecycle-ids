"""Re-score every published window offline and check it against the campaign CSVs.

For each row of every <condition>_raw_data.csv: select the model the ml-service
would select (by app label), recompute score_samples on the stored features,
and check that
  - the score equals `anomaly_score` (6 decimals),
  - `threshold_used` is explained: phase rule (-0.95 on a warmup window), the
    model's offset_, or -0.95 from the controller's post-Ready hold (only in
    conditions where the controller runs),
  - `is_anomaly` equals score < threshold.

Usage:
    python3 scripts/utils/replay_scores.py experiments/results/main_campaign_phase1_20260527-011238 \\
        experiments/results/main_campaign_phase2_20260527-094911
"""

from __future__ import annotations

import argparse
import csv
import sys
from collections import Counter, defaultdict
from pathlib import Path

import pandas as pd

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "ml-service"))
from app.models_dict import init_registry  # noqa: E402
from app.registry_state import FEATURE_COLS  # noqa: E402
from app.scoring import _WARMUP_THRESHOLD  # noqa: E402

CONTROLLER_OFF = {"BLIND_STEADY", "BLIND_ROLLOUT", "PHASE_ROLLOUT"}


def _rows(path: Path) -> list[dict]:
    with path.open() as fh:
        return list(csv.DictReader(line for line in fh if not line.startswith("#")))


def main() -> int:
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("campaigns", nargs="+", type=Path)
    p.add_argument("--models-dir", type=Path, default=ROOT / "models")
    args = p.parse_args()

    registry = init_registry(args.models_dir)
    counts: Counter = Counter()
    problems: list[str] = []

    for campaign in args.campaigns:
        for csv_path in sorted(campaign.glob("trial_*/*_raw_data.csv")):
            condition = csv_path.name.removesuffix("_raw_data.csv")
            by_label: dict[str, list[dict]] = defaultdict(list)
            for r in _rows(csv_path):
                by_label[f"app={r['app_label']}" if r["app_label"] else ""].append(r)
            for label, rows in by_label.items():
                entry = registry.select(label)
                X = pd.DataFrame([[float(r[c]) for c in FEATURE_COLS] for r in rows], columns=FEATURE_COLS)
                for r, s in zip(rows, entry.model.score_samples(X)):
                    counts["windows"] += 1
                    score = round(float(s), 6)
                    threshold = float(r["threshold_used"])
                    where = f"{csv_path.relative_to(campaign.parent)} window {r['window_index']} pod {r['pod_name']}"
                    if score != float(r["anomaly_score"]):
                        problems.append(f"score {score} != {r['anomaly_score']}: {where}")
                    phase_rule = r["apply_phase_authoritative"] == "1" and r["lifecycle_phase"] == "running_warmup"
                    if phase_rule and threshold == _WARMUP_THRESHOLD:
                        counts["threshold: phase rule"] += 1
                    elif not phase_rule and abs(threshold - round(entry.offset, 6)) < 1e-6:
                        counts["threshold: model offset_"] += 1
                    elif threshold == _WARMUP_THRESHOLD and condition not in CONTROLLER_OFF:
                        counts["threshold: controller hold"] += 1
                    else:
                        problems.append(f"unexplained threshold {threshold}: {where}")
                    if (score < threshold) != (r["is_anomaly"] == "1"):
                        problems.append(f"verdict mismatch: {where}")

    for k, v in counts.items():
        print(f"{k:28s} {v:>7d}")
    if problems:
        print(f"\n{len(problems)} problem(s):")
        for line in problems[:20]:
            print("  " + line)
        return 1
    print("\nOK: every published window re-scores identically")
    return 0


if __name__ == "__main__":
    sys.exit(main())
