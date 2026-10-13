#!/usr/bin/env python3
"""Build a local macOS arm64 onefile candidate from an explicit locked venv.

No downloads, SDK installation, signing identity discovery, publishing, or artifact
lock updates occur here. Production provenance/OS support are separate gates.
"""

import argparse
import hashlib
import importlib.metadata
import importlib.util
import json
import os
from pathlib import Path
import platform
import re
import shutil
import signal
import subprocess
import sys
import tempfile
import threading
import time


ROOT = Path(__file__).resolve().parents[1]
SDK_PINS = {
    "sdkVersion": "2.54.2", "sdkCommit": "2dbaaf5964490cb02f811cf2ed38c8a893f5c027",
    "pybind11Commit": "0bd8896a4010f2d91b2340570c24fa08606ec406",
    "patchSha256": "eb3acdba968cb716133a176a0051af7d06568594cd3b1d8a65f6d3b1202317c1",
}
SOURCE_FILES = (
    "server.py", "realsense_runtime.py", "eye_observation.py", "startup_trace.py", "index.html", "config.example.json",
    "manifest.json", "module.json", "api.openapi.json", "package.json",
    "packaging/native_entry.py", "packaging/native_self_test.py", "packaging/native_tls.py",
    "packaging/native_inputs.py", "packaging/startup_hook.py", "packaging/monitor.spec",
    "scripts/build-native-runtime.py", "scripts/test-staged-runtime.mjs", "scripts/hash-runtime.mjs", "scripts/startup-trace.mjs", "scripts/startup-diagnostics.mjs",
)


def build_options(mode="off"):
    if mode not in ("off", "bootloader-importtime"):
        raise ValueError("Invalid startup diagnostics mode.")
    return {"schemaVersion": 1, "startupDiagnostics": mode}


def source_snapshot(output, lock, provenance, node, mode="off"):

    """Freeze only explicit build/validation inputs, never a repository tree."""
    snapshot = Path(output) / "source-snapshot"
    options_path = Path(output) / "build-options.json"
    options_path.parent.mkdir(parents=True, exist_ok=True)
    options_path.write_text(json.dumps(build_options(mode), sort_keys=True, separators=(",", ":")) + "\n")
    paths = {name: ROOT / name for name in SOURCE_FILES}
    paths.update({"inputs/requirements.lock": Path(lock), "inputs/sdk-provenance.json": Path(provenance), "inputs/build-options.json": options_path})
    bindings = {}
    for name, source in paths.items():
        if not source.is_file() or source.is_symlink():
            raise ValueError("Snapshot input must be a regular file: " + name)
        data = source.read_bytes()
        digest = hashlib.sha256(data).hexdigest()
        destination = snapshot / name
        destination.parent.mkdir(parents=True, exist_ok=True)
        destination.write_bytes(data)
        bindings[name] = {"path": str(source.absolute()), "sha256": digest}
    bindings["external/node-executable"] = {"path": str(node), "sha256": sha256(node)}
    verify_input_bindings(snapshot, bindings)
    fingerprint = hashlib.sha256(json.dumps({name: item["sha256"] for name, item in bindings.items()
                                            if not name.startswith("external/")},
                                            sort_keys=True, separators=(",", ":")).encode()).hexdigest()
    return snapshot, bindings, fingerprint


def verify_input_bindings(snapshot, bindings):
    expected = set(SOURCE_FILES) | {"inputs/requirements.lock", "inputs/sdk-provenance.json", "inputs/build-options.json"}
    if set(bindings) != expected | {"external/node-executable"}:
        raise RuntimeError("Build input binding file set changed.")
    options = json.loads((snapshot / "inputs/build-options.json").read_text())
    if options != build_options(options.get("startupDiagnostics")) or type(options.get("schemaVersion")) is not int:
        raise RuntimeError("Invalid build options schema.")
    if Path(bindings["inputs/build-options.json"]["path"]).resolve() == (snapshot / "inputs/build-options.json").resolve():
        raise RuntimeError("Build options require independent live and frozen inputs.")
    actual = set()
    for path in snapshot.rglob("*"):
        if path.is_symlink():
            raise RuntimeError("Build input snapshot contains a symlink.")
        if path.is_file():
            actual.add(path.relative_to(snapshot).as_posix())
    if actual != expected:
        raise RuntimeError("Build input snapshot file set changed.")
    for name, item in bindings.items():
        source = Path(item["path"])
        if source.is_symlink() or not source.is_file() or sha256(source) != item["sha256"]:
            raise RuntimeError("Live build input changed: " + name)
        if not name.startswith("external/"):
            frozen = snapshot / name
            if frozen.is_symlink() or not frozen.is_file() or sha256(frozen) != item["sha256"]:
                raise RuntimeError("Build input snapshot changed: " + name)


