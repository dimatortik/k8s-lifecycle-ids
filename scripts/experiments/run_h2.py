#!/usr/bin/env python3
"""H2 driver: score pod windows of benign traffic (steady state or rolling update).

Reads lifecycle-tagged Tracee events from stdin, aggregates them per
(pod_uid, 10 s window), POSTs each window to the ml-service /score endpoint
and writes a per-window CSV. Every anomaly is a false positive, so
FPR = anomalous windows / scored windows.

Usage:
    kubectl logs -n tracee ds/tracee -f \\
        | python3 scripts/utils/lifecycle_tagger.py \\
        | python3 scripts/experiments/run_h2.py --trial-id 1
"""

import argparse
import csv
import datetime
import json
import os
import signal
import sys
import tempfile
import time
from pathlib import Path
from typing import Dict, Optional, Tuple

import requests

sys.path.insert(0, str(Path(__file__).resolve().parent))
from _protected_filter import resolve_protected_prefixes, matches_protected  # noqa: E402

# Resolved in main() (needs kubectl), so --help works without a cluster.
_PROTECTED_PREFIXES: set[str] = set()

WINDOW_SECONDS  = 10
# Upper bound only; the orchestrator time-boxes each trial (TRIAL_MAX_SECS).
MAX_WINDOWS     = 25
ML_SERVICE_URL  = "http://localhost:8080/score"
NAMESPACE       = "default"

_REGIME_SCHEMA_VERSION = "2026-05-14-ablation"

_ROOT        = Path(__file__).resolve().parents[2]
DEFAULT_RESULTS_PATH = _ROOT / "experiments" / "results" / "h2_raw_data.csv"

_CSV_FIELDS = [
    "trial_id",
    "pod_uid", "pod_name", "lifecycle_phase", "app_label",
    "apply_phase_authoritative",
    "window_index", "window_id", "window_start_sec",
    "event_rate_per_sec", "unique_syscalls_count", "unique_processes_count",
    "syscall_intensity_per_sec",
    "anomaly_score", "threshold_used", "is_anomaly",
]

# Only used by the optional intensity rule in the ml-service (disabled).
_HIGH_FREQ_SYSCALLS = frozenset({"futex"})

_TELEMETRY_FIELDS = [
    "trial_id", "pod_uid", "window_index",
    "wall_clock_send_ts", "response_ms", "status", "error_detail",
]


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
    p.add_argument("--condition", type=str, default=None,
                   help="Trial condition label (e.g. BLIND_STEADY) recorded "
                        "in the validity JSON.  When omitted, derived from "
                        "the results-path filename stem (anything before the "
                        "trailing '_raw_data').")
    return p.parse_args()


def _passes_filter(event: dict) -> bool:
    """Admit only events from pods of the protected deployments."""
    if not event.get("pod_uid"):
        return False
    if event.get("lifecycle_phase") not in ("running_warmup", "running_ready"):
        return False
    k8s = event.get("kubernetes") or {}
    pod_name = k8s.get("podName") or event.get("hostName") or ""
    return matches_protected(pod_name, _PROTECTED_PREFIXES)


def _event_pod_name(event: dict) -> str:
    """Pod name, preferring lifecycle_tagger's `pod_name` over Tracee's `kubernetes.podName`."""
    name = event.get("pod_name")
    if name:
        return name
    k8s = event.get("kubernetes") or {}
    return k8s.get("podName") or event.get("hostName") or ""


def _event_app_label(event: dict) -> str:
    """Return the `app_label` added by lifecycle_tagger."""
    return str(event.get("app_label", "") or "").strip().lower()


_consumer_epoch_ns: Optional[int] = None


def _window_id_wallclock() -> int:
    # Windows follow receive time, not event timestamps: Tracee delivers events
    # with non-monotonic kernel timestamps, which made event-time windows jump.
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


def _csv_writer(results_path: Path):
    is_new = not results_path.exists() or results_path.stat().st_size == 0
    fh = results_path.open("a", newline="")
    if is_new:
        fh.write(f"# schema_version={_REGIME_SCHEMA_VERSION}\n")
    writer = csv.DictWriter(fh, fieldnames=_CSV_FIELDS)
    if is_new:
        writer.writeheader()
    return fh, writer


def _telemetry_writer(telemetry_path: Path):
    """Open per-trial /score telemetry CSV in overwrite mode."""
    fh = telemetry_path.open("w", newline="")
    writer = csv.DictWriter(fh, fieldnames=_TELEMETRY_FIELDS)
    writer.writeheader()
    fh.flush()
    return fh, writer


