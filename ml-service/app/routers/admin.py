import logging

from fastapi import APIRouter, HTTPException

from app.models_dict import init_registry
from app.registry_state import FEATURE_COLS, set_registry
from app.state import threshold_manager

logger = logging.getLogger(__name__)
router = APIRouter()


@router.post("/admin/reload")
async def admin_reload():
    """Re-scan the models directory and rebuild the in-process registry."""
    try:
        new_registry = init_registry()
    except (FileNotFoundError, RuntimeError) as exc:
        raise HTTPException(status_code=500, detail=f"reload failed: {exc}") from exc
    fitted = getattr(new_registry.fallback.model, "feature_names_in_", None)
    if fitted is not None and list(fitted) != FEATURE_COLS:
        raise HTTPException(
            status_code=500,
            detail=f"fallback model feature mismatch: got {list(fitted)}",
        )
    set_registry(new_registry)
    keys = list(new_registry.known_keys())
    logger.info("admin/reload  per_service_models=%d  keys=%s",
                len(keys), keys)
    return {
        "status": "reloaded",
        "per_service_models_count": len(keys),
        "per_service_models": keys,
        "fallback_offset": new_registry.fallback.offset,
    }


@router.post("/admin/clear_thresholds")
async def admin_clear_thresholds():
    """Flush both the per-pod and per-label threshold tables and cancel every pending TTL task."""
    n_pod, n_label = await threshold_manager.clear_all()
    logger.info(
        "admin/clear_thresholds  cleared_pod_entries=%d  cleared_label_entries=%d",
        n_pod, n_label,
    )
    return {
        "status": "cleared",
        "cleared_pod_entries": n_pod,
        "cleared_label_entries": n_label,
    }
