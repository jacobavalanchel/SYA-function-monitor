import http.client
import importlib.util
import json
import os
import threading
import unittest
import urllib.error
from pathlib import Path
from unittest import mock

import server

ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location("monitor_mock_http", ROOT / "mock/server.py")
mock_server = importlib.util.module_from_spec(spec)
spec.loader.exec_module(mock_server)
spec = importlib.util.spec_from_file_location("monitor_attention_control", ROOT / "mock/attention_control.py")
attention_control = importlib.util.module_from_spec(spec)
spec.loader.exec_module(attention_control)


class QuietProxyHandler(server.ProxyHandler):
    def log_message(self, _format, *_args):
        return None


class RuntimeProofAssertions:
    runtime = server
    handler = QuietProxyHandler

    def setUp(self):
        self.token_patch = mock.patch.object(self.runtime, "RUNTIME_TOKEN", "test-runtime-token", create=True)
        self.proof_patch = mock.patch.object(self.runtime, "RUNTIME_PROOF_KEY", "test-runtime-proof-key", create=True)
        self.token_patch.start()
        self.proof_patch.start()
        self.addCleanup(self.token_patch.stop)
        self.addCleanup(self.proof_patch.stop)
        self.httpd = self.runtime.ThreadedHTTPServer(("127.0.0.1", 0), self.handler)
        self.httpd.daemon_threads = True
        self.thread = threading.Thread(target=self.httpd.serve_forever, daemon=True)
        self.thread.start()
        self.port = self.httpd.server_address[1]

    def tearDown(self):
        self.httpd.shutdown()
        self.httpd.server_close()
        self.thread.join(timeout=2)

    def request_with_headers(self, method, path, body=None, headers=None):
        connection = http.client.HTTPConnection("127.0.0.1", self.port, timeout=2)
        try:
            connection.request(method, path, body=body, headers=headers or {})
            response = connection.getresponse()
            return response.status, response.read(), dict(response.getheaders())
        finally:
            connection.close()

    def request(self, method, path, body=None, headers=None):
        status, payload, _headers = self.request_with_headers(method, path, body, headers)
        return status, payload

    def authorized_headers(self, **headers):
        return {"Authorization": "Bearer test-runtime-token", **headers}

    def test_authenticated_health_proves_process_identity_without_exposing_key(self):
        # Known vector computed with the consumer's Node createHmac API.
        expected = "57d9bdc2a9606d61df219e614b75119f7ff275f60477df065e95e321522d94c2"
        with mock.patch.dict(os.environ, {"FUNCTION_RUNTIME_PROOF_KEY": "changed-after-start"}):
            status, body, headers = self.request_with_headers("GET", "/health", headers=self.authorized_headers())
        self.assertEqual(status, 200)
        self.assertEqual(headers.get("X-SYA-Runtime-Proof"), expected)
        self.assertTrue(json.loads(body)["ok"])
        self.assertNotIn("test-runtime-proof-key", str(headers) + body.decode())
        self.assertNotIn("test-runtime-token", str(headers) + body.decode())

    def test_missing_proof_key_leaves_authenticated_health_without_proof(self):
        with mock.patch.object(self.runtime, "RUNTIME_PROOF_KEY", ""):
            status, body, headers = self.request_with_headers("GET", "/health", headers=self.authorized_headers())
        self.assertEqual(status, 200)
        self.assertTrue(json.loads(body)["ok"])
        self.assertNotIn("X-SYA-Runtime-Proof", headers)

    def test_standalone_health_remains_available_but_never_claims_token_proof(self):
        with mock.patch.object(self.runtime, "RUNTIME_TOKEN", ""):
            for request_headers in ({}, self.authorized_headers()):
                status, _body, headers = self.request_with_headers("GET", "/health", headers=request_headers)
                self.assertEqual(status, 200)
                self.assertNotIn("X-SYA-Runtime-Proof", headers)

    def test_only_direct_authenticated_health_get_emits_proof(self):
        cases = [
            ("GET", "/manifest", None),
            ("GET", "/api/monitor/actions", None),
            ("POST", "/api/monitor/invoke", json.dumps({"action": "health"})),
            ("POST", "/health", "{}"),
        ]
        for method, path, body in cases:
            with self.subTest(method=method, path=path):
                status, body, headers = self.request_with_headers(
                    method, path, body, self.authorized_headers(**{"Content-Type": "application/json"}))
                self.assertNotIn("X-SYA-Runtime-Proof", headers)
                if method == "POST" and path == "/api/monitor/invoke":
                    self.assertEqual(status, 200)
                    result = json.loads(body)
                    self.assertTrue(result["ok"])
                    self.assertEqual(result["data"]["functionId"], "monitor")
                    self.assertEqual(result["data"]["version"], json.loads((ROOT / "manifest.json").read_text())["version"])

    def test_non_ascii_authorization_is_rejected_before_dispatch(self):
        cases = [("GET", "/health"), ("POST", "/api/monitor/invoke"), ("GET", "/api/monitor/events")]
        with mock.patch.object(self.runtime, "dispatch_action", create=True) as dispatch, \
                mock.patch.object(self.httpd, "handle_error"):
            for method, path in cases:
                with self.subTest(method=method, path=path):
                    status, body, headers = self.request_with_headers(
                        method, path, "{}" if method == "POST" else None,
                        {"Authorization": "Bearer invalid-\u00e9", "Content-Type": "application/json"})
                    self.assertEqual(status, 401)
                    self.assertEqual(json.loads(body)["error"]["code"], "UNAUTHORIZED")
                    self.assertNotIn("X-SYA-Runtime-Proof", headers)
            dispatch.assert_not_called()

    def test_all_declared_get_post_and_sse_routes_reject_missing_and_wrong_tokens(self):
        openapi = json.loads((ROOT / "api.openapi.json").read_text())
        # Stubs make any authorization regression fail safely before hardware
        # actions, and turn an accidentally accepted SSE stream into finite JSON.
        stream_method = "_send_event_stream" if self.runtime is server else "_handle_sse"
        def finite_stream(handler):
            handler._send_json(200, {"ok": True})
        with mock.patch.object(self.runtime, "dispatch_action", return_value={}, create=True), \
                mock.patch.object(self.handler, stream_method, finite_stream):
            for path, item in openapi["paths"].items():
                for method in ("get", "post"):
                    if method not in item:
                        continue
                    for authorization in (None, "Bearer incorrect"):
                        with self.subTest(method=method, path=path, authorization=authorization):
                            request_headers = {"Content-Type": "application/json"}
                            if authorization:
                                request_headers["Authorization"] = authorization
                            status, body, headers = self.request_with_headers(
                                method.upper(), path, "{}" if method == "post" else None, request_headers)
                            self.assertEqual(status, 401)
                            self.assertEqual(json.loads(body)["error"]["code"], "UNAUTHORIZED")
                            self.assertNotIn("X-SYA-Runtime-Proof", headers)


