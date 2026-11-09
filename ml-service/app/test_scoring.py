"""Threshold precedence and TTL behaviour of the scorer, with a stub model (no trained models needed)."""

import asyncio
import os
import sys
import unittest
from types import SimpleNamespace

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from app.models import FeatureWindow  # noqa: E402
from app.scoring import _WARMUP_THRESHOLD, score_window  # noqa: E402
from app.state import threshold_manager  # noqa: E402

MODEL_OFFSET = -0.60
POD_OVERRIDE = -0.80
LABEL_OVERRIDE = -0.70


class _StubModel:
    def __init__(self, score: float) -> None:
        self.score = score

    def score_samples(self, X):
        return [self.score] * len(X)


def _entry(score: float) -> SimpleNamespace:
    return SimpleNamespace(model=_StubModel(score), offset=MODEL_OFFSET, key="app=cartservice")


def _window(phase: str, apply_phase_authoritative: bool = True) -> FeatureWindow:
    return FeatureWindow(
        event_rate_per_sec=10.0, unique_syscalls_count=3, unique_processes_count=2,
        syscall_intensity_per_sec=0.0, pod_uid="pod-1", pod_name="cartservice-1",
        lifecycle_phase=phase, namespace="default", label="app=cartservice",
        apply_phase_authoritative=apply_phase_authoritative,
    )


async def _score(window, score, pod_override=None, label_override=None):
    await threshold_manager.clear_all()
    if pod_override is not None:
        await threshold_manager.raise_pod_threshold("pod-1", pod_override, 60)
    if label_override is not None:
        await threshold_manager.raise_label_threshold("default", "app=cartservice", label_override, 60)
    result = score_window(window, _entry(score))
    await threshold_manager.clear_all()
    return result


def score(window, score=-0.65, **overrides):
    return asyncio.run(_score(window, score, **overrides))


class ThresholdPrecedenceTests(unittest.TestCase):
    def test_warmup_window_uses_phase_rule_even_with_pod_override(self):
        r = score(_window("running_warmup"), pod_override=POD_OVERRIDE)
        self.assertEqual(r["threshold"], _WARMUP_THRESHOLD)
        self.assertTrue(r["phase_auth_fired"])

    def test_blind_condition_skips_phase_rule(self):
        r = score(_window("running_warmup", apply_phase_authoritative=False))
        self.assertEqual(r["threshold"], MODEL_OFFSET)
        self.assertFalse(r["phase_auth_fired"])

    def test_pod_override_beats_label_override(self):
        r = score(_window("running_ready"), pod_override=POD_OVERRIDE, label_override=LABEL_OVERRIDE)
        self.assertEqual(r["threshold"], POD_OVERRIDE)

    def test_label_override_when_no_pod_override(self):
        r = score(_window("running_ready"), label_override=LABEL_OVERRIDE)
        self.assertEqual(r["threshold"], LABEL_OVERRIDE)

    def test_model_offset_by_default(self):
        r = score(_window("running_ready"))
        self.assertEqual(r["threshold"], MODEL_OFFSET)

    def test_verdict_is_score_below_threshold(self):
        # -0.65 is anomalous against the model offset (-0.60) ...
        self.assertTrue(score(_window("running_ready"), score=-0.65)["is_anomaly"])
        # ... but not against the warmup threshold (-0.95).
        self.assertFalse(score(_window("running_warmup"), score=-0.65)["is_anomaly"])


class TtlTests(unittest.TestCase):
    def test_pod_override_expires_after_ttl(self):
        async def run():
            await threshold_manager.clear_all()
            await threshold_manager.raise_pod_threshold("pod-1", POD_OVERRIDE, 1)
            before = threshold_manager.get_threshold("pod-1", None, None, MODEL_OFFSET)
            await asyncio.sleep(1.2)
            after = threshold_manager.get_threshold("pod-1", None, None, MODEL_OFFSET)
            return before, after

        before, after = asyncio.run(run())
        self.assertEqual(before, POD_OVERRIDE)
        self.assertEqual(after, MODEL_OFFSET)

    def test_clear_restores_model_default(self):
        async def run():
            await threshold_manager.clear_all()
            await threshold_manager.raise_pod_threshold("pod-1", POD_OVERRIDE, 60)
            await threshold_manager.restore_pod_threshold("pod-1")
            return threshold_manager.get_threshold("pod-1", None, None, MODEL_OFFSET)

        self.assertEqual(asyncio.run(run()), MODEL_OFFSET)


if __name__ == "__main__":
    unittest.main()
