#!/usr/bin/env python3
"""Unit tests for the statistics library (TOST, effect sizes, bootstrap, Holm)."""

import math
import sys
import unittest
from pathlib import Path
from typing import Optional

import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parent))

from calculate_statistics import (
    ALPHA,
    DELTA_EQUIV,
    DELTA_NONINF,
    TrialMetrics,
    _bootstrap_median_ci,
    _holm_correction,
    _rank_biserial,
    _run_all_primary_tests,
    _tost_paired,
    _wilcoxon_one_sample,
    _wilcoxon_paired,
)


def _make_trial(trial_id: int,
                 fpr_b: Optional[float] = None,
                 fpr_u: Optional[float] = None,
                 fpr_s: Optional[float] = None,
                 tpr_s: Optional[float] = None,
                 ) -> TrialMetrics:
    """Factory for a synthetic TrialMetrics with sensible window counts."""
    return TrialMetrics(
        trial_id=trial_id,
        fpr_b=fpr_b, fpr_u=fpr_u, fpr_s=fpr_s, tpr_s=tpr_s, sep=None,
        n_baseline_windows=30 if fpr_b is not None else 0,
        n_h1_windows=30 if fpr_u is not None else 0,
        n_h2_windows=30 if fpr_s is not None else 0,
        n_h3_malicious_windows=14 if tpr_s is not None else 0,
    )


class RankBiserialTests(unittest.TestCase):
    def test_all_positive_diffs_returns_plus_one(self):
        self.assertAlmostEqual(_rank_biserial([0.1, 0.2, 0.3, 0.4]), 1.0)

    def test_all_negative_diffs_returns_minus_one(self):
        self.assertAlmostEqual(_rank_biserial([-0.1, -0.2, -0.3, -0.4]), -1.0)

    def test_balanced_diffs_returns_near_zero(self):
        self.assertAlmostEqual(_rank_biserial([-0.1, 0.1, -0.2, 0.2]), 0.0)

    def test_all_zero_diffs_returns_zero(self):
        self.assertEqual(_rank_biserial([0, 0, 0]), 0.0)

    def test_empty_returns_zero(self):
        self.assertEqual(_rank_biserial([]), 0.0)


class BootstrapMedianCITests(unittest.TestCase):
    def test_ci_contains_true_median_on_uniform(self):
        values = [0.05] * 8 + [0.06, 0.04]
        low, high = _bootstrap_median_ci(values)
        self.assertLessEqual(low, 0.05)
        self.assertGreaterEqual(high, 0.05)

    def test_ci_widens_with_high_variance(self):
        narrow_low, narrow_high = _bootstrap_median_ci([0.05] * 10)
        wide_low, wide_high = _bootstrap_median_ci(
            [0.0, 0.02, 0.04, 0.05, 0.06, 0.08, 0.10, 0.12, 0.20, 0.30]
        )
        self.assertLess(narrow_high - narrow_low, wide_high - wide_low)

    def test_too_few_samples_returns_nan(self):
        low, high = _bootstrap_median_ci([0.05])
        self.assertTrue(math.isnan(low))
        self.assertTrue(math.isnan(high))

    def test_deterministic_with_seed(self):
        a = _bootstrap_median_ci([0.01, 0.02, 0.03, 0.04, 0.05, 0.06], seed=42)
        b = _bootstrap_median_ci([0.01, 0.02, 0.03, 0.04, 0.05, 0.06], seed=42)
        self.assertEqual(a, b)


class WilcoxonOneSampleTests(unittest.TestCase):
    def test_alternative_greater_rejects_positive_shift(self):
        _, p, _ = _wilcoxon_one_sample([0.05, 0.06, 0.07, 0.08, 0.09], "greater")
        self.assertLess(p, ALPHA)

    def test_alternative_less_rejects_negative_shift(self):
        _, p, _ = _wilcoxon_one_sample(
            [-0.05, -0.06, -0.07, -0.08, -0.09], "less"
        )
        self.assertLess(p, ALPHA)

    def test_all_zero_returns_p_equal_one(self):
        _, p, note = _wilcoxon_one_sample([0, 0, 0, 0], "greater")
        self.assertEqual(p, 1.0)
        self.assertIn("undefined", note)


class TostPairedTests(unittest.TestCase):
    def test_tight_equivalence_rejects_null(self):
        diffs = [0.001, 0.002, -0.001, 0.0005, -0.0005,
                 0.0008, -0.0008, 0.0009, -0.0009, 0.0007,
                 0.001, 0.0015, -0.0012, 0.0011, -0.0014,
                 0.0013, -0.0011, 0.0009, -0.0007, 0.0008]
        result = _tost_paired(diffs, delta=0.01)
        self.assertLess(result.p_tost, ALPHA,
                        f"p_TOST={result.p_tost} should be < α={ALPHA} for tight equivalence")

    def test_over_suppression_fails_to_reject(self):
        diffs = [-0.05, -0.06, -0.04, -0.05, -0.07,
                 -0.05, -0.06, -0.04, -0.05, -0.05]
        result = _tost_paired(diffs, delta=0.01)
        self.assertGreaterEqual(result.p_tost, ALPHA)
        self.assertGreater(result.p_lower, 0.5,
                           "p_lower should be high when diffs ≪ -δ")

    def test_under_suppression_fails_to_reject(self):
        diffs = [0.05, 0.06, 0.04, 0.05, 0.07,
                 0.05, 0.06, 0.04, 0.05, 0.05]
        result = _tost_paired(diffs, delta=0.01)
        self.assertGreaterEqual(result.p_tost, ALPHA)
        self.assertGreater(result.p_upper, 0.5,
                           "p_upper should be high when diffs ≫ +δ")

    def test_too_few_pairs_returns_nan(self):
        result = _tost_paired([0.01], delta=0.01)
        self.assertTrue(math.isnan(result.p_tost))

    def test_p_tost_equals_max(self):
        for diffs in (
            [-0.05, -0.06, -0.04, -0.05, -0.07, -0.05, -0.06, -0.04, -0.05, -0.05],
            [0.05, 0.06, 0.04, 0.05, 0.07, 0.05, 0.06, 0.04, 0.05, 0.05],
            [0.001, -0.001, 0.002, -0.002, 0.0005, -0.0005, 0.001, -0.001, 0.002, -0.002],
        ):
            r = _tost_paired(diffs, delta=0.01)
            self.assertAlmostEqual(r.p_tost, max(r.p_lower, r.p_upper))


