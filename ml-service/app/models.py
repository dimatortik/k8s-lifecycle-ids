from typing import Optional

from pydantic import BaseModel, Field, model_validator


class FeatureWindow(BaseModel):
    """Features of one pod over one 10-second tumbling window."""

    event_rate_per_sec: float
    unique_syscalls_count: int
    unique_processes_count: int

    syscall_intensity_per_sec: float

    pod_uid: Optional[str] = None
    pod_name: Optional[str] = None
    lifecycle_phase: Optional[str] = None

    namespace: Optional[str] = None
    label: Optional[str] = None

    # False in the BLIND_* ablation conditions: skip the warmup phase rule.
    apply_phase_authoritative: bool = True

    @model_validator(mode="after")
    def _require_identity(self) -> "FeatureWindow":
        has_uid = bool(self.pod_uid)
        has_label = bool(self.namespace) and bool(self.label)
        if not has_uid and not has_label:
            raise ValueError(
                "FeatureWindow requires either pod_uid or "
                "(namespace, label) for threshold lookup."
            )
        return self


class ThresholdUpdate(BaseModel):
    """Payload for PATCH /threshold."""

    pod_uid: Optional[str] = None

    namespace: Optional[str] = None
    label: Optional[str] = None

    value: Optional[float] = None
    ttl_seconds: Optional[int] = Field(default=60, ge=1)

    @model_validator(mode="after")
    def _require_identity(self) -> "ThresholdUpdate":
        has_uid = bool(self.pod_uid)
        has_label = bool(self.namespace) and bool(self.label)
        if not has_uid and not has_label:
            raise ValueError(
                "ThresholdUpdate requires either pod_uid or "
                "(namespace, label)."
            )
        return self
