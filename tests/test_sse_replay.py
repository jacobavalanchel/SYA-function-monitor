"""Bounded observation resume without cameras, SDKs, or durable storage."""
import importlib.util
import http.client
import io
import json
from pathlib import Path
import queue
import threading
import unittest
from unittest import mock

import server
from realsense_runtime import EventBroker, attention_event_id, attention_observation

spec = importlib.util.spec_from_file_location("monitor_mock_replay", Path(__file__).resolve().parents[1] / "mock/server.py")
mock_server = importlib.util.module_from_spec(spec)
spec.loader.exec_module(mock_server)


def observation(number):
    record = attention_observation({"state": "focused", "source": "test", "confidence": .9}, None,
                                   "test-session", 500, 1000 + number * 500)
    record["id"] = f"observation-{number}"
    return record


def drain(subscriber):
    messages = []
    while True:
        try:
            messages.append(subscriber.get_nowait())
        except queue.Empty:
            return messages


class SseReplayTests(unittest.TestCase):
    def test_replay_is_exclusive_stable_and_live_resumes_without_payload_mutation(self):
        broker = EventBroker()
        live = broker.subscribe()
        originals = [observation(i) for i in range(3)]
        for record in originals:
            broker.publish("attention.status", record)
        resumed = broker.subscribe(originals[0]["id"])
        replay = drain(resumed)
        self.assertEqual([message["payload"]["id"] for message in replay], [record["id"] for record in originals[1:]])
        for message, original in zip(replay, originals[1:]):
            self.assertEqual(message["payload"], {**original, "delivery": "replay"})
        self.assertTrue(all(message["payload"]["delivery"] == "live" for message in drain(live)))
        broker.publish("attention.status", observation(3))
        self.assertEqual(resumed.get_nowait()["payload"], observation(3))

    def test_repeated_cursor_and_duplicate_publish_are_idempotent(self):
        broker = EventBroker()
        for i in range(3):
            broker.publish("attention.status", observation(i))
        broker.publish("attention.status", observation(1))
        first = drain(broker.subscribe("observation-0"))
        second = drain(broker.subscribe("observation-0"))
        self.assertEqual(first, second)
        self.assertEqual(len(first), 2)
        first[0]["payload"]["state"] = "distracted"
        self.assertEqual(drain(broker.subscribe("observation-0")), second)
        self.assertEqual(drain(broker.subscribe("observation-2")), [])

    def test_absent_unknown_evicted_and_previous_process_cursors_are_live_only(self):
        broker = EventBroker(replay_size=2)
        for i in range(3):
            broker.publish("attention.status", observation(i))
        for cursor in (None, "", "unknown", "observation-0"):
            subscriber = broker.subscribe(cursor)
            self.assertEqual(drain(subscriber), [])
            broker.unsubscribe(subscriber)
        self.assertEqual(drain(EventBroker().subscribe("observation-2")), [])
        self.assertEqual([message["payload"]["id"] for message in drain(broker.subscribe("observation-1"))], ["observation-2"])

    def test_default_retention_is_2048_attention_events_and_never_preview_images(self):
        broker = EventBroker()
        for i in range(2050):
            broker.publish("attention.status", observation(i))
            broker.publish("realsense.frame", {"id": f"frame-{i}", "rgb": "not-retained"})
        self.assertEqual(len(broker._retained), 2048)
        self.assertEqual(drain(broker.subscribe("observation-1")), [])
        replay = drain(broker.subscribe("observation-2"))
        self.assertEqual(len(replay), 2047)
        self.assertTrue(all(message["event"] == "attention.status" for message in replay))

    def test_preview_pressure_does_not_evict_pending_replay(self):
        broker = EventBroker(queue_size=2)
        for i in range(10):
            broker.publish("attention.status", observation(i))
        subscriber = broker.subscribe("observation-0")
        for i in range(10):
            broker.publish("realsense.frame", {"frame": i})
        messages = drain(subscriber)
        self.assertEqual([message["payload"]["id"] for message in messages[:9]], [f"observation-{i}" for i in range(1, 10)])
        self.assertEqual([message["payload"]["frame"] for message in messages[9:]], [8, 9])

    def test_attention_overflow_closes_before_later_cursor_and_replays_every_missed_id(self):
        broker = EventBroker(queue_size=2)
        subscriber = broker.subscribe()
        broker.publish("attention.status", observation(0))
        cursor = subscriber.get_nowait()["payload"]["id"]
        broker.publish("attention.status", observation(1))
        broker.publish("realsense.frame", {"frame": 1})
        broker.publish("realsense.frame", {"frame": 2})
        broker.publish("attention.status", observation(2))
        for i in range(100):
            broker.publish("realsense.frame", {"frame": i})
        self.assertLessEqual(subscriber._live.qsize(), 2)
        with self.assertRaises(ConnectionAbortedError):
            subscriber.get_nowait()
        broker.unsubscribe(subscriber)
        resumed = broker.subscribe(cursor)
        self.assertEqual([message["payload"] for message in drain(resumed)],
                         [{**observation(i), "delivery": "replay"} for i in (1, 2)])
        broker.publish("attention.status", observation(3))
        self.assertEqual(resumed.get_nowait()["payload"], observation(3))

    def test_real_and_mock_overflow_ends_http_connection_before_skipping_observations(self):
        for module, handler_class, method in ((server, server.ProxyHandler, "_send_event_stream"),
                                             (mock_server, mock_server.MockHandler, "_handle_sse")):
            broker = EventBroker(queue_size=1)
            broker.publish("attention.status", observation(0))
            class Wire(io.BytesIO):
                def flush(self):
                    broker.publish("attention.status", observation(1))
                    broker.publish("realsense.frame", {"frame": 1})
                    broker.publish("attention.status", observation(2))
            handler = object.__new__(handler_class)
            handler.headers = {"Last-Event-ID": "observation-0"}
            handler.wfile = Wire()
            handler.send_response = mock.Mock()
            handler.send_header = mock.Mock()
            handler.end_headers = mock.Mock()
            handler._cors = mock.Mock()
            handler.close_connection = False
            with mock.patch.object(module, "EVENT_BROKER", broker):
                getattr(handler, method)()
            self.assertTrue(handler.close_connection)
            self.assertNotIn(b"event: attention.status", handler.wfile.getvalue())
            self.assertEqual(broker._subscribers, set())
            self.assertEqual([message["payload"]["id"] for message in drain(broker.subscribe("observation-0"))],
                             ["observation-1", "observation-2"])

    def test_publish_subscribe_boundary_neither_loses_nor_duplicates_an_observation(self):
        for _ in range(30):
            broker = EventBroker()
            broker.publish("attention.status", observation(0))
            gate = threading.Barrier(2)
            def publish():
                gate.wait()
                broker.publish("attention.status", observation(1))
            worker = threading.Thread(target=publish)
            worker.start()
            gate.wait()
            subscriber = broker.subscribe("observation-0")
            worker.join(timeout=1)
            self.assertFalse(worker.is_alive())
            self.assertEqual([message["payload"]["id"] for message in drain(subscriber)], ["observation-1"])

    def test_retained_snapshot_survives_provider_mutation_and_invalid_ids_never_set_cursor(self):
        broker = EventBroker()
        broker.publish("attention.status", observation(0))
        original = observation(1)
        broker.publish("attention.status", original)
        original["state"] = "distracted"
        self.assertEqual(drain(broker.subscribe("observation-0"))[0]["payload"]["state"], "focused")
        for event_id in (None, "", "bad\nline", "bad\rline", "bad\0line"):
            self.assertEqual(attention_event_id({"event": "attention.status", "payload": {"id": event_id}}), "")

    def test_real_and_mock_handlers_honor_last_event_id_then_emit_live(self):
        for module, handler_class, method in ((server, server.ProxyHandler, "_send_event_stream"),
                                             (mock_server, mock_server.MockHandler, "_handle_sse")):
            with self.subTest(module=module.__name__):
                broker = EventBroker()
                for i in range(3):
                    broker.publish("attention.status", observation(i))
                class Wire(io.BytesIO):
                    def flush(self):
                        wire = self.getvalue()
                        if b'"id":"observation-3"' in wire:
                            raise BrokenPipeError("synthetic client closed")
                        if wire.count(b"event: attention.status") == 2:
                            broker.publish("attention.status", observation(3))
                handler = object.__new__(handler_class)
                handler.headers = {"Last-Event-ID": "observation-0"}
                handler.wfile = Wire()
                handler.send_response = mock.Mock()
                handler.send_header = mock.Mock()
                handler.end_headers = mock.Mock()
                handler._cors = mock.Mock()
                with mock.patch.object(module, "EVENT_BROKER", broker):
                    getattr(handler, method)()
                blocks = handler.wfile.getvalue().decode().strip().split("\n\n")
                messages = [json.loads(next(line[6:] for line in block.splitlines() if line.startswith("data: "))) for block in blocks]
                records = [message["payload"] for message in messages[1:]]
                self.assertEqual([record["id"] for record in records], ["observation-1", "observation-2", "observation-3"])
                self.assertEqual([record["delivery"] for record in records], ["replay", "replay", "live"])
                for block, record in zip(blocks[1:], records):
                    self.assertEqual(block.splitlines()[0], f"id: {record['id']}")
                self.assertEqual(broker._subscribers, set())

    def test_authenticated_http_last_event_id_header_selects_replay(self):
        class FiniteBroker(EventBroker):
            def subscribe(self, last_event_id=None):
                subscriber = super().subscribe(last_event_id)
                original_get = subscriber.get
                def get(**_kwargs):
                    try:
                        return original_get(block=False)
                    except queue.Empty:
                        raise BrokenPipeError("synthetic finite stream")
                subscriber.get = get
                return subscriber
        class Handler(server.ProxyHandler):
            def log_message(self, *_args):
                pass
            def _send_event_stream(self):
                super()._send_event_stream()
                self.close_connection = True
        broker = FiniteBroker()
        for i in range(3):
            broker.publish("attention.status", observation(i))
        httpd = server.http.server.ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        worker = threading.Thread(target=httpd.serve_forever, daemon=True)
        connection = http.client.HTTPConnection("127.0.0.1", httpd.server_address[1], timeout=2)
        try:
            with mock.patch.object(server, "EVENT_BROKER", broker), mock.patch.object(server, "RUNTIME_TOKEN", "resume-token"):
                worker.start()
                connection.request("GET", "/api/monitor/events", headers={"Authorization": "Bearer resume-token", "last-event-id": "observation-1"})
                response = connection.getresponse()
                self.assertEqual(response.status, 200)
                wire = response.read().decode()
                self.assertIn("id: observation-2\n", wire)
                self.assertNotIn("id: observation-1\n", wire)
                self.assertIn('"delivery":"replay"', wire)
                self.assertEqual(broker._subscribers, set())
        finally:
            connection.close()
            httpd.shutdown()
            httpd.server_close()
            worker.join(timeout=2)

    def test_real_and_mock_release_subscriber_when_sse_handshake_fails(self):
        for module, handler_class, method in ((server, server.ProxyHandler, "_send_event_stream"),
                                             (mock_server, mock_server.MockHandler, "_handle_sse")):
            broker = EventBroker()
            handler = object.__new__(handler_class)
            handler.headers = {}
            handler.send_response = mock.Mock(side_effect=BrokenPipeError("client closed during handshake"))
            with mock.patch.object(module, "EVENT_BROKER", broker):
                getattr(handler, method)()
            self.assertEqual(broker._subscribers, set())


if __name__ == "__main__":
    unittest.main()
