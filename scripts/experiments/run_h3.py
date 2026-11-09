#!/usr/bin/env python3
"""H3 driver: score pod windows during a rolling update with a concurrent attack.

Reads lifecycle-tagged Tracee events from stdin, aggregates them per
(pod_uid, 10 s window), POSTs each window to the ml-service /score endpoint
and writes a per-window CSV. Windows of the attacker pod are ground-truth
malicious; TPR = TP / (TP + FN) over those windows.

Usage:
    kubectl logs -n tracee ds/tracee -f \\
        | python3 scripts/utils/lifecycle_tagger.py \\
        | python3 scripts/experiments/run_h3.py --trial-id 1
"""

import argparse
import csv
import json
import os
import sys
import time
from pathlib import Path
from typing import Dict, Optional, Tuple

import requests

sys.path.insert(0, str(Path(__file__).resolve().parent))
from _protected_filter import resolve_protected_prefixes, matches_protected  # noqa: E402

# Resolved in main() (needs kubectl), so --help works without a cluster.
_PROTECTED_PREFIXES: set[str] = set()

WINDOW_SECONDS   = 10
MAX_WINDOWS      = 25
INJECT_AT_WINDOW = 5
ML_SERVICE_URL   = "http://localhost:8080/score"
NAMESPACE        = "default"

_REGIME_SCHEMA_VERSION = "2026-05-14-ablation"

_ROOT           = Path(__file__).resolve().parents[2]
DEFAULT_RESULTS_PATH = _ROOT / "experiments" / "results" / "h3_raw_data.csv"
ATTACK_MANIFEST = _ROOT / "k8s" / "scenarios" / "exfil-attacker.yaml"

_DEFAULT_ATTACKERS = "exfil-attacker,token-attacker,rshell-attacker"
ATTACKER_POD_NAMES: set[str] = {
    n.strip() for n in os.environ.get(
        "ATTACKER_POD_NAMES", _DEFAULT_ATTACKERS,
    ).split(",") if n.strip()
}
ATTACKER_POD_NAME = next(iter(ATTACKER_POD_NAMES), "exfil-attacker")

_CSV_FIELDS = [
    "trial_id",
    "pod_uid", "pod_name", "lifecycle_phase", "app_label",
    "apply_phase_authoritative",
    "window_index", "window_id", "window_start_sec",
    "event_rate_per_sec", "unique_syscalls_count", "unique_processes_count",
    "syscall_intensity_per_sec", "anomaly_score", "threshold_used",
    "is_anomaly", "ground_truth",
]

# Only used by the optional intensity rule in the ml-service (disabled).
_HIGH_FREQ_SYSCALLS = frozenset({"futex"})


def _parse_bool_arg(s: str) -> bool:
    """Parse "true" / "false" (case-insensitive) for argparse."""
    return s.strip().lower() != "false"


def _parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--trial-id", type=int, default=0,
                   help="Trial number for N=10 statistical runs (default: 0 = ad-hoc).")
    p.add_argument("--results-path", type=Path, default=None,
                   help=f"CSV output path (default: {DEFAULT_RESULTS_PATH}).")
    p.add_argument("--apply-phase-authoritative",
                   dest="apply_phase_auth",
                   type=_parse_bool_arg, default=True,
                   help="When false, /score skips the phase-authoritative "
                        "branch (BLIND_* conditions). Default: true.")
    return p.parse_args()


def _event_pod_name(event: dict) -> str:
    """Pod name, preferring lifecycle_tagger's `pod_name` over Tracee's `kubernetes.podName`.

    Tracee leaves `kubernetes.podName` empty for the first seconds of a new pod;
    the tagger resolves it from the container ID right away.
    """
    name = event.get("pod_name")
    if name:
        return name
    k8s = event.get("kubernetes") or {}
    return k8s.get("podName") or event.get("hostName") or ""


def _event_app_label(event: dict) -> str:
    """Return the `app_label` added by lifecycle_tagger."""
    return str(event.get("app_label", "") or "").strip().lower()


