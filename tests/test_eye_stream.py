"""FOC-011 synthetic new-frame, ownership and lifecycle evidence only."""
from dataclasses import asdict
import json
from pathlib import Path
import subprocess
import sys
import tempfile
import threading
import unittest
from unittest import mock

import server
from eye_observation import EXTRACTOR_VERSION, EyeObservation, measure_eye_geometry
from realsense_runtime import FocusEstimator
from test_realsense_runtime import FakeNumpy, FakeProcessor, centered_landmarks
from test_server_realsense import BrokenPipeline, FakeFrame, FakeFrameSet, StuckWorker


class SequencePipeline:
    def __init__(self, stop_event, frames):
        self.stop_event = stop_event
        self.frames = iter(frames)

    def wait_for_frames(self, _timeout):
        try:
            return next(self.frames)
        except StopIteration:
            self.stop_event.set()
            raise RuntimeError("synthetic input ended")

    def stop(self):
        pass


class EyeStreamTests(unittest.TestCase):
    def setUp(self):
        self.patch = mock.patch.object(server, "EVENT_BROKER")
        self.patch.start()
        self.addCleanup(self.patch.stop)

    def adapter(self, frames):
        adapter = server.RealSenseAdapter()
        adapter.active = True
        adapter.stream_config["previewEnabled"] = False
        adapter.status = lambda: {"active": adapter.active}
        adapter.pipeline = SequencePipeline(adapter.stop_event, frames)
        face = type("Face", (), {"landmark": centered_landmarks()})()
        processor = FakeProcessor([face])
        adapter.focus_detector = FocusEstimator(history_size=1, processor=processor, numpy_module=FakeNumpy())
        return adapter, processor

    def seed(self, adapter, timestamp=0.0, frame=0):
        geometry = measure_eye_geometry(centered_landmarks(), 2, 1)
        adapter.eye_window.observe(EyeObservation(
            session_id=adapter.attention_session_id, color_frame_number=frame,
            captured_at_ms=1000, acquired_monotonic_s=timestamp,
            completed_monotonic_s=timestamp + .01, geometry=geometry,
        ))

    def test_new_frames_record_once_with_separate_clocks_and_no_public_fields(self):
        adapter, processor = self.adapter([FakeFrameSet(0), FakeFrameSet(15), FakeFrameSet(30)])
        with mock.patch.object(server.time, "monotonic", side_effect=[1, 1.1, 1.5, 1.6, 2, 2.1]), mock.patch.object(
            adapter.eye_window, "observe", wraps=adapter.eye_window.observe
        ) as observe:
            adapter._stream_loop(adapter.pipeline, adapter.stop_event)
        self.assertEqual(processor.calls, 3)
        self.assertEqual(observe.call_count, 3)
        observations = [call.args[0] for call in observe.call_args_list]
        self.assertEqual([item.color_frame_number for item in observations], [0, 15, 30])
        self.assertEqual([item.acquired_monotonic_s for item in observations], [1, 1.5, 2])
        self.assertEqual([item.completed_monotonic_s for item in observations], [1.1, 1.6, 2.1])
        report = adapter.eye_window.snapshot(2.1)
        self.assertEqual(report["valid_samples"], 3)
        self.assertAlmostEqual(report["coverage_seconds"], 1)
        before = adapter.eye_window.snapshot(2.1)
        adapter.latest_frame()
        adapter.status()
        self.assertEqual(adapter.eye_window.snapshot(2.1), before)
        for public in (adapter.latest_attention, adapter.latest_raw_frame):
            self.assertNotIn("geometry", public)
            self.assertNotIn("eye", public)
            self.assertNotIn("energy", public)

    def test_duplicate_color_identity_does_not_create_eye_coverage(self):
        adapter, processor = self.adapter([FakeFrameSet(0), FakeFrameSet(0)])
        with mock.patch.object(server.time, "monotonic", side_effect=[1, 1.1, 1.5, 1.6]):
            adapter._stream_loop(adapter.pipeline, adapter.stop_event)
        report = adapter.eye_window.snapshot(1.6)
        self.assertEqual(processor.calls, 2, "Keep attention's existing cadence, reuse each pass")
        self.assertEqual(report["valid_samples"], 1)
        self.assertEqual(report["invalid_samples"], 1)
        self.assertEqual(report["coverage_seconds"], 0)

    def test_missing_color_identity_never_uses_depth_identity(self):
        frames = FakeFrameSet(None)
        frames.depth = FakeFrame(bytes([1, 0, 2, 0]), 2, 1, 99)
        adapter, _ = self.adapter([frames])
        with mock.patch.object(server.time, "monotonic", side_effect=[1, 1.1]), mock.patch.object(
            adapter.eye_window, "observe", wraps=adapter.eye_window.observe
        ) as observe:
            adapter._stream_loop(adapter.pipeline, adapter.stop_event)
        self.assertIsNone(observe.call_args.args[0].color_frame_number)
        self.assertEqual(adapter.latest_raw_frame["frameNumber"], 0)
        self.assertEqual(adapter.eye_window.snapshot(1.1)["valid_samples"], 0)

    def test_frame_identity_reader_preserves_zero_and_rejects_bad_values(self):
        adapter, _ = self.adapter([])
        for value in (None, True, -1, 1.5, "42"):
            self.assertIsNone(adapter._color_frame_number(FakeFrame(b"", 1, 1, value)))
        self.assertEqual(adapter._color_frame_number(FakeFrame(b"", 1, 1, 0)), 0)
        with mock.patch.object(FakeFrame, "get_frame_number", side_effect=RuntimeError("read failed")):
            self.assertIsNone(adapter._color_frame_number(FakeFrame(b"", 1, 1, 4)))

    def test_inference_failure_clears_prior_window_and_retains_invalid_reason(self):
        adapter, processor = self.adapter([FakeFrameSet(1)])
        self.seed(adapter)
        processor.error = RuntimeError("synthetic inference failure")
        with mock.patch.object(server.time, "monotonic", side_effect=[1, 1.1]):
            adapter._stream_loop(adapter.pipeline, adapter.stop_event)
        report = adapter.eye_window.snapshot(1.1)
        self.assertEqual(report["valid_samples"], 0)
        self.assertEqual(report["invalid_samples"], 1)
        self.assertEqual(report["reason_counts"], {"inference_failed": 1})
        self.assertEqual(report["coverage_seconds"], 0)

    def test_stream_inference_failure_trace_matches_real_offline_cli(self):
        adapter, processor = self.adapter([FakeFrameSet(frame) for frame in (1, 2, 3, 4)])
        process = processor.process
        calls = 0

        def fail_second(image):
            nonlocal calls
            calls += 1
            if calls == 2:
                raise RuntimeError("synthetic inference failure")
            return process(image)

        processor.process = fail_second
        with mock.patch.object(server.time, "monotonic", side_effect=[1, 1.1, 1.5, 1.6, 2, 2.1, 2.5, 2.6]), mock.patch.object(
            adapter.eye_window, "observe", wraps=adapter.eye_window.observe
        ) as observe:
            adapter._stream_loop(adapter.pipeline, adapter.stop_event)
        report = adapter.eye_window.snapshot(2.6)
        self.assertEqual(report["valid_samples"], 2)
        self.assertEqual(report["invalid_samples"], 1)
        self.assertEqual(report["reason_counts"], {"inference_failed": 1})
        self.assertEqual(report["coverage_seconds"], .5)
        settings = {"schema_version": 1, "extractor_version": EXTRACTOR_VERSION,
                    "session_id": adapter.attention_session_id, **asdict(adapter.eye_window.config)}
        rows = [{"schema_version": 1, **asdict(call.args[0])} for call in observe.call_args_list]
        with tempfile.TemporaryDirectory() as directory:
            directory = Path(directory)
            config = directory / "config.json"
            source = directory / "observations.jsonl"
            config.write_text(json.dumps(settings), encoding="utf-8")
            source.write_text("".join(json.dumps(row) + "\n" for row in rows), encoding="utf-8")
            cli = Path(__file__).resolve().parents[1] / "scripts/evaluate-eye-observations.py"
            result = subprocess.run([sys.executable, str(cli), "--config", str(config), "--input", str(source)],
                                    capture_output=True, text=True, timeout=5)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.stderr, "")
        self.assertEqual(json.loads(result.stdout), report)

    def test_inference_failure_cannot_reopen_prior_frame_identity(self):
        adapter, processor = self.adapter([FakeFrameSet(5), FakeFrameSet(10)])
        self.seed(adapter, frame=10)
        process = processor.process
        first = True

        def fail_once(image):
            nonlocal first
            if first:
                first = False
                raise RuntimeError("synthetic failed older frame")
            return process(image)

        processor.process = fail_once
        with mock.patch.object(server.time, "monotonic", side_effect=[1, 1.1, 1.5, 1.6]):
            adapter._stream_loop(adapter.pipeline, adapter.stop_event)
        report = adapter.eye_window.snapshot(1.6)
        self.assertEqual(report["valid_samples"], 0)
        self.assertEqual(report["invalid_samples"], 2)
        self.assertEqual(report["coverage_seconds"], 0)

    def test_operational_window_bounds_follow_actual_color_cadence(self):
        adapter, _ = self.adapter([])
        adapter.stream_config.update(focusFps=5, colorFps=1)
        config = adapter._new_eye_window().config
        self.assertEqual(config.duration_seconds, 60)
        self.assertEqual(config.max_samples, 600)
        self.assertEqual(config.max_gap_seconds, 2)

    def test_stop_shutdown_and_acquisition_failure_clear_memory(self):
        for action in ("stop", "shutdown", "failure", "stop_timeout"):
            with self.subTest(action=action):
                adapter, _ = self.adapter([])
                self.seed(adapter)
                if action == "failure":
                    adapter.pipeline = BrokenPipeline()
                    adapter._stream_loop(adapter.pipeline, adapter.stop_event)
                elif action == "stop_timeout":
                    adapter.worker = StuckWorker()
                    adapter.stop()
                else:
                    getattr(adapter, action)()
                report = adapter.eye_window.snapshot(1)
                self.assertEqual(report["retained_samples"], 0)
                self.assertEqual(report["coverage_seconds"], 0)

    def test_old_worker_cannot_commit_or_clear_new_session_after_inference(self):
        adapter, processor = self.adapter([FakeFrameSet(1)])
        entered, release = threading.Event(), threading.Event()
        process = processor.process

        def paused(image):
            entered.set()
            if not release.wait(2):
                raise RuntimeError("synthetic release timeout")
            return process(image)

        processor.process = paused
        old_pipeline, old_stop = adapter.pipeline, adapter.stop_event
        worker = threading.Thread(target=adapter._stream_loop, args=(old_pipeline, old_stop))
        worker.start()
        try:
            self.assertTrue(entered.wait(2))
            with adapter.lock:
                adapter.pipeline = object()
                adapter.stop_event = threading.Event()
                adapter.attention_session_id = "new-synthetic-session"
                adapter.eye_window = adapter._new_eye_window()
                self.seed(adapter)
                before = adapter.eye_window.snapshot(1)
        finally:
            release.set()
            worker.join(3)
        self.assertFalse(worker.is_alive())
        self.assertEqual(adapter.eye_window.snapshot(1), before)
        self.assertIsNone(adapter.latest_attention)
        self.assertIsNone(adapter.latest_raw_frame)

    def test_old_acquisition_failure_cannot_clear_new_session(self):
        adapter, _ = self.adapter([])
        entered, release = threading.Event(), threading.Event()
        old_pipeline, old_stop = adapter.pipeline, adapter.stop_event

        def fail_late(_timeout):
            entered.set()
            if not release.wait(2):
                raise RuntimeError("synthetic release timeout")
            raise RuntimeError("synthetic old acquisition failure")

        old_pipeline.wait_for_frames = fail_late
        worker = threading.Thread(target=adapter._stream_loop, args=(old_pipeline, old_stop))
        worker.start()
        try:
            self.assertTrue(entered.wait(2))
            with adapter.lock:
                adapter.pipeline = object()
                adapter.stop_event = threading.Event()
                adapter.attention_session_id = "new-synthetic-session"
                adapter.eye_window = adapter._new_eye_window()
                self.seed(adapter)
                before = adapter.eye_window.snapshot(1)
        finally:
            release.set()
            worker.join(3)
        self.assertFalse(worker.is_alive())
        self.assertEqual(adapter.eye_window.snapshot(1), before)
        self.assertTrue(adapter.active)
        self.assertFalse(adapter.stop_event.is_set())


if __name__ == "__main__":
    unittest.main()
