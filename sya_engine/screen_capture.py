"""
Screen capture provider.
Attempts live screen grabbing via native macOS tools.
Gracefully falls back to configured/recent screenshots if macOS display permissions are restricted.
Provides JPEG preview for the web dashboard.
"""

import os
import time
import subprocess
from typing import Optional, Tuple
from PIL import Image


class ScreenCaptureProvider:
    """
    Screen capture manager for desktop activity observation.
    """
    def __init__(
        self,
        fallback_dir: Optional[str] = None,
        tmp_capture_path: str = "/tmp/sya_live_screen.png"
    ):
        self.tmp_capture_path = tmp_capture_path
        if fallback_dir is None:
            fallback_dir = os.path.abspath(
                os.path.join(os.path.dirname(__file__), "..", "data", "test_session", "screenshots")
            )
        self.fallback_dir = fallback_dir
        self.latest_path: Optional[str] = None
        self.latest_jpeg: Optional[bytes] = None
        self.can_live_grab: Optional[bool] = None

        # Pre-seed latest with existing fallback
        self._seed_latest()

    def _seed_latest(self):
        if os.path.isdir(self.fallback_dir):
            files = sorted(
                [os.path.join(self.fallback_dir, f) for f in os.listdir(self.fallback_dir)
                 if f.lower().endswith((".png", ".jpg", ".jpeg"))]
            )
            if files:
                self.latest_path = files[-1]
                self._update_jpeg(self.latest_path)

    def _update_jpeg(self, path: str):
        try:
            with open(path, "rb") as f:
                self.latest_jpeg = f.read()
        except Exception:
            pass

    def capture(self) -> Tuple[str, bool]:
        """
        Capture current desktop screen.
        Returns:
            (screenshot_path, is_live_grab)
        """
        # Try native screencapture
        try:
            res = subprocess.run(
                ["screencapture", "-x", "-m", self.tmp_capture_path],
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                timeout=2.0
            )
            if res.returncode == 0 and os.path.isfile(self.tmp_capture_path) and os.path.getsize(self.tmp_capture_path) > 1000:
                self.can_live_grab = True
                self.latest_path = self.tmp_capture_path
                self._update_jpeg(self.tmp_capture_path)
                return self.tmp_capture_path, True
        except Exception:
            pass

        # Fallback to recent screenshot
        self.can_live_grab = False
        if not self.latest_path or not os.path.isfile(self.latest_path):
            self._seed_latest()

        return self.latest_path or "", False

    def set_custom_screenshot(self, filepath: str):
        """Set a user-specified screenshot."""
        if os.path.isfile(filepath):
            self.latest_path = os.path.abspath(filepath)
            self._update_jpeg(self.latest_path)

    def get_latest_jpeg(self) -> Optional[bytes]:
        """Return latest screenshot bytes for UI display."""
        if not self.latest_jpeg and self.latest_path and os.path.isfile(self.latest_path):
            self._update_jpeg(self.latest_path)
        return self.latest_jpeg
