"""Runtime helpers for RealSense preview events and local focus inference."""

import base64
from datetime import datetime, timezone
import io
import queue
import threading
import time
import uuid
from collections import deque


LANDMARK_INDEX = {
    "left_eye_outer": 33,
    "left_eye_inner": 173,
    "right_eye_outer": 263,
    "right_eye_inner": 398,
    "left_eye_top": 222,
    "left_eye_bottom": 230,
    "right_eye_top": 442,
    "right_eye_bottom": 450,
    "left_pupil": 468,
    "right_pupil": 473,
    "nose": 4,
    "chin": 152,
    "forehead": 10,
}

ATTENTION_THRESHOLDS = {
    "face_vertical_down": 0.66,
    "face_vertical_up": 0.46,
    "face_horizontal_right": 0.77,
    "face_horizontal_left": 0.34,
    "eye_vertical_down": 0.49,
    "eye_vertical_up": 0.34,
    "eye_horizontal_right": 0.66,
    "eye_horizontal_left": 0.35,
}


def utc_timestamp():
    return datetime.now(timezone.utc).isoformat(timespec="milliseconds").replace("+00:00", "Z")


def _ratio(first, middle, last):
    distance = abs(first - last)
    return abs(first - middle) / distance if distance else 0.0


def _classify(value, low, high, low_label, high_label):
    if value > high:
        return high_label
    if value < low:
        return low_label
    return "center"


def classify_attention_landmarks(landmarks):
    """Classify MediaPipe's 478 face landmarks using the existing monitor heuristic."""
    idx = LANDMARK_INDEX
    thresholds = ATTENTION_THRESHOLDS
    ratios = {
        "faceHorizontal": _ratio(
            landmarks[idx["left_eye_outer"]].x,
            landmarks[idx["nose"]].x,
            landmarks[idx["right_eye_outer"]].x,
        ),
        "faceVertical": _ratio(
            landmarks[idx["forehead"]].y,
            landmarks[idx["nose"]].y,
            landmarks[idx["chin"]].y,
        ),
        "leftEyeHorizontal": _ratio(
            landmarks[idx["left_eye_outer"]].x,
            landmarks[idx["left_pupil"]].x,
            landmarks[idx["left_eye_inner"]].x,
        ),
        "leftEyeVertical": _ratio(
            landmarks[idx["left_eye_top"]].y,
            landmarks[idx["left_pupil"]].y,
            landmarks[idx["left_eye_bottom"]].y,
        ),
        "rightEyeHorizontal": _ratio(
            landmarks[idx["right_eye_inner"]].x,
            landmarks[idx["right_pupil"]].x,
            landmarks[idx["right_eye_outer"]].x,
        ),
        "rightEyeVertical": _ratio(
            landmarks[idx["right_eye_top"]].y,
            landmarks[idx["right_pupil"]].y,
            landmarks[idx["right_eye_bottom"]].y,
        ),
    }

    face_horizontal = _classify(
        ratios["faceHorizontal"],
        thresholds["face_horizontal_left"],
        thresholds["face_horizontal_right"],
        "left",
        "right",
    )
    face_vertical = _classify(
        ratios["faceVertical"],
        thresholds["face_vertical_up"],
        thresholds["face_vertical_down"],
        "up",
        "down",
    )

    eye_vertical_values = [ratios["leftEyeVertical"], ratios["rightEyeVertical"]]
    if any(value > thresholds["eye_vertical_down"] for value in eye_vertical_values):
        eye_vertical = "down"
    elif any(value < thresholds["eye_vertical_up"] for value in eye_vertical_values):
        eye_vertical = "up"
    else:
        eye_vertical = "center"

    eye_horizontal_values = [ratios["leftEyeHorizontal"], ratios["rightEyeHorizontal"]]
    if any(value > thresholds["eye_horizontal_right"] for value in eye_horizontal_values):
        eye_horizontal = "right"
    elif any(value < thresholds["eye_horizontal_left"] for value in eye_horizontal_values):
        eye_horizontal = "left"
    else:
        eye_horizontal = "center"

    directions = {
        "faceHorizontal": face_horizontal,
        "faceVertical": face_vertical,
        "eyeHorizontal": eye_horizontal,
        "eyeVertical": eye_vertical,
    }
    focused = (
        all(value == "center" for value in directions.values())
        or (face_vertical == "up" and eye_vertical == "down")
        or (face_vertical == "down" and eye_vertical == "up")
        or (face_horizontal == "left" and eye_horizontal == "right")
        or (face_horizontal == "right" and eye_horizontal == "left")
    )
    return ("focused" if focused else "distracted"), directions, ratios


