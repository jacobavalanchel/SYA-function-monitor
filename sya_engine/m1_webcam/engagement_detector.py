"""
M1: Webcam Engagement Inference Module.
Based on EmotiEffLib (commit 520a051c64cd191521e5934655314e769a319684).
- Feature extraction: enet_b0_8_best_vgaf.onnx (CPU)
- Engagement classification: engagement_single_attention.h5 (Keras / TensorFlow on CPU)
- Face detection: MTCNN (CPU)

Fixes applied:
1. Boundary fix: N - sliding_window_width + 1 instead of N - sliding_window_width.
2. Caching: model instances and feature extractors are loaded once and reused.
3. Feature dim assertion: exactly 1280 (B0) -> concatenated with std to 2560.
4. Robust quality/rejection: missing face, multiple faces (>1), low confidence (<0.9),
   or temporal discontinuity (>1.0s gap) reject with explicit reason instead of defaulting to Distracted.
5. Preserves native timestamps and frame timings.
"""

import os
import time
import hashlib
from typing import List, Tuple, Optional, Dict, Any
import numpy as np
import cv2
from facenet_pytorch import MTCNN
from emotiefflib.facial_analysis import EmotiEffLibRecognizer
from emotiefflib.engagement_classification_model import get_engagement_model

from ..schemas import M1EngagementResult

# Expected SHA256 checksums from fixed commit
EXPECTED_ONNX_SHA256 = "fa07e841fd06c7a67ee651ea4e6e4a3a2bb5695f47b37a7da50492526f59c898"
EXPECTED_H5_SHA256 = "243b4699eec398a335d32774849f65b2f0e2d63e358df479c5ee95a002cac30d"


def verify_file_sha256(filepath: str, expected_sha256: str) -> bool:
    """Verify SHA256 checksum of a file."""
    if not os.path.isfile(filepath):
        return False
    hasher = hashlib.sha256()
    with open(filepath, "rb") as f:
        while chunk := f.read(65536):
            hasher.update(chunk)
    return hasher.hexdigest().lower() == expected_sha256.lower()


