#!/usr/bin/env python3
"""Install and diagnose the pinned SYA Intel RealSense L515 runtime."""

import argparse
import contextlib
import datetime
import gc
import hashlib
import json
import os
import platform
import re
import shutil
import subprocess
import sys
import tempfile
import time
import uuid
from pathlib import Path

try:
    import fcntl
except ImportError:  # pragma: no cover - installer is macOS-only
    fcntl = None


SDK_VERSION = "2.54.2"
SDK_COMMIT = "2dbaaf5964490cb02f811cf2ed38c8a893f5c027"
SDK_REPOSITORY = "https://github.com/IntelRealSense/librealsense.git"
PYBIND11_COMMIT = "0bd8896a4010f2d91b2340570c24fa08606ec406"
L515_PRODUCT_ID = "0b64"
SUPPORTED_FIRMWARE = "1.5.8.1"
PATCH_SHA256 = "eb3acdba968cb716133a176a0051af7d06568594cd3b1d8a65f6d3b1202317c1"
MIN_PYTHON = (3, 9)
MAX_PYTHON = (3, 12)
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


def normalize_product_id(value):
    return str(value or "").strip().lower().removeprefix("0x").zfill(4)


def is_l515(device):
    return normalize_product_id(device.get("productId")) == L515_PRODUCT_ID


def supported_python(version_info=None):
    selected = version_info or sys.version_info
    return MIN_PYTHON <= tuple(selected[:2]) < MAX_PYTHON


def positive_int(value):
    try:
        parsed = int(value)
    except ValueError as error:
        raise argparse.ArgumentTypeError("must be a positive integer") from error
    if parsed <= 0:
        raise argparse.ArgumentTypeError("must be a positive integer")
    return parsed


