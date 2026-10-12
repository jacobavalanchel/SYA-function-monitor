"""Offline diagnostics for the actual frozen inference and preview libraries.

This deliberately does not import server, create a RealSense context, enumerate
devices, capture input, emit attention events, or contact a model provider.
"""

import base64
import ctypes
import hashlib
import io
from pathlib import Path
import sys
import time


EXPECTED_VERSIONS = {"numpy": "1.26.4", "Pillow": "11.3.0", "mediapipe": "0.10.21",
                     "pyrealsense2": "2.54.2"}
MODEL_FILES = (
    "modules/face_detection/face_detection_short_range.tflite",
    "modules/face_landmark/face_landmark_front_cpu.binarypb",
    "modules/face_landmark/face_landmark.tflite",
    "modules/face_landmark/face_landmark_with_attention.tflite",
)


def _inside(path, root):
    try:
        path.resolve().relative_to(root.resolve())
        return True
    except ValueError:
        return False


def loaded_native_libraries():
    """Reject build-machine dylib fallback after the real imports/inference."""
    if sys.platform != "darwin" or not getattr(sys, "frozen", False):
        return {"checked": False, "reason": "requires-frozen-macos"}
    bundle = Path(sys._MEIPASS).resolve()
    executable = Path(sys.executable).resolve()
    system = ctypes.CDLL("/usr/lib/libSystem.B.dylib")
    system._dyld_image_count.restype = ctypes.c_uint32
    system._dyld_get_image_name.argtypes = [ctypes.c_uint32]
    system._dyld_get_image_name.restype = ctypes.c_char_p
    bundled = set()
    unexpected = []
    for index in range(system._dyld_image_count()):
        name = system._dyld_get_image_name(index)
        if not name:
            raise RuntimeError("dyld returned an unnamed loaded image.")
        path = Path(name.decode("utf-8")).resolve()
        if path == executable or str(path).startswith(("/System/Library/", "/usr/lib/")):
            continue
        if not _inside(path, bundle):
            unexpected.append(str(path))
        else:
            bundled.add(str(path.relative_to(bundle)))
    if unexpected:
        raise RuntimeError("Native libraries loaded outside the frozen bundle: " + ", ".join(unexpected))
    if not bundled:
        raise RuntimeError("No bundled native libraries were observed.")
    return {"checked": True, "bundled": sorted(bundled), "externalNonSystem": []}


def run_self_test():
    started = time.monotonic()
    tls = {"checked": False, "reason": "source-runtime"}
    if getattr(sys, "frozen", False):
        from native_tls import audit_native_tls

        tls = audit_native_tls()
    import numpy
    import PIL
    from PIL import Image
    import mediapipe
    import pyrealsense2
    import pyrsutils  # noqa: F401; a real native dependency of the pinned SDK.
    from realsense_runtime import FocusEstimator, encode_rgb_jpeg

    versions = {"numpy": numpy.__version__, "Pillow": PIL.__version__,
                "mediapipe": mediapipe.__version__, "pyrealsense2": pyrealsense2.__version__}
    if versions != EXPECTED_VERSIONS:
        raise RuntimeError("Native dependency versions do not match the pinned runtime.")
    frozen = bool(getattr(sys, "frozen", False))
    if frozen:
        bundle = Path(sys._MEIPASS)
        for module in (numpy, PIL, mediapipe, pyrealsense2, pyrsutils):
            if not _inside(Path(module.__file__), bundle):
                raise RuntimeError("Dependency loaded outside the frozen bundle: " + module.__name__)

    model_root = Path(mediapipe.__file__).resolve().parent
    models = {}
    for relative in MODEL_FILES:
        path = model_root / relative
        if not path.is_file() or path.stat().st_size == 0 or not _inside(path, model_root):
            raise RuntimeError("Required FaceMesh model resource is missing: " + relative)
        models[relative] = hashlib.sha256(path.read_bytes()).hexdigest()

    # Generated pixels are never a user recording. No face detection is expected;
    # this exercises the actual graph/detector, not the landmark branch or accuracy.
    width, height = 160, 120
    pixels = numpy.zeros((height, width, 3), dtype=numpy.uint8)
    pixels[:, :, 0] = numpy.arange(width, dtype=numpy.uint8)
    pixels[:, :, 1] = numpy.arange(height, dtype=numpy.uint8)[:, None]
    rgb = pixels.tobytes()
    estimator = FocusEstimator()
    try:
        if not estimator.available:
            raise RuntimeError(estimator.error or "FaceMesh initialization failed.")
        observations = []
        for offset in range(5):
            record, face_detected, _changed = estimator.analyze(rgb, width, height, offset + 1)
            if record.get("boundary") or estimator.error or face_detected:
                raise RuntimeError(estimator.error or "Unexpected synthetic FaceMesh output.")
            if any("energy" in key.lower() for key in record):
                raise RuntimeError("Synthetic diagnostics must not create an energy signal.")
            observations.append(record["state"])
        if observations != ["unknown", "unknown", "unknown", "unknown", "away"]:
            raise RuntimeError("The real FaceMesh no-face observation path did not complete.")
    finally:
        estimator.close()

    jpeg = base64.b64decode(encode_rgb_jpeg(rgb, width, height), validate=True)
    with Image.open(io.BytesIO(jpeg)) as decoded:
        decoded.load()
        if decoded.format != "JPEG" or decoded.size != (width, height) or decoded.mode != "RGB":
            raise RuntimeError("The actual Pillow JPEG round trip failed.")
    libraries = loaded_native_libraries()
    return {
        "ok": True, "kind": "synthetic-native-dependency-self-test", "frozen": frozen,
        "releaseEligible": False,
        "versions": versions, "modelSha256": models,
        "faceMesh": {"actualGraphRuns": 5, "syntheticNoFace": True,
                     "landmarkBranchExercised": False, "accuracyValidated": False},
        "jpeg": {"decoded": True, "width": width, "height": height, "bytes": len(jpeg)},
        "sdk": {"imported": True, "deviceContextCreated": False, "devicesEnumerated": False},
        "nativeLibraries": libraries, "tls": tls, "energySignalEmitted": False,
        "elapsedMs": round((time.monotonic() - started) * 1000),
    }
