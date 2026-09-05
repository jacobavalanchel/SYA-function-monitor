import json
import re
import unittest
from pathlib import Path

import server


ROOT = Path(__file__).resolve().parents[1]


class ContractParityTests(unittest.TestCase):
    def test_versions_match(self):
        package = json.loads((ROOT / "package.json").read_text(encoding="utf-8"))
        manifest = json.loads((ROOT / "manifest.json").read_text(encoding="utf-8"))
        openapi = json.loads((ROOT / "api.openapi.json").read_text(encoding="utf-8"))
        source = (ROOT / "src" / "main.ts").read_text(encoding="utf-8")
        versions = {package["version"], manifest["version"], openapi["info"]["version"]}
        self.assertEqual(versions, {"0.2.0"})
        self.assertIn("version: '0.2.0'", source)

    def test_action_sets_match(self):
        openapi = json.loads((ROOT / "api.openapi.json").read_text(encoding="utf-8"))
        openapi_actions = {
            operation["operationId"]
            for path in openapi["paths"].values()
            for operation in path.values()
            if isinstance(operation, dict) and "operationId" in operation
        } - {"events"}
        server_actions = {definition["action"] for definition in server.ACTION_DEFINITIONS}
        source = (ROOT / "src" / "main.ts").read_text(encoding="utf-8")
        source_actions = set(re.findall(r"\{ action: '([^']+)'", source))
        self.assertEqual(server_actions, openapi_actions)
        self.assertEqual(server_actions, source_actions)

    def test_live_events_are_declared(self):
        manifest = json.loads((ROOT / "manifest.json").read_text(encoding="utf-8"))
        self.assertIn("realsense.frame", manifest["events"])
        self.assertIn("realsense.training.sample", manifest["events"])
        self.assertIn("attention.status", manifest["events"])

    def test_install_patch_contains_no_firmware_updater(self):
        patch = (ROOT / "patches" / "librealsense-v2.54.2-macos-l515.patch").read_text(
            encoding="utf-8"
        )
        cli = (ROOT / "scripts" / "realsense_l515.py").read_text(encoding="utf-8")
        self.assertNotIn("tools/fw-update", patch)
        self.assertNotIn("rs-fw-update", cli)
        self.assertNotIn("L51X_FW_Image", cli)


if __name__ == "__main__":
    unittest.main()