class HttpRuntimeTests(RuntimeProofAssertions, unittest.TestCase):
    def test_runtime_token_is_required(self):
        status, body = self.request("GET", "/health")
        self.assertEqual(status, 401)
        self.assertEqual(json.loads(body)["error"]["code"], "UNAUTHORIZED")

        status, body = self.request("GET", "/health", headers=self.authorized_headers())
        self.assertEqual(status, 200)
        self.assertTrue(json.loads(body)["ok"])

    def test_event_stream_rejects_missing_and_wrong_tokens(self):
        for headers in ({}, {"Authorization": "Bearer incorrect"}):
            status, body = self.request("GET", "/api/monitor/events", headers=headers)
            self.assertEqual(status, 401)
            self.assertEqual(json.loads(body)["error"]["code"], "UNAUTHORIZED")

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


class MockHttpRuntimeTests(RuntimeProofAssertions, unittest.TestCase):
    runtime = mock_server
    handler = mock_server.MockHandler

    def test_controller_updates_authenticated_mock_over_http(self):
        with mock.patch.dict(os.environ, {"FUNCTION_RUNTIME_TOKEN": "test-runtime-token", "http_proxy": "http://127.0.0.1:1", "no_proxy": ""}), \
                mock.patch.object(attention_control.urllib.request, "proxy_bypass", return_value=False), \
                mock.patch.dict(mock_server.ATTENTION, clear=False), \
                mock.patch.object(mock_server, "_ATTENTION_RECORD", None), \
                mock.patch.object(mock_server, "_ATTENTION_STOPPED", False), \
                mock.patch.object(mock_server, "_broadcast_event"):
            result = attention_control._post_status(f"http://127.0.0.1:{self.port}", "focused", 0.95)
            self.assertTrue(result["ok"])
            self.assertEqual(result["data"]["attentionStatus"], "focused")
            self.assertEqual(mock_server.ATTENTION["attentionStatus"], "focused")

    def test_controller_preserves_tokenless_standalone_http(self):
        with mock.patch.dict(os.environ, {"FUNCTION_RUNTIME_TOKEN": ""}), \
                mock.patch.object(mock_server, "RUNTIME_TOKEN", ""), \
                mock.patch.dict(mock_server.ATTENTION, clear=False), \
                mock.patch.object(mock_server, "_ATTENTION_RECORD", None), \
                mock.patch.object(mock_server, "_ATTENTION_STOPPED", False), \
                mock.patch.object(mock_server, "_broadcast_event"):
            result = attention_control._post_status(f"http://127.0.0.1:{self.port}", "distracted", 0.35)
            self.assertTrue(result["ok"])
            self.assertEqual(result["data"]["attentionStatus"], "distracted")


