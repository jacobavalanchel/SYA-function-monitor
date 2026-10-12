#!/usr/bin/env python3
"""Standalone mock server for the SYA Monitor function API.

No screen capture or camera hardware required.  All data is drawn from
the companion data.json file and mutated in-memory so that POST actions
(screen.start, camera.show, etc.) are reflected in subsequent GET calls.

Port: 8770 (or PORT env) — deliberately different from the real server (8765).
"""

from __future__ import annotations

import http.server
import hmac
import json
import os
import queue
import socketserver
import sys
import threading
import time
import urllib.parse
import uuid

# ---------------------------------------------------------------------------
# Paths
# ---------------------------------------------------------------------------
MOCK_DIR = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.dirname(MOCK_DIR))
from realsense_runtime import ACCESS_COVERAGE, EventBroker, critical_event_id, attention_observation, realsense_access_event

DATA_PATH = os.path.join(MOCK_DIR, "data.json")
MANIFEST_PATH = os.path.join(os.path.dirname(MOCK_DIR), "manifest.json")

DEFAULT_PORT = 8770
RUNTIME_TOKEN = os.environ.get("FUNCTION_RUNTIME_TOKEN", "").strip()
RUNTIME_PROOF_KEY = os.environ.get("FUNCTION_RUNTIME_PROOF_KEY", "")
MOCK_EXTRA_EVENTS = ["attention.status", "attention.config.status"]

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _resolve_port() -> int:
    """PORT env var > argv[1] > DEFAULT_PORT."""
    env_port = os.environ.get("PORT")
    if env_port:
        try:
            return int(env_port)
        except ValueError:
            pass
    if len(sys.argv) > 1:
        try:
            return int(sys.argv[1])
        except ValueError:
            pass
    return DEFAULT_PORT


def _load_json(path: str) -> dict:
    with open(path, "r", encoding="utf-8") as fh:
        return json.load(fh)


def _load_data() -> dict:
    return _load_json(DATA_PATH)


def _load_manifest() -> dict:
    if os.path.exists(MANIFEST_PATH):
        manifest = _load_json(MANIFEST_PATH)
    else:
        manifest = {
            "id": "monitor",
            "version": "0.1.3",
            "runtime": "local-http",
            "capabilities": [
                "screen-monitoring",
                "camera-monitoring",
                "record-storage",
                "attention-config",
                "action-discovery",
                "generic-invoke",
            ],
            "events": ["screen.capture", "camera.frame", "screen.status", "camera.status"],
            "permissions": ["screen", "camera", "storage"],
        }

    events = list(manifest.get("events", []))
    for event in MOCK_EXTRA_EVENTS:
        if event not in events:
            events.append(event)
    manifest["events"] = events
    return manifest


def _ok(data) -> dict:
    return {"ok": True, "data": data}


def _err(code: str, message: str, details=None) -> dict:
    error: dict = {"code": code, "message": message}
    if details is not None:
        error["details"] = details
    return {"ok": False, "error": error}


# ---------------------------------------------------------------------------
# Mutable state — seeded from data.json on startup
# ---------------------------------------------------------------------------
_INITIAL = _load_data()

SCREEN = dict(_INITIAL["screen"])
CAMERA = dict(_INITIAL["camera"])
STORAGE = dict(_INITIAL["storage"])
SCREEN_RECORDS = list(_INITIAL["screenRecords"])
CAMERA_RECORDS = list(_INITIAL["cameraRecords"])
ATTENTION = dict(_INITIAL["attentionConfig"])
_ATTENTION_SESSION = str(uuid.uuid4())
_ATTENTION_RECORD = None
_ATTENTION_STOPPED = False

# Incrementing counters to make mock data feel alive
_SCREEN_COUNTER = len(SCREEN_RECORDS) + 1
_CAMERA_COUNTER = len(CAMERA_RECORDS) + 1

