"""
Session Manifest loading, validation, and path resolution.
Supports relative paths resolved against the manifest file directory.
"""

import os
import json
from typing import Dict, Any
import cv2

from ..schemas import SessionManifest, SessionManifestScreenshot, SessionManifestWebcam


def load_session_manifest(manifest_path: str) -> SessionManifest:
    """
    Load, resolve, and validate a session manifest file.
    All relative paths inside the manifest are resolved relative to manifest_path directory.
    """
    if not os.path.isfile(manifest_path):
        raise FileNotFoundError(f"Manifest file not found: {manifest_path}")

    base_dir = os.path.dirname(os.path.abspath(manifest_path))

    with open(manifest_path, "r", encoding="utf-8") as f:
        data = json.load(f)

    # Validate unit
    time_unit = data.get("time_unit", "seconds")
    if time_unit != "seconds":
        raise ValueError(f"Unsupported time_unit: {time_unit}. Only 'seconds' is supported.")

    # Resolve webcam video path
    webcam_data = data.get("webcam", {})
    raw_video_path = webcam_data.get("video_path")
    if not raw_video_path:
        raise ValueError("Manifest missing required 'webcam.video_path'")

    if not os.path.isabs(raw_video_path):
        resolved_video_path = os.path.normpath(os.path.join(base_dir, raw_video_path))
    else:
        resolved_video_path = raw_video_path

    if not os.path.isfile(resolved_video_path):
        raise FileNotFoundError(f"Resolved webcam video file does not exist: {resolved_video_path}")

    # Inspect video duration and fps if not provided
    cap = cv2.VideoCapture(resolved_video_path)
    video_fps = float(cap.get(cv2.CAP_PROP_FPS))
    video_frames = int(cap.get(cv2.CAP_PROP_FRAME_COUNT))
    cap.release()

    computed_duration = video_frames / video_fps if video_fps > 0 else 0.0
    declared_duration = webcam_data.get("duration")
    duration = declared_duration if declared_duration is not None else computed_duration
    fps = webcam_data.get("fps") if webcam_data.get("fps") is not None else video_fps

    webcam_obj = SessionManifestWebcam(
        video_path=resolved_video_path,
        start_time_offset=float(webcam_data.get("start_time_offset", 0.0)),
        duration=duration,
        fps=fps
    )

    # Resolve screenshot paths
    raw_screenshots = data.get("screenshots", [])
    if not raw_screenshots:
        raise ValueError("Manifest must contain at least one screenshot entry")

    screenshots_objs = []
    prev_ts = -1.0
    for idx, s in enumerate(raw_screenshots):
        raw_img_path = s.get("image_path")
        if not raw_img_path:
            raise ValueError(f"Screenshot entry {idx} missing 'image_path'")

        if not os.path.isabs(raw_img_path):
            resolved_img_path = os.path.normpath(os.path.join(base_dir, raw_img_path))
        else:
            resolved_img_path = raw_img_path

        if not os.path.isfile(resolved_img_path):
            raise FileNotFoundError(f"Screenshot file does not exist: {resolved_img_path}")

        ts = float(s.get("timestamp", 0.0))
        if ts < prev_ts:
            raise ValueError(f"Screenshot timestamps must be monotonically non-decreasing. Found {ts} after {prev_ts}")
        prev_ts = ts

        screenshots_objs.append(
            SessionManifestScreenshot(
                image_path=resolved_img_path,
                timestamp=ts,
                app_name=s.get("app_name"),
                window_title=s.get("window_title")
            )
        )

    return SessionManifest(
        session_id=str(data["session_id"]),
        time_unit="seconds",
        work_scope=str(data["work_scope"]),
        current_goal=str(data["current_goal"]),
        webcam=webcam_obj,
        screenshots=screenshots_objs,
        past_context=str(data.get("past_context", ""))
    )
