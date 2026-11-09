"""Recent-scores ring buffer backing GET /scores/recent."""

from __future__ import annotations

import time
from collections import deque
from typing import Deque, Optional

from app.models import FeatureWindow

_SCORE_HISTORY_MAXLEN = 4096
_score_history: Deque[dict] = deque(maxlen=_SCORE_HISTORY_MAXLEN)


def record_score(ts: float, window: FeatureWindow, model_key: str,
                 raw_score: float, threshold: float, is_anomaly: bool) -> None:
    """Append to the ring buffer that backs /scores/recent."""
    _score_history.append({
        "ts": ts,
        "pod_uid": window.pod_uid or "",
        "pod_name": window.pod_name or "",
        "namespace": window.namespace or "",
        "label": window.label or "",
        "lifecycle_phase": window.lifecycle_phase or "",
        "model_key": model_key,
        "score": raw_score,
        "threshold": threshold,
        "is_anomaly": is_anomaly,
    })


def query_recent(window_seconds: float,
                 namespace: Optional[str],
                 label: Optional[str],
                 pod_uid: Optional[str],
                 limit: int) -> list[dict]:
    """Return matching entries newest-first within the look-back window."""
    cutoff = time.time() - window_seconds
    out: list[dict] = []
    for rec in reversed(_score_history):
        if rec["ts"] < cutoff:
            break
        if namespace is not None and rec["namespace"] != namespace:
            continue
        if label is not None and rec["label"] != label:
            continue
        if pod_uid is not None and rec["pod_uid"] != pod_uid:
            continue
        out.append(rec)
        if len(out) >= limit:
            break
    return out


def clear_history() -> None:
    _score_history.clear()
