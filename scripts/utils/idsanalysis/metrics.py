"""FPR / TPR / Sep + the rolled-pod metric (the H2 denominator)."""

from __future__ import annotations

from pathlib import Path
from typing import Optional

import pandas as pd


def fpr(df: Optional[pd.DataFrame]) -> tuple[Optional[float], int]:
    if df is None or len(df) == 0:
        return None, 0
    return float(df["is_anomaly"].mean()), int(len(df))


def tpr(df: Optional[pd.DataFrame]) -> tuple[Optional[float], int]:
    if df is None or "ground_truth" not in df.columns:
        return None, 0
    malicious = df[df["ground_truth"] == "malicious"]
    if len(malicious) == 0:
        return None, 0
    return float(malicious["is_anomaly"].mean()), int(len(malicious))


def sep(df: Optional[pd.DataFrame]) -> Optional[float]:
    """Score separation: median(malicious) − median(benign)."""
    if (df is None or "ground_truth" not in df.columns
            or "anomaly_score" not in df.columns):
        return None
    mal = df[df["ground_truth"] == "malicious"]["anomaly_score"]
    ben = df[df["ground_truth"] == "benign"]["anomaly_score"]
    if len(mal) == 0 or len(ben) == 0:
        return None
    return float(mal.median() - ben.median())


def rolled_pod_uids(df: pd.DataFrame) -> set[str]:
    """pod_uids whose earliest scoring window was the warm-up phase."""
    if not {"pod_uid", "lifecycle_phase", "window_index"} <= set(df.columns):
        return set()
    first = (df.sort_values("window_index")
               .groupby("pod_uid")
               .first()
               .reset_index())
    return set(first.loc[first["lifecycle_phase"] == "running_warmup",
                         "pod_uid"].tolist())


def compute_per_rolled_pod_fpr(csv_path: Path) -> dict:
    """Compute per-rolled-pod FPR variants for a single trial CSV."""
    empty = {
        "rolled_pod_uids": [], "rolled_pod_count": 0,
        "fpr_rolled_all": None, "fpr_rolled_warmup": None,
        "n_rows_rolled_all": 0, "n_rows_rolled_warmup": 0,
        "anomalies_rolled_all": 0, "anomalies_rolled_warmup": 0,
    }
    if not csv_path.exists() or csv_path.stat().st_size == 0:
        return dict(empty)

    df = pd.read_csv(csv_path, comment="#")
    if len(df) == 0:
        return dict(empty)

    rolled_uids = sorted(rolled_pod_uids(df))
    rolled_df = df[df["pod_uid"].isin(rolled_uids)]
    warmup_df = rolled_df[rolled_df["lifecycle_phase"] == "running_warmup"]

    n_all = int(len(rolled_df))
    n_warmup = int(len(warmup_df))
    anom_all = int(rolled_df["is_anomaly"].sum()) if n_all > 0 else 0
    anom_warmup = int(warmup_df["is_anomaly"].sum()) if n_warmup > 0 else 0

    fpr_all = (anom_all / n_all) if n_all > 0 else None
    fpr_warmup = (anom_warmup / n_warmup) if n_warmup > 0 else None

    return {
        "rolled_pod_uids":         rolled_uids,
        "rolled_pod_count":        len(rolled_uids),
        "fpr_rolled_all":          fpr_all,
        "fpr_rolled_warmup":       fpr_warmup,
        "n_rows_rolled_all":       n_all,
        "n_rows_rolled_warmup":    n_warmup,
        "anomalies_rolled_all":    anom_all,
        "anomalies_rolled_warmup": anom_warmup,
    }
