"""FOC-011: synthetic eye geometry, sampling continuity, and bounded evidence."""
from dataclasses import FrozenInstanceError, replace
import math
from types import SimpleNamespace
import unittest

from eye_observation import (
    EXTRACTOR_VERSION,
    EyeGeometry, EyeObservation, EyeObservationWindow, EyeWindowConfig,
    invalid_eye_geometry, measure_eye_geometry,
)


def landmarks(width=640, height=480, scale=1):
    """Fixed FaceMesh indices, independent of the extractor's contour ordering.

    The right lid separations are 6 and 10 pixels; the left are 14 and 18.
    Each eye is 40 pixels wide, giving EAR .2 and .4 independently.
    """
    points = [SimpleNamespace(x=.5, y=.5) for _ in range(478)]
    pixels = {
        33: (80, 100), 160: (90, 96), 158: (110, 94),
        133: (120, 100), 153: (110, 104), 144: (90, 102),
        362: (180, 100), 385: (190, 94), 387: (210, 90),
        263: (220, 100), 373: (210, 108), 380: (190, 108),
    }
    for index, (px, py) in pixels.items():
        points[index] = SimpleNamespace(x=px * scale / width, y=py * scale / height)
    return points


def observation(frame=1, acquired=0., session="synthetic", geometry=None, **overrides):
    values = dict(session_id=session, color_frame_number=frame, captured_at_ms=1000,
                  acquired_monotonic_s=acquired, completed_monotonic_s=acquired + .05,
                  geometry=geometry if geometry is not None else measure_eye_geometry(landmarks(), 640, 480))
    values.update(overrides)
    return EyeObservation(**values)


def window(duration=2, capacity=20, gap=1):
    result = EyeObservationWindow(EyeWindowConfig(duration, capacity, gap))
    result.reset("synthetic")
    return result


class GeometryTests(unittest.TestCase):
    def test_pixel_aspect_ratio_and_independent_eyes(self):
        for width, height in ((640, 480), (1280, 720), (480, 640)):
            result = measure_eye_geometry(landmarks(width, height), width, height)
            self.assertIsNone(result.reason)
            self.assertAlmostEqual(result.right_ear, .2)
            self.assertAlmostEqual(result.left_ear, .4)
            self.assertAlmostEqual(result.right_width_px, 40)
            self.assertEqual(result.extractor_version, EXTRACTOR_VERSION)
            self.assertEqual(result.schema_version, 1)

    def test_uniform_image_scaling_preserves_ear_and_changes_width(self):
        result = measure_eye_geometry(landmarks(1280, 960, 2), 1280, 960)
        self.assertAlmostEqual(result.left_ear, .4)
        self.assertAlmostEqual(result.right_ear, .2)
        self.assertAlmostEqual(result.left_width_px, 80)

    def assert_invalid(self, result, reason):
        self.assertEqual(result.reason, reason)
        self.assertIsNone(result.left_ear)
        self.assertIsNone(result.right_ear)
        self.assertIsNone(result.left_width_px)
        self.assertIsNone(result.right_width_px)

    def test_no_face_and_missing_landmarks(self):
        self.assert_invalid(measure_eye_geometry(None, 640, 480), "no_face")
        for points in ([], [object()] * 478, {}):
            self.assert_invalid(measure_eye_geometry(points, 640, 480), "missing_landmarks")

    def test_invalid_dimensions_never_retain_nonfinite_numbers(self):
        for bad in (0, -1, True, 640.0, None, math.nan, math.inf, 10 ** 400):
            result = measure_eye_geometry(landmarks(), bad, 480)
            self.assert_invalid(result, "invalid_dimensions")
            self.assertIsNone(result.width)
            self.assertEqual(result.height, 480)

    def test_nonfinite_bool_nonnumeric_and_out_of_bounds_coordinates(self):
        for bad in (math.nan, math.inf, -math.inf, True, "private text"):
            points = landmarks()
            points[33].x = bad
            self.assert_invalid(measure_eye_geometry(points, 640, 480), "non_finite_landmark")
        for bad in (-.01, 1.01):
            points = landmarks()
            points[362].y = bad
            self.assert_invalid(measure_eye_geometry(points, 640, 480), "out_of_bounds_landmark")

    def test_degenerate_eye_invalidates_measurements_without_division(self):
        points = landmarks()
        points[133] = points[33]
        self.assert_invalid(measure_eye_geometry(points, 640, 480), "degenerate_eye")

    def test_closed_geometry_is_zero_ear_without_invented_classification(self):
        points = landmarks()
        for index in (33, 160, 158, 133, 153, 144, 362, 385, 387, 263, 373, 380):
            points[index].y = 100 / 480
        result = measure_eye_geometry(points, 640, 480)
        self.assertEqual(result.left_ear, 0)
        self.assertEqual(result.right_ear, 0)
        self.assertIsNone(result.reason)
        self.assertNotIn("energy", result.__dict__)

    def test_dict_points_and_unused_landmarks(self):
        points = [dict(x=point.x, y=point.y) for point in landmarks()]
        points[0] = {"x": math.nan}
        self.assertIsNone(measure_eye_geometry(points, 640, 480).reason)

    def test_records_are_frozen_and_unknown_invalid_reasons_fail(self):
        for record, field, value in ((observation(), "session_id", "changed"),
                                     (observation().geometry, "left_ear", 1),
                                     (EyeWindowConfig(1, 1, 1), "max_samples", 5)):
            with self.assertRaises(FrozenInstanceError):
                setattr(record, field, value)
        with self.assertRaisesRegex(ValueError, "invalid_geometry_reason"):
            invalid_eye_geometry("private text", 640, 480)