def patch_sha256():
    digest = hashlib.sha256()
    with PATCH_PATH.open("rb") as patch_file:
        for chunk in iter(lambda: patch_file.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


@contextlib.contextmanager
def install_locks(paths):
    if fcntl is None:
        raise RuntimeError("Filesystem locking is unavailable on this platform.")
    handles = []
    try:
        for lock_path in sorted({Path(path).resolve() for path in paths}, key=str):
            lock_path.parent.mkdir(parents=True, exist_ok=True)
            handle = lock_path.open("a+", encoding="utf-8")
            try:
                fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError as error:
                handle.close()
                raise RuntimeError(f"Another RealSense installation is using {lock_path}.") from error
            handle.seek(0)
            handle.truncate()
            handle.write(f"pid={os.getpid()}\n")
            handle.flush()
            handles.append(handle)
        yield
    finally:
        for handle in reversed(handles):
            try:
                fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
            finally:
                handle.close()


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
    l515_usb_devices = [device for device in usb_devices if is_l515(device)]
    l515_sdk_devices = [device for device in enumerated_devices if is_l515(device)]
    firmware_versions = {
        str(device.get("firmwareVersion", "")) for device in l515_sdk_devices
    }
    firmware_supported = bool(l515_sdk_devices) and firmware_versions == {SUPPORTED_FIRMWARE}
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
            "status": "pass" if supported_python() else "fail",
            "detail": (
                f"{sys.executable} ({platform.python_version()}); supported range is "
                f"{MIN_PYTHON[0]}.{MIN_PYTHON[1]}-{MAX_PYTHON[0]}.{MAX_PYTHON[1] - 1}"
            ),
        },
        {
            "id": "librealsense-sdk",
            "status": "pass"
            if (py_ok and py_version == SDK_VERSION) or SDK_VERSION in (pkg_version + enumerate_version)
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
            "status": "pass" if l515_usb_devices or l515_sdk_devices else "warn",
            "detail": l515_usb_devices or l515_sdk_devices or "No Intel RealSense L515 USB device detected",
        },
        {
            "id": "sdk-access",
            "status": "pass" if l515_sdk_devices else "fail",
            "detail": l515_sdk_devices or enumerate_error or "No SDK-accessible Intel RealSense L515",
        },
        {
            "id": "firmware",
            "status": "pass" if firmware_supported else "fail",
            "detail": (
                f"L515 firmware {SUPPORTED_FIRMWARE}"
                if firmware_supported
                else f"Expected L515 firmware {SUPPORTED_FIRMWARE}; detected {sorted(firmware_versions) or 'unavailable'}"
            ),
        },
    ]
    privilege_required = bool(
        sys.platform == "darwin"
        and l515_usb_devices
        and not l515_sdk_devices
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


def git_tree(source, *, patched):
    with tempfile.TemporaryDirectory(prefix="sya-l515-index-") as temporary:
        index_path = Path(temporary) / "index"
        env = os.environ.copy()
        env["GIT_INDEX_FILE"] = str(index_path)
        run(["git", "-C", source, "read-tree", SDK_COMMIT], env=env)
        if patched:
            run(
                [
                    "git",
                    "-C",
                    source,
                    "apply",
                    "--cached",
                    "--unidiff-zero",
                    "--whitespace=nowarn",
                    PATCH_PATH,
                ],
                env=env,
            )
        else:
            run(["git", "-C", source, "add", "-A", "--", "."], env=env)
        return run(["git", "-C", source, "write-tree"], capture=True, env=env).stdout.strip()


def ensure_clean_patched_source(source):
    commit = command_output(["git", "-C", source, "rev-parse", "HEAD"])
    if commit != SDK_COMMIT:
        raise RuntimeError(f"Expected librealsense {SDK_COMMIT}, found {commit or 'no Git checkout'}.")
    if not PATCH_PATH.exists():
        raise RuntimeError(f"Missing pinned patch: {PATCH_PATH}")
    actual_patch_sha = patch_sha256()
    if actual_patch_sha != PATCH_SHA256:
        raise RuntimeError(
            f"Pinned patch checksum mismatch: expected {PATCH_SHA256}, found {actual_patch_sha}."
        )

    clean_tree = command_output(["git", "-C", source, "rev-parse", f"{SDK_COMMIT}^{{tree}}"])
    expected_tree = git_tree(source, patched=True)
    actual_tree = git_tree(source, patched=False)
    if actual_tree == expected_tree:
        return "already-applied"
    if actual_tree != clean_tree:
        raise RuntimeError(
            "The librealsense checkout has unrelated changes or a partial patch. Use a clean v2.54.2 checkout."
        )

    forward = subprocess.run(
        [
            "git",
            "-C",
            str(source),
            "apply",
            "--check",
            "--unidiff-zero",
            "--whitespace=nowarn",
            str(PATCH_PATH),
        ],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )
    if forward.returncode == 0:
        run(
            [
                "git",
                "-C",
                source,
                "apply",
                "--unidiff-zero",
                "--whitespace=nowarn",
                PATCH_PATH,
            ]
        )
        if git_tree(source, patched=False) != expected_tree:
            raise RuntimeError("The librealsense source did not match the pinned patch after application.")
        return "applied"
    raise RuntimeError(
        f"Pinned librealsense patch cannot be applied: {forward.stderr.strip() or 'unknown error'}"
    )


def install_runtime(args):
    if sys.platform != "darwin":
        raise RuntimeError("The pinned L515 source patch installer currently supports macOS only.")
    if platform.machine() not in {"arm64", "x86_64"}:
        raise RuntimeError(f"Unsupported macOS architecture: {platform.machine()}")
    if not supported_python():
        raise RuntimeError(
            "Run the installer with Python 3.9, 3.10, or 3.11; other versions are not supported."
        )
    for command in ["git", "cmake", "brew"]:
        if not shutil.which(command):
            raise RuntimeError(f"Required command not found: {command}")
    if not REQUIREMENTS_PATH.is_file():
        raise RuntimeError(f"Missing pinned dependency file: {REQUIREMENTS_PATH}")
    if not PATCH_PATH.is_file() or patch_sha256() != PATCH_SHA256:
        raise RuntimeError("The pinned librealsense patch is missing or its checksum does not match.")

    root = Path(args.root).expanduser().resolve()
    venv = Path(args.venv).expanduser().resolve()
    source = Path(args.source).expanduser().resolve() if args.source else root / "src" / "librealsense"
    if root in {Path("/"), Path.home()}:
        raise RuntimeError("Refusing to use a filesystem root or the home directory as the install root.")

    libusb_prefix = Path(command_output(["brew", "--prefix", "libusb"]))
    libusb_library = libusb_prefix / "lib" / "libusb-1.0.dylib"
    libusb_headers = libusb_prefix / "include" / "libusb-1.0"
    if not libusb_library.is_file() or not libusb_headers.is_dir():
        raise RuntimeError("Homebrew libusb is missing. Install it with: brew install libusb")

    with install_locks(
        [
            root / ".install.lock",
            source.parent / f".{source.name}.sya-install.lock",
            venv.parent / f".{venv.name}.sya-install.lock",
        ]
    ):
        _install_runtime(args, root, venv, source, libusb_prefix)


def _install_runtime(args, root, venv, source, libusb_prefix):
    prefix = root / "sdk"
    source.parent.mkdir(parents=True, exist_ok=True)

    if not source.exists():
        clone_path = source.with_name(f".{source.name}.clone-{uuid.uuid4().hex[:8]}")
        try:
            run(
                [
                    "git",
                    "clone",
                    "--branch",
                    f"v{SDK_VERSION}",
                    "--depth",
                    "1",
                    SDK_REPOSITORY,
                    clone_path,
                ]
            )
            clone_path.replace(source)
        except Exception:
            shutil.rmtree(clone_path, ignore_errors=True)
            raise
    patch_state = ensure_clean_patched_source(source)

    venv.parent.mkdir(parents=True, exist_ok=True)
    if not (venv / "bin" / "python3").exists():
        run([sys.executable, "-m", "venv", venv])
    venv_python = venv / "bin" / "python3"
    interpreter = json.loads(
        run(
            [
                venv_python,
                "-c",
                "import json,platform,sys; print(json.dumps({'version': list(sys.version_info[:3]), 'machine': platform.machine()}))",
            ],
            capture=True,
        ).stdout
    )
    if not supported_python(interpreter["version"]):
        raise RuntimeError(
            f"The target venv uses unsupported Python {'.'.join(map(str, interpreter['version']))}."
        )
    if interpreter["machine"] != platform.machine():
        raise RuntimeError(
            f"The target venv architecture is {interpreter['machine']}, expected {platform.machine()}."
        )
    build = root / "build" / (
        f"macos-{platform.machine()}-python{interpreter['version'][0]}.{interpreter['version'][1]}"
    )
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
    if build.exists():
        shutil.rmtree(build)
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
        "-DBUILD_LEGACY_PYBACKEND=OFF",
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
            "pyrsutils",
            "--parallel",
            str(args.jobs),
        ]
    )
    run(["cmake", "--install", build, "--config", "Release"])

    verification = json.loads(run(
        [
            venv_python,
            "-c",
            (
                "import json, pathlib, pyrealsense2 as rs, pyrsutils, sysconfig; "
                "print(json.dumps({'version': rs.__version__, 'pyrealsense2': rs.__file__, "
                "'pyrsutils': pyrsutils.__file__, 'sitePackages': sysconfig.get_paths()['platlib']}))"
            ),
        ],
        capture=True,
    ).stdout)
    if verification.get("version") != SDK_VERSION:
        raise RuntimeError(f"Installed pyrealsense2 verification failed: {verification}")
    site_packages_path = Path(verification["sitePackages"]).resolve()
    for module_name in ["pyrealsense2", "pyrsutils"]:
        module_path = Path(verification[module_name]).resolve()
        if site_packages_path not in module_path.parents:
            raise RuntimeError(f"{module_name} loaded outside the target venv: {module_path}")
    for stale_backend in site_packages_path.glob("pybackend2*"):
        if stale_backend.is_dir() and not stale_backend.is_symlink():
            shutil.rmtree(stale_backend)
        else:
            stale_backend.unlink()

    installed_packages = run([venv_python, "-m", "pip", "freeze", "--all"], capture=True).stdout.splitlines()

    state = {
        "installedAt": datetime.datetime.now(datetime.timezone.utc).isoformat(),
        "sdkVersion": SDK_VERSION,
        "sdkCommit": SDK_COMMIT,
        "pybind11Commit": PYBIND11_COMMIT,
        "patch": str(PATCH_PATH),
        "patchSha256": PATCH_SHA256,
        "patchState": patch_state,
        "architecture": platform.machine(),
        "python": str(venv_python),
        "pythonVersion": ".".join(map(str, interpreter["version"])),
        "module": verification["pyrealsense2"],
        "prefix": str(prefix),
        "packages": installed_packages,
        "firmwareOperationsIncluded": False,
    }
    state_path = root / "install.json"
    temporary = root / "install.json.tmp"
    temporary.write_text(json.dumps(state, indent=2) + "\n", encoding="utf-8")
    temporary.replace(state_path)
    print(json.dumps(state, indent=2))
    print("No firmware image or firmware update command was installed.")


