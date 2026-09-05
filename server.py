#!/usr/bin/env python3
"""Local HTTP runtime for the SYA monitor function artifact."""

import http.server
import array
import json
import os
import queue
import re
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
import uuid

from realsense_runtime import EventBroker, FocusEstimator, encode_rgb_jpeg, utc_timestamp

STATIC_DIR = os.path.dirname(os.path.abspath(__file__))
MODULE_ROOT = os.path.dirname(STATIC_DIR)
DEFAULT_CONFIG_PATH = os.path.join(STATIC_DIR, "config.local.json")
FALLBACK_CONFIG_PATH = os.path.join(STATIC_DIR, "config.json")
MANIFEST_PATHS = [
    os.path.join(STATIC_DIR, "manifest.json"),
    os.path.join(MODULE_ROOT, "manifest.json"),
]
DEFAULT_PORT = 8765
DEFAULT_RECORDS_DIR = os.environ.get(
    "MONITOR_RECORDS_DIR",
    os.path.join(os.path.expanduser("~"), ".sya", "monitor", "records"),
)
USER_CONFIG_PATH = os.path.join(os.path.expanduser("~"), ".sya", "monitor", "config.local.json")

ACTION_DEFINITIONS = [
    {"action": "health", "method": "GET", "path": "/health", "description": "Check monitor runtime health."},
    {"action": "manifest", "method": "GET", "path": "/manifest", "description": "Read monitor runtime manifest."},
    {"action": "actions.list", "method": "GET", "path": "/api/monitor/actions", "description": "List callable monitor actions."},
    {
        "action": "capabilities.list",
        "method": "GET",
        "path": "/api/monitor/capabilities",
        "description": "List monitor capabilities, events, permissions, and actions.",
    },
    {"action": "invoke", "method": "POST", "path": "/api/monitor/invoke", "description": "Invoke a monitor action by action name."},
    {"action": "screen.status", "method": "GET", "path": "/api/monitor/screen/status", "description": "Read screen monitor status."},
    {"action": "screen.start", "method": "POST", "path": "/api/monitor/screen/start", "description": "Start screen monitoring."},
    {"action": "screen.stop", "method": "POST", "path": "/api/monitor/screen/stop", "description": "Stop screen monitoring."},
    {"action": "camera.status", "method": "GET", "path": "/api/monitor/camera/status", "description": "Read camera monitor status."},
    {"action": "camera.start", "method": "POST", "path": "/api/monitor/camera/start", "description": "Start camera monitoring."},
    {"action": "camera.stop", "method": "POST", "path": "/api/monitor/camera/stop", "description": "Stop camera monitoring."},
    {"action": "camera.show", "method": "POST", "path": "/api/monitor/camera/show", "description": "Show the camera monitor window."},
    {"action": "camera.hide", "method": "POST", "path": "/api/monitor/camera/hide", "description": "Hide the camera monitor window."},
    {"action": "camera.sources", "method": "GET", "path": "/api/monitor/camera/sources", "description": "List available camera data sources."},
    {"action": "realsense.status", "method": "GET", "path": "/api/monitor/realsense/status", "description": "Read Intel RealSense adapter status."},
    {"action": "realsense.devices", "method": "GET", "path": "/api/monitor/realsense/devices", "description": "List connected Intel RealSense devices."},
    {"action": "realsense.start", "method": "POST", "path": "/api/monitor/realsense/start", "description": "Start Intel RealSense streaming."},
    {"action": "realsense.stop", "method": "POST", "path": "/api/monitor/realsense/stop", "description": "Stop Intel RealSense streaming."},
    {"action": "realsense.capture", "method": "POST", "path": "/api/monitor/realsense/capture", "description": "Capture a RealSense RGB/depth sample for inference or training."},
    {
        "action": "realsense.frame.latest",
        "method": "GET",
        "path": "/api/monitor/realsense/frame/latest",
        "description": "Read the latest in-memory RealSense color preview and focus result.",
    },
    {
        "action": "realsense.training.start",
        "method": "POST",
        "path": "/api/monitor/realsense/training/start",
        "description": "Start a RealSense training data capture session.",
    },
    {
        "action": "realsense.training.stop",
        "method": "POST",
        "path": "/api/monitor/realsense/training/stop",
        "description": "Stop the active RealSense training data capture session.",
    },
    {"action": "storage.stats", "method": "GET", "path": "/api/monitor/storage/stats", "description": "Read monitor storage statistics."},
    {
        "action": "storage.screenRecords",
        "method": "GET",
        "path": "/api/monitor/storage/screen-records",
        "description": "List stored screen records.",
    },
    {
        "action": "storage.cameraRecords",
        "method": "GET",
        "path": "/api/monitor/storage/camera-records",
        "description": "List stored camera records.",
    },
    {
        "action": "attention.config.status",
        "method": "GET",
        "path": "/api/monitor/attention/config/status",
        "description": "Read attention LLM config status.",
    },
    {
        "action": "settings.status",
        "method": "GET",
        "path": "/api/monitor/settings/status",
        "description": "Read persisted monitor settings.",
    },
    {
        "action": "settings.save",
        "method": "POST",
        "path": "/api/monitor/settings/save",
        "description": "Persist monitor settings for camera, RealSense, storage, and recording.",
    },
]


def default_screen_status():
    return {
        "isMonitoring": False,
        "captureRateMs": 10000,
        "activeWindow": "",
        "idleTimeSeconds": 0,
        "totalCaptures": 0,
        "lastCaptureTime": None,
        "storagePath": "",
    }


def default_camera_status():
    return {
        "isActive": False,
        "windowCreated": False,
        "faceDetected": False,
        "totalFrames": 0,
        "lastFrameTime": None,
        "storagePath": "",
    }


SCREEN_STATE = default_screen_status()
CAMERA_STATE = default_camera_status()
CAMERA_STATE["source"] = "builtin"
CAMERA_STATE["availableSources"] = ["builtin", "realsense"]
EVENT_BROKER = EventBroker()