class AttentionControlSafetyTests(unittest.TestCase):
    def test_authenticated_controller_rejects_unsafe_destinations_before_network(self):
        destinations = [
            "http://example.invalid", "http://192.0.2.1", "http://127.0.0.1.example.invalid",
            "https://127.0.0.1", "file:///tmp/mock", "http://user:password@127.0.0.1",
            "http://127.0.0.1?target=remote", "http://127.0.0.1#remote", "http://127.0.0.1:invalid",
        ]
        with mock.patch.dict(os.environ, {"FUNCTION_RUNTIME_TOKEN": "test-runtime-token"}), \
                mock.patch.object(attention_control.urllib.request, "urlopen") as urlopen, \
                mock.patch.object(attention_control.urllib.request, "build_opener") as build_opener:
            for destination in destinations:
                with self.subTest(destination=destination), self.assertRaisesRegex(urllib.error.URLError, "loopback HTTP"):
                    attention_control._post_status(destination, "focused", 0.95)
            urlopen.assert_not_called()
            build_opener.assert_not_called()

    def test_authenticated_controller_normalizes_loopback_without_dns(self):
        self.assertEqual(attention_control._authenticated_base_url("http://localhost:8770"), "http://127.0.0.1:8770")
        self.assertEqual(attention_control._authenticated_base_url("http://[::1]:8770"), "http://[::1]:8770")

    def test_authenticated_controller_does_not_follow_redirects(self):
        requests = []

        class RedirectHandler(http.server.BaseHTTPRequestHandler):
            def log_message(self, _format, *_args):
                pass

            def do_POST(self):
                self.rfile.read(int(self.headers.get("Content-Length", "0")))
                requests.append((self.command, self.path, self.headers.get("Authorization") == "Bearer test-runtime-token"))
                self.send_response(302)
                self.send_header("Location", "/redirected")
                self.send_header("Content-Length", "0")
                self.end_headers()

            def do_GET(self):
                requests.append((self.command, self.path, False))
                self.send_response(200)
                self.send_header("Content-Length", "2")
                self.end_headers()
                self.wfile.write(b"{}")

        httpd = server.ThreadedHTTPServer(("127.0.0.1", 0), RedirectHandler)
        thread = threading.Thread(target=httpd.serve_forever, daemon=True)
        thread.start()
        try:
            with mock.patch.dict(os.environ, {"FUNCTION_RUNTIME_TOKEN": "test-runtime-token"}), \
                    self.assertRaises(urllib.error.HTTPError) as error:
                attention_control._post_status(f"http://127.0.0.1:{httpd.server_address[1]}", "focused", 0.95)
            self.assertEqual(error.exception.code, 302)
            error.exception.close()
            self.assertEqual(requests, [("POST", "/api/monitor/mock/attention/status", True)])
        finally:
            httpd.shutdown()
            httpd.server_close()
            thread.join(timeout=2)


class RuntimeContractTests(unittest.TestCase):
    def test_all_declared_operations_document_conditional_bearer_and_denials(self):
        api = json.loads((ROOT / "api.openapi.json").read_text())
        self.assertEqual(api["security"], [{"RuntimeBearer": []}, {}])
        scheme = api["components"]["securitySchemes"]["RuntimeBearer"]
        self.assertEqual((scheme["type"], scheme["scheme"]), ("http", "bearer"))
        self.assertIn("FUNCTION_RUNTIME_TOKEN", scheme["description"])
        for path, item in api["paths"].items():
            for method, operation in item.items():
                if method not in {"get", "post", "put", "patch", "delete", "head", "options", "trace"}:
                    continue
                with self.subTest(path=path, method=method):
                    for code, response_name in (("401", "RuntimeUnauthorized"), ("403", "RuntimeForbidden")):
                        self.assertEqual(operation["responses"][code]["$ref"], f"#/components/responses/{response_name}")
                        self.assertIn("application/json", api["components"]["responses"][response_name]["content"])


if __name__ == "__main__":
    unittest.main()