def _passes_filter(event: dict) -> bool:
    """Admit events from the attacker pod and from protected-deployment pods."""
    pod_name = _event_pod_name(event)
    # Checked before pod_uid/phase: a fresh attacker pod's first events can
    # arrive before the tagger's watch knows it (pod_uid="", phase unknown).
    # The attacker is not protected, so it never gets warmup suppression.
    if pod_name in ATTACKER_POD_NAMES:
        return True
    if not event.get("pod_uid"):
        return False
    if event.get("lifecycle_phase") not in ("running_warmup", "running_ready"):
        return False
    return matches_protected(pod_name, _PROTECTED_PREFIXES)


_consumer_epoch_ns: Optional[int] = None


def _window_id_wallclock() -> int:
    # Receive-time windows, same as run_h2.py.
    global _consumer_epoch_ns
    now_ns = time.monotonic_ns()
    if _consumer_epoch_ns is None:
        _consumer_epoch_ns = now_ns
    return int((now_ns - _consumer_epoch_ns) // (WINDOW_SECONDS * 1_000_000_000))


def _extract_features(events: list) -> dict:
    return {
        "event_rate_per_sec":        len(events) / WINDOW_SECONDS,
        "unique_syscalls_count":     len({e.get("eventName")   for e in events}),
        "unique_processes_count":    len({e.get("processName") for e in events}),
        "syscall_intensity_per_sec": sum(1 for e in events if e.get("eventName") in _HIGH_FREQ_SYSCALLS) / WINDOW_SECONDS,
    }


import subprocess  # noqa: E402  — used only by the non-blocking fallback


def _inject_attack() -> None:
    if os.environ.get("SKIP_INTERNAL_INJECT") == "1":
        print(f"\n[H3] >>> Window {INJECT_AT_WINDOW} reached.  "
              "Injection is handled by the orchestrator (SKIP_INTERNAL_INJECT=1).")
        return

    cmd = ["kubectl", "apply", "-f", str(ATTACK_MANIFEST)]
    print(f"\n[H3] >>> Attack injection at Window {INJECT_AT_WINDOW}: {' '.join(cmd)}")
    try:
        subprocess.Popen(
            cmd,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            start_new_session=True,
        )
    except Exception as exc:
        print(f"[H3] WARNING: attack injection spawn failed: {exc}", file=sys.stderr)


def _csv_writer(results_path: Path):
    is_new = not results_path.exists() or results_path.stat().st_size == 0
    fh = results_path.open("a", newline="")
    if is_new:
        fh.write(f"# schema_version={_REGIME_SCHEMA_VERSION}\n")
    writer = csv.DictWriter(fh, fieldnames=_CSV_FIELDS)
    if is_new:
        writer.writeheader()
    return fh, writer


def _ground_truth_for(pod_name: str) -> str:
    # Ground truth by pod: every window of an attacker pod is malicious.
    return "malicious" if pod_name in ATTACKER_POD_NAMES else "benign"


def _score_pod_window(pod_uid: str, pod_name: str, app_label: str, phase: str,
                      wid: int, events: list, window_index: int,
                      writer, trial_id: int, apply_phase_auth: bool) -> Tuple[Optional[bool], str]:
    features = _extract_features(events)

    label = f"app={app_label}" if app_label else ""

    payload = {
        "pod_uid":                     pod_uid,
        "pod_name":                    pod_name,
        "lifecycle_phase":             phase,
        "namespace":                   NAMESPACE,
        "label":                       label,
        "apply_phase_authoritative":   apply_phase_auth,
        "event_rate_per_sec":          features["event_rate_per_sec"],
        "unique_syscalls_count":       features["unique_syscalls_count"],
        "unique_processes_count":      features["unique_processes_count"],
        "syscall_intensity_per_sec":   features["syscall_intensity_per_sec"],
    }
    try:
        resp = requests.post(ML_SERVICE_URL, json=payload, timeout=5)
        resp.raise_for_status()
        result = resp.json()
    except Exception as exc:
        print(f"  [ERROR] ML service unreachable: {exc}", file=sys.stderr)
        return None, _ground_truth_for(pod_name)

    score      = result["score"]
    threshold  = result["threshold"]
    is_anomaly = result["is_anomaly"]
    gt         = _ground_truth_for(pod_name)

    flag   = "*** ANOMALY ***" if is_anomaly else "ok"
    gt_tag = f" [{gt.upper()}]"
    label_disp = label if label else "(fallback)"
    print(
        f"W{window_index:>2} pod={pod_name[:18]:<18} phase={phase[:14]:<14} "
        f"label={label_disp[:24]:<24} rate={features['event_rate_per_sec']:.2f}/s  "
        f"score={score:.4f}  θ={threshold:.4f}{gt_tag}  | {flag}"
    )

    writer.writerow({
        "trial_id":                  trial_id,
        "pod_uid":                   pod_uid,
        "pod_name":                  pod_name,
        "lifecycle_phase":           phase,
        "app_label":                 app_label,
        "apply_phase_authoritative": int(apply_phase_auth),
        "window_index":              window_index,
        "window_id":                 wid,
        "window_start_sec":          wid * WINDOW_SECONDS,
        "event_rate_per_sec":        features["event_rate_per_sec"],
        "unique_syscalls_count":     features["unique_syscalls_count"],
        "unique_processes_count":    features["unique_processes_count"],
        "syscall_intensity_per_sec": features["syscall_intensity_per_sec"],
        "anomaly_score":             round(float(score), 6),
        "threshold_used":            round(float(threshold), 6),
        "is_anomaly":                int(is_anomaly),
        "ground_truth":              gt,
    })
    return bool(is_anomaly), gt


def main() -> None:
    global _PROTECTED_PREFIXES
    args = _parse_args()
    _PROTECTED_PREFIXES = resolve_protected_prefixes()
    trial_id = args.trial_id
    apply_phase_auth = bool(args.apply_phase_auth)
    results_path = args.results_path or DEFAULT_RESULTS_PATH

    health = None
    last_exc = None
    for attempt in range(1, 7):
        try:
            health = requests.get("http://localhost:8080/health", timeout=10)
            health.raise_for_status()
            print(f"ML service healthy (attempt {attempt}): {health.json()}")
            break
        except Exception as exc:
            last_exc = exc
            backoff = min(30, 5 * attempt)
            print(f"  [WARN] ML service unreachable (attempt {attempt}/6): "
                  f"{type(exc).__name__}: {exc} — retrying in {backoff}s",
                  file=sys.stderr)
            time.sleep(backoff)
    if health is None:
        print(f"ERROR: ML service not reachable at localhost:8080 after 6 attempts. "
              f"Last error: {last_exc}", file=sys.stderr)
        print("Expose it on localhost:8080, e.g. kubectl port-forward -n ids-system svc/ml-service 8080:8080", file=sys.stderr)
        sys.exit(1)

    if not ATTACK_MANIFEST.exists():
        print(f"ERROR: attack manifest not found at {ATTACK_MANIFEST}", file=sys.stderr)
        sys.exit(1)

    print(f"Trial ID: {trial_id}")
    print(f"Results will be written to {results_path}")
    print(f"apply_phase_authoritative = {apply_phase_auth}")
    print(f"Scoring up to {MAX_WINDOWS} windows ({MAX_WINDOWS * WINDOW_SECONDS}s) ...")
    print(f"Attacker pod injection at Window {INJECT_AT_WINDOW} (or by the orchestrator)\n")

    results_path.parent.mkdir(parents=True, exist_ok=True)
    csv_fh, writer = _csv_writer(results_path)

    current_wid: Optional[int] = None
    bucket: Dict[str, Tuple[str, str, str, list]] = {}
    window_index      = 0
    attack_injected   = False
    true_positives    = 0
    false_negatives   = 0
    rows_no_app_label = 0

    def close_window() -> None:
        nonlocal bucket, window_index, attack_injected
        nonlocal true_positives, false_negatives, rows_no_app_label
        if not bucket:
            return
        window_index += 1
        if window_index == INJECT_AT_WINDOW and not attack_injected:
            _inject_attack()
            attack_injected = True
        # Unlike run_h2.py, a window takes the phase of its last event.
        for pod_uid, (pod_name, app_label, last_phase, events) in bucket.items():
            if not events:
                continue
            detected, gt = _score_pod_window(
                pod_uid, pod_name, app_label, last_phase, current_wid,
                events, window_index, writer, trial_id, apply_phase_auth,
            )
            csv_fh.flush()
            if detected is None:
                continue
            if gt == "malicious":
                if detected:
                    true_positives += 1
                else:
                    false_negatives += 1
            if not app_label:
                rows_no_app_label += 1
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
            wid = _window_id_wallclock()
            if current_wid is None:
                current_wid = wid
            if wid != current_wid:
                close_window()
                current_wid = wid

            pod_uid = event["pod_uid"]
            pod_name = _event_pod_name(event)
            app_label = _event_app_label(event)
            phase = event.get("lifecycle_phase", "unknown")
            existing = bucket.get(pod_uid)
            if existing is None:
                bucket[pod_uid] = (pod_name, app_label, phase, [event])
            else:
                ex_name, ex_label, _ex_phase, ex_events = existing
                merged_label = ex_label if ex_label else app_label
                bucket[pod_uid] = (ex_name, merged_label, phase,
                                   ex_events + [event])

            if window_index >= MAX_WINDOWS:
                break
        if window_index < MAX_WINDOWS:
            close_window()
    except KeyboardInterrupt:
        print(f"\nInterrupted after {window_index} complete windows.")
        close_window()

    total_malicious = true_positives + false_negatives
    tpr_s = true_positives / total_malicious if total_malicious > 0 else 0.0

    if window_index == 0:
        print("\nNo pod-windows scored.", file=sys.stderr)
        sys.exit(1)

    print(f"\n{'─' * 70}")
    print(f"H3 — Malicious Pod-Window Scores (rows where pod_name='{ATTACKER_POD_NAME}'):")
    print(f"  (Re-read from {results_path})")
    try:
        with open(results_path, newline="") as fh:
            rows = list(csv.DictReader(line for line in fh if not line.startswith("#")))
        attack_rows = [r for r in rows
                       if r.get("ground_truth") == "malicious"
                       and int(r.get("trial_id", 0)) == trial_id]
        for r in attack_rows:
            flag = "DETECTED" if int(r["is_anomaly"]) else "missed"
            print(
                f"  W{int(r['window_index']):>2}: pod={r['pod_name'][:20]:<20} "
                f"score={float(r['anomaly_score']):.4f}  "
                f"θ={float(r['threshold_used']):.4f}  "
                f"→ {flag}"
            )
    except Exception as exc:
        print(f"  (could not re-read CSV: {exc})")

    print(f"\n{'─' * 70}")
    print(f"H3 RESULT — Rolling Update + Concurrent Attack (per-pod, apply_phase_auth={apply_phase_auth})")
    print(f"  Windows observed        : {window_index}")
    print(f"  Malicious pod-windows   : {total_malicious}")
    print(f"  True positives (TP)     : {true_positives}")
    print(f"  False negatives (FN)    : {false_negatives}")
    print(f"  TPR_s                   : {tpr_s:.4f}  ({tpr_s * 100:.1f}%)")
    print(f"  Rows w/ empty app_label : {rows_no_app_label}")
    print(f"  Note: ground truth tagged via pod_name='{ATTACKER_POD_NAME}' on")
    print(f"        per-pod feature vectors (strict TP/FN attribution).")
    print(f"{'─' * 70}")

    if tpr_s >= 0.90:
        print(f"H3 SUPPORTED: controller suppression did not mask the exfil attack.")
        print(f"  ΔTPR ≤ 10%  (TPR_s = {tpr_s * 100:.1f}%)")
    else:
        print(f"H3 NOT SUPPORTED: TPR_s = {tpr_s * 100:.1f}% — suppression masked attack detections.")

    csv_fh.close()
    print(f"\nRaw data written to {results_path}")


if __name__ == "__main__":
    main()
