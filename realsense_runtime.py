"""Runtime helpers for RealSense preview events and local focus inference."""

import base64
import copy
import errno
from datetime import datetime, timezone
import io
import queue
import re
import threading
import time
import uuid
from collections import OrderedDict, deque

from eye_observation import invalid_eye_geometry, measure_eye_geometry


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


def attention_observation(sample, previous, session_id, expected_interval_ms, timestamp_ms, boundary=None):
    """Stamp a completed inference or lifecycle boundary, never a cached poll."""
    state = "unknown" if boundary else sample["state"]
    changed = previous is None or previous.get("sessionId") != session_id or (previous["state"], previous["source"]) != (state, sample["source"])
    record = {
        key: sample[key] for key in ("confidence", "source", "reason", "modelVersion", "directions")
        if key in sample
    }
    record.update({
        "id": f"attention-{uuid.uuid4()}",
        "timestamp": timestamp_ms,
        "observedAt": timestamp_ms,
        "state": state,
        "stateChangedAt": timestamp_ms if changed else previous["stateChangedAt"],
        "expectedIntervalMs": expected_interval_ms,
        "sessionId": session_id,
        "delivery": "live",
        "kind": "boundary" if boundary else "transition" if changed else "observation",
    })
    if boundary:
        record["boundary"] = boundary
    return record


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


ACCESS_COVERAGE = "session-and-manual-capture"
ACCESS_ERROR_MESSAGES = {
    "REALSENSE_PERMISSION_DENIED": "RealSense device access was denied.",
    "REALSENSE_START_FAILED": "RealSense session could not be started.",
    "REALSENSE_CAPTURE_FAILED": "RealSense manual sample could not be captured.",
    "REALSENSE_CANCELLED": "RealSense sample was discarded after the session stopped.",
}


def access_error_code(error, fallback):
    """Classify explicit OS/SDK permission failures without exposing their text."""
    seen = set()
    while error is not None and id(error) not in seen:
        seen.add(id(error))
        if isinstance(error, PermissionError) or getattr(error, "errno", None) in (errno.EACCES, errno.EPERM):
            return "REALSENSE_PERMISSION_DENIED"
        if re.search(r"permission denied|access (?:is )?denied|operation not permitted", str(error), re.IGNORECASE):
            return "REALSENSE_PERMISSION_DENIED"
        error = error.__cause__ or error.__context__
    return fallback


class RealSenseActionError(RuntimeError):
    def __init__(self, code, message):
        super().__init__(message)
        self.code = code
        self.public_message = ACCESS_ERROR_MESSAGES[code]


def realsense_access_event(session_id, operation, error_code, active):
    """Session boundary metadata only; never raw/preview frame accounting."""
    allowed = {
        ("session-stop", "REALSENSE_STOPPED", False),
        ("session-stop", "REALSENSE_STOP_FAILED", True),
        ("stream-error", "REALSENSE_PERMISSION_DENIED", False),
        ("stream-error", "REALSENSE_STREAM_FAILED", False),
    }
    if (operation, error_code, active) not in allowed:
        raise ValueError("Invalid RealSense access boundary.")
    return {
        "id": f"realsense-access-{uuid.uuid4()}", "timestamp": int(time.time() * 1000),
        "source": "realsense", "sessionId": session_id, "operation": operation,
        "outcome": "success" if error_code == "REALSENSE_STOPPED" else "denied" if error_code == "REALSENSE_PERMISSION_DENIED" else "error",
        "errorCode": error_code, "active": bool(active), "coverage": "session", "delivery": "live",
    }


def critical_event_id(message):
    """Only durable observation/access metadata advances SSE, never images."""
    value = message.get("payload", {}).get("id") if message.get("event") in ("attention.status", "realsense.access") else None
    return value if isinstance(value, str) and value and not any(char in value for char in "\0\r\n") else ""


attention_event_id = critical_event_id  # Backwards-compatible Python helper import.


class _EventSubscription:
    def __init__(self, queue_size, replay):
        self._live = queue.Queue(maxsize=queue_size)
        self._replay = deque(replay)
        self._lock = threading.Lock()
        self._overflowed = False

    def get(self, block=True, timeout=None):
        with self._lock:
            if self._overflowed:
                raise ConnectionAbortedError("Critical event queue overflow; reconnect with Last-Event-ID.")
            if self._replay:
                return self._replay.popleft()
        message = self._live.get(block=block, timeout=timeout)
        with self._lock:
            if self._overflowed:
                raise ConnectionAbortedError("Critical event queue overflow; reconnect with Last-Event-ID.")
            return message

    def get_nowait(self):
        return self.get(block=False)

    def offer(self, message):
        with self._lock:
            if self._overflowed:
                return
            try:
                self._live.put_nowait(message)
            except queue.Full:
                try:
                    removed = self._live.get_nowait()
                except queue.Empty:
                    removed = None
                if removed and critical_event_id(removed):
                    # Never deliver a later cursor after losing an observation.
                    # Close this bounded stream; the broker retains the missing
                    # events for replay after the client's last durable cursor.
                    self._overflowed = True
                    while True:
                        try:
                            self._live.get_nowait()
                        except queue.Empty:
                            break
                    self._live.put_nowait(None)  # Wake a blocked reader.
                    return
                self._live.put_nowait(message)


