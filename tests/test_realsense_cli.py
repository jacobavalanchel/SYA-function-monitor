import argparse
import contextlib
import io
import json
import sys
import tempfile
import types
import unittest
from pathlib import Path
from unittest import mock

from scripts import realsense_l515 as cli


class FakeDevice:
    def __init__(self, product_id, serial="device-1", firmware="1.5.8.1"):
        self.values = {
            "product_id": product_id,
            "serial_number": serial,
            "firmware_version": firmware,
        }

    def get_info(self, field):
        return self.values[field]


class FakeConfig:
    def enable_device(self, serial):
        self.serial = serial

    def enable_stream(self, *_args):
        return None


class FakeFrames:
    def get_color_frame(self):
        return object()

    def get_depth_frame(self):
        return object()


class FakePipeline:
    def __init__(self, device, stop_error=None):
        self.device = device
        self.stop_error = stop_error

    def start(self, _config):
        return types.SimpleNamespace(get_device=lambda: self.device)

    def wait_for_frames(self, _timeout):
        return FakeFrames()

    def stop(self):
        if self.stop_error:
            raise self.stop_error


def fake_realsense(version="2.54.2", firmware="1.5.8.1", stop_error=None):
    module = types.ModuleType("pyrealsense2")
    device = FakeDevice("0b64", firmware=firmware)
    module.__version__ = version
    module.camera_info = types.SimpleNamespace(
        product_id="product_id",
        serial_number="serial_number",
        firmware_version="firmware_version",
    )
    module.stream = types.SimpleNamespace(color="color", depth="depth")
    module.format = types.SimpleNamespace(rgb8="rgb8", z16="z16")
    module.context = lambda: types.SimpleNamespace(query_devices=lambda: [device])
    module.pipeline = lambda: FakePipeline(device, stop_error)
    module.config = FakeConfig
    return module


class RealSenseCliTests(unittest.TestCase):
    def smoke_args(self):
        return argparse.Namespace(
            serial="",
            depth_only=False,
            color_only=False,
            width=640,
            height=480,
            fps=30,
            frames=1,
            timeout_ms=1000,
            json=True,
        )

    def run_smoke(self, module):
        output = io.StringIO()
        with mock.patch.dict(sys.modules, {"pyrealsense2": module}), contextlib.redirect_stdout(output):
            result = cli.smoke(self.smoke_args())
        return result, json.loads(output.getvalue())

    def test_patch_checksum_is_pinned(self):
        self.assertEqual(cli.patch_sha256(), cli.PATCH_SHA256)

    def test_product_id_normalization_identifies_l515(self):
        self.assertTrue(cli.is_l515({"productId": "0x0B64"}))
        self.assertFalse(cli.is_l515({"productId": "0B07"}))

    def test_nonpositive_smoke_arguments_are_rejected(self):
        with contextlib.redirect_stderr(io.StringIO()):
            with self.assertRaises(SystemExit):
                cli.build_parser().parse_args(["smoke", "--frames", "0"])

    def test_exact_patched_source_is_idempotent(self):
        with mock.patch.object(cli, "command_output", side_effect=[cli.SDK_COMMIT, "clean-tree"]), mock.patch.object(
            cli, "git_tree", side_effect=["patched-tree", "patched-tree"]
        ):
            self.assertEqual(cli.ensure_clean_patched_source(Path("/checkout")), "already-applied")

    def test_source_with_unrelated_changes_is_rejected(self):
        with mock.patch.object(cli, "command_output", side_effect=[cli.SDK_COMMIT, "clean-tree"]), mock.patch.object(
            cli, "git_tree", side_effect=["patched-tree", "dirty-tree"]
        ):
            with self.assertRaisesRegex(RuntimeError, "unrelated changes"):
                cli.ensure_clean_patched_source(Path("/checkout"))

    def test_smoke_rejects_non_l515_device(self):
        camera_info = types.SimpleNamespace(product_id="product_id", serial_number="serial_number")
        fake_rs = types.SimpleNamespace(
            __version__="2.54.2",
            camera_info=camera_info,
            context=lambda: types.SimpleNamespace(
                query_devices=lambda: [FakeDevice("0b07")]
            ),
            pipeline=lambda: self.fail("pipeline must not start for a non-L515 device"),
        )
        output = io.StringIO()
        with mock.patch.dict(sys.modules, {"pyrealsense2": fake_rs}), contextlib.redirect_stdout(output):
            result = cli.smoke(self.smoke_args())
        self.assertEqual(result, 2)
        report = json.loads(output.getvalue())
        self.assertFalse(report["ok"])
        self.assertIn("L515", report["error"])

    def test_smoke_happy_path_validates_sdk_firmware_and_frames(self):
        result, report = self.run_smoke(fake_realsense())
        self.assertEqual(result, 0)
        self.assertTrue(report["ok"])
        self.assertEqual(report["productId"], "0b64")
        self.assertTrue(report["firmwareSupported"])

    def test_smoke_rejects_sdk_version_mismatch(self):
        fake_rs = fake_realsense(version="2.55.1")
        fake_rs.pipeline = lambda: self.fail("pipeline must not start with an unsupported SDK")
        result, report = self.run_smoke(fake_rs)
        self.assertEqual(result, 2)
        self.assertFalse(report["ok"])
        self.assertEqual(report["sdkVersion"], "2.55.1")

    def test_smoke_rejects_firmware_mismatch(self):
        fake_rs = fake_realsense(firmware="1.5.7.1")
        fake_rs.pipeline = lambda: self.fail("pipeline must not start with unsupported firmware")
        result, report = self.run_smoke(fake_rs)
        self.assertEqual(result, 2)
        self.assertFalse(report["ok"])
        self.assertFalse(report["firmwareSupported"])

    def test_smoke_reports_pipeline_release_failure(self):
        result, report = self.run_smoke(fake_realsense(stop_error=RuntimeError("release failed")))
        self.assertEqual(result, 2)
        self.assertFalse(report["ok"])
        self.assertEqual(report["releaseError"], "release failed")

    def test_smoke_import_failure_is_json(self):
        result, report = self.run_smoke(None)
        self.assertEqual(result, 2)
        self.assertFalse(report["ok"])
        self.assertIn("pyrealsense2", report["error"])

    def test_stale_lock_file_does_not_block_install(self):
        with tempfile.TemporaryDirectory() as temporary:
            lock_path = Path(temporary) / "install.lock"
            lock_path.write_text("pid=999999\n", encoding="utf-8")
            with cli.install_locks([lock_path]):
                self.assertIn("pid=", lock_path.read_text(encoding="utf-8"))


if __name__ == "__main__":
    unittest.main()
