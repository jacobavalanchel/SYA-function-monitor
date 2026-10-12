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
    "server.py", "realsense_runtime.py", "index.html", "config.example.json",
    "manifest.json", "module.json", "api.openapi.json", "package.json",
    "packaging/native_entry.py", "packaging/native_self_test.py", "packaging/native_tls.py",
    "packaging/native_inputs.py", "packaging/monitor.spec",
    "scripts/build-native-runtime.py", "scripts/test-staged-runtime.mjs", "scripts/hash-runtime.mjs",
)


def source_snapshot(output, lock, provenance, node):
    """Freeze only explicit build/validation inputs, never a repository tree."""
    snapshot = Path(output) / "source-snapshot"
    paths = {name: ROOT / name for name in SOURCE_FILES}
    paths.update({"inputs/requirements.lock": Path(lock), "inputs/sdk-provenance.json": Path(provenance)})
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
    expected = {name for name in bindings if not name.startswith("external/")}
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
                process.communicate(timeout=1)
                return
        raise RuntimeError("Owned native process group did not exit after TERM/KILL.")
    finally:
        for signum, handler in previous.items():
            signal.signal(signum, handler)


def run_bounded(command, *, env, cwd, timeout, log=None):
    stream = Path(log).open("wb") if log else subprocess.PIPE
    try:
        process = subprocess.Popen(command, cwd=cwd, env=env, stdout=stream,
                                   stderr=subprocess.STDOUT if log else subprocess.PIPE,
                                   start_new_session=True)
        try:
            output, error = process.communicate(timeout=timeout)
            if process.returncode:
                if log:
                    raise RuntimeError("Native build failed; inspect the isolated build log: " + str(log))
                diagnostic = (output or b"")[-2000:] + b"\n" + (error or b"")[-4000:]
                raise RuntimeError("Native validation failed: " + diagnostic.decode("utf-8", errors="replace"))
            if not wait_owned_group(process, 0.25):
                raise RuntimeError("Native command left running descendants after its leader exited.")
            return (output or b"").decode("utf-8")
        except BaseException as error:
            cleanup_owned_group(process)
            if isinstance(error, subprocess.TimeoutExpired):
                raise RuntimeError("Native build/diagnostic process exceeded its deadline.") from None
            raise
    finally:
        if log:
            stream.close()


def verify_candidate(executable, output, timeout):
    with tempfile.TemporaryDirectory(prefix="native-self-test-", dir=output) as scratch:
        scratch = Path(scratch)
        env = isolated_environment(scratch / "home", scratch / "tmp")
        launched = time.monotonic()
        text = run_bounded([str(executable), "--self-test"], env=env, cwd=scratch, timeout=timeout)
        process_wall_ms = round((time.monotonic() - launched) * 1000)
        result = json.loads(text)
        if result.get("ok") is not True or result.get("frozen") is not True:
            raise RuntimeError("The candidate did not pass an actual frozen dependency self-test.")
        if result.get("nativeLibraries", {}).get("checked") is not True:
            raise RuntimeError("The candidate's loaded native dependency closure was not checked.")
        if list((scratch / "tmp").glob("_MEI*")):
            raise RuntimeError("The onefile candidate did not clean its extraction directory.")
        return {**result, "externalLaunchToExitMs": process_wall_ms}


def verify_http_stage(stage, output, node, snapshot):
    """Use the existing contract/auth/proof/SSE smoke on the declared executable."""
    program = """
import { pathToFileURL } from 'node:url';
const { testStagedRuntime } = await import(pathToFileURL(process.argv[2]).href);
const abort = new AbortController();
const stop = () => abort.abort();
process.once('SIGINT', stop); process.once('SIGTERM', stop);
try {
  const result = await testStagedRuntime(process.argv[3], {
    startupTimeoutMs: 30000, requestTimeoutMs: 1500, stopTimeoutMs: 2000,
    signal: abort.signal,
  });
  console.log(JSON.stringify(result));
} catch (error) { console.error(error.message); process.exitCode = 1; }
finally { process.removeListener('SIGINT', stop); process.removeListener('SIGTERM', stop); }
"""
    with tempfile.TemporaryDirectory(prefix="native-http-test-", dir=output) as scratch:
        scratch = Path(scratch)
        env = isolated_environment(scratch / "home", scratch / "tmp")
        started = time.monotonic()
        # Keep argv[1] distinct from the imported helper: its CLI guard must
        # remain false when this wrapper calls the exported function once.
        text = run_bounded([str(node), "--input-type=module", "-e", program, "sya-native-http-wrapper",
                            str(snapshot / "scripts/test-staged-runtime.mjs"), str(stage)],
                           env=env, cwd=scratch, timeout=180)
        result = json.loads(text)
        if (not re.fullmatch(r"[0-9a-f]{64}", str(result.get("runtimeSha256", ""))) or
                result.get("sseHandshake") is not True or result.get("authenticatedOperations", 0) < 1):
            raise RuntimeError("The native HTTP stage was not verified.")
        return {**result, "externalHttpValidationWallMs": round((time.monotonic() - started) * 1000),
                "startupTimeoutMs": 30000, "stopTimeoutMs": 2000}


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
    snapshot, bindings, snapshot_digest = source_snapshot(output, lock, provenance, node)
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
    self_test = verify_candidate(executable, output, args.self_test_timeout)
    http_test = verify_http_stage(stage, output, node, snapshot)
    if http_test["runtimeSha256"] != initial_stage_digest:
        raise RuntimeError("Native stage changed during dependency validation.")
    with tempfile.TemporaryDirectory(prefix="中文 路径 ", dir=output) as relocated:
        relocated_stage = Path(relocated) / "stage"
        shutil.copytree(stage, relocated_stage)
        relocated_test = verify_candidate(relocated_stage / "bin" / "monitor-server", output, args.self_test_timeout)
        relocated_http = verify_http_stage(relocated_stage, output, node, snapshot)
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
        "schemaVersion": 1, "kind": "local-native-candidate", "target": "macos-arm64",
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
