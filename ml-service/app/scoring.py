"""Scoring core: optional intensity rule + per-service IsolationForest."""

from __future__ import annotations

import logging
import os
import time

import pandas as pd

from app.models import FeatureWindow
from app.recent import record_score
from app.registry_state import FEATURE_COLS
from app.state import threshold_manager

logger = logging.getLogger(__name__)

# Optional rule-based guard; disabled by default (inf) and in all experiments.
_INTENSITY_RULE_THRESHOLD = float(
    os.environ.get("IDS_INTENSITY_RULE_THRESHOLD", "inf")
)
_INTENSITY_SENTINEL_SCORE = -9.99
# Same value as the controller's --high-threshold (controller/cmd/main.go).
_WARMUP_THRESHOLD = -0.95


def score_window(window: FeatureWindow, entry) -> dict:
    """Score one window against `entry` (already selected from the registry)."""
    # Threshold precedence:
    #   1. phase rule: a running_warmup window gets -0.95, unless the caller
    #      disables it (apply_phase_authoritative=false in the BLIND_* conditions);
    #   2. per-pod override set by the controller (PATCH /threshold);
    #   3. per-label override;
    #   4. the model's own offset_.
    if window.apply_phase_authoritative and window.lifecycle_phase == "running_warmup":
        current_threshold = _WARMUP_THRESHOLD
        phase_auth_fired = True
    else:
        current_threshold = threshold_manager.get_threshold(
            window.pod_uid, window.namespace, window.label,
            model_default=entry.offset,
        )
        phase_auth_fired = False

    if window.syscall_intensity_per_sec > _INTENSITY_RULE_THRESHOLD:
        logger.warning(
            "INTENSITY RULE FIRED  intensity=%.2f/s > %.1f  pod_uid=%s ns=%s label=%s"
            " model=%s apply_phase_auth=%s phase_auth_fired=%s — forced anomaly",
            window.syscall_intensity_per_sec, _INTENSITY_RULE_THRESHOLD,
            window.pod_uid or "", window.namespace or "", window.label or "",
            entry.key, window.apply_phase_authoritative, phase_auth_fired,
        )
        record_score(time.time(), window, entry.key,
                     _INTENSITY_SENTINEL_SCORE, current_threshold, True)
        return {
            "score": _INTENSITY_SENTINEL_SCORE,
            "threshold": current_threshold,
            "is_anomaly": True,
            "model": entry.key,
            "phase_auth_fired": phase_auth_fired,
        }

    df = pd.DataFrame(
        [[
            window.event_rate_per_sec,
            window.unique_syscalls_count,
            window.unique_processes_count,
        ]],
        columns=FEATURE_COLS,
    )
    # Lower score = more anomalous.
    raw_score = float(entry.model.score_samples(df)[0])
    is_anomaly = raw_score < current_threshold

    record_score(time.time(), window, entry.key,
                 raw_score, current_threshold, is_anomaly)

    logger.info(
        "IF scored  model=%s pod_uid=%s ns=%s label=%s phase=%s "
        "score=%.4f threshold=%.4f anomaly=%s apply_phase_auth=%s "
        "phase_auth_fired=%s",
        entry.key, window.pod_uid or "", window.namespace or "",
        window.label or "", window.lifecycle_phase or "",
        raw_score, current_threshold, is_anomaly,
        window.apply_phase_authoritative, phase_auth_fired,
    )
    return {
        "score": raw_score,
        "threshold": current_threshold,
        "is_anomaly": is_anomaly,
        "model": entry.key,
        "phase_auth_fired": phase_auth_fired,
    }
