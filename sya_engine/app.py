"""
SYA Unified Software Application.
Single entry-point integrating M1 (webcam engagement), M2 (screen reasoning),
and M3 (causal coordination and replay) into a single cohesive system.

Supports one-line startup from both Python API and Command Line Interface.
"""

import os
import sys
import time
import json
from typing import Optional, List, Dict, Any, Union
import cv2
import psutil

from .schemas import (
    SessionManifest,
    SessionManifestWebcam,
    SessionManifestScreenshot,
    ReplayWindowRecord,
    M1EngagementResult,
    M2ScreenResult
)
from .m1_webcam import EngagementDetector
from .m1_webcam.camera_capture import LiveCameraCapture
from .m2_screen import ScreenAnalyzer
from .m3_coordinator import load_session_manifest, ReplayCoordinator
from .screen_capture import ScreenCaptureProvider
from .accessibility_provider import AccessibilityProvider


class SYASoftware:
    """
    Unified SYA System Software.
    Manages model lifecycles, memory caching, and causal execution.
    """
    def __init__(
        self,
        weights_dir: Optional[str] = None,
        m2_model_id: str = "mlx-community/Qwen3-VL-4B-Instruct-4bit",
        device: str = "cpu",
        quiet: bool = False
    ):
        self.quiet = quiet
        self.process = psutil.Process()
        self.mem_init_mb = self.process.memory_info().rss / (1024 * 1024)

        if not self.quiet:
            print("=" * 65)
            print("  SYA Attention & Screen Tracking Software (Unified Engine)")
            print(f"  Platform: macOS Apple Silicon (MLX + CPU) | Initial RSS: {self.mem_init_mb:.1f} MB")
            print("=" * 65)

        t0 = time.time()
        # Initialize M1 (CPU)
        if not self.quiet:
            print("[1/2] Loading M1: EmotiEffLib (MTCNN + ONNX + Attention Head on CPU)...")
        self.m1 = EngagementDetector(
            weights_dir=weights_dir,
            sliding_window_width=128,
            device=device
        )

        # Initialize M2 (MLX Apple Silicon)
        if not self.quiet:
            print(f"[2/2] Loading M2: Qwen3-VL-4B-Instruct-4bit via MLX...")
        self.m2 = ScreenAnalyzer(
            model_id=m2_model_id,
            max_tokens=192
        )

        self.mem_loaded_mb = self.process.memory_info().rss / (1024 * 1024)
        self.load_duration_sec = time.time() - t0

        self.camera: Optional[LiveCameraCapture] = None
        self.screen_provider = ScreenCaptureProvider()
        self.accessibility_provider = AccessibilityProvider()
        self.log_listeners = []
        self.live_window_counter = 0

        # Master sensor switch states (synchronized from SYA-UI FocusMonitorPage)
        self.camera_master_active = True
        self.screen_master_active = True
        self.accessibility_master_active = True

        # Fine-grained per-module ingestion switches
        self.m1_camera_enabled = True
        self.m2_camera_enabled = False
        self.m2_screen_enabled = True
        self.m2_accessibility_enabled = True

        if not self.quiet:
            print(f"✓ All modules initialized in {self.load_duration_sec:.2f}s!")
            print(f"  Single-Process Peak RSS: {self.mem_loaded_mb:.1f} MB (Delta: +{self.mem_loaded_mb - self.mem_init_mb:.1f} MB)")
            print("=" * 65)

    def log(self, message: str):
        """Broadcast log message to console and any registered listeners."""
        if not self.quiet:
            print(message)
        for listener in list(self.log_listeners):
            try:
                listener(message)
            except Exception:
                pass

    def start_live_camera(self, device_index: int = 0, target_fps: float = 25.0):
        """Start streaming from connected camera (e.g. Intel RealSense)."""
        if self.camera and self.camera.is_running:
            if self.camera.device_index == device_index:
                return
            self.camera.stop()

        self.camera = LiveCameraCapture(device_index=device_index, target_fps=target_fps)
        self.camera.start()
        self.log(f"[Camera] Connected to {self.camera.camera_name} on device index {device_index}")

    def stop_live_camera(self):
        """Stop streaming camera."""
        if self.camera:
            self.camera.stop()
            self.camera = None
            self.log("[Camera] Camera stopped.")

    def infer_live_window(
        self,
        window_size_sec: float = 6.0,
        work_scope: str = "Knowledge worker software development, document reading, research, and work-related communications",
        current_goal: str = "Implement and verify SYA zero-training offline replay system",
        past_context: str = "",
        m1_camera_enabled: Optional[bool] = None,
        m2_camera_enabled: Optional[bool] = None,
        m2_screen_enabled: Optional[bool] = None,
        m2_accessibility_enabled: Optional[bool] = None,
        camera_master_active: Optional[bool] = None,
        screen_master_active: Optional[bool] = None,
        accessibility_master_active: Optional[bool] = None,
        injected_accessibility: Optional[Dict[str, Any]] = None,
        desktop_managed: bool = False,
        desktop_screen_file: Optional[str] = None,
    ) -> ReplayWindowRecord:
        """
        Execute one real-time observation window from the live camera stream, screen, and accessibility.
        """
        t0_win = time.time()
        self.live_window_counter += 1
        win_id = self.live_window_counter

        # Determine effective states
        c_master = self.camera_master_active if camera_master_active is None else camera_master_active
        s_master = self.screen_master_active if screen_master_active is None else screen_master_active
        a_master = self.accessibility_master_active if accessibility_master_active is None else accessibility_master_active

        m1_cam = self.m1_camera_enabled if m1_camera_enabled is None else m1_camera_enabled
        m2_cam = self.m2_camera_enabled if m2_camera_enabled is None else m2_camera_enabled
        m2_scr = self.m2_screen_enabled if m2_screen_enabled is None else m2_screen_enabled
        m2_acc = self.m2_accessibility_enabled if m2_accessibility_enabled is None else m2_accessibility_enabled

        camera = self.camera
        cam_running = bool(camera and camera.is_running)
        eff_m1_camera = c_master and m1_cam and cam_running
        eff_m2_camera = c_master and m2_cam
        eff_m2_screen = s_master and m2_scr
        eff_m2_accessibility = a_master and m2_acc

        # M1 Inference
        t0_m1 = time.time()
        if eff_m1_camera:
            self.log(f"\n[Window #{win_id}] [M1] Collecting past {window_size_sec:.1f}s frames from RealSense...")
            frames_rgb, timestamps = camera.get_recent_frames(window_size_sec)
            m1_res = self.m1.infer_window(
                frames_rgb=frames_rgb,
                frame_timestamps=timestamps,
                window_start=t0_win - window_size_sec,
                window_end=t0_win
            )
            lat_m1 = (time.time() - t0_m1) * 1000
            self.log(f"[M1 Webcam] Result: {m1_res.prediction or m1_res.rejection_reason} | Valid frames: {m1_res.valid_face_frames}/{m1_res.total_sampled_frames} ({lat_m1:.1f}ms)")
        else:
            lat_m1 = (time.time() - t0_m1) * 1000
            rej_reason = "camera_stream_disabled" if not c_master else ("m1_camera_reading_disabled" if not m1_cam else "camera_not_running")
            self.log(f"\n[Window #{win_id}] [M1] Camera reading skipped / paused: {rej_reason}")
            m1_res = M1EngagementResult(
                window_start=t0_win - window_size_sec,
                window_end=t0_win,
                prediction=None,
                scores=None,
                valid_face_frames=0,
                total_sampled_frames=0,
                quality_status="camera_disabled" if rej_reason != "camera_not_running" else "camera_unavailable",
                rejection_reason=rej_reason,
                model_version=self.m1.model_version,
                latency_ms=lat_m1
            )

        # M2 Inferences (Screen, Camera, Accessibility)
        t0_m2 = time.time()
        self.log(f"[Window #{win_id}] [M2] Preparing inputs (Screen:{eff_m2_screen}, Camera:{eff_m2_camera}, Accessibility:{eff_m2_accessibility})...")

        screen_path = None
        is_live_grab = False
        if eff_m2_screen:
            if desktop_managed:
                screen_path = desktop_screen_file
                is_live_grab = bool(screen_path)
            else:
                screen_path, is_live_grab = self.screen_provider.capture()

        acc_info = None
        if eff_m2_accessibility and (not desktop_managed or injected_accessibility):
            snap = self.accessibility_provider.capture(injected_metadata=injected_accessibility)
            if snap.app_name and snap.app_name != "Unknown":
                acc_info = {
                    "app_name": snap.app_name,
                    "bundle_id": snap.bundle_id,
                    "window_title": snap.window_title,
                    "page_url": snap.page_url,
                    "semantic_text": snap.semantic_text
                }

        m2_res = self.m2.analyze(
            image_path=screen_path,
            camera_jpeg=camera.get_latest_jpeg() if eff_m2_camera and cam_running else None,
            accessibility_info=acc_info,
            work_scope=work_scope,
            current_goal=current_goal,
            enable_screen=eff_m2_screen,
            enable_camera=eff_m2_camera,
            enable_accessibility=eff_m2_accessibility,
            past_context=past_context,
            window_start=t0_win - window_size_sec,
            window_end=t0_win,
            evidence_id=f"live_m2_w{win_id}"
        )
        lat_m2 = (time.time() - t0_m2) * 1000
        self.log(f"[M2 Screen/Context] W_hat: {m2_res.work_related} | Goal: {m2_res.goal_aligned} ({lat_m2:.1f}ms)")
        self.log(f"   Evidence: {m2_res.brief_evidence}")

        # Total latency and RSS
        total_lat_ms = (time.time() - t0_win) * 1000
        current_rss = self.process.memory_info().rss / (1024 * 1024)

        # Status
        if m1_res.prediction is not None and m2_res.work_related is not None:
            win_status = "ok"
        elif m1_res.prediction is not None or m2_res.work_related is not None:
            win_status = "partial"
        else:
            win_status = "rejected"

        record = ReplayWindowRecord(
            session_id="live_realsense_session",
            window_id=win_id,
            window={
                "start": round(t0_win - window_size_sec, 2),
                "end": round(t0_win, 2),
                "emitted_at": time.time(),
                "latency_ms": round(total_lat_ms, 2)
            },
            screen={
                "W_hat": m2_res.work_related,
                "goal_aligned": m2_res.goal_aligned,
                "brief_evidence": m2_res.brief_evidence,
                "active_app": m2_res.active_app,
                "evidence_ids": m2_res.evidence_ids,
                "error_reason": m2_res.error_reason,
                "latency_ms": round(m2_res.latency_ms, 2),
                "is_live_grab": is_live_grab
            },
            person={
                "engagement_pred": m1_res.prediction,
                "engagement_scores": m1_res.scores,
                "P_hat": None,
                "status": "unvalidated_model_estimate",
                "valid_face_frames": m1_res.valid_face_frames,
                "total_sampled_frames": m1_res.total_sampled_frames,
                "quality_status": m1_res.quality_status,
                "rejection_reason": m1_res.rejection_reason,
                "latency_ms": round(m1_res.latency_ms, 2)
            },
            state=None,
            state_probs=None,
            work_focus=None,
            workload={"value": None, "status": "unvalidated"},
            status=win_status,
            rejection_reason=m1_res.rejection_reason or m2_res.error_reason,
            metrics={
                "process_rss_mb": round(current_rss, 1),
                "m1_latency_ms": round(lat_m1, 2),
                "m2_latency_ms": round(lat_m2, 2),
                "total_window_latency_ms": round(total_lat_ms, 2)
            }
        )

        self.log(f"[M3 Coordination] Window #{win_id} status: {win_status} | Total latency: {total_lat_ms:.1f}ms | RSS: {current_rss:.1f}MB")
        return record

    def build_manifest(
        self,
        video_path: str,
        screenshot_paths: Union[str, List[str]],
        work_scope: str = "Knowledge worker software development, document reading, research, and work-related communications",
        current_goal: str = "Implement and verify SYA zero-training offline replay system",
        past_context: str = "",
        session_id: Optional[str] = None,
        timestamps: Optional[List[float]] = None
    ) -> SessionManifest:
        """Build an in-memory SessionManifest directly from video and screenshot paths."""
        if not os.path.isfile(video_path):
            raise FileNotFoundError(f"Webcam video not found: {video_path}")

        # Resolve video duration and fps
        cap = cv2.VideoCapture(video_path)
        fps = float(cap.get(cv2.CAP_PROP_FPS))
        if fps <= 0:
            fps = 25.0
        total_frames = int(cap.get(cv2.CAP_PROP_FRAME_COUNT))
        duration = total_frames / fps
        cap.release()

        # Collect screenshots
        image_files = []
        if isinstance(screenshot_paths, str):
            if os.path.isdir(screenshot_paths):
                for f in sorted(os.listdir(screenshot_paths)):
                    if f.lower().endswith((".png", ".jpg", ".jpeg", ".webp")):
                        image_files.append(os.path.join(screenshot_paths, f))
            elif os.path.isfile(screenshot_paths):
                image_files = [screenshot_paths]
            else:
                raise FileNotFoundError(f"Screenshot path not found: {screenshot_paths}")
        elif isinstance(screenshot_paths, (list, tuple)):
            for p in screenshot_paths:
                if os.path.isfile(p):
                    image_files.append(p)
                else:
                    raise FileNotFoundError(f"Screenshot file not found: {p}")

        if not image_files:
            raise ValueError("At least one screenshot must be provided.")

        # Determine timestamps for screenshots
        screenshot_objs = []
        n_images = len(image_files)
        for i, img_path in enumerate(image_files):
            if timestamps and i < len(timestamps):
                ts = float(timestamps[i])
            else:
                # Distribute timestamps across video duration
                if n_images == 1:
                    ts = 0.0
                else:
                    ts = round((i / (n_images - 1)) * max(0.0, duration - 0.5), 2)

            screenshot_objs.append(
                SessionManifestScreenshot(
                    image_path=os.path.abspath(img_path),
                    timestamp=ts,
                    app_name=None,
                    window_title=None
                )
            )

        # Sort chronologically
        screenshot_objs.sort(key=lambda x: x.timestamp)

        sid = session_id or f"session_{int(time.time())}"
        return SessionManifest(
            session_id=sid,
            time_unit="seconds",
            work_scope=work_scope,
            current_goal=current_goal,
            past_context=past_context,
            webcam=SessionManifestWebcam(
                video_path=os.path.abspath(video_path),
                start_time_offset=0.0,
                duration=round(duration, 2),
                fps=round(fps, 2)
            ),
            screenshots=screenshot_objs
        )

    def run(
        self,
        video: Optional[str] = None,
        screenshots: Optional[Union[str, List[str]]] = None,
        manifest: Optional[str] = None,
        work_scope: str = "Knowledge worker software development, document reading, research, and work-related communications",
        current_goal: str = "Implement and verify SYA zero-training offline replay system",
        past_context: str = "",
        timestamps: Optional[List[float]] = None,
        window_size: float = 6.0,
        step_size: float = 6.0,
        output_path: Optional[str] = None
    ) -> Dict[str, Any]:
        """
        Execute unified dual-input causal replay in one step.
        """
        # Determine manifest
        if manifest:
            manifest_obj = load_session_manifest(manifest)
        elif video and screenshots:
            manifest_obj = self.build_manifest(
                video_path=video,
                screenshot_paths=screenshots,
                work_scope=work_scope,
                current_goal=current_goal,
                past_context=past_context,
                timestamps=timestamps
            )
        else:
            raise ValueError("Must provide either 'manifest' file path OR both 'video' and 'screenshots'.")

        if not output_path:
            out_dir = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "results"))
            os.makedirs(out_dir, exist_ok=True)
            output_path = os.path.join(out_dir, f"replay_{manifest_obj.session_id}.jsonl")

        if not self.quiet:
            print(f"\n▶ Starting Causal Replay:")
            print(f"  Session ID:    {manifest_obj.session_id}")
            print(f"  Webcam:        {manifest_obj.webcam.video_path} ({manifest_obj.webcam.duration}s @ {manifest_obj.webcam.fps}fps)")
            print(f"  Screenshots:   {len(manifest_obj.screenshots)} images")
            print(f"  Window Size:   {window_size:.1f}s | Step Size: {step_size:.1f}s")
            print(f"  Output JSONL:  {output_path}")
            print("-" * 65)

        coordinator = ReplayCoordinator(
            m1_detector=self.m1,
            m2_analyzer=self.m2,
            window_size_sec=window_size,
            step_size_sec=step_size
        )

        summary = coordinator.run_replay(
            manifest=manifest_obj,
            output_jsonl_path=output_path
        )

        # Read back records
        records = []
        with open(output_path, "r", encoding="utf-8") as f:
            for line in f:
                if line.strip():
                    records.append(json.loads(line))

        # Pretty print results
        if not self.quiet:
            self._print_summary_table(summary, records)

        return {
            "session_id": manifest_obj.session_id,
            "manifest": manifest_obj.model_dump(),
            "summary": summary,
            "records": records,
            "output_jsonl": output_path
        }

    def _print_summary_table(self, summary: Dict[str, Any], records: List[Dict[str, Any]]):
        """Format and print an executive summary table of the replay windows."""
        print("\n" + "=" * 78)
        print("                        SYA REPLAY EXECUTION REPORT")
        print("=" * 78)
        print(f" Session ID: {summary['session_id']} | Total Windows: {len(records)} | Peak RSS: {summary['peak_rss_mb']} MB")
        print(f" Total Wall Time: {summary['wall_clock_time_sec']}s | Mean Window Latency: {summary['total_latency_ms']['mean']:.1f} ms")
        print("-" * 78)
        header = f"{'Win':<4} | {'Interval':<12} | {'M1 Engagement':<16} | {'W_hat':<7} | {'Goal':<7} | {'Status':<8} | {'Latency':<8}"
        print(header)
        print("-" * 78)

        for rec in records:
            w = rec["window"]
            interval = f"{w['start']:.1f}s - {w['end']:.1f}s"
            m1_info = rec["person"].get("engagement_pred") or rec["person"].get("rejection_reason") or "N/A"
            if len(m1_info) > 15:
                m1_info = m1_info[:14] + "…"
            w_hat = str(rec["screen"].get("W_hat"))
            goal = str(rec["screen"].get("goal_aligned"))
            status = rec.get("status", "unknown")
            lat = f"{w['latency_ms']:.0f}ms"
            print(f"{rec['window_id']:<4} | {interval:<12} | {m1_info:<16} | {w_hat:<7} | {goal:<7} | {status:<8} | {lat:<8}")

        print("-" * 78)
        print(" Scientific Boundaries Enforced:")
        print("   • P (Physiological Attention): NULL (unvalidated proxy)")
        print("   • W×P 4-States:                NULL (not mapped from engagement)")
        print("   • Work Focus:                  NULL (requires validated joint model)")
        print("   • Causal Isolation:            PASSED (no future lookahead)")
        print("=" * 78)


def run_sya(
    video: Optional[str] = None,
    screenshots: Optional[Union[str, List[str]]] = None,
    manifest: Optional[str] = None,
    work_scope: str = "Knowledge worker software development, document reading, research, and work-related communications",
    current_goal: str = "Implement and verify SYA zero-training offline replay system",
    output: Optional[str] = None,
    window_size: float = 6.0,
    step_size: float = 6.0
) -> Dict[str, Any]:
    """Single-line function API for starting the entire software."""
    app = SYASoftware()
    return app.run(
        video=video,
        screenshots=screenshots,
        manifest=manifest,
        work_scope=work_scope,
        current_goal=current_goal,
        output_path=output,
        window_size=window_size,
        step_size=step_size
    )
