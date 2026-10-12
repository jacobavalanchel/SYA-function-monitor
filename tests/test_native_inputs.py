"""Small wheel/venv fixtures: no downloads, package imports, or sensors."""

import hashlib
import importlib.metadata
import importlib.util
import json
from pathlib import Path
import py_compile
import shutil
import sys
import tempfile
import unittest
from unittest import mock
import zipfile

try:
    from packaging.markers import default_environment
    from packaging.tags import sys_tags
except ImportError:
    raise unittest.SkipTest("native input tests require the locked packaging dependency")

ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location("native_inputs_under_test", ROOT / "packaging" / "native_inputs.py")
inputs = importlib.util.module_from_spec(spec)
spec.loader.exec_module(inputs)


class NativeInputTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory(prefix="native-input-fixture-")
        self.addCleanup(self.temporary.cleanup)
        self.prefix = Path(self.temporary.name).resolve() / "venv"
        self.paths = {"purelib": self.prefix / "lib/purelib", "platlib": self.prefix / "lib/platlib",
                      "scripts": self.prefix / "bin", "data": self.prefix,
                      "headers": self.prefix / "include/site/python3.11"}
        for path in self.paths.values():
            path.mkdir(parents=True, exist_ok=True)
        self.wheelhouse = self.prefix.parent / "wheels"
        self.wheelhouse.mkdir()
        self.lock = self.prefix.parent / "requirements.lock"
        self.rows = []

    def package(self, name="demo", version="1.0", requires=(), extra_files=None,
                pure=True, extras=(), internal_tag="py3-none-any", metadata_name=None):
        info = f"{name}-{version}.dist-info"
        metadata = (f"Metadata-Version: 2.1\nName: {metadata_name or name}\nVersion: {version}\n"
                    + "".join(f"Requires-Dist: {value}\n" for value in requires)
                    + "".join(f"Provides-Extra: {value}\n" for value in extras) + "\n")
        content = {f"{name}/__init__.py": b"VALUE = 1\n",
                   f"{name}/model.bin": b"synthetic model bytes",
                   info + "/METADATA": metadata.encode(),
                   info + "/WHEEL": ("Wheel-Version: 1.0\nRoot-Is-Purelib: " + str(pure).lower()
                                     + "\nTag: " + internal_tag + "\n").encode(),
                   info + "/RECORD": b"installer may rewrite this\n"}
        content.update(extra_files or {})
        wheel = self.wheelhouse / f"{name}-{version}-py3-none-any.whl"
        with zipfile.ZipFile(wheel, "w") as archive:
            for relative, data in content.items():
                archive.writestr(relative, data)
                destination, _ = inputs._member_destination(relative, info, f"{name}-{version}.data",
                                                            pure, self.paths, name)
                destination.parent.mkdir(parents=True, exist_ok=True)
                destination.write_bytes(data)
        checksum = hashlib.sha256(wheel.read_bytes()).hexdigest()
        self.rows.append(f"{name}=={version} --hash=sha256:{checksum}")
        self.lock.write_text("\n".join(self.rows) + "\n")
        return wheel

    def environment(self):
        return {"prefix": self.prefix, "paths": self.paths,
                "distributions": list(importlib.metadata.distributions(path=[str(self.paths["purelib"]), str(self.paths["platlib"])])),
                "tags": set(sys_tags()), "markers": default_environment()}

    def verify(self):
        with mock.patch.object(inputs, "_environment", return_value=self.environment()):
            return inputs.verify_dependencies(self.lock, self.wheelhouse)

    def test_complete_graph_and_stable_fingerprint_with_split_install_schemes(self):
        self.package("demo", requires=("child>=2",), extra_files={
            "demo-1.0.data/platlib/native_stub.bin": b"native fixture",
            "demo-1.0.data/purelib/shared_resource.txt": b"purelib resource",
            "demo-1.0.data/data/share/model.bin": b"outside site but inside venv"})
        self.package("child", "2.0", pure=False)
        result = self.verify()
        self.assertEqual(result["versions"], {"child": "2.0", "demo": "1.0"})
        self.assertEqual(result["fingerprint"], self.verify()["fingerprint"])
        self.assertTrue(result["releaseEligible"])
        self.assertEqual(len(result["wheels"]), 2)
        self.lock.write_text(self.lock.read_text() + "# owner reviewed input\n")
        self.assertNotEqual(result["fingerprint"], self.verify()["fingerprint"])

    def test_missing_transitive_dependency_is_not_hidden_by_top_level_presence(self):
        self.package(requires=("not-installed>=1",))
        with self.assertRaisesRegex(ValueError, "transitive dependency"):
            self.verify()

    def test_missing_locked_distribution_is_rejected(self):
        self.package()
        self.package("child")
        shutil.rmtree(self.paths["purelib"] / "child-1.0.dist-info")
        with self.assertRaisesRegex(ValueError, "Missing installed dependencies"):
            self.verify()

    def test_conflicting_transitive_version_is_rejected(self):
        self.package(requires=("child>=2",))
        self.package("child", "1.0")
        with self.assertRaisesRegex(ValueError, "version conflict"):
            self.verify()

    def test_markers_and_requested_extras_are_followed(self):
        self.package(requires=("child[models]>=1", "never-installed; python_version < '2'"))
        self.package("child", requires=("model-dependency; extra == 'models'",), extras=("models",))
        with self.assertRaisesRegex(ValueError, "model-dependency"):
            self.verify()
        self.package("model_dependency", "1.0")
        self.assertIn("model-dependency", self.verify()["versions"])

    def test_changed_python_native_and_model_bytes_each_fail(self):
        self.package(extra_files={"demo/extension.so": b"native fixture"})
        for filename in ("__init__.py", "extension.so", "model.bin"):
            path = self.paths["purelib"] / "demo" / filename
            original = path.read_bytes()
            path.write_bytes(original + b"changed")
            with self.assertRaisesRegex(ValueError, "Installed wheel content differs"):
                self.verify()
            path.write_bytes(original)

    def test_approved_vendored_metadata_is_verified_as_package_content(self):
        self.package(extra_files={"demo/_vendor/child-2.0.dist-info/METADATA": b"Name: child\nVersion: 2.0\n"})
        self.verify()
        path = self.paths["purelib"] / "demo/_vendor/child-2.0.dist-info/METADATA"
        path.write_bytes(b"changed vendor metadata")
        with self.assertRaisesRegex(ValueError, "Installed wheel content differs"):
            self.verify()

    def test_changed_wheel_hash_and_ambiguous_wheelhouse_fail(self):
        wheel = self.package()
        original = wheel.read_bytes()
        wheel.write_bytes(original + b"changed")
        with self.assertRaisesRegex(ValueError, "Wheel hash differs"):
            self.verify()
        wheel.write_bytes(original)
        shutil.copyfile(wheel, self.wheelhouse / "demo-1.0-1-py3-none-any.whl")
        with self.assertRaisesRegex(ValueError, "exactly one wheel"):
            self.verify()

    def test_unapproved_distribution_or_unrecorded_python_file_fail(self):
        self.package()
        self.package("extra")
        self.rows.pop()
        self.lock.write_text("\n".join(self.rows))
        with self.assertRaisesRegex(ValueError, "Unapproved extra distribution"):
            self.verify()
        shutil.rmtree(self.paths["purelib"] / "extra-1.0.dist-info")
        shutil.rmtree(self.paths["purelib"] / "extra")
        (self.wheelhouse / "extra-1.0-py3-none-any.whl").unlink()
        (self.paths["purelib"] / "injected.py").write_text("print('unexpected')\n")
        with self.assertRaisesRegex(ValueError, "Unapproved installed input"):
            self.verify()

    def test_installer_record_and_entry_scripts_are_bound_but_not_compared_to_zip(self):
        self.package(extra_files={"demo-1.0.dist-info/entry_points.txt": b"[console_scripts]\ndemo-cli = demo:main\n"})
        (self.paths["scripts"] / "demo-cli").write_bytes(b"#!/synthetic/venv/python\nfrom demo import main\nmain()\n")
        record = self.paths["purelib"] / "demo-1.0.dist-info/RECORD"
        record.write_text("installer generated paths\n")
        first = self.verify()
        record.write_text("installer generated paths changed\n")
        self.assertNotEqual(first["fingerprint"], self.verify()["fingerprint"])

    def test_changed_internal_tags_do_not_get_mediapipe_exception(self):
        self.package(internal_tag="cp311-cp311-macosx_14_0_x86_64")
        with self.assertRaisesRegex(ValueError, "compatibility tags disagree"):
            self.verify()

    def test_bytecode_must_match_approved_source(self):
        self.package()
        source = self.paths["purelib"] / "demo/__init__.py"
        pyc = Path(py_compile.compile(str(source), doraise=True))
        self.verify()
        source.write_text("VALUE = 2\n")
        py_compile.compile(str(source), cfile=str(pyc), doraise=True)
        source.write_text("VALUE = 1\n")
        with self.assertRaisesRegex(ValueError, "bytecode differs"):
            self.verify()

    def test_symlinked_installed_file_is_rejected(self):
        self.package()
        source = self.paths["purelib"] / "demo/model.bin"
        other = self.prefix / "replacement.bin"
        source.rename(other)
        source.symlink_to(other)
        with self.assertRaisesRegex(ValueError, "symlink"):
            self.verify()

    def test_real_environment_rejects_system_site_even_without_extra_distribution(self):
        (self.prefix / "pyvenv.cfg").write_text("include-system-site-packages = true\n")
        with mock.patch.object(inputs.sys, "prefix", str(self.prefix)), \
                mock.patch.object(inputs.sys, "base_prefix", str(self.prefix.parent / "python")):
            with self.assertRaisesRegex(ValueError, "System site packages"):
                inputs._environment()

    def test_pinned_sdk_local_aliases_are_bound_and_external_alias_is_rejected(self):
        self.package()
        root = self.paths["platlib"]
        suffix = inputs.importlib.machinery.EXTENSION_SUFFIXES[0]
        native = root / ("pyrealsense2.2.54.2" + suffix)
        native.write_bytes(b"synthetic SDK")
        minor = root / ("pyrealsense2.2.54" + suffix)
        minor.symlink_to(native.name)
        alias = root / ("pyrealsense2" + suffix)
        alias.symlink_to(minor.name)
        result = self.verify()
        self.assertEqual(len(result["sdkAliases"]), 2)
        self.assertTrue(result["sdkRequiresSeparateProvenance"])
        alias.unlink()
        alias.symlink_to(native)
        with self.assertRaisesRegex(ValueError, "SDK alias escapes"):
            self.verify()

    def test_external_distribution_is_rejected(self):
        self.package()
        environment = self.environment()
        real = environment["distributions"][0]
        fake = mock.Mock(metadata=real.metadata, version=real.version)
        fake.locate_file.return_value = self.prefix.parent / "global-site"
        environment["distributions"] = [fake]
        with mock.patch.object(inputs, "_environment", return_value=environment):
            with self.assertRaisesRegex(ValueError, "outside the isolated site"):
                inputs.verify_dependencies(self.lock, self.wheelhouse)


if __name__ == "__main__":
    unittest.main()