class EngagementDetector:
    """
    Cached, CPU-based engagement detector for webcam video streams.
    """
    def __init__(
        self,
        weights_dir: Optional[str] = None,
        sliding_window_width: int = 128,
        min_face_prob: float = 0.90,
        max_time_gap_sec: float = 1.0,
        device: str = "cpu"
    ):
        self.device = device
        self.sliding_window_width = sliding_window_width
        self.min_face_prob = min_face_prob
        self.max_time_gap_sec = max_time_gap_sec
        self.model_version = "EmotiEffLib@520a051c:enet_b0_8_best_vgaf.onnx+engagement_single_attention.h5"

        # Determine weights path
        if weights_dir is None:
            # Look in standard locations: Implementation/weights or ~/.emotiefflib
            local_weights = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "..", "weights"))
            if os.path.exists(local_weights):
                weights_dir = local_weights
            else:
                weights_dir = os.path.expanduser("~/.emotiefflib")

        self.weights_dir = weights_dir
        self.onnx_path = os.path.join(weights_dir, "enet_b0_8_best_vgaf.onnx")
        self.h5_path = os.path.join(weights_dir, "engagement_single_attention.h5")

        # Verify weights
        if not os.path.exists(self.onnx_path):
            raise FileNotFoundError(f"ONNX model not found at {self.onnx_path}")
        if not os.path.exists(self.h5_path):
            raise FileNotFoundError(f"Engagement head weights not found at {self.h5_path}")

        if not verify_file_sha256(self.onnx_path, EXPECTED_ONNX_SHA256):
            raise ValueError(f"ONNX model at {self.onnx_path} failed SHA256 checksum verification!")
        if not verify_file_sha256(self.h5_path, EXPECTED_H5_SHA256):
            raise ValueError(f"H5 weights at {self.h5_path} failed SHA256 checksum verification!")

        # Initialize MTCNN detector
        self.mtcnn = MTCNN(keep_all=False, post_process=False, min_face_size=40, device=self.device)

        # Initialize ONNX feature extractor
        self.fer = EmotiEffLibRecognizer(engine="onnx", model_name="enet_b0_8_best_vgaf", device=self.device)

        # Pre-load and cache engagement head model (feature_dim = 2560 for B0 + std)
        # Note: sliding_window_width frames in window
        self.engagement_head = get_engagement_model(2560, self.sliding_window_width)

        self.idx_to_class = {
            0: "Distracted",
            1: "Engaged"
        }

    def detect_and_crop_face(self, frame_rgb: np.ndarray) -> Tuple[Optional[np.ndarray], str, float]:
        """
        Detect face using MTCNN.
        Returns:
            (cropped_face_rgb, status, confidence)
            status can be 'ok', 'no_face', 'low_confidence', 'multiple_faces'
        """
        boxes, probs = self.mtcnn.detect(frame_rgb, landmarks=False)
        if boxes is None or probs is None or len(probs) == 0 or probs[0] is None:
            return None, "no_face", 0.0

        # Check for multiple prominent faces
        confident_faces = [b for b, p in zip(boxes, probs) if p is not None and p >= self.min_face_prob]
        if len(confident_faces) > 1:
            return None, "multiple_faces", float(probs[0])

        conf = float(probs[0])
        if conf < self.min_face_prob:
            return None, "low_confidence", conf

        box = boxes[0].astype(int)
        h, w, _ = frame_rgb.shape
        x1, y1 = max(0, box[0]), max(0, box[1])
        x2, y2 = min(w, box[2]), min(h, box[3])

        if x2 <= x1 or y2 <= y1:
            return None, "invalid_box", conf

        face_crop = frame_rgb[y1:y2, x1:x2]
        return face_crop, "ok", conf

    def classify_window_features(self, features: np.ndarray) -> Tuple[str, Dict[str, float]]:
        """
        Run cached engagement classification head on a single window of 128 frames.
        features: (128, 1280)
        """
        if features.shape != (self.sliding_window_width, 1280):
            raise ValueError(f"Expected shape ({self.sliding_window_width}, 1280), got {features.shape}")

        stat_func = np.std
        std_feat = stat_func(features, axis=0).reshape((1, -1)) # (1, 1280)
        mean_x = np.repeat(std_feat, self.sliding_window_width, axis=0) # (128, 1280)

        # Concatenate std and original features -> (128, 2560)
        window_feature = np.concatenate((mean_x, features), axis=1) # (128, 2560)
        window_batch = np.expand_dims(window_feature, axis=0) # (1, 128, 2560)

        # Predict using cached head
        scores = self.engagement_head.predict(window_batch, verbose=0)[0] # shape (2,)
        pred_idx = int(np.argmax(scores))
        pred_label = self.idx_to_class[pred_idx]
        scores_dict = {
            "Distracted": float(scores[0]),
            "Engaged": float(scores[1])
        }
        return pred_label, scores_dict

    def infer_window(
        self,
        frames_rgb: List[np.ndarray],
        frame_timestamps: List[float],
        window_start: float,
        window_end: float
    ) -> M1EngagementResult:
        """
        Causal window inference on a sequence of frames occurring within [window_start, window_end].
        """
        t0 = time.time()
        total_sampled = len(frames_rgb)

        if total_sampled == 0:
            return M1EngagementResult(
                window_start=window_start,
                window_end=window_end,
                prediction=None,
                scores=None,
                valid_face_frames=0,
                total_sampled_frames=0,
                quality_status="no_frames",
                rejection_reason="no_frames_in_window",
                model_version=self.model_version,
                latency_ms=(time.time() - t0) * 1000
            )

        valid_faces = []
        valid_timestamps = []
        quality_counts = {"ok": 0, "no_face": 0, "multiple_faces": 0, "low_confidence": 0}

        for frame, ts in zip(frames_rgb, frame_timestamps):
            crop, status, _ = self.detect_and_crop_face(frame)
            quality_counts[status] = quality_counts.get(status, 0) + 1
            if status == "ok" and crop is not None:
                valid_faces.append(crop)
                valid_timestamps.append(ts)

        valid_count = len(valid_faces)

        # Check rejection reasons
        if quality_counts["multiple_faces"] > 0.3 * total_sampled:
            return M1EngagementResult(
                window_start=window_start,
                window_end=window_end,
                prediction=None,
                scores=None,
                valid_face_frames=valid_count,
                total_sampled_frames=total_sampled,
                quality_status="multiple_faces",
                rejection_reason="multiple_faces_detected_in_window",
                model_version=self.model_version,
                latency_ms=(time.time() - t0) * 1000
            )

        # Check temporal continuity
        if len(valid_timestamps) >= 2:
            gaps = np.diff(valid_timestamps)
            max_gap = float(np.max(gaps))
            if max_gap > self.max_time_gap_sec:
                return M1EngagementResult(
                    window_start=window_start,
                    window_end=window_end,
                    prediction=None,
                    scores=None,
                    valid_face_frames=valid_count,
                    total_sampled_frames=total_sampled,
                    quality_status="time_gap_exceeded",
                    rejection_reason=f"temporal_gap_{max_gap:.2f}s_exceeds_threshold",
                    model_version=self.model_version,
                    latency_ms=(time.time() - t0) * 1000
                )

        # We require at least sliding_window_width valid face frames to form a window
        if valid_count < self.sliding_window_width:
            reason = "no_face_detected" if valid_count == 0 else f"insufficient_face_frames_{valid_count}_of_{self.sliding_window_width}"
            return M1EngagementResult(
                window_start=window_start,
                window_end=window_end,
                prediction=None,
                scores=None,
                valid_face_frames=valid_count,
                total_sampled_frames=total_sampled,
                quality_status="insufficient_frames",
                rejection_reason=reason,
                model_version=self.model_version,
                latency_ms=(time.time() - t0) * 1000
            )

        # Extract features for the latest sliding_window_width valid face frames
        window_faces = valid_faces[-self.sliding_window_width:]
        features = self.fer.extract_features(window_faces)

        # Classify using cached head
        pred_label, scores_dict = self.classify_window_features(features)

        latency_ms = (time.time() - t0) * 1000
        return M1EngagementResult(
            window_start=window_start,
            window_end=window_end,
            prediction=pred_label,
            scores=scores_dict,
            classes_order=["Distracted", "Engaged"],
            valid_face_frames=valid_count,
            total_sampled_frames=total_sampled,
            quality_status="good",
            rejection_reason=None,
            model_version=self.model_version,
            latency_ms=latency_ms
        )

    def smoke_video(self, video_path: str, max_frames: Optional[int] = None) -> Dict[str, Any]:
        """
        Smoke test on an entire video file.
        Extracts all face frames, fixes the boundary bug, and returns summary metrics.
        """
        t0 = time.time()
        if not os.path.exists(video_path):
            raise FileNotFoundError(f"Video file not found: {video_path}")

        cap = cv2.VideoCapture(video_path)
        fps = float(cap.get(cv2.CAP_PROP_FPS))
        if fps <= 0:
            fps = 25.0
        total_video_frames = int(cap.get(cv2.CAP_PROP_FRAME_COUNT))

        facial_images = []
        timestamps = []
        frame_idx = 0

        while cap.isOpened():
            ret, frame = cap.read()
            if not ret:
                break
            if max_frames and frame_idx >= max_frames:
                break

            ts = frame_idx / fps
            frame_idx += 1
            image_rgb = cv2.cvtColor(frame, cv2.COLOR_BGR2RGB)
            crop, status, _ = self.detect_and_crop_face(image_rgb)
            if status == "ok" and crop is not None:
                facial_images.append(crop)
                timestamps.append(ts)

        cap.release()
        read_time = time.time() - t0

        t_feat = time.time()
        if len(facial_images) < self.sliding_window_width:
            return {
                "video_path": video_path,
                "fps": fps,
                "total_video_frames": total_video_frames,
                "valid_face_frames": len(facial_images),
                "status": "rejected",
                "rejection_reason": f"valid_frames_{len(facial_images)}_less_than_window_{self.sliding_window_width}",
                "elapsed_sec": time.time() - t0
            }

        # Extract features in batches
        features = self.fer.extract_features(facial_images)
        feat_time = time.time() - t_feat

        # Slicing with boundary fix: N - sliding_window_width + 1
        num_windows = features.shape[0] - self.sliding_window_width + 1
        windows_features = []
        for i in range(num_windows):
            w = features[i : i + self.sliding_window_width]
            std_feat = np.std(w, axis=0).reshape((1, -1))
            mean_x = np.repeat(std_feat, self.sliding_window_width, axis=0)
            windows_features.append(np.concatenate((mean_x, w), axis=1))

        windows_batch = np.array(windows_features)

        t_cls = time.time()
        scores = self.engagement_head.predict(windows_batch, verbose=0)
        cls_time = time.time() - t_cls

        preds = np.argmax(scores, axis=1)
        labels = [self.idx_to_class[p] for p in preds]

        mean_scores = np.mean(scores, axis=0)
        overall_pred = self.idx_to_class[int(np.argmax(mean_scores))]

        return {
            "video_path": video_path,
            "fps": fps,
            "total_video_frames": total_video_frames,
            "valid_face_frames": len(facial_images),
            "num_windows": num_windows,
            "overall_prediction": overall_pred,
            "mean_scores": {
                "Distracted": float(mean_scores[0]),
                "Engaged": float(mean_scores[1])
            },
            "first_5_labels": labels[:5],
            "last_5_labels": labels[-5:],
            "timing_sec": {
                "read_and_detect": read_time,
                "feature_extraction": feat_time,
                "classification": cls_time,
                "total": time.time() - t0
            },
            "status": "success"
        }
