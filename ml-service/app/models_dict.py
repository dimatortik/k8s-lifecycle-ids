"""Per-service model registry with a fleet-wide fallback model (baseline.pkl)."""

from __future__ import annotations

import logging
import os
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterable

import joblib

logger = logging.getLogger(__name__)

_REPO_MODELS_DIR = Path(__file__).resolve().parents[2] / "models"
_FALLBACK_PATH = _REPO_MODELS_DIR / "baseline.pkl"
_OB_DIR = _REPO_MODELS_DIR / "onlineboutique"

_OB_SERVICES: dict[str, str] = {
    "frontend":              "frontend.pkl",
    "cartservice":           "cartservice.pkl",
    "productcatalogservice": "productcatalogservice.pkl",
    "currencyservice":       "currencyservice.pkl",
    "checkoutservice":       "checkoutservice.pkl",
    "paymentservice":        "paymentservice.pkl",
    "shippingservice":       "shippingservice.pkl",
    "emailservice":          "emailservice.pkl",
    "recommendationservice": "recommendationservice.pkl",
    "adservice":             "adservice.pkl",
    "loadgenerator":         "loadgenerator.pkl",
}


@dataclass(frozen=True)
class ModelEntry:
    """Bundles a loaded sklearn estimator with its identifying metadata."""
    key: str
    service: str
    path: Path
    model: Any
    offset: float
    is_fallback: bool


def _normalize_label(label: str | None) -> str | None:
    """Strip whitespace and lowercase the right-hand side of `app=<svc>`."""
    if not label:
        return None
    s = label.strip()
    if "=" in s:
        k, _, v = s.partition("=")
        if k.strip().lower() != "app":
            return None
        return v.strip().lower() or None
    return s.lower() or None


def _load_one(path: Path) -> Any:
    if not path.exists():
        raise FileNotFoundError(path)
    model = joblib.load(path)
    if not hasattr(model, "offset_"):
        raise RuntimeError(
            f"Model at {path} has no .offset_ attribute — not an "
            "IsolationForest, or training script is out of sync."
        )
    return model


class ModelRegistry:
    """Maps `app=<service>` to its model; unknown or missing services use the fallback."""

    def __init__(self, fallback: ModelEntry) -> None:
        self._fallback = fallback
        self._registry: dict[str, ModelEntry] = {}

    @property
    def fallback(self) -> ModelEntry:
        return self._fallback

    def register(self, entry: ModelEntry) -> None:
        self._registry[entry.key] = entry
        logger.info(
            "Registered model  key=%s path=%s offset=%.4f",
            entry.key, entry.path, entry.offset,
        )

    def select(self, label: str | None) -> ModelEntry:
        """Resolve a /score payload's `label` to a ModelEntry."""
        svc = _normalize_label(label)
        if svc is not None:
            key = f"app={svc}"
            entry = self._registry.get(key)
            if entry is not None:
                return entry
            if svc in _OB_SERVICES:
                logger.info(
                    "Per-service model missing for %s — falling back to baseline",
                    svc,
                )
        return self._fallback

    def known_keys(self) -> Iterable[str]:
        return self._registry.keys()


def build_registry(models_dir: Path | None = None) -> ModelRegistry:
    """Construct the global registry at startup."""
    base_path = _FALLBACK_PATH if models_dir is None else (models_dir / "baseline.pkl")
    fallback_model = _load_one(base_path)
    fallback_entry = ModelEntry(
        key="__fallback__", service="__fallback__",
        path=base_path, model=fallback_model,
        offset=float(fallback_model.offset_), is_fallback=True,
    )
    registry = ModelRegistry(fallback=fallback_entry)

    ob_dir = _OB_DIR if models_dir is None else (models_dir / "onlineboutique")
    if not ob_dir.exists():
        logger.info(
            "Per-service models dir absent (%s) — running fallback-only mode",
            ob_dir,
        )
        return registry

    for svc, filename in _OB_SERVICES.items():
        path = ob_dir / filename
        if not path.exists():
            continue
        try:
            mdl = _load_one(path)
        except Exception as exc:                         # noqa: BLE001
            logger.error(
                "Failed to load per-service model  svc=%s path=%s err=%s",
                svc, path, exc,
            )
            continue
        registry.register(ModelEntry(
            key=f"app={svc}", service=svc, path=path,
            model=mdl, offset=float(mdl.offset_), is_fallback=False,
        ))
    return registry


def init_registry(models_dir: Path | None = None) -> ModelRegistry:
    """Build the registry from IDS_MODELS_DIR, or from models/ next to the app."""
    override = os.environ.get("IDS_MODELS_DIR")
    if override and models_dir is None:
        models_dir = Path(override)
    return build_registry(models_dir)