# A trial with more than 5% failed /score POSTs is marked invalid in
# <condition>_validity.json; a trial with no POSTs at all is invalid too.
_VALIDITY_ERROR_RATE_THRESHOLD = 0.05


def _atomic_write_json(payload: dict, path: Path) -> None:
    """Write *payload* as JSON to *path* atomically."""
    tmp_fd, tmp_path = tempfile.mkstemp(
        prefix=path.name + ".tmp.",
        dir=str(path.parent),
    )
    try:
        with os.fdopen(tmp_fd, "w") as fh:
            json.dump(payload, fh, indent=2)
            fh.write("\n")
            fh.flush()
            os.fsync(fh.fileno())
        os.replace(tmp_path, path)
    except Exception:
        try:
            os.unlink(tmp_path)
        except OSError:
            pass
        raise


def _write_validity(condition: str, trial_id: int,
                    counters: dict, validity_path: Path) -> None:
    """Write the per-trial /score validity verdict to a sibling JSON file."""
    total = counters.get("total", 0)
    timeouts = counters.get("timeout", 0)
    errors = counters.get("error", 0)

    if total == 0:
        _atomic_write_json({
            "condition":       condition,
            "trial_id":        trial_id,
            "total_attempted": 0,
            "timeout_count":   0,
            "error_count":     0,
            "timeout_rate":    0.0,
            "error_rate":      0.0,
            "valid":           False,
            "reason":          "no_score_attempts",
        }, validity_path)
        return

    timeout_rate = timeouts / total
    error_rate = errors / total
    valid = error_rate < _VALIDITY_ERROR_RATE_THRESHOLD
    if valid:
        reason = "ok"
    else:
        pct_threshold = _VALIDITY_ERROR_RATE_THRESHOLD * 100
        reason = f"high_error_rate ({error_rate * 100:.1f}% > {pct_threshold:.0f}%)"
    _atomic_write_json({
        "condition":       condition,
        "trial_id":        trial_id,
        "total_attempted": total,
        "timeout_count":   timeouts,
        "error_count":     errors,
        "timeout_rate":    round(timeout_rate, 6),
        "error_rate":      round(error_rate, 6),
        "valid":           valid,
        "reason":          reason,
    }, validity_path)


