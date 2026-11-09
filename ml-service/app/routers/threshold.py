import logging

from fastapi import APIRouter
from fastapi.responses import JSONResponse

from app.models import ThresholdUpdate
from app.state import threshold_manager

logger = logging.getLogger(__name__)
router = APIRouter()


@router.patch("/threshold")
async def update_threshold(update: ThresholdUpdate):
    """Set or clear a threshold override for one pod (pod_uid) or one label."""
    by_pod = bool(update.pod_uid)

    if update.value is None:
        if by_pod:
            await threshold_manager.restore_pod_threshold(update.pod_uid)
            logger.info("Threshold restored  pod_uid=%s", update.pod_uid)
            return {"status": "restored", "pod_uid": update.pod_uid}
        else:
            await threshold_manager.restore_label_threshold(update.namespace, update.label)
            logger.info("Threshold restored  ns=%s label=%s",
                        update.namespace, update.label)
            return {"status": "restored",
                    "namespace": update.namespace, "label": update.label}

    ttl = update.ttl_seconds if update.ttl_seconds is not None else 60
    if by_pod:
        fresh = await threshold_manager.raise_pod_threshold(
            update.pod_uid, update.value, ttl
        )
        payload = {
            "status": "raised", "pod_uid": update.pod_uid,
            "value": update.value, "ttl_seconds": ttl,
        }
        if not fresh:
            logger.warning("409 conflict, overwriting  pod_uid=%s", update.pod_uid)
            payload["status"] = "conflict_overwritten"
            return JSONResponse(status_code=409, content=payload)
        logger.info("Threshold raised  pod_uid=%s value=%.4f",
                    update.pod_uid, update.value)
        return payload

    fresh = await threshold_manager.raise_label_threshold(
        update.namespace, update.label, update.value, ttl
    )
    payload = {
        "status": "raised", "namespace": update.namespace,
        "label": update.label, "value": update.value, "ttl_seconds": ttl,
    }
    if not fresh:
        logger.warning("409 conflict, overwriting  ns=%s label=%s",
                       update.namespace, update.label)
        payload["status"] = "conflict_overwritten"
        return JSONResponse(status_code=409, content=payload)
    logger.info("Threshold raised  ns=%s label=%s value=%.4f",
                update.namespace, update.label, update.value)
    return payload
