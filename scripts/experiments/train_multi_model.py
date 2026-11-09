#!/usr/bin/env python3
"""Train one IsolationForest per Online Boutique service plus a fleet-wide fallback.

Reads lifecycle-tagged Tracee events on stdin, builds per-pod 10 s window
features, and writes models/onlineboutique/<service>.pkl and models/baseline.pkl.
Services with fewer than MIN_ROWS_PER_SERVICE rows fall back to baseline.pkl.

Usage:
    kubectl logs -n tracee ds/tracee -f \\
        | python3 scripts/utils/lifecycle_tagger.py \\
        | python3 scripts/experiments/train_multi_model.py
"""

from __future__ import annotations

import datetime as _dt
import json
import os
import shutil
import subprocess
import sys
from collections import defaultdict
from dataclasses import dataclass
from pathlib import Path
from typing import Optional

import joblib
import numpy as np
import pandas as pd
from sklearn.ensemble import IsolationForest
from sklearn.model_selection import train_test_split

WINDOW_SECONDS = 10
FEATURE_COLS = [
    "event_rate_per_sec",
    "unique_syscalls_count",
    "unique_processes_count",
]
HIGH_FREQ_SYSCALLS = frozenset({"futex"})

MODELS_ROOT = Path(__file__).resolve().parents[2] / "models"
OB_DIR = MODELS_ROOT / "onlineboutique"
FLEET_BASELINE_PATH      = MODELS_ROOT / "baseline.pkl"
FLEET_BASELINE_ARCHIVE   = MODELS_ROOT / "baseline.phase1-nginx.pkl"
FLEET_PROVENANCE_PATH    = MODELS_ROOT / "baseline.provenance.json"

MIN_ROWS_PER_SERVICE = int(os.environ.get("MIN_ROWS_PER_SERVICE", "30"))
TARGET_ROWS_TOTAL    = int(os.environ.get("TARGET_ROWS_TOTAL",    "1500"))
MIN_ROWS_POOLED      = int(os.environ.get("MIN_ROWS_POOLED",      "200"))

_OB_SERVICE_WHITELIST = frozenset({
    "frontend", "cartservice", "productcatalogservice",
    "currencyservice", "checkoutservice", "emailservice",
    "recommendationservice", "adservice", "paymentservice",
    "shippingservice", "loadgenerator", "redis-cart",
})


def _passes_filter(event: dict) -> bool:
    # Train on steady-state (Ready) windows only.
    if event.get("lifecycle_phase") != "running_ready":
        return False
    if not event.get("pod_uid"):
        return False
    k8s = event.get("kubernetes") or {}
    pod_ns = k8s.get("podNamespace") or ""
    if pod_ns != "default":
        return False
    return True


def _resolve_service(event: dict) -> Optional[str]:
    k8s = event.get("kubernetes") or {}
    labels = k8s.get("podLabels") or {}
    if isinstance(labels, dict) and labels.get("app"):
        svc = str(labels["app"]).strip().lower()
        return svc if svc in _OB_SERVICE_WHITELIST else None
    pod_name = k8s.get("podName") or event.get("hostName") or ""
    head = pod_name.split("-", 1)[0].strip().lower()
    return head if head in _OB_SERVICE_WHITELIST else None

import time as _time

_consumer_epoch_ns: Optional[int] = None


