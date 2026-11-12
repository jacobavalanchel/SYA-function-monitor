"""
RealSense / Live UVC Camera Capture Engine.
Continuously reads frames from connected camera into a rolling thread-safe buffer.
Provides:
1. Sliding window frame extraction for M1 engagement inference.
2. Real-time MJPEG preview for Web UI debugging.
"""

import time
import sys
import threading
from collections import deque
from typing import List, Tuple, Optional
import cv2
import numpy as np


class LiveCameraCapture:
    """
    Background threaded capture engine for live cameras (e.g. Intel RealSense L515).
    """
    def __init__(
        self,
        device_index: int = 0,
        target_fps: float = 25.0,
        buffer_duration_sec: float = 20.0
    ):
        self.device_index = device_index
        self.target_fps = target_fps
        self.max_buffer_len = int(buffer_duration_sec * target_fps)
        self.buffer = deque(maxlen=self.max_buffer_len)
        self.lock = threading.Lock()

        self.cap: Optional[cv2.VideoCapture] = None
        self.is_running = False
        self.thread: Optional[threading.Thread] = None

        self.latest_frame_rgb: Optional[np.ndarray] = None
        self.latest_jpeg: Optional[bytes] = None
        self.last_frame_at = 0.0
        self.stream_error: Optional[str] = None
        self.camera_name = f"Camera Device {device_index}"
        self.actual_fps = 0.0
        self.frame_count = 0

    def start(self):
        """Open camera and start background capture thread."""
        if self.is_running:
            return

        self.cap = cv2.VideoCapture(self.device_index, cv2.CAP_AVFOUNDATION) if sys.platform == "darwin" else cv2.VideoCapture(self.device_index)
        if not self.cap.isOpened():
            raise RuntimeError(f"Unable to open camera at index {self.device_index}. Please check connection and permissions.")

        # Try configuring 1080p or 720p
        self.cap.set(cv2.CAP_PROP_FRAME_WIDTH, 1280)
        self.cap.set(cv2.CAP_PROP_FRAME_HEIGHT, 720)
        self.cap.set(cv2.CAP_PROP_FPS, self.target_fps)

        # Detect model if possible
        backend = self.cap.getBackendName()
        w = int(self.cap.get(cv2.CAP_PROP_FRAME_WIDTH))
        h = int(self.cap.get(cv2.CAP_PROP_FRAME_HEIGHT))
        self.camera_name = f"RealSense / UVC Camera ({w}x{h} @ {backend})"

        self.is_running = True
        self.thread = threading.Thread(target=self._capture_loop, daemon=True)
        self.thread.start()
        print(f"[Camera] Started live capture on {self.camera_name} (buffer: {self.max_buffer_len} frames)")

    def _capture_loop(self):
        """Internal capture loop running in background thread."""
        frame_interval = 1.0 / self.target_fps
        fps_counter = 0
        fps_timer = time.time()
        last_delivery = time.monotonic()

        while self.is_running and self.cap and self.cap.isOpened():
            t0 = time.time()
            ret, frame = self.cap.read()
            if not ret or frame is None:
                if time.monotonic() - last_delivery > 4.0:
                    self.stream_error = "camera_stream_lost" if self.frame_count else "camera_no_frames"
                    break
                time.sleep(0.01)
                continue

            now = time.time()
            frame_rgb = cv2.cvtColor(frame, cv2.COLOR_BGR2RGB)

            # Generate JPEG preview for UI
            encoded, jpeg_buf = cv2.imencode(".jpg", frame, [cv2.IMWRITE_JPEG_QUALITY, 80])
            if not encoded:
                continue
            jpeg_bytes = jpeg_buf.tobytes()
            last_delivery = time.monotonic()

            with self.lock:
                self.buffer.append((frame_rgb, now))
                self.latest_frame_rgb = frame_rgb
                self.latest_jpeg = jpeg_bytes
                self.last_frame_at = now
                self.frame_count += 1

            fps_counter += 1
            if now - fps_timer >= 1.0:
                self.actual_fps = fps_counter / (now - fps_timer)
                fps_counter = 0
                fps_timer = now

            # Sleep to match target FPS
            elapsed = time.time() - t0
            sleep_time = max(0.0, frame_interval - elapsed)
            if sleep_time > 0:
                time.sleep(sleep_time)

        if self.is_running:
            self.stream_error = self.stream_error or "camera_stream_lost"
            cap = self.cap
            self.cap = None
            if cap:
                cap.release()
            self.is_running = False

    def get_recent_frames(self, duration_sec: float) -> Tuple[List[np.ndarray], List[float]]:
        """
        Extract frames and timestamps occurring within the past duration_sec seconds.
        Returns:
            (frames_rgb, timestamps)
        """
        now = time.time()
        cutoff = now - duration_sec

        with self.lock:
            items = list(self.buffer)

        frames = []
        timestamps = []
        for f, ts in items:
            if ts >= cutoff:
                frames.append(f)
                timestamps.append(ts)

        return frames, timestamps

    def get_latest_jpeg(self) -> Optional[bytes]:
        """Return the latest frame encoded as JPEG for streaming."""
        with self.lock:
            return self.latest_jpeg if self.is_running and time.time() - self.last_frame_at <= 2.0 else None

    def stop(self):
        """Stop background capture and release camera device."""
        self.is_running = False
        if self.thread:
            self.thread.join(timeout=1.0)
            self.thread = None

        if self.cap:
            self.cap.release()
            self.cap = None
        with self.lock:
            self.buffer.clear()
            self.latest_frame_rgb = None
            self.latest_jpeg = None
            self.last_frame_at = 0.0

        print("[Camera] Live capture stopped and device released.")