class RealSenseAdapter:
    def __init__(self):
        self.rs = None
        self.import_error = ""
        self.pipeline = None
        self.profile = None
        self.active = False
        self.started_at = None
        self.total_frames = 0
        self.last_frame_time = None
        self.last_sample_path = ""
        self.last_error = ""
        self.preview_error = ""
        self.total_samples = 0
        self.device_cache = []
        self.device_serial = ""
        self.depth_scale = None
        self.latest_frame_data = None
        self.latest_raw_frame = None
        self.latest_attention = None
        self.focus_detector = None
        self.worker = None
        self.stop_event = threading.Event()
        self.lock = threading.RLock()
        self.frame_condition = threading.Condition(self.lock)
        self.stream_config = {
            "width": 640,
            "height": 480,
            "fps": 30,
            "colorWidth": 640,
            "colorHeight": 480,
            "colorFps": 30,
            "depthWidth": 640,
            "depthHeight": 480,
            "depthFps": 30,
            "enableColor": True,
            "enableDepth": True,
            "previewEnabled": True,
            "previewFps": 2,
            "jpegQuality": 72,
            "focusEnabled": True,
            "focusFps": 2,
            "serial": "",
        }
        self.training_session = None

    def _load_sdk(self):
        if self.rs is not None:
            return self.rs
        if self.import_error:
            raise RuntimeError(self.import_error)

        try:
            import pyrealsense2 as rs  # type: ignore
        except Exception as error:
            self.import_error = (
                "pyrealsense2 is not installed. Install Intel RealSense SDK "
                "and the pyrealsense2 Python package before selecting the RealSense camera source."
            )
            self.last_error = self.import_error
            raise RuntimeError(self.import_error) from error

        self.rs = rs
        return rs

    def _device_info(self, device):
        rs = self.rs
        fields = {
            "name": "name",
            "serialNumber": "serial_number",
            "firmwareVersion": "firmware_version",
            "productId": "product_id",
            "physicalPort": "physical_port",
            "usbType": "usb_type_descriptor",
        }
        info = {}
        for key, attr in fields.items():
            try:
                camera_info = getattr(rs.camera_info, attr)
                if device.supports(camera_info):
                    info[key] = device.get_info(camera_info)
            except Exception:
                continue
        info["sdkAccessible"] = True
        return info

    def _query_devices(self):
        rs = self._load_sdk()
        context = rs.context()
        return list(context.query_devices())

    def _device_serial(self, device):
        rs = self.rs
        try:
            serial_info = rs.camera_info.serial_number
            if device.supports(serial_info):
                return device.get_info(serial_info)
        except Exception:
            return ""
        return ""

    def _usb_realsense_devices(self):
        root = "/sys/bus/usb/devices"
        if not os.path.isdir(root):
            return self._darwin_usb_realsense_devices() if sys.platform == "darwin" else []

        devices = []
        for name in os.listdir(root):
            path = os.path.join(root, name)
            vendor = self._read_sysfs_value(path, "idVendor").lower()
            if vendor != "8086":
                continue

            product = self._read_sysfs_value(path, "product")
            manufacturer = self._read_sysfs_value(path, "manufacturer")
            product_id = self._read_sysfs_value(path, "idProduct")
            serial = self._read_sysfs_value(path, "serial")
            descriptor = f"{manufacturer} {product}".lower()
            if "realsense" not in descriptor:
                continue

            devices.append({
                "name": product or "Intel RealSense camera",
                "serialNumber": serial,
                "productId": product_id,
                "physicalPort": name,
                "sdkAccessible": False,
            })
        return devices

    def _darwin_usb_realsense_devices(self):
        try:
            result = subprocess.run(
                ["system_profiler", "SPUSBDataType", "-json", "-detailLevel", "mini"],
                check=False,
                capture_output=True,
                text=True,
                timeout=15,
            )
            if result.returncode != 0:
                return self._darwin_ioreg_realsense_devices()
            report = json.loads(result.stdout)
        except (OSError, subprocess.SubprocessError, json.JSONDecodeError):
            return self._darwin_ioreg_realsense_devices()

        devices = []

        def visit(value):
            if isinstance(value, list):
                for item in value:
                    visit(item)
                return
            if not isinstance(value, dict):
                return

            name = str(value.get("_name", ""))
            vendor_id = str(value.get("vendor_id", "")).lower()
            product_id = str(value.get("product_id", "")).lower()
            if "0x8086" in vendor_id and ("realsense" in name.lower() or "0x0b64" in product_id):
                devices.append({
                    "name": name or "Intel RealSense camera",
                    "serialNumber": str(value.get("serial_num", "")),
                    "productId": product_id.split()[0].replace("0x", ""),
                    "physicalPort": str(value.get("location_id", "")),
                    "usbType": str(value.get("speed", "")),
                    "sdkAccessible": False,
                })
            for child in value.values():
                if isinstance(child, (dict, list)):
                    visit(child)

        visit(report)
        unique = {}
        for device in devices:
            key = device.get("serialNumber") or device.get("physicalPort") or device.get("name")
            unique[key] = device
        return list(unique.values()) or self._darwin_ioreg_realsense_devices()

    def _darwin_ioreg_realsense_devices(self):
        try:
            result = subprocess.run(
                ["ioreg", "-p", "IOUSB", "-c", "IOUSBHostDevice", "-r", "-l"],
                check=False,
                capture_output=True,
                text=True,
                timeout=15,
            )
            if result.returncode != 0:
                return []
        except (OSError, subprocess.SubprocessError):
            return []

        devices = []
        blocks = re.split(r"(?m)(?=^\s*\+-o )", result.stdout)
        for block in blocks:
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
            devices.append({
                "name": name_match.group(1).strip() if name_match else "Intel RealSense camera",
                "serialNumber": serial_match.group(1).lstrip("0") if serial_match else "",
                "productId": f"{int(product_match.group(1)):04x}" if product_match else "",
                "physicalPort": location_match.group(1) if location_match else "",
                "usbType": f"{speed / 1_000_000_000:g} Gb/s" if speed else "",
                "sdkAccessible": False,
            })
        return devices

    def _read_sysfs_value(self, path, filename):
        try:
            with open(os.path.join(path, filename), "r", encoding="utf-8") as handle:
                return handle.read().strip()
        except OSError:
            return ""

    def _inaccessible_usb_message(self):
        usb_devices = self._usb_realsense_devices()
        if not usb_devices:
            return ""
        if any(str(device.get("productId", "")).lower() == "0b64" for device in usb_devices):
            if sys.platform == "darwin":
                return (
                    "Intel RealSense L515 is visible over USB, but librealsense cannot access its depth sensor. "
                    "Run scripts/realsense-l515 doctor and smoke from Terminal; direct USB access on macOS "
                    "may require elevated privileges."
                )
            sdk_version = getattr(self.rs, "__version__", "") if self.rs is not None else ""
            version_tuple = self._version_tuple(sdk_version)
            if version_tuple and version_tuple > (2, 54, 2):
                return (
                    f"Intel RealSense L515 is connected, but pyrealsense2 {sdk_version} no longer supports it. "
                    "Install the pinned SYA librealsense/pyrealsense2 2.54.2 runtime."
                )
        return (
            "Intel RealSense USB device is connected, but pyrealsense2 cannot enumerate it. "
            "Install librealsense udev rules or grant this user access to the RealSense USB/HID interfaces."
        )

    def _version_tuple(self, version):
        parts = []
        for part in str(version).split("."):
            if not part.isdigit():
                break
            parts.append(int(part))
        if not parts:
            return ()
        while len(parts) < 3:
            parts.append(0)
        return tuple(parts[:3])

    def list_devices(self):
        try:
            devices = []
            for device in self._query_devices():
                devices.append(self._device_info(device))
            with self.lock:
                self.device_cache = devices
                self.last_error = ""
            return devices
        except Exception as error:
            with self.lock:
                self.device_cache = []
                self.last_error = self._inaccessible_usb_message() or str(error)
            return []

    def _records_dir(self):
        configured = load_realsense_config().get("recordsDir")
        if isinstance(configured, str) and configured.strip():
            selected = configured.strip()
        else:
            selected = os.environ.get("MONITOR_RECORDS_DIR", DEFAULT_RECORDS_DIR)
        return os.path.abspath(os.path.expanduser(selected))

    def status(self):
        with self.lock:
            active = self.active
            cached_devices = list(self.device_cache)
            latest_attention = dict(self.latest_attention) if self.latest_attention else None
            preview_available = self.latest_frame_data is not None
            detector = self.focus_detector
        sdk_devices = cached_devices if active and cached_devices else (
            self.list_devices() if not self.import_error else []
        )
        usb_devices = [] if sdk_devices else self._usb_realsense_devices()
        error = self.last_error or self.import_error
        if not error and usb_devices:
            error = self._inaccessible_usb_message()
        focus_enabled = bool(self.stream_config.get("focusEnabled", True))
        focus_error = detector.error if detector is not None else ""
        return {
            "source": "realsense",
            "available": bool(self.rs and sdk_devices),
            "sdkLoaded": bool(self.rs),
            "sdkVersion": getattr(self.rs, "__version__", "") if self.rs is not None else "",
            "backend": "pyrealsense2",
            "privilegeRequired": bool(sys.platform == "darwin" and usb_devices and not sdk_devices),
            "active": active,
            "startedAt": self.started_at,
            "totalFrames": self.total_frames,
            "totalSamples": self.total_samples,
            "lastFrameTime": self.last_frame_time,
            "lastSamplePath": self.last_sample_path,
            "recordsDir": self._records_dir(),
            "stream": self.stream_config,
            "devices": sdk_devices or usb_devices,
            "trainingSession": self.training_session,
            "preview": {
                "enabled": bool(self.stream_config.get("previewEnabled", True)),
                "available": preview_available,
                "fps": self.stream_config.get("previewFps", 2),
                "error": self.preview_error,
            },
            "inference": {
                "enabled": focus_enabled,
                "available": bool(detector and detector.available),
                "modelVersion": detector.model_version if detector else "",
                "state": latest_attention.get("state") if latest_attention else "unknown",
                "error": focus_error,
            },
            "error": error,
        }

    def _read_stream_config(self, payload):
        config = dict(self.stream_config)
        configured_defaults = load_realsense_config()
        for key in ["width", "height", "fps", "colorWidth", "colorHeight", "colorFps", "depthWidth", "depthHeight", "depthFps", "previewFps", "jpegQuality", "focusFps"]:
            value = configured_defaults.get(key)
            if isinstance(value, int) and value > 0:
                config[key] = value
        for key in ["enableColor", "enableDepth", "previewEnabled", "focusEnabled"]:
            if isinstance(configured_defaults.get(key), bool):
                config[key] = configured_defaults[key]
        serial = configured_defaults.get("serial")
        if isinstance(serial, str) and serial.strip():
            config["serial"] = serial.strip()

        if isinstance(payload, dict):
            for key in ["width", "height", "fps", "colorWidth", "colorHeight", "colorFps", "depthWidth", "depthHeight", "depthFps", "previewFps", "jpegQuality", "focusFps"]:
                value = payload.get(key)
                if isinstance(value, int) and value > 0:
                    config[key] = value

            for key in ["enableColor", "enableDepth", "previewEnabled", "focusEnabled"]:
                if isinstance(payload.get(key), bool):
                    config[key] = payload[key]

            serial = payload.get("serial")
            if isinstance(serial, str):
                config["serial"] = serial.strip()

        if not config["enableColor"] and not config["enableDepth"]:
            raise ValueError("At least one RealSense stream must be enabled.")
        if (config.get("previewEnabled") or config.get("focusEnabled")) and not config["enableColor"]:
            raise ValueError("RealSense preview and focus inference require the color stream.")

        config["previewFps"] = min(config.get("previewFps", 2), 5)
        config["focusFps"] = min(config.get("focusFps", 2), 5)
        config["jpegQuality"] = min(config.get("jpegQuality", 72), 90)

        for prefix in ["color", "depth"]:
            if config.get(f"{prefix}Width", 0) <= 0:
                config[f"{prefix}Width"] = config["width"]
            if config.get(f"{prefix}Height", 0) <= 0:
                config[f"{prefix}Height"] = config["height"]
            if config.get(f"{prefix}Fps", 0) <= 0:
                config[f"{prefix}Fps"] = config["fps"]

        return config

    def _video_profiles(self, devices, stream_type, stream_format):
        profiles = []
        for device in devices:
            for sensor in device.query_sensors():
                for profile in sensor.get_stream_profiles():
                    try:
                        if profile.stream_type() != stream_type or profile.format() != stream_format:
                            continue
                        video_profile = profile.as_video_stream_profile()
                        profiles.append({
                            "width": video_profile.width(),
                            "height": video_profile.height(),
                            "fps": profile.fps(),
                        })
                    except Exception:
                        continue
        return profiles

    def _choose_video_profile(self, devices, label, stream_type, stream_format, width, height, fps):
        profiles = self._video_profiles(devices, stream_type, stream_format)
        if not profiles:
            raise RuntimeError(f"No RealSense {label} stream profiles are available.")

        for profile in profiles:
            if profile["width"] == width and profile["height"] == height and profile["fps"] == fps:
                return profile

        same_fps = [profile for profile in profiles if profile["fps"] == fps]
        candidates = same_fps or profiles
        return min(
            candidates,
            key=lambda profile: (
                abs(profile["width"] - width) + abs(profile["height"] - height),
                abs(profile["fps"] - fps),
                profile["width"] * profile["height"],
            ),
        )

    def _frame_number(self, frame):
        if not frame:
            return 0
        try:
            return int(frame.get_frame_number())
        except Exception:
            return 0

    def _validate_frame_bytes(self, label, data, width, height, bytes_per_pixel):
        expected = width * height * bytes_per_pixel
        if width <= 0 or height <= 0 or len(data) != expected:
            raise RuntimeError(
                f"RealSense {label} frame is malformed: expected {expected} bytes for "
                f"{width}x{height}, received {len(data)}."
            )

    def _publish_attention(self, record):
        if record:
            EVENT_BROKER.publish("attention.status", record)

    def _stream_loop(self, pipeline, stop_event):
        last_preview_at = float("-inf")
        last_focus_at = float("-inf")
        try:
            while not stop_event.is_set():
                try:
                    frames = pipeline.wait_for_frames(1000)
                except Exception as error:
                    if stop_event.is_set():
                        break
                    raise RuntimeError(f"RealSense frame acquisition failed: {error}") from error

                color_frame = frames.get_color_frame()
                depth_frame = frames.get_depth_frame()
                if not color_frame and not depth_frame:
                    continue

                now_monotonic = time.monotonic()
                timestamp_ms = int(time.time() * 1000)
                captured_at = utc_timestamp()
                color_bytes = bytes(color_frame.get_data()) if color_frame else None
                depth_bytes = bytes(depth_frame.get_data()) if depth_frame else None
                frame_number = self._frame_number(color_frame or depth_frame)
                color_width = color_frame.get_width() if color_frame else 0
                color_height = color_frame.get_height() if color_frame else 0
                depth_width = depth_frame.get_width() if depth_frame else 0
                depth_height = depth_frame.get_height() if depth_frame else 0
                if color_bytes is not None:
                    self._validate_frame_bytes("color", color_bytes, color_width, color_height, 3)
                if depth_bytes is not None:
                    self._validate_frame_bytes("depth", depth_bytes, depth_width, depth_height, 2)

                with self.lock:
                    attention = self.latest_attention
                    face_detected = (
                        CAMERA_STATE.get("faceDetected", False)
                        if self.focus_detector is not None
                        else False
                    )
                attention_changed = False
                focus_interval = 1.0 / max(1, self.stream_config.get("focusFps", 2))
                if (
                    color_bytes
                    and self.focus_detector is not None
                    and now_monotonic - last_focus_at >= focus_interval
                ):
                    attention, face_detected, attention_changed = self.focus_detector.analyze(
                        color_bytes, color_width, color_height, timestamp_ms
                    )
                    last_focus_at = now_monotonic

                jpeg_base64 = None
                preview_interval = 1.0 / max(1, self.stream_config.get("previewFps", 2))
                if (
                    color_bytes
                    and self.stream_config.get("previewEnabled", True)
                    and now_monotonic - last_preview_at >= preview_interval
                ):
                    try:
                        jpeg_base64 = encode_rgb_jpeg(
                            color_bytes,
                            color_width,
                            color_height,
                            self.stream_config.get("jpegQuality", 72),
                        )
                        self.preview_error = ""
                    except Exception as error:
                        self.preview_error = str(error)
                    last_preview_at = now_monotonic

                raw_frame = {
                    "timestamp": timestamp_ms,
                    "capturedAt": captured_at,
                    "frameNumber": frame_number,
                    "colorBytes": color_bytes,
                    "colorWidth": color_width,
                    "colorHeight": color_height,
                    "depthBytes": depth_bytes,
                    "depthWidth": depth_width,
                    "depthHeight": depth_height,
                }
                public_frame = None
                with self.frame_condition:
                    if self.pipeline is not pipeline:
                        break
                    self.total_frames += 1
                    self.last_frame_time = captured_at
                    self.latest_raw_frame = raw_frame
                    if attention is not None:
                        self.latest_attention = attention
                    if jpeg_base64 is not None:
                        public_frame = {
                            "source": "realsense",
                            "frameId": f"frame-{timestamp_ms}-{frame_number}",
                            "timestamp": timestamp_ms,
                            "capturedAt": captured_at,
                            "frameNumber": frame_number,
                            "width": color_width,
                            "height": color_height,
                            "mimeType": "image/jpeg",
                            "jpegBase64": jpeg_base64,
                            "deviceSerial": self.device_serial,
                            "depth": {
                                "available": bool(depth_frame),
                                "width": depth_width,
                                "height": depth_height,
                                "format": "z16" if depth_frame else "",
                                "depthScaleMeters": self._depth_scale() if depth_frame else None,
                            },
                            "attention": attention,
                        }
                        self.latest_frame_data = public_frame
                    CAMERA_STATE["totalFrames"] = self.total_frames
                    CAMERA_STATE["lastFrameTime"] = timestamp_ms
                    CAMERA_STATE["faceDetected"] = bool(face_detected)
                    self.frame_condition.notify_all()

                if attention_changed:
                    self._publish_attention(attention)
                if public_frame is not None:
                    EVENT_BROKER.publish("realsense.frame", public_frame)
        except Exception as error:
            error_message = str(error)
            stop_event.set()
            try:
                pipeline.stop()
            except Exception:
                pass
            with self.lock:
                self.last_error = error_message
                if self.pipeline is pipeline:
                    detector = self.focus_detector
                    self.pipeline = None
                    self.profile = None
                    self.worker = None
                    self.focus_detector = None
                    self.depth_scale = None
                    self.latest_frame_data = None
                    self.latest_raw_frame = None
                    self.active = False
                    CAMERA_STATE["isActive"] = False
                    if CAMERA_STATE.get("source") == "realsense":
                        CAMERA_STATE["source"] = "builtin"
                else:
                    detector = None
            if detector:
                try:
                    detector.close()
                except Exception:
                    pass
            record = {
                "id": f"attention-{int(time.time() * 1000)}-{uuid.uuid4().hex[:8]}",
                "timestamp": int(time.time() * 1000),
                "state": "unknown",
                "confidence": None,
                "source": "realsense-runtime",
                "reason": error_message,
                "modelVersion": "runtime-1",
            }
            with self.lock:
                self.latest_attention = record
            self._publish_attention(record)

    def latest_frame(self):
        with self.lock:
            if self.latest_frame_data is None:
                return None
            return json.loads(json.dumps(self.latest_frame_data))

    def start(self, payload=None):
        with self.lock:
            if self.active:
                return self.status()

        rs = self._load_sdk()
        config_values = self._read_stream_config(payload)
        try:
            devices = self._query_devices()
        except Exception as error:
            message = self._inaccessible_usb_message() or str(error)
            self.last_error = message
            raise RuntimeError(message) from error
        if not devices:
            message = self._inaccessible_usb_message()
            if not message:
                message = "No Intel RealSense devices detected. Connect a camera before starting the RealSense source."
            self.last_error = message
            raise RuntimeError(message)

        selected_devices = devices
        if config_values["serial"]:
            selected_devices = [
                device for device in devices if self._device_serial(device) == config_values["serial"]
            ]
            if not selected_devices:
                message = f"Intel RealSense device with serial {config_values['serial']} was not detected."
                self.last_error = message
                raise RuntimeError(message)

        pipeline = rs.pipeline()
        config = rs.config()
        detector = None

        if config_values["serial"]:
            config.enable_device(config_values["serial"])
        if config_values["enableColor"]:
            color_profile = self._choose_video_profile(
                selected_devices,
                "color",
                rs.stream.color,
                rs.format.rgb8,
                config_values["colorWidth"],
                config_values["colorHeight"],
                config_values["colorFps"],
            )
            config_values["colorWidth"] = color_profile["width"]
            config_values["colorHeight"] = color_profile["height"]
            config_values["colorFps"] = color_profile["fps"]
            config.enable_stream(
                rs.stream.color,
                config_values["colorWidth"],
                config_values["colorHeight"],
                rs.format.rgb8,
                config_values["colorFps"],
            )
        if config_values["enableDepth"]:
            depth_profile = self._choose_video_profile(
                selected_devices,
                "depth",
                rs.stream.depth,
                rs.format.z16,
                config_values["depthWidth"],
                config_values["depthHeight"],
                config_values["depthFps"],
            )
            config_values["depthWidth"] = depth_profile["width"]
            config_values["depthHeight"] = depth_profile["height"]
            config_values["depthFps"] = depth_profile["fps"]
            config.enable_stream(
                rs.stream.depth,
                config_values["depthWidth"],
                config_values["depthHeight"],
                rs.format.z16,
                config_values["depthFps"],
            )

        try:
            profile = pipeline.start(config)
            device = profile.get_device()
            device_info = self._device_info(device)
            depth_scale = None
            if config_values["enableDepth"]:
                try:
                    depth_scale = device.first_depth_sensor().get_depth_scale()
                except Exception:
                    depth_scale = None
            detector = FocusEstimator() if config_values.get("focusEnabled", True) else None
            stop_event = threading.Event()
            worker = threading.Thread(
                target=self._stream_loop,
                args=(pipeline, stop_event),
                name="sya-realsense-stream",
                daemon=True,
            )
            with self.lock:
                self.profile = profile
                self.pipeline = pipeline
                self.active = True
                self.started_at = utc_timestamp()
                self.stream_config = config_values
                self.device_cache = [device_info]
                self.device_serial = device_info.get("serialNumber", "")
                self.depth_scale = depth_scale
                self.latest_frame_data = None
                self.latest_raw_frame = None
                self.latest_attention = None
                self.focus_detector = detector
                self.stop_event = stop_event
                self.worker = worker
                self.last_error = ""
                self.preview_error = ""
                CAMERA_STATE["source"] = "realsense"
                CAMERA_STATE["isActive"] = True
                CAMERA_STATE["windowCreated"] = False
                CAMERA_STATE["faceDetected"] = False
                CAMERA_STATE["storagePath"] = self._records_dir()
            worker.start()
            return self.status()
        except Exception as error:
            try:
                pipeline.stop()
            except Exception:
                pass
            if detector:
                try:
                    detector.close()
                except Exception:
                    pass
            with self.lock:
                self.pipeline = None
                self.profile = None
                self.worker = None
                self.focus_detector = None
                self.depth_scale = None
                self.active = False
                self.last_error = self._inaccessible_usb_message() or str(error)
            raise RuntimeError(self.last_error) from error

    def stop(self, _payload=None):
        with self.lock:
            pipeline = self.pipeline
            worker = self.worker
            detector = self.focus_detector
            was_active = self.active or pipeline is not None
            self.stop_event.set()

        if pipeline:
            try:
                pipeline.stop()
            except Exception as error:
                if not self.last_error:
                    self.last_error = str(error)
        if worker and worker is not threading.current_thread():
            worker.join(timeout=6)
            if worker.is_alive():
                with self.lock:
                    self.last_error = "Timed out while stopping the RealSense acquisition worker."
                return self.status()
        if detector:
            try:
                detector.close()
            except Exception as error:
                if not self.last_error:
                    self.last_error = f"Failed to release focus inference: {error}"

        with self.lock:
            self.pipeline = None
            self.profile = None
            self.worker = None
            self.focus_detector = None
            self.depth_scale = None
            self.latest_frame_data = None
            self.latest_raw_frame = None
            self.active = False
            CAMERA_STATE["isActive"] = False
            if CAMERA_STATE.get("source") == "realsense":
                CAMERA_STATE["source"] = "builtin"

        if was_active:
            timestamp_ms = int(time.time() * 1000)
            record = {
                "id": f"attention-{timestamp_ms}-{uuid.uuid4().hex[:8]}",
                "timestamp": timestamp_ms,
                "state": "unknown",
                "confidence": None,
                "source": "realsense-runtime",
                "reason": "RealSense monitoring stopped.",
                "modelVersion": "runtime-1",
            }
            with self.lock:
                self.latest_attention = record
            self._publish_attention(record)
        return self.status()

    def _depth_scale(self):
        return self.depth_scale

    def _sample_dir(self, payload):
        if isinstance(payload, dict) and isinstance(payload.get("sessionId"), str) and payload["sessionId"].strip():
            session_id = payload["sessionId"].strip()
        elif self.training_session and self.training_session.get("sessionId"):
            session_id = self.training_session["sessionId"]
        else:
            session_id = "inference"

        if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._-]{0,127}", session_id) or session_id in {".", ".."}:
            raise ValueError("RealSense sessionId contains unsupported characters.")

        sample_id = "{}-{}".format(
            time.strftime("%Y%m%dT%H%M%SZ", time.gmtime()),
            uuid.uuid4().hex[:8],
        )
        records_root = os.path.realpath(os.path.join(self._records_dir(), "realsense"))
        sample_dir = os.path.realpath(os.path.join(records_root, session_id, sample_id))
        if os.path.commonpath([records_root, sample_dir]) != records_root:
            raise ValueError("RealSense sample path must stay inside the configured records directory.")
        return sample_id, sample_dir

    def _write_binary_atomic(self, path, data):
        temp_path = f"{path}.tmp-{uuid.uuid4().hex[:8]}"
        descriptor = os.open(temp_path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        try:
            with os.fdopen(descriptor, "wb") as output:
                output.write(data)
            os.replace(temp_path, path)
        except Exception:
            try:
                os.unlink(temp_path)
            except OSError:
                pass
            raise

    def _write_color_ppm(self, path, width, height, data):
        body = "P6\n{} {}\n255\n".format(width, height).encode("ascii") + data
        self._write_binary_atomic(path, body)
        return {
            "width": width,
            "height": height,
            "format": "rgb8",
            "mimeType": "image/x-portable-pixmap",
            "path": path,
            "bytes": len(data),
        }

    def _write_depth_pgm(self, path, width, height, raw_data):
        values = array.array("H")
        values.frombytes(raw_data)
        if sys.byteorder == "little":
            values.byteswap()
        data = values.tobytes()
        body = "P5\n{} {}\n65535\n".format(width, height).encode("ascii") + data
        self._write_binary_atomic(path, body)
        return {
            "width": width,
            "height": height,
            "format": "z16",
            "mimeType": "image/x-portable-graymap",
            "path": path,
            "bytes": len(data),
            "depthScaleMeters": self._depth_scale(),
        }

    def capture(self, payload=None):
        payload = payload if isinstance(payload, dict) else {}
        start_if_needed = payload.get("startIfNeeded", True) is not False
        stop_after_capture = False
        if not self.active:
            if not start_if_needed:
                raise RuntimeError("RealSense stream is not active.")
            self.start(payload)
            stop_after_capture = payload.get("keepRunning", False) is not True

        timeout_ms = payload.get("timeoutMs", 5000)
        if not isinstance(timeout_ms, int) or timeout_ms <= 0:
            timeout_ms = 5000

        try:
            with self.frame_condition:
                ready = self.frame_condition.wait_for(
                    lambda: self.latest_raw_frame is not None or not self.active,
                    timeout=timeout_ms / 1000,
                )
                if not ready or self.latest_raw_frame is None:
                    raise RuntimeError("RealSense did not return color or depth frames before the capture timeout.")
                raw_frame = dict(self.latest_raw_frame)
                attention = dict(self.latest_attention) if self.latest_attention else None

            sample_id, sample_dir = self._sample_dir(payload)
            os.makedirs(sample_dir, mode=0o700, exist_ok=True)
            captured_at = raw_frame["capturedAt"]
            result = {
                "source": "realsense",
                "sampleId": sample_id,
                "capturedAt": captured_at,
                "frameNumber": raw_frame["frameNumber"],
                "path": sample_dir,
                "label": payload.get("label") or (self.training_session or {}).get("label", ""),
                "color": None,
                "depth": None,
                "attention": attention,
            }

            if raw_frame["colorBytes"]:
                result["color"] = self._write_color_ppm(
                    os.path.join(sample_dir, "color.ppm"),
                    raw_frame["colorWidth"],
                    raw_frame["colorHeight"],
                    raw_frame["colorBytes"],
                )
            if raw_frame["depthBytes"]:
                result["depth"] = self._write_depth_pgm(
                    os.path.join(sample_dir, "depth.pgm"),
                    raw_frame["depthWidth"],
                    raw_frame["depthHeight"],
                    raw_frame["depthBytes"],
                )

            metadata_path = os.path.join(sample_dir, "metadata.json")
            metadata_body = (json.dumps(result, indent=2) + "\n").encode("utf-8")
            self._write_binary_atomic(metadata_path, metadata_body)
            result["metadataPath"] = metadata_path

            self.total_samples += 1
            self.last_sample_path = sample_dir
            CAMERA_STATE["storagePath"] = self._records_dir()
            if self.training_session or payload.get("purpose") == "training":
                EVENT_BROKER.publish("realsense.training.sample", result)
            return result
        finally:
            if stop_after_capture:
                self.stop()

    def training_start(self, payload=None):
        payload = payload if isinstance(payload, dict) else {}
        session_id = payload.get("sessionId")
        if not isinstance(session_id, str) or not session_id.strip():
            session_id = "train-{}-{}".format(
                time.strftime("%Y%m%dT%H%M%SZ", time.gmtime()),
                uuid.uuid4().hex[:8],
            )
        session_id = session_id.strip()
        if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._-]{0,127}", session_id) or session_id in {".", ".."}:
            raise ValueError("RealSense sessionId contains unsupported characters.")
        records_root = os.path.realpath(os.path.join(self._records_dir(), "realsense"))
        session_path = os.path.realpath(os.path.join(records_root, session_id))
        if os.path.commonpath([records_root, session_path]) != records_root:
            raise ValueError("RealSense training path must stay inside the configured records directory.")

        self.training_session = {
            "sessionId": session_id,
            "label": payload.get("label") if isinstance(payload.get("label"), str) else "",
            "taskId": payload.get("taskId") if isinstance(payload.get("taskId"), str) else "",
            "notes": payload.get("notes") if isinstance(payload.get("notes"), str) else "",
            "startedAt": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
            "path": session_path,
        }
        os.makedirs(self.training_session["path"], mode=0o700, exist_ok=True)
        if payload.get("autoStart", True) is not False:
            self.start(payload)
        return self.status()

    def training_stop(self, payload=None):
        session = self.training_session
        self.training_session = None
        if isinstance(payload, dict) and payload.get("stopCamera") is True:
            self.stop()
        return {
            "stoppedAt": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
            "session": session,
            "status": self.status(),
        }


