"""
M2: Work Understanding Module using Qwen3-VL-4B-Instruct-4bit via MLX.
Supports:
1. Visual Input (Screenshots or Camera workspace frames)
2. Semantic Input (Accessibility foreground app, window title, document text)
3. Hybrid Input (Visual + Accessibility combined)
Evaluates:
- work_related: bool | null
- goal_aligned: bool | null
- brief_evidence: str
"""

import os
import re
import json
import time
from io import BytesIO
from typing import Optional, Dict, Any, List
from PIL import Image

from ..schemas import M2ScreenResult

try:
    from mlx_vlm import load, generate
    from mlx_vlm.prompt_utils import apply_chat_template
    from mlx_vlm.utils import load_config
    MLX_AVAILABLE = True
except ImportError:
    MLX_AVAILABLE = False


def extract_json_block(text: str) -> Optional[Dict[str, Any]]:
    """Extract and parse JSON object from model output text."""
    match = re.search(r"```(?:json)?\s*(\{.*?\})\s*```", text, re.DOTALL)
    if match:
        try:
            return json.loads(match.group(1))
        except json.JSONDecodeError:
            pass

    brace_match = re.search(r"(\{.*\})", text, re.DOTALL)
    if brace_match:
        try:
            return json.loads(brace_match.group(1))
        except json.JSONDecodeError:
            pass

    try:
        return json.loads(text.strip())
    except json.JSONDecodeError:
        return None