def dependency_verifier(snapshot):
    spec = importlib.util.spec_from_file_location("sya_native_snapshot_inputs", snapshot / "packaging/native_inputs.py")
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    previous_bytecode = sys.dont_write_bytecode
    try:
        sys.dont_write_bytecode = True
        spec.loader.exec_module(module)
    finally:
        sys.dont_write_bytecode = previous_bytecode
    return module.verify_dependencies


def absolute_input(path, *, directory=False):
    supplied = Path(path)
    if not supplied.is_absolute():
        raise ValueError("Build tool/wheelhouse inputs must be explicit absolute paths.")
    resolved = supplied.resolve()
    if directory:
        if not resolved.is_dir():
            raise ValueError("Wheelhouse must be an existing directory.")
    elif not resolved.is_file() or not os.access(resolved, os.X_OK):
        raise ValueError("Node executable must be an existing executable file.")
    return resolved


def sha256(path):
    digest = hashlib.sha256()
    with Path(path).open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def canonical_name(name):
    return re.sub(r"[-_.]+", "-", name).lower()


def candidate_output(path):
    """Only new task-owned temp/build outputs; aliases cannot enter source/config."""
    lexical = Path(os.path.abspath(Path(path).expanduser()))
    resolved = lexical.resolve()
    source = ROOT.resolve()
    build_roots = (source / "build", source / "dist")
    temporary_roots = [Path("/tmp").resolve()]
    if sys.platform == "darwin":
        # Python's confstr_names does not expose Apple's private constant.
        # Query the OS tool, without trusting an inherited TMPDIR override.
        os_temp = subprocess.run(["/usr/bin/getconf", "DARWIN_USER_TEMP_DIR"],
                                 check=True, capture_output=True, text=True, timeout=5,
                                 env={"PATH": "/usr/bin:/bin"}).stdout.strip()
        if not os_temp or not Path(os_temp).is_absolute():
            raise ValueError("Cannot resolve the OS-provided temporary directory.")
        temporary_roots.append(Path(os_temp).resolve())

    def below(value, root):
        return root in value.parents

    config_roots = tuple((Path.home() / suffix).resolve() for suffix in
                         (".sya", ".config", "Library/Application Support"))
    if any(resolved == root or below(resolved, root) for root in config_roots):
        raise ValueError("Candidate output must not enter user configuration directories.")
    if any(part.casefold() == ".git" for part in lexical.parts + resolved.parts):
        raise ValueError("Candidate output must not touch Git metadata.")
    for value in (lexical, resolved):
        if (value == source or below(value, source)) and not any(below(value, root) for root in build_roots):
            raise ValueError("Candidate output must not enter the source input tree.")
    if not any(below(resolved, root) for root in (*temporary_roots, *build_roots)):
        raise ValueError("Candidate output must be a dedicated temp, build, or dist subdirectory.")
    if lexical.exists() or lexical.is_symlink() or resolved.exists():
        raise ValueError("Refusing to overwrite an existing candidate output.")
    return resolved


def locked_versions(path):
    """Read the strict, hash-bearing pip lock without evaluating requirements."""
    content = Path(path).read_text(encoding="utf-8").replace("\\\n", " ")
    versions = {}
    for line in content.splitlines():
        line = line.split("#", 1)[0].strip()
        if not line:
            continue
        match = re.fullmatch(r"([A-Za-z0-9][A-Za-z0-9._-]*)==([^\s;]+)((?:\s+--hash=sha256:[0-9a-f]{64})+)", line)
        if not match:
            raise ValueError("The native dependency lock must contain only hash-pinned versions.")
        name = canonical_name(match[1])
        if name in versions:
            raise ValueError("Duplicate package in native dependency lock: " + name)
        versions[name] = match[2]
    for name, version in {"pyinstaller": "6.22.3", "numpy": "1.26.4", "pillow": "11.3.0", "mediapipe": "0.10.21"}.items():
        if versions.get(name) != version:
            raise ValueError("Missing pinned native dependency: " + name)
    return versions


def verify_environment(lock):
    if sys.platform != "darwin" or platform.machine() != "arm64" or sys.version_info[:2] != (3, 11):
        raise RuntimeError("Use an explicit native macOS arm64 Python 3.11 build environment.")
    if sys.prefix == sys.base_prefix:
        raise RuntimeError("Use an isolated venv populated from the native hash lock.")
    versions = locked_versions(lock)
    for name, version in versions.items():
        if importlib.metadata.version(name) != version:
            raise RuntimeError("The build environment differs from the lock: " + name)
    return versions


