"""
Data schemas for SYA zero-training offline replay system.
Strict validation of inputs and outputs across M1, M2, and M3.
"""

from typing import List, Optional, Dict, Any, Union
from pydantic import BaseModel, Field


class M1EngagementResult(BaseModel):
    """Output schema for M1 webcam engagement detector."""
    window_start: float = Field(..., description="Window start time in seconds relative to session start")
    window_end: float = Field(..., description="Window end time in seconds relative to session start")
    prediction: Optional[str] = Field(None, description="'Engaged', 'Distracted', or null if rejected")
    scores: Optional[Dict[str, float]] = Field(None, description="Class probabilities e.g. {'Distracted': 0.1, 'Engaged': 0.9}")
    classes_order: List[str] = Field(default_factory=lambda: ["Distracted", "Engaged"])
    valid_face_frames: int = Field(..., description="Number of valid face frames detected in window")
    total_sampled_frames: int = Field(..., description="Total sampled video frames in window")
    quality_status: str = Field(..., description="'good', 'low_quality', 'multiple_faces', or 'no_face'")
    rejection_reason: Optional[str] = Field(None, description="Explicit reason if window was rejected")
    model_version: str = Field(..., description="Model and weights identifier")
    latency_ms: float = Field(..., description="Execution time in milliseconds")


class M2ScreenResult(BaseModel):
    """Output schema for M2 screen understanding."""
    window_start: float = Field(..., description="Window start time in seconds")
    window_end: float = Field(..., description="Window end time in seconds")
    work_related: Optional[bool] = Field(None, description="True if within user confirmed work scope; False otherwise; null if unknown")
    goal_aligned: Optional[bool] = Field(None, description="True if actively advancing current concrete goal; False otherwise; null if unknown")
    brief_evidence: str = Field(..., description="Concise visual/textual evidence from screen")
    active_app: Optional[str] = Field(None, description="Detected or declared application name")
    error_reason: Optional[str] = Field(None, description="Error or rejection reason if schema check failed")
    model_version: str = Field(..., description="Model identifier")
    evidence_ids: List[str] = Field(default_factory=list, description="IDs of screenshots used in this inference")
    latency_ms: float = Field(..., description="Execution time in milliseconds")


class SessionManifestScreenshot(BaseModel):
    """Single screenshot entry in session manifest."""
    image_path: str = Field(..., description="Relative or absolute path to screenshot image")
    timestamp: float = Field(..., description="Timestamp in seconds relative to session start")
    app_name: Optional[str] = Field(None, description="Active application name at time of capture")
    window_title: Optional[str] = Field(None, description="Window title at time of capture")


class SessionManifestWebcam(BaseModel):
    """Webcam video mapping in session manifest."""
    video_path: str = Field(..., description="Relative or absolute path to webcam video file")
    start_time_offset: float = Field(0.0, description="Video offset relative to session start in seconds (usually 0.0)")
    duration: Optional[float] = Field(None, description="Known duration of video in seconds")
    fps: Optional[float] = Field(None, description="Native video FPS if known")


class SessionManifest(BaseModel):
    """Full session manifest defining an offline replay session."""
    session_id: str = Field(..., description="Unique identifier of recording session")
    time_unit: str = Field("seconds", description="Unit for all timestamps (must be 'seconds')")
    work_scope: str = Field(..., description="User confirmed work domain and accepted task categories")
    current_goal: str = Field(..., description="Current concrete objective for the session")
    webcam: SessionManifestWebcam = Field(..., description="Webcam video information")
    screenshots: List[SessionManifestScreenshot] = Field(..., description="Chronological list of screenshots")
    past_context: Optional[str] = Field("", description="Optional initial context or background info")


class ReplayWindowRecord(BaseModel):
    """Standardized JSONL output record for each replay window in M3."""
    session_id: str
    window_id: int
    window: Dict[str, Any] = Field(..., description="start, end, emitted_at, latency_ms")
    screen: Dict[str, Any] = Field(..., description="M2 result summary: W_hat, goal_aligned, brief_evidence, etc.")
    person: Dict[str, Any] = Field(..., description="M1 result summary: engagement_scores, valid_frames, quality, etc.")
    
    # Crucial scientific constraint fields:
    # Engagement is an estimated proxy; P (physiological attention), 4-state WxP, and work_focus are strictly null!
    state: Optional[str] = Field(None, description="WxP 4-state is NULL in zero-training minimum baseline")
    state_probs: Optional[Dict[str, float]] = Field(None, description="NULL (uncalibrated)")
    work_focus: Optional[bool] = Field(None, description="NULL (work_focus requires validated WxP, cannot be mapped directly)")
    workload: Dict[str, Any] = Field(default_factory=lambda: {"value": None, "status": "unvalidated"})
    
    status: str = Field(..., description="'ok', 'partial', or 'rejected'")
    rejection_reason: Optional[str] = None
    metrics: Dict[str, Any] = Field(..., description="Latency and memory usage metrics")