REALSENSE = RealSenseAdapter()


def resolve_port():
    if len(sys.argv) > 1:
        try:
            return int(sys.argv[1])
        except ValueError:
            pass

    env_port = os.environ.get("PORT")
    if env_port:
        try:
            return int(env_port)
        except ValueError:
            pass

    return DEFAULT_PORT


def load_manifest():
    for manifest_path in MANIFEST_PATHS:
        if os.path.exists(manifest_path):
            with open(manifest_path, "r", encoding="utf-8") as manifest_file:
                return json.load(manifest_file)

    return {
        "id": "monitor",
        "version": "0.0.0",
        "runtime": "local-http",
        "permissions": ["screen", "camera", "storage"],
    }


def response(data):
    return {"ok": True, "data": data}


def error_response(code, message, details=None):
    error = {"code": code, "message": message}
    if details is not None:
        error["details"] = details
    return {"ok": False, "error": error}


def _config_path_candidates():
    configured_path = os.environ.get("MONITOR_CONFIG_PATH")
    if configured_path:
        yield configured_path
    yield USER_CONFIG_PATH
    yield DEFAULT_CONFIG_PATH
    yield FALLBACK_CONFIG_PATH


def _writable_config_path():
    configured_path = os.environ.get("MONITOR_CONFIG_PATH")
    return configured_path if configured_path else USER_CONFIG_PATH


