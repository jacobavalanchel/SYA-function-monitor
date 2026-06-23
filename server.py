#!/usr/bin/env python3
"""Simple server that serves static files and proxies LLM API calls to avoid CORS."""

import http.server
import json
import urllib.request
import urllib.error
import os

PORT = 8765
STATIC_DIR = os.path.dirname(os.path.abspath(__file__))

class ProxyHandler(http.server.SimpleHTTPRequestHandler):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, directory=STATIC_DIR, **kwargs)

    def do_POST(self):
        if self.path == "/api/llm":
            self._proxy_llm()
        else:
            self.send_error(404)

    def _proxy_llm(self):
        content_length = int(self.headers.get("Content-Length", 0))
        body = self.rfile.read(content_length)

        try:
            req_data = json.loads(body)
        except json.JSONDecodeError:
            self.send_error(400, "Invalid JSON")
            return

        api_url = req_data.pop("_api_url", "")
        api_key = req_data.pop("_api_key", "")

        if not api_url or not api_key:
            self.send_error(400, "Missing _api_url or _api_key")
            return

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
            self.send_response(e.code)
            self.send_header("Content-Type", "application/json")
            self.send_header("Access-Control-Allow-Origin", "*")
            self.end_headers()
            self.wfile.write(json.dumps({"error": err_body}).encode())
        except Exception as e:
            self.send_response(502)
            self.send_header("Content-Type", "application/json")
            self.end_headers()
            self.wfile.write(json.dumps({"error": str(e)}).encode())

    def do_OPTIONS(self):
        self.send_response(204)
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Access-Control-Allow-Methods", "POST, GET, OPTIONS")
        self.send_header("Access-Control-Allow-Headers", "Content-Type, Authorization")
        self.end_headers()

if __name__ == "__main__":
    with http.server.HTTPServer(("", PORT), ProxyHandler) as httpd:
        print(f"Serving on http://localhost:{PORT}")
        print(f"Static files from: {STATIC_DIR}")
        print(f"LLM proxy at: POST /api/llm")
        httpd.serve_forever()