_LOCK = threading.RLock()
REALSENSE = {"source": "realsense", "active": False, "available": True,
             "sessionId": str(uuid.uuid4()), "accessCoverage": ACCESS_COVERAGE,
             "startedAt": None, "totalFrames": 0, "totalSamples": 0,
             "lastFrameTime": None, "lastSamplePath": "", "error": "", "trainingSession": None,
             "devices": [], "preview": {"enabled": False, "available": False, "fps": 0, "error": ""},
             "inference": {"enabled": False, "available": False, "state": "unknown", "error": ""}}


def _now_iso() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%S.000Z", time.gmtime())


# ---------------------------------------------------------------------------
# Action definitions (mirrors the real server)
# ---------------------------------------------------------------------------
ACTION_DEFINITIONS = [
    {"action": "health", "method": "GET", "path": "/health", "description": "Check monitor runtime health."},
    {"action": "manifest", "method": "GET", "path": "/manifest", "description": "Read monitor runtime manifest."},
    {"action": "actions.list", "method": "GET", "path": "/api/monitor/actions", "description": "List callable monitor actions."},
    {"action": "capabilities.list", "method": "GET", "path": "/api/monitor/capabilities", "description": "List monitor capabilities, events, permissions, and actions."},
    {"action": "invoke", "method": "POST", "path": "/api/monitor/invoke", "description": "Invoke a monitor action by action name."},
    {"action": "screen.status", "method": "GET", "path": "/api/monitor/screen/status", "description": "Read screen monitor status."},
    {"action": "screen.start", "method": "POST", "path": "/api/monitor/screen/start", "description": "Start screen monitoring."},
    {"action": "screen.stop", "method": "POST", "path": "/api/monitor/screen/stop", "description": "Stop screen monitoring."},
    {"action": "camera.status", "method": "GET", "path": "/api/monitor/camera/status", "description": "Read camera monitor status."},
    {"action": "camera.start", "method": "POST", "path": "/api/monitor/camera/start", "description": "Start camera monitoring."},
    {"action": "camera.stop", "method": "POST", "path": "/api/monitor/camera/stop", "description": "Stop camera monitoring."},
    {"action": "camera.show", "method": "POST", "path": "/api/monitor/camera/show", "description": "Show the camera monitor window."},
    {"action": "camera.hide", "method": "POST", "path": "/api/monitor/camera/hide", "description": "Hide the camera monitor window."},
    {"action": "storage.stats", "method": "GET", "path": "/api/monitor/storage/stats", "description": "Read monitor storage statistics."},
    {"action": "storage.screenRecords", "method": "GET", "path": "/api/monitor/storage/screen-records", "description": "List stored screen records."},
    {"action": "storage.cameraRecords", "method": "GET", "path": "/api/monitor/storage/camera-records", "description": "List stored camera records."},
    {"action": "attention.config.status", "method": "GET", "path": "/api/monitor/attention/config/status", "description": "Read attention LLM config status."},
    {"action": "attention.status.set", "method": "POST", "path": "/api/monitor/mock/attention/status", "description": "Mock-only: update and emit attention status."},
]


# ---------------------------------------------------------------------------
# Action handlers — each returns the data portion (not the envelope)
# ---------------------------------------------------------------------------

def _h_health():
    manifest = _load_manifest()
    return {"status": "ok", "functionId": "monitor", "version": manifest.get("version", "0.1.3")}


def _h_manifest():
    return _load_manifest()


def _h_actions_list():
    return ACTION_DEFINITIONS


def _h_capabilities():
    manifest = _load_manifest()
    return {
        "id": manifest.get("id", "monitor"),
        "version": manifest.get("version", "0.1.3"),
        "capabilities": manifest.get("capabilities", []),
        "events": manifest.get("events", []),
        "permissions": manifest.get("permissions", []),
        "actions": ACTION_DEFINITIONS,
    }


# -- Screen ---------------------------------------------------------------

def _h_screen_status():
    with _LOCK:
        return dict(SCREEN)