def _deep_merge(base, update):
    if not isinstance(base, dict):
        base = {}
    if not isinstance(update, dict):
        return base
    merged = dict(base)
    for key, value in update.items():
        if isinstance(value, dict) and isinstance(merged.get(key), dict):
            merged[key] = _deep_merge(merged[key], value)
        else:
            merged[key] = value
    return merged


def _mask_sensitive_config(config):
    safe_config = json.loads(json.dumps(config)) if isinstance(config, dict) else {}
    api = safe_config.get("api")
    if isinstance(api, dict) and api.get("key"):
        api["key"] = "********"
    return safe_config


def _empty_api_config():
    return {
        "path": "",
        "key": "",
        "model": "gpt-4-turbo",
        "config_path": "",
    }


def load_api_config():
    for config_path in _config_path_candidates():
        if not config_path or not os.path.exists(config_path):
            continue

        with open(config_path, "r", encoding="utf-8") as config_file:
            data = json.load(config_file)

        api = data.get("api", data)
        return {
            "path": api.get("path") or api.get("url") or api.get("api_path") or "",
            "key": api.get("key") or api.get("api_key") or "",
            "model": api.get("model") or "gpt-4-turbo",
            "config_path": config_path,
        }

    return _empty_api_config()


def load_monitor_config():
    for config_path in _config_path_candidates():
        if not config_path or not os.path.exists(config_path):
            continue
        with open(config_path, "r", encoding="utf-8") as config_file:
            return json.load(config_file)
    return {}


