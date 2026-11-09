"""ml-service FastAPI app: loads the model registry and mounts the routers."""

import logging
import os
from contextlib import asynccontextmanager

from fastapi import FastAPI

from app.models_dict import init_registry
from app.recent import clear_history
from app.registry_state import FEATURE_COLS, set_registry
from app.routers import admin, health, score, threshold
from app.scoring import _INTENSITY_RULE_THRESHOLD

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)


@asynccontextmanager
async def lifespan(app: FastAPI):
    # Threshold tables, TTL tasks and the recent-scores buffer live in process
    # memory, so the service must run as exactly one worker.
    workers = int(os.environ.get("WEB_CONCURRENCY", "1"))
    if workers != 1:
        raise RuntimeError(
            f"WEB_CONCURRENCY={workers} but ml-service requires WEB_CONCURRENCY=1 "
            "(threshold state is kept in process memory)."
        )

    registry = init_registry()
    fitted = getattr(registry.fallback.model, "feature_names_in_", None)
    if fitted is not None and list(fitted) != FEATURE_COLS:
        raise RuntimeError(
            f"Stale fallback model at {registry.fallback.path}.\n"
            f"  fitted features: {list(fitted)}\n"
            f"  expected:        {FEATURE_COLS}\n"
            f"Retrain it with scripts/experiments/train_multi_model.sh"
        )
    set_registry(registry)
    logger.info(
        "ML service ready  fallback=%s offset=%.4f per_service_models=%d",
        registry.fallback.path, registry.fallback.offset,
        sum(1 for _ in registry.known_keys()),
    )
    logger.info("intensity_rule_threshold=%s (inf = disabled)",
                _INTENSITY_RULE_THRESHOLD)
    yield
    set_registry(None)
    clear_history()


app = FastAPI(title="IDS ML Service", version="0.3.0", lifespan=lifespan)

app.include_router(health.router)
app.include_router(admin.router)
app.include_router(threshold.router)
app.include_router(score.router)