def encode_rgb_jpeg(rgb_bytes, width, height, quality=72):
    try:
        from PIL import Image
    except Exception as error:
        raise RuntimeError("Pillow is required for RealSense live preview encoding.") from error

    image = Image.frombytes("RGB", (width, height), rgb_bytes)
    output = io.BytesIO()
    image.save(output, format="JPEG", quality=max(40, min(int(quality), 90)), optimize=False)
    return base64.b64encode(output.getvalue()).decode("ascii")


class EventBroker:
    """Small bounded fan-out queue used by the local SSE endpoint."""

    def __init__(self, queue_size=12):
        self._queue_size = queue_size
        self._subscribers = set()
        self._lock = threading.Lock()

    def subscribe(self):
        subscriber = queue.Queue(maxsize=self._queue_size)
        with self._lock:
            self._subscribers.add(subscriber)
        return subscriber

    def unsubscribe(self, subscriber):
        with self._lock:
            self._subscribers.discard(subscriber)

    def publish(self, event, payload):
        message = {"functionId": "monitor", "event": event, "payload": payload}
        with self._lock:
            subscribers = list(self._subscribers)
        for subscriber in subscribers:
            try:
                subscriber.put_nowait(message)
            except queue.Full:
                try:
                    subscriber.get_nowait()
                except queue.Empty:
                    pass
                try:
                    subscriber.put_nowait(message)
                except queue.Full:
                    pass


class FocusEstimator:
    """Optional MediaPipe face/iris estimator with a five-observation debounce."""

    def __init__(self, history_size=5, processor=None, numpy_module=None, model_version=""):
        self.history = deque(maxlen=max(1, int(history_size)))
        self.current = None
        self.error = ""
        self.available = False
        self._processor = processor
        self._numpy = numpy_module
        self.model_version = model_version

        if self._processor is not None and self._numpy is not None:
            self.available = True
            self.model_version = self.model_version or "injected"
            return

        try:
            import mediapipe as mediapipe
            import numpy as numpy

            self._numpy = numpy
            self._processor = mediapipe.solutions.face_mesh.FaceMesh(
                static_image_mode=False,
                max_num_faces=1,
                refine_landmarks=True,
                min_detection_confidence=0.5,
                min_tracking_confidence=0.5,
            )
            self.model_version = f"mediapipe-{mediapipe.__version__}"
            self.available = True
        except Exception as error:
            self.error = f"MediaPipe focus inference is unavailable: {error}"

    def _record(self, state, timestamp_ms, reason, directions=None):
        return {
            "id": f"attention-{timestamp_ms}-{uuid.uuid4().hex[:8]}",
            "timestamp": timestamp_ms,
            "state": state,
            "confidence": None,
            "source": "realsense-face-mesh",
            "reason": reason,
            "modelVersion": self.model_version or "unavailable",
            "directions": directions,
        }

    def observe(self, candidate, timestamp_ms, reason, directions=None):
        self.history.append(candidate)
        stable = len(self.history) == self.history.maxlen and all(
            item == candidate for item in self.history
        )
        if not stable:
            if self.current is not None:
                return self.current, False
            return self._record(
                "unknown",
                timestamp_ms,
                f"Calibrating focus state ({len(self.history)}/{self.history.maxlen}).",
            ), False

        changed = self.current is None or self.current["state"] != candidate
        if changed:
            self.current = self._record(candidate, timestamp_ms, reason, directions)
        return self.current, changed

    def analyze(self, rgb_bytes, width, height, timestamp_ms):
        if not self.available:
            record = self._record("unknown", timestamp_ms, self.error or "Focus inference is unavailable.")
            changed = self.current is None or self.current["reason"] != record["reason"]
            if changed:
                self.current = record
            return self.current, False, changed

        try:
            image = self._numpy.frombuffer(rgb_bytes, dtype=self._numpy.uint8).reshape(
                (height, width, 3)
            )
            result = self._processor.process(image)
            faces = getattr(result, "multi_face_landmarks", None)
            if not faces:
                record, changed = self.observe(
                    "away", timestamp_ms, "No face was detected in the RealSense color frame."
                )
                return record, False, changed

            state, directions, _ratios = classify_attention_landmarks(faces[0].landmark)
            reason = (
                "Face and iris direction remained within the configured focus bounds."
                if state == "focused"
                else "Face or iris direction remained outside the configured focus bounds: "
                + ", ".join(f"{key}={value}" for key, value in directions.items())
                + "."
            )
            record, changed = self.observe(state, timestamp_ms, reason, directions)
            return record, True, changed
        except Exception as error:
            self.error = f"Focus inference failed: {error}"
            record = self._record("unknown", timestamp_ms, self.error)
            changed = self.current is None or self.current["reason"] != record["reason"]
            if changed:
                self.current = record
            return self.current, False, changed

    def close(self):
        close = getattr(self._processor, "close", None)
        if callable(close):
            close()
        self._processor = None
        self.available = False