def settings_status():
    config = load_monitor_config()
    readable_path = ""
    for config_path in _config_path_candidates():
        if config_path and os.path.exists(config_path):
            readable_path = config_path
            break
    writable_path = _writable_config_path()
    return {
        "configured": bool(config),
        "configPath": readable_path or writable_path,
        "writablePath": writable_path,
        "config": _mask_sensitive_config(config),
        "realsense": load_realsense_config(),
    }


def settings_save(payload=None):
    if not isinstance(payload, dict):
        raise ValueError("settings.save expects a JSON object payload.")
    config_path = _writable_config_path()
    os.makedirs(os.path.dirname(config_path), exist_ok=True)
    next_config = _deep_merge(load_monitor_config(), payload)
    tmp_path = f"{config_path}.tmp"
    with open(tmp_path, "w", encoding="utf-8") as config_file:
        json.dump(next_config, config_file, indent=2, sort_keys=True)
        config_file.write("\n")
    os.replace(tmp_path, config_path)
    return settings_status()


def load_realsense_config():
    config = load_monitor_config()
    camera_config = config.get("camera", {}) if isinstance(config, dict) else {}
    realsense_config = camera_config.get("realsense", {}) if isinstance(camera_config, dict) else {}
    return realsense_config if isinstance(realsense_config, dict) else {}


