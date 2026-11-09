"""API smoke tests for the ml-service (needs the trained models, see IDS_MODELS_DIR)."""

import os
import sys
import unittest

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from fastapi.testclient import TestClient  # noqa: E402

from app.main import app  # noqa: E402

_FRONT = {"namespace": "default", "label": "app=frontend"}


_MODELS_DIR = os.environ.get("IDS_MODELS_DIR") or os.path.join(os.path.dirname(__file__), "..", "..", "models")


@unittest.skipUnless(os.path.exists(os.path.join(_MODELS_DIR, "baseline.pkl")),
                     "trained models not present (run `make data`)")
class MlServiceSmokeTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.client_cm = TestClient(app)
        cls.c = cls.client_cm.__enter__()

    @classmethod
    def tearDownClass(cls):
        cls.client_cm.__exit__(None, None, None)

    def test_health_reports_loaded_registry(self):
        r = self.c.get("/health")
        self.assertEqual(r.status_code, 200)
        body = r.json()
        self.assertEqual(body["status"], "ok")
        self.assertTrue(body["model_loaded"])
        self.assertGreaterEqual(len(body["per_service_models"]), 1)

    def test_score_ready_uses_model_default(self):
        r = self.c.post("/score", json={
            "event_rate_per_sec": 0.4, "unique_syscalls_count": 1,
            "unique_processes_count": 1, "syscall_intensity_per_sec": 0.4,
            "lifecycle_phase": "running_ready", **_FRONT})
        self.assertEqual(r.status_code, 200)
        b = r.json()
        self.assertIn("score", b)
        self.assertIn("is_anomaly", b)
        self.assertFalse(b["phase_auth_fired"])

    def test_score_warmup_is_phase_authoritative(self):
        r = self.c.post("/score", json={
            "event_rate_per_sec": 5.0, "unique_syscalls_count": 2,
            "unique_processes_count": 1, "syscall_intensity_per_sec": 5.0,
            "lifecycle_phase": "running_warmup", **_FRONT})
        b = r.json()
        self.assertTrue(b["phase_auth_fired"])
        self.assertAlmostEqual(b["threshold"], -0.95)

    def test_score_warmup_blind_bypasses_phase_rule(self):
        r = self.c.post("/score", json={
            "event_rate_per_sec": 5.0, "unique_syscalls_count": 2,
            "unique_processes_count": 1, "syscall_intensity_per_sec": 5.0,
            "lifecycle_phase": "running_warmup",
            "apply_phase_authoritative": False, **_FRONT})
        b = r.json()
        self.assertFalse(b["phase_auth_fired"])
        self.assertNotAlmostEqual(b["threshold"], -0.95)

    def test_threshold_raise_then_conflict_then_clear(self):
        r1 = self.c.patch("/threshold", json={"pod_uid": "smoke-pod", "value": -0.8})
        self.assertEqual(r1.status_code, 200)
        self.assertEqual(r1.json()["status"], "raised")
        r2 = self.c.patch("/threshold", json={"pod_uid": "smoke-pod", "value": -0.8})
        self.assertEqual(r2.status_code, 409)
        self.assertEqual(r2.json()["status"], "conflict_overwritten")
        sc = self.c.post("/score", json={
            "event_rate_per_sec": 0.4, "unique_syscalls_count": 1,
            "unique_processes_count": 1, "syscall_intensity_per_sec": 0.4,
            "pod_uid": "smoke-pod", "lifecycle_phase": "running_ready", **_FRONT})
        self.assertAlmostEqual(sc.json()["threshold"], -0.8)
        r3 = self.c.patch("/threshold", json={"pod_uid": "smoke-pod", "value": None})
        self.assertEqual(r3.json()["status"], "restored")

    def test_admin_clear_and_reload(self):
        cl = self.c.post("/admin/clear_thresholds")
        self.assertEqual(cl.status_code, 200)
        self.assertEqual(cl.json()["status"], "cleared")
        rl = self.c.post("/admin/reload")
        self.assertEqual(rl.status_code, 200)
        self.assertEqual(rl.json()["status"], "reloaded")

    def test_scores_recent_returns_envelope(self):
        self.c.post("/score", json={
            "event_rate_per_sec": 0.4, "unique_syscalls_count": 1,
            "unique_processes_count": 1, "syscall_intensity_per_sec": 0.4,
            "lifecycle_phase": "running_ready", **_FRONT})
        r = self.c.get("/scores/recent",
                       params={"namespace": "default", "window_seconds": 300})
        self.assertEqual(r.status_code, 200)
        b = r.json()
        self.assertIn("count", b)
        self.assertIn("argmax", b)
        self.assertIn("scores", b)


if __name__ == "__main__":
    unittest.main()
