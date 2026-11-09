"""Live model-registry holder + the IF feature contract."""

from __future__ import annotations

from typing import Optional

from app.models_dict import ModelRegistry

FEATURE_COLS = [
    "event_rate_per_sec",
    "unique_syscalls_count",
    "unique_processes_count",
]

_registry: Optional[ModelRegistry] = None


def get_registry() -> Optional[ModelRegistry]:
    return _registry


def set_registry(registry: Optional[ModelRegistry]) -> None:
    global _registry
    _registry = registry
