# -*- coding: utf-8 -*-
"""Regression tests for the S1 fixes. Standard library only::

    python -m unittest discover -s tests -v

Each test is small and synthetic; none needs a real recording or the GUI.
"""
import sys
import tempfile
import types
import unittest
from pathlib import Path

import numpy as np
import pandas as pd

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

import processing as P


def make_frame(seconds=120.0, fs=10.0, offset=100.0, level=1.0e4, tau=600.0, signal=None):
    """A two-channel recording with an exponential bleach, as the loader would give."""
    t = np.arange(0, seconds, 1 / fs)
    bleach = level * np.exp(-t / tau)
    if signal is not None:
        bleach = bleach + signal(t)
    frame = pd.DataFrame({
        "TimeStamp": t * 1000.0,
        "CH1-470": offset + bleach,
        "CH1-410": offset + 0.6 * bleach,
    })
    frame.attrs["source_fingerprint"] = "test:frame"
    return frame


def config(seconds=120.0, **overrides):
    values = dict(
        range_start_min=0.0, range_end_min=seconds / 60.0,
        offsets={"470": 100.0, "410": 100.0},
        baselines={"470": 10050.0, "410": 6050.0},
        wavelengths=["470", "410"], analysis_wavelength="470", reference_wavelength=None,
        fit_model="single_exponential", smooth_seconds=0.0,
        fit_regions={"470": [(0.1, seconds / 60 - 0.1)], "410": [(0.1, seconds / 60 - 0.1)]},
        fit_models={"470": "single_exponential", "410": "single_exponential"},
        fit_baseline_modes={"470": "fixed", "410": "fixed"},
    )
    values.update(overrides)
    return P.ProcessingConfig(**values)


class ExclusionWindowTest(unittest.TestCase):
    """07: an exclusion that removes too much must stop the fit, not be dropped."""

    def test_exclusion_that_removes_most_of_the_region_raises(self):
        data = make_frame()
        cfg = config(exclusion_windows=((0.2, 1.9),))     # nearly the whole fit region
        with self.assertRaises(ValueError) as caught:
            P.process_data(data, cfg, data_token=("exclusion", len(data)))
        self.assertIn("marker exclusion windows", str(caught.exception))

    def test_modest_exclusion_is_applied_and_counted(self):
        data = make_frame()
        cfg = config(fit_regions={"470": [(0.1, 1.9)], "410": [(0.1, 1.9)]},
                     exclusion_windows=((0.100, 0.110),))
        processed, details = P.process_data(data, cfg, data_token=("exclusion2", len(data)))
        self.assertGreater(details["fit_470_parameters"]["excluded_samples"], 0)
        self.assertLess(int(processed["fit_used_470"].sum()), len(processed))


class EventGapTest(unittest.TestCase):
    """05: a hole wider than the limit excludes the trial; a short one is interpolated."""

    def _event_frame(self, hole):
        t = np.arange(0, 100.0, 0.1)
        values = 100.0 + 0.01 * t
        if hole is not None:
            values[(t >= hole[0]) & (t <= hole[1])] = np.nan
        return pd.DataFrame({"original_time_s": t, "event_signal": values})

    def test_window_inside_a_long_hole_is_excluded(self):
        frame = self._event_frame((40.0, 60.0))
        with self.assertRaises(ValueError) as caught:
            P.calculate_event_locked_traces(frame, [50 / 60], "ev", 5.0, 5.0, 5.0,
                                            signal_column="event_signal")
        self.assertIn("hole", str(caught.exception))

    def test_short_dropped_frames_are_still_interpolated(self):
        frame = self._event_frame((50.0, 50.4))       # 0.4 s < 1 s limit
        trials, _average, details = P.calculate_event_locked_traces(
            frame, [50 / 60], "ev", 5.0, 5.0, 5.0, signal_column="event_signal")
        self.assertEqual(int(trials["trial"].nunique()), 1)
        self.assertEqual(details["excluded_events"], [])

    def test_sparse_recording_is_not_mistaken_for_a_hole(self):
        t = np.arange(0, 200.0, 2.0)                  # 0.5 Hz: spacing alone is 2 s
        frame = pd.DataFrame({"original_time_s": t, "event_signal": 100.0 + 0.01 * t})
        trials, _average, details = P.calculate_event_locked_traces(
            frame, [100 / 60], "ev", 10.0, 10.0, 10.0, signal_column="event_signal")
        self.assertEqual(int(trials["trial"].nunique()), 1)
        self.assertEqual(details["excluded_events"], [])

    def test_longest_gap_inside_reports_the_hole(self):
        time_s = np.array([0.0, 0.1, 0.2, 30.0, 30.1])
        self.assertAlmostEqual(P.longest_gap_inside(time_s, 5.0, 10.0), 29.8, places=6)
        self.assertAlmostEqual(P.longest_gap_inside(time_s, 0.0, 0.2), 0.1, places=6)


