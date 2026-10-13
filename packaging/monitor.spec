# Local macOS arm64 candidate; production signing/platform support is separate.
import importlib.metadata
import _hashlib
import _ssl
import json
import os
from pathlib import Path
import platform
import sys

from PyInstaller.utils.hooks import collect_data_files, collect_dynamic_libs, collect_submodules, copy_metadata
from PyInstaller.depend.bindepend import get_imports

ROOT = Path(SPECPATH).resolve().parent
if sys.platform != "darwin" or platform.machine() != "arm64" or sys.version_info[:2] != (3, 11):
    raise RuntimeError("This candidate spec requires native macOS arm64 Python 3.11.")
for name, version in {"pyinstaller": "6.22.3", "numpy": "1.26.4", "Pillow": "11.3.0", "mediapipe": "0.10.21", "certifi": "2026.7.22"}.items():
    if importlib.metadata.version(name) != version:
        raise RuntimeError("Unpinned build dependency: " + name)

options = json.loads((ROOT / "inputs/build-options.json").read_text())
if (set(options) != {"schemaVersion", "startupDiagnostics"} or type(options["schemaVersion"]) is not int
        or options["schemaVersion"] != 1 or options["startupDiagnostics"] not in ("off", "bootloader-importtime")):
    raise RuntimeError("Invalid bound native build options.")
diagnostic = options["startupDiagnostics"] == "bootloader-importtime"
interpreter_options = [("X importtime", None, "OPTION")] if diagnostic else []

sdk = json.loads(Path(os.environ["SYA_NATIVE_SDK_PROVENANCE"]).read_text())
datas = [(str(ROOT / name), ".") for name in ("manifest.json", "index.html", "config.example.json")]
datas += collect_data_files("mediapipe")
datas += copy_metadata("mediapipe", recursive=True)
datas += collect_data_files("certifi")
datas += copy_metadata("certifi")
binaries = collect_dynamic_libs("mediapipe")
binaries += [(str(item["path"]), ".") for item in sdk["libraries"]]


def interpreter_openssl_libraries(extensions, dependency_reader):
    """Select the actual interpreter pair, never a similarly named wheel copy."""
    expected = {"libssl.3.dylib", "libcrypto.3.dylib"}
    libraries = {}
    for extension in extensions:
        for name, source in dependency_reader(str(extension)):
            basename = Path(name).name
            if basename not in expected:
                continue
            if not source:
                raise RuntimeError("Cannot resolve interpreter OpenSSL dependency: " + basename)
            path = Path(source).resolve(strict=True)
            if not path.is_file():
                raise RuntimeError("Interpreter OpenSSL dependency must be a file.")
            if basename in libraries and libraries[basename] != str(path):
                raise RuntimeError("Interpreter extensions disagree on their OpenSSL libraries.")
            libraries[basename] = str(path)
    if set(libraries) != expected:
        raise RuntimeError("The interpreter's complete OpenSSL 3 pair is required.")
    # libssl itself must link to the same libcrypto as _ssl/_hashlib.
    crypto = [(name, source) for name, source in dependency_reader(libraries["libssl.3.dylib"])
              if Path(name).name == "libcrypto.3.dylib"]
    if (len(crypto) != 1 or not crypto[0][1]
            or str(Path(crypto[0][1]).resolve(strict=True)) != libraries["libcrypto.3.dylib"]):
        raise RuntimeError("Interpreter libssl and libcrypto do not form a consistent pair.")
    return libraries


def preserve_interpreter_openssl(binaries_toc, datas_toc, libraries):
    """Replace only the two known root aliases; retain every cv2 original."""
    # Analysis routes all non-BINARY/EXTENSION entries, including SYMLINK, to
    # datas after its final combined normalization. Inspect both final lists.
    toc = binaries_toc + datas_toc
    for name, expected_source in libraries.items():
        existing = [row for row in toc if row[0] == name]
        if not existing:
            raise RuntimeError("Analysis omitted an explicitly collected OpenSSL library: " + name)
        for _destination, source, kind in existing:
            if kind == "SYMLINK" and source == "cv2/.dylibs/" + name:
                continue
            if kind == "BINARY" and str(Path(source).resolve(strict=True)) == expected_source:
                continue
            raise RuntimeError("Unexpected root OpenSSL collision: " + name)
    return ([row for row in binaries_toc if row[0] not in libraries]
            + [(name, source, "BINARY") for name, source in sorted(libraries.items())],
            [row for row in datas_toc if row[0] not in libraries])


openssl_libraries = interpreter_openssl_libraries((_ssl.__file__, _hashlib.__file__), get_imports)
# Explicit inputs ensure recursive dependency analysis still examines this pair.
binaries += [(source, ".") for source in openssl_libraries.values()]

def runtime_module(name):
    # 0.10.21's optional checkpoint conversion tool eagerly imports undeclared
    # torch. genai/__init__.py does not import it, and FaceMesh/solution_base do
    # not reference it. Keep genai itself, all actual runtime imports, model data
    # and every locked dependency. Model Maker is another separate training tool:
    # its __init__ references a model_maker.python tree absent from this wheel,
    # and mediapipe/__init__ does not import it. Neither tool runs FaceMesh.
    tools = ("mediapipe.tasks.python.genai.converter", "mediapipe.model_maker")
    if any(name == tool or name.startswith(tool + ".") for tool in tools):
        return False
    return not any(part == "tests" or part == "test" or part.endswith("_test") for part in name.split("."))

hiddenimports = collect_submodules("mediapipe", filter=runtime_module, on_error="raise")
hiddenimports += ["pyrealsense2", "pyrsutils", "PIL.JpegImagePlugin"]
a = Analysis([str(ROOT / "packaging" / "native_entry.py")],
             pathex=[str(ROOT), str(ROOT / "packaging")], binaries=binaries,
             datas=datas, hiddenimports=hiddenimports, hookspath=[], hooksconfig={},
             runtime_hooks=[str(ROOT / "packaging" / "startup_hook.py")],
             excludes=[], noarchive=False, optimize=0)
# PyInstaller 6.22.3 gives SYMLINK a higher TOC priority than BINARY. cv2's
# bundled OpenSSL aliases can therefore displace the newer interpreter pair,
# even when the correct libraries were explicitly supplied above. Preserve the
# selected pair after Analysis; do not remove or alter cv2's original libraries.
a.binaries, a.datas = preserve_interpreter_openssl(a.binaries, a.datas, openssl_libraries)
pyz = PYZ(a.pure)
exe = EXE(pyz, a.scripts, a.binaries, a.datas, interpreter_options, name="monitor-server",
          debug=diagnostic, bootloader_ignore_signals=False, strip=False, upx=False,
          console=True, disable_windowed_traceback=False, argv_emulation=False,
          target_arch="arm64", codesign_identity=None, entitlements_file=None)
