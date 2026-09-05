#!/usr/bin/env python3
"""Install and diagnose the pinned SYA Intel RealSense L515 runtime."""

import argparse
import datetime
import gc
import json
import os
import platform
import re
import shutil
import subprocess
import sys
import time
from pathlib import Path


SDK_VERSION = "2.54.2"
SDK_COMMIT = "2dbaaf5964490cb02f811cf2ed38c8a893f5c027"
SDK_REPOSITORY = "https://github.com/IntelRealSense/librealsense.git"
L515_PRODUCT_ID = "0b64"
SUPPORTED_FIRMWARE = "1.5.8.1"
MODULE_ROOT = Path(__file__).resolve().parents[1]
PATCH_PATH = MODULE_ROOT / "patches" / "librealsense-v2.54.2-macos-l515.patch"
REQUIREMENTS_PATH = MODULE_ROOT / "requirements-realsense.txt"
DEFAULT_ROOT = Path.home() / ".sya" / "monitor" / "realsense"
DEFAULT_VENV = Path.home() / ".sya" / "monitor" / ".venv"


def run(command, *, cwd=None, capture=False, env=None):
    options = {
        "cwd": str(cwd) if cwd else None,
        "check": True,
        "text": True,
        "env": env,
    }
    if capture:
        options["stdout"] = subprocess.PIPE
        options["stderr"] = subprocess.PIPE
    return subprocess.run([str(item) for item in command], **options)


def command_output(command):
    try:
        return run(command, capture=True).stdout.strip()
    except (OSError, subprocess.CalledProcessError):
        return ""


def import_status(module_name):
    try:
        module = __import__(module_name)
        return True, getattr(module, "__version__", "installed"), getattr(module, "__file__", "")
    except Exception as error:
        return False, "", str(error)


def darwin_usb_devices():
    if sys.platform != "darwin" or not shutil.which("system_profiler"):
        return []
    try:
        result = run(
            ["system_profiler", "SPUSBDataType", "-json", "-detailLevel", "mini"],
            capture=True,
        )
        report = json.loads(result.stdout)
    except (OSError, subprocess.CalledProcessError, json.JSONDecodeError):
        report = {}

    devices = []

    def visit(value):
        if isinstance(value, list):
            for item in value:
                visit(item)
            return
        if not isinstance(value, dict):
            return
        name = str(value.get("_name", ""))
        vendor = str(value.get("vendor_id", "")).lower()
        product = str(value.get("product_id", "")).lower()
        if "0x8086" in vendor and ("realsense" in name.lower() or f"0x{L515_PRODUCT_ID}" in product):
            devices.append(
                {
                    "name": name or "Intel RealSense camera",
                    "serialNumber": str(value.get("serial_num", "")),
                    "productId": product.split()[0].replace("0x", ""),
                    "locationId": str(value.get("location_id", "")),
                    "speed": str(value.get("speed", "")),
                }
            )
        for child in value.values():
            if isinstance(child, (dict, list)):
                visit(child)

    visit(report)
    return devices or darwin_ioreg_devices()


def darwin_ioreg_devices():
    if sys.platform != "darwin" or not shutil.which("ioreg"):
        return []
    try:
        output = run(
            ["ioreg", "-p", "IOUSB", "-c", "IOUSBHostDevice", "-r", "-l"],
            capture=True,
        ).stdout
    except (OSError, subprocess.CalledProcessError):
        return []

    devices = []
    for block in re.split(r"(?m)(?=^\s*\+-o )", output):
        if "realsense" not in block.lower():
            continue
        vendor_match = re.search(r'"idVendor"\s*=\s*(\d+)', block)
        product_match = re.search(r'"idProduct"\s*=\s*(\d+)', block)
        if not vendor_match or int(vendor_match.group(1)) != 0x8086:
            continue
        name_match = re.search(r"\+-o\s+(.+?)@", block)
        serial_match = re.search(r'"USB Serial Number"\s*=\s*"([^"]*)"', block)
        location_match = re.search(r'"locationID"\s*=\s*(\d+)', block)
        speed_match = re.search(r'"UsbLinkSpeed"\s*=\s*(\d+)', block)
        speed = int(speed_match.group(1)) if speed_match else 0
        devices.append(
            {
                "name": name_match.group(1).strip() if name_match else "Intel RealSense camera",
                "serialNumber": serial_match.group(1).lstrip("0") if serial_match else "",
                "productId": f"{int(product_match.group(1)):04x}" if product_match else "",
                "locationId": location_match.group(1) if location_match else "",
                "speed": f"{speed / 1_000_000_000:g} Gb/s" if speed else "",
            }
        )
    return devices