def config_status():
    api_config = load_api_config()
    return {
        "configured": bool(api_config["path"] and api_config["key"]),
        "apiPath": api_config["path"],
        "model": api_config["model"],
        "configFile": os.path.basename(api_config["config_path"]) if api_config["config_path"] else "",
    }


def capabilities():
    manifest = load_manifest()
    return {
        "id": manifest.get("id", "monitor"),
        "version": manifest.get("version", "0.0.0"),
        "capabilities": manifest.get("capabilities", ["screen-monitoring", "camera-monitoring", "record-storage", "attention-config"]),
        "events": manifest.get("events", ["screen.capture", "camera.frame", "screen.status", "camera.status"]),
        "permissions": manifest.get("permissions", ["screen", "camera", "storage"]),
        "actions": ACTION_DEFINITIONS,
    }


def storage_stats():
    return {
        "totalRecords": 0,
        "screenshotsDir": "",
        "cameraDir": REALSENSE._records_dir(),
        "logsDir": "",
        "diskUsageMB": 0,
        "realsenseDir": os.path.join(REALSENSE._records_dir(), "realsense"),
    }


def parse_positive_int(value, fallback=None):
    if value in (None, ""):
        return fallback
    try:
        parsed = int(value)
    except ValueError:
        return fallback
    return parsed if parsed > 0 else fallback


