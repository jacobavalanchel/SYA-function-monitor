import http.client
import importlib.util
import json
import threading
import unittest
from contextlib import contextmanager
from pathlib import Path
from unittest import mock

import server


ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location("monitor_mock_loopback", ROOT / "mock/server.py")
mock_server = importlib.util.module_from_spec(spec)
spec.loader.exec_module(mock_server)


class QuietProxyHandler(server.ProxyHandler):
    def log_message(self, _format, *_args):
        pass


class LoopbackServerTests(unittest.TestCase):
    runtimes = ((server, QuietProxyHandler), (mock_server, mock_server.MockHandler))

    @contextmanager
    def running_server(self, runtime, handler):
        # Fail immediately instead of depending on the machine's DNS latency.
        with mock.patch("socket.getfqdn", side_effect=AssertionError("Unexpected reverse DNS")) as getfqdn, \
                mock.patch.object(runtime, "RUNTIME_TOKEN", "loopback-test-token"):
            with runtime.ThreadedHTTPServer(("127.0.0.1", 0), handler) as httpd:
                thread = threading.Thread(target=httpd.serve_forever, kwargs={"poll_interval": 0.01}, daemon=True)
                thread.start()
                try:
                    yield httpd
                finally:
                    httpd.shutdown()
                    thread.join(timeout=2)
                    self.assertFalse(thread.is_alive())
            getfqdn.assert_not_called()

    def request_health(self, httpd, headers=None):
        connection = http.client.HTTPConnection("127.0.0.1", httpd.server_port, timeout=2)
        try:
            connection.request("GET", "/health", headers=headers or {})
            response = connection.getresponse()
            return response.status, json.loads(response.read())
        finally:
            connection.close()

    def test_real_and_mock_start_without_dns_and_serve_health_on_bound_loopback_port(self):
        for runtime, handler in self.runtimes:
            with self.subTest(runtime=runtime.__name__), self.running_server(runtime, handler) as httpd:
                self.assertEqual(httpd.server_name, "127.0.0.1")
                self.assertGreater(httpd.server_port, 0)
                self.assertEqual(httpd.socket.getsockname(), ("127.0.0.1", httpd.server_port))
                status, body = self.request_health(httpd, {"Authorization": "Bearer loopback-test-token"})
                self.assertEqual(status, 200)
                self.assertTrue(body["ok"])
                self.assertEqual(body["data"]["functionId"], "monitor")
                self.assertEqual(body["data"]["status"], "ok")

    def test_health_still_requires_configured_token_in_real_and_mock(self):
        for runtime, handler in self.runtimes:
            with self.subTest(runtime=runtime.__name__), self.running_server(runtime, handler) as httpd:
                for headers in ({}, {"Authorization": "Bearer wrong-token"}):
                    status, body = self.request_health(httpd, headers)
                    self.assertEqual(status, 401)
                    self.assertEqual(body["error"]["code"], "UNAUTHORIZED")

    def test_real_server_still_rejects_non_loopback_host_header(self):
        with self.running_server(server, QuietProxyHandler) as httpd:
            status, body = self.request_health(httpd, {
                "Authorization": "Bearer loopback-test-token",
                "Host": f"attacker.example:{httpd.server_port}",
            })
            self.assertEqual(status, 403)
            self.assertEqual(body["error"]["code"], "FORBIDDEN")
            status, body = self.request_health(httpd, {
                "Authorization": "Bearer loopback-test-token",
                "Host": f"localhost:{httpd.server_port}",
            })
            self.assertEqual(status, 200)
            self.assertTrue(body["ok"])


if __name__ == "__main__":
    unittest.main()
