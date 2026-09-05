import json
import os
import queue
import tempfile
import unittest
from unittest import mock

import server


class FakeFrame:
    def __init__(self, data, width, height, frame_number):
        self._data = data
        self._width = width
        self._height = height
        self._frame_number = frame_number

    def get_data(self):
        return self._data

    def get_width(self):
        return self._width

    def get_height(self):
        return self._height

    def get_frame_number(self):
        return self._frame_number


class FakeFrameSet:
    def __init__(self):
        self.color = FakeFrame(bytes([255, 0, 0, 0, 255, 0]), 2, 1, 41)
        self.depth = FakeFrame(bytes([1, 0, 2, 0]), 2, 1, 41)

    def get_color_frame(self):
        return self.color

    def get_depth_frame(self):
        return self.depth


class OneFramePipeline:
    def __init__(self, adapter, frames=None):
        self.adapter = adapter
        self.frames = frames or FakeFrameSet()
        self.calls = 0
        self.stopped = False

    def wait_for_frames(self, _timeout):
        self.calls += 1
        if self.calls == 1:
            return self.frames
        self.adapter.stop_event.set()
        raise RuntimeError("stopped")

    def stop(self):
        self.stopped = True


class EmptyColorFrameSet(FakeFrameSet):
    def get_color_frame(self):
        return None


class BrokenPipeline:
    def __init__(self):
        self.stopped = False

    def wait_for_frames(self, _timeout):
        raise RuntimeError("device disconnected")

    def stop(self):
        self.stopped = True


