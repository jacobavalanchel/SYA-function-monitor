"""MON-018: fresh provider observations, lifecycle boundaries and mock parity."""
import importlib.util
import io
import json
from pathlib import Path
import queue
import threading
import unittest
import uuid
from unittest import mock

import server
from realsense_runtime import EventBroker, FocusEstimator, attention_observation
from test_realsense_runtime import FakeNumpy, FakeProcessor, centered_landmarks
from test_server_realsense import FakeFrameSet, BrokenPipeline, FakeDetector

ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location("monitor_mock_heartbeat", ROOT / "mock/server.py")
mock_server = importlib.util.module_from_spec(spec)
spec.loader.exec_module(mock_server)


class FramesPipeline:
    def __init__(self, adapter, count):
        self.adapter = adapter
        self.remaining = count
        self.stopped = False

    def wait_for_frames(self, _timeout):
        if not self.remaining:
            self.adapter.stop_event.set()
            raise RuntimeError("test input ended")
        self.remaining -= 1
        return FakeFrameSet()

    def stop(self):
        self.stopped = True


class ObservationTests(unittest.TestCase):
    def setUp(self):
        self.broker = EventBroker(queue_size=100)
        self.patch = mock.patch.object(server, "EVENT_BROKER", self.broker)
        self.patch.start()
        self.subscriber = self.broker.subscribe()
        self.addCleanup(self.patch.stop)
        self.addCleanup(self.broker.unsubscribe, self.subscriber)

    def events(self):
        events = []
        while True:
            try:
                event = self.subscriber.get_nowait()
                if event["event"] == "attention.status":
                    events.append(event["payload"])
            except queue.Empty:
                return events

    def assert_contract(self, record):
        schema = json.loads((ROOT / "api.openapi.json").read_text())["components"]["schemas"]
        required = schema["AttentionRecord"]["required"] + schema["AttentionObservation"]["allOf"][1]["required"]
        self.assertTrue(set(required) <= record.keys())
        self.assertEqual(record["timestamp"], record["observedAt"])
        self.assertEqual(record["delivery"], "live")
        self.assertLessEqual(record["stateChangedAt"], record["observedAt"])
        self.assertGreater(record["expectedIntervalMs"], 0)
        uuid.UUID(record["sessionId"])
        self.assertNotIn("energy", record)
        if record["kind"] == "boundary":
            self.assertEqual(record["state"], "unknown")
            self.assertIn(record["boundary"], ("stopped", "disconnected"))
        else:
            self.assertNotIn("boundary", record)

    def adapter(self, count=1):
        adapter = server.RealSenseAdapter()
        adapter.pipeline = FramesPipeline(adapter, count)
        adapter.active = True
        adapter.stream_config["previewEnabled"] = False
        # Avoid SDK enumeration/OS probing in lifecycle tests.
        adapter.status = lambda: {"active": adapter.active}
        return adapter

    def test_multiple_focused_frames_have_fresh_ids_and_fixed_transition_time(self):
        adapter = self.adapter(5)
        face = type("Face", (), {"landmark": centered_landmarks()})()
        adapter.focus_detector = FocusEstimator(history_size=1, processor=FakeProcessor([face]), numpy_module=FakeNumpy())
        with mock.patch.object(server.time, "monotonic", side_effect=[0, .1, .5, .6, 1]), mock.patch.object(
            server.time, "time", side_effect=[1, 1.1, 1.5, 1.6, 2]
        ):
            adapter._stream_loop(adapter.pipeline, adapter.stop_event)
        records = self.events()
        self.assertEqual([record["observedAt"] for record in records], [1000, 1500, 2000])
        self.assertEqual([record["kind"] for record in records], ["transition", "observation", "observation"])
        self.assertEqual({record["stateChangedAt"] for record in records}, {1000})
        self.assertEqual({record["expectedIntervalMs"] for record in records}, {500})
        self.assertEqual(len({record["id"] for record in records}), 3)
        self.assertEqual({record["state"] for record in records}, {"focused"})
        for record in records:
            self.assert_contract(record)
        before = dict(adapter.latest_attention)
        adapter.latest_frame()
        adapter.status()
        self.assertEqual(adapter.latest_attention, before)
        self.assertEqual(self.events(), [])

    def test_same_millisecond_observations_and_state_source_changes(self):
        session = str(uuid.uuid4())
        sample = {"state": "focused", "source": "realsense-face-mesh", "confidence": None, "reason": "test"}
        first = attention_observation(sample, None, session, 500, 1000)
        repeated = attention_observation(sample, first, session, 500, 1000)
        changed = attention_observation({**sample, "state": "away"}, repeated, session, 500, 2000)
        new_source = attention_observation({**sample, "state": "away", "source": "other"}, changed, session, 500, 3000)
        self.assertNotEqual(first["id"], repeated["id"])
        self.assertEqual(repeated["stateChangedAt"], 1000)
        self.assertEqual(changed["stateChangedAt"], 2000)
        self.assertEqual(new_source["stateChangedAt"], 3000)
        self.assertEqual(new_source["kind"], "transition")

    def test_debounce_still_reports_new_sample_without_premature_transition(self):
        estimator = FocusEstimator(history_size=2, processor=object(), numpy_module=object())
        estimator.observe("focused", 1000, "centered")
        stable, _ = estimator.observe("focused", 1500, "centered")
        pending, changed = estimator.observe("away", 2000, "no face")
        self.assertEqual(pending["state"], "focused")
        self.assertFalse(changed)
        self.assertEqual(pending["timestamp"], 2000)
        self.assertNotEqual(stable["id"], pending["id"])

    def test_inference_error_is_boundary_and_clears_debounce_history(self):
        adapter = self.adapter()
        adapter.focus_detector = FocusEstimator(processor=FakeProcessor(error=RuntimeError("bad inference")), numpy_module=FakeNumpy())
        adapter.focus_detector.history.extend(["focused"] * 4)
        with mock.patch.object(server.time, "time", side_effect=[1, 2]):
            adapter._stream_loop(adapter.pipeline, adapter.stop_event)
        records = self.events()
        self.assertEqual(len(records), 1)
        self.assertEqual(records[0]["boundary"], "disconnected")
        self.assertEqual(records[0]["observedAt"], 2000, "Failure boundary uses failure time, not frame time")
        self.assertEqual(list(adapter.focus_detector.history), [])
        self.assert_contract(records[0])

    def test_missing_detector_support_is_boundary_not_fresh_observation(self):
        adapter = self.adapter()
        detector = FocusEstimator(processor=object(), numpy_module=object())
        detector.available = False
        detector.error = "Unavailable in this fixture"
        adapter.focus_detector = detector
        adapter._stream_loop(adapter.pipeline, adapter.stop_event)
        self.assertEqual(self.events()[0]["kind"], "boundary")

    def test_acquisition_failure_and_stop_have_explicit_current_session_boundaries(self):
        adapter = self.adapter()
        adapter.pipeline = BrokenPipeline()
        adapter._stream_loop(adapter.pipeline, adapter.stop_event)
        record = self.events()[0]
        self.assertEqual(record["boundary"], "disconnected")
        self.assertEqual(record["sessionId"], adapter.attention_session_id)
        self.assert_contract(record)
        adapter = self.adapter()
        adapter.stop()
        record = self.events()[0]
        self.assertEqual(record["boundary"], "stopped")
        self.assert_contract(record)
        adapter.stop()
        self.assertEqual(self.events(), [])

    def test_old_pipeline_cannot_publish_into_new_session(self):
        adapter = self.adapter()
        old_pipeline = BrokenPipeline()
        old_stop = threading.Event()
        adapter._stream_loop(old_pipeline, old_stop)
        self.assertEqual(self.events(), [])
        self.assertIsNone(adapter.latest_attention)
        self.assertTrue(adapter.active)

    def test_stop_during_inference_has_no_late_observation(self):
        adapter = self.adapter()
        entered, release = threading.Event(), threading.Event()
        detector = FakeDetector()
        def analyze(*_args):
            entered.set()
            if not release.wait(2):
                raise RuntimeError("test release timeout")
            return {"state": "focused", "source": "realsense-face-mesh", "confidence": None, "reason": "test"}, True, True
        detector.analyze = analyze
        adapter.focus_detector = detector
        worker = threading.Thread(target=adapter._stream_loop, args=(adapter.pipeline, adapter.stop_event))
        adapter.worker = worker
        worker.start()
        stopper = None
        try:
            self.assertTrue(entered.wait(2))
            stopper = threading.Thread(target=adapter.stop)
            stopper.start()
            self.assertTrue(adapter.stop_event.wait(2))
        finally:
            release.set()
            worker.join(3)
            if stopper:
                stopper.join(3)
        self.assertFalse(worker.is_alive())
        self.assertFalse(stopper.is_alive())
        records = self.events()
        self.assertEqual([record["kind"] for record in records], ["boundary"])
        self.assertEqual(records[0]["boundary"], "stopped")
        self.assertIsNone(adapter.latest_frame_data)
        self.assertIsNone(adapter.latest_raw_frame)

    def test_successful_restart_gets_new_session_and_idempotent_start_does_not(self):
        adapter = server.RealSenseAdapter()
        sdk = mock.MagicMock()
        adapter.rs = sdk
        info = {"productId": "0b64", "firmwareVersion": "1.5.8.1", "serialNumber": "synthetic"}
        with mock.patch.object(adapter, "_query_devices", return_value=[object()]), mock.patch.object(
            adapter, "_device_info", return_value=info
        ), mock.patch.object(adapter, "_choose_video_profile", return_value={"width": 2, "height": 1, "fps": 30}), mock.patch.object(
            server, "FocusEstimator", side_effect=FakeDetector
        ), mock.patch.object(server.threading, "Thread") as thread, mock.patch.object(
            adapter, "status", side_effect=lambda: {"active": adapter.active}
        ), mock.patch.object(adapter, "_records_dir", return_value="/synthetic"):
            thread.return_value.is_alive.return_value = False
            adapter.start()
            first_session = adapter.attention_session_id
            adapter.start()
            self.assertEqual(adapter.attention_session_id, first_session)
            adapter.stop()
            self.assertEqual(self.events()[0]["sessionId"], first_session)
            adapter.start()
            self.assertNotEqual(adapter.attention_session_id, first_session)
            self.assertIsNone(adapter.latest_attention)
            adapter.stop()

    def test_expected_interval_follows_focus_and_color_cadence(self):
        adapter = self.adapter()
        sample = {"state": "focused", "source": "test", "confidence": None, "reason": "test"}
        adapter.stream_config.update(focusFps=5, colorFps=30)
        self.assertEqual(adapter._observation(sample, 1)["expectedIntervalMs"], 200)
        adapter.stream_config.update(focusFps=5, colorFps=1)
        self.assertEqual(adapter._observation(sample, 1)["expectedIntervalMs"], 1000)

    def test_mock_matches_observation_stop_restart_and_failure_contract(self):
        mock_server.CAMERA["isActive"] = False
        with mock.patch.object(mock_server, "_broadcast_event") as broadcast:
            mock_server._h_camera_start()
            first = mock_server._h_attention_status_set({"status": "focused"})
            second = mock_server._h_attention_status_set({"status": "focused"})
            self.assertEqual(second["kind"], "observation")
            self.assertNotEqual(first["id"], second["id"])
            self.assertEqual(first["stateChangedAt"], second["stateChangedAt"])
            self.assert_contract(second)
            failure = mock_server._h_attention_status_set({"status": "unknown", "boundary": "disconnected"})
            self.assert_contract(failure)
            mock_server._h_camera_stop()
            before = broadcast.call_count
            rejected = mock_server._h_attention_status_set({"status": "focused"})
            self.assertFalse(rejected["ok"])
            self.assertEqual(broadcast.call_count, before)
            mock_server._h_camera_start()
            restarted = mock_server._h_attention_status_set({"status": "focused"})
            self.assertNotEqual(restarted["sessionId"], first["sessionId"])
            self.assertEqual(restarted["kind"], "transition")
            self.assert_contract(restarted)
            mock_server._h_camera_stop()

    def test_sse_record_id_matches_payload_and_unsubscribes_on_disconnect(self):
        sample = {"state": "focused", "source": "test", "confidence": None, "reason": "test"}
        record = attention_observation(sample, None, str(uuid.uuid4()), 500, 1000)
        owner = self
        class Wire(io.BytesIO):
            def flush(self):
                if b"event: attention.status" in self.getvalue():
                    raise BrokenPipeError("test client closed")
                owner.broker.publish("attention.status", record)
        handler = object.__new__(server.ProxyHandler)
        handler.headers = {}
        handler.wfile = Wire()
        handler.send_response = mock.Mock()
        handler.send_header = mock.Mock()
        handler.end_headers = mock.Mock()
        handler._send_event_stream()
        blocks = handler.wfile.getvalue().decode().strip().split("\n\n")
        self.assertEqual(blocks[1].splitlines()[0], f"id: {record['id']}")
        payload = json.loads(next(line[6:] for line in blocks[1].splitlines() if line.startswith("data: ")))
        self.assertEqual(payload["payload"], record)
        self.assertEqual(self.broker._subscribers, {self.subscriber})


if __name__ == "__main__":
    unittest.main()