def verify_sdk(provenance):
    sdk = json.loads(Path(provenance).read_text(encoding="utf-8"))
    for key, expected in SDK_PINS.items():
        if sdk.get(key) != expected:
            raise ValueError("SDK provenance has an unpinned " + key)
    if sdk.get("architecture") != "arm64" or sdk.get("pythonVersion") != platform.python_version():
        raise ValueError("SDK provenance architecture/Python do not match the build environment.")
    modules = sdk.get("modules")
    libraries = sdk.get("libraries")
    if not isinstance(modules, dict) or set(modules) != {"pyrealsense2", "pyrsutils"}:
        raise ValueError("SDK provenance must bind both native Python modules.")
    if not isinstance(libraries, list) or len(libraries) < 2:
        raise ValueError("SDK provenance must bind librealsense and libusb.")
    bound_paths = set()
    for record in [*modules.values(), *libraries]:
        if not isinstance(record, dict) or not isinstance(record.get("path"), str):
            raise ValueError("Invalid SDK file binding.")
        path = Path(record["path"])
        if not path.is_absolute() or not path.is_file() or path.is_symlink():
            raise ValueError("SDK file binding must name an existing absolute regular file.")
        if not re.fullmatch(r"[a-f0-9]{64}", str(record.get("sha256", ""))) or sha256(path) != record["sha256"]:
            raise ValueError("SDK file binding checksum mismatch.")
        if path.resolve() in bound_paths:
            raise ValueError("Duplicate SDK file binding.")
        bound_paths.add(path.resolve())
    names = [Path(record["path"]).name for record in libraries]
    if not any(name.startswith("librealsense2") for name in names) or not any(name.startswith("libusb-1.0") for name in names):
        raise ValueError("SDK bindings are missing librealsense or libusb.")
    for name, record in modules.items():
        spec = importlib.util.find_spec(name)
        if spec is None or spec.origin is None or Path(spec.origin).resolve() != Path(record["path"]).resolve():
            raise ValueError("Native SDK module resolution differs from provenance: " + name)
    return sdk


def isolated_environment(home, temporary, *, build=False):
    # An allow-list prevents provider/release credentials and Python/dyld/user-site
    # overrides from reaching either Analysis or the native diagnostic process.
    home, temporary = Path(home), Path(temporary)
    home.mkdir(parents=True, exist_ok=True)
    temporary.mkdir(parents=True, exist_ok=True)
    config = home / "config.json"
    config.write_text("{}\n", encoding="utf-8")
    config.chmod(0o600)
    return {
        "HOME": str(home), "TMPDIR": str(temporary), "TMP": str(temporary), "TEMP": str(temporary),
        "PATH": str(Path(sys.executable).parent) + ":/usr/bin:/bin:/usr/sbin:/sbin" if build else "",
        "LANG": "en_US.UTF-8", "LC_ALL": "en_US.UTF-8", "PYTHONNOUSERSITE": "1",
        "PYTHONDONTWRITEBYTECODE": "1", "MPLBACKEND": "Agg", "MPLCONFIGDIR": str(home / "matplotlib"),
        "MONITOR_CONFIG_PATH": str(config), "MONITOR_RECORDS_DIR": str(home / "records"),
    }


def process_group_exists(pid):
    try:
        os.killpg(pid, 0)
        return True
    except ProcessLookupError:
        return False
    except PermissionError:
        # Darwin can briefly report EPERM for a group whose leader is a zombie.
        # Treat that as still present, never as proof of successful cleanup.
        return True


def wait_owned_group(process, timeout):
    deadline = time.monotonic() + timeout
    while True:
        process.poll()  # Reap our direct child, even if grandchildren outlive it.
        if not process_group_exists(process.pid) and process.returncode is not None:
            return True
        if time.monotonic() >= deadline:
            return False
        time.sleep(0.025)


def cleanup_owned_group(process, term_timeout=5, kill_timeout=5):
    previous = {}
    if threading.current_thread() is threading.main_thread():
        for signum in (signal.SIGINT, signal.SIGTERM):
            previous[signum] = signal.signal(signum, signal.SIG_IGN)
    try:
        for signum, timeout in ((signal.SIGTERM, term_timeout), (signal.SIGKILL, kill_timeout)):
            try:
                os.killpg(process.pid, signum)
            except (ProcessLookupError, PermissionError):
                pass  # The bounded group check below remains authoritative.
            if wait_owned_group(process, timeout):
                process.wait(timeout=1)
                return
        raise RuntimeError("Owned native process group did not exit after TERM/KILL.")
    finally:
        for signum, handler in previous.items():
            signal.signal(signum, handler)


