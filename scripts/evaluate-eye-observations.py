#!/usr/bin/env python3
"""Evaluate explicit synthetic eye-observation JSONL without native dependencies."""
from __future__ import annotations

import argparse
from contextlib import nullcontext
import json
import math
from pathlib import Path
import sys

MODULE_ROOT = Path(__file__).resolve().parents[1]
# Source checkouts keep the module at root; staged packages keep it in assets.
MODULE_DIRECTORY = MODULE_ROOT if (MODULE_ROOT / "eye_observation.py").is_file() else MODULE_ROOT / "assets"
sys.path.insert(0, str(MODULE_DIRECTORY))
from eye_observation import (  # noqa: E402
    EXTRACTOR_VERSION, GEOMETRY_REASONS, SCHEMA_VERSION,
    EyeGeometry, EyeObservation, EyeObservationWindow, EyeWindowConfig,
    observation_has_coherent_timestamps,
)

MAX_CONFIG_BYTES = 16 * 1024
MAX_LINE_BYTES = 64 * 1024
MAX_OFFLINE_SAMPLES = 100_000
CONFIG_FIELDS = {"schema_version", "extractor_version", "session_id", "duration_seconds", "max_samples", "max_gap_seconds"}
OBSERVATION_FIELDS = {"schema_version", "session_id", "color_frame_number", "captured_at_ms", "acquired_monotonic_s", "completed_monotonic_s", "geometry"}
GEOMETRY_FIELDS = {"schema_version", "extractor_version", "width", "height", "left_ear", "right_ear", "left_width_px", "right_width_px", "reason"}


class InputError(Exception):
    """Only fixed internal codes may cross the error-report boundary."""


def fail(code):
    raise InputError(code)


def _object_pairs(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            fail("duplicate_key")
        result[key] = value
    return result


def _constant(_value):
    fail("non_finite_number")


def _float(value):
    number = float(value)
    if not math.isfinite(number):
        fail("non_finite_number")
    return number


def decode(data):
    try:
        return json.loads(data.decode("utf-8"), object_pairs_hook=_object_pairs,
                          parse_constant=_constant, parse_float=_float)
    except UnicodeError:
        fail("invalid_utf8")
    except (json.JSONDecodeError, ValueError, RecursionError, OverflowError):
        fail("invalid_json")


def exact_fields(value, fields):
    if not isinstance(value, dict) or value.keys() != fields:
        fail("invalid_structure")


def number(value, minimum=0, strictly_positive=False):
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        fail("invalid_number")
    try:
        finite = math.isfinite(value)
    except (ValueError, OverflowError):
        finite = False
    if not finite:
        fail("non_finite_number")
    if value < minimum or (strictly_positive and value == minimum):
        fail("invalid_number")
    return value


def integer(value, minimum=0):
    if type(value) is not int or value < minimum:
        fail("invalid_integer")
    # Prevent pathological finite-looking JSON integers becoming float overflow.
    number(value, minimum)
    return value


def version(value):
    if type(value) is not int or value != SCHEMA_VERSION:
        fail("unsupported_schema_version")


def extractor(value):
    if value != EXTRACTOR_VERSION:
        fail("unsupported_extractor_version")


def session(value, nullable=False):
    if nullable and value is None:
        return value
    if not isinstance(value, str) or not value or len(value) > 256:
        fail("invalid_session_id")
    return value


def parse_config(value):
    exact_fields(value, CONFIG_FIELDS)
    version(value["schema_version"])
    extractor(value["extractor_version"])
    session_id = session(value["session_id"])
    duration = number(value["duration_seconds"], strictly_positive=True)
    samples = integer(value["max_samples"], minimum=1)
    if samples > MAX_OFFLINE_SAMPLES:
        fail("sample_limit_exceeded")
    gap = number(value["max_gap_seconds"], strictly_positive=True)
    try:
        config = EyeWindowConfig(duration, samples, gap)
    except ValueError:
        fail("invalid_window_config")
    return config, session_id


def parse_geometry(value):
    exact_fields(value, GEOMETRY_FIELDS)
    version(value["schema_version"])
    extractor(value["extractor_version"])
    reason = value["reason"]
    if reason is not None and (not isinstance(reason, str) or reason not in GEOMETRY_REASONS):
        fail("invalid_geometry_reason")
    for field in ("width", "height"):
        if value[field] is None:
            if reason is None:
                fail("invalid_geometry")
        else:
            integer(value[field], minimum=1)
    for field in ("left_ear", "right_ear", "left_width_px", "right_width_px"):
        if reason is not None:
            if value[field] is not None:
                fail("invalid_geometry")
        else:
            number(value[field], strictly_positive=field.endswith("width_px"))
    return EyeGeometry(**value)


def parse_observation(value):
    exact_fields(value, OBSERVATION_FIELDS)
    version(value["schema_version"])
    session_id = session(value["session_id"], nullable=True)
    frame = value["color_frame_number"]
    if frame is not None:
        integer(frame)
    captured = integer(value["captured_at_ms"])
    acquired = number(value["acquired_monotonic_s"])
    completed = number(value["completed_monotonic_s"])
    # A finite reversed clock is valid input for the window's quality rejection.
    return EyeObservation(session_id, frame, captured, acquired, completed,
                          parse_geometry(value["geometry"]))


def report_error(line, code):
    print(json.dumps({"line": line, "code": code}, separators=(",", ":")), file=sys.stderr)


class SafeArgumentParser(argparse.ArgumentParser):
    def error(self, _message):
        report_error(0, "invalid_arguments")
        raise SystemExit(2)


def main(argv=None):
    parser = SafeArgumentParser(description=__doc__)
    parser.add_argument("--config", required=True, help="Versioned JSON configuration path")
    parser.add_argument("--input", required=True, help="JSONL path, or - for standard input")
    parser.add_argument("--as-of-monotonic-s", help="Explicit snapshot endpoint; defaults to latest coherent current-session completion/acquisition")
    args = parser.parse_args(argv)
    line_number = 0
    try:
        with open(args.config, "rb") as handle:
            data = handle.read(MAX_CONFIG_BYTES + 1)
        if len(data) > MAX_CONFIG_BYTES:
            fail("config_too_large")
        config, session_id = parse_config(decode(data))
        window = EyeObservationWindow(config)
        window.reset(session_id)
        as_of = 0.0
        context = nullcontext(sys.stdin.buffer) if args.input == "-" else open(args.input, "rb")
        with context as handle:
            while True:
                raw = handle.readline(MAX_LINE_BYTES + 1)
                if not raw:
                    break
                line_number += 1
                if len(raw) > MAX_LINE_BYTES:
                    fail("line_too_long")
                observation = parse_observation(decode(raw))
                window.observe(observation)
                if observation.session_id == session_id and observation_has_coherent_timestamps(observation):
                    as_of = max(as_of, observation.acquired_monotonic_s, observation.completed_monotonic_s)
        if line_number == 0:
            fail("empty_input")
        if args.as_of_monotonic_s is not None:
            try:
                as_of = float(args.as_of_monotonic_s)
            except (ValueError, OverflowError):
                fail("invalid_as_of_monotonic_s")
            if not math.isfinite(as_of) or as_of < 0:
                fail("invalid_as_of_monotonic_s")
        try:
            result = window.snapshot(as_of)
        except ValueError:
            fail("invalid_as_of_monotonic_s")
        print(json.dumps(result, sort_keys=True, allow_nan=False, separators=(",", ":")))
        return 0
    except InputError as error:
        report_error(line_number, str(error))
    except OSError:
        report_error(line_number, "input_io_error")
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
