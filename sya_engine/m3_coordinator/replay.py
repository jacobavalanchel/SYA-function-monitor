"""
M3: Time Coordination, Causal Windowing, and Offline Replay Engine.
Strictly causal: only inputs occurring <= window_end are processed.
All records written to JSONL with unvalidated proxies (P=null, state=null, work_focus=null).
"""

import os
import time
import json
from typing import Optional, List, Dict, Any, Tuple, Generator
import psutil
import cv2
import numpy as np

from ..schemas import (
    SessionManifest,
    ReplayWindowRecord,
    M1EngagementResult,
    M2ScreenResult
)
from ..m1_webcam import EngagementDetector
from ..m2_screen import ScreenAnalyzer


def extract_video_frames_in_range(
    video_path: str,
    start_sec: float,
    end_sec: float,
    video_offset_sec: float = 0.0
) -> Tuple[List[np.ndarray], List[float]]:
    """
    Extract RGB frames and relative session timestamps from a video file
    within the time interval [start_sec, end_sec].
    """
    cap = cv2.VideoCapture(video_path)
    fps = float(cap.get(cv2.CAP_PROP_FPS))
    if fps <= 0:
        fps = 25.0

    frames_rgb = []
    timestamps = []

    # Map session start_sec to video internal time
    video_t_start = max(0.0, start_sec - video_offset_sec)
    video_t_end = max(0.0, end_sec - video_offset_sec)

    start_frame = int(video_t_start * fps)
    end_frame = int(video_t_end * fps)

    cap.set(cv2.CAP_PROP_POS_FRAMES, start_frame)
    current_frame = start_frame

    while cap.isOpened() and current_frame <= end_frame:
        ret, frame = cap.read()
        if not ret:
            break

        session_ts = (current_frame / fps) + video_offset_sec
        if session_ts >= start_sec and session_ts <= end_sec:
            image_rgb = cv2.cvtColor(frame, cv2.COLOR_BGR2RGB)
            frames_rgb.append(image_rgb)
            timestamps.append(session_ts)

        current_frame += 1

    cap.release()
    return frames_rgb, timestamps