def _h_screen_start(payload: dict | None):
    with _LOCK:
        if isinstance(payload, dict) and isinstance(payload.get("rateMs"), (int, float)) and payload["rateMs"] > 0:
            SCREEN["captureRateMs"] = int(payload["rateMs"])
        SCREEN["isMonitoring"] = True
        SCREEN["lastCaptureTime"] = _now_iso()
        status = dict(SCREEN)
    _broadcast_event("screen.status", status)
    return status


def _h_screen_stop(_payload=None):
    with _LOCK:
        SCREEN["isMonitoring"] = False
        status = dict(SCREEN)
    _broadcast_event("screen.status", status)
    return status


# -- Camera ---------------------------------------------------------------

def _h_camera_status():
    with _LOCK:
        return dict(CAMERA)


def _h_camera_start(_payload=None):
    global _ATTENTION_SESSION, _ATTENTION_RECORD, _ATTENTION_STOPPED
    if isinstance(_payload, dict) and (_payload.get("source") == "realsense" or _payload.get("dataSource") == "realsense"):
        _h_realsense_start(_payload)
        return _h_camera_status()
    with _LOCK:
        if not CAMERA.get("isActive"):
            _ATTENTION_SESSION = str(uuid.uuid4())
            _ATTENTION_RECORD = None
        _ATTENTION_STOPPED = False
        CAMERA["isActive"] = True
        CAMERA["windowCreated"] = True
        CAMERA["lastFrameTime"] = _now_iso()
        status = dict(CAMERA)
    _broadcast_event("camera.status", status)
    return status


def _h_camera_stop(_payload=None):
    global _ATTENTION_RECORD, _ATTENTION_STOPPED
    if CAMERA.get("source") == "realsense" or (isinstance(_payload, dict) and _payload.get("source") == "realsense"):
        _h_realsense_stop(_payload)
        return _h_camera_status()
    with _LOCK:
        if not _ATTENTION_STOPPED:
            _ATTENTION_RECORD = attention_observation(
                {"state": "unknown", "source": "mock-api", "confidence": None,
                 "reason": "Mock camera stopped.", "modelVersion": "mock-1"},
                _ATTENTION_RECORD, _ATTENTION_SESSION, 500, int(time.time() * 1000), "stopped",
            )
            ATTENTION.update(_ATTENTION_RECORD, attentionStatus="unknown")
            _broadcast_event("attention.status", dict(ATTENTION))
        _ATTENTION_STOPPED = True
        CAMERA["isActive"] = False
        status = dict(CAMERA)
    _broadcast_event("camera.status", status)
    return status


def _h_camera_show(_payload=None):
    with _LOCK:
        CAMERA["windowCreated"] = True
        status = dict(CAMERA)
    _broadcast_event("camera.status", status)
    return status


def _h_camera_hide(_payload=None):
    with _LOCK:
        CAMERA["windowCreated"] = False
        status = dict(CAMERA)
    _broadcast_event("camera.status", status)
    return status


# -- Storage --------------------------------------------------------------

def _h_realsense_status():
    with _LOCK:
        return json.loads(json.dumps(REALSENSE))


def _h_realsense_start(payload=None):
    with _LOCK:
        if not REALSENSE["active"]:
            REALSENSE.update(active=True, sessionId=str(uuid.uuid4()), startedAt=_now_iso(), error="")
        CAMERA.update(source="realsense", isActive=True)
        return _h_realsense_status()


def _h_realsense_end(code="REALSENSE_STOPPED"):
    with _LOCK:
        was_active = REALSENSE["active"]
        REALSENSE["active"] = code == "REALSENSE_STOP_FAILED"
        CAMERA.update(source="realsense" if REALSENSE["active"] else "builtin", isActive=REALSENSE["active"])
        if was_active:
            operation = "session-stop" if code in ("REALSENSE_STOPPED", "REALSENSE_STOP_FAILED") else "stream-error"
            _broadcast_event("realsense.access", realsense_access_event(REALSENSE["sessionId"], operation, code, REALSENSE["active"]))
        return _h_realsense_status()


