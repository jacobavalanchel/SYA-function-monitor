"""FOC-011: bounded, in-memory eye geometry and sampling-quality observations.

EAR is uncalibrated geometry, not an eye-closure or energy probability. Host
acquisition timestamps are not sensor exposure times. No native imports or I/O.
"""
from __future__ import annotations

from collections import Counter, deque
from dataclasses import dataclass
import math
from numbers import Integral, Real
from statistics import mean
from typing import Optional

SCHEMA_VERSION = 1
EXTRACTOR_VERSION = "mediapipe-face-mesh-0.10.21-ear-v1"
RIGHT_EYE_INDICES = (33, 160, 158, 133, 153, 144)
LEFT_EYE_INDICES = (362, 385, 387, 263, 373, 380)
GEOMETRY_REASONS = frozenset({
    "invalid_dimensions", "no_face", "missing_landmarks", "non_finite_landmark",
    "out_of_bounds_landmark", "degenerate_eye", "inference_failed",
})


def _finite_number(value):
    if isinstance(value, bool) or not isinstance(value, Real):
        return False
    try:
        return math.isfinite(value)
    except (OverflowError, ValueError):
        return False


def _dimension(value):
    return isinstance(value, Integral) and not isinstance(value, bool) and value > 0 and _finite_number(value)


@dataclass(frozen=True)
class EyeGeometry:
    width: Optional[int]
    height: Optional[int]
    left_ear: Optional[float]
    right_ear: Optional[float]
    left_width_px: Optional[float]
    right_width_px: Optional[float]
    reason: Optional[str] = None
    schema_version: int = SCHEMA_VERSION
    extractor_version: str = EXTRACTOR_VERSION


@dataclass(frozen=True)
class EyeObservation:
    session_id: Optional[str]
    color_frame_number: Optional[int]
    captured_at_ms: int
    acquired_monotonic_s: float
    completed_monotonic_s: float
    geometry: EyeGeometry


def observation_has_coherent_timestamps(observation):
    """Only a complete, ordered clock pair may move retention/report time."""
    acquired = observation.acquired_monotonic_s
    completed = observation.completed_monotonic_s
    captured = observation.captured_at_ms
    return (_finite_number(acquired) and acquired >= 0
            and _finite_number(completed) and completed >= acquired
            and isinstance(captured, Integral) and not isinstance(captured, bool) and captured >= 0)


@dataclass(frozen=True)
class EyeWindowConfig:
    duration_seconds: float
    max_samples: int
    max_gap_seconds: float

    def __post_init__(self):
        if not _finite_number(self.duration_seconds) or self.duration_seconds <= 0:
            raise ValueError("invalid_duration_seconds")
        if isinstance(self.max_samples, bool) or not isinstance(self.max_samples, int) or self.max_samples < 1:
            raise ValueError("invalid_max_samples")
        if not _finite_number(self.max_samples) or not _finite_number(self.max_samples / self.duration_seconds):
            raise ValueError("non_finite_sample_rate")
        if not _finite_number(self.max_gap_seconds) or self.max_gap_seconds <= 0:
            raise ValueError("invalid_max_gap_seconds")


def invalid_eye_geometry(reason, width, height):
    """Produce a fixed reason and no measurements; never retain invalid numbers."""
    if reason not in GEOMETRY_REASONS:
        raise ValueError("invalid_geometry_reason")
    return EyeGeometry(
        width=int(width) if _dimension(width) else None,
        height=int(height) if _dimension(height) else None,
        left_ear=None, right_ear=None, left_width_px=None, right_width_px=None,
        reason=reason,
    )


def measure_eye_geometry(landmarks, width, height):
    """Calculate each eye's EAR after restoring image-pixel aspect ratio.

    The six contour points are ordered corner, upper, upper, corner, lower,
    lower. Only their x/y coordinates are used; the output retains no landmarks.
    """
    if not _dimension(width) or not _dimension(height):
        return invalid_eye_geometry("invalid_dimensions", width, height)
    if landmarks is None:
        return invalid_eye_geometry("no_face", width, height)
    pixels = {}
    for index in RIGHT_EYE_INDICES + LEFT_EYE_INDICES:
        try:
            point = landmarks[index]
            x, y = (point["x"], point["y"]) if isinstance(point, dict) else (point.x, point.y)
        except (IndexError, KeyError, TypeError, AttributeError):
            return invalid_eye_geometry("missing_landmarks", width, height)
        if not _finite_number(x) or not _finite_number(y):
            return invalid_eye_geometry("non_finite_landmark", width, height)
        if not 0 <= x <= 1 or not 0 <= y <= 1:
            return invalid_eye_geometry("out_of_bounds_landmark", width, height)
        pixels[index] = (float(x) * width, float(y) * height)
    measurements = []
    for indices in (LEFT_EYE_INDICES, RIGHT_EYE_INDICES):
        p = [pixels[index] for index in indices]
        eye_width = math.hypot(p[0][0] - p[3][0], p[0][1] - p[3][1])
        if eye_width <= 0 or not math.isfinite(eye_width):
            return invalid_eye_geometry("degenerate_eye", width, height)
        # Divide before adding to avoid overflow for unusually large dimensions.
        ear = (math.hypot(p[1][0] - p[5][0], p[1][1] - p[5][1]) / eye_width / 2
               + math.hypot(p[2][0] - p[4][0], p[2][1] - p[4][1]) / eye_width / 2)
        if not math.isfinite(ear):
            return invalid_eye_geometry("degenerate_eye", width, height)
        measurements.append((ear, eye_width))
    return EyeGeometry(int(width), int(height), measurements[0][0], measurements[1][0],
                       measurements[0][1], measurements[1][1])


