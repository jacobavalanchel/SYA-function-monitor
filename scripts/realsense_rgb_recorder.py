#!/usr/bin/env python3
"""Record Intel RealSense RGB video into hourly files with disk cleanup."""

from __future__ import annotations

import argparse
import datetime as dt
import glob
import os
import shutil
import signal
import subprocess
import sys
import time
from pathlib import Path


STOP = False
CHILD: subprocess.Popen | None = None


def log(message: str) -> None:
    timestamp = dt.datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    print(f"[{timestamp}] {message}", flush=True)


def handle_signal(signum: int, _frame) -> None:
    global STOP
    STOP = True
    log(f"received signal {signum}; stopping after current ffmpeg process exits")
    if CHILD and CHILD.poll() is None:
        CHILD.terminate()


def run_text(command: list[str], timeout: int = 10) -> str:
    try:
        result = subprocess.run(command, check=False, capture_output=True, text=True, timeout=timeout)
    except (OSError, subprocess.SubprocessError):
        return ""
    return (result.stdout or "") + (result.stderr or "")


def is_realsense_video_device(device: str) -> bool:
    props = run_text(["udevadm", "info", "-q", "property", "-n", device])
    return "ID_VENDOR_ID=8086" in props and "RealSense" in props


def yuyv_sizes(device: str) -> list[tuple[int, int]]:
    output = run_text(["v4l2-ctl", "-d", device, "--list-formats-ext"])
    sizes: list[tuple[int, int]] = []
    in_yuyv = False
    for raw_line in output.splitlines():
        line = raw_line.strip()
        if line.startswith("["):
            in_yuyv = "'YUYV'" in line
            continue
        if not in_yuyv or not line.startswith("Size: Discrete "):
            continue
        size = line.removeprefix("Size: Discrete ").strip()
        if "x" not in size:
            continue
        width_text, height_text = size.split("x", 1)
        if width_text.isdigit() and height_text.isdigit():
            sizes.append((int(width_text), int(height_text)))
    return sizes


def choose_rgb_device(requested_device: str | None) -> tuple[str, list[tuple[int, int]]]:
    candidates = [requested_device] if requested_device else sorted(glob.glob("/dev/video*"))
    best: tuple[str, list[tuple[int, int]]] | None = None
    for device in candidates:
        if not device or not os.path.exists(device):
            continue
        if not requested_device and not is_realsense_video_device(device):
            continue
        sizes = yuyv_sizes(device)
        if not sizes:
            continue
        if best is None or max(w * h for w, h in sizes) > max(w * h for w, h in best[1]):
            best = (device, sizes)

    if best is None:
        raise RuntimeError("No Intel RealSense RGB/YUYV V4L2 video device was found.")
    return best


def choose_size(sizes: list[tuple[int, int]], requested_width: int, requested_height: int) -> tuple[int, int]:
    requested = (requested_width, requested_height)
    if requested in sizes:
        return requested
    return min(
        sizes,
        key=lambda size: (
            abs(size[0] - requested_width) + abs(size[1] - requested_height),
            -size[0] * size[1],
        ),
    )


def free_bytes(path: Path) -> int:
    return shutil.disk_usage(path).free


def oldest_recording(record_dir: Path, current_output: Path | None) -> Path | None:
    files = []
    for path in record_dir.glob("*.mp4"):
        if current_output and path.resolve() == current_output.resolve():
            continue
        if path.stat().st_size <= 0:
            continue
        files.append(path)
    if not files:
        return None
    return min(files, key=lambda path: path.stat().st_mtime)


def cleanup_one_if_needed(record_dir: Path, home_path: Path, min_free_bytes: int, current_output: Path | None) -> None:
    available = free_bytes(home_path)
    if available >= min_free_bytes:
        return

    victim = oldest_recording(record_dir, current_output)
    if victim is None:
        log(
            f"free space on {home_path} is {available / (1024 ** 3):.1f} GiB, "
            "but there are no completed recordings to delete"
        )
        return

    size = victim.stat().st_size
    victim.unlink()
    log(
        f"deleted oldest recording {victim} ({size / (1024 ** 2):.1f} MiB); "
        f"free space was {available / (1024 ** 3):.1f} GiB"
    )


def has_encoder(name: str) -> bool:
    return name in run_text(["ffmpeg", "-hide_banner", "-encoders"], timeout=20)


