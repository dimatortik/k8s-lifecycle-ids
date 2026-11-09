from fastapi import APIRouter

from app.registry_state import get_registry

router = APIRouter()


@router.get("/health")
async def health():
    registry = get_registry()
    return {
        "status": "ok",
        "model_loaded": registry is not None,
        "per_service_models": (
            list(registry.known_keys()) if registry else []
        ),
    }