def sdk_devices():
    try:
        import pyrealsense2 as rs

        context = rs.context()
        devices = []
        for device in list(context.query_devices()):
            entry = {}
            for key, field in [
                ("name", rs.camera_info.name),
                ("serialNumber", rs.camera_info.serial_number),
                ("firmwareVersion", rs.camera_info.firmware_version),
                ("productId", rs.camera_info.product_id),
                ("usbType", rs.camera_info.usb_type_descriptor),
            ]:
                if device.supports(field):
                    entry[key] = device.get_info(field)
            devices.append(entry)
        return devices, ""
    except Exception as error:
        return [], str(error)


def doctor_report():
    py_ok, py_version, py_detail = import_status("pyrealsense2")
    pillow_ok, pillow_version, pillow_detail = import_status("PIL")
    media_ok, media_version, media_detail = import_status("mediapipe")
    numpy_ok, numpy_version, numpy_detail = import_status("numpy")
    usb_devices = darwin_usb_devices()
    enumerated_devices, enumerate_error = sdk_devices() if py_ok else ([], py_detail)
    pkg_version = command_output(["pkg-config", "--modversion", "realsense2"])
    enumerate_version = ""
    enumerate_tool = shutil.which("rs-enumerate-devices")
    if enumerate_tool:
        enumerate_version = command_output([enumerate_tool, "--version"])

    checks = [
        {
            "id": "platform",
            "status": "pass" if sys.platform == "darwin" else "warn",
            "detail": f"{platform.system()} {platform.release()} {platform.machine()}",
        },
        {
            "id": "python",
            "status": "pass" if sys.version_info >= (3, 9) else "fail",
            "detail": f"{sys.executable} ({platform.python_version()})",
        },
        {
            "id": "librealsense-sdk",
            "status": "pass"
            if py_ok and py_version == SDK_VERSION or SDK_VERSION in (pkg_version + enumerate_version)
            else "warn",
            "detail": pkg_version or enumerate_version or py_version or "librealsense pkg-config/tool not found",
        },
        {
            "id": "pyrealsense2",
            "status": "pass" if py_ok and py_version == SDK_VERSION else "fail",
            "detail": f"{py_version} at {py_detail}" if py_ok else py_detail,
        },
        {
            "id": "preview-dependencies",
            "status": "pass" if pillow_ok else "fail",
            "detail": f"Pillow {pillow_version}" if pillow_ok else pillow_detail,
        },
        {
            "id": "focus-dependencies",
            "status": "pass" if media_ok and numpy_ok else "warn",
            "detail": (
                f"MediaPipe {media_version}; NumPy {numpy_version}"
                if media_ok and numpy_ok
                else f"MediaPipe: {media_detail}; NumPy: {numpy_detail}"
            ),
        },
        {
            "id": "usb-device",
            "status": "pass" if usb_devices or enumerated_devices else "warn",
            "detail": usb_devices or enumerated_devices or "No Intel RealSense USB device detected",
        },
        {
            "id": "sdk-access",
            "status": "pass" if enumerated_devices else "fail",
            "detail": enumerated_devices or enumerate_error or "No SDK-accessible RealSense device",
        },
    ]
    privilege_required = bool(
        sys.platform == "darwin"
        and usb_devices
        and not enumerated_devices
        and ("access" in enumerate_error.lower() or "permission" in enumerate_error.lower())
    )
    return {
        "ready": all(item["status"] == "pass" for item in checks if item["id"] != "focus-dependencies"),
        "sdkVersion": SDK_VERSION,
        "supportedFirmware": SUPPORTED_FIRMWARE,
        "privilegeRequired": privilege_required,
        "checks": checks,
        "next": (
            "Retry smoke with sudo only after reviewing the command; the CLI never elevates itself."
            if privilege_required
            else "Run install when the pinned binding or preview dependencies are missing."
        ),
    }


