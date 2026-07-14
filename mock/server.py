#!/usr/bin/env python3
"""Standalone mock server for the SYA Monitor function API.

No screen capture or camera hardware required.  All data is drawn from
the companion data.json file and mutated in-memory so that POST actions
(screen.start, camera.show, etc.) are reflected in subsequent GET calls.

Port: 8770 (or PORT env) — deliberately different from the real server (8765).
"""

from __future__ import annotations

import http.server
import json
import os
import sys
import threading
import time
import urllib.parse

# ---------------------------------------------------------------------------
# Paths
# ---------------------------------------------------------------------------
MOCK_DIR = os.path.dirname(os.path.abspath(__file__))
DATA_PATH = os.path.join(MOCK_DIR, "data.json")
MANIFEST_PATH = os.path.join(os.path.dirname(MOCK_DIR), "manifest.json")

DEFAULT_PORT = 8770
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

# Incrementing counters to make mock data feel alive
_SCREEN_COUNTER = len(SCREEN_RECORDS) + 1
_CAMERA_COUNTER = len(CAMERA_RECORDS) + 1

_LOCK = threading.Lock()


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
    with _LOCK:
        CAMERA["isActive"] = True
        CAMERA["windowCreated"] = True
        CAMERA["lastFrameTime"] = _now_iso()
        status = dict(CAMERA)
    _broadcast_event("camera.status", status)
    return status


def _h_camera_stop(_payload=None):
    with _LOCK:
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


# ---------------------------------------------------------------------------
# SSE worker — pushes periodic events to connected clients
# ---------------------------------------------------------------------------
_SSE_CLIENTS: list[http.server.BaseHTTPRequestHandler] = []
_SSE_LOCK = threading.Lock()

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
    body = json.dumps({"functionId": "monitor", "event": event, "payload": payload})
    line = f"event: {event}\ndata: {body}\n\n"

    with _SSE_LOCK:
        clients = list(_SSE_CLIENTS)

    dead = []
    for client in clients:
        try:
            client.wfile.write(line.encode())
            client.wfile.flush()
        except Exception:
            dead.append(client)

    if dead:
        with _SSE_LOCK:
            for client in dead:
                if client in _SSE_CLIENTS:
                    _SSE_CLIENTS.remove(client)


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
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream")
        self.send_header("Cache-Control", "no-cache")
        self.send_header("Connection", "keep-alive")
        self._cors()
        self.end_headers()

        # Emit ready event immediately
        ready = json.dumps({"functionId": "monitor", "event": "ready", "payload": {}})
        self.wfile.write(f"event: ready\ndata: {ready}\n\n".encode())
        self.wfile.flush()

        with _SSE_LOCK:
            _SSE_CLIENTS.append(self)

        # Keep the connection open — the SSE worker thread pushes events.
        # We spin here so the handler doesn't return (which would close the conn).
        try:
            while True:
                time.sleep(1)
        except (BrokenPipeError, ConnectionResetError):
            pass
        finally:
            with _SSE_LOCK:
                if self in _SSE_CLIENTS:
                    _SSE_CLIENTS.remove(self)


# ---------------------------------------------------------------------------
# HTTP server
# ---------------------------------------------------------------------------

class ThreadedHTTPServer(http.server.ThreadingHTTPServer):
    allow_reuse_address = True
    daemon_threads = True


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