def run_bounded(command, *, env, cwd, timeout, log=None, stderr_chunks=None, lifecycle=None):
    if not log:
        return run_bounded_capture(command, env=env, cwd=cwd, timeout=timeout, stderr_chunks=stderr_chunks, lifecycle=lifecycle)

    state = lifecycle if lifecycle is not None else {}
    started = time.monotonic()
    state.update({"deadlineMs": timeout * 1000, "failure": None, "cleanupFailure": None, "cleanup": "not_spawned"})
    try:
        with Path(log).open("wb") as stream:
            try:
                process = subprocess.Popen(command, cwd=cwd, env=env, stdout=stream,
                                           stderr=subprocess.STDOUT, start_new_session=True)
            except Exception:
                state["failure"] = "spawn"
                raise NativeValidationError("spawn") from None
            state.update({"pid": process.pid, "processGroup": process.pid, "cleanup": "pending"})
            try:
                process.communicate(timeout=timeout)
                if process.returncode:
                    raise NativeValidationError("nonzero")
                if not wait_owned_group(process, 0.25):
                    raise NativeValidationError("cleanup")
                state["cleanup"] = "group_gone"
                return ""
            except BaseException as error:
                failure = NativeValidationError("timeout") if isinstance(error, subprocess.TimeoutExpired) else error
                state["failure"] = getattr(failure, "code", "interrupted")
                try:
                    cleanup_owned_group(process)
                    state["cleanup"] = "group_gone"
                except BaseException:
                    state["cleanup"] = "failed"
                    state["cleanupFailure"] = "cleanup"
                raise failure
            finally:
                state["exitCode"] = process.returncode
    finally:
        state["elapsedMs"] = (time.monotonic() - started) * 1000


class NativeValidationError(RuntimeError):
    def __init__(self, code):
        self.code = code
        messages = {"spawn": "Native validation spawn failed.", "timeout": "Native build/diagnostic process exceeded its deadline.",
                    "nonzero": "Native validation failed (nonzero exit).", "cleanup": "Native command left running descendants or cleanup failed.",
                    "reader": "Native diagnostic pipe reader failed.", "stdout_limit": "Native validation stdout exceeded its limit.",
                    "json": "Native validation JSON failed.", "contract": "Native self-test contract failed.",
                    "extraction": "Native extraction directory cleanup failed.", "parser": "Startup diagnostic parser failed.",
                    "coverage": "Startup diagnostic coverage failed."}
        super().__init__(messages[code])


def run_bounded_capture(command, *, env, cwd, timeout, stderr_chunks=None, lifecycle=None):
    # Readers retain bounded data but count every byte actually received.
    state = lifecycle if lifecycle is not None else {}
    started = time.monotonic()
    state.update({"pid": None, "processGroup": None, "cleanup": "not_spawned", "exitCode": None,
                  "deadlineMs": timeout * 1000, "elapsedMs": 0, "failure": None,
                  "cleanupFailure": None, "readerFailures": [], "pipesDrained": False,
                  "receivedBytes": {"stdout": 0, "stderr": 0}, "omittedBytes": {"stdout": 0, "stderr": 0}})
    output = bytearray()
    retained_stderr = 0
    reader_failures = []
    try:
        process = subprocess.Popen(command, cwd=cwd, env=env, stdout=subprocess.PIPE,
                                   stderr=subprocess.PIPE, start_new_session=True)
    except Exception:
        state.update({"failure": "spawn", "elapsedMs": (time.monotonic() - started) * 1000})
        raise NativeValidationError("spawn") from None
    state.update({"pid": process.pid, "processGroup": process.pid, "cleanup": "pending"})

    def drain(stream, name):
        nonlocal retained_stderr
        try:
            while True:
                chunk = stream.read1(8192)
                if not chunk:
                    break
                state["receivedBytes"][name] += len(chunk)
                if name == "stdout":
                    keep = min(len(chunk), max(0, 4 * 1024 * 1024 - len(output)))
                    output.extend(chunk[:keep])
                else:
                    keep = min(len(chunk), max(0, 4 * 1024 * 1024 - retained_stderr)) if stderr_chunks is not None else 0
                    if keep:
                        stderr_chunks.append({"ms": (time.monotonic() - started) * 1000,
                                              "text": chunk[:keep].decode("ascii", errors="replace")})
                        retained_stderr += keep
                state["omittedBytes"][name] += len(chunk) - keep
        except Exception:
            reader_failures.append(name + "_read")
        finally:
            try:
                stream.close()
            except Exception:
                reader_failures.append(name + "_close")

    threads = [threading.Thread(target=drain, args=(process.stdout, "stdout"), daemon=True),
               threading.Thread(target=drain, args=(process.stderr, "stderr"), daemon=True)]
    for thread in threads:
        thread.start()
    failure = None
    try:
        process.wait(timeout=timeout)
        if process.returncode:
            raise NativeValidationError("nonzero")
        if not wait_owned_group(process, 0.25):
            raise NativeValidationError("cleanup")
        state["cleanup"] = "group_gone"
    except BaseException as error:
        failure = NativeValidationError("timeout") if isinstance(error, subprocess.TimeoutExpired) else error
        state["failure"] = getattr(failure, "code", "interrupted")
        try:
            cleanup_owned_group(process)
            state["cleanup"] = "group_gone"
        except BaseException:
            state["cleanup"] = "failed"
            state["cleanupFailure"] = "cleanup"
    finally:
        for thread in threads:
            thread.join(timeout=2)
        state.update({"exitCode": process.returncode, "pipesDrained": not any(thread.is_alive() for thread in threads),
                      "readerFailures": sorted(set(reader_failures)), "elapsedMs": (time.monotonic() - started) * 1000})
    if not failure and (reader_failures or not state["pipesDrained"]):
        failure = NativeValidationError("reader")
    if not failure and state["omittedBytes"]["stdout"]:
        failure = NativeValidationError("stdout_limit")
    if failure:
        state["failure"] = getattr(failure, "code", "interrupted")
        raise failure
    try:
        return output.decode("utf-8")
    except UnicodeError:
        state["failure"] = "json"
        raise NativeValidationError("json") from None


