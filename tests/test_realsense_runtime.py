import base64
import io
import queue
import unittest

from realsense_runtime import (
    EventBroker,
    FocusEstimator,
    classify_attention_landmarks,
    encode_rgb_jpeg,
)


class Point:
    def __init__(self, x=0.5, y=0.5):
        self.x = x
        self.y = y


class FakeImage:
    def reshape(self, _shape):
        return self


class FakeNumpy:
    uint8 = object()

    @staticmethod
    def frombuffer(_data, dtype=None):
        del dtype
        return FakeImage()


class FakeProcessor:
    def __init__(self, faces=None, error=None):
        self.faces = faces
        self.error = error

    def process(self, _image):
        if self.error:
            raise self.error
        return type("Result", (), {"multi_face_landmarks": self.faces})()


def centered_landmarks():
    landmarks = [Point() for _index in range(478)]
    coordinates = {
        33: (0.0, 0.5),
        173: (1.0, 0.5),
        263: (1.0, 0.5),
        398: (0.0, 0.5),
        222: (0.0, 0.0),
        230: (0.0, 1.0),
        442: (1.0, 0.0),
        450: (1.0, 1.0),
        468: (0.5, 0.4),
        473: (0.5, 0.4),
        4: (0.5, 0.55),
        152: (0.5, 1.0),
        10: (0.5, 0.0),
    }
    for index, (x, y) in coordinates.items():
        landmarks[index] = Point(x, y)
    return landmarks


class RealSenseRuntimeTests(unittest.TestCase):
    def test_centered_landmarks_are_focused(self):
        state, directions, ratios = classify_attention_landmarks(centered_landmarks())
        self.assertEqual(state, "focused")
        self.assertTrue(all(value == "center" for value in directions.values()))
        self.assertAlmostEqual(ratios["faceHorizontal"], 0.5)

    def test_off_axis_face_is_distracted(self):
        landmarks = centered_landmarks()
        landmarks[4].x = 0.9
        state, directions, _ratios = classify_attention_landmarks(landmarks)
        self.assertEqual(state, "distracted")
        self.assertEqual(directions["faceHorizontal"], "right")

    def test_focus_state_requires_five_matching_observations(self):
        estimator = FocusEstimator(history_size=5, processor=object(), numpy_module=object())
        for index in range(4):
            record, changed = estimator.observe("focused", 1000 + index, "centered")
            self.assertEqual(record["state"], "unknown")
            self.assertFalse(changed)
        record, changed = estimator.observe("focused", 1004, "centered")
        self.assertEqual(record["state"], "focused")
        self.assertTrue(changed)

    def test_no_person_becomes_away_after_debounce(self):
        estimator = FocusEstimator(
            history_size=5,
            processor=FakeProcessor(faces=[]),
            numpy_module=FakeNumpy(),
        )
        for index in range(4):
            record, face_detected, changed = estimator.analyze(b"rgb", 1, 1, 1000 + index)
            self.assertEqual(record["state"], "unknown")
            self.assertFalse(face_detected)
            self.assertFalse(changed)

        record, face_detected, changed = estimator.analyze(b"rgb", 1, 1, 1004)
        self.assertEqual(record["state"], "away")
        self.assertIn("No face", record["reason"])
        self.assertIsNone(record["confidence"])
        self.assertFalse(face_detected)
        self.assertTrue(changed)

    def test_inference_exception_becomes_unknown(self):
        estimator = FocusEstimator(
            processor=FakeProcessor(error=RuntimeError("bad model output")),
            numpy_module=FakeNumpy(),
        )
        record, face_detected, changed = estimator.analyze(b"rgb", 1, 1, 1000)
        self.assertEqual(record["state"], "unknown")
        self.assertIn("bad model output", record["reason"])
        self.assertFalse(face_detected)
        self.assertTrue(changed)

    def test_event_broker_drops_oldest_when_subscriber_is_slow(self):
        broker = EventBroker(queue_size=2)
        subscriber = broker.subscribe()
        broker.publish("realsense.frame", {"frame": 1})
        broker.publish("realsense.frame", {"frame": 2})
        broker.publish("realsense.frame", {"frame": 3})
        self.assertEqual(subscriber.get_nowait()["payload"]["frame"], 2)
        self.assertEqual(subscriber.get_nowait()["payload"]["frame"], 3)
        with self.assertRaises(queue.Empty):
            subscriber.get_nowait()
        broker.unsubscribe(subscriber)

    def test_rgb_preview_is_a_jpeg(self):
        encoded = encode_rgb_jpeg(bytes([255, 0, 0, 0, 255, 0]), 2, 1, 72)
        raw = base64.b64decode(encoded)
        self.assertEqual(raw[:2], b"\xff\xd8")
        from PIL import Image

        image = Image.open(io.BytesIO(raw))
        self.assertEqual(image.size, (2, 1))


if __name__ == "__main__":
    unittest.main()
