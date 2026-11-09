import asyncio
import logging
from typing import Dict, Optional, Tuple

logger = logging.getLogger(__name__)

_LabelKey = Tuple[str, str]
_PodKey = str


class ThresholdManager:
    """Threshold overrides keyed by pod_uid (used by the controller) or by (namespace, label).

    Every override carries a TTL and expires on its own, so a crashed controller
    cannot leave a pod suppressed forever.
    """

    DEFAULT_THRESHOLD: float = -0.5645

    def __init__(self) -> None:
        self._pod_thresholds: Dict[_PodKey, float] = {}
        self._pod_tasks: Dict[_PodKey, asyncio.Task] = {}
        self._label_thresholds: Dict[_LabelKey, float] = {}
        self._label_tasks: Dict[_LabelKey, asyncio.Task] = {}
        self._lock: Optional[asyncio.Lock] = None

    @property
    def _get_lock(self) -> asyncio.Lock:
        if self._lock is None:
            self._lock = asyncio.Lock()
        return self._lock

    def get_threshold(
        self,
        pod_uid: Optional[str],
        namespace: Optional[str],
        label: Optional[str],
        model_default: Optional[float] = None,
    ) -> float:
        """Per-pod override, then per-label override, then the model's own threshold."""
        if pod_uid:
            v = self._pod_thresholds.get(pod_uid)
            if v is not None:
                return v
        if namespace and label:
            v = self._label_thresholds.get((namespace, label))
            if v is not None:
                return v
        return model_default if model_default is not None else self.DEFAULT_THRESHOLD

    async def raise_pod_threshold(
        self, pod_uid: str, value: float, ttl_seconds: int
    ) -> bool:
        """Set a per-pod override; returns False if one already existed."""
        async with self._get_lock:
            conflict = pod_uid in self._pod_thresholds
            existing = self._pod_tasks.pop(pod_uid, None)
            if existing:
                existing.cancel()
            self._pod_thresholds[pod_uid] = value
            self._pod_tasks[pod_uid] = asyncio.create_task(
                self._ttl_restore_pod(pod_uid, ttl_seconds)
            )
        if conflict:
            logger.warning(
                "Conflicting pod suppression overwritten",
                extra={"pod_uid": pod_uid, "new_value": value},
            )
        return not conflict

    async def restore_pod_threshold(self, pod_uid: str) -> None:
        """Clear the per-pod entry (idempotent)."""
        async with self._get_lock:
            t = self._pod_tasks.pop(pod_uid, None)
            if t:
                t.cancel()
            self._pod_thresholds.pop(pod_uid, None)
        logger.info("Pod threshold restored", extra={"pod_uid": pod_uid})

    async def _ttl_restore_pod(self, pod_uid: _PodKey, ttl_seconds: int) -> None:
        try:
            await asyncio.sleep(ttl_seconds)
            async with self._get_lock:
                self._pod_thresholds.pop(pod_uid, None)
                self._pod_tasks.pop(pod_uid, None)
            logger.info(
                "TTL expired — pod threshold auto-restored",
                extra={"pod_uid": pod_uid, "ttl_seconds": ttl_seconds},
            )
        except asyncio.CancelledError:
            pass

    async def raise_label_threshold(
        self, namespace: str, label: str, value: float, ttl_seconds: int
    ) -> bool:
        key = (namespace, label)
        async with self._get_lock:
            conflict = key in self._label_thresholds
            existing = self._label_tasks.pop(key, None)
            if existing:
                existing.cancel()
            self._label_thresholds[key] = value
            self._label_tasks[key] = asyncio.create_task(
                self._ttl_restore_label(key, ttl_seconds)
            )
        if conflict:
            logger.warning(
                "Conflicting label suppression overwritten",
                extra={"namespace": namespace, "label": label, "new_value": value},
            )
        return not conflict

    async def restore_label_threshold(self, namespace: str, label: str) -> None:
        key = (namespace, label)
        async with self._get_lock:
            t = self._label_tasks.pop(key, None)
            if t:
                t.cancel()
            self._label_thresholds.pop(key, None)
        logger.info(
            "Label threshold restored",
            extra={"namespace": namespace, "label": label},
        )

    async def _ttl_restore_label(self, key: _LabelKey, ttl_seconds: int) -> None:
        try:
            await asyncio.sleep(ttl_seconds)
            async with self._get_lock:
                self._label_thresholds.pop(key, None)
                self._label_tasks.pop(key, None)
            logger.info(
                "TTL expired — label threshold auto-restored",
                extra={"key": str(key), "ttl_seconds": ttl_seconds},
            )
        except asyncio.CancelledError:
            pass

    async def clear_all(self) -> tuple[int, int]:
        """Cancel every pending TTL task and empty both threshold tables."""
        async with self._get_lock:
            n_pod = len(self._pod_thresholds)
            n_label = len(self._label_thresholds)
            for t in self._pod_tasks.values():
                t.cancel()
            for t in self._label_tasks.values():
                t.cancel()
            self._pod_tasks.clear()
            self._label_tasks.clear()
            self._pod_thresholds.clear()
            self._label_thresholds.clear()
        return n_pod, n_label


threshold_manager = ThresholdManager()