class FingerprintTest(unittest.TestCase):
    """17: a recording identity that changes when the data changes."""

    def test_fingerprint_tracks_content_and_length(self):
        with tempfile.TemporaryDirectory() as tmp:
            first = Path(tmp) / "a.csv"
            second = Path(tmp) / "b.csv"
            first.write_text("meta\nTimeStamp,CH1-470\n0,1\n100,2\n", encoding="utf-8")
            second.write_text("meta\nTimeStamp,CH1-470\n0,1\n100,3\n", encoding="utf-8")
            third = Path(tmp) / "c.csv"
            third.write_text(first.read_text(encoding="utf-8") + "200,3\n", encoding="utf-8")
            a = P.recording_fingerprint(first)
            self.assertEqual(a, P.recording_fingerprint(first))
            self.assertNotEqual(a, P.recording_fingerprint(second))
            self.assertNotEqual(a, P.recording_fingerprint(third))


class ReferenceLeakTest(unittest.TestCase):
    """03: the read-out must report the share of the response the subtraction removes."""

    def _channels(self, share, noise=0.0, seed=3):
        rng = np.random.default_rng(seed)
        t = np.arange(0, 120.0, 1.0)
        resp = np.zeros_like(t)
        resp[(t >= 55) & (t < 85)] = 10.0        # starts at the marker, so the pre-window is clean
        primary = 1000.0 + resp + rng.normal(0, noise, t.size)
        reference = 500.0 + share * resp + rng.normal(0, noise, t.size)
        return primary, reference, t / 60.0

    def test_clean_reference_reports_no_loss(self):
        primary, reference, time_min = self._channels(0.0, noise=0.05)
        leak = P.reference_response_leak(primary, reference, time_min, 1.667, 1010.0, 500.0,
                                         (55 / 60,))
        self.assertIsNotNone(leak)
        self.assertLess(abs(leak["response_fraction_removed"]), 0.1)

    def test_reference_carrying_the_response_reports_the_loss(self):
        # 10 counts on the analysis channel, 3 on the reference: k = (3/500)/(10/1000) ~ 0.6.
        # With the RWD slope 1.667 the subtraction removes 1.667 * 0.6 * 500/1010 ~ 0.50.
        primary, reference, time_min = self._channels(0.3, noise=0.05)
        leak = P.reference_response_leak(primary, reference, time_min, 1.667, 1010.0, 500.0,
                                         (55 / 60,))
        self.assertIsNotNone(leak)
        self.assertGreater(leak["response_fraction_removed"], 0.3)
        self.assertLess(leak["response_fraction_kept"], 0.7)

    def test_no_markers_means_no_read_out(self):
        primary, reference, time_min = self._channels(3.0)
        self.assertIsNone(P.reference_response_leak(
            primary, reference, time_min, 2.0, 1010.0, 503.0, ()))


class FittedCurveReplayTest(unittest.TestCase):
    """17: a saved fit is only valid on the recording it was fitted on."""

    def test_process_data_records_the_fingerprint(self):
        data = make_frame()
        _processed, details = P.process_data(data, config(), data_token=("fp", len(data)))
        self.assertEqual(details["source_fingerprint"], "test:frame")


class TrialMapTest(unittest.TestCase):
    """22: the exported trial numbers must be traceable back to a marker."""

    def test_map_keeps_marker_link(self):
        import app
        trials = pd.DataFrame({
            "event_signal_key": ["combined:470/410"] * 3,
            "event_signal_label": ["RWD 470/410"] * 3,
            "trial": [1, 2, 3],
            "source_event_index": [1, 2, 4],       # event 3 was excluded
            "marker_time_min": [10.0, 20.0, 40.0],
            "marker_name": ["Injection"] * 3,
            "relative_time_s": [0.0, 0.0, 0.0],
            "f0": [1000.0, 1001.0, 1002.0],
            "baseline_dff_sd": [0.1, 0.1, 0.1],
            "baseline_dff_n": [50, 50, 50],
        })
        stub = types.SimpleNamespace(event_trials=trials)
        frame = app.PretreatmentApp.event_trial_map_frame(stub)
        self.assertEqual(list(frame["trial"]), [1, 2, 3])
        self.assertEqual(list(frame["source_event_index"]), [1, 2, 4])
        self.assertEqual(list(frame["marker_time_min"]), [10.0, 20.0, 40.0])