class ReplayCoordinator:
    """
    Orchestrates M1 and M2 in synchronized offline replay mode.
    """
    def __init__(
        self,
        m1_detector: EngagementDetector,
        m2_analyzer: ScreenAnalyzer,
        window_size_sec: float = 10.0,
        step_size_sec: float = 5.0
    ):
        self.m1 = m1_detector
        self.m2 = m2_analyzer
        self.window_size_sec = window_size_sec
        self.step_size_sec = step_size_sec
        self.process = psutil.Process()

    def run_replay(
        self,
        manifest: SessionManifest,
        output_jsonl_path: str
    ) -> Dict[str, Any]:
        """
        Execute dual-input causal replay on the session manifest.
        Writes records to output_jsonl_path.
        """
        t_start_replay = time.time()
        os.makedirs(os.path.dirname(os.path.abspath(output_jsonl_path)), exist_ok=True)

        # Determine replay duration from webcam and screenshots
        webcam_duration = manifest.webcam.duration or 0.0
        max_screenshot_time = max([s.timestamp for s in manifest.screenshots]) if manifest.screenshots else 0.0
        total_duration = max(webcam_duration, max_screenshot_time)

        if total_duration < self.window_size_sec:
            # If duration is shorter than window_size_sec, evaluate at least one window
            window_ends = [total_duration]
        else:
            window_ends = []
            curr_end = self.window_size_sec
            while curr_end <= total_duration:
                window_ends.append(curr_end)
                curr_end += self.step_size_sec
            if not window_ends:
                window_ends = [total_duration]

        records = []
        m1_latencies = []
        m2_latencies = []
        total_latencies = []
        peak_rss_mb = self.process.memory_info().rss / (1024 * 1024)

        with open(output_jsonl_path, "w", encoding="utf-8") as out_f:
            for window_idx, w_end in enumerate(window_ends):
                w_start = max(0.0, w_end - self.window_size_sec)
                t0_window = time.time()

                # --- 1. M1: Webcam window inference ---
                m1_t0 = time.time()
                frames_rgb, frame_ts = extract_video_frames_in_range(
                    video_path=manifest.webcam.video_path,
                    start_sec=w_start,
                    end_sec=w_end,
                    video_offset_sec=manifest.webcam.start_time_offset
                )
                m1_res: M1EngagementResult = self.m1.infer_window(
                    frames_rgb=frames_rgb,
                    frame_timestamps=frame_ts,
                    window_start=w_start,
                    window_end=w_end
                )
                m1_lat = (time.time() - m1_t0) * 1000
                m1_latencies.append(m1_lat)

                # --- 2. M2: Screen screenshot inference ---
                # Strictly causal: select screenshot(s) available at or before w_end
                causal_screenshots = [s for s in manifest.screenshots if s.timestamp <= w_end]

                m2_t0 = time.time()
                if causal_screenshots:
                    # Choose the most recent screenshot before or at w_end
                    latest_shot = causal_screenshots[-1]
                    m2_res: M2ScreenResult = self.m2.analyze_screenshot(
                        image_path=latest_shot.image_path,
                        work_scope=manifest.work_scope,
                        current_goal=manifest.current_goal,
                        app_name=latest_shot.app_name,
                        past_context=manifest.past_context,
                        window_start=w_start,
                        window_end=w_end,
                        evidence_id=f"screenshot_t{latest_shot.timestamp:.1f}"
                    )
                else:
                    m2_res = M2ScreenResult(
                        window_start=w_start,
                        window_end=w_end,
                        work_related=None,
                        goal_aligned=None,
                        brief_evidence="No screenshot available prior to window end",
                        active_app=None,
                        error_reason="no_screen_input_available",
                        model_version=self.m2.model_version,
                        evidence_ids=[],
                        latency_ms=0.0
                    )
                m2_lat = (time.time() - m2_t0) * 1000
                m2_latencies.append(m2_lat)

                # Total window latency
                w_latency_ms = (time.time() - t0_window) * 1000
                total_latencies.append(w_latency_ms)

                # Track memory RSS
                current_rss = self.process.memory_info().rss / (1024 * 1024)
                peak_rss_mb = max(peak_rss_mb, current_rss)

                # Determine overall window status
                if m1_res.prediction is not None and m2_res.work_related is not None:
                    win_status = "ok"
                elif m1_res.prediction is not None or m2_res.work_related is not None:
                    win_status = "partial"
                else:
                    win_status = "rejected"

                # Compose ReplayWindowRecord
                record = ReplayWindowRecord(
                    session_id=manifest.session_id,
                    window_id=window_idx,
                    window={
                        "start": w_start,
                        "end": w_end,
                        "emitted_at": time.time(),
                        "latency_ms": round(w_latency_ms, 2)
                    },
                    screen={
                        "W_hat": m2_res.work_related,
                        "goal_aligned": m2_res.goal_aligned,
                        "brief_evidence": m2_res.brief_evidence,
                        "active_app": m2_res.active_app,
                        "evidence_ids": m2_res.evidence_ids,
                        "error_reason": m2_res.error_reason,
                        "latency_ms": round(m2_res.latency_ms, 2)
                    },
                    person={
                        "engagement_pred": m1_res.prediction,
                        "engagement_scores": m1_res.scores,
                        "P_hat": None,  # Strictly unvalidated
                        "status": "unvalidated_model_estimate",
                        "valid_face_frames": m1_res.valid_face_frames,
                        "total_sampled_frames": m1_res.total_sampled_frames,
                        "quality_status": m1_res.quality_status,
                        "rejection_reason": m1_res.rejection_reason,
                        "latency_ms": round(m1_res.latency_ms, 2)
                    },
                    state=None,  # Strictly unvalidated
                    state_probs=None,
                    work_focus=None,
                    workload={"value": None, "status": "unvalidated"},
                    status=win_status,
                    rejection_reason=m1_res.rejection_reason or m2_res.error_reason,
                    metrics={
                        "process_rss_mb": round(current_rss, 1),
                        "m1_latency_ms": round(m1_lat, 2),
                        "m2_latency_ms": round(m2_lat, 2),
                        "total_window_latency_ms": round(w_latency_ms, 2)
                    }
                )

                records.append(record)
                out_f.write(json.dumps(record.model_dump(), ensure_ascii=False) + "\n")
                out_f.flush()

        total_wall_sec = time.time() - t_start_replay

        summary = {
            "session_id": manifest.session_id,
            "total_windows": len(records),
            "output_jsonl": output_jsonl_path,
            "duration_sec": total_duration,
            "wall_clock_time_sec": round(total_wall_sec, 2),
            "peak_rss_mb": round(peak_rss_mb, 1),
            "m1_latency_ms": {
                "mean": round(float(np.mean(m1_latencies)), 2) if m1_latencies else 0.0,
                "p50": round(float(np.median(m1_latencies)), 2) if m1_latencies else 0.0,
                "p95": round(float(np.percentile(m1_latencies, 95)), 2) if m1_latencies else 0.0
            },
            "m2_latency_ms": {
                "mean": round(float(np.mean(m2_latencies)), 2) if m2_latencies else 0.0,
                "p50": round(float(np.median(m2_latencies)), 2) if m2_latencies else 0.0,
                "p95": round(float(np.percentile(m2_latencies, 95)), 2) if m2_latencies else 0.0
            },
            "total_latency_ms": {
                "mean": round(float(np.mean(total_latencies)), 2) if total_latencies else 0.0,
                "p50": round(float(np.median(total_latencies)), 2) if total_latencies else 0.0,
                "p95": round(float(np.percentile(total_latencies, 95)), 2) if total_latencies else 0.0
            }
        }
        return summary
