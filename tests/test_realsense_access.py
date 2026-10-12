import importlib.util
import io
import json
from pathlib import Path
import queue
import tempfile
import threading
import unittest
from unittest import mock

import server
from realsense_runtime import (
    ACCESS_COVERAGE, EventBroker, RealSenseActionError, access_error_code,
    critical_event_id, realsense_access_event,
)

ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location("mock_realsense_access", ROOT / "mock/server.py")
mock_server = importlib.util.module_from_spec(spec)
spec.loader.exec_module(mock_server)


def drain(subscriber):
    records = []
    while True:
        try:
            records.append(subscriber.get_nowait())
        except queue.Empty:
            return records


class Pipeline:
    def __init__(self, failure=None, stop_failure=False):
        self.failure = failure or RuntimeError("synthetic disconnect")
        self.stop_failure = stop_failure
        self.stopped = False

    def wait_for_frames(self, _timeout):
        raise self.failure

    def stop(self):
        if self.stop_failure:
            raise RuntimeError("synthetic stop failure")
        self.stopped = True


class RealSenseAccessTests(unittest.TestCase):
    def setUp(self):
        self.broker = EventBroker()
        self.patch = mock.patch.object(server, "EVENT_BROKER", self.broker)
        self.patch.start()
        self.subscriber = self.broker.subscribe()
        self.adapter = server.RealSenseAdapter()
        # Status must never load a real SDK, enumerate USB, or inspect user config.
        self.adapter.import_error = "Synthetic SDK boundary"
        self.adapter._usb_realsense_devices = lambda: []
        self.adapter._records_dir = lambda: "/synthetic-unused-records"

    def tearDown(self):
        self.patch.stop()

    def access_events(self):
        return [event["payload"] for event in drain(self.subscriber) if event["event"] == "realsense.access"]

    def assert_metadata(self, event):
        schema = json.loads((ROOT / "api.openapi.json").read_text())["components"]["schemas"]["RealSenseAccessEvent"]
        self.assertEqual(set(event), set(schema["required"]))
        for name, value in event.items():
            rule = schema["properties"][name]
            if "const" in rule:
                self.assertEqual(value, rule["const"])
            if "enum" in rule:
                self.assertIn(value, rule["enum"])
        self.assertEqual(critical_event_id({"event": "realsense.access", "payload": event}), event["id"])

    def test_stop_records_one_session_boundary_and_cached_reads_do_not_add_accesses(self):
        self.adapter.active = True
        self.adapter.pipeline = Pipeline()
        status = self.adapter.stop()
        self.assertFalse(status["active"])
        self.assertEqual(status["accessCoverage"], ACCESS_COVERAGE)
        events = self.access_events()
        self.assertEqual(len(events), 1)
        self.assert_metadata(events[0])
        self.assertEqual(events[0]["sessionId"], status["sessionId"])
        self.assertEqual(events[0]["operation"], "session-stop")
        self.assertEqual(events[0]["errorCode"], "REALSENSE_STOPPED")
        self.assertEqual(events[0]["outcome"], "success")
        self.adapter.status(); self.adapter.latest_frame(); self.adapter.stop()
        self.assertEqual(self.access_events(), [])

    def test_async_permission_fault_is_metadata_only_and_stops_the_source(self):
        pipeline = Pipeline(PermissionError("/private/raw-path token=NEVER_AUDIT_THIS"))
        self.adapter.active = True
        self.adapter.pipeline = pipeline
        self.adapter._stream_loop(pipeline, self.adapter.stop_event)
        self.assertTrue(pipeline.stopped)
        self.assertFalse(self.adapter.active)
        event, = self.access_events()
        self.assert_metadata(event)
        self.assertEqual(event["operation"], "stream-error")
        self.assertEqual(event["outcome"], "denied")
        self.assertEqual(event["errorCode"], "REALSENSE_PERMISSION_DENIED")
        self.assertNotIn("private", json.dumps(event))
        self.assertNotIn("NEVER_AUDIT", json.dumps(event))

    def test_stop_failures_never_claim_inactive_for_explicit_or_async_stop(self):
        for asynchronous in (False, True):
            with self.subTest(asynchronous=asynchronous):
                pipeline = Pipeline(stop_failure=True)
                self.adapter.active = True
                self.adapter.pipeline = pipeline
                self.adapter.stop_event.clear()
                self.adapter.latest_attention = None
                if asynchronous:
                    self.adapter._stream_loop(pipeline, self.adapter.stop_event)
                else:
                    self.adapter.stop()
                event, = self.access_events()
                self.assertEqual(event["errorCode"], "REALSENSE_STOP_FAILED")
                self.assertTrue(event["active"])
                self.assertTrue(self.adapter.active)
                self.assertIs(self.adapter.pipeline, pipeline)

    def test_start_permission_and_http_error_codes_do_not_expose_raw_exception(self):
        with mock.patch.object(self.adapter, "_load_sdk", side_effect=PermissionError("/secret token=raw")):
            with self.assertRaises(RealSenseActionError) as caught:
                self.adapter.start()
        self.assertEqual(caught.exception.code, "REALSENSE_PERMISSION_DENIED")
        handler = object.__new__(server.ProxyHandler)
        handler._send_json = mock.Mock()
        with mock.patch.object(server, "dispatch_action", side_effect=caught.exception):
            handler._send_action("realsense.start")
        _, body = handler._send_json.call_args.args
        self.assertEqual(body["error"]["code"], "REALSENSE_PERMISSION_DENIED")
        self.assertNotIn("secret", json.dumps(body))
        self.assertNotIn("token", json.dumps(body))
        self.assertEqual(access_error_code(RuntimeError("No camera available"), "REALSENSE_START_FAILED"), "REALSENSE_START_FAILED")

    def test_stop_during_manual_sample_write_discards_files_and_training_event(self):
        self.adapter.active = True
        self.adapter.latest_raw_frame = {
            "capturedAt": "2026-10-13T12:00:00Z", "frameNumber": 1,
            "colorBytes": b"\xff\x00\x00", "colorWidth": 1, "colorHeight": 1,
            "depthBytes": b"\x01\x00", "depthWidth": 1, "depthHeight": 1,
        }
        original_write = self.adapter._write_color_ppm
        def write_then_stop(*args):
            result = original_write(*args)
            self.adapter.stop()
            return result
        with tempfile.TemporaryDirectory() as directory:
            self.adapter._records_dir = lambda: directory
            with mock.patch.object(self.adapter, "_write_color_ppm", side_effect=write_then_stop):
                with self.assertRaises(RealSenseActionError) as caught:
                    self.adapter.capture({"purpose": "training", "startIfNeeded": False})
            self.assertEqual(caught.exception.code, "REALSENSE_CANCELLED")
            self.assertFalse(any(path.is_file() for path in Path(directory).rglob("*")))
        self.assertEqual(self.adapter.total_samples, 0)
        self.assertFalse(any(event["event"] == "realsense.training.sample" for event in drain(self.subscriber)))

    def test_mon_017_stop_wins_final_sample_commit_and_discards_complete_files(self):
        self.adapter.active = True
        self.adapter.latest_raw_frame = {
            "capturedAt": "2026-10-13T12:00:00Z", "frameNumber": 1,
            "colorBytes": b"\xff\x00\x00", "colorWidth": 1, "colorHeight": 1,
            "depthBytes": b"\x01\x00", "depthWidth": 1, "depthHeight": 1,
        }
        metadata_written = threading.Event()
        release_write = threading.Event()
        outcomes = []
        original_write = self.adapter._write_binary_atomic

        def write_then_wait(file, data):
            original_write(file, data)
            if Path(file).name == "metadata.json":
                metadata_written.set()
                if not release_write.wait(2):
                    raise RuntimeError("Synthetic final-write barrier timed out.")

        def capture():
            try:
                outcomes.append(self.adapter.capture({"purpose": "training", "startIfNeeded": False}))
            except Exception as error:
                outcomes.append(error)

        worker = threading.Thread(target=capture)
        with tempfile.TemporaryDirectory() as directory:
            self.adapter._records_dir = lambda: directory
            with mock.patch.object(self.adapter, "_write_binary_atomic", side_effect=write_then_wait):
                try:
                    worker.start()
                    self.assertTrue(metadata_written.wait(2))
                    self.assertEqual(len(list(Path(directory).rglob("*.ppm"))), 1)
                    self.assertEqual(len(list(Path(directory).rglob("*.pgm"))), 1)
                    self.assertEqual(len(list(Path(directory).rglob("metadata.json"))), 1)
                    self.assertFalse(self.adapter.stop()["active"])
                finally:
                    release_write.set()
                    worker.join(timeout=2)
                self.assertFalse(worker.is_alive())
            self.assertFalse(any(file.is_file() for file in Path(directory).rglob("*")))
        self.assertEqual(len(outcomes), 1)
        self.assertIsInstance(outcomes[0], RealSenseActionError)
        self.assertEqual(outcomes[0].code, "REALSENSE_CANCELLED")
        self.assertEqual(self.adapter.total_samples, 0)
        self.assertEqual(self.adapter.last_sample_path, "")
        self.assertEqual([event["event"] for event in drain(self.subscriber)],
                         ["attention.status", "realsense.access"])

    def test_mon_017_sample_commit_wins_and_publishes_before_concurrent_stop(self):
        self.adapter.active = True
        self.adapter.latest_raw_frame = {
            "capturedAt": "2026-10-13T12:00:00Z", "frameNumber": 1,
            "colorBytes": b"\xff\x00\x00", "colorWidth": 1, "colorHeight": 1,
            "depthBytes": b"\x01\x00", "depthWidth": 1, "depthHeight": 1,
        }
        metadata_written = threading.Event()
        commit_entered = threading.Event()
        stop_attempted = threading.Event()
        outcomes = []
        stopped = []
        original_write = self.adapter._write_binary_atomic
        original_lock = self.adapter.lock

        def capture():
            try:
                outcomes.append(self.adapter.capture({"purpose": "training", "startIfNeeded": False}))
            except Exception as error:
                outcomes.append(error)

        def stop():
            try:
                stopped.append(self.adapter.stop())
            except Exception as error:
                stopped.append(error)

        worker = threading.Thread(target=capture)
        stopper = threading.Thread(target=stop)

        class ObservedLock:
            def __enter__(self):
                if threading.current_thread() is stopper:
                    stop_attempted.set()
                return original_lock.__enter__()

            def __exit__(self, *args):
                return original_lock.__exit__(*args)

        def write_and_mark(file, data):
            original_write(file, data)
            if Path(file).name == "metadata.json":
                metadata_written.set()

        with tempfile.TemporaryDirectory() as directory:
            def records_dir():
                if threading.current_thread() is worker and metadata_written.is_set():
                    # Pause after final validation but before publication. The
                    # competing stop has attempted the shared lock, not slept.
                    commit_entered.set()
                    if not stop_attempted.wait(2):
                        raise RuntimeError("Synthetic stop-lock barrier timed out.")
                    self.assertFalse(self.adapter.stop_event.is_set())
                return directory

            self.adapter._records_dir = records_dir
            with mock.patch.object(self.adapter, "lock", ObservedLock()), \
                    mock.patch.object(self.adapter, "_write_binary_atomic", side_effect=write_and_mark):
                try:
                    worker.start()
                    self.assertTrue(commit_entered.wait(2))
                    stopper.start()
                finally:
                    worker.join(timeout=3)
                    if stopper.ident is not None:
                        stopper.join(timeout=3)
                self.assertFalse(worker.is_alive())
                self.assertFalse(stopper.is_alive())
            self.assertEqual(len(outcomes), 1)
            self.assertIsInstance(outcomes[0], dict)
            sample = outcomes[0]
            for key in ("color", "depth"):
                self.assertTrue(Path(sample[key]["path"]).is_file())
            self.assertTrue(Path(sample["metadataPath"]).is_file())
        self.assertEqual(len(stopped), 1)
        self.assertIsInstance(stopped[0], dict)
        self.assertFalse(stopped[0]["active"])
        self.assertEqual(self.adapter.total_samples, 1)
        self.assertEqual(self.adapter.last_sample_path, sample["path"])
        events = drain(self.subscriber)
        self.assertEqual([event["event"] for event in events],
                         ["realsense.training.sample", "attention.status", "realsense.access"])
        self.assertEqual(events[0]["payload"], sample)

    def test_preview_pressure_cannot_skip_access_and_replay_preserves_identity(self):
        broker = EventBroker(queue_size=2)
        first = realsense_access_event(self.adapter.attention_session_id, "session-stop", "REALSENSE_STOPPED", False)
        second = realsense_access_event(self.adapter.attention_session_id, "stream-error", "REALSENSE_STREAM_FAILED", False)
        subscriber = broker.subscribe()
        broker.publish("realsense.access", first)
        subscriber.get_nowait()
        broker.publish("realsense.access", second)
        for index in range(20):
            broker.publish("realsense.frame", {"jpegBase64": "NOT_RETAINED", "frameNumber": index})
        with self.assertRaises(ConnectionAbortedError):
            subscriber.get_nowait()
        replay = drain(broker.subscribe(first["id"]))
        self.assertEqual(replay, [{"functionId": "monitor", "event": "realsense.access", "payload": {**second, "delivery": "replay"}}])
        self.assertEqual(len(broker._retained), 2)

    def test_mock_routes_lifecycle_and_faults_match_the_real_access_contract(self):
        expected = {item["action"]: (item["method"], item["path"]) for item in server.ACTION_DEFINITIONS if item["action"].startswith("realsense.")}
        actual = {item["action"]: (item["method"], item["path"]) for item in mock_server.ACTION_DEFINITIONS if item["action"].startswith("realsense.")}
        self.assertEqual(actual, expected)
        with mock.patch.object(mock_server, "EVENT_BROKER", self.broker):
            mock_server.REALSENSE["active"] = False
            status = mock_server._h_realsense_start()
            self.assertEqual(status["accessCoverage"], ACCESS_COVERAGE)
            self.assertTrue(status["active"])
            mock_server._h_realsense_capture({"startIfNeeded": False})
            self.assertEqual(self.access_events(), [])
            self.assertEqual(mock_server.REALSENSE["totalFrames"], 0)
            mock_server._h_realsense_stop()
            event, = self.access_events()
            self.assert_metadata(event)
            self.assertEqual(event["sessionId"], status["sessionId"])
            mock_server._h_realsense_start()
            mock_server._h_realsense_end("REALSENSE_PERMISSION_DENIED")
            event, = self.access_events()
            self.assert_metadata(event)
            self.assertEqual(event["outcome"], "denied")
            self.assertFalse(event["active"])

    def test_real_and_mock_sse_wire_replays_access_metadata_with_its_original_id(self):
        for module, handler_class, method in ((server, server.ProxyHandler, "_send_event_stream"),
                                             (mock_server, mock_server.MockHandler, "_handle_sse")):
            with self.subTest(module=module.__name__):
                broker = EventBroker()
                records = [realsense_access_event(self.adapter.attention_session_id, "session-stop", "REALSENSE_STOPPED", False) for _ in range(2)]
                for record in records:
                    broker.publish("realsense.access", record)
                class Wire(io.BytesIO):
                    def flush(self):
                        if b"event: realsense.access" in self.getvalue():
                            raise BrokenPipeError("Synthetic client closed")
                handler = object.__new__(handler_class)
                handler.headers = {"Last-Event-ID": records[0]["id"]}
                handler.wfile = Wire()
                handler.send_response = mock.Mock()
                handler.send_header = mock.Mock()
                handler.end_headers = mock.Mock()
                handler._cors = mock.Mock()
                with mock.patch.object(module, "EVENT_BROKER", broker):
                    getattr(handler, method)()
                wire = handler.wfile.getvalue().decode()
                self.assertIn(f"id: {records[1]['id']}\nevent: realsense.access", wire)
                payloads = [json.loads(line[6:]) for line in wire.splitlines() if line.startswith("data: ")]
                self.assertEqual(payloads[-1]["payload"], {**records[1], "delivery": "replay"})
                self.assertEqual(broker._subscribers, set())


if __name__ == "__main__":
    unittest.main()
