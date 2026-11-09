import time
from typing import Optional

from fastapi import APIRouter, HTTPException, Query

from app.models import FeatureWindow
from app.recent import _SCORE_HISTORY_MAXLEN, query_recent
from app.registry_state import get_registry
from app.scoring import score_window

router = APIRouter()


@router.post("/score")
async def score(window: FeatureWindow):
    """Score one feature window and return score, threshold used and verdict."""
    registry = get_registry()
    if registry is None:
        raise HTTPException(status_code=503, detail="Model not loaded")
    entry = registry.select(window.label)
    return score_window(window, entry)


@router.get("/scores/recent")
async def scores_recent(
    window_seconds: float = Query(
        10.0, gt=0, le=300,
        description="Look-back window in seconds (default 10).",
    ),
    namespace: Optional[str] = Query(
        None, description="Filter by namespace (exact match)."),
    label: Optional[str] = Query(
        None, description="Filter by label (exact match, e.g. 'app=frontend')."),
    pod_uid: Optional[str] = Query(
        None, description="Filter by pod_uid (exact match)."),
    limit: int = Query(
        500, gt=0, le=_SCORE_HISTORY_MAXLEN,
        description="Hard cap on number of returned entries."),
):
    """Return the most recent `/score` results, filtered by Online Boutique identity fields."""
    if get_registry() is None:
        raise HTTPException(status_code=503, detail="Model not loaded")

    out = query_recent(window_seconds, namespace, label, pod_uid, limit)

    argmax_rec = min(out, key=lambda r: r["score"]) if out else None

    return {
        "window_seconds": window_seconds,
        "now": time.time(),
        "count": len(out),
        "argmax": argmax_rec,
        "scores": out,
    }