class HolmCorrectionTests(unittest.TestCase):
    def test_orders_and_corrects_five_pvalues(self):
        ps = [
            ("A", 0.001),
            ("B", 0.04),
            ("C", 0.20),
            ("D", 0.30),
            ("E", 0.50),
        ]
        out = _holm_correction(ps)
        corr = {lbl: c for lbl, _, c, _ in out}
        self.assertLessEqual(corr["A"], corr["B"])
        self.assertLessEqual(corr["B"], corr["C"])
        self.assertLessEqual(corr["C"], corr["D"])
        self.assertLessEqual(corr["D"], corr["E"])
        a_verdict = next(v for lbl, _, _, v in out if lbl == "A")
        self.assertIn("reject", a_verdict)

    def test_nan_pvalues_pass_through_with_dash_verdict(self):
        ps = [("X", float("nan")), ("Y", 0.01)]
        out = _holm_correction(ps)
        verdicts = {lbl: v for lbl, _, _, v in out}
        self.assertEqual(verdicts["X"], "—")
        self.assertIn("reject", verdicts["Y"])

    def test_empty_input(self):
        self.assertEqual(_holm_correction([]), [])

    def test_corrected_p_capped_at_one(self):
        ps = [("A", 0.6), ("B", 0.7), ("C", 0.9)]
        out = _holm_correction(ps)
        for _, _, c, _ in out:
            self.assertLessEqual(c, 1.0)


class PrimaryTestDiscoveryTests(unittest.TestCase):
    def test_phase1_compatible_trials_yield_four_primary_tests(self):
        trials = [
            _make_trial(i, fpr_b=0.05, fpr_u=0.08, fpr_s=0.002)
            for i in range(1, 11)
        ]
        results = _run_all_primary_tests(trials)
        labels = sorted(r.label for r in results if r.family == "primary")
        self.assertEqual(labels, ["H1", "H2a", "H2b", "H2c"])

    def test_h2c_reports_over_suppression_in_extras(self):
        trials = [
            _make_trial(i, fpr_b=0.05, fpr_u=0.08, fpr_s=0.002)
            for i in range(1, 11)
        ]
        results = _run_all_primary_tests(trials)
        h2c = next(r for r in results if r.label == "H2c")
        self.assertLess(h2c.extras["median_diff"], -0.04)
        self.assertGreaterEqual(h2c.raw_p, 0.5)

    def test_h2a_rejects_when_controller_clearly_helps(self):
        trials = [
            _make_trial(i, fpr_b=0.05, fpr_u=0.08, fpr_s=0.002)
            for i in range(1, 11)
        ]
        results = _run_all_primary_tests(trials)
        h2a = next(r for r in results if r.label == "H2a")
        self.assertLess(h2a.raw_p, ALPHA)
        self.assertGreater(h2a.effect_size, 0.5)

    def test_h2b_has_tost_diagnostics_and_ci(self):
        trials = [
            _make_trial(i,
                        fpr_b=0.05 + 0.001 * i,
                        fpr_u=0.08 + 0.0005 * i,
                        fpr_s=0.002 + 0.0002 * i)
            for i in range(1, 11)
        ]
        results = _run_all_primary_tests(trials)
        h2b = next(r for r in results if r.label == "H2b")
        self.assertIn("p_lower", h2b.extras)
        self.assertIn("p_upper", h2b.extras)
        self.assertEqual(h2b.raw_p, max(h2b.extras["p_lower"], h2b.extras["p_upper"]))
        self.assertFalse(math.isnan(h2b.ci_low))
        self.assertFalse(math.isnan(h2b.ci_high))
        self.assertLessEqual(h2b.ci_low, h2b.ci_high)


class WilcoxonPairedBackwardCompatTests(unittest.TestCase):
    """The existing _wilcoxon_paired API must still return (W, p, note)."""

    def test_default_two_sided(self):
        a = [0.01, 0.02, 0.03, 0.04, 0.05, 0.06]
        b = [0.0, 0.0, 0.0, 0.0, 0.0, 0.0]
        W, p, note = _wilcoxon_paired(a, b)
        self.assertEqual(note, "")
        self.assertGreaterEqual(W, 0)
        self.assertLess(p, ALPHA)

    def test_one_sided_greater_alternative(self):
        a = [0.01, 0.02, 0.03, 0.04, 0.05, 0.06]
        b = [0.0, 0.0, 0.0, 0.0, 0.0, 0.0]
        _, p_two, _ = _wilcoxon_paired(a, b, alternative="two-sided")
        _, p_greater, _ = _wilcoxon_paired(a, b, alternative="greater")
        self.assertLess(p_greater, p_two + 1e-9)


class PreregisteredDeltaTests(unittest.TestCase):
    """The pre-registered margin must remain 0.01 absolute."""

    def test_delta_equiv_pinned(self):
        self.assertEqual(DELTA_EQUIV, 0.01)

    def test_delta_noninf_pinned(self):
        self.assertEqual(DELTA_NONINF, 0.01)


if __name__ == "__main__":
    unittest.main(verbosity=2)
