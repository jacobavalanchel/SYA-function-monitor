"""Failure boundaries for native packaging; real inference is a separate gate."""

import contextlib
import ast
import importlib.util
import io
import json
import os
from pathlib import Path
import signal
import shutil
import socket
import subprocess
import sys
import tempfile
import threading
import time
import types
import unittest
import urllib.error
import urllib.request
from unittest import mock


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))


def load(name, relative):
    spec = importlib.util.spec_from_file_location(name, ROOT / relative)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


builder = load("native_builder_tests", "scripts/build-native-runtime.py")
entry = load("native_entry_tests", "packaging/native_entry.py")
self_test = load("native_self_test_tests", "packaging/native_self_test.py")
server = load("native_server_tests", "server.py")


def capture_and_log(method):
    def run(self):
        for logged in (False, True):
            with self.subTest(logged=logged):
                method(self, logged)
    return run


class NativeBuildBoundaryTests(unittest.TestCase):
    def resistant_child_command(self, root, *, exit_leader=False):
        witness = root / "owned-pids.json"
        code = (
            "import json,os,pathlib,signal,sys,time\n"
            "child = os.fork()\n"
            "if child == 0:\n"
            " signal.signal(signal.SIGTERM, signal.SIG_IGN)\n"
            " pathlib.Path(sys.argv[1] + '.ready').write_text(str(os.getpid()))\n"
            " while True: time.sleep(1)\n"
            "else:\n"
            " while not pathlib.Path(sys.argv[1] + '.ready').exists(): time.sleep(0.01)\n"
            " pathlib.Path(sys.argv[1] + '.tmp').write_text(json.dumps({'leader':os.getpid(),'child':child}))\n"
            " pathlib.Path(sys.argv[1] + '.tmp').replace(sys.argv[1])\n"
            + (" sys.exit(0)\n" if exit_leader else " time.sleep(30)\n")
        )
        return [sys.executable, "-c", code, str(witness), "sya-native-owned-fixture"], witness

    def assert_group_gone(self, witness):
        pids = json.loads(witness.read_text())
        self.assertFalse(builder.process_group_exists(pids["leader"]))

    @capture_and_log
    def test_timeout_kills_logged_resistant_descendant_after_leader_exits(self, logged):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            command, witness = self.resistant_child_command(root)
            env = builder.isolated_environment(root / "home", root / "tmp")
            lifecycle = {}
            with self.assertRaisesRegex(RuntimeError, "deadline"):
                builder.run_bounded(command, env=env, cwd=root, timeout=0.3, log=root / "child.log" if logged else None, lifecycle=lifecycle)
            self.assert_group_gone(witness)
            if not logged:
                self.assertTrue(lifecycle["pipesDrained"])

    @capture_and_log
    def test_normal_leader_exit_also_cleans_unfinished_descendant(self, logged):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            command, witness = self.resistant_child_command(root, exit_leader=True)
            env = builder.isolated_environment(root / "home", root / "tmp")
            lifecycle = {}
            with self.assertRaisesRegex(RuntimeError, "descendants"):
                builder.run_bounded(command, env=env, cwd=root, timeout=5, log=root / "child.log" if logged else None, lifecycle=lifecycle)
            self.assert_group_gone(witness)
            if not logged:
                self.assertTrue(lifecycle["pipesDrained"])

    @capture_and_log
    def test_keyboard_interrupt_cleans_detached_process_group(self, logged):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            command, witness = self.resistant_child_command(root)
            env = builder.isolated_environment(root / "home", root / "tmp")
            lifecycle = {}
            wait_method = "communicate" if logged else "wait"
            communicate = getattr(subprocess.Popen, wait_method)
            interrupted = False

            def interrupt_once(process, *args, **kwargs):
                nonlocal interrupted
                if not interrupted:
                    interrupted = True
                    deadline = time.monotonic() + 3
                    while not witness.exists() and time.monotonic() < deadline:
                        time.sleep(0.01)
                    self.assertTrue(witness.exists())
                    raise KeyboardInterrupt()
                return communicate(process, *args, **kwargs)

            with mock.patch.object(subprocess.Popen, wait_method, interrupt_once):
                with self.assertRaises(KeyboardInterrupt):
                    builder.run_bounded(command, env=env, cwd=root, timeout=10, log=root / "child.log" if logged else None, lifecycle=lifecycle)
            self.assert_group_gone(witness)
            if not logged:
                self.assertTrue(lifecycle["pipesDrained"])

    @capture_and_log
    def test_cli_sigterm_enters_child_cleanup(self, logged):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            command, witness = self.resistant_child_command(root)
            env = builder.isolated_environment(root / "home", root / "tmp")
            lifecycle = {}
            code = (
                "import importlib.util,json,sys\n"
                "s=importlib.util.spec_from_file_location('builder',sys.argv[1]); b=importlib.util.module_from_spec(s); s.loader.exec_module(b)\n"
                "command=json.loads(sys.argv[2]); root=sys.argv[3]; lifecycle={}\n"
                f"b.build=lambda args: b.run_bounded(command,env=b.isolated_environment(root+'/inner-home',root+'/inner-tmp'),cwd=root,timeout=60,log=(root+'/child.log' if {logged!r} else None),lifecycle=lifecycle)\n"
                "sys.argv=['builder','--output',root+'/unused','--sdk-provenance',root+'/unused','--node-executable',root+'/unused','--wheelhouse',root+'/unused']\n"
                "try: b.main()\n"
                "finally: b.Path(root+'/lifecycle.json').write_text(json.dumps(lifecycle))\n"
            )
            process = subprocess.Popen([sys.executable, "-c", code, str(ROOT / "scripts/build-native-runtime.py"),
                                        json.dumps(command), str(root)], env=env, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
            try:
                deadline = time.monotonic() + 5
                while not witness.exists() and time.monotonic() < deadline:
                    time.sleep(0.02)
                self.assertTrue(witness.exists())
                process.send_signal(signal.SIGTERM)
                _output, error = process.communicate(timeout=12)
                self.assertEqual(process.returncode, 128 + signal.SIGTERM, error.decode())
                self.assert_group_gone(witness)
                if not logged:
                    self.assertTrue(json.loads((root / "lifecycle.json").read_text())["pipesDrained"])
            finally:
                if process.poll() is None:
                    process.kill()
                process.communicate(timeout=3)
                if witness.exists():
                    try:
                        os.killpg(json.loads(witness.read_text())["leader"], signal.SIGKILL)
                    except ProcessLookupError:
                        pass

    def test_snapshot_binds_live_contracts_lock_and_provenance_without_private_files(self):
        self.assertIn("eye_observation.py", builder.SOURCE_FILES)
        with tempfile.TemporaryDirectory() as temporary:
            root, output = Path(temporary) / "source", Path(temporary) / "output"
            root.mkdir()
            for name in ("server.py", "eye_observation.py", "manifest.json", "private-config.json"):
                (root / name).write_text(name)
            lock, provenance = root / "lock", root / "sdk.json"
            lock.write_text("same-version old-hash")
            provenance.write_text("sdk")
            with mock.patch.object(builder, "ROOT", root), mock.patch.object(builder, "SOURCE_FILES", ("server.py", "eye_observation.py", "manifest.json")):
                snapshot, bindings, digest = builder.source_snapshot(output, lock, provenance, Path(sys.executable).resolve())
                self.assertFalse((snapshot / "private-config.json").exists())
                self.assertEqual(len(digest), 64)
                (snapshot / "private-config.json").write_text("unexpected")
                with self.assertRaisesRegex(RuntimeError, "file set changed"):
                    builder.verify_input_bindings(snapshot, bindings)
                (snapshot / "private-config.json").unlink()
                for changed in (root / "server.py", root / "eye_observation.py", root / "manifest.json", lock, provenance):
                    original = changed.read_bytes()
                    changed.write_bytes(original + b" mutation")
                    with self.assertRaisesRegex(RuntimeError, "Live build input changed"):
                        builder.verify_input_bindings(snapshot, bindings)
                    changed.write_bytes(original)
                (snapshot / "server.py").write_text("mutated copy")
                with self.assertRaisesRegex(RuntimeError, "snapshot changed"):
                    builder.verify_input_bindings(snapshot, bindings)

    def test_diagnostic_options_bound_independently_and_strictly(self):
        with tempfile.TemporaryDirectory() as temporary:
            output = Path(temporary)
            lock, sdk = output / "lock", output / "sdk"
            lock.write_text("lock")
            sdk.write_text("sdk")
            snapshot, bindings, _ = builder.source_snapshot(output, lock, sdk, Path(sys.executable).resolve(), "bootloader-importtime")
            self.assertEqual(json.loads((snapshot / "inputs/build-options.json").read_text()),
                             {"schemaVersion": 1, "startupDiagnostics": "bootloader-importtime"})
            live = Path(bindings["inputs/build-options.json"]["path"])
            frozen = snapshot / "inputs/build-options.json"
            self.assertNotEqual(live, frozen)
            original = live.read_bytes()
            live.write_text("{}")
            with self.assertRaisesRegex(RuntimeError, "Live build input changed"):
                builder.verify_input_bindings(snapshot, bindings)
            live.write_bytes(original)
            reduced = {key: value for key, value in bindings.items() if key != "scripts/startup-diagnostics.mjs"}
            with self.assertRaisesRegex(RuntimeError, "file set changed"):
                builder.verify_input_bindings(snapshot, reduced)
            with self.assertRaises(ValueError):
                builder.build_options("imports")

    @unittest.skipUnless(shutil.which("node"), "Node required for shared diagnostic parser")
    def test_selftest_failure_retains_only_structured_diagnostics(self):
        with tempfile.TemporaryDirectory() as temporary:
            output = Path(temporary)
            executable = output / "fake-native"
            executable.write_text("#!" + sys.executable + "\nimport sys\nsys.stderr.write('[PYI-123:DEBUG] LOADER: process level = 1\\nimport time: 20 | 30 | ssl\\nPRIVATE_LEAK_SENTINEL\\n')\nsys.exit(1)\n")
            executable.chmod(0o700)
            with self.assertRaisesRegex(RuntimeError, "Native validation failed") as failure:
                builder.verify_candidate(executable, output, 5, node=Path(shutil.which("node")).resolve(),
                                         snapshot=ROOT, mode="bootloader-importtime")
            text = (output / "selftest-startup-original.json").read_text()
            self.assertNotIn("PRIVATE_LEAK_SENTINEL", text + str(failure.exception))
            record = json.loads(text)
            self.assertEqual(record["status"], "failed")
            self.assertEqual(record["startupDiagnostics"]["events"][-1]["module"], "ssl")

    @unittest.skipUnless(shutil.which("node"), "Node required for shared diagnostic parser")
    def test_selftest_contract_failure_is_failed_and_success_is_passed(self):
        with tempfile.TemporaryDirectory() as temporary:
            output = Path(temporary)
            node = Path(shutil.which("node")).resolve()
            for payload in ({"ok": False}, {"ok": True, "frozen": True},
                            {"ok": True, "frozen": True, "nativeLibraries": {"checked": True}}):
                with mock.patch.object(builder, "run_bounded", return_value=json.dumps(payload)):
                    if payload.get("nativeLibraries"):
                        result = builder.verify_candidate(output / "unused", output, 5, node=node, snapshot=ROOT)
                        self.assertEqual(result["status"], "passed")
                    else:
                        with self.assertRaises(RuntimeError):
                            builder.verify_candidate(output / "unused", output, 5, node=node, snapshot=ROOT)
                evidence = json.loads((output / "selftest-startup-original.json").read_text())
                self.assertEqual(evidence["status"], "passed" if payload.get("nativeLibraries") else "failed")

    def test_reader_failure_closes_streams_and_fails_without_raw_text(self):
        class Broken(io.BytesIO):
            def read1(self, _size):
                raise OSError("PRIVATE_READER_SENTINEL")
        stdout, stderr = Broken(), io.BytesIO(b"")
        child = types.SimpleNamespace(pid=123, stdout=stdout, stderr=stderr, returncode=0, wait=lambda **kwargs: 0)
        state = {}
        with mock.patch.object(builder.subprocess, "Popen", return_value=child), mock.patch.object(builder, "wait_owned_group", return_value=True):
            with self.assertRaises(builder.NativeValidationError) as failure:
                builder.run_bounded_capture([], env={}, cwd=".", timeout=1, lifecycle=state)
        self.assertEqual(failure.exception.code, "reader")
        self.assertTrue(stdout.closed and stderr.closed)
        self.assertEqual(state["readerFailures"], ["stdout_read"])
        self.assertNotIn("PRIVATE_READER_SENTINEL", json.dumps(state) + str(failure.exception))

    def test_cleanup_failure_preserves_nonzero_and_actual_byte_counts(self):
        child = types.SimpleNamespace(pid=123, stdout=io.BytesIO(b"ok"), stderr=io.BytesIO(b"x" * (4 * 1024 * 1024 + 100)), returncode=7, wait=lambda **kwargs: 7)
        state, chunks = {}, []
        with mock.patch.object(builder.subprocess, "Popen", return_value=child), mock.patch.object(builder, "cleanup_owned_group", side_effect=RuntimeError("PRIVATE_CLEANUP_SENTINEL")):
            with self.assertRaises(builder.NativeValidationError) as failure:
                builder.run_bounded_capture([], env={}, cwd=".", timeout=1, lifecycle=state, stderr_chunks=chunks)
        self.assertEqual(failure.exception.code, "nonzero")
        self.assertEqual(state["cleanupFailure"], "cleanup")
        self.assertEqual(state["receivedBytes"]["stderr"], 4 * 1024 * 1024 + 100)
        self.assertEqual(state["omittedBytes"]["stderr"], 100)
        self.assertEqual(state["deadlineMs"], 1000)
        self.assertGreaterEqual(state["elapsedMs"], 0)
        self.assertTrue(state["pipesDrained"])
        self.assertNotIn("PRIVATE_CLEANUP_SENTINEL", json.dumps(state) + str(failure.exception))

    def test_parser_failure_still_persists_original_failure_and_lifecycle(self):
        with tempfile.TemporaryDirectory() as temporary:
            def failed_run(*args, **kwargs):
                kwargs["lifecycle"].update({"pid": 123, "cleanup": "group_gone", "failure": "nonzero"})
                raise builder.NativeValidationError("nonzero")
            with mock.patch.object(builder, "run_bounded", side_effect=failed_run), mock.patch.object(builder, "parse_diagnostics", side_effect=RuntimeError("PRIVATE_PARSER_SENTINEL")):
                with self.assertRaises(builder.NativeValidationError) as failure:
                    builder.verify_candidate(Path(temporary) / "unused", temporary, 1, node="unused", snapshot=ROOT, mode="bootloader-importtime")
            record = json.loads((Path(temporary) / "selftest-startup-original.json").read_text())
            self.assertEqual(failure.exception.code, "nonzero")
            self.assertEqual(record["failure"], "nonzero")
            self.assertEqual(record["parserFailure"], "parser")
            self.assertEqual(record["lifecycle"]["cleanup"], "group_gone")
            self.assertNotIn("PRIVATE_PARSER_SENTINEL", json.dumps(record))

    @unittest.skipUnless(shutil.which("node"), "Node required for shared diagnostic coverage")
    def test_diagnostic_selftest_requires_debug_and_importtime_events(self):
        with tempfile.TemporaryDirectory() as temporary:
            output = Path(temporary)
            payload = {"ok": True, "frozen": True, "nativeLibraries": {"checked": True}}
            with mock.patch.object(builder, "run_bounded", return_value=json.dumps(payload)):
                with self.assertRaises(builder.NativeValidationError) as failure:
                    builder.verify_candidate(output / "unused", output, 1, node=Path(shutil.which("node")).resolve(),
                                             snapshot=ROOT, mode="bootloader-importtime")
            self.assertEqual(failure.exception.code, "coverage")
            record = json.loads((output / "selftest-startup-original.json").read_text())
            self.assertEqual(record["status"], "failed")
            self.assertFalse(record["startupDiagnostics"]["coverage"]["ok"])

    @unittest.skipUnless(shutil.which("node"), "Node required for shared HTTP diagnostic coverage")
    def test_diagnostic_http_requires_debug_and_importtime_events(self):
        with tempfile.TemporaryDirectory() as temporary:
            output = Path(temporary)
            (output / "scripts").mkdir()
            (output / "inputs").mkdir()
            (output / "inputs/build-options.json").write_text(json.dumps(builder.build_options("bootloader-importtime")))
            shutil.copyfile(ROOT / "scripts/startup-diagnostics.mjs", output / "scripts/startup-diagnostics.mjs")
            (output / "scripts/test-staged-runtime.mjs").write_text("export async function testStagedRuntime() { return { runtimeSha256: 'a'.repeat(64), sseHandshake:true, authenticatedOperations:29, startupTrace:{status:'complete'} }; }")
            with self.assertRaises(RuntimeError):
                builder.verify_http_stage(output, output, Path(shutil.which("node")).resolve(), output,
                                          invocation="original", expected_stage_digest="a" * 64)
            evidence = json.loads(next(output.glob("http-startup-*.json")).read_text())
            self.assertEqual(evidence["status"], "failed")
            self.assertEqual(evidence["error"], "Native diagnostic coverage failed.")

    def test_node_and_wheelhouse_must_be_explicit_absolute_inputs(self):
        for directory in (False, True):
            with self.assertRaisesRegex(ValueError, "absolute"):
                builder.absolute_input("relative", directory=directory)

    @unittest.skipUnless(shutil.which("node"), "requires explicit local Node for actual HTTP wrapper test")
    def test_http_wrapper_calls_imported_helper_once_without_cli_entry(self):
        with tempfile.TemporaryDirectory() as temporary:
            output = Path(temporary).resolve()
            stage = output / "stage"
            stage.mkdir()
            snapshot = output / "snapshot"
            (snapshot / "scripts").mkdir(parents=True)
            # Match the real helper's CLI guard so a successful import cannot
            # silently run the same validation twice before JSON is parsed.
            (snapshot / "scripts/test-staged-runtime.mjs").write_text("""
import fs from 'node:fs';
import path from 'node:path';
import { fileURLToPath } from 'node:url';
export async function testStagedRuntime(directory, options = {}) {
  fs.appendFileSync(path.join(directory, 'calls.jsonl'), JSON.stringify({
    directory, startupTimeoutMs: options.startupTimeoutMs,
    requestTimeoutMs: options.requestTimeoutMs, stopTimeoutMs: options.stopTimeoutMs,
    hasSignal: options.signal instanceof AbortSignal, collectStartupTrace: options.collectStartupTrace,
  }) + '\\n');
  return { runtimeSha256: 'a'.repeat(64), sseHandshake: true, authenticatedOperations: 29, startupTrace: { status: 'complete' } };
}
if (process.argv[1] && path.resolve(process.argv[1]) === fileURLToPath(import.meta.url)) {
  console.log(JSON.stringify(await testStagedRuntime(process.argv[2])));
}
""")
            run = builder.run_bounded

            def collect_before_validation(command, **kwargs):
                text = run(command, **kwargs)
                record = json.loads(Path(command[-1]).read_text())
                self.assertEqual(record["status"], "collected")
                self.assertEqual(record["invocation"], "original")
                self.assertEqual(record["expectedRuntimeSha256"], "a" * 64)
                return text

            with mock.patch.object(builder, "run_bounded", side_effect=collect_before_validation):
                result = builder.verify_http_stage(stage, output, Path(shutil.which("node")).resolve(), snapshot,
                                                   invocation="original", expected_stage_digest="a" * 64)
            self.assertEqual(result["authenticatedOperations"], 29)
            self.assertEqual(result["timeoutScope"], "builder-diagnostic")
            self.assertEqual(json.loads((output / result["startupEvidenceFile"]).read_text())["status"], "passed")
            calls = [json.loads(line) for line in (stage / "calls.jsonl").read_text().splitlines()]
            self.assertEqual(calls, [{"directory": str(stage), "startupTimeoutMs": 30000,
                                     "requestTimeoutMs": 1500, "stopTimeoutMs": 2000, "hasSignal": True,
                                     "collectStartupTrace": True}])

    @unittest.skipUnless(shutil.which("node"), "requires Node for synthetic HTTP wrapper test")
    def test_http_wrapper_keeps_incomplete_and_failed_startup_evidence(self):
        with tempfile.TemporaryDirectory() as temporary:
            output = Path(temporary).resolve()
            snapshot = output / "snapshot"
            (snapshot / "scripts").mkdir(parents=True)
            for status in ("missing", "partial", "invalid"):
                with self.subTest(status=status):
                    (snapshot / "scripts/test-staged-runtime.mjs").write_text(
                        "export async function testStagedRuntime() { return "
                        + json.dumps({"runtimeSha256": "a" * 64, "sseHandshake": True,
                                      "authenticatedOperations": 29, "startupTrace": {"status": status}})
                        + "; }")
                    with self.assertRaisesRegex(RuntimeError, "Native validation failed"):
                        builder.verify_http_stage(output, output, Path(shutil.which("node")).resolve(), snapshot,
                                                  invocation="relocated", expected_stage_digest="a" * 64)
            records = [json.loads(file.read_text()) for file in output.glob("http-startup-*.json")]
            self.assertEqual(len(records), 3)
            self.assertEqual({record["startupTrace"]["status"] for record in records}, {"missing", "partial", "invalid"})
            self.assertTrue(all(record["status"] == "failed" and record["startupTimeoutMs"] == 30000 for record in records))
            self.assertTrue(all(record["invocation"] == "relocated" and record["expectedRuntimeSha256"] == "a" * 64 for record in records))

    def test_abrupt_http_wrapper_failure_never_leaves_empty_or_running_json(self):
        for failure in (RuntimeError("synthetic-wrapper-exit"), KeyboardInterrupt()):
            with self.subTest(failure=type(failure).__name__), tempfile.TemporaryDirectory() as temporary:
                output = Path(temporary).resolve()

                def terminate(command, **_kwargs):
                    evidence = Path(command[-1])
                    self.assertEqual(json.loads(evidence.read_text())["status"], "running")
                    # Simulate interruption while replacing the wrapper result.
                    evidence.write_text("")
                    raise failure

                with mock.patch.object(builder, "run_bounded", side_effect=terminate):
                    with self.assertRaises(type(failure)):
                        builder.verify_http_stage(output, output, Path(sys.executable), output,
                                                  invocation="original", expected_stage_digest="a" * 64)
                records = [json.loads(file.read_text()) for file in output.glob("http-startup-*.json")]
                self.assertEqual(len(records), 1)
                self.assertEqual(records[0]["status"], "failed")
                self.assertIsNone(records[0]["startupTrace"])
                self.assertEqual(records[0]["error"], "HTTP startup wrapper did not complete.")
                self.assertEqual(records[0]["invocation"], "original")
                self.assertEqual(records[0]["expectedRuntimeSha256"], "a" * 64)

    def test_rejected_http_result_is_failed_and_keeps_collected_trace(self):
        trace = {"status": "complete", "stages": [{"stage": "bootstrap", "receiptMs": 1}]}
        for invalid in ({"runtimeSha256": "b" * 64}, {"runtimeSha256": "invalid"}, {"sseHandshake": False},
                        {"authenticatedOperations": 0}, {"authenticatedOperations": True}, {"authenticatedOperations": "29"}):
            with self.subTest(invalid=invalid), tempfile.TemporaryDirectory() as temporary:
                output = Path(temporary).resolve()
                result = {"runtimeSha256": "a" * 64, "sseHandshake": True, "authenticatedOperations": 29,
                          "startupTrace": trace, **invalid}

                def collect(command, **_kwargs):
                    evidence = Path(command[-1])
                    pending = json.loads(evidence.read_text())
                    evidence.write_text(json.dumps({**pending, "status": "collected", "result": result, "startupTrace": trace}))
                    return json.dumps(result)

                with mock.patch.object(builder, "run_bounded", side_effect=collect):
                    with self.assertRaisesRegex(RuntimeError, "native HTTP stage was not verified"):
                        builder.verify_http_stage(output, output, Path(sys.executable), output,
                                                  invocation="original", expected_stage_digest="a" * 64)
                record = json.loads(next(output.glob("http-startup-*.json")).read_text())
                self.assertEqual(record["status"], "failed")
                self.assertEqual(record["result"], result)
                self.assertEqual(record["startupTrace"], trace)
                self.assertEqual(record["expectedRuntimeSha256"], "a" * 64)
                self.assertEqual(record["invocation"], "original")

    def test_post_save_http_supervision_failure_keeps_result_and_trace(self):
        trace = {"status": "partial", "stages": [{"stage": "bootstrap", "receiptMs": 1}]}
        for failure in (RuntimeError("synthetic supervisor failure"), KeyboardInterrupt()):
            with self.subTest(failure=type(failure).__name__), tempfile.TemporaryDirectory() as temporary:
                output = Path(temporary).resolve()
                result = {"runtimeSha256": "a" * 64, "startupTrace": trace}

                def terminate(command, **_kwargs):
                    evidence = Path(command[-1])
                    pending = json.loads(evidence.read_text())
                    evidence.write_text(json.dumps({**pending, "status": "collected", "result": result, "startupTrace": trace}))
                    raise failure

                with mock.patch.object(builder, "run_bounded", side_effect=terminate):
                    with self.assertRaises(type(failure)):
                        builder.verify_http_stage(output, output, Path(sys.executable), output,
                                                  invocation="relocated", expected_stage_digest="a" * 64)
                record = json.loads(next(output.glob("http-startup-*.json")).read_text())
                self.assertEqual(record["status"], "failed")
                self.assertEqual(record["result"], result)
                self.assertEqual(record["startupTrace"], trace)
                self.assertEqual(record["expectedRuntimeSha256"], "a" * 64)
                self.assertEqual(record["invocation"], "relocated")

    @unittest.skipUnless(shutil.which("node"), "requires explicit local Node for actual HTTP mutation test")
    def test_missing_native_server_main_fails_http_despite_self_test_success(self):
        with tempfile.TemporaryDirectory() as temporary:
            output = Path(temporary)
            stage = output / "stage"
            (stage / "bin").mkdir(parents=True)
            for name in ("manifest.json", "module.json", "api.openapi.json", "package.json", "server.py", "realsense_runtime.py", "eye_observation.py"):
                shutil.copyfile(ROOT / name, stage / name)
            # Reproduce the review mutation on the real entry source. The fake
            # diagnostic intentionally passes; only the service smoke can catch it.
            source = (ROOT / "packaging/native_entry.py").read_text()
            self.assertIn("server.main()", source)
            mutated = source.replace("server.main()", "pass  # regression: server startup removed")
            executable = stage / "bin/monitor-server"
            executable.write_text("#!" + sys.executable + "\n" + mutated)
            executable.chmod(0o700)
            (stage / "native_self_test.py").write_text(
                "def run_self_test(): return {'ok': True, 'frozen': True, 'nativeLibraries': {'checked': True}}\n")
            # TLS entry bootstrap is also real source, without network activity.
            if (ROOT / "packaging/native_tls.py").is_file():
                shutil.copyfile(ROOT / "packaging/native_tls.py", stage / "native_tls.py")
            self.assertTrue(builder.verify_candidate(executable, output, 10)["ok"])
            with self.assertRaisesRegex(RuntimeError, "Native validation failed"):
                builder.verify_http_stage(stage, output, Path(shutil.which("node")).resolve(), ROOT,
                                          invocation="original", expected_stage_digest="a" * 64)

    @unittest.skipUnless(importlib.util.find_spec("PyInstaller") and importlib.util.find_spec("mediapipe"),
                         "requires the actual locked native build environment")
    def test_real_mediapipe_collection_keeps_runtime_and_avoids_optional_converter(self):
        from PyInstaller.utils.hooks import collect_submodules

        # Execute just the spec's selection function, without running a build.
        parsed = ast.parse((ROOT / "packaging" / "monitor.spec").read_text())
        selection = next(node for node in parsed.body if isinstance(node, ast.FunctionDef) and node.name == "runtime_module")
        namespace = {}
        exec(compile(ast.Module(body=[selection], type_ignores=[]), "monitor.spec", "exec"), namespace)
        modules = collect_submodules("mediapipe", filter=namespace["runtime_module"], on_error="raise")
        for required in ("mediapipe.python.solutions.face_mesh", "mediapipe.python.solution_base",
                         "mediapipe.python._framework_bindings", "mediapipe.calculators.core.gate_calculator_pb2",
                         "mediapipe.tasks.python.genai", "mediapipe.tasks.python.vision.face_landmarker"):
            self.assertIn(required, modules)
        for tool in ("mediapipe.tasks.python.genai.converter", "mediapipe.model_maker"):
            self.assertFalse(any(name == tool or name.startswith(tool + ".") for name in modules))

    def test_lock_requires_exact_versions_and_hashes(self):
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "lock"
            rows = [f"{name}=={version} --hash=sha256:{'a' * 64}" for name, version in
                    {"PyInstaller": "6.22.3", "numpy": "1.26.4", "Pillow": "11.3.0", "mediapipe": "0.10.21"}.items()]
            path.write_text("\n".join(rows))
            self.assertEqual(builder.locked_versions(path)["pillow"], "11.3.0")
            for invalid in (rows + [rows[0]], [row.split(" --hash")[0] for row in rows],
                            rows + ["--extra-index-url https://example.invalid"],
                            [row.replace("0.10.21", "0.10.22") for row in rows]):
                path.write_text("\n".join(invalid))
                with self.assertRaises(ValueError):
                    builder.locked_versions(path)

    def test_repository_lock_can_be_parsed(self):
        versions = builder.locked_versions(ROOT / "packaging" / "requirements-macos-arm64.lock")
        self.assertIn("jaxlib", versions)
        self.assertIn("opencv-contrib-python", versions)
        self.assertIn("protobuf", versions)

    def test_environment_does_not_forward_credentials_or_python_fallback(self):
        with tempfile.TemporaryDirectory() as temporary:
            with mock.patch.dict(os.environ, {"SYA_WORK_CONTEXT_API_KEY": "private", "GH_TOKEN": "private",
                                            "PYTHONPATH": "/injected", "DYLD_LIBRARY_PATH": "/injected",
                                            "MONITOR_PYTHON": "/injected"}):
                env = builder.isolated_environment(Path(temporary) / "home", Path(temporary) / "tmp")
            for key in ("SYA_WORK_CONTEXT_API_KEY", "GH_TOKEN", "PYTHONPATH", "DYLD_LIBRARY_PATH", "MONITOR_PYTHON"):
                self.assertNotIn(key, env)
            self.assertEqual(env["PATH"], "")
            self.assertEqual(json.loads(Path(env["MONITOR_CONFIG_PATH"]).read_text()), {})

    def sdk_fixture(self, root):
        records = []
        for name in ("pyrealsense2.so", "pyrsutils.so", "librealsense2.2.54.2.dylib", "libusb-1.0.0.dylib"):
            path = root / name
            path.write_bytes(name.encode())
            records.append({"path": str(path), "sha256": builder.sha256(path)})
        value = {**builder.SDK_PINS, "architecture": "arm64", "pythonVersion": builder.platform.python_version(),
                 "modules": dict(zip(("pyrealsense2", "pyrsutils"), records[:2])), "libraries": records[2:]}
        path = root / "provenance.json"
        path.write_text(json.dumps(value))
        return path, value

    def test_sdk_bindings_reject_changed_bits_source_and_resolution(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            path, value = self.sdk_fixture(root)
            resolver = lambda name: types.SimpleNamespace(origin=value["modules"][name]["path"])
            with mock.patch.object(builder.importlib.util, "find_spec", side_effect=resolver):
                self.assertEqual(builder.verify_sdk(path), value)
                altered = {**value, "sdkCommit": "0" * 40}
                path.write_text(json.dumps(altered))
                with self.assertRaisesRegex(ValueError, "sdkCommit"):
                    builder.verify_sdk(path)
                path.write_text(json.dumps(value))
                Path(value["libraries"][0]["path"]).write_bytes(b"changed")
                with self.assertRaisesRegex(ValueError, "checksum"):
                    builder.verify_sdk(path)
            path, value = self.sdk_fixture(root)
            with mock.patch.object(builder.importlib.util, "find_spec", return_value=types.SimpleNamespace(origin="/different/module.so")):
                with self.assertRaisesRegex(ValueError, "resolution"):
                    builder.verify_sdk(path)

    def test_existing_output_is_never_overwritten(self):
        with tempfile.TemporaryDirectory() as temporary:
            output = Path(temporary)
            witness = output / "existing"
            witness.write_text("preserve")
            args = types.SimpleNamespace(requirements_lock="unused", sdk_provenance="unused", output=output)
            with mock.patch.object(builder, "verify_environment"), mock.patch.object(builder, "verify_sdk"):
                with self.assertRaisesRegex(ValueError, "overwrite"):
                    builder.build(args)
            self.assertEqual(witness.read_text(), "preserve")

    def test_output_rejects_source_aliases_git_and_user_configuration(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary) / "source"
            root.mkdir()
            alias = Path(temporary) / "source-alias"
            alias.symlink_to(root, target_is_directory=True)
            with mock.patch.object(builder, "ROOT", root):
                for unsafe in (root / "new", root / "build" / ".." / "new", alias / "new",
                               root / "build" / ".git" / "new", Path.home() / ".sya" / "new-native"):
                    with self.subTest(path=unsafe), self.assertRaises(ValueError):
                        builder.candidate_output(unsafe)
                self.assertEqual(builder.candidate_output(root / "build" / "new"), (root / "build" / "new").resolve())
                self.assertEqual(builder.candidate_output(root / "dist" / "new"), (root / "dist" / "new").resolve())
                self.assertEqual(builder.candidate_output(Path(temporary) / "new"), (Path(temporary) / "new").resolve())

    def test_failed_process_never_echoes_raw_loader_stderr(self):
        with tempfile.TemporaryDirectory() as temporary:
            env = builder.isolated_environment(Path(temporary) / "home", Path(temporary) / "tmp")
            with self.assertRaisesRegex(RuntimeError, "Native validation failed") as failure:
                builder.run_bounded([sys.executable, "-c", "import sys; sys.stderr.write('Library not loaded: missing.dylib'); sys.exit(1)"],
                                    env=env, cwd=temporary, timeout=5)
            self.assertNotIn("missing.dylib", str(failure.exception))

    def test_real_child_timeout_is_bounded(self):
        with tempfile.TemporaryDirectory() as temporary:
            env = builder.isolated_environment(Path(temporary) / "home", Path(temporary) / "tmp")
            with self.assertRaisesRegex(RuntimeError, "deadline"):
                builder.run_bounded([sys.executable, "-c", "import time; time.sleep(30)"],
                                    env=env, cwd=temporary, timeout=0.05)

    def test_self_test_entry_never_imports_server(self):
        fake_test = types.ModuleType("native_self_test")
        fake_test.run_self_test = mock.Mock(return_value={"ok": True})
        original_import = __import__

        def guarded_import(name, *args, **kwargs):
            if name == "server":
                raise AssertionError("Diagnostics imported the HTTP server")
            return original_import(name, *args, **kwargs)

        with mock.patch.dict(sys.modules, {"native_self_test": fake_test}), \
                mock.patch.object(sys, "argv", ["native", "--self-test"]), \
                mock.patch("builtins.__import__", side_effect=guarded_import), \
                contextlib.redirect_stdout(io.StringIO()) as output:
            self.assertEqual(entry.main(), 0)
        self.assertEqual(json.loads(output.getvalue()), {"ok": True})
        fake_test.run_self_test.assert_called_once_with()

    def test_self_test_failure_cannot_report_success(self):
        fake_test = types.ModuleType("native_self_test")
        fake_test.run_self_test = mock.Mock(side_effect=RuntimeError("Missing model"))
        with mock.patch.dict(sys.modules, {"native_self_test": fake_test}), \
                mock.patch.object(sys, "argv", ["native", "--self-test"]), \
                contextlib.redirect_stdout(io.StringIO()) as output:
            self.assertEqual(entry.main(), 1)
        self.assertFalse(json.loads(output.getvalue())["ok"])


class NativeShutdownTests(unittest.TestCase):
    def adapter(self):
        adapter = server.RealSenseAdapter()
        for method in ("status", "list_devices", "_usb_realsense_devices", "_records_dir"):
            setattr(adapter, method, mock.Mock(side_effect=AssertionError("Unexpected device/config access")))
        return adapter

    def test_inactive_shutdown_does_not_enumerate_devices_or_read_config(self):
        adapter = self.adapter()
        self.assertTrue(adapter.shutdown())
        self.assertTrue(adapter.stop_event.is_set())

    def test_shutdown_releases_only_acquired_resources(self):
        adapter = self.adapter()
        pipeline, detector = mock.Mock(), mock.Mock()
        adapter.pipeline, adapter.focus_detector, adapter.active = pipeline, detector, True
        adapter._publish_access_boundary = mock.Mock()
        adapter._boundary = mock.Mock()
        self.assertTrue(adapter.shutdown())
        pipeline.stop.assert_called_once_with()
        detector.close.assert_called_once_with()
        self.assertIsNone(adapter.pipeline)
        self.assertFalse(adapter.active)

    def test_detector_close_failure_is_reported_and_handle_retained(self):
        adapter = self.adapter()
        detector = mock.Mock()
        detector.close.side_effect = RuntimeError("Failed to close native graph")
        adapter.focus_detector = detector
        self.assertFalse(adapter.shutdown())
        self.assertIs(adapter.focus_detector, detector)
        self.assertIn("focus inference", adapter.last_error)

    def test_shutdown_latch_rejects_queued_and_future_start(self):
        adapter = self.adapter()
        adapter._load_sdk = mock.Mock(side_effect=AssertionError("SDK acquired after terminal shutdown"))
        errors = []

        def start():
            try:
                adapter.start()
            except Exception as error:
                errors.append(error)

        adapter.lifecycle_lock.acquire()
        starting = threading.Thread(target=start)
        stopping = threading.Thread(target=adapter.shutdown)
        try:
            starting.start()
            stopping.start()
            deadline = time.monotonic() + 1
            while not adapter._shutting_down and time.monotonic() < deadline:
                time.sleep(0.005)
            self.assertTrue(adapter._shutting_down)
        finally:
            adapter.lifecycle_lock.release()
        starting.join(1)
        stopping.join(1)
        self.assertFalse(starting.is_alive() or stopping.is_alive())
        self.assertEqual(len(errors), 1)
        self.assertIsInstance(errors[0], server.RealSenseActionError)
        with self.assertRaises(server.RealSenseActionError):
            adapter.start()
        adapter._load_sdk.assert_not_called()

    def test_failed_shutdown_retains_active_handle_without_enumerating(self):
        adapter = self.adapter()
        pipeline = mock.Mock()
        pipeline.stop.side_effect = RuntimeError("SDK failed to release")
        adapter.pipeline, adapter.active = pipeline, True
        adapter._publish_access_boundary = mock.Mock()
        adapter._boundary = mock.Mock()
        self.assertFalse(adapter.shutdown())
        self.assertIs(adapter.pipeline, pipeline)
        self.assertTrue(adapter.active)

    def run_main(self, shutdown, deadline=0.1):
        handlers = {}

        def signal_handler(number, handler):
            old = handlers.get(number, signal.SIG_DFL)
            handlers[number] = handler
            return old

        httpd = mock.MagicMock()
        httpd.__enter__.return_value = httpd
        httpd.handle_request.side_effect = lambda: handlers[signal.SIGTERM](signal.SIGTERM, None)
        with mock.patch.object(server, "ThreadedHTTPServer", return_value=httpd), \
                mock.patch.object(server, "load_api_config", return_value={"path": "", "key": ""}), \
                mock.patch.object(server, "resolve_port", return_value=0), \
                mock.patch.object(server.signal, "signal", side_effect=signal_handler), \
                mock.patch.object(server.REALSENSE, "shutdown", side_effect=shutdown), \
                contextlib.redirect_stdout(io.StringIO()):
            server.main(shutdown_timeout=deadline)
        self.assertEqual(handlers, {signal.SIGTERM: signal.SIG_DFL, signal.SIGINT: signal.SIG_DFL})

    def test_sigterm_runs_no_discovery_shutdown_and_restores_handlers(self):
        shutdown = mock.Mock(return_value=True)
        self.run_main(shutdown)
        shutdown.assert_called_once_with()

    def test_main_shutdown_failure_is_not_successful_exit(self):
        with self.assertRaisesRegex(RuntimeError, "shutdown"):
            self.run_main(lambda: False)

    def test_main_shutdown_deadline_is_bounded(self):
        release = threading.Event()
        try:
            with self.assertRaisesRegex(RuntimeError, "deadline"):
                self.run_main(lambda: release.wait(30), deadline=0.01)
        finally:
            release.set()

    def test_real_server_sigterm_exits_without_device_discovery(self):
        with tempfile.TemporaryDirectory() as temporary:
            with socket.socket() as reservation:
                reservation.bind(("127.0.0.1", 0))
                port = reservation.getsockname()[1]
            env = builder.isolated_environment(Path(temporary) / "home", Path(temporary) / "tmp")
            env["PORT"] = str(port)
            code = (
                "import sys; sys.path.insert(0, sys.argv[1]); import server\n"
                "def forbidden(*args, **kwargs): raise AssertionError('Device/config discovery during shutdown')\n"
                "server.REALSENSE.status = forbidden\n"
                "server.REALSENSE.list_devices = forbidden\n"
                "server.REALSENSE._usb_realsense_devices = forbidden\n"
                "server.REALSENSE._records_dir = forbidden\n"
                "server.main()\n"
            )
            process = subprocess.Popen([sys.executable, "-c", code, str(ROOT)], env=env, cwd=temporary,
                                       stdout=subprocess.PIPE, stderr=subprocess.PIPE)
            try:
                opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
                deadline = time.monotonic() + 5
                while True:
                    try:
                        with opener.open(f"http://127.0.0.1:{port}/health", timeout=0.2) as response:
                            self.assertEqual(response.status, 200)
                        break
                    except (urllib.error.URLError, TimeoutError):
                        if time.monotonic() >= deadline or process.poll() is not None:
                            self.fail("The isolated real HTTP service did not start.")
                        time.sleep(0.02)
                process.send_signal(signal.SIGTERM)
                _out, error = process.communicate(timeout=3)
                self.assertEqual(process.returncode, 0, error.decode())
            finally:
                if process.poll() is None:
                    process.kill()
                process.communicate(timeout=3)


if __name__ == "__main__":
    unittest.main()
