#!/usr/bin/env python3
"""Local HTTP runtime for the SYA monitor function artifact."""

import http.server
import json
import os
import sys
import urllib.error
import urllib.parse
import urllib.request

STATIC_DIR = os.path.dirname(os.path.abspath(__file__))
MODULE_ROOT = os.path.dirname(STATIC_DIR)
DEFAULT_CONFIG_PATH = os.path.join(STATIC_DIR, "config.local.json")
FALLBACK_CONFIG_PATH = os.path.join(STATIC_DIR, "config.json")
MANIFEST_PATH = os.path.join(MODULE_ROOT, "manifest.json")
DEFAULT_PORT = 8765

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
    if not os.path.exists(MANIFEST_PATH):
        return {
            "id": "monitor",
            "version": "0.0.0",
            "runtime": "local-http",
            "permissions": ["screen", "camera", "storage"],
        }

    with open(MANIFEST_PATH, "r", encoding="utf-8") as manifest_file:
        return json.load(manifest_file)


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
    yield DEFAULT_CONFIG_PATH
    yield FALLBACK_CONFIG_PATH


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
        "cameraDir": "",
        "logsDir": "",
        "diskUsageMB": 0,
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
    CAMERA_STATE["isActive"] = True
    CAMERA_STATE["windowCreated"] = True
    return CAMERA_STATE


def camera_stop(_payload=None):
    CAMERA_STATE["isActive"] = False
    return CAMERA_STATE


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
        "camera.status": lambda: CAMERA_STATE,
        "camera.start": lambda: camera_start(payload),
        "camera.stop": lambda: camera_stop(payload),
        "camera.show": lambda: camera_show(payload),
        "camera.hide": lambda: camera_hide(payload),
        "storage.stats": storage_stats,
        "storage.screenRecords": lambda: list_records(parse_positive_int(query.get("limit"))),
        "storage.cameraRecords": lambda: list_records(parse_positive_int(query.get("limit"))),
        "attention.config.status": config_status,
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
            "/api/monitor/storage/stats": "storage.stats",
            "/api/monitor/storage/screen-records": "storage.screenRecords",
            "/api/monitor/storage/camera-records": "storage.cameraRecords",
            "/api/monitor/attention/config/status": "attention.config.status",
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
        self.send_header("Access-Control-Allow-Origin", "*")
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
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream")
        self.send_header("Cache-Control", "no-cache")
        self.send_header("Access-Control-Allow-Origin", "*")
        self.end_headers()
        event = json.dumps({"functionId": "monitor", "event": "ready", "payload": {}})
        self.wfile.write(f"event: ready\ndata: {event}\n\n".encode())
        self.wfile.flush()

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
                self.send_header("Access-Control-Allow-Origin", "*")
                self.end_headers()
                self.wfile.write(resp_body)
        except urllib.error.HTTPError as e:
            err_body = e.read().decode("utf-8", errors="replace")
            self._send_json(e.code, {"error": err_body})
        except Exception as e:
            self._send_json(502, {"error": str(e)})

    def do_OPTIONS(self):
        self.send_response(204)
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Access-Control-Allow-Methods", "POST, GET, OPTIONS")
        self.send_header("Access-Control-Allow-Headers", "Content-Type, Authorization")
        self.end_headers()


if __name__ == "__main__":
    port = resolve_port()
    api_config = load_api_config()
    with http.server.HTTPServer(("127.0.0.1", port), ProxyHandler) as httpd:
        print(f"Serving on http://127.0.0.1:{port}")
        print(f"Static files from: {STATIC_DIR}")
        print("Health at: GET /health")
        print("LLM proxy at: POST /api/llm")
        if api_config["path"] and api_config["key"]:
            print(f"API config: {api_config['config_path']}")
            print(f"API path: {api_config['path']}")
        else:
            print("API config: missing; copy config.example.json to config.local.json")
        httpd.serve_forever()
