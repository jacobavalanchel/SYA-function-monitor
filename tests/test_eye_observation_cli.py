"""FOC-011: strict, streaming, synthetic-only offline quality reporting."""
from dataclasses import asdict
import importlib.util
import io
import json
from pathlib import Path
import subprocess
import sys
import tempfile
from types import SimpleNamespace
import unittest
from unittest import mock

from eye_observation import EXTRACTOR_VERSION, EyeObservationWindow, EyeWindowConfig
from test_eye_observation import observation

ROOT = Path(__file__).resolve().parents[1]
CLI = ROOT / "scripts/evaluate-eye-observations.py"
spec = importlib.util.spec_from_file_location("eye_evaluator_test", CLI)
evaluator = importlib.util.module_from_spec(spec)
spec.loader.exec_module(evaluator)


def config(**changes):
    value = {"schema_version": 1, "extractor_version": EXTRACTOR_VERSION, "session_id": "synthetic",
             "duration_seconds": 2, "max_samples": 20, "max_gap_seconds": 1}
    value.update(changes)
    return value


def row(frame=1, acquired=100, **changes):
    value = {"schema_version": 1, **asdict(observation(frame, acquired))}
    value.update(changes)
    return value


class EvaluatorTests(unittest.TestCase):
    def run_cli(self, rows=None, *, raw=None, settings=None, raw_config=None, extra=(), stdin=False):
        with tempfile.TemporaryDirectory() as directory:
            directory = Path(directory)
            config_path = directory / "synthetic-config.json"
            input_path = directory / "synthetic-observations.jsonl"
            config_path.write_bytes(raw_config if raw_config is not None else json.dumps(config() if settings is None else settings).encode())
            data = raw if raw is not None else "".join(json.dumps(value) + "\n" for value in rows).encode()
            input_path.write_bytes(data)
            return subprocess.run([sys.executable, str(CLI), "--config", str(config_path),
                                   "--input", "-" if stdin else str(input_path), *extra],
                                  input=data if stdin else None, capture_output=True, timeout=5)

    def assert_error(self, result, line, code):
        self.assertEqual(result.returncode, 2, result.stderr)
        self.assertEqual(result.stdout, b"")
        self.assertEqual(json.loads(result.stderr), {"line": line, "code": code})
        self.assertNotIn(b"Traceback", result.stderr)
        self.assertNotIn(b"private text", result.stderr)

    def test_report_matches_pure_window_exactly(self):
        rows = [row(1, 100), row(2, 100.5)]
        result = self.run_cli(rows)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.stderr, b"")
        target = EyeObservationWindow(EyeWindowConfig(2, 20, 1))
        target.reset("synthetic")
        for item in rows:
            target.observe(evaluator.parse_observation(item))
        expected = target.snapshot(100.55)
        self.assertEqual(json.loads(result.stdout), expected)
        self.assertEqual(expected["coverage_seconds"], .5)
        self.assertEqual(expected["coverage_ratio"], .25)
        self.assertNotIn("energy", expected)
        self.assertNotIn("blink_count", expected)

    def test_incoherent_clock_does_not_advance_default_endpoint_or_poison_trace(self):
        bad = row(2, 1e9, completed_monotonic_s=0)
        for rows, endpoint, valid in (([row(1, 100), bad], 100.05, 1),
                                     ([row(1, 100), bad, row(3, 100.5)], 100.55, 2)):
            with self.subTest(endpoint=endpoint):
                result = self.run_cli(rows)
                self.assertEqual(result.returncode, 0, result.stderr)
                report = json.loads(result.stdout)
                self.assertEqual(report["as_of_monotonic_s"], endpoint)
                self.assertEqual(report["valid_samples"], valid)
                self.assertEqual(report["invalid_samples"], 1)
                self.assertEqual(report["reason_counts"], {"invalid_timestamp": 1})
                self.assertEqual(report["coverage_seconds"], 0)

    def test_explicit_as_of_clips_coverage_without_extrapolation(self):
        result = self.run_cli([row(1, 100), row(2, 100.5)], extra=("--as-of-monotonic-s", "102.25"))
        self.assertEqual(result.returncode, 0, result.stderr)
        report = json.loads(result.stdout)
        self.assertEqual(report["coverage_seconds"], .25)
        self.assertEqual(report["anchor_samples"], 1)
        self.assertEqual(report["valid_samples"], 1)

    def test_stdin_and_final_line_without_newline(self):
        result = self.run_cli(raw=json.dumps(row()).encode(), stdin=True)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(json.loads(result.stdout)["valid_samples"], 1)

    def test_bad_second_line_suppresses_entire_success_report_and_input_echo(self):
        raw = json.dumps(row()).encode() + b'\n{"private text": invalid}\n'
        self.assert_error(self.run_cli(raw=raw), 2, "invalid_json")

    def test_empty_blank_and_non_object_input(self):
        for raw, line, code in ((b"", 0, "empty_input"), (b"\n", 1, "invalid_json"),
                                (b"[]\n", 1, "invalid_structure"), (b"null", 1, "invalid_structure")):
            with self.subTest(raw=raw):
                self.assert_error(self.run_cli(raw=raw), line, code)

    def test_invalid_utf8_and_oversized_line(self):
        self.assert_error(self.run_cli(raw=b"\xff private text"), 1, "invalid_utf8")
        self.assert_error(self.run_cli(raw=b" " * (evaluator.MAX_LINE_BYTES + 1)), 1, "line_too_long")

    def test_deep_nesting_and_oversized_config_are_bounded(self):
        self.assert_error(self.run_cli(raw=b"[" * 2000 + b"]" * 2000), 1, "invalid_json")
        self.assert_error(self.run_cli([row()], raw_config=b" " * (evaluator.MAX_CONFIG_BYTES + 1)), 0, "config_too_large")

    def test_duplicate_keys_are_rejected_even_when_final_value_looks_valid(self):
        raw = json.dumps(row()).replace('"schema_version": 1', '"schema_version": 2,"schema_version": 1', 1).encode()
        self.assert_error(self.run_cli(raw=raw), 1, "duplicate_key")
        raw_config = json.dumps(config()).replace('"schema_version": 1', '"schema_version": 1,"schema_version": 1').encode()
        self.assert_error(self.run_cli([row()], raw_config=raw_config), 0, "duplicate_key")

    def test_nan_infinity_and_float_overflow_are_rejected(self):
        for constant in ("NaN", "Infinity", "-Infinity", "1e999"):
            raw = json.dumps(row()).replace('"acquired_monotonic_s": 100', '"acquired_monotonic_s": ' + constant).encode()
            self.assert_error(self.run_cli(raw=raw), 1, "non_finite_number")

    def test_boolean_cannot_impersonate_number_or_version(self):
        fields = {"schema_version": "unsupported_schema_version", "color_frame_number": "invalid_integer",
                  "captured_at_ms": "invalid_integer", "acquired_monotonic_s": "invalid_number",
                  "completed_monotonic_s": "invalid_number"}
        for field, code in fields.items():
            self.assert_error(self.run_cli([row(**{field: True})]), 1, code)
        item = row()
        item["geometry"]["left_ear"] = True
        self.assert_error(self.run_cli([item]), 1, "invalid_number")
        self.assert_error(self.run_cli([row()], settings=config(max_samples=True)), 0, "invalid_integer")

    def test_geometry_versions_reasons_and_measurement_consistency(self):
        cases = (("schema_version", 2, "unsupported_schema_version"),
                 ("extractor_version", "private text", "unsupported_extractor_version"),
                 ("reason", "private text", "invalid_geometry_reason"),
                 ("reason", "no_face", "invalid_geometry"),
                 ("width", None, "invalid_geometry"),
                 ("left_width_px", 0, "invalid_number"))
        for field, value, code in cases:
            item = row()
            item["geometry"][field] = value
            self.assert_error(self.run_cli([item]), 1, code)

    def test_structure_is_exact_at_each_level(self):
        self.assert_error(self.run_cli([row(energy="private text")]), 1, "invalid_structure")
        item = row()
        item.pop("captured_at_ms")
        self.assert_error(self.run_cli([item]), 1, "invalid_structure")
        item = row()
        item["geometry"]["unexpected"] = "private text"
        self.assert_error(self.run_cli([item]), 1, "invalid_structure")
        self.assert_error(self.run_cli([row()], settings=config(unexpected=True)), 0, "invalid_structure")

    def test_config_requires_versions_explicit_values_and_resource_bound(self):
        for changes, code in (({"schema_version": 2}, "unsupported_schema_version"),
                              ({"extractor_version": "other"}, "unsupported_extractor_version"),
                              ({"duration_seconds": 0}, "invalid_number"),
                              ({"max_gap_seconds": -1}, "invalid_number"),
                              ({"max_samples": 100001}, "sample_limit_exceeded"),
                              ({"session_id": ""}, "invalid_session_id")):
            self.assert_error(self.run_cli([row()], settings=config(**changes)), 0, code)
        incomplete = config()
        del incomplete["max_gap_seconds"]
        self.assert_error(self.run_cli([row()], settings=incomplete), 0, "invalid_structure")

    def test_tiny_duration_cannot_generate_nonfinite_report_rate(self):
        self.assert_error(self.run_cli([row()], settings=config(duration_seconds=1e-320)), 0, "invalid_window_config")

    def test_all_finite_extreme_geometry_statistics_remain_finite(self):
        first, second = row(1, 100), row(2, 100.5)
        for item in (first, second):
            item["geometry"].update(left_ear=1e308, right_ear=1e308)
        result = self.run_cli([first, second])
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(json.loads(result.stdout)["left_ear"]["median"], 1e308)

    def test_missing_identity_wrong_session_and_bad_clock_are_quality_rejections(self):
        result = self.run_cli([row(1, 100), row(None, 100.1), row(99, 999, session_id="other"),
                               row(2, 100.2, completed_monotonic_s=100.1), row(3, 100.3)])
        self.assertEqual(result.returncode, 0, result.stderr)
        report = json.loads(result.stdout)
        self.assertEqual(report["valid_samples"], 2)
        self.assertEqual(report["invalid_samples"], 3)
        self.assertEqual(report["coverage_seconds"], 0)
        self.assertEqual(report["reason_counts"], {"invalid_timestamp": 1, "missing_frame_identity": 1, "wrong_session": 1})
        self.assertEqual(report["as_of_monotonic_s"], 100.35)

    def test_explicit_invalid_as_of_has_no_traceback_or_input_echo(self):
        for endpoint in ("NaN", "Infinity", "private text", "-1", "99"):
            self.assert_error(self.run_cli([row()], extra=("--as-of-monotonic-s", endpoint)), 1, "invalid_as_of_monotonic_s")

    def test_unknown_arguments_have_fixed_error_without_echo(self):
        self.assert_error(self.run_cli([row()], extra=("--private text",)), 0, "invalid_arguments")

    def test_io_failure_does_not_echo_path(self):
        result = subprocess.run([sys.executable, str(CLI), "--config", "/nonexistent/private text/config.json", "--input", "-"],
                                capture_output=True, timeout=5)
        self.assert_error(result, 0, "input_io_error")

    def test_readline_has_explicit_byte_bound_and_only_final_stdout(self):
        class BoundedStream(io.BytesIO):
            calls = 0

            def readline(self, size=-1):
                self.calls += 1
                if size != evaluator.MAX_LINE_BYTES + 1:
                    raise AssertionError("unbounded input read")
                return super().readline(size)

        stream = BoundedStream((json.dumps(row(1, 100)) + "\n" + json.dumps(row(2, 100.5)) + "\n").encode())
        stdout, stderr = io.StringIO(), io.StringIO()
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "config.json"
            path.write_text(json.dumps(config()))
            with mock.patch.object(evaluator.sys, "stdin", SimpleNamespace(buffer=stream)), mock.patch.object(
                evaluator.sys, "stdout", stdout
            ), mock.patch.object(evaluator.sys, "stderr", stderr):
                result = evaluator.main(["--config", str(path), "--input", "-"])
        self.assertEqual(result, 0)
        self.assertEqual(stream.calls, 3)
        self.assertEqual(len(stdout.getvalue().splitlines()), 1)
        self.assertEqual(stderr.getvalue(), "")

    def test_module_import_is_native_dependency_free(self):
        code = "import sys; import eye_observation; assert not ({'numpy','mediapipe','pyrealsense2','cv2'} & sys.modules.keys())"
        result = subprocess.run([sys.executable, "-c", code], cwd=ROOT, capture_output=True, timeout=5)
        self.assertEqual(result.returncode, 0, result.stderr)


if __name__ == "__main__":
    unittest.main()