def parse_diagnostics(chunks, node, snapshot, mode, *, expected_parent_pid=None):
    program = """
import fs from 'node:fs';
import { pathToFileURL } from 'node:url';
const { createStartupDiagnosticsCollector, startupDiagnosticsCoverage } = await import(pathToFileURL(process.argv[1]).href);
let ms = 0;
const collector = createStartupDiagnosticsCollector({ nowMs: () => ms, mode: process.argv[2] });
for (const chunk of JSON.parse(fs.readFileSync(0, 'utf8'))) { ms = chunk.ms; collector.push(chunk.text); }
const result = collector.finish();
result.coverage = startupDiagnosticsCoverage(result, { mode: process.argv[2], expectedParentPid: JSON.parse(process.argv[3]) });
console.log(JSON.stringify(result));
"""
    result = subprocess.run([str(node), "--input-type=module", "-e", program,
                             str(snapshot / "scripts/startup-diagnostics.mjs"), mode, json.dumps(expected_parent_pid)],
                            input=json.dumps(chunks), capture_output=True, text=True, timeout=15,
                            env={"PATH": ""})
    if result.returncode:
        raise RuntimeError("Startup diagnostic parser failed.")
    return json.loads(result.stdout)


def verify_candidate(executable, output, timeout, *, node=None, snapshot=None, mode="off", invocation="original"):
    with tempfile.TemporaryDirectory(prefix="native-self-test-", dir=output) as scratch:
        scratch = Path(scratch)
        env = isolated_environment(scratch / "home", scratch / "tmp")
        launched = time.monotonic()
        chunks = []
        evidence = {"invocation": invocation, "status": "failed", "deadlineMs": timeout * 1000,
                    "lifecycle": {}, "failure": None, "parserFailure": None, "startupDiagnostics": None}
        failure = None
        result = None
        process_wall_ms = None
        try:
            text = run_bounded([str(executable), "--self-test"], env=env, cwd=scratch, timeout=timeout,
                               stderr_chunks=chunks, lifecycle=evidence["lifecycle"])
            process_wall_ms = round((time.monotonic() - launched) * 1000)
            try:
                result = json.loads(text)
            except (ValueError, TypeError):
                raise NativeValidationError("json") from None
            if (not isinstance(result, dict) or result.get("ok") is not True or result.get("frozen") is not True
                    or not isinstance(result.get("nativeLibraries"), dict) or result["nativeLibraries"].get("checked") is not True):
                raise NativeValidationError("contract")
            if list((scratch / "tmp").glob("_MEI*")):
                raise NativeValidationError("extraction")
        except BaseException as error:
            failure = error
            evidence["failure"] = getattr(error, "code", "interrupted" if isinstance(error, (KeyboardInterrupt, SystemExit)) else "contract")
        finally:
            evidence["externalLaunchToExitMs"] = process_wall_ms if process_wall_ms is not None else round((time.monotonic() - launched) * 1000)
            evidence["elapsedMs"] = evidence["externalLaunchToExitMs"]
            if node and snapshot:
                try:
                    diagnostic = parse_diagnostics(chunks, node, snapshot, mode,
                                                   expected_parent_pid=evidence["lifecycle"].get("pid"))
                    diagnostic["transportReceivedBytes"] = evidence["lifecycle"].get("receivedBytes", {}).get("stderr", 0)
                    diagnostic["transportOmittedBytes"] = evidence["lifecycle"].get("omittedBytes", {}).get("stderr", 0)
                    if diagnostic["transportOmittedBytes"]:
                        diagnostic["status"] = "incomplete"
                        diagnostic["reasonCodes"].append("transport_byte_limit")
                        diagnostic["coverage"].update({"ok": False, "status": "failed"})
                        diagnostic["coverage"]["reasonCodes"].append("transport_truncated")
                    evidence["startupDiagnostics"] = diagnostic
                    if mode != "off" and (diagnostic["transportOmittedBytes"] or not diagnostic["coverage"]["ok"]):
                        if failure is None:
                            failure = NativeValidationError("coverage")
                            evidence["failure"] = "coverage"
                except Exception:
                    evidence["parserFailure"] = "parser"
                    if failure is None:
                        failure = NativeValidationError("parser")
                        evidence["failure"] = "parser"
            elif mode != "off" and failure is None:
                failure = NativeValidationError("parser")
                evidence["failure"] = evidence["parserFailure"] = "parser"
            evidence["status"] = "failed" if failure else "passed"
            # Lifecycle and original fixed failure survive parser failure.
            try:
                (Path(output) / ("selftest-startup-" + invocation + ".json")).write_text(json.dumps(evidence, indent=2) + "\n")
            except OSError:
                if failure is None:
                    failure = RuntimeError("Self-test structured diagnostic persistence failed.")
        if failure:
            raise failure
        return {**result, **evidence}