class WindowTests(unittest.TestCase):
    def test_snapshot_watermark_rejects_backward_endpoint_before_pruning(self):
        target = window()
        for frame, acquired in ((1, 0), (2, .5), (3, 1)):
            target.observe(observation(frame, acquired))
        self.assertEqual(target.snapshot(1)["coverage_seconds"], 1)
        later = target.snapshot(100)
        with self.assertRaisesRegex(ValueError, "as_of_before_snapshot"):
            target.snapshot(1)
        self.assertEqual(target.snapshot(100), later)
        target.reset("synthetic")
        with self.assertRaisesRegex(ValueError, "as_of_before_snapshot"):
            target.snapshot(1)
        target.reset("new")
        self.assertEqual(target.snapshot(1)["retained_samples"], 0)
        target.reset()
        self.assertEqual(target.snapshot(0)["retained_samples"], 0)

    def test_inference_failure_clears_samples_but_keeps_ordering(self):
        target = window()
        target.observe(observation(10, 0))
        target.observe(observation(11, .5))
        target.observe(observation(9, .75, geometry=invalid_eye_geometry("inference_failed", 640, 480)))
        self.assertFalse(target.observe(observation(11, .8)))
        self.assertTrue(target.observe(observation(12, 1)))
        report = target.snapshot(1)
        self.assertEqual(report["valid_samples"], 1)
        self.assertEqual(report["invalid_samples"], 2)
        self.assertEqual(report["total_observations_since_reset"], 3)
        self.assertEqual(report["reason_counts"], {"out_of_order_frame": 1, "duplicate_frame": 1})
        self.assertEqual(report["coverage_seconds"], 0)

    def test_wrong_session_inference_failure_cannot_clear_current_samples(self):
        target = window()
        target.observe(observation(1, 0))
        target.observe(observation(2, .5))
        target.observe(observation(999, 100, session="old",
                                   geometry=invalid_eye_geometry("inference_failed", 640, 480)))
        report = target.snapshot(.5)
        self.assertEqual(report["valid_samples"], 2)
        self.assertEqual(report["invalid_samples"], 1)
        self.assertEqual(report["reason_counts"], {"wrong_session": 1})
        self.assertEqual(report["coverage_seconds"], .5)

    def test_incoherent_clock_preserves_time_evidence_but_deduplicates_frame(self):
        target = window()
        target.observe(observation(1, 0))
        target.observe(observation(2, .5))
        self.assertFalse(target.observe(observation(3, 1e9, completed_monotonic_s=0)))
        self.assertFalse(target.observe(observation(3, .75)))
        self.assertTrue(target.observe(observation(4, 1)))
        report = target.snapshot(1)
        self.assertEqual(report["valid_samples"], 3)
        self.assertEqual(report["invalid_samples"], 2)
        self.assertEqual(report["reason_counts"], {"invalid_timestamp": 1, "duplicate_frame": 1})
        self.assertEqual(report["coverage_seconds"], .5)

    def test_config_requires_positive_finite_explicit_values(self):
        for args in ((0, 5, 1), (math.nan, 5, 1), (True, 5, 1), (1, 0, 1),
                     (1, True, 1), (1, 1.5, 1), (1, 5, math.inf), (1, 5, 0), (1e-320, 1, 1), (1, 10 ** 400, 1)):
            with self.assertRaises(ValueError):
                EyeWindowConfig(*args)

    def test_only_explicit_reset_can_bind_session(self):
        target = EyeObservationWindow(EyeWindowConfig(2, 20, 1))
        self.assertFalse(target.observe(observation()))
        self.assertIsNone(target.snapshot(0)["session_id"])
        target.reset("synthetic")
        self.assertTrue(target.observe(observation()))
        self.assertEqual(target.snapshot(0)["valid_samples"], 1)
        target.reset()
        self.assertEqual(target.snapshot(1)["retained_samples"], 0)

    def test_valid_frame_zero_skip_numbers_and_utc_rollback(self):
        target = window()
        self.assertTrue(target.observe(observation(0, 0, captured_at_ms=1000)))
        self.assertTrue(target.observe(observation(40, .5, captured_at_ms=500)))
        report = target.snapshot(1)
        self.assertEqual(report["valid_samples"], 2)
        self.assertEqual(report["coverage_seconds"], .5)
        self.assertEqual(report["coverage_ratio"], .25)
        self.assertEqual(report["valid_sample_rate_hz"], 1)
        self.assertEqual(report["interval_seconds"]["count"], 1)
        self.assertAlmostEqual(report["inference_latency_seconds"]["mean"], .05)
        self.assertAlmostEqual(report["left_ear"]["mean"], .4)

    def test_no_extrapolation_before_first_or_after_last_observation(self):
        target = window()
        target.observe(observation(1, .5))
        self.assertEqual(target.snapshot(1)["coverage_seconds"], 0)
        target.observe(observation(2, 1))
        self.assertEqual(target.snapshot(2)["coverage_seconds"], .5)
        self.assertEqual(target.snapshot(4)["coverage_seconds"], 0)
        self.assertEqual(target.snapshot(4)["valid_samples"], 0)

    def test_window_clip_uses_one_anchor_excluded_from_counts(self):
        target = window(duration=1, gap=2)
        for frame, acquired in ((1, 0), (2, .5), (3, 1.5)):
            target.observe(observation(frame, acquired))
        report = target.snapshot(1.75)
        self.assertEqual(report["anchor_samples"], 1)
        self.assertEqual(report["retained_samples"], 2)
        self.assertEqual(report["valid_samples"], 1)
        self.assertEqual(report["coverage_seconds"], .75)
        self.assertEqual(report["interval_seconds"]["mean"], 1)

    def test_invalid_observation_breaks_both_adjacent_intervals(self):
        target = window()
        target.observe(observation(1, 0))
        target.observe(observation(2, .5, geometry=invalid_eye_geometry("no_face", 640, 480)))
        target.observe(observation(3, 1))
        target.observe(observation(4, 1.5))
        report = target.snapshot(1.5)
        self.assertEqual(report["valid_samples"], 3)
        self.assertEqual(report["invalid_samples"], 1)
        self.assertEqual(report["reason_counts"], {"no_face": 1})
        self.assertEqual(report["coverage_seconds"], .5)
        self.assertEqual(report["left_ear"]["count"], 3)

    def test_long_gap_is_rejected_then_new_segment_can_start(self):
        target = window(duration=10, gap=1)
        target.observe(observation(1, 0))
        self.assertFalse(target.observe(observation(2, 3)))
        self.assertTrue(target.observe(observation(3, 3.5)))
        self.assertTrue(target.observe(observation(4, 4)))
        report = target.snapshot(4)
        self.assertEqual(report["valid_samples"], 3)
        self.assertEqual(report["reason_counts"], {"excessive_gap": 1})
        self.assertEqual(report["coverage_seconds"], .5)

    def test_duplicate_out_of_order_missing_and_wrong_session_cut_continuity(self):
        cases = [
            (observation(1, .5), "duplicate_frame"),
            (observation(0, .5), "out_of_order_frame"),
            (observation(None, .5), "missing_frame_identity"),
            (observation(True, .5), "invalid_frame_identity"),
            (observation(2, .5, session=None), "missing_session"),
            (observation(999, 100, session="old"), "wrong_session"),
        ]
        for rejected, reason in cases:
            with self.subTest(reason=reason):
                target = window()
                target.observe(observation(1, 0))
                self.assertFalse(target.observe(rejected))
                self.assertTrue(target.observe(observation(3, 1)))
                report = target.snapshot(1)
                self.assertEqual(report["valid_samples"], 2)
                self.assertEqual(report["reason_counts"], {reason: 1})
                self.assertEqual(report["coverage_seconds"], 0)

    def test_bad_times_and_backward_monotonic_time_reject(self):
        for overrides, reason in (({"acquired_monotonic_s": math.nan}, "invalid_timestamp"),
                                  ({"completed_monotonic_s": math.inf}, "invalid_timestamp"),
                                  ({"completed_monotonic_s": .2}, "invalid_timestamp"),
                                  ({"captured_at_ms": True}, "invalid_timestamp"),
                                  ({"acquired_monotonic_s": 0}, "nonincreasing_timestamp")):
            target = window()
            target.observe(observation(1, 0))
            self.assertFalse(target.observe(observation(2, .5, **overrides)))
            target.observe(observation(3, 1))
            report = target.snapshot(1)
            self.assertEqual(report["reason_counts"], {reason: 1})
            self.assertEqual(report["coverage_seconds"], 0)

    def test_frame_and_time_highwater_never_roll_back_after_rejection(self):
        target = window(duration=10, gap=10)
        target.observe(observation(10, 5))
        target.observe(observation(8, 4))
        self.assertFalse(target.observe(observation(9, 4.5)))
        self.assertFalse(target.observe(observation(11, 4.9)))
        self.assertFalse(target.observe(observation(11, 5.5)))
        self.assertTrue(target.observe(observation(12, 6)))
        report = target.snapshot(6)
        self.assertEqual(report["valid_samples"], 2)
        self.assertEqual(report["coverage_seconds"], 0)
        self.assertEqual(report["reason_counts"], {"out_of_order_frame": 2, "nonincreasing_timestamp": 1, "duplicate_frame": 1})

    def test_invalid_geometry_still_advances_identity_watermark(self):
        target = window()
        target.observe(observation(1, 0, geometry=invalid_eye_geometry("no_face", 640, 480)))
        self.assertFalse(target.observe(observation(1, .5)))
        self.assertEqual(target.snapshot(.5)["valid_samples"], 0)

    def test_capacity_includes_anchor_and_does_not_claim_evicted_coverage(self):
        target = window(duration=2, capacity=2)
        for frame, acquired in enumerate((0, .5, 1, 1.5), 1):
            target.observe(observation(frame, acquired))
        report = target.snapshot(1.5)
        self.assertEqual(report["retained_samples"], 2)
        self.assertEqual(report["valid_samples"], 2)
        self.assertEqual(report["coverage_seconds"], .5)
        self.assertTrue(report["capacity_truncated"])
        self.assertEqual(report["capacity_evicted_since_reset"], 2)
        self.assertFalse(target.snapshot(4)["capacity_truncated"])
        target = window(duration=.5, capacity=1)
        target.observe(observation(1, 0))
        target.observe(observation(2, .5))
        report = target.snapshot(.75)
        self.assertEqual(report["retained_samples"], 1)
        self.assertEqual(report["coverage_seconds"], 0)
        self.assertTrue(report["capacity_truncated"])

    def test_capacity_bound_counts_invalid_samples_too(self):
        target = window(duration=60, capacity=3)
        for index in range(30):
            target.observe(observation(index, index / 10, geometry=invalid_eye_geometry("no_face", 640, 480)))
        report = target.snapshot(3)
        self.assertEqual(report["retained_samples"], 3)
        self.assertEqual(report["invalid_samples"], 3)
        self.assertEqual(report["capacity_evicted_since_reset"], 27)

    def test_time_expiry_keeps_at_most_one_anchor_without_capacity_truncation(self):
        target = window(duration=.4)
        for index in range(10):
            target.observe(observation(index, index / 2))
        report = target.snapshot(4.5)
        self.assertEqual(report["retained_samples"], 2)
        self.assertEqual(report["anchor_samples"], 1)
        self.assertAlmostEqual(report["coverage_seconds"], .4)
        self.assertFalse(report["capacity_truncated"])

    def test_reset_clears_counts_evictions_identity_and_continuity(self):
        target = window(capacity=1)
        target.observe(observation(100, 10))
        target.observe(observation(101, 10.5))
        target.reset("new")
        self.assertTrue(target.observe(observation(0, 0, session="new")))
        report = target.snapshot(0)
        self.assertEqual(report["total_observations_since_reset"], 1)
        self.assertFalse(report["capacity_truncated"])
        self.assertEqual(report["coverage_seconds"], 0)
        self.assertEqual(report["capacity_evicted_since_reset"], 0)

    def test_same_session_reset_clears_records_but_preserves_ordering(self):
        target = window()
        target.observe(observation(100, 10))
        target.reset("synthetic")
        self.assertEqual(target.snapshot(10)["retained_samples"], 0)
        self.assertFalse(target.observe(observation(99, 9)))
        self.assertFalse(target.observe(observation(100, 10.5)))
        self.assertTrue(target.observe(observation(101, 11)))
        self.assertEqual(target.snapshot(11)["coverage_seconds"], 0)
        target.reset(None)
        target.reset("synthetic")
        self.assertTrue(target.observe(observation(0, 0)))

    def test_malformed_geometry_and_invalid_as_of_fail_closed(self):
        target = window()
        target.observe(observation(1, 1, geometry=replace(observation().geometry, left_ear=math.nan)))
        self.assertEqual(target.snapshot(1)["reason_counts"], {"invalid_geometry": 1})
        for as_of in (math.nan, math.inf, True, -.5, .5):
            with self.assertRaises(ValueError):
                target.snapshot(as_of)


if __name__ == "__main__":
    unittest.main()
