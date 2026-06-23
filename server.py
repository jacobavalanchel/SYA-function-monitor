#!/usr/bin/env python3
"""Simple server that serves static files and proxies LLM API calls to avoid CORS."""

import http.server
import json
import urllib.request
import urllib.error
import os

PORT = 8765
STATIC_DIR = os.path.dirname(os.path.abspath(__file__))
DEFAULT_CONFIG_PATH = os.path.join(STATIC_DIR, "config.local.json")
FALLBACK_CONFIG_PATH = os.path.join(STATIC_DIR, "config.json")

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

class ProxyHandler(http.server.SimpleHTTPRequestHandler):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, directory=STATIC_DIR, **kwargs)

    def do_GET(self):
        if self.path == "/api/config":
            self._send_config_status()
        else:
            super().do_GET()

    def do_POST(self):
        if self.path == "/api/llm":
            self._proxy_llm()
        else:
            self.send_error(404)

    def _send_json(self, status_code, payload):
        self.send_response(status_code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Access-Control-Allow-Origin", "*")
        self.end_headers()
        self.wfile.write(json.dumps(payload).encode())

    def _send_config_status(self):
        api_config = load_api_config()
        self._send_json(200, {
            "configured": bool(api_config["path"] and api_config["key"]),
            "apiPath": api_config["path"],
            "model": api_config["model"],
            "configFile": os.path.basename(api_config["config_path"]) if api_config["config_path"] else "",
        })

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
    api_config = load_api_config()
    with http.server.HTTPServer(("", PORT), ProxyHandler) as httpd:
        print(f"Serving on http://localhost:{PORT}")
        print(f"Static files from: {STATIC_DIR}")
        print(f"LLM proxy at: POST /api/llm")
        if api_config["path"] and api_config["key"]:
            print(f"API config: {api_config['config_path']}")
            print(f"API path: {api_config['path']}")
        else:
            print("API config: missing; copy config.example.json to config.local.json")
        httpd.serve_forever()