def print_report(report, as_json=False):
    if as_json:
        print(json.dumps(report, indent=2))
        return
    print(f"RealSense L515 runtime: {'ready' if report['ready'] else 'needs attention'}")
    for check in report["checks"]:
        marker = {"pass": "OK", "warn": "WARN", "fail": "FAIL"}[check["status"]]
        detail = check["detail"]
        if isinstance(detail, list):
            detail = json.dumps(detail, separators=(",", ":"))
        print(f"[{marker:4}] {check['id']}: {detail}")
    print(report["next"])


def ensure_clean_patched_source(source):
    commit = command_output(["git", "-C", source, "rev-parse", "HEAD"])
    if commit != SDK_COMMIT:
        raise RuntimeError(f"Expected librealsense {SDK_COMMIT}, found {commit or 'no Git checkout'}.")
    if not PATCH_PATH.exists():
        raise RuntimeError(f"Missing pinned patch: {PATCH_PATH}")

    forward = subprocess.run(
        ["git", "-C", str(source), "apply", "--check", str(PATCH_PATH)],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )
    if forward.returncode == 0:
        run(["git", "-C", source, "apply", str(PATCH_PATH)])
        return "applied"

    reverse = subprocess.run(
        ["git", "-C", str(source), "apply", "--reverse", "--check", str(PATCH_PATH)],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )
    if reverse.returncode == 0:
        return "already-applied"
    raise RuntimeError(
        "The librealsense checkout has unrelated changes or a partial patch. Use a clean v2.54.2 checkout."
    )