def verify_http_stage(stage, output, node, snapshot, *, invocation, expected_stage_digest):
    """Use the existing contract/auth/proof/SSE smoke on the declared executable."""
    if invocation not in ("original", "relocated") or not re.fullmatch(r"[0-9a-f]{64}", expected_stage_digest):
        raise ValueError("Native HTTP evidence requires a fixed invocation and verified stage digest.")
    program = """
import fs from 'node:fs';
import { pathToFileURL } from 'node:url';
const pending = JSON.parse(fs.readFileSync(process.argv[4], 'utf8'));
const { testStagedRuntime } = await import(pathToFileURL(process.argv[2]).href);
const abort = new AbortController();
const stop = () => abort.abort();
process.once('SIGINT', stop); process.once('SIGTERM', stop);
try {
  const result = await testStagedRuntime(process.argv[3], {
    startupTimeoutMs: 30000, requestTimeoutMs: 1500, stopTimeoutMs: 2000,
    signal: abort.signal, collectStartupTrace: true, startupDiagnosticsMode: pending.startupDiagnosticsMode,
  });
  if (result.startupTrace?.status !== 'complete') {
    const error = new Error('Native startup trace is not complete.');
    error.startupTrace = result.startupTrace; error.startupDiagnostics = result.startupDiagnostics; throw error;
  }
  if (pending.startupDiagnosticsMode !== 'off') {
    const { startupDiagnosticsCoverage } = await import(new URL('./startup-diagnostics.mjs', pathToFileURL(process.argv[2])));
    const coverage = startupDiagnosticsCoverage(result.startupDiagnostics, { mode: pending.startupDiagnosticsMode });
    if (result.startupDiagnostics) result.startupDiagnostics.coverage = coverage;
    if (!coverage.ok) {
      const error = new Error('Native diagnostic coverage failed.');
      error.startupTrace = result.startupTrace; error.startupDiagnostics = result.startupDiagnostics; throw error;
    }
  }
  // Only the supervising Python validator may finalize this as passed.
  fs.writeFileSync(process.argv[4], JSON.stringify({ ...pending, status: 'collected', startupTrace: result.startupTrace, result }, null, 2));
  console.log(JSON.stringify(result));
} catch (error) {
  fs.writeFileSync(process.argv[4], JSON.stringify({ ...pending, status: 'failed', error: error.message, startupDiagnostics: error.startupDiagnostics ?? null, startupTrace: error.startupTrace ?? null }, null, 2));
  console.error(error.message); process.exitCode = 1;
}
finally { process.removeListener('SIGINT', stop); process.removeListener('SIGTERM', stop); }
"""
    with tempfile.TemporaryDirectory(prefix="native-http-test-", dir=output) as scratch:
        scratch = Path(scratch)
        env = isolated_environment(scratch / "home", scratch / "tmp")
        started = time.monotonic()
        # Preserve each invocation independently, including incomplete traces
        # and failures; relocation must not overwrite original-start evidence.
        descriptor, evidence_path = tempfile.mkstemp(prefix="http-startup-", suffix=".json", dir=output)
        binding = {"invocation": invocation, "expectedRuntimeSha256": expected_stage_digest,
                   "timeoutScope": "builder-diagnostic", "startupTimeoutMs": 30000, "startupDiagnosticsMode": json.loads((snapshot / "inputs/build-options.json").read_text())["startupDiagnostics"] if (snapshot / "inputs/build-options.json").exists() else "off"}
        pending = {**binding, "status": "running", "startupTrace": None}
        with os.fdopen(descriptor, "w", encoding="utf-8") as evidence:
            json.dump(pending, evidence)
        # Keep argv[1] distinct from the imported helper: its CLI guard must
        # remain false when this wrapper calls the exported function once.
        failure_message = "HTTP startup wrapper did not complete."
        try:
            text = run_bounded([str(node), "--input-type=module", "-e", program, "sya-native-http-wrapper",
                                str(snapshot / "scripts/test-staged-runtime.mjs"), str(stage), evidence_path],
                               env=env, cwd=scratch, timeout=180)
            failure_message = "The native HTTP stage was not verified."
            result = json.loads(text)
            if (not isinstance(result, dict) or result.get("runtimeSha256") != expected_stage_digest
                    or result.get("sseHandshake") is not True
                    or type(result.get("authenticatedOperations")) is not int or result["authenticatedOperations"] < 1
                    or not isinstance(result.get("startupTrace"), dict)
                    or result["startupTrace"].get("status") != "complete"):
                raise RuntimeError(failure_message)
            Path(evidence_path).write_text(json.dumps({**binding, "status": "passed",
                "startupTrace": result["startupTrace"], "result": result}) + "\n", encoding="utf-8")
        except BaseException:
            # The wrapper normally persists its sanitized failure after cleanup.
            # Abrupt termination/import failure must still leave readable failed
            # evidence, without claiming an unavailable partial trace exists.
            try:
                try:
                    record = json.loads(Path(evidence_path).read_text())
                except (OSError, ValueError):
                    record = pending
                if not isinstance(record, dict):
                    record = pending
                # Keep any collected result/trace when supervision or Python
                # validation fails, and retain the helper-owned identity fields.
                Path(evidence_path).write_text(json.dumps({**pending, **record, **binding, "status": "failed",
                    "error": record.get("error") or failure_message}) + "\n", encoding="utf-8")
            except OSError:
                pass  # Do not replace the original startup/interruption failure.
            raise
        return {**result, "externalHttpValidationWallMs": round((time.monotonic() - started) * 1000),
                "startupTimeoutMs": 30000, "timeoutScope": "builder-diagnostic", "stopTimeoutMs": 2000,
                "startupEvidenceFile": Path(evidence_path).name}