def _geometry_reason(geometry):
    if not isinstance(geometry, EyeGeometry):
        return "invalid_geometry"
    if type(geometry.schema_version) is not int or geometry.schema_version != SCHEMA_VERSION or geometry.extractor_version != EXTRACTOR_VERSION:
        return "invalid_geometry"
    if geometry.reason is not None:
        return geometry.reason if geometry.reason in GEOMETRY_REASONS else "invalid_geometry"
    if not _dimension(geometry.width) or not _dimension(geometry.height):
        return "invalid_geometry"
    for value in (geometry.left_ear, geometry.right_ear):
        if not _finite_number(value) or value < 0:
            return "invalid_geometry"
    for value in (geometry.left_width_px, geometry.right_width_px):
        if not _finite_number(value) or value <= 0:
            return "invalid_geometry"
    return None


def _distribution(values):
    values = sorted(values)
    count = len(values)
    if not count:
        return {"count": 0, "min": None, "max": None, "mean": None, "median": None}
    middle = count // 2
    median = values[middle]
    if count % 2 == 0:
        median = values[middle - 1] + (values[middle] - values[middle - 1]) / 2
    return {"count": count, "min": values[0], "max": values[-1], "mean": mean(values), "median": median}


@dataclass(frozen=True)
class _Entry:
    observation: EyeObservation
    effective_time: float
    reason: Optional[str]
    continuous: bool


