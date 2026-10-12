"""Synthetic startup evidence tests; no frozen binary, device or model imports."""

import contextlib
import errno
import importlib.util
import io
import json
import os
from pathlib import Path
import runpy
import socket
import sys
import tempfile
import types
import unittest
from unittest import mock


ROOT = Path(__file__).resolve().parents[1]


def load(name, relative):
    spec = importlib.util.spec_from_file_location(name, ROOT / relative)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class StartupTraceTests(unittest.TestCase):
    def setUp(self):
        self.trace = load("synthetic_startup_trace", "startup_trace.py")

    def test_source_and_unknown_stages_are_silent(self):
        output = io.StringIO()
        with mock.patch.object(sys, "frozen", False, create=True), contextlib.redirect_stderr(output):
            for stage in self.trace.STAGES:
                self.trace.emit(stage)
        with mock.patch.object(sys, "frozen", True, create=True), contextlib.redirect_stderr(output):
            self.trace.emit("private/path-or-config")
        self.assertEqual(output.getvalue(), "")
        self.assertEqual(self.trace._emitted, set())

    def test_frozen_markers_are_fixed_once_and_flush_immediately(self):
        output = mock.Mock()
        with mock.patch.object(sys, "frozen", True, create=True), \
                mock.patch.object(sys, "stderr", output), \
                mock.patch.object(self.trace.time, "monotonic_ns", return_value=123):
            for stage in self.trace.STAGES:
                self.trace.emit(stage)
                self.trace.emit(stage)
        self.assertEqual(output.method_calls, [call for stage in self.trace.STAGES for call in
                         (mock.call.write(f"[sya-startup-v1] {stage} 123\n"), mock.call.flush())])

    def test_stderr_failures_never_escape_and_are_not_retried(self):
        for method in ("write", "flush"):
            with self.subTest(method=method):
                self.trace._emitted.clear()
                output = mock.Mock()
                getattr(output, method).side_effect = OSError("private-data")
                with mock.patch.object(sys, "frozen", True, create=True), mock.patch.object(sys, "stderr", output):
                    self.trace.emit("bootstrap")
                    self.trace.emit("bootstrap")
                self.assertEqual(output.write.call_count, 1)

    def test_hook_is_first_custom_hook_and_only_emits_bootstrap(self):
        events = []
        fake = types.SimpleNamespace(emit=events.append)
        with mock.patch.dict(sys.modules, {"startup_trace": fake}):
            runpy.run_path(str(ROOT / "packaging/startup_hook.py"))
        self.assertEqual(events, ["bootstrap"])
        self.assertIn('runtime_hooks=[str(ROOT / "packaging" / "startup_hook.py")]',
                      (ROOT / "packaging/monitor.spec").read_text())

    def test_entry_orders_import_freeze_tls_and_server_stages(self):
        events = []
        fake_trace = types.SimpleNamespace(emit=events.append)
        fake_tls = types.SimpleNamespace(configure_native_tls=lambda: events.append("actual_tls"))
        fake_server = types.SimpleNamespace(main=lambda: events.append("actual_server_main"))
        with mock.patch.object(sys, "frozen", True, create=True), \
                mock.patch.object(sys, "argv", ["synthetic-native"]), \
                mock.patch.dict(sys.modules, {"startup_trace": fake_trace, "native_tls": fake_tls, "server": fake_server}):
            entry = load("synthetic_native_entry", "packaging/native_entry.py")
            with mock.patch.object(entry.multiprocessing, "freeze_support", side_effect=lambda: events.append("actual_freeze")):
                self.assertEqual(entry.main(), 0)
        self.assertEqual(events, ["entry", "entry_imports_complete", "actual_freeze", "freeze_support_complete",
                                  "tls_start", "actual_tls", "tls_complete", "server_import_start",
                                  "server_import_complete", "actual_server_main"])

    def test_frozen_self_test_keeps_one_json_stdout_and_no_server_import(self):
        output, errors = io.StringIO(), io.StringIO()
        fake_test = types.SimpleNamespace(run_self_test=lambda: {"ok": True})
        fake_tls = types.SimpleNamespace(configure_native_tls=lambda: None)
        with mock.patch.object(sys, "frozen", True, create=True), \
                mock.patch.object(sys, "argv", ["synthetic-native", "--self-test"]), \
                mock.patch.dict(sys.modules, {"startup_trace": self.trace, "native_tls": fake_tls,
                                             "native_self_test": fake_test, "server": None}), \
                contextlib.redirect_stdout(output), contextlib.redirect_stderr(errors):
            entry = load("synthetic_self_test_entry", "packaging/native_entry.py")
            with mock.patch.object(entry.multiprocessing, "freeze_support"):
                self.assertEqual(entry.main(), 0)
        self.assertEqual(output.getvalue(), '{"ok": true}\n')
        self.assertEqual(len(errors.getvalue().splitlines()), 5)
        self.assertNotIn("server_import", errors.getvalue())

    def test_tls_and_server_import_failures_do_not_emit_completion(self):
        for fails_tls in (True, False):
            with self.subTest(fails_tls=fails_tls):
                self.trace._emitted.clear()
                output = io.StringIO()
                tls = mock.Mock(side_effect=RuntimeError("synthetic TLS failure") if fails_tls else None)
                with mock.patch.object(sys, "frozen", True, create=True), \
                        mock.patch.object(sys, "argv", ["synthetic-native"]), \
                        mock.patch.dict(sys.modules, {"startup_trace": self.trace,
                                                     "native_tls": types.SimpleNamespace(configure_native_tls=tls),
                                                     "server": None}), \
                        contextlib.redirect_stderr(output):
                    entry = load("synthetic_failing_entry", "packaging/native_entry.py")
                    with mock.patch.object(entry.multiprocessing, "freeze_support"), \
                            self.assertRaises(RuntimeError if fails_tls else ModuleNotFoundError):
                        entry.main()
                expected = ["entry", "entry_imports_complete", "freeze_support_complete", "tls_start"]
                if not fails_tls:
                    expected += ["tls_complete", "server_import_start"]
                self.assertEqual([line.split()[1] for line in output.getvalue().splitlines()], expected)
                tls.assert_called_once_with()

    @contextlib.contextmanager
    def isolated_server(self, config_text="{}"):
        with tempfile.TemporaryDirectory() as temporary, \
                mock.patch.dict(os.environ, {"HOME": temporary,
                                             "MONITOR_CONFIG_PATH": str(Path(temporary) / "config.json"),
                                             "MONITOR_RECORDS_DIR": str(Path(temporary) / "records")}, clear=True), \
                mock.patch.object(sys, "path", [str(ROOT), *sys.path]), \
                mock.patch.dict(sys.modules, {"startup_trace": self.trace, "pyrealsense2": None,
                                             "numpy": None, "mediapipe": None, "PIL": None}):
            Path(os.environ["MONITOR_CONFIG_PATH"]).write_text(config_text)
            yield load("synthetic_startup_server", "server.py")

    def test_actual_server_occupied_loopback_port_has_no_bind_or_loop_completion(self):
        output = io.StringIO()
        with self.isolated_server() as server, socket.socket() as occupied:
            occupied.bind(("127.0.0.1", 0))
            occupied.listen(1)
            with mock.patch.object(sys, "frozen", True, create=True), \
                    mock.patch.object(sys, "argv", ["synthetic-server", str(occupied.getsockname()[1])]), \
                    contextlib.redirect_stderr(output), self.assertRaises(OSError) as failure:
                server.main()
            self.assertEqual(failure.exception.errno, errno.EADDRINUSE)
            self.assertIsNone(server.REALSENSE.rs)
            self.assertIsNone(server.REALSENSE.pipeline)
            self.assertIsNone(server.REALSENSE.focus_detector)
            self.assertIsNone(server.REALSENSE.worker)
        self.assertEqual([line.split()[1] for line in output.getvalue().splitlines()],
                         ["config_start", "config_complete", "bind_start"])

    def test_actual_config_failure_has_no_config_or_bind_completion(self):
        output = io.StringIO()
        with self.isolated_server("{ invalid synthetic json") as server, \
                mock.patch.object(sys, "frozen", True, create=True), \
                mock.patch.object(sys, "argv", ["synthetic-server", "0"]), \
                mock.patch.object(server, "ThreadedHTTPServer", side_effect=AssertionError("must not bind")) as bind, \
                contextlib.redirect_stderr(output), self.assertRaises(json.JSONDecodeError):
            server.main()
        bind.assert_not_called()
        self.assertEqual([line.split()[1] for line in output.getvalue().splitlines()], ["config_start"])


if __name__ == "__main__":
    unittest.main()