def stage_digest(stage, output, node, snapshot):
    with tempfile.TemporaryDirectory(prefix="native-stage-hash-", dir=output) as scratch:
        scratch = Path(scratch)
        env = isolated_environment(scratch / "home", scratch / "tmp")
        result = json.loads(run_bounded([str(node), str(snapshot / "scripts/hash-runtime.mjs"), str(stage), "--json"],
                                        env=env, cwd=scratch, timeout=30))
        value = result.get("runtimeSha256")
        if not re.fullmatch(r"[a-f0-9]{64}", str(value)):
            raise RuntimeError("Cannot bind the initial native stage digest.")
        return value


def build(args):
    output = candidate_output(args.output)
    # The helper, Analysis subprocess, and smoke must not mutate installed
    # bytecode after the complete dependency fingerprint is established.
    sys.dont_write_bytecode = True
    lock = Path(args.requirements_lock).resolve()
    provenance = Path(args.sdk_provenance).resolve()
    node = absolute_input(args.node_executable)
    wheelhouse = absolute_input(args.wheelhouse, directory=True)
    output.mkdir(parents=True, mode=0o700)
    snapshot, bindings, snapshot_digest = source_snapshot(output, lock, provenance, node, args.startup_diagnostics)
    frozen_lock = snapshot / "inputs/requirements.lock"
    frozen_provenance = snapshot / "inputs/sdk-provenance.json"
    versions = verify_environment(frozen_lock)
    verify_dependencies = dependency_verifier(snapshot)
    dependencies = verify_dependencies(frozen_lock, wheelhouse)
    if dependencies["versions"] != versions:
        raise RuntimeError("Dependency byte verification and version lock disagree.")
    sdk = verify_sdk(frozen_provenance)
    stage = output / "stage"
    (stage / "bin").mkdir(parents=True)
    for name in ("manifest.json", "module.json", "api.openapi.json", "package.json"):
        shutil.copyfile(snapshot / name, stage / name)
    env = isolated_environment(output / "build-home", output / "build-tmp", build=True)
    env["SYA_NATIVE_SDK_PROVENANCE"] = str(frozen_provenance)
    run_bounded([sys.executable, "-m", "PyInstaller", "--clean", "--noconfirm",
                 "--distpath", str(stage / "bin"), "--workpath", str(output / "work"),
                 str(snapshot / "packaging" / "monitor.spec")],
                env=env, cwd=snapshot, timeout=args.build_timeout, log=output / "build.log")
    executable = stage / "bin" / "monitor-server"
    if not executable.is_file() or executable.is_symlink() or not os.access(executable, os.X_OK):
        raise RuntimeError("PyInstaller did not produce the expected native executable.")
    initial_stage_digest = stage_digest(stage, output, node, snapshot)
    self_test = verify_candidate(executable, output, args.self_test_timeout, node=node, snapshot=snapshot, mode=args.startup_diagnostics)
    http_test = verify_http_stage(stage, output, node, snapshot, invocation="original", expected_stage_digest=initial_stage_digest)
    if http_test["runtimeSha256"] != initial_stage_digest:
        raise RuntimeError("Native stage changed during dependency validation.")
    with tempfile.TemporaryDirectory(prefix="中文 路径 ", dir=output) as relocated:
        relocated_stage = Path(relocated) / "stage"
        shutil.copytree(stage, relocated_stage)
        relocated_test = verify_candidate(relocated_stage / "bin" / "monitor-server", output, args.self_test_timeout, node=node, snapshot=snapshot, mode=args.startup_diagnostics, invocation="relocated")
        relocated_http = verify_http_stage(relocated_stage, output, node, snapshot,
                                           invocation="relocated", expected_stage_digest=initial_stage_digest)
        if relocated_http["runtimeSha256"] != http_test["runtimeSha256"]:
            raise RuntimeError("Relocated native stage differs from the verified original stage.")
    # Bind only inputs that remained unchanged through actual frozen validation.
    verify_input_bindings(snapshot, bindings)
    final_dependencies = verify_dependencies(frozen_lock, wheelhouse)
    if verify_sdk(frozen_provenance) != sdk or final_dependencies["fingerprint"] != dependencies["fingerprint"]:
        raise RuntimeError("Build dependencies changed during candidate validation.")
    verify_input_bindings(snapshot, bindings)
    if stage_digest(stage, output, node, snapshot) != initial_stage_digest:
        raise RuntimeError("Native stage changed before writing candidate evidence.")
    report = {
        "schemaVersion": 2, "buildOptions": build_options(args.startup_diagnostics), "kind": "local-native-candidate", "target": "macos-arm64",
        "productionSigned": False, "minimumOsValidated": False, "published": False, "releaseEligible": False,
        "pythonVersion": platform.python_version(), "buildOsVersion": platform.mac_ver()[0],
        "requirementsLockSha256": bindings["inputs/requirements.lock"]["sha256"], "versions": versions,
        "sdkProvenanceSha256": bindings["inputs/sdk-provenance.json"]["sha256"], "sdkPins": SDK_PINS,
        "sourceSnapshotSha256": snapshot_digest, "inputBindings": bindings, "dependencies": dependencies,
        "executableSha256": sha256(executable), "selfTest": self_test,
        "relocatedSelfTest": relocated_test, "httpTest": http_test, "relocatedHttpTest": relocated_http,
        "stageRuntimeSha256": http_test["runtimeSha256"],
    }
    (output / "native-build-provenance.json").write_text(json.dumps(report, indent=2) + "\n", encoding="utf-8")
    return {"stage": str(stage), "report": str(output / "native-build-provenance.json"),
            "productionSigned": False, "published": False, "releaseEligible": False}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--startup-diagnostics", choices=("off", "bootloader-importtime"), default="off")
    parser.add_argument("--output", required=True, help="New output directory; must not exist.")
    parser.add_argument("--sdk-provenance", required=True)
    parser.add_argument("--node-executable", required=True, help="Absolute Node executable for actual staged HTTP validation.")
    parser.add_argument("--wheelhouse", required=True, help="Absolute directory of hash-approved dependency wheels.")
    parser.add_argument("--requirements-lock", default=str(ROOT / "packaging" / "requirements-macos-arm64.lock"))
    parser.add_argument("--build-timeout", type=float, default=1800)
    parser.add_argument("--self-test-timeout", type=float, default=180)
    args = parser.parse_args()
    if not all(0 < value <= 3600 for value in (args.build_timeout, args.self_test_timeout)):
        parser.error("timeouts must be positive and at most 3600 seconds")
    def terminate(signum, _frame):
        raise SystemExit(128 + signum)

    previous = signal.signal(signal.SIGTERM, terminate)
    try:
        try:
            result = build(args)
        except (OSError, ValueError, RuntimeError, importlib.metadata.PackageNotFoundError) as error:
            parser.exit(1, str(error) + "\n")
    finally:
        signal.signal(signal.SIGTERM, previous)
    print(json.dumps(result, sort_keys=True))


if __name__ == "__main__":
    main()