def _h_realsense_stop(_payload=None):
    return _h_realsense_end()


def _h_realsense_capture(payload=None):
    payload = payload if isinstance(payload, dict) else {}
    with _LOCK:
        started = not REALSENSE["active"]
        if started and payload.get("startIfNeeded", True) is False:
            return _err("REALSENSE_CAPTURE_FAILED", "RealSense manual sample could not be captured.")
        if started:
            _h_realsense_start(payload)
        REALSENSE["totalSamples"] += 1
        sample = {"source": "realsense", "sampleId": f"mock-sample-{uuid.uuid4()}", "capturedAt": _now_iso(),
                  "frameNumber": REALSENSE["totalSamples"], "color": None, "depth": None, "attention": None}
        if REALSENSE["trainingSession"] or payload.get("purpose") == "training":
            _broadcast_event("realsense.training.sample", sample)
        if started and payload.get("keepRunning", False) is not True:
            _h_realsense_stop()
        return sample


def _h_realsense_training_start(payload=None):
    payload = payload if isinstance(payload, dict) else {}
    with _LOCK:
        REALSENSE["trainingSession"] = {"sessionId": payload.get("sessionId") or f"mock-training-{uuid.uuid4()}", "startedAt": _now_iso()}
        if payload.get("autoStart", True) is not False:
            _h_realsense_start(payload)
        return _h_realsense_status()


def _h_realsense_training_stop(payload=None):
    with _LOCK:
        session = REALSENSE["trainingSession"]
        REALSENSE["trainingSession"] = None
        if isinstance(payload, dict) and payload.get("stopCamera") is True:
            _h_realsense_stop()
        return {"stoppedAt": _now_iso(), "session": session, "status": _h_realsense_status()}


def _h_storage_stats():
    with _LOCK:
        return dict(STORAGE)


def _h_storage_screen_records(limit: int | None):
    with _LOCK:
        records = list(SCREEN_RECORDS)
    if limit is not None and limit > 0:
        records = records[:limit]
    return records


def _h_storage_camera_records(limit: int | None):
    with _LOCK:
        records = list(CAMERA_RECORDS)
    if limit is not None and limit > 0:
        records = records[:limit]
    return records


# -- Attention ------------------------------------------------------------

def _h_attention_config_status():
    with _LOCK:
        return dict(ATTENTION)


_ATTENTION_STATUSES = {"focused", "distracted", "away", "unknown"}


def _h_attention_status_set(payload: dict | None):
    global _ATTENTION_RECORD
    if not isinstance(payload, dict):
        return _err("INVALID_PAYLOAD", "Payload must be an object.")

    raw_status = payload.get("attentionStatus", payload.get("status"))
    if not isinstance(raw_status, str):
        return _err("INVALID_PAYLOAD", "Payload must include attentionStatus or status.")

    attention_status = raw_status.strip().lower()
    if attention_status not in _ATTENTION_STATUSES:
        return _err(
            "INVALID_ATTENTION_STATUS",
            "Attention status must be one of: focused, distracted, away, unknown.",
            {"allowed": sorted(_ATTENTION_STATUSES)},
        )

    confidence = payload.get("confidence")
    if confidence is not None:
        if not isinstance(confidence, (int, float)) or not 0 <= float(confidence) <= 1:
            return _err("INVALID_PAYLOAD", "confidence must be a number between 0 and 1.")
        confidence = round(float(confidence), 3)

    with _LOCK:
        if _ATTENTION_STOPPED:
            return _err("MONITOR_STOPPED", "Start the mock camera before supplying another observation.")
        boundary = payload.get("boundary")
        if boundary not in (None, "disconnected"):
            return _err("INVALID_PAYLOAD", "Use camera.stop for stopped boundaries; only disconnected is accepted here.")
        _ATTENTION_RECORD = attention_observation(
            {"state": attention_status, "source": str(payload.get("source") or "mock-api"),
             "confidence": confidence, "reason": str(payload.get("reason") or "Synthetic observation."),
             "modelVersion": "mock-1"},
            _ATTENTION_RECORD, _ATTENTION_SESSION, 500, int(time.time() * 1000), boundary,
        )
        ATTENTION.pop("boundary", None)
        ATTENTION.update(_ATTENTION_RECORD)
        attention_status = _ATTENTION_RECORD["state"]
        ATTENTION["attentionStatus"] = attention_status
        ATTENTION["lastUpdatedAt"] = _now_iso()
        ATTENTION["source"] = str(payload.get("source") or "mock-api")
        if confidence is not None:
            ATTENTION["confidence"] = confidence
        if "configured" in payload:
            ATTENTION["configured"] = bool(payload["configured"])
        if "reason" in payload:
            ATTENTION["reason"] = str(payload["reason"])
        status = dict(ATTENTION)

        # Share the lifecycle lock with stop so no observation follows its boundary.
        _broadcast_event("attention.status", status)
        _broadcast_event("attention.config.status", status)
    return status


