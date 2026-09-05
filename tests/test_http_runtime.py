import http.client
import json
import threading
import unittest
from unittest import mock

import server


class QuietProxyHandler(server.ProxyHandler):
    def log_message(self, _format, *_args):
        return None


class HttpRuntimeTests(unittest.TestCase):
    def setUp(self):
        self.original_token = server.RUNTIME_TOKEN
        server.RUNTIME_TOKEN = "test-runtime-token"
        self.httpd = server.http.server.ThreadingHTTPServer(("127.0.0.1", 0), QuietProxyHandler)
        self.httpd.daemon_threads = True
        self.thread = threading.Thread(target=self.httpd.serve_forever, daemon=True)
        self.thread.start()
        self.port = self.httpd.server_address[1]

    def tearDown(self):
        self.httpd.shutdown()
        self.httpd.server_close()
        self.thread.join(timeout=2)
        server.RUNTIME_TOKEN = self.original_token

    def request(self, method, path, body=None, headers=None):
        connection = http.client.HTTPConnection("127.0.0.1", self.port, timeout=2)
        connection.request(method, path, body=body, headers=headers or {})
        response = connection.getresponse()
        payload = response.read()
        connection.close()
        return response.status, payload

    def authorized_headers(self, **headers):
        return {"Authorization": "Bearer test-runtime-token", **headers}

    def test_runtime_token_is_required(self):
        status, body = self.request("GET", "/health")
        self.assertEqual(status, 401)
        self.assertEqual(json.loads(body)["error"]["code"], "UNAUTHORIZED")

        status, body = self.request("GET", "/health", headers=self.authorized_headers())
        self.assertEqual(status, 200)
        self.assertTrue(json.loads(body)["ok"])

    def test_static_server_does_not_expose_config_or_repository(self):
        for path in ["/config.local.json", "/config.example.json", "/.git/config", "/server.py"]:
            status, _body = self.request("GET", path, headers=self.authorized_headers())
            self.assertEqual(status, 404, path)

    def test_invalid_json_does_not_dispatch_action(self):
        headers = self.authorized_headers(**{"Content-Type": "application/json"})
        with mock.patch.object(server, "dispatch_action") as dispatch:
            status, body = self.request("POST", "/api/monitor/camera/start", b"{", headers)
        self.assertEqual(status, 400)
        self.assertEqual(json.loads(body)["error"]["code"], "INVALID_PAYLOAD")
        dispatch.assert_not_called()

    def test_non_json_body_does_not_dispatch_action(self):
        headers = self.authorized_headers(**{"Content-Type": "text/plain"})
        with mock.patch.object(server, "dispatch_action") as dispatch:
            status, body = self.request("POST", "/api/monitor/camera/start", b"{}", headers)
        self.assertEqual(status, 415)
        self.assertEqual(json.loads(body)["error"]["code"], "UNSUPPORTED_MEDIA_TYPE")
        dispatch.assert_not_called()

    def test_cross_origin_request_is_rejected_without_token_mode(self):
        server.RUNTIME_TOKEN = ""
        status, body = self.request("GET", "/health", headers={"Origin": "https://attacker.example"})
        self.assertEqual(status, 403)
        self.assertEqual(json.loads(body)["error"]["code"], "FORBIDDEN")


if __name__ == "__main__":
    unittest.main()