class EyeObservationWindow:
    """Single-session scalar window. The owner must serialize all method calls.

    Counts/distributions describe retained observations acquired in [T-D, T].
    Rejected unplaceable observations are assigned the acquisition high-water
    mark, not their untrusted timestamp. A single pre-window anchor may support
    clipped coverage, but is excluded from counts and distributions and is also
    subject to max_samples. No coverage is extrapolated after the final frame.
    """

    def __init__(self, config):
        if not isinstance(config, EyeWindowConfig):
            raise TypeError("invalid_window_config")
        self.config = config
        self.reset()

    def reset(self, session_id=None):
        if session_id is not None and (not isinstance(session_id, str) or not session_id):
            raise ValueError("invalid_session_id")
        preserve_order = session_id is not None and session_id == getattr(self, "session_id", None)
        frame_highwater = self._frame_highwater if preserve_order else None
        time_highwater = self._time_highwater if preserve_order else None
        snapshot_highwater = self._snapshot_highwater if preserve_order else None
        self.session_id = session_id
        self._entries = deque()
        self._frame_highwater = frame_highwater
        self._time_highwater = time_highwater
        self._snapshot_highwater = snapshot_highwater
        self._previous_valid = False
        self._total_observations = 0
        self._capacity_evicted = 0
        self._discarded_through = None
        self._lost_interval_end = None

    def _prune(self, as_of):
        start = as_of - self.config.duration_seconds
        # Keep the final entry before the left boundary as a possible anchor.
        while len(self._entries) > 1 and self._entries[1].effective_time < start:
            self._entries.popleft()

    def observe(self, observation):
        if not isinstance(observation, EyeObservation):
            raise TypeError("invalid_observation")
        if (self.session_id is not None and observation.session_id == self.session_id
                and _geometry_reason(observation.geometry) == "inference_failed"):
            # Live and offline traces share this boundary. Forget prior samples
            # but preserve same-session replay and snapshot watermarks.
            self.reset(self.session_id)
        self._total_observations += 1
        reason = None
        if not isinstance(observation.session_id, str) or not observation.session_id:
            reason = "missing_session"
        elif self.session_id is None or observation.session_id != self.session_id:
            reason = "wrong_session"
        frame = observation.color_frame_number
        frame_ok = isinstance(frame, Integral) and not isinstance(frame, bool) and frame >= 0
        acquired = observation.acquired_monotonic_s
        time_ok = observation_has_coherent_timestamps(observation)
        old_time = self._time_highwater
        if reason is None:
            if not frame_ok:
                reason = "missing_frame_identity" if frame is None else "invalid_frame_identity"
            elif not time_ok:
                reason = "invalid_timestamp"
            elif self._frame_highwater is not None and frame <= self._frame_highwater:
                reason = "duplicate_frame" if frame == self._frame_highwater else "out_of_order_frame"
            elif old_time is not None and acquired <= old_time:
                reason = "nonincreasing_timestamp"
            elif old_time is not None and acquired - old_time > self.config.max_gap_seconds:
                reason = "excessive_gap"
            else:
                reason = _geometry_reason(observation.geometry)
        # Even rejected current-session fields cannot later be replayed as new.
        if observation.session_id == self.session_id and self.session_id is not None:
            if frame_ok:
                self._frame_highwater = frame if self._frame_highwater is None else max(frame, self._frame_highwater)
            if time_ok:
                self._time_highwater = acquired if old_time is None else max(acquired, old_time)
        effective_time = self._time_highwater if self._time_highwater is not None else 0.0
        continuous = reason is None and self._previous_valid
        self._entries.append(_Entry(observation, effective_time, reason, continuous))
        self._previous_valid = reason is None
        self._prune(effective_time)
        while len(self._entries) > self.config.max_samples:
            removed = self._entries.popleft()
            self._capacity_evicted += 1
            self._discarded_through = removed.effective_time
            self._lost_interval_end = self._entries[0].effective_time
        return reason is None

    def snapshot(self, as_of_monotonic_s):
        if not _finite_number(as_of_monotonic_s) or as_of_monotonic_s < 0:
            raise ValueError("invalid_as_of_monotonic_s")
        if self._time_highwater is not None and as_of_monotonic_s < self._time_highwater:
            raise ValueError("as_of_before_observation")
        if self._snapshot_highwater is not None and as_of_monotonic_s < self._snapshot_highwater:
            raise ValueError("as_of_before_snapshot")
        self._snapshot_highwater = as_of_monotonic_s
        self._prune(as_of_monotonic_s)
        start = as_of_monotonic_s - self.config.duration_seconds
        entries = list(self._entries)
        inside = [entry for entry in entries if start <= entry.effective_time <= as_of_monotonic_s]
        valid = [entry for entry in inside if entry.reason is None]
        reasons = Counter(entry.reason for entry in inside if entry.reason is not None)
        coverage = []
        intervals = []
        for previous, current in zip(entries, entries[1:]):
            if previous.reason is None and current.reason is None and current.continuous:
                begin = previous.observation.acquired_monotonic_s
                end = current.observation.acquired_monotonic_s
                clipped = min(end, as_of_monotonic_s) - max(begin, start)
                if clipped > 0:
                    coverage.append(clipped)
                    intervals.append(end - begin)
        coverage_seconds = min(self.config.duration_seconds, math.fsum(coverage))
        capacity_truncated = self._discarded_through is not None and (
            start <= self._discarded_through or start < self._lost_interval_end)
        return {
            "schema_version": SCHEMA_VERSION,
            "extractor_version": EXTRACTOR_VERSION,
            "session_id": self.session_id,
            "as_of_monotonic_s": as_of_monotonic_s,
            "window_start_monotonic_s": start,
            "duration_seconds": self.config.duration_seconds,
            "max_samples": self.config.max_samples,
            "max_gap_seconds": self.config.max_gap_seconds,
            "retained_samples": len(entries),
            "anchor_samples": len(entries) - len(inside),
            "total_observations_since_reset": self._total_observations,
            "samples_in_window": len(inside),
            "valid_samples": len(valid),
            "invalid_samples": len(inside) - len(valid),
            "reason_counts": dict(sorted(reasons.items())),
            "coverage_seconds": coverage_seconds,
            "coverage_ratio": coverage_seconds / self.config.duration_seconds,
            "valid_sample_rate_hz": len(valid) / self.config.duration_seconds,
            "capacity_truncated": bool(capacity_truncated),
            "capacity_evicted_since_reset": self._capacity_evicted,
            "interval_seconds": _distribution(intervals),
            "inference_latency_seconds": _distribution([
                entry.observation.completed_monotonic_s - entry.observation.acquired_monotonic_s for entry in valid]),
            "left_ear": _distribution([entry.observation.geometry.left_ear for entry in valid]),
            "right_ear": _distribution([entry.observation.geometry.right_ear for entry in valid]),
            "denominators": {
                "coverage_ratio": "configured_duration_seconds",
                "valid_sample_rate_hz": "configured_duration_seconds",
                "sample_counts": "retained_samples_in_closed_window_excluding_anchor",
                "ear_and_latency_distributions": "valid_samples_in_closed_window_excluding_anchor",
                "reason_counts": "invalid_samples_in_closed_window_excluding_anchor",
                "interval_seconds": "proven_adjacent_valid_intervals_intersecting_window",
                "rejected_sample_time": "current_session_acquisition_highwater",
            },
        }