def list_records(_limit=None):
    return []


def screen_start(payload=None):
    rate_ms = None
    if isinstance(payload, dict):
        rate_ms = payload.get("rateMs")
    if isinstance(rate_ms, (int, float)) and rate_ms > 0:
        SCREEN_STATE["captureRateMs"] = rate_ms
    SCREEN_STATE["isMonitoring"] = True
    return SCREEN_STATE


def screen_stop(_payload=None):
    SCREEN_STATE["isMonitoring"] = False
    return SCREEN_STATE


def camera_start(_payload=None):
    payload = _payload if isinstance(_payload, dict) else {}
    if payload.get("source") == "realsense" or payload.get("dataSource") == "realsense":
        return REALSENSE.start(payload)
    CAMERA_STATE["source"] = "builtin"
    CAMERA_STATE["isActive"] = True
    CAMERA_STATE["windowCreated"] = True
    return CAMERA_STATE


def camera_stop(_payload=None):
    payload = _payload if isinstance(_payload, dict) else {}
    if payload.get("source") == "realsense" or CAMERA_STATE.get("source") == "realsense":
        return REALSENSE.stop(payload)
    CAMERA_STATE["isActive"] = False
    return CAMERA_STATE


def camera_status():
    status = dict(CAMERA_STATE)
    status["realsense"] = REALSENSE.status()
    return status


def camera_sources():
    realsense_status = REALSENSE.status()
    return [
        {
            "id": "builtin",
            "label": "Built-in camera",
            "available": True,
            "active": CAMERA_STATE.get("source") == "builtin" and CAMERA_STATE.get("isActive"),
            "modes": ["inference"],
        },
        {
            "id": "realsense",
            "label": "Intel RealSense RGB-D camera",
            "available": bool(realsense_status["available"]),
            "active": bool(realsense_status["active"]),
            "modes": ["inference", "training"],
            "devices": realsense_status["devices"],
            "error": realsense_status["error"],
        },
    ]


def camera_show(_payload=None):
    CAMERA_STATE["windowCreated"] = True
    return CAMERA_STATE


def camera_hide(_payload=None):
    CAMERA_STATE["windowCreated"] = False
    return CAMERA_STATE


def dispatch_action(action, payload=None, query=None):
    query = query or {}
    handlers = {
        "health": lambda: {"status": "ok", "functionId": "monitor", "version": load_manifest().get("version", "0.0.0")},
        "manifest": load_manifest,
        "actions.list": lambda: ACTION_DEFINITIONS,
        "capabilities.list": capabilities,
        "screen.status": lambda: SCREEN_STATE,
        "screen.start": lambda: screen_start(payload),
        "screen.stop": lambda: screen_stop(payload),
        "camera.status": camera_status,
        "camera.start": lambda: camera_start(payload),
        "camera.stop": lambda: camera_stop(payload),
        "camera.show": lambda: camera_show(payload),
        "camera.hide": lambda: camera_hide(payload),
        "camera.sources": camera_sources,
        "realsense.status": REALSENSE.status,
        "realsense.devices": REALSENSE.list_devices,
        "realsense.start": lambda: REALSENSE.start(payload),
        "realsense.stop": lambda: REALSENSE.stop(payload),
        "realsense.capture": lambda: REALSENSE.capture(payload),
        "realsense.frame.latest": REALSENSE.latest_frame,
        "realsense.training.start": lambda: REALSENSE.training_start(payload),
        "realsense.training.stop": lambda: REALSENSE.training_stop(payload),
        "storage.stats": storage_stats,
        "storage.screenRecords": lambda: list_records(parse_positive_int(query.get("limit"))),
        "storage.cameraRecords": lambda: list_records(parse_positive_int(query.get("limit"))),
        "attention.config.status": config_status,
        "settings.status": settings_status,
        "settings.save": lambda: settings_save(payload),
    }

    handler = handlers.get(action)
    if handler is None:
        raise KeyError(action)
    return handler()


