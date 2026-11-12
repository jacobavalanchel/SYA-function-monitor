"""
SYA Minimum Workable CLI.
Unified command line interface for:
- M1 smoke: Real video engagement inference
- M2 inference: Single screenshot screen understanding
- Dual-input replay: Synchronized causal offline replay from session manifest
"""

import os
import sys
import json
import argparse
import time
import psutil

from .m1_webcam import EngagementDetector
from .m2_screen import ScreenAnalyzer
from .m3_coordinator import load_session_manifest, ReplayCoordinator


def cmd_m1_smoke(args):
    """Run M1 webcam smoke test on a real video file."""
    print(f"=== M1 Webcam Engagement Smoke ===")
    print(f"Video path: {args.video}")
    t0 = time.time()
    process = psutil.Process()
    mem_init = process.memory_info().rss / (1024 * 1024)

    detector = EngagementDetector(
        weights_dir=args.weights_dir,
        sliding_window_width=args.sliding_window_width,
        min_face_prob=args.min_face_prob,
        device=args.device
    )

    mem_after_load = process.memory_info().rss / (1024 * 1024)
    print(f"Loaded M1 models. Memory RSS: {mem_init:.1f} MB -> {mem_after_load:.1f} MB")

    result = detector.smoke_video(video_path=args.video, max_frames=args.max_frames)
    mem_final = process.memory_info().rss / (1024 * 1024)
    result["peak_rss_mb"] = round(mem_final, 1)

    print("\n--- Smoke Test Results ---")
    print(json.dumps(result, indent=2, ensure_ascii=False))

    if args.output:
        os.makedirs(os.path.dirname(os.path.abspath(args.output)), exist_ok=True)
        with open(args.output, "w", encoding="utf-8") as f:
            json.dump(result, f, indent=2, ensure_ascii=False)
        print(f"\nSaved smoke test result to: {args.output}")


def cmd_m2_infer(args):
    """Run M2 screenshot understanding using Qwen3-VL-4B-Instruct-4bit."""
    print(f"=== M2 Screen Understanding Inference ===")
    print(f"Image path: {args.image}")
    print(f"Work Scope: {args.work_scope}")
    print(f"Current Goal: {args.goal}")
    print(f"Active App: {args.app}")

    process = psutil.Process()
    mem_init = process.memory_info().rss / (1024 * 1024)

    t0 = time.time()
    analyzer = ScreenAnalyzer(
        model_id=args.model_id,
        max_tokens=args.max_tokens,
        temperature=args.temperature
    )
    mem_load = process.memory_info().rss / (1024 * 1024)
    print(f"Model loaded in {time.time() - t0:.2f}s. Memory RSS: {mem_init:.1f} MB -> {mem_load:.1f} MB")

    t_inf = time.time()
    result = analyzer.analyze_screenshot(
        image_path=args.image,
        work_scope=args.work_scope,
        current_goal=args.goal,
        app_name=args.app,
        past_context=args.context,
        window_start=0.0,
        window_end=1.0
    )
    mem_final = process.memory_info().rss / (1024 * 1024)

    output_data = result.model_dump()
    output_data["memory_rss_mb"] = round(mem_final, 1)

    print("\n--- Inference Output ---")
    print(json.dumps(output_data, indent=2, ensure_ascii=False))

    if args.output:
        os.makedirs(os.path.dirname(os.path.abspath(args.output)), exist_ok=True)
        with open(args.output, "w", encoding="utf-8") as f:
            json.dump(output_data, f, indent=2, ensure_ascii=False)
        print(f"\nSaved M2 inference result to: {args.output}")