class RealSenseAdapterTests(unittest.TestCase):
    def setUp(self):
        server.CAMERA_STATE.update(server.default_camera_status())
        server.CAMERA_STATE["source"] = "builtin"
        server.CAMERA_STATE["availableSources"] = ["builtin", "realsense"]

    def test_stream_worker_caches_and_emits_bounded_preview(self):
        adapter = server.RealSenseAdapter()
        pipeline = OneFramePipeline(adapter)
        adapter.pipeline = pipeline
        adapter.active = True
        adapter.depth_scale = 0.001
        adapter.device_serial = "f1470873"
        adapter.stream_config["focusEnabled"] = False
        subscriber = server.EVENT_BROKER.subscribe()
        try:
            adapter._stream_loop(pipeline, adapter.stop_event)
            event = subscriber.get(timeout=1)
        finally:
            server.EVENT_BROKER.unsubscribe(subscriber)

        self.assertEqual(event["event"], "realsense.frame")
        frame = adapter.latest_frame()
        self.assertEqual(frame["frameNumber"], 41)
        self.assertEqual(frame["mimeType"], "image/jpeg")
        self.assertEqual(frame["deviceSerial"], "f1470873")
        self.assertTrue(frame["depth"]["available"])
        self.assertNotIn("colorBytes", frame)
        self.assertEqual(adapter.total_frames, 1)

    def test_capture_uses_cached_frame_and_emits_training_sample(self):
        adapter = server.RealSenseAdapter()
        adapter.active = True
        adapter.depth_scale = 0.001
        adapter.latest_raw_frame = {
            "timestamp": 1000,
            "capturedAt": "2026-09-06T10:00:00Z",
            "frameNumber": 7,
            "colorBytes": bytes([255, 0, 0, 0, 255, 0]),
            "colorWidth": 2,
            "colorHeight": 1,
            "depthBytes": bytes([1, 0, 2, 0]),
            "depthWidth": 2,
            "depthHeight": 1,
        }
        adapter.latest_attention = {
            "id": "attention-test",
            "timestamp": 1000,
            "state": "focused",
            "confidence": None,
            "source": "realsense-face-mesh",
            "reason": "centered",
        }
        subscriber = server.EVENT_BROKER.subscribe()
        try:
            with tempfile.TemporaryDirectory() as records_dir:
                adapter._records_dir = lambda: records_dir
                result = adapter.capture({"purpose": "training", "sessionId": "session-1"})
                self.assertTrue(os.path.isfile(result["color"]["path"]))
                self.assertTrue(os.path.isfile(result["depth"]["path"]))
                self.assertTrue(os.path.isfile(result["metadataPath"]))
                with open(result["metadataPath"], "r", encoding="utf-8") as metadata:
                    saved = json.load(metadata)
                self.assertEqual(saved["frameNumber"], 7)
                self.assertEqual(saved["attention"]["state"], "focused")
                self.assertEqual(adapter.total_samples, 1)
                event = subscriber.get(timeout=1)
        finally:
            server.EVENT_BROKER.unsubscribe(subscriber)
        self.assertEqual(event["event"], "realsense.training.sample")

    def test_depth_only_frame_is_cached_without_preview(self):
        adapter = server.RealSenseAdapter()
        pipeline = OneFramePipeline(adapter, EmptyColorFrameSet())
        adapter.pipeline = pipeline
        adapter.active = True
        adapter.stream_config["focusEnabled"] = False
        adapter._stream_loop(pipeline, adapter.stop_event)
        self.assertIsNone(adapter.latest_frame())
        self.assertIsNotNone(adapter.latest_raw_frame["depthBytes"])
        self.assertIsNone(adapter.latest_raw_frame["colorBytes"])
        self.assertEqual(adapter.total_frames, 1)

    def test_acquisition_failure_clears_stale_frames_and_emits_unknown(self):
        adapter = server.RealSenseAdapter()
        pipeline = BrokenPipeline()
        adapter.pipeline = pipeline
        adapter.active = True
        adapter.latest_frame_data = {"frameId": "stale"}
        adapter.latest_raw_frame = {"frameNumber": 1}
        server.CAMERA_STATE["source"] = "realsense"
        server.CAMERA_STATE["isActive"] = True
        subscriber = server.EVENT_BROKER.subscribe()
        try:
            adapter._stream_loop(pipeline, adapter.stop_event)
            event = subscriber.get(timeout=1)
        finally:
            server.EVENT_BROKER.unsubscribe(subscriber)

        self.assertTrue(pipeline.stopped)
        self.assertFalse(adapter.active)
        self.assertIsNone(adapter.pipeline)
        self.assertIsNone(adapter.latest_frame())
        self.assertIsNone(adapter.latest_raw_frame)
        self.assertEqual(server.CAMERA_STATE["source"], "builtin")
        self.assertEqual(event["event"], "attention.status")
        self.assertEqual(event["payload"]["state"], "unknown")
        self.assertIn("device disconnected", event["payload"]["reason"])

    def test_stop_clears_stale_frames(self):
        adapter = server.RealSenseAdapter()
        adapter.active = True
        adapter.latest_frame_data = {"frameId": "stale"}
        adapter.latest_raw_frame = {"frameNumber": 1}
        adapter.import_error = "SDK unavailable for test"
        adapter.stop()
        self.assertIsNone(adapter.latest_frame())
        self.assertIsNone(adapter.latest_raw_frame)

    def test_session_path_rejects_traversal(self):
        adapter = server.RealSenseAdapter()
        with tempfile.TemporaryDirectory() as records_dir:
            adapter._records_dir = lambda: records_dir
            with self.assertRaisesRegex(ValueError, "sessionId"):
                adapter._sample_dir({"sessionId": "../outside"})
            with self.assertRaisesRegex(ValueError, "sessionId"):
                adapter.training_start({"sessionId": "../../outside", "autoStart": False})

    def test_records_directory_expands_home(self):
        adapter = server.RealSenseAdapter()
        with mock.patch.object(server, "load_realsense_config", return_value={"recordsDir": "~/captures"}):
            self.assertEqual(adapter._records_dir(), os.path.join(os.path.expanduser("~"), "captures"))

    def test_latest_frame_action_exists(self):
        self.assertIsNone(server.dispatch_action("realsense.frame.latest"))
        actions = {definition["action"] for definition in server.ACTION_DEFINITIONS}
        self.assertIn("realsense.frame.latest", actions)


if __name__ == "__main__":
    unittest.main()
