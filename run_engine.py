#!/usr/bin/env python3
"""
SYA Live Attention & Work Understanding Engine Launcher.
Runs the zero-training M1 (EmotiEffLib RealSense/Webcam) + M2 (MLX Qwen3-VL-4B Multimodal Screen/Context) + M3 engine.
Serves FastAPI API and live debugger at http://localhost:8000.
"""

import os
import sys

# Force offline mode for pre-downloaded weights to guarantee ~2s instant cold start
os.environ.setdefault("HF_HUB_OFFLINE", "1")

# Ensure current directory is in sys.path
BASE_DIR = os.path.dirname(os.path.abspath(__file__))
if BASE_DIR not in sys.path:
    sys.path.insert(0, BASE_DIR)

from sya_engine.web_ui import run_server

def main():
    import argparse
    parser = argparse.ArgumentParser(description="SYA Engine Backend Launcher")
    parser.add_argument("--host", default="127.0.0.1", help="Binding host (default 127.0.0.1)")
    parser.add_argument("--port", type=int, default=8000, help="Port (default 8000)")
    parser.add_argument("--device", type=int, default=0, help="Camera device index (default 0)")
    parser.add_argument("--window", type=float, default=6.0, help="Window size in seconds (default 6.0)")
    parser.add_argument("--step", type=float, default=10.0, help="Continuous loop step size in seconds (default 10.0)")
    parser.add_argument("--goal", type=str, default="Implement and verify SYA zero-training offline replay system")
    parser.add_argument("--scope", type=str, default="Knowledge worker software development, document reading, research, and work-related communications")

    args = parser.parse_args()

    print("=" * 65)
    print("  🚀 Starting SYA Live Attention & Work Understanding Engine")
    print(f"  Directory: {BASE_DIR}")
    print(f"  Web Dashboard & API: http://localhost:{args.port}")
    print(f"  Camera Stream: http://localhost:{args.port}/video_feed")
    print("=" * 65)

    run_server(
        host=args.host,
        port=args.port,
        device_index=args.device,
        window_size=args.window,
        step_size=args.step,
        current_goal=args.goal,
        work_scope=args.scope
    )

if __name__ == "__main__":
    main()