def cmd_replay(args):
    """Run dual-input synchronized causal offline replay."""
    print(f"=== Dual-Input Causal Offline Replay ===")
    print(f"Manifest: {args.manifest}")
    print(f"Output JSONL: {args.output}")

    manifest = load_session_manifest(args.manifest)
    print(f"Session ID: {manifest.session_id}")
    print(f"Work Scope: {manifest.work_scope}")
    print(f"Current Goal: {manifest.current_goal}")
    print(f"Webcam Video: {manifest.webcam.video_path} (offset: {manifest.webcam.start_time_offset}s, duration: {manifest.webcam.duration}s)")
    print(f"Screenshots count: {len(manifest.screenshots)}")

    process = psutil.Process()
    mem_start = process.memory_info().rss / (1024 * 1024)

    print("\nInitializing M1 (Webcam Engagement Detector on CPU)...")
    m1 = EngagementDetector(
        weights_dir=args.weights_dir,
        sliding_window_width=args.sliding_window_width,
        device="cpu"
    )

    print("Initializing M2 (Qwen3-VL-4B-Instruct-4bit on MLX)...")
    m2 = ScreenAnalyzer(
        model_id=args.model_id,
        max_tokens=args.max_tokens
    )

    mem_models = process.memory_info().rss / (1024 * 1024)
    print(f"Both models loaded into single process! RSS: {mem_start:.1f} MB -> {mem_models:.1f} MB")

    coordinator = ReplayCoordinator(
        m1_detector=m1,
        m2_analyzer=m2,
        window_size_sec=args.window_size,
        step_size_sec=args.step_size
    )

    summary = coordinator.run_replay(
        manifest=manifest,
        output_jsonl_path=args.output
    )

    print("\n=== Replay Execution Summary ===")
    print(json.dumps(summary, indent=2, ensure_ascii=False))

    summary_file = os.path.splitext(args.output)[0] + "_summary.json"
    with open(summary_file, "w", encoding="utf-8") as f:
        json.dump(summary, f, indent=2, ensure_ascii=False)
    print(f"\nReplay completed! Results written to:\n- {args.output}\n- {summary_file}")


def main():
    parser = argparse.ArgumentParser(description="SYA Minimum Workable Zero-Training Offline Replay")
    subparsers = parser.add_subparsers(dest="command", required=True, help="Sub-command to execute")

    # M1 smoke
    p_m1 = subparsers.add_parser("m1-smoke", help="Run M1 webcam engagement smoke test on a video")
    p_m1.add_argument("--video", type=str, required=True, help="Path to video file")
    p_m1.add_argument("--weights-dir", type=str, default=None, help="Directory containing M1 weights")
    p_m1.add_argument("--sliding-window-width", type=int, default=128, help="Window size in face frames (default: 128)")
    p_m1.add_argument("--min-face-prob", type=float, default=0.90, help="Minimum face detection confidence (default: 0.90)")
    p_m1.add_argument("--device", type=str, default="cpu", help="Device for M1 (default: cpu)")
    p_m1.add_argument("--max-frames", type=int, default=None, help="Max frames to process (default: all)")
    p_m1.add_argument("--output", type=str, default=None, help="Optional output JSON path for results")
    p_m1.set_defaults(func=cmd_m1_smoke)

    # M2 infer
    p_m2 = subparsers.add_parser("m2-infer", help="Run M2 screen understanding inference on a screenshot")
    p_m2.add_argument("--image", type=str, required=True, help="Path to screenshot image")
    p_m2.add_argument("--work-scope", type=str, required=True, help="Declared work scope")
    p_m2.add_argument("--goal", type=str, required=True, help="Current concrete goal")
    p_m2.add_argument("--app", type=str, default=None, help="Active application name (optional)")
    p_m2.add_argument("--context", type=str, default=None, help="Prior context (optional)")
    p_m2.add_argument("--model-id", type=str, default="mlx-community/Qwen3-VL-4B-Instruct-4bit", help="MLX Model ID")
    p_m2.add_argument("--max-tokens", type=int, default=192, help="Max generation tokens")
    p_m2.add_argument("--temperature", type=float, default=0.0, help="Sampling temperature")
    p_m2.add_argument("--output", type=str, default=None, help="Optional output JSON path for results")
    p_m2.set_defaults(func=cmd_m2_infer)

    # Replay
    p_rep = subparsers.add_parser("replay", help="Run synchronized dual-input causal offline replay")
    p_rep.add_argument("--manifest", type=str, required=True, help="Path to session manifest JSON")
    p_rep.add_argument("--output", type=str, required=True, help="Output JSONL path for predictions")
    p_rep.add_argument("--weights-dir", type=str, default=None, help="Directory containing M1 weights")
    p_rep.add_argument("--sliding-window-width", type=int, default=128, help="M1 window size in face frames (default: 128)")
    p_rep.add_argument("--model-id", type=str, default="mlx-community/Qwen3-VL-4B-Instruct-4bit", help="M2 MLX Model ID")
    p_rep.add_argument("--max-tokens", type=int, default=192, help="M2 max tokens")
    p_rep.add_argument("--window-size", type=float, default=10.0, help="Window size in seconds (default: 10.0)")
    p_rep.add_argument("--step-size", type=float, default=5.0, help="Step size in seconds (default: 5.0)")
    p_rep.set_defaults(func=cmd_replay)

    args = parser.parse_args()
    args.func(args)


if __name__ == "__main__":
    main()