class ProxyHandler(http.server.SimpleHTTPRequestHandler):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, directory=STATIC_DIR, **kwargs)

    def do_GET(self):
        parsed = urllib.parse.urlparse(self.path)
        path = parsed.path
        query = dict(urllib.parse.parse_qsl(parsed.query))

        routes = {
            "/health": "health",
            "/manifest": "manifest",
            "/api/monitor/actions": "actions.list",
            "/api/monitor/capabilities": "capabilities.list",
            "/api/monitor/screen/status": "screen.status",
            "/api/monitor/camera/status": "camera.status",
            "/api/monitor/camera/sources": "camera.sources",
            "/api/monitor/realsense/status": "realsense.status",
            "/api/monitor/realsense/devices": "realsense.devices",
            "/api/monitor/realsense/frame/latest": "realsense.frame.latest",
            "/api/monitor/storage/stats": "storage.stats",
            "/api/monitor/storage/screen-records": "storage.screenRecords",
            "/api/monitor/storage/camera-records": "storage.cameraRecords",
            "/api/monitor/attention/config/status": "attention.config.status",
            "/api/monitor/settings/status": "settings.status",
        }

        if path in routes:
            self._send_action(routes[path], query=query)
        elif path == "/api/config":
            self._send_config_status()
        elif path == "/api/monitor/events":
            self._send_event_stream()
        else:
            super().do_GET()

    def do_POST(self):
        parsed = urllib.parse.urlparse(self.path)
        path = parsed.path
        payload = self._read_json_body()

        routes = {
            "/api/monitor/screen/start": "screen.start",
            "/api/monitor/screen/stop": "screen.stop",
            "/api/monitor/camera/start": "camera.start",
            "/api/monitor/camera/stop": "camera.stop",
            "/api/monitor/camera/show": "camera.show",
            "/api/monitor/camera/hide": "camera.hide",
            "/api/monitor/realsense/start": "realsense.start",
            "/api/monitor/realsense/stop": "realsense.stop",
            "/api/monitor/realsense/capture": "realsense.capture",
            "/api/monitor/realsense/training/start": "realsense.training.start",
            "/api/monitor/realsense/training/stop": "realsense.training.stop",
            "/api/monitor/settings/save": "settings.save",
        }

        if path == "/api/llm":
            self._proxy_llm(payload)
        elif path == "/api/monitor/invoke":
            self._invoke(payload)
        elif path in routes:
            self._send_action(routes[path], payload=payload)
        else:
            self.send_error(404)

    def _read_json_body(self):
        content_length = int(self.headers.get("Content-Length", 0))
        if content_length <= 0:
            return {}

        body = self.rfile.read(content_length)
        try:
            return json.loads(body)
        except json.JSONDecodeError:
            self.send_error(400, "Invalid JSON")
            return {}

    def _send_json(self, status_code, payload):
        self.send_response(status_code)
        self.send_header("Content-Type", "application/json")
        self.end_headers()
        self.wfile.write(json.dumps(payload).encode())

    def _send_action(self, action, payload=None, query=None):
        try:
            self._send_json(200, response(dispatch_action(action, payload=payload, query=query)))
        except KeyError:
            self._send_json(404, error_response("UNKNOWN_ACTION", f"Unknown monitor action: {action}"))
        except Exception as error:
            self._send_json(500, error_response("MONITOR_ACTION_ERROR", str(error)))

    def _invoke(self, payload):
        if not isinstance(payload, dict) or not isinstance(payload.get("action"), str):
            self._send_json(400, error_response("INVALID_PAYLOAD", "Payload action must be a string."))
            return
        action = payload["action"]
        if action == "invoke":
            self._send_json(400, error_response("INVALID_PAYLOAD", "Nested invoke is not supported."))
            return
        self._send_action(action, payload=payload.get("payload"))

    def _send_config_status(self):
        self._send_json(200, config_status())

    def _send_event_stream(self):
        subscriber = EVENT_BROKER.subscribe()
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream")
        self.send_header("Cache-Control", "no-cache")
        self.send_header("Connection", "keep-alive")
        self.end_headers()
        ready = json.dumps({"functionId": "monitor", "event": "ready", "payload": {}})
        try:
            self.wfile.write(f"event: ready\ndata: {ready}\n\n".encode())
            self.wfile.flush()
            while True:
                try:
                    message = subscriber.get(timeout=15)
                except queue.Empty:
                    self.wfile.write(b": keep-alive\n\n")
                    self.wfile.flush()
                    continue
                event_name = message["event"]
                event_body = json.dumps(message, separators=(",", ":"))
                self.wfile.write(f"event: {event_name}\ndata: {event_body}\n\n".encode())
                self.wfile.flush()
        except (BrokenPipeError, ConnectionResetError, OSError):
            pass
        finally:
            EVENT_BROKER.unsubscribe(subscriber)

    def _proxy_llm(self, req_data):
        if not isinstance(req_data, dict):
            self.send_error(400, "Invalid JSON")
            return

        api_config = load_api_config()
        api_url = api_config["path"]
        api_key = api_config["key"]

        if not api_url or not api_key:
            self._send_json(500, {
                "error": "Missing API config. Copy config.example.json to config.local.json and set api.path and api.key."
            })
            return

        if api_config["model"] and not req_data.get("model"):
            req_data["model"] = api_config["model"]

        headers = {
            "Content-Type": "application/json",
            "Authorization": f"Bearer {api_key}",
        }

        req = urllib.request.Request(
            api_url,
            data=json.dumps(req_data).encode(),
            headers=headers,
            method="POST",
        )

        try:
            with urllib.request.urlopen(req, timeout=60) as resp:
                resp_body = resp.read()
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.end_headers()
                self.wfile.write(resp_body)
        except urllib.error.HTTPError as e:
            err_body = e.read().decode("utf-8", errors="replace")
            self._send_json(e.code, {"error": err_body})
        except Exception as e:
            self._send_json(502, {"error": str(e)})

    def do_OPTIONS(self):
        self.send_response(204)
        self.send_header("Access-Control-Allow-Methods", "POST, GET, OPTIONS")
        self.send_header("Access-Control-Allow-Headers", "Content-Type, Authorization")
        self.end_headers()


if __name__ == "__main__":
    port = resolve_port()
    api_config = load_api_config()
    with http.server.ThreadingHTTPServer(("127.0.0.1", port), ProxyHandler) as httpd:
        httpd.daemon_threads = True
        print(f"Serving on http://127.0.0.1:{port}")
        print(f"Static files from: {STATIC_DIR}")
        print("Health at: GET /health")
        print("LLM proxy at: POST /api/llm")
        if api_config["path"] and api_config["key"]:
            print(f"API config: {api_config['config_path']}")
            print(f"API path: {api_config['path']}")
        else:
            print("API config: missing; copy config.example.json to config.local.json")
        try:
            httpd.serve_forever()
        finally:
            REALSENSE.stop()