# ---------------------------------------------------------------------------
# Dispatch table
# ---------------------------------------------------------------------------

GET_HANDLERS: dict[str, tuple] = {
    "/health":                              (_h_health,),
    "/manifest":                            (_h_manifest,),
    "/api/monitor/actions":                (_h_actions_list,),
    "/api/monitor/capabilities":           (_h_capabilities,),
    "/api/monitor/screen/status":          (_h_screen_status,),
    "/api/monitor/camera/status":          (_h_camera_status,),
    "/api/monitor/storage/stats":          (_h_storage_stats,),
    "/api/monitor/storage/screen-records": (_h_storage_screen_records,),
    "/api/monitor/storage/camera-records": (_h_storage_camera_records,),
    "/api/monitor/attention/config/status": (_h_attention_config_status,),
}

POST_HANDLERS: dict[str, tuple] = {
    "/api/monitor/screen/start":                (_h_screen_start,),
    "/api/monitor/screen/stop":                 (_h_screen_stop,),
    "/api/monitor/camera/start":                (_h_camera_start,),
    "/api/monitor/camera/stop":                 (_h_camera_stop,),
    "/api/monitor/camera/show":                 (_h_camera_show,),
    "/api/monitor/camera/hide":                 (_h_camera_hide,),
    "/api/monitor/mock/attention/status":       (_h_attention_status_set,),
}

# Action-name dispatch — mirrors the real server's dispatch_action()
ACTION_HANDLERS: dict[str, tuple] = {
    "health":                   (_h_health,),
    "manifest":                 (_h_manifest,),
    "actions.list":            (_h_actions_list,),
    "capabilities.list":       (_h_capabilities,),
    "screen.status":           (_h_screen_status,),
    "screen.start":            (_h_screen_start,),
    "screen.stop":             (_h_screen_stop,),
    "camera.status":           (_h_camera_status,),
    "camera.start":            (_h_camera_start,),
    "camera.stop":             (_h_camera_stop,),
    "camera.show":             (_h_camera_show,),
    "camera.hide":             (_h_camera_hide,),
    "storage.stats":           (_h_storage_stats,),
    "storage.screenRecords":   (_h_storage_screen_records,),
    "storage.cameraRecords":   (_h_storage_camera_records,),
    "attention.config.status": (_h_attention_config_status,),
    "attention.status.set":    (_h_attention_status_set,),
}