def install_runtime(args):
    if sys.platform != "darwin":
        raise RuntimeError("The pinned L515 source patch installer currently supports macOS only.")
    if platform.machine() not in {"arm64", "x86_64"}:
        raise RuntimeError(f"Unsupported macOS architecture: {platform.machine()}")
    for command in ["git", "cmake", "brew"]:
        if not shutil.which(command):
            raise RuntimeError(f"Required command not found: {command}")

    root = Path(args.root).expanduser().resolve()
    venv = Path(args.venv).expanduser().resolve()
    source = Path(args.source).expanduser().resolve() if args.source else root / "src" / "librealsense"
    build = root / "build" / f"macos-{platform.machine()}-python{sys.version_info.major}.{sys.version_info.minor}"
    prefix = root / "sdk"
    root.mkdir(parents=True, exist_ok=True)
    source.parent.mkdir(parents=True, exist_ok=True)

    if not source.exists():
        run(["git", "clone", "--branch", f"v{SDK_VERSION}", "--depth", "1", SDK_REPOSITORY, source])
    patch_state = ensure_clean_patched_source(source)

    if not (venv / "bin" / "python3").exists():
        run([sys.executable, "-m", "venv", venv])
    venv_python = venv / "bin" / "python3"
    run([venv_python, "-m", "pip", "install", "--upgrade", "pip==26.0.1"])
    if args.skip_focus:
        run([venv_python, "-m", "pip", "install", "numpy==1.26.4", "Pillow==11.3.0"])
    else:
        run([venv_python, "-m", "pip", "install", "-r", REQUIREMENTS_PATH])

    site_packages = run(
        [
            venv_python,
            "-c",
            "import sysconfig; print(sysconfig.get_paths()['platlib'])",
        ],
        capture=True,
    ).stdout.strip()
    libusb_prefix = Path(command_output(["brew", "--prefix", "libusb"]))
    if not libusb_prefix.exists():
        raise RuntimeError("Homebrew libusb is missing. Install it with: brew install libusb")

    build.mkdir(parents=True, exist_ok=True)
    rpath = f"{prefix / 'lib'};{libusb_prefix / 'lib'}"
    configure = [
        "cmake",
        "-S",
        source,
        "-B",
        build,
        "-DCMAKE_POLICY_VERSION_MINIMUM=3.5",
        "-DCMAKE_BUILD_TYPE=Release",
        f"-DCMAKE_OSX_ARCHITECTURES={platform.machine()}",
        "-DCMAKE_OSX_DEPLOYMENT_TARGET=12.0",
        f"-DCMAKE_INSTALL_PREFIX={prefix}",
        f"-DCMAKE_INSTALL_RPATH={rpath}",
        "-DBUILD_SHARED_LIBS=ON",
        "-DFORCE_RSUSB_BACKEND=ON",
        "-DHWM_OVER_XU=OFF",
        "-DBUILD_EASYLOGGINGPP=ON",
        "-DBUILD_EXAMPLES=OFF",
        "-DBUILD_GRAPHICAL_EXAMPLES=OFF",
        "-DBUILD_GLSL_EXTENSIONS=OFF",
        "-DBUILD_TOOLS=OFF",
        "-DBUILD_PYTHON_BINDINGS=ON",
        f"-DPYTHON_EXECUTABLE={venv_python}",
        f"-DPYTHON_INSTALL_DIR={site_packages}",
        "-DBUILD_UNIT_TESTS=OFF",
        "-DBUILD_LEGACY_LIVE_TEST=OFF",
        "-DBUILD_WITH_OPENMP=OFF",
        "-DCHECK_FOR_UPDATES=OFF",
        "-DIMPORT_DEPTH_CAM_FW=OFF",
        "-DENABLE_CCACHE=OFF",
        f"-DLIBUSB_LIB={libusb_prefix / 'lib' / 'libusb-1.0.dylib'}",
        f"-DLIBUSB_INC={libusb_prefix / 'include' / 'libusb-1.0'}",
    ]
    run(configure)
    run(
        [
            "cmake",
            "--build",
            build,
            "--config",
            "Release",
            "--target",
            "pyrealsense2",
            "pybackend2",
            "pyrsutils",
            "--parallel",
            str(args.jobs),
        ]
    )
    run(["cmake", "--install", build, "--config", "Release"])

    verification = run(
        [
            venv_python,
            "-c",
            "import pyrealsense2 as rs; print(rs.__version__); print(rs.__file__)",
        ],
        capture=True,
    ).stdout.strip().splitlines()
    if not verification or verification[0] != SDK_VERSION:
        raise RuntimeError(f"Installed pyrealsense2 verification failed: {verification}")

    state = {
        "installedAt": datetime.datetime.now(datetime.timezone.utc).isoformat(),
        "sdkVersion": SDK_VERSION,
        "sdkCommit": SDK_COMMIT,
        "patch": str(PATCH_PATH),
        "patchState": patch_state,
        "architecture": platform.machine(),
        "python": str(venv_python),
        "pythonVersion": platform.python_version(),
        "module": verification[1] if len(verification) > 1 else "",
        "prefix": str(prefix),
        "firmwareOperationsIncluded": False,
    }
    state_path = root / "install.json"
    temporary = root / "install.json.tmp"
    temporary.write_text(json.dumps(state, indent=2) + "\n", encoding="utf-8")
    temporary.replace(state_path)
    print(json.dumps(state, indent=2))
    print("No firmware image or firmware update command was installed.")