class ScreenAnalyzer:
    """
    MLX-based frozen Qwen3-VL-4B-Instruct screen & work reasoning engine.
    Supports both visual and accessibility semantic inputs.
    """
    def __init__(
        self,
        model_id: str = "mlx-community/Qwen3-VL-4B-Instruct-4bit",
        max_tokens: int = 192,
        temperature: float = 0.0
    ):
        if not MLX_AVAILABLE:
            raise RuntimeError("mlx_vlm is not installed. Please install mlx and mlx-vlm.")

        self.model_id = model_id
        self.max_tokens = max_tokens
        self.temperature = temperature
        self.model_version = f"{model_id}@mlx"

        # Pre-load model and processor
        self.model, self.processor = load(model_id)
        self.config = load_config(model_id)

    def analyze(
        self,
        image_path: Optional[str] = None,
        camera_image_path: Optional[str] = None,
        accessibility_info: Optional[Dict[str, Any]] = None,
        work_scope: str = "",
        current_goal: str = "",
        enable_screen: bool = True,
        enable_camera: bool = False,
        enable_accessibility: bool = True,
        past_context: Optional[str] = None,
        window_start: float = 0.0,
        window_end: float = 0.0,
        evidence_id: Optional[str] = None,
        camera_jpeg: Optional[bytes] = None,
    ) -> M2ScreenResult:
        """
        Analyze current user activity against declared work scope and goal using available inputs.
        """
        t0 = time.time()
        ev_ids = []

        # Determine visual inputs
        active_image = None
        if enable_screen and image_path and os.path.isfile(image_path):
            active_image = image_path
            ev_ids.append(f"screen:{os.path.basename(image_path)}")
        elif enable_camera:
            if camera_jpeg:
                try:
                    with Image.open(BytesIO(camera_jpeg)) as frame:
                        active_image = frame.convert("RGB")
                    ev_ids.append("camera:live")
                except OSError:
                    pass
            elif camera_image_path and os.path.isfile(camera_image_path):
                active_image = camera_image_path
                ev_ids.append(f"camera:{os.path.basename(camera_image_path)}")

        # Determine accessibility inputs
        has_acc = False
        app_name = "Unknown"
        acc_text = ""
        if enable_accessibility and accessibility_info:
            has_acc = True
            app_name = accessibility_info.get("app_name") or accessibility_info.get("appName") or "Unknown"
            win_title = accessibility_info.get("window_title") or accessibility_info.get("windowTitle") or ""
            page_url = accessibility_info.get("page_url") or accessibility_info.get("pageUrl") or ""
            raw_text = accessibility_info.get("semantic_text") or accessibility_info.get("text") or ""
            ev_ids.append(f"accessibility:{app_name}")

            acc_text = (
                f"[Accessibility Semantic Signal]:\n"
                f"- Foreground Application: {app_name}\n"
                f"- Window Title: {win_title or 'Untitled'}\n"
            )
            if page_url:
                acc_text += f"- Web URL / Domain: {page_url}\n"
            if raw_text:
                acc_text += f"- Visible Text Excerpt: {raw_text[:600]}\n"

        # Check if all inputs are disabled
        if not active_image and not has_acc:
            any_enabled = enable_screen or enable_camera or enable_accessibility
            return M2ScreenResult(
                window_start=window_start,
                window_end=window_end,
                work_related=None,
                goal_aligned=None,
                brief_evidence=("Enabled M2 inputs have no current observations." if any_enabled
                                else "All M2 input sources (Screen, Camera, Accessibility) are disabled."),
                active_app=None,
                error_reason="m2_inputs_unavailable" if any_enabled else "all_m2_inputs_disabled",
                model_version=self.model_version,
                evidence_ids=["disabled"],
                latency_ms=(time.time() - t0) * 1000
            )

        context_info = f"Prior Context: {past_context}" if past_context else "Prior Context: None"

        # Assemble prompt
        prompt_parts = [
            "You are an objective monitor evaluating whether the user's activity is work-related and goal-aligned.",
            "Determine two separate boolean attributes:",
            "1. 'work_related': true if the content falls within the user's declared Work Scope. "
            "Note: Necessary work communication (e.g. asking team members, reviewing documentation) "
            "MUST be considered work-related (true) even if the app switches.",
            "2. 'goal_aligned': true if the activity directly and actively contributes to the Current Concrete Goal.",
            "Note: Activity can be work_related=true but goal_aligned=false (e.g. general email or unrelated bug triage).\n",
            f"[Declared Work Scope]: {work_scope}",
            f"[Current Concrete Goal]: {current_goal}",
            f"[{context_info}]"
        ]

        if acc_text:
            prompt_parts.append(acc_text)
        if active_image:
            prompt_parts.append("[Visual Evidence]: Refer to the attached image representing the current visual state.")

        prompt_parts.extend([
            "\nYou MUST respond ONLY with valid JSON in this exact structure:",
            "```json",
            "{",
            '  "work_related": true | false | null,',
            '  "goal_aligned": true | false | null,',
            '  "brief_evidence": "<concise description of visible content, window title, or semantic activity>",',
            f'  "active_app": "{app_name}"',
            "}",
            "```"
        ])

        system_instruction = "\n".join(prompt_parts)

        # Chat template
        num_imgs = 1 if active_image else 0
        formatted_prompt = apply_chat_template(
            self.processor,
            self.config,
            prompt=system_instruction,
            num_images=num_imgs
        )

        try:
            gen_kwargs = {
                "model": self.model,
                "processor": self.processor,
                "prompt": formatted_prompt,
                "max_tokens": self.max_tokens,
                "temperature": self.temperature,
                "verbose": False
            }
            if active_image:
                gen_kwargs["image"] = [active_image]

            raw_result = generate(**gen_kwargs)
            raw_output = getattr(raw_result, "text", str(raw_result))
        except Exception as e:
            return M2ScreenResult(
                window_start=window_start,
                window_end=window_end,
                work_related=None,
                goal_aligned=None,
                brief_evidence="Inference execution failure",
                active_app=app_name,
                error_reason=f"inference_error: {str(e)}",
                model_version=self.model_version,
                evidence_ids=ev_ids,
                latency_ms=(time.time() - t0) * 1000
            )

        # Parse JSON
        parsed = extract_json_block(raw_output)
        latency_ms = (time.time() - t0) * 1000

        if parsed is None or not isinstance(parsed, dict):
            return M2ScreenResult(
                window_start=window_start,
                window_end=window_end,
                work_related=None,
                goal_aligned=None,
                brief_evidence=f"Failed to parse valid JSON from output: {raw_output[:100]}...",
                active_app=app_name,
                error_reason="json_decode_error",
                model_version=self.model_version,
                evidence_ids=ev_ids,
                latency_ms=latency_ms
            )

        w_rel = parsed.get("work_related")
        g_aln = parsed.get("goal_aligned")
        evidence = str(parsed.get("brief_evidence", "")).strip()
        detected_app = parsed.get("active_app") or app_name

        if not isinstance(w_rel, bool) and w_rel is not None:
            w_rel = None
        if not isinstance(g_aln, bool) and g_aln is not None:
            g_aln = None

        if not evidence:
            evidence = f"Output: {raw_output.strip()[:80]}"

        return M2ScreenResult(
            window_start=window_start,
            window_end=window_end,
            work_related=w_rel,
            goal_aligned=g_aln,
            brief_evidence=evidence,
            active_app=str(detected_app) if detected_app else None,
            error_reason=None,
            model_version=self.model_version,
            evidence_ids=ev_ids,
            latency_ms=latency_ms
        )

    # Backwards compatibility alias
    def analyze_screenshot(self, image_path: str, work_scope: str, current_goal: str, **kwargs):
        return self.analyze(
            image_path=image_path,
            work_scope=work_scope,
            current_goal=current_goal,
            enable_screen=True,
            enable_accessibility=False,
            **kwargs
        )