# RealSense actions use the same routes and lifecycle metadata as the real
# provider. Fixtures never open hardware, write samples, or claim raw-frame counts.
_REALSENSE_HANDLERS = [
    ("realsense.status", "GET", "status", _h_realsense_status),
    ("realsense.devices", "GET", "devices", lambda: []),
    ("realsense.start", "POST", "start", _h_realsense_start),
    ("realsense.stop", "POST", "stop", _h_realsense_stop),
    ("realsense.capture", "POST", "capture", _h_realsense_capture),
    ("realsense.frame.latest", "GET", "frame/latest", lambda: None),
    ("realsense.training.start", "POST", "training/start", _h_realsense_training_start),
    ("realsense.training.stop", "POST", "training/stop", _h_realsense_training_stop),
]
for _action, _method, _suffix, _handler in _REALSENSE_HANDLERS:
    _path = f"/api/monitor/realsense/{_suffix}"
    ACTION_DEFINITIONS.append({"action": _action, "method": _method, "path": _path, "description": "Synthetic RealSense lifecycle fixture."})
    (GET_HANDLERS if _method == "GET" else POST_HANDLERS)[_path] = (_handler,)
    ACTION_HANDLERS[_action] = (_handler,)


# ---------------------------------------------------------------------------
# SSE worker — pushes periodic events to connected clients
# ---------------------------------------------------------------------------
EVENT_BROKER = EventBroker()

# Mock event payloads rotated each cycle
_SCREEN_WINDOWS = [
    "Visual Studio Code — SYA-UI/app/src/pages/DashboardPage.tsx",
    "Google Chrome — GitHub Pull Request #142 — SYA-UI",
    "iTerm2 — htop — SYA monitor server",
    "Finder — ~/Desktop/SYA-UI/function/monitor",
    "Slack — #engineering — SYA team",
    "Visual Studio Code — SYA-UI/function/monitor/server.py",
]
_WINDOW_IDX = 0
_FACE_SEQUENCE = [True, True, False, True, True]
_FACE_IDX = 0
_SSE_TICK = 0


def _next_window() -> str:
    global _WINDOW_IDX
    w = _SCREEN_WINDOWS[_WINDOW_IDX % len(_SCREEN_WINDOWS)]
    _WINDOW_IDX += 1
    return w


def _next_face() -> bool:
    global _FACE_IDX
    f = _FACE_SEQUENCE[_FACE_IDX % len(_FACE_SEQUENCE)]
    _FACE_IDX += 1
    return f


def _broadcast_event(event: str, payload: dict):
    EVENT_BROKER.publish(event, payload)


def _sse_worker():
    """Emit periodic screen.capture (every 10 s) and camera.frame (every 3 s)."""
    global _SSE_TICK
    while True:
        time.sleep(3)
        _SSE_TICK += 1

        timestamp = _now_iso()
        face_detected = _next_face()
        with _LOCK:
            CAMERA["faceDetected"] = face_detected
            CAMERA["lastFrameTime"] = timestamp
            CAMERA["totalFrames"] = int(CAMERA.get("totalFrames", 0)) + 1
            cam_payload = {
                "faceDetected": CAMERA["faceDetected"],
                "timestamp": CAMERA["lastFrameTime"],
            }
        _broadcast_event("camera.frame", cam_payload)

        if _SSE_TICK % 3 == 0:
            timestamp = _now_iso()
            active_window = _next_window()
            idle_time = (_SSE_TICK * 7) % 60
            with _LOCK:
                SCREEN["activeWindow"] = active_window
                SCREEN["idleTimeSeconds"] = idle_time
                SCREEN["lastCaptureTime"] = timestamp
                SCREEN["totalCaptures"] = int(SCREEN.get("totalCaptures", 0)) + 1
                scr_payload = {
                    "activeWindow": SCREEN["activeWindow"],
                    "idleTimeSeconds": SCREEN["idleTimeSeconds"],
                    "timestamp": SCREEN["lastCaptureTime"],
                }
            _broadcast_event("screen.capture", scr_payload)


# ---------------------------------------------------------------------------
# HTTP request handler
# ---------------------------------------------------------------------------

def _parse_limit(payload: dict) -> int | None:
    """Extract an optional positive integer limit from a payload dict."""
    if not isinstance(payload, dict):
        return None
    limit_str = payload.get("limit")
    if limit_str is None:
        return None
    try:
        limit = int(limit_str)
        return limit if limit > 0 else None
    except (ValueError, TypeError):
        return None