def _score_pod_window(pod_uid: str, pod_name: str, app_label: str, phase: str,
                      wid: int, events: list, window_index: int,
                      writer, trial_id: int, apply_phase_auth: bool,
                      telemetry_writer=None, telemetry_fh=None,
                      validity_counters: Optional[dict] = None) -> Optional[bool]:
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

    send_ts = datetime.datetime.now(datetime.timezone.utc).isoformat(timespec="milliseconds")
    t0 = time.monotonic()
    status = "ok"
    error_detail = ""
    result = None
    try:
        resp = requests.post(ML_SERVICE_URL, json=payload, timeout=5)
        resp.raise_for_status()
        result = resp.json()
    except requests.exceptions.Timeout as exc:
        status = "timeout"
        error_detail = type(exc).__name__
    except requests.exceptions.HTTPError as exc:
        status = "http_error"
        sc = exc.response.status_code if exc.response is not None else "?"
        error_detail = f"HTTP {sc}"
    except Exception as exc:
        status = "exception"
        error_detail = f"{type(exc).__name__}: {str(exc)[:80]}"
    elapsed_ms = round((time.monotonic() - t0) * 1000.0, 2)

    if telemetry_writer is not None:
        telemetry_writer.writerow({
            "trial_id":           trial_id,
            "pod_uid":            pod_uid,
            "window_index":       window_index,
            "wall_clock_send_ts": send_ts,
            "response_ms":        elapsed_ms,
            "status":             status,
            "error_detail":       error_detail,
        })
        if telemetry_fh is not None:
            telemetry_fh.flush()

    if validity_counters is not None:
        validity_counters["total"] += 1
        if status == "timeout":
            validity_counters["timeout"] += 1
        if status != "ok":
            validity_counters["error"] += 1

    if status != "ok":
        print(f"  [ERROR] ML service unreachable: {status} {error_detail} ({elapsed_ms:.0f}ms)",
              file=sys.stderr)
        return None

    score      = result["score"]
    threshold  = result["threshold"]
    is_anomaly = result["is_anomaly"]

    flag = "*** ANOMALY ***" if is_anomaly else "ok"
    label_disp = label if label else "(fallback)"
    print(
        f"W{window_index:>2} pod={pod_name[:18]:<18} phase={phase[:14]:<14} "
        f"label={label_disp[:24]:<24} rate={features['event_rate_per_sec']:.2f}/s  "
        f"score={score:.4f}  θ={threshold:.4f}  | {flag}"
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
    })
    return bool(is_anomaly)


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

    print(f"Trial ID: {trial_id}")
    print(f"Results will be appended to {results_path}")
    print(f"apply_phase_authoritative = {apply_phase_auth}")
    print(f"Scoring up to {MAX_WINDOWS} windows ({MAX_WINDOWS * WINDOW_SECONDS}s) ...\n")

    results_path.parent.mkdir(parents=True, exist_ok=True)
    csv_fh, writer = _csv_writer(results_path)

    stem = results_path.stem
    if stem.endswith("_raw_data"):
        stem = stem[:-len("_raw_data")]
    telemetry_path = results_path.with_name(f"{stem}_score_telemetry.csv")
    validity_path  = results_path.with_name(f"{stem}_validity.json")
    telemetry_fh, telemetry_writer = _telemetry_writer(telemetry_path)
    print(f"Per-POST telemetry: {telemetry_path}")

    condition = args.condition or stem
    validity_counters: dict = {"total": 0, "timeout": 0, "error": 0}
    _validity_written = [False]

    def _do_write_validity() -> None:
        if _validity_written[0]:
            return
        _validity_written[0] = True
        try:
            _write_validity(condition, trial_id, validity_counters, validity_path)
        except Exception as exc:
            print(f"  [WARN] could not write validity.json: {exc}",
                  file=sys.stderr)

    # The orchestrator stops the trial with SIGTERM; write the verdict first.
    def _handle_sigterm(signum, frame):
        _do_write_validity()
        sys.exit(0)

    signal.signal(signal.SIGTERM, _handle_sigterm)

    current_wid: Optional[int] = None
    bucket: Dict[str, Tuple[str, str, str, list]] = {}
    window_index       = 0
    rows_scored        = 0
    rows_anomalous     = 0
    rows_no_app_label  = 0
    a1_warmup_wins     = 0

    def close_window() -> None:
        nonlocal bucket, window_index, rows_scored, rows_anomalous, rows_no_app_label, a1_warmup_wins
        if not bucket:
            return
        window_index += 1
        for pod_uid, (pod_name, app_label, last_phase, events) in bucket.items():
            if not events:
                continue
            # "Warmup wins": a window that contains any warmup event is labelled
            # warmup. Go services become Ready within 1-2 s, so the Ready
            # transition usually falls inside the pod's first window.
            phases_seen = {ev.get("lifecycle_phase") for ev in events}
            if "running_warmup" in phases_seen:
                phase = "running_warmup"
                a1_warmup_wins += 1
            else:
                phase = last_phase
            anomaly = _score_pod_window(
                pod_uid, pod_name, app_label, phase, current_wid,
                events, window_index, writer, trial_id, apply_phase_auth,
                telemetry_writer=telemetry_writer, telemetry_fh=telemetry_fh,
                validity_counters=validity_counters,
            )
            csv_fh.flush()
            if anomaly is None:
                continue
            rows_scored += 1
            if anomaly:
                rows_anomalous += 1
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
                # The first event of a pod may arrive before the tagger knows
                # its labels; take the label from a later event if needed.
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

    if rows_scored == 0:
        _do_write_validity()
        print("\nNo pod-windows scored.", file=sys.stderr)
        sys.exit(1)

    fpr_s = rows_anomalous / rows_scored
    no_label_pct = 100.0 * rows_no_app_label / rows_scored
    print(f"\n{'─' * 60}")
    print(f"H2 RESULT — Rolling Update (per-pod, apply_phase_auth={apply_phase_auth})")
    print(f"  Windows observed       : {window_index}")
    print(f"  Pod-windows scored     : {rows_scored}")
    print(f"  Anomalous rows         : {rows_anomalous}")
    print(f"  FPR                    : {fpr_s:.4f}  ({fpr_s * 100:.1f}%)")
    print(f"  Rows w/ empty app_label: {rows_no_app_label}  ({no_label_pct:.1f}%)")
    print(f"  Warmup-wins applied    : {a1_warmup_wins} windows")
    print(f"{'─' * 60}")

    _do_write_validity()

    csv_fh.close()
    telemetry_fh.close()
    print(f"\nRaw data appended to {results_path}")
    print(f"Score telemetry written to {telemetry_path}")
    print(f"Validity verdict written to {validity_path}")


if __name__ == "__main__":
    main()