def smoke(args):
    try:
        import pyrealsense2 as rs
    except Exception as error:
        raise RuntimeError(f"pyrealsense2 is unavailable: {error}") from error

    pipeline = rs.pipeline()
    config = rs.config()
    if args.serial:
        config.enable_device(args.serial)
    enable_color = not args.depth_only
    enable_depth = not args.color_only
    if enable_color:
        config.enable_stream(rs.stream.color, args.width, args.height, rs.format.rgb8, args.fps)
    if enable_depth:
        config.enable_stream(rs.stream.depth, args.width, args.height, rs.format.z16, args.fps)

    started = False
    color_frames = 0
    depth_frames = 0
    first_frame_ms = None
    started_at = time.monotonic()
    try:
        profile = pipeline.start(config)
        started = True
        device = profile.get_device()
        serial = device.get_info(rs.camera_info.serial_number)
        firmware = device.get_info(rs.camera_info.firmware_version)
        for _index in range(args.frames):
            frames = pipeline.wait_for_frames(args.timeout_ms)
            if frames.get_color_frame():
                color_frames += 1
            if frames.get_depth_frame():
                depth_frames += 1
            if first_frame_ms is None:
                first_frame_ms = round((time.monotonic() - started_at) * 1000)
        result = {
            "ok": color_frames == (args.frames if enable_color else 0)
            and depth_frames == (args.frames if enable_depth else 0),
            "serialNumber": serial,
            "firmwareVersion": firmware,
            "sdkVersion": rs.__version__,
            "requestedFrames": args.frames,
            "colorFrames": color_frames,
            "depthFrames": depth_frames,
            "firstFrameMs": first_frame_ms,
            "profile": f"{args.width}x{args.height}@{args.fps}",
        }
    except Exception as error:
        usb = darwin_usb_devices()
        result = {
            "ok": False,
            "error": str(error),
            "usbDevices": usb,
            "privilegeRequired": bool(
                sys.platform == "darwin"
                and usb
                and ("access" in str(error).lower() or "permission" in str(error).lower())
            ),
        }
    finally:
        if started:
            pipeline.stop()
        del pipeline
        gc.collect()

    print(json.dumps(result, indent=2) if args.json else result)
    return 0 if result["ok"] else 2


def print_env(args):
    venv = Path(args.venv).expanduser().resolve()
    python_path = venv / "bin" / "python3"
    print(f'export MONITOR_PYTHON="{python_path}"')
    print(f'export MONITOR_REALSENSE_VENV="{venv}"')


def build_parser():
    parser = argparse.ArgumentParser(
        description="Install and diagnose SYA's pinned Intel RealSense L515 runtime."
    )
    subparsers = parser.add_subparsers(dest="command", required=True)

    doctor_parser = subparsers.add_parser("doctor", help="Check SDK, Python, USB, and focus dependencies.")
    doctor_parser.add_argument("--json", action="store_true")

    install_parser = subparsers.add_parser("install", help="Build the patched 2.54.2 SDK and Python binding.")
    install_parser.add_argument("--root", default=str(DEFAULT_ROOT))
    install_parser.add_argument("--venv", default=str(DEFAULT_VENV))
    install_parser.add_argument("--source", help="Use a clean librealsense v2.54.2 checkout instead of cloning.")
    install_parser.add_argument("--jobs", type=int, default=max(1, min(os.cpu_count() or 2, 8)))
    install_parser.add_argument("--skip-focus", action="store_true", help="Install preview dependencies without MediaPipe.")

    smoke_parser = subparsers.add_parser("smoke", help="Acquire and release a bounded set of L515 frames.")
    smoke_parser.add_argument("--serial", default="")
    smoke_parser.add_argument("--frames", type=int, default=30)
    smoke_parser.add_argument("--width", type=int, default=640)
    smoke_parser.add_argument("--height", type=int, default=480)
    smoke_parser.add_argument("--fps", type=int, default=30)
    smoke_parser.add_argument("--timeout-ms", type=int, default=5000)
    stream_group = smoke_parser.add_mutually_exclusive_group()
    stream_group.add_argument("--depth-only", action="store_true")
    stream_group.add_argument("--color-only", action="store_true")
    smoke_parser.add_argument("--json", action="store_true")

    env_parser = subparsers.add_parser("env", help="Print environment variables used by the Monitor runtime.")
    env_parser.add_argument("--venv", default=str(DEFAULT_VENV))
    return parser


def main():
    args = build_parser().parse_args()
    try:
        if args.command == "doctor":
            report = doctor_report()
            print_report(report, args.json)
            return 0 if report["ready"] else 2
        if args.command == "install":
            install_runtime(args)
            return 0
        if args.command == "smoke":
            return smoke(args)
        if args.command == "env":
            print_env(args)
            return 0
    except (OSError, RuntimeError, subprocess.CalledProcessError) as error:
        print(f"realsense-l515: {error}", file=sys.stderr)
        return 1
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