class MockHandler(http.server.BaseHTTPRequestHandler):

    # Suppress default request logging to stderr
    def log_message(self, fmt, *args):
        pass

    def _request_authorized(self):
        self._runtime_token_authenticated = False
        if RUNTIME_TOKEN:
            authorization = self.headers.get("Authorization", "")
            if (not authorization.isascii() or not RUNTIME_TOKEN.isascii() or
                    not hmac.compare_digest(authorization, f"Bearer {RUNTIME_TOKEN}")):
                self._send_err(401, "UNAUTHORIZED", "Missing or invalid runtime token.")
                return False
            self._runtime_token_authenticated = True
        return True

    # ----- CORS / preflight -----------------------------------------------

    def _cors(self):
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Access-Control-Allow-Methods", "GET, POST, OPTIONS")
        self.send_header("Access-Control-Allow-Headers", "Content-Type, Authorization")

    def do_OPTIONS(self):
        self.send_response(204)
        self._cors()
        self.end_headers()

    # ----- JSON helpers ---------------------------------------------------

    def _send_json(self, status: int, payload):
        body = json.dumps(payload).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self._cors()
        self.send_header("Content-Length", str(len(body)))
        if (status == 200 and self.command == "GET" and
                urllib.parse.urlparse(self.path).path == "/health" and
                getattr(self, "_runtime_token_authenticated", False) and RUNTIME_PROOF_KEY):
            proof = hmac.new(RUNTIME_PROOF_KEY.encode("utf-8"), b"sya-runtime-v1", digestmod="sha256").hexdigest()
            self.send_header("X-SYA-Runtime-Proof", proof)
        self.end_headers()
        self.wfile.write(body)

    def _send_ok(self, data):
        self._send_json(200, _ok(data))

    def _send_result(self, result):
        if isinstance(result, dict) and result.get("ok") is False and "error" in result:
            self._send_json(400, result)
            return
        self._send_ok(result)

    def _send_err(self, status: int, code: str, message: str, details=None):
        self._send_json(status, _err(code, message, details))

    def _read_body(self) -> dict | None:
        content_length = int(self.headers.get("Content-Length", 0))
        if content_length <= 0:
            return {}
        raw = self.rfile.read(content_length)
        try:
            return json.loads(raw)
        except json.JSONDecodeError:
            self._send_err(400, "INVALID_JSON", "Request body is not valid JSON.")
            return None

    # ----- Routing --------------------------------------------------------

    def do_GET(self):
        if not self._request_authorized():
            return
        parsed = urllib.parse.urlparse(self.path)
        path = parsed.path
        query = dict(urllib.parse.parse_qsl(parsed.query))

        if path == "/api/monitor/events":
            self._handle_sse()
            return

        handler_info = GET_HANDLERS.get(path)
        if handler_info is None:
            self._send_err(404, "NOT_FOUND", f"No route for GET {path}")
            return

        handler = handler_info[0]

        # Extract optional "limit" query param for record endpoints
        if path in ("/api/monitor/storage/screen-records", "/api/monitor/storage/camera-records"):
            limit_str = query.get("limit")
            limit = None
            if limit_str is not None:
                try:
                    limit = int(limit_str)
                    if limit < 1:
                        limit = None
                except ValueError:
                    limit = None
            self._send_ok(handler(limit))
        else:
            self._send_ok(handler())

    def do_POST(self):
        if not self._request_authorized():
            return
        parsed = urllib.parse.urlparse(self.path)
        path = parsed.path

        if path == "/api/monitor/invoke":
            self._handle_invoke()
            return

        handler_info = POST_HANDLERS.get(path)
        if handler_info is None:
            self._send_err(404, "NOT_FOUND", f"No route for POST {path}")
            return

        handler = handler_info[0]
        payload = self._read_body()
        # _read_body may have already sent an error response
        if payload is None:
            return
        self._send_result(handler(payload))

    # ----- Generic invoke -------------------------------------------------

    def _handle_invoke(self):
        payload = self._read_body()
        if payload is None:
            return
        if not isinstance(payload, dict) or not isinstance(payload.get("action"), str):
            self._send_err(400, "INVALID_PAYLOAD", "Payload must include a string 'action' field.")
            return

        action = payload["action"]
        if action == "invoke":
            self._send_err(400, "INVALID_PAYLOAD", "Nested invoke is not supported.")
            return

        inner_payload = payload.get("payload")
        handler_info = ACTION_HANDLERS.get(action)

        if handler_info is None:
            self._send_err(404, "UNKNOWN_ACTION", f"Unknown monitor action: {action}")
            return

        handler = handler_info[0]
        # POST-mutable actions accept a payload; pure GET actions don't
        if handler in (
            _h_screen_start,
            _h_screen_stop,
            _h_camera_start,
            _h_camera_stop,
            _h_camera_show,
            _h_camera_hide,
            _h_attention_status_set,
            _h_realsense_start, _h_realsense_stop, _h_realsense_capture,
            _h_realsense_training_start, _h_realsense_training_stop,
        ):
            self._send_result(handler(inner_payload))
        elif handler is _h_storage_screen_records:
            limit = _parse_limit(payload)
            self._send_ok(handler(limit))
        elif handler is _h_storage_camera_records:
            limit = _parse_limit(payload)
            self._send_ok(handler(limit))
        else:
            self._send_ok(handler())

    # ----- SSE stream -----------------------------------------------------

    def _handle_sse(self):
        subscriber = EVENT_BROKER.subscribe(self.headers.get("Last-Event-ID"))
        try:
            self.send_response(200)
            self.send_header("Content-Type", "text/event-stream")
            self.send_header("Cache-Control", "no-cache")
            self.send_header("Connection", "keep-alive")
            self._cors()
            self.end_headers()
            ready = json.dumps({"functionId": "monitor", "event": "ready", "payload": {}})
            self.wfile.write(f"event: ready\ndata: {ready}\n\n".encode())
            self.wfile.flush()
            while True:
                try:
                    message = subscriber.get(timeout=15)
                except queue.Empty:
                    self.wfile.write(b": keep-alive\n\n")
                    self.wfile.flush()
                    continue
                cursor = critical_event_id(message)
                event_id = f"id: {cursor}\n" if cursor else ""
                body = json.dumps(message, separators=(",", ":"))
                self.wfile.write(f"{event_id}event: {message['event']}\ndata: {body}\n\n".encode())
                self.wfile.flush()
        except (BrokenPipeError, ConnectionResetError, OSError):
            self.close_connection = True
        finally:
            EVENT_BROKER.unsubscribe(subscriber)


# ---------------------------------------------------------------------------
# HTTP server
# ---------------------------------------------------------------------------

class ThreadedHTTPServer(http.server.ThreadingHTTPServer):
    allow_reuse_address = True
    daemon_threads = True

    def server_bind(self):
        # Keep startup independent of reverse DNS for the numeric loopback bind.
        socketserver.TCPServer.server_bind(self)
        self.server_name, self.server_port = self.server_address[:2]


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main():
    port = _resolve_port()

    # Start the SSE background thread
    sse_thread = threading.Thread(target=_sse_worker, daemon=True)
    sse_thread.start()

    server = ThreadedHTTPServer(("127.0.0.1", port), MockHandler)
    print(f"Mock monitor server listening on http://127.0.0.1:{port}")
    print(f"Health:        GET  /health")
    print(f"Manifest:      GET  /manifest")
    print(f"Screen status: GET  /api/monitor/screen/status")
    print(f"Screen start:  POST /api/monitor/screen/start")
    print(f"SSE events:    GET  /api/monitor/events")
    print()
    print("All data is mocked — no screen capture or camera hardware required.")
    print("Press Ctrl+C to stop.")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\nShutting down.")
        server.shutdown()


if __name__ == "__main__":
    main()
