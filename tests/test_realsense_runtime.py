import base64
import io
import queue
import unittest

from realsense_runtime import (
    EventBroker,
    FocusEstimator,
    classify_attention_landmarks,
    encode_rgb_jpeg,
    utc_timestamp,
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
        self.calls = 0

    def process(self, _image):
        self.calls += 1
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
        # Fixed FaceMesh topology with distinct upper/lower separations per eye.
        160: (0.1, 0.46),
        158: (0.3, 0.45),
        133: (0.4, 0.5),
        153: (0.3, 0.57),
        144: (0.1, 0.54),
        362: (0.6, 0.5),
        385: (0.7, 0.42),
        387: (0.9, 0.4),
        373: (0.9, 0.64),
        380: (0.7, 0.58),
    }
    for index, (x, y) in coordinates.items():
        landmarks[index] = Point(x, y)
    return landmarks


class RealSenseRuntimeTests(unittest.TestCase):
    def test_eye_scalars_reuse_single_attention_inference(self):
        landmarks = centered_landmarks()
        face = type("Face", (), {"landmark": landmarks})()
        processor = FakeProcessor([face])
        estimator = FocusEstimator(history_size=1, processor=processor, numpy_module=FakeNumpy())
        record, detected, changed, geometry = estimator.analyze_frame(b"rgb", 640, 480, 1000)
        self.assertEqual(processor.calls, 1)
        self.assertEqual(record["state"], "focused")
        self.assertTrue(detected)
        self.assertTrue(changed)
        # Pixel widths are 256; lid gaps are 38.4/57.6 and 76.8/115.2.
        # Pin the external landmark mapping without calling the extractor again.
        self.assertAlmostEqual(geometry.right_ear, .1875)
        self.assertAlmostEqual(geometry.left_ear, .375)
        self.assertAlmostEqual(geometry.right_width_px, 256)
        self.assertAlmostEqual(geometry.left_width_px, 256)
        self.assertIsNone(geometry.reason)
        self.assertNotIn("energy", record)
        self.assertNotIn("geometry", record)
        self.assertEqual(len(estimator.analyze(b"rgb", 640, 480, 1500)), 3)
        self.assertEqual(processor.calls, 2)

    def test_bad_eye_geometry_does_not_change_attention(self):
        landmarks = centered_landmarks()
        landmarks[160].x = float("nan")  # Not used by attention direction.
        processor = FakeProcessor([type("Face", (), {"landmark": landmarks})()])
        estimator = FocusEstimator(history_size=1, processor=processor, numpy_module=FakeNumpy())
        record, detected, _, geometry = estimator.analyze_frame(b"rgb", 640, 480, 1000)
        self.assertEqual(record["state"], "focused")
        self.assertTrue(detected)
        self.assertEqual(geometry.reason, "non_finite_landmark")
        self.assertIsNone(geometry.left_ear)
        self.assertEqual(processor.calls, 1)

    def test_no_face_and_failed_inference_have_fixed_geometry_reasons(self):
        for processor, reason in ((FakeProcessor([]), "no_face"),
                                  (FakeProcessor(error=RuntimeError("private detail")), "inference_failed")):
            with self.subTest(reason=reason):
                estimator = FocusEstimator(processor=processor, numpy_module=FakeNumpy())
                *_, geometry = estimator.analyze_frame(b"rgb", 640, 480, 1000)
                self.assertEqual(geometry.reason, reason)
                self.assertIsNone(geometry.right_ear)
                self.assertNotIn("private detail", repr(geometry))

    def test_utc_timestamp_preserves_millisecond_session_boundary(self):
        self.assertRegex(utc_timestamp(), r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}\.\d{3}Z$")

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
