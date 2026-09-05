import argparse
import contextlib
import io
import json
import sys
import types
import unittest
from pathlib import Path
from unittest import mock

from scripts import realsense_l515 as cli


class FakeDevice:
    def __init__(self, product_id, serial="device-1"):
        self.values = {
            "product_id": product_id,
            "serial_number": serial,
        }

    def get_info(self, field):
        return self.values[field]


class RealSenseCliTests(unittest.TestCase):
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
            camera_info=camera_info,
            context=lambda: types.SimpleNamespace(
                query_devices=lambda: [FakeDevice("0b07")]
            ),
            pipeline=lambda: self.fail("pipeline must not start for a non-L515 device"),
        )
        args = argparse.Namespace(
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
        output = io.StringIO()
        with mock.patch.dict(sys.modules, {"pyrealsense2": fake_rs}), contextlib.redirect_stdout(output):
            result = cli.smoke(args)
        self.assertEqual(result, 2)
        report = json.loads(output.getvalue())
        self.assertFalse(report["ok"])
        self.assertIn("L515", report["error"])


if __name__ == "__main__":
    unittest.main()
