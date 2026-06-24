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


def screen_status():
    return {
        "isMonitoring": False,
        "captureRateMs": 10000,
        "activeWindow": "",
        "idleTimeSeconds": 0,
        "totalCaptures": 0,
        "lastCaptureTime": None,
        "storagePath": "",
    }


def camera_status():
    return {
        "isActive": False,
        "windowCreated": False,
        "faceDetected": False,
        "totalFrames": 0,
        "lastFrameTime": None,
        "storagePath": "",
    }


class ProxyHandler(http.server.SimpleHTTPRequestHandler):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, directory=STATIC_DIR, **kwargs)

    def do_GET(self):
        parsed = urllib.parse.urlparse(self.path)
        path = parsed.path

        if path == "/health":
            self._send_json(200, response({
                "status": "ok",
                "functionId": "monitor",
                "version": load_manifest().get("version", "0.0.0"),
            }))
        elif path == "/manifest":
            self._send_json(200, load_manifest())
        elif path == "/api/config":
            self._send_config_status()
        elif path == "/api/monitor/screen/status":
            self._send_json(200, response(screen_status()))
        elif path == "/api/monitor/camera/status":
            self._send_json(200, response(camera_status()))
        elif path == "/api/monitor/storage/stats":
            self._send_json(200, response({
                "totalRecords": 0,
                "screenshotsDir": "",
                "cameraDir": "",
                "logsDir": "",
                "diskUsageMB": 0,
            }))
        elif path == "/api/monitor/storage/screen-records":
            self._send_json(200, response([]))
        elif path == "/api/monitor/storage/camera-records":
            self._send_json(200, response([]))
        elif path == "/api/monitor/attention/config/status":
            self._send_json(200, response(config_status()))
        elif path == "/api/monitor/events":
            self._send_event_stream()
        else:
            super().do_GET()

    def do_POST(self):
        parsed = urllib.parse.urlparse(self.path)
        path = parsed.path

        if path == "/api/llm":
            self._proxy_llm()
        elif path == "/api/monitor/screen/start":
            status = screen_status()
            status["isMonitoring"] = True
            self._send_json(200, response(status))
        elif path == "/api/monitor/screen/stop":
            self._send_json(200, response(screen_status()))
        elif path == "/api/monitor/camera/start":
            status = camera_status()
            status["isActive"] = True
            self._send_json(200, response(status))
        elif path == "/api/monitor/camera/stop":
            self._send_json(200, response(camera_status()))
        else:
            self.send_error(404)

    def _send_json(self, status_code, payload):
        self.send_response(status_code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Access-Control-Allow-Origin", "*")
        self.end_headers()
        self.wfile.write(json.dumps(payload).encode())

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

    def _proxy_llm(self):
        content_length = int(self.headers.get("Content-Length", 0))
        body = self.rfile.read(content_length)

        try:
            req_data = json.loads(body)
        except json.JSONDecodeError:
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