class EventBroker:
    """Bounded fan-out plus the latest 2,048 observation/access events for SSE resume.

    A missing, unknown, or evicted cursor starts live only. The buffer does not
    survive process restarts and never claims to recover gaps outside retention.
    """

    def __init__(self, queue_size=12, replay_size=2048):
        self._queue_size = max(1, int(queue_size))
        self._replay_size = max(0, int(replay_size))
        self._retained = OrderedDict()
        self._subscribers = set()
        self._lock = threading.Lock()

    def subscribe(self, last_event_id=None):
        with self._lock:
            replay = []
            if last_event_id and last_event_id in self._retained:
                after_cursor = False
                for event_id, message in self._retained.items():
                    if after_cursor:
                        recovered = copy.deepcopy(message)
                        recovered["payload"]["delivery"] = "replay"
                        replay.append(recovered)
                    if event_id == last_event_id:
                        after_cursor = True
            subscriber = _EventSubscription(self._queue_size, replay)
            self._subscribers.add(subscriber)
        return subscriber

    def unsubscribe(self, subscriber):
        with self._lock:
            self._subscribers.discard(subscriber)

    def publish(self, event, payload):
        message = {"functionId": "monitor", "event": event, "payload": payload}
        with self._lock:
            event_id = critical_event_id(message)
            if event_id and self._replay_size:
                if event_id in self._retained:
                    return
                self._retained[event_id] = copy.deepcopy(message)
                if len(self._retained) > self._replay_size:
                    self._retained.popitem(last=False)
            # Retention, snapshot, and live registration share a lock: an event
            # is either replayed or queued live, never omitted at the boundary.
            for subscriber in self._subscribers:
                subscriber.offer(message)


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
                # A new frame was analyzed; retain the debounced state but not
                # the previous sample's identity or observation timestamp.
                self.current = self._record(self.current["state"], timestamp_ms,
                                            self.current["reason"], self.current.get("directions"))
                return self.current, False
            return self._record(
                "unknown",
                timestamp_ms,
                f"Calibrating focus state ({len(self.history)}/{self.history.maxlen}).",
            ), False

        changed = self.current is None or self.current["state"] != candidate
        self.current = self._record(candidate, timestamp_ms, reason, directions)
        return self.current, changed

    def analyze(self, rgb_bytes, width, height, timestamp_ms):
        """Compatibility wrapper for callers consuming only attention."""
        return self.analyze_frame(rgb_bytes, width, height, timestamp_ms)[:3]

    def analyze_frame(self, rgb_bytes, width, height, timestamp_ms):
        """Run FaceMesh once and extract independent in-memory eye scalars."""
        geometry = invalid_eye_geometry("inference_failed", width, height)
        if not self.available:
            record = self._record("unknown", timestamp_ms, self.error or "Focus inference is unavailable.")
            changed = self.current is None or self.current["reason"] != record["reason"]
            self.history.clear()
            record["boundary"] = "disconnected"
            self.current = record
            return self.current, False, changed, geometry

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
                return record, False, changed, invalid_eye_geometry("no_face", width, height)

            # Geometry validation is independent of attention classification:
            # unusable eye contours must not turn successful attention into a
            # failure or trigger a second FaceMesh pass.
            geometry = measure_eye_geometry(faces[0].landmark, width, height)
            state, directions, _ratios = classify_attention_landmarks(faces[0].landmark)
            reason = (
                "Face and iris direction remained within the configured focus bounds."
                if state == "focused"
                else "Face or iris direction remained outside the configured focus bounds: "
                + ", ".join(f"{key}={value}" for key, value in directions.items())
                + "."
            )
            record, changed = self.observe(state, timestamp_ms, reason, directions)
            return record, True, changed, geometry
        except Exception as error:
            self.error = f"Focus inference failed: {error}"
            record = self._record("unknown", timestamp_ms, self.error)
            changed = self.current is None or self.current["reason"] != record["reason"]
            self.history.clear()
            record["boundary"] = "disconnected"
            self.current = record
            return self.current, False, changed, invalid_eye_geometry("inference_failed", width, height)

    def close(self):
        close = getattr(self._processor, "close", None)
        if callable(close):
            close()
        self._processor = None
        self.available = False