class BleachCorrectionTest(unittest.TestCase):
    """The two correction forms must differ exactly where the model says they do."""

    def _multiplicative_frame(self, seconds=600.0, tau=300.0):
        fs = 30.0
        t = np.arange(0, seconds, 1 / fs)
        bleach = 4.0e4 * np.exp(-t / tau)
        response = np.zeros_like(t)
        response[(t >= 100) & (t < 110)] = 0.10
        response[(t >= 480) & (t < 490)] = 0.10
        raw = 100.0 + bleach * (1 + response)
        frame = pd.DataFrame({"TimeStamp": t * 1000, "CH1-470": raw, "CH1-410": raw.copy()})
        frame.attrs["source_fingerprint"] = "test:bleach"
        baseline = 100.0 + float(np.median(raw[-int(20 * fs):]))
        return frame, baseline, fs

    def _config(self, baseline, seconds, form):
        return P.ProcessingConfig(
            range_start_min=0.0, range_end_min=seconds / 60,
            offsets={"470": 100.0, "410": 100.0},
            baselines={"470": baseline, "410": baseline},
            wavelengths=["470", "410"], analysis_wavelength="470", reference_wavelength=None,
            fit_model="single_exponential", smooth_seconds=0.0,
            fit_regions={"470": [(2.0, 7.0)], "410": [(2.0, 7.0)]},   # away from both responses
            fit_models={"470": "single_exponential", "410": "single_exponential"},
            fit_baseline_modes={"470": "fit_constant", "410": "fit_constant"},
            bleach_correction=form)

    def _amplitudes(self, form):
        frame, baseline, fs = self._multiplicative_frame()
        processed, details = P.process_data(frame, self._config(baseline, 600.0, form),
                                            data_token=("bleach", form))
        normalized, _n = P.calculate_normalized_traces(processed, 80 / 60, 95 / 60, 0.0, fs)
        tm = normalized["time_min"].to_numpy(float) * 60
        dff = normalized["dff_percent"].to_numpy(float)
        early = dff[(tm >= 100) & (tm < 110)].max()
        late = dff[(tm >= 480) & (tm < 490)].max()
        return early, late, details

    def test_multiplicative_keeps_equal_responses_equal(self):
        early, late, details = self._amplitudes("multiplicative")
        self.assertEqual(details["bleach_correction"], "multiplicative")
        self.assertAlmostEqual(early, 10.0, delta=0.3)
        self.assertAlmostEqual(late, 10.0, delta=0.3)

    def test_additive_inflates_the_early_response(self):
        early, late, _details = self._amplitudes("additive")
        self.assertGreater(early, 1.5 * late)

    def test_unknown_form_is_rejected(self):
        frame, baseline, _fs = self._multiplicative_frame()
        with self.assertRaises(ValueError):
            P.process_data(frame, self._config(baseline, 600.0, "divided"),
                           data_token=("bleach", "bad"))


class AverageZTest(unittest.TestCase):
    """The trial average gives a larger, still honest, z than a single trial."""

    def test_average_z_exceeds_single_trial_z(self):
        fs = 20.0
        rng = np.random.default_rng(4)
        segment, count = 40.0, 6
        times, values, markers = [], [], []
        for index in range(count):
            base = index * segment
            local = np.arange(0, segment, 1 / fs)
            signal = 100.0 + np.where(local >= 15.0, 10.0, 0.0) + rng.normal(0, 1.0, local.size)
            times.append(base + local)
            values.append(signal)
            markers.append((base + 15.0) / 60.0)
        frame = pd.DataFrame({"original_time_s": np.concatenate(times),
                              "event_signal": np.concatenate(values)})
        trials, average, details = P.calculate_event_locked_traces(
            frame, markers, "ev", 5.0, 10.0, 5.0, signal_column="event_signal")
        single = trials.groupby("trial")["zscore"].max().mean()
        pooled = float(average["zscore_of_average"].max())
        self.assertGreater(pooled, 1.5 * single)
        self.assertIn("zscore_of_average_note", details)


class AverageZBasisTest(unittest.TestCase):
    """The average panel must be able to show either z, and say which one it is."""

    def _stub(self, basis):
        return types.SimpleNamespace(
            vars={"event_z_basis": types.SimpleNamespace(get=lambda: basis)})

    def test_default_basis_is_the_mean_of_per_trial_z(self):
        import app
        column, sem, label = app.PretreatmentApp.event_average_z_columns(self._stub("mean of trials"))
        self.assertEqual(column, "zscore_mean")
        self.assertEqual(sem, "zscore_sem")
        self.assertIn("per-trial", label)

    def test_average_trace_basis_has_no_sem_band(self):
        import app
        column, sem, label = app.PretreatmentApp.event_average_z_columns(self._stub("average trace"))
        self.assertEqual(column, "zscore_of_average")
        self.assertIsNone(sem)
        self.assertIn("average trace", label)


if __name__ == "__main__":
    unittest.main(verbosity=2)