def smoke(args):
    pipeline = None
    started = False
    color_frames = 0
    depth_frames = 0
    first_frame_ms = None
    started_at = time.monotonic()
    result = None
    sdk_version = ""
    serial = ""
    product_id = ""
    firmware = ""
    try:
        import pyrealsense2 as rs

        sdk_version = getattr(rs, "__version__", "")
        if sdk_version != SDK_VERSION:
            raise RuntimeError(
                f"pyrealsense2 must be exactly {SDK_VERSION}; detected {sdk_version or 'unknown'}."
            )
        l515_devices = []
        for device in list(rs.context().query_devices()):
            product_id = device.get_info(rs.camera_info.product_id)
            serial_number = device.get_info(rs.camera_info.serial_number)
            if normalize_product_id(product_id) == L515_PRODUCT_ID:
                l515_devices.append((device, serial_number))
        if args.serial:
            l515_devices = [entry for entry in l515_devices if entry[1] == args.serial]
        if not l515_devices:
            target = f" with serial {args.serial}" if args.serial else ""
            raise RuntimeError(f"No SDK-accessible Intel RealSense L515{target} was detected.")

        selected_device, selected_serial = l515_devices[0]
        serial = selected_serial
        product_id = normalize_product_id(selected_device.get_info(rs.camera_info.product_id))
        firmware = selected_device.get_info(rs.camera_info.firmware_version)
        if firmware != SUPPORTED_FIRMWARE:
            raise RuntimeError(
                f"Intel RealSense L515 firmware must be exactly {SUPPORTED_FIRMWARE}; "
                f"detected {firmware}. Firmware updates are never performed automatically."
            )
        pipeline = rs.pipeline()
        config = rs.config()
        config.enable_device(selected_serial)
        enable_color = not args.depth_only
        enable_depth = not args.color_only
        if enable_color:
            config.enable_stream(rs.stream.color, args.width, args.height, rs.format.rgb8, args.fps)
        if enable_depth:
            config.enable_stream(rs.stream.depth, args.width, args.height, rs.format.z16, args.fps)

        profile = pipeline.start(config)
        started = True
        device = profile.get_device()
        serial = device.get_info(rs.camera_info.serial_number)
        firmware = device.get_info(rs.camera_info.firmware_version)
        product_id = normalize_product_id(device.get_info(rs.camera_info.product_id))
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
            and depth_frames == (args.frames if enable_depth else 0)
            and product_id == L515_PRODUCT_ID
            and firmware == SUPPORTED_FIRMWARE
            and sdk_version == SDK_VERSION,
            "serialNumber": serial,
            "productId": product_id,
            "firmwareVersion": firmware,
            "firmwareSupported": firmware == SUPPORTED_FIRMWARE,
            "sdkVersion": sdk_version,
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
            "sdkVersion": sdk_version,
            "usbDevices": usb,
            "privilegeRequired": bool(
                sys.platform == "darwin"
                and usb
                and ("access" in str(error).lower() or "permission" in str(error).lower())
            ),
        }
        if serial:
            result["serialNumber"] = serial
        if product_id:
            result["productId"] = product_id
        if firmware:
            result["firmwareVersion"] = firmware
            result["firmwareSupported"] = firmware == SUPPORTED_FIRMWARE
    finally:
        if started:
            try:
                pipeline.stop()
            except Exception as error:
                result["ok"] = False
                result["releaseError"] = str(error)
        pipeline = None
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
    install_parser.add_argument("--jobs", type=positive_int, default=max(1, min(os.cpu_count() or 2, 8)))
    install_parser.add_argument("--skip-focus", action="store_true", help="Install preview dependencies without MediaPipe.")

    smoke_parser = subparsers.add_parser("smoke", help="Acquire and release a bounded set of L515 frames.")
    smoke_parser.add_argument("--serial", default="")
    smoke_parser.add_argument("--frames", type=positive_int, default=30)
    smoke_parser.add_argument("--width", type=positive_int, default=640)
    smoke_parser.add_argument("--height", type=positive_int, default=480)
    smoke_parser.add_argument("--fps", type=positive_int, default=30)
    smoke_parser.add_argument("--timeout-ms", type=positive_int, default=5000)
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
