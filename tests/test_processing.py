import os
import sys
import unittest
from fractions import Fraction

from hbpipe.common import processing as P
from hbpipe.common.intervals import IntervalSet, PrefixCoverage

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "tools"))
from fake_mock import Signal, ramp_values  # noqa: E402


def loader_from(fn, missing=frozenset()):
    def load(sensor, lo, hi):
        return [None if (i, sensor) in missing else tuple(fn(sensor, i))
                for i in range(lo, hi + 1)]
    return load


def full_history_peaks(x, upto):
    """Reference: the literal rules, chain from index 0, no shortcuts."""
    peaks, prev = [], None
    for i in range(800, upto + 1):
        if i + 1 >= len(x):
            continue
        v = x[i]
        if v > x[i - 1] and v > x[i + 1] and v > max(x[i - 800:i]) / 2 and \
                (prev is None or i - prev >= 100):
            peaks.append(i)
            prev = i
    return peaks


class RoundingTests(unittest.TestCase):
    def test_half_away_from_zero(self):
        self.assertEqual(P.round1(Fraction(1, 4)), 0.3)
        self.assertEqual(P.round1(Fraction(-1, 4)), -0.3)
        self.assertEqual(P.round1(72), 72.0)
        self.assertEqual(P.round1(Fraction(1, 3)), 0.3)


class WindowTests(unittest.TestCase):
    def test_ramp_complete_window(self):
        r = P.compute_window(loader_from(ramp_values), 10, "offline")
        self.assertEqual((r["missing_1"], r["missing_2"], r["complete"]), (0, 0, True))
        # sensor 1 channel 1 over i in [4000, 4399] is 3i+1: min 12001, max 13198, mean 12599.5
        self.assertEqual(r["min_1"][0], 12001)
        self.assertEqual(r["max_1"][0], 13198)
        self.assertEqual(r["mean_1"][0], 12599.5)
        self.assertEqual(r["mode"], "offline")

    def test_missing_samples_counted_not_filled(self):
        missing = {(i, 2) for i in range(4000, 4100)}
        r = P.compute_window(loader_from(ramp_values, missing), 10, "online")
        self.assertEqual((r["missing_1"], r["missing_2"], r["complete"]), (0, 100, False))
        self.assertEqual(r["min_2"][0], 2 * 4100 + 1)

    def test_empty_window_nulls(self):
        missing = {(i, s) for i in range(0, 401) for s in (1, 2)}
        r = P.compute_window(loader_from(ramp_values, missing), 0, "online")
        self.assertIsNone(r["mean_1"])
        self.assertIsNone(r["magnitude"])
        self.assertIsNone(r["heart_rate"])

    def test_hr_null_before_window_4(self):
        sig = Signal(7, 400 * 60)
        self.assertIsNone(P.compute_window(loader_from(sig.values), 3, "online")["heart_rate"])

    def test_hr_null_if_span_has_gap(self):
        sig = Signal(7, 400 * 60)
        r = P.compute_window(loader_from(sig.values, {(5000, 1)}), 14, "online")
        self.assertIsNone(r["heart_rate"])

    def test_hr_schedule_60_90_72(self):
        sig = Signal(7, 400 * 60 * 6)
        load = loader_from(sig.values)
        for w, expect in ((60, 60.0), (180, 90.0), (300, 72.0)):
            hr = P.compute_window(load, w, "online")["heart_rate"]
            self.assertAlmostEqual(hr, expect, delta=1.0, msg=f"window {w}")

    def test_warmup_chain_equals_full_history(self):
        total = 400 * 60 * 5
        sig = Signal(3, total)
        x = [sig.values(1, i)[1] for i in range(total + 1)]
        ref = full_history_peaks(x, total - 2)
        for w in range(4, total // 400 - 1, 7):
            lo, _hi = P.s1_range(w)
            span_lo, span_hi = 400 * (w - 4), 400 * (w + 1) - 1
            got = [p for p in P.detect_peaks(x[lo:span_hi + 2], lo, span_hi) if p >= span_lo]
            want = [p for p in ref if span_lo <= p <= span_hi]
            self.assertEqual(got, want, f"window {w}")

    def test_ready_needs_final_inputs(self):
        cov1, cov2 = IntervalSet(), IntervalSet()
        cov1.add(0, 4400)
        cov2.add(0, 4400)
        self.assertFalse(P.window_ready(cov1, cov2, 10))  # needs sensor 1 index 4400 too
        cov1.add(4400, 4401)
        self.assertTrue(P.window_ready(cov1, cov2, 10))
        self.assertTrue(P.window_ready(PrefixCoverage(10, True), PrefixCoverage(10, True), 50))

    def test_intervals_merge(self):
        s = IntervalSet()
        s.add(10, 20)
        s.add(0, 5)
        s.add(5, 10)
        s.add(30, 40)
        self.assertTrue(s.covers(0, 20))
        self.assertFalse(s.covers(15, 31))
        s.add(19, 31)
        self.assertTrue(s.covers(0, 40))
        self.assertEqual(len(s.starts), 1)


if __name__ == "__main__":
    unittest.main()