def ffmpeg_command(args: argparse.Namespace, device: str, size: tuple[int, int], output: Path) -> list[str]:
    width, height = size
    if has_encoder("libx264"):
        codec_args = ["-c:v", "libx264", "-preset", args.preset, "-crf", str(args.crf), "-pix_fmt", "yuv420p"]
    else:
        codec_args = ["-c:v", "mpeg4", "-q:v", "5"]

    return [
        "ffmpeg",
        "-hide_banner",
        "-loglevel",
        args.ffmpeg_loglevel,
        "-nostdin",
        "-y",
        "-f",
        "v4l2",
        "-thread_queue_size",
        "512",
        "-framerate",
        str(args.fps),
        "-video_size",
        f"{width}x{height}",
        "-i",
        device,
        "-an",
        "-frames:v",
        str(max(args.fps * args.segment_seconds, 1)),
        *codec_args,
        "-g",
        str(max(args.fps * 2, 1)),
        "-metadata",
        "source=Intel RealSense RGB",
        "-metadata",
        "depth=false",
        "-movflags",
        "+faststart",
        str(output),
    ]


def record_forever(args: argparse.Namespace) -> None:
    global CHILD

    record_dir = Path(args.record_dir).expanduser().resolve()
    home_path = Path(args.home_path).expanduser().resolve()
    min_free_bytes = int(args.min_free_gb * 1024 ** 3)
    record_dir.mkdir(parents=True, exist_ok=True)

    device, sizes = choose_rgb_device(args.device)
    size = choose_size(sizes, args.width, args.height)
    log(f"recording RealSense RGB from {device} at {size[0]}x{size[1]}@{args.fps} into {record_dir}")
    log(f"cleanup threshold: keep at least {args.min_free_gb:.1f} GiB free on {home_path}")

    while not STOP:
        cleanup_one_if_needed(record_dir, home_path, min_free_bytes, None)
        started = dt.datetime.now().strftime("%Y%m%d_%H%M%S")
        output = record_dir / f"realsense_rgb_{started}.mp4"
        command = ffmpeg_command(args, device, size, output)
        log("starting segment " + str(output))
        log("ffmpeg command: " + " ".join(command))
        CHILD = subprocess.Popen(command)

        while not STOP:
            return_code = CHILD.poll()
            if return_code is not None:
                if output.exists() and output.stat().st_size < 1024:
                    output.unlink()
                    log(f"discarded empty segment {output}")
                elif return_code == 0:
                    log(f"finished segment {output}")
                else:
                    log(f"ffmpeg exited with code {return_code} while writing {output}")
                break
            cleanup_one_if_needed(record_dir, home_path, min_free_bytes, output)
            time.sleep(args.check_interval_seconds)

        if CHILD and CHILD.poll() is None:
            CHILD.terminate()
            try:
                CHILD.wait(timeout=10)
            except subprocess.TimeoutExpired:
                CHILD.kill()
                CHILD.wait(timeout=5)
        if output.exists() and output.stat().st_size < 1024:
            output.unlink()
            log(f"discarded incomplete segment {output}")
        CHILD = None

        if not STOP:
            time.sleep(args.restart_delay_seconds)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--device", default=os.environ.get("REALSENSE_RGB_DEVICE"))
    parser.add_argument("--record-dir", default=os.environ.get("REALSENSE_RGB_RECORD_DIR", str(Path.home() / "realsense-rgb-recordings")))
    parser.add_argument("--home-path", default=os.environ.get("REALSENSE_RGB_HOME_PATH", "/home"))
    parser.add_argument("--min-free-gb", type=float, default=float(os.environ.get("REALSENSE_RGB_MIN_FREE_GB", "20")))
    parser.add_argument("--segment-seconds", type=int, default=int(os.environ.get("REALSENSE_RGB_SEGMENT_SECONDS", "3600")))
    parser.add_argument("--check-interval-seconds", type=int, default=int(os.environ.get("REALSENSE_RGB_CHECK_INTERVAL_SECONDS", "60")))
    parser.add_argument("--restart-delay-seconds", type=int, default=int(os.environ.get("REALSENSE_RGB_RESTART_DELAY_SECONDS", "5")))
    parser.add_argument("--width", type=int, default=int(os.environ.get("REALSENSE_RGB_WIDTH", "1280")))
    parser.add_argument("--height", type=int, default=int(os.environ.get("REALSENSE_RGB_HEIGHT", "720")))
    parser.add_argument("--fps", type=int, default=int(os.environ.get("REALSENSE_RGB_FPS", "30")))
    parser.add_argument("--preset", default=os.environ.get("REALSENSE_RGB_X264_PRESET", "veryfast"))
    parser.add_argument("--crf", type=int, default=int(os.environ.get("REALSENSE_RGB_X264_CRF", "23")))
    parser.add_argument("--ffmpeg-loglevel", default=os.environ.get("REALSENSE_RGB_FFMPEG_LOGLEVEL", "warning"))
    return parser.parse_args()


def main() -> int:
    for signum in (signal.SIGINT, signal.SIGTERM):
        signal.signal(signum, handle_signal)

    if shutil.which("ffmpeg") is None:
        log("ffmpeg is not installed or not in PATH")
        return 2
    if shutil.which("v4l2-ctl") is None:
        log("v4l2-ctl is not installed or not in PATH")
        return 2

    try:
        record_forever(parse_args())
    except Exception as error:
        log(f"fatal: {error}")
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