def _window_id_wallclock() -> int:
    # Receive-time windows, same as the scoring drivers, so features match.
    global _consumer_epoch_ns
    now_ns = _time.monotonic_ns()
    if _consumer_epoch_ns is None:
        _consumer_epoch_ns = now_ns
    return int((now_ns - _consumer_epoch_ns) // (WINDOW_SECONDS * 1_000_000_000))


@dataclass
class _PodBucket:
    """Holds the in-progress event list for one (pod_uid, service) inside the currently-open tumbling window."""
    service: str
    events: list


def _finalize_pod_window(events: list) -> dict:
    rate = len(events) / WINDOW_SECONDS
    return {
        "event_rate_per_sec":     rate,
        "unique_syscalls_count":  len({e.get("eventName") for e in events}),
        "unique_processes_count": len({e.get("processName") for e in events}),
    }


@dataclass
class _TrainResult:
    service: str
    rows: int
    offset: Optional[float]
    path: Optional[Path]
    skipped_reason: Optional[str] = None
    held_out_fpr: Optional[float] = None
    train_n: int = 0
    holdout_n: int = 0


def _train_one_service(service: str, rows: list[dict]) -> _TrainResult:
    """Train one service's IsolationForest and estimate its FPR on a 50% hold-out."""
    n = len(rows)
    if n < MIN_ROWS_PER_SERVICE:
        return _TrainResult(service=service, rows=n, offset=None, path=None,
                            skipped_reason=f"need ≥{MIN_ROWS_PER_SERVICE} rows")

    df = pd.DataFrame(rows, columns=FEATURE_COLS)
    # Tiny jitter: idle pods give near-constant integer features, and
    # zero-variance columns leave IsolationForest nothing to split on.
    rng = np.random.default_rng(42)
    for col in FEATURE_COLS:
        df[col] = df[col] + rng.uniform(-1e-3, 1e-3, size=len(df))

    if n >= 2 * MIN_ROWS_PER_SERVICE:
        train_df, holdout_df = train_test_split(
            df, test_size=0.5, random_state=42, shuffle=True,
        )
    else:
        train_df, holdout_df = df, None

    # With n < 256 every tree sees the whole training set (no sub-sampling).
    max_samples = min(256, len(train_df))
    model = IsolationForest(
        n_estimators=200,
        max_samples=max_samples,
        contamination=0.05,
        random_state=42,
    )
    model.fit(train_df)

    held_out_fpr: Optional[float] = None
    if holdout_df is not None and len(holdout_df) > 0:
        ho_scores = model.score_samples(holdout_df)
        held_out_fpr = float((ho_scores < model.offset_).mean())

    OB_DIR.mkdir(parents=True, exist_ok=True)
    path = OB_DIR / f"{service}.pkl"
    joblib.dump(model, path)
    return _TrainResult(
        service=service, rows=n,
        offset=float(model.offset_), path=path,
        held_out_fpr=held_out_fpr,
        train_n=len(train_df),
        holdout_n=len(holdout_df) if holdout_df is not None else 0,
    )


def _git_commit_short() -> str:
    """Best-effort `git rev-parse --short HEAD` for the provenance sidecar."""
    try:
        out = subprocess.check_output(
            ["git", "-C", str(MODELS_ROOT.parent),
             "rev-parse", "--short", "HEAD"],
            stderr=subprocess.DEVNULL, text=True, timeout=3,
        )
        return out.strip()
    except Exception:
        return ""


def _archive_phase1_baseline() -> None:
    """Move an existing models/baseline.pkl aside before the new fleet model overwrites it."""
    if FLEET_BASELINE_PATH.exists() and not FLEET_BASELINE_ARCHIVE.exists():
        shutil.copy2(FLEET_BASELINE_PATH, FLEET_BASELINE_ARCHIVE)
        print(f"[fleet-pool] archived existing baseline → "
              f"{FLEET_BASELINE_ARCHIVE.name}", file=sys.stderr)


@dataclass
class _PoolResult:
    rows: int
    train_n: int
    holdout_n: int
    offset: float
    held_out_fpr: Optional[float]
    services_pooled: list[str]
    path: Path


def _train_fleet_pool(
    per_service_rows: dict[str, list[dict]],
) -> Optional[_PoolResult]:
    """Train one IsolationForest on the union of all per-service rows."""
    all_rows: list[dict] = []
    services_pooled: list[str] = []
    for svc in sorted(per_service_rows):
        rows = per_service_rows[svc]
        if not rows:
            continue
        all_rows.extend(rows)
        services_pooled.append(svc)
    n = len(all_rows)
    if n < MIN_ROWS_POOLED:
        print(f"[fleet-pool] ERROR: only {n} rows pooled "
              f"(< MIN_ROWS_POOLED={MIN_ROWS_POOLED}) — refusing to emit "
              f"a degenerate fallback model.  Increase DURATION_SECS or "
              f"OB_TRAIN_REPLICAS and re-run.", file=sys.stderr)
        return None

    df = pd.DataFrame(all_rows, columns=FEATURE_COLS)
    rng = np.random.default_rng(42)
    for col in FEATURE_COLS:
        df[col] = df[col] + rng.uniform(-1e-3, 1e-3, size=len(df))

    train_df, holdout_df = train_test_split(
        df, test_size=0.5, random_state=42, shuffle=True,
    )
    max_samples = min(256, len(train_df))
    model = IsolationForest(
        n_estimators=200,
        max_samples=max_samples,
        contamination=0.05,
        random_state=42,
    )
    model.fit(train_df)
    ho_scores = model.score_samples(holdout_df)
    held_out_fpr = float((ho_scores < model.offset_).mean())

    _archive_phase1_baseline()
    MODELS_ROOT.mkdir(parents=True, exist_ok=True)
    joblib.dump(model, FLEET_BASELINE_PATH)
    return _PoolResult(
        rows=n, train_n=len(train_df), holdout_n=len(holdout_df),
        offset=float(model.offset_), held_out_fpr=held_out_fpr,
        services_pooled=services_pooled, path=FLEET_BASELINE_PATH,
    )


def _write_fleet_provenance(pool: _PoolResult) -> None:
    """Write the provenance JSON next to the fleet model."""
    training_profile = os.environ.get("TRAINING_PROFILE", "steady")
    doc = {
        "purpose": "registry-fallback-for-unregistered-OB-services",
        "workload": "online-boutique-steady" if training_profile == "steady"
                    else f"online-boutique-{training_profile}",
        "training_profile": training_profile,
        "n_rows_total": pool.rows,
        "n_train": pool.train_n,
        "n_holdout": pool.holdout_n,
        "held_out_fpr": pool.held_out_fpr,
        "offset_": pool.offset,
        "services_pooled": pool.services_pooled,
        "trained_at": _dt.datetime.now(_dt.timezone.utc).isoformat(),
        "git_commit": _git_commit_short(),
        "schema_version": "2026-05-14-ablation",
    }
    FLEET_PROVENANCE_PATH.write_text(json.dumps(doc, indent=2) + "\n")


def main() -> None:
    current_wid: Optional[int] = None
    bucket: dict[tuple[str, str], _PodBucket] = {}
    per_service_rows: dict[str, list[dict]] = defaultdict(list)
    total_rows = 0

    def close_window() -> None:
        nonlocal bucket, total_rows
        if not bucket:
            return
        for (_uid, service), pb in bucket.items():
            if not pb.events:
                continue
            feat = _finalize_pod_window(pb.events)
            per_service_rows[service].append(feat)
            total_rows += 1
        bucket = {}

    try:
        for raw in sys.stdin:
            raw = raw.strip()
            if not raw:
                continue
            try:
                event = json.loads(raw)
            except json.JSONDecodeError:
                continue

            if not _passes_filter(event):
                continue
            service = _resolve_service(event)
            if not service:
                continue

            wid = _window_id_wallclock()
            if current_wid is None:
                current_wid = wid
            if wid != current_wid:
                close_window()
                current_wid = wid

            pod_uid = event.get("pod_uid", "")
            key = (pod_uid, service)
            pb = bucket.get(key)
            if pb is None:
                pb = _PodBucket(service=service, events=[])
                bucket[key] = pb
            pb.events.append(event)

            if total_rows >= TARGET_ROWS_TOTAL:
                break

        close_window()

    except KeyboardInterrupt:
        print(f"\n[interrupted] flushing in-progress window "
              f"({total_rows} rows so far)", file=sys.stderr)
        close_window()

    if not per_service_rows:
        print("No service produced any rows. Did the tagger emit kubernetes "
              "metadata?  Exiting without writing models.", file=sys.stderr)
        sys.exit(1)

    results: list[_TrainResult] = []
    for service in sorted(per_service_rows):
        results.append(_train_one_service(service, per_service_rows[service]))

    print("\n" + "─" * 96)
    print(f"{'service':<28} {'rows':>6} {'train/ho':>10}  "
          f"{'offset_':>10}  {'held_out_FPR':>13}   path")
    print("─" * 96)
    trained = 0
    for r in results:
        if r.path is None:
            print(f"{r.service:<28} {r.rows:>6} {'-':>10}  "
                  f"{'(skipped)':>10}  {'-':>13}   {r.skipped_reason}")
            continue
        trained += 1
        try:
            rel = r.path.relative_to(Path.cwd())
        except ValueError:
            rel = r.path
        train_ho = f"{r.train_n}/{r.holdout_n}"
        ho_fpr = f"{r.held_out_fpr*100:.2f}%" if r.held_out_fpr is not None else "n<2×min"
        print(f"{r.service:<28} {r.rows:>6} {train_ho:>10}  "
              f"{r.offset:>10.4f}  {ho_fpr:>13}   {rel}")
    print("─" * 96)
    print(f"Trained {trained}/{len(results)} services. "
          f"Output dir: {OB_DIR}")
    print("Note: contamination=0.05 calibrates offset_ to flag ~5% of TRAINING")
    print("      rows.  `held_out_FPR` is the structural FPR_b on unseen idle")
    print("      traffic — the academically defensible baseline figure.")

    print("\n" + "─" * 96)
    print("Phase-2 fleet-pool: training the registry fallback `baseline.pkl`")
    print("─" * 96)
    pool = _train_fleet_pool(per_service_rows)
    if pool is None:
        sys.exit(3)
    _write_fleet_provenance(pool)
    ho_fpr_s = (f"{pool.held_out_fpr * 100:.2f}%"
                if pool.held_out_fpr is not None else "n/a")
    print(f"  rows pooled           : {pool.rows}")
    print(f"  train/holdout split   : {pool.train_n} / {pool.holdout_n}")
    print(f"  offset_               : {pool.offset:.4f}")
    print(f"  held-out FPR          : {ho_fpr_s}")
    print(f"  services pooled       : {', '.join(pool.services_pooled)}")
    print(f"  → {pool.path}")
    print(f"  → {FLEET_PROVENANCE_PATH}")
    print("─" * 96)

    if trained == 0:
        sys.exit(2)


if __name__ == "__main__":
    main()
