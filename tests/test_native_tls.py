"""Offline TLS verification, using only temporary synthetic loopback certificates."""

import contextlib
import ast
import hashlib
import http.server
import importlib.util
import io
import json
import os
from pathlib import Path
import shutil
import socketserver
import ssl
import subprocess
import sys
import tempfile
import threading
import types
import unittest
import urllib.error
import urllib.request
from unittest import mock


ROOT = Path(__file__).resolve().parents[1]


def load(name, relative):
    spec = importlib.util.spec_from_file_location(name, ROOT / relative)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


tls = load("native_tls_tests", "packaging/native_tls.py")
entry = load("native_tls_entry_tests", "packaging/native_entry.py")


def spec_openssl_functions():
    # Exercise spec selection without running Analysis, signing, or a build.
    tree = ast.parse((ROOT / "packaging/monitor.spec").read_text())
    selected = [node for node in tree.body if isinstance(node, ast.FunctionDef)
                and node.name in {"interpreter_openssl_libraries", "preserve_interpreter_openssl"}]
    namespace = {"Path": Path}
    exec(compile(ast.Module(body=selected, type_ignores=[]), "monitor.spec", "exec"), namespace)
    return namespace


class NativeOpenSslSelectionTests(unittest.TestCase):
    def fixture(self, root):
        files = {name: root / name for name in ("_ssl.so", "_hashlib.so", "libssl.3.dylib", "libcrypto.3.dylib")}
        for name, path in files.items():
            path.write_text("synthetic " + name)
        pair = {name: str(files[name].resolve()) for name in ("libssl.3.dylib", "libcrypto.3.dylib")}
        imports = {str(files["_ssl.so"]): list(pair.items()),
                   str(files["_hashlib.so"]): [("libcrypto.3.dylib", pair["libcrypto.3.dylib"])],
                   pair["libssl.3.dylib"]: [("libcrypto.3.dylib", pair["libcrypto.3.dylib"])]}
        return files, pair, imports

    def test_selects_exact_interpreter_linked_pair(self):
        functions = spec_openssl_functions()
        with tempfile.TemporaryDirectory() as temporary:
            files, pair, imports = self.fixture(Path(temporary))
            selected = functions["interpreter_openssl_libraries"](
                (files["_ssl.so"], files["_hashlib.so"]), imports.__getitem__)
            self.assertEqual(selected, pair)

    def test_missing_or_inconsistent_interpreter_pair_is_rejected(self):
        select = spec_openssl_functions()["interpreter_openssl_libraries"]
        with tempfile.TemporaryDirectory() as temporary:
            files, pair, imports = self.fixture(Path(temporary))
            extensions = (files["_ssl.so"], files["_hashlib.so"])
            for bad in ([("libcrypto.3.dylib", None)], []):
                broken = {**imports, str(files["_ssl.so"]): bad}
                with self.assertRaises(RuntimeError):
                    select(extensions, broken.__getitem__)
            other = Path(temporary) / "different-libcrypto.3.dylib"
            other.write_text("another library")
            for referring in (str(files["_hashlib.so"]), pair["libssl.3.dylib"]):
                broken = {**imports, referring: [("libcrypto.3.dylib", str(other))]}
                with self.assertRaises(RuntimeError):
                    select(extensions, broken.__getitem__)

    def test_cv2_root_alias_cannot_displace_interpreter_but_originals_remain(self):
        preserve = spec_openssl_functions()["preserve_interpreter_openssl"]
        with tempfile.TemporaryDirectory() as temporary:
            _, pair, _ = self.fixture(Path(temporary))
            originals = [("cv2/.dylibs/" + name, "/wheel/cv2/" + name, "BINARY") for name in pair]
            unrelated = [("other.dylib", "/wheel/other.dylib", "BINARY")]
            aliases = [(name, "cv2/.dylibs/" + name, "SYMLINK") for name in pair]
            data = [("certifi/cacert.pem", "/wheel/certifi/cacert.pem", "DATA")]
            # Actual Analysis places these root SYMLINK entries in a.datas.
            result, result_data = preserve(originals + unrelated, aliases + data, pair)
            self.assertEqual(result_data, data)
            self.assertEqual(result[:len(originals + unrelated)], originals + unrelated)
            self.assertEqual({name: (source, kind) for name, source, kind in result if name in pair},
                             {name: (source, "BINARY") for name, source in pair.items()})
            self.assertFalse(any(row[2] == "SYMLINK" for row in result))

    def test_unknown_collision_or_missing_explicit_input_fails_closed(self):
        preserve = spec_openssl_functions()["preserve_interpreter_openssl"]
        with tempfile.TemporaryDirectory() as temporary:
            _, pair, _ = self.fixture(Path(temporary))
            correct = [(name, source, "BINARY") for name, source in pair.items()]
            self.assertEqual(preserve(correct, [], pair), (sorted(correct), []))
            for bad in (correct[1:], [(correct[0][0], "unknown/crypto", "SYMLINK"), correct[1]]):
                with self.assertRaises(RuntimeError):
                    preserve([], bad, pair)


@contextlib.contextmanager
def frozen_ca(directory, data, expected_hash=None):
    root = Path(directory)
    package = root / "certifi"
    package.mkdir(exist_ok=True)
    module_path = package / "__init__.py"
    ca = package / "cacert.pem"
    ca.write_bytes(data)
    certifi = types.ModuleType("certifi")
    certifi.__file__ = str(module_path)
    # This is the actual locked wheel's module spelling; wheel metadata uses
    # the PEP 440 normalized form 2026.7.22.
    certifi.__version__ = "2026.07.22"
    certifi.where = lambda: str(ca)
    with mock.patch.object(sys, "frozen", True, create=True), \
            mock.patch.object(sys, "_MEIPASS", str(root), create=True), \
            mock.patch.dict(sys.modules, {"certifi": certifi}), \
            mock.patch.object(tls.importlib.metadata, "version", return_value="2026.7.22"), \
            mock.patch.object(tls, "CA_SHA256", expected_hash or hashlib.sha256(data).hexdigest()), \
            mock.patch.dict(os.environ, {"SSL_CERT_FILE": "/nonexistent/external-ca.pem",
                                         "SSL_CERT_DIR": "/nonexistent/external-ca-directory"}):
        yield ca, certifi


class NativeTlsBoundaryTests(unittest.TestCase):
    def test_actual_wheel_version_spelling_and_virtual_pyz_module_path(self):
        with tempfile.TemporaryDirectory() as temporary, frozen_ca(temporary, b"synthetic") as (ca, certifi):
            self.assertFalse(Path(certifi.__file__).exists())
            self.assertEqual(tls._bundle_ca(), ca.resolve())
            with mock.patch.object(tls.importlib.metadata, "version", return_value="2026.6.17"):
                with self.assertRaisesRegex(RuntimeError, "version"):
                    tls._bundle_ca()

    def test_source_entry_does_not_touch_tls_environment_or_import_certifi(self):
        with mock.patch.object(sys, "frozen", False, create=True), \
                mock.patch.dict(os.environ, {"SSL_CERT_FILE": "source-ca", "SSL_CERT_DIR": "source-dir"}), \
                mock.patch.object(tls, "_bundle_ca", side_effect=AssertionError("source trust was inspected")):
            self.assertEqual(tls.configure_native_tls(), {"checked": False, "reason": "source-runtime"})
            self.assertEqual(tls.audit_native_tls(), {"checked": False, "reason": "source-runtime"})
            self.assertEqual(os.environ["SSL_CERT_FILE"], "source-ca")
            self.assertEqual(os.environ["SSL_CERT_DIR"], "source-dir")

    def test_corrupt_bundle_fails_before_changing_environment(self):
        with tempfile.TemporaryDirectory() as temporary, frozen_ca(temporary, b"tampered", "0" * 64):
            with self.assertRaisesRegex(RuntimeError, "digest"):
                tls.configure_native_tls()
            self.assertEqual(os.environ["SSL_CERT_FILE"], "/nonexistent/external-ca.pem")

    def test_external_ca_and_module_paths_are_rejected(self):
        with tempfile.TemporaryDirectory() as temporary, tempfile.TemporaryDirectory() as outside:
            external = Path(outside) / "external.pem"
            external.write_bytes(b"synthetic")
            with frozen_ca(temporary, b"synthetic") as (ca, certifi):
                certifi.where = lambda: str(external)
                with self.assertRaisesRegex(RuntimeError, "frozen bundle"):
                    tls.configure_native_tls()
                certifi.where = lambda: str(ca)
                certifi.__file__ = str(external)
                with self.assertRaisesRegex(RuntimeError, "frozen bundle"):
                    tls.configure_native_tls()

    def test_wrong_version_and_invalid_pem_are_rejected(self):
        with tempfile.TemporaryDirectory() as temporary, frozen_ca(temporary, b"not a CA") as (_, certifi):
            certifi.__version__ = "0.0.0"
            with self.assertRaisesRegex(RuntimeError, "version"):
                tls.configure_native_tls()
            certifi.__version__ = tls.CERTIFI_VERSION
            with self.assertRaises(ssl.SSLError):
                tls.configure_native_tls()

    def test_native_entry_initializes_trust_before_self_test_and_server(self):
        events = []
        fake_tls = types.ModuleType("native_tls")
        fake_tls.configure_native_tls = lambda: events.append("tls")
        fake_test = types.ModuleType("native_self_test")
        fake_test.run_self_test = lambda: events.append("self-test") or {"ok": True}
        fake_server = types.ModuleType("server")
        fake_server.main = lambda: events.append("server")
        with mock.patch.object(sys, "frozen", True, create=True), \
                mock.patch.object(entry.multiprocessing, "freeze_support"), \
                mock.patch.dict(sys.modules, {"native_tls": fake_tls, "native_self_test": fake_test,
                                              "server": fake_server}), \
                contextlib.redirect_stdout(io.StringIO()):
            with mock.patch.object(sys, "argv", ["native", "--self-test"]):
                self.assertEqual(entry.main(), 0)
            self.assertEqual(events, ["tls", "self-test"])
            events.clear()
            with mock.patch.object(sys, "argv", ["native"]):
                self.assertEqual(entry.main(), 0)
            self.assertEqual(events, ["tls", "server"])

    def test_self_test_tls_failure_is_machine_readable_and_does_not_run(self):
        with mock.patch.object(entry, "initialize_tls", side_effect=RuntimeError("CA digest mismatch")), \
                mock.patch.object(sys, "argv", ["native", "--self-test"]), \
                contextlib.redirect_stdout(io.StringIO()) as output:
            self.assertEqual(entry.main(), 1)
        self.assertEqual(json.loads(output.getvalue()),
                         {"ok": False, "errorType": "RuntimeError", "error": "Native self-test failed."})


class QuietHandler(http.server.BaseHTTPRequestHandler):
    def do_GET(self):
        body = b"synthetic local TLS fixture"
        self.send_response(200)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *_args):
        pass


class LoopbackServer(http.server.HTTPServer):
    def server_bind(self):
        # The fixture only needs the numeric address, never reverse DNS.
        socketserver.TCPServer.server_bind(self)
        self.server_name, self.server_port = self.server_address[:2]


@unittest.skipUnless(shutil.which("openssl"), "synthetic TLS certificates require openssl")
class NativeTlsLoopbackTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.temporary = tempfile.TemporaryDirectory(prefix="sya-native-tls-test-")
        cls.root = Path(cls.temporary.name)
        cls.openssl = shutil.which("openssl")

        def openssl(*args):
            subprocess.run([cls.openssl, *args], cwd=cls.root, check=True,
                           stdout=subprocess.DEVNULL, stderr=subprocess.PIPE, timeout=15,
                           env={"PATH": os.defpath, "HOME": str(cls.root), "OPENSSL_CONF": os.devnull})

        try:
            # Every key is synthetic, temporary, and discarded on test cleanup.
            for name in ("trusted", "untrusted"):
                (cls.root / f"{name}.cnf").write_text(
                    "[req]\nprompt=no\ndistinguished_name=dn\nx509_extensions=ca\n"
                    f"[dn]\nCN=SYA synthetic {name} test root\n[ca]\n"
                    "basicConstraints=critical,CA:TRUE\nkeyUsage=critical,keyCertSign,cRLSign\n"
                    "subjectKeyIdentifier=hash\nauthorityKeyIdentifier=keyid:always\n")
                openssl("req", "-new", "-x509", "-newkey", "rsa:2048", "-nodes", "-days", "1",
                        "-config", f"{name}.cnf", "-keyout", f"{name}.key", "-out", f"{name}.pem")
            (cls.root / "server.cnf").write_text(
                "[req]\nprompt=no\ndistinguished_name=dn\n[dn]\nCN=localhost\n")
            openssl("req", "-new", "-newkey", "rsa:2048", "-nodes", "-config", "server.cnf",
                    "-keyout", "server.key", "-out", "server.csr")
            (cls.root / "server.ext").write_text(
                "basicConstraints=critical,CA:FALSE\nkeyUsage=critical,digitalSignature,keyEncipherment\n"
                "extendedKeyUsage=serverAuth\nsubjectAltName=DNS:localhost\n"
                "subjectKeyIdentifier=hash\nauthorityKeyIdentifier=keyid,issuer\n")
            openssl("x509", "-req", "-in", "server.csr", "-CA", "trusted.pem", "-CAkey", "trusted.key",
                    "-CAcreateserial", "-days", "1", "-extfile", "server.ext", "-out", "server.pem")
        except BaseException:
            cls.temporary.cleanup()
            raise

    @classmethod
    def tearDownClass(cls):
        cls.temporary.cleanup()

    @contextlib.contextmanager
    def server(self):
        httpd = LoopbackServer(("127.0.0.1", 0), QuietHandler)
        context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
        context.load_cert_chain(str(self.root / "server.pem"), str(self.root / "server.key"))
        httpd.socket = context.wrap_socket(httpd.socket, server_side=True)
        thread = threading.Thread(target=httpd.serve_forever, kwargs={"poll_interval": 0.02}, daemon=True)
        thread.start()
        try:
            yield httpd.server_port
        finally:
            httpd.shutdown()
            httpd.server_close()
            thread.join(timeout=2)
            self.assertFalse(thread.is_alive(), "Loopback TLS fixture did not stop")

    def opener(self):
        # A real urllib HTTPSHandler with its normal default SSL context.
        return urllib.request.build_opener(urllib.request.ProxyHandler({}))

    def test_valid_certificate_and_hostname_succeed_with_only_bundled_roots(self):
        with tempfile.TemporaryDirectory() as directory, \
                frozen_ca(directory, (self.root / "trusted.pem").read_bytes()) as (ca, _), self.server() as port:
            result = tls.configure_native_tls()
            self.assertEqual(result["trustedCertificates"], 1)
            self.assertTrue(result["certificateRequired"] and result["hostnameChecked"])
            self.assertFalse(result["externalTrustLoaded"])
            paths = ssl.get_default_verify_paths()
            self.assertEqual(paths.cafile, str(ca.resolve()))
            self.assertIsNone(paths.capath)
            with self.opener().open(f"https://localhost:{port}/", timeout=3) as response:
                self.assertEqual(response.read(), b"synthetic local TLS fixture")

    def test_trusted_certificate_with_wrong_hostname_is_rejected(self):
        with tempfile.TemporaryDirectory() as directory, \
                frozen_ca(directory, (self.root / "trusted.pem").read_bytes()), self.server() as port:
            tls.configure_native_tls()
            with self.assertRaises(urllib.error.URLError) as raised:
                self.opener().open(f"https://127.0.0.1:{port}/", timeout=3)
            self.assertIsInstance(raised.exception.reason, ssl.SSLCertVerificationError)

    def test_untrusted_certificate_is_rejected(self):
        with tempfile.TemporaryDirectory() as directory, \
                frozen_ca(directory, (self.root / "untrusted.pem").read_bytes()), self.server() as port:
            tls.configure_native_tls()
            with self.assertRaises(urllib.error.URLError) as raised:
                self.opener().open(f"https://localhost:{port}/", timeout=3)
            self.assertIsInstance(raised.exception.reason, ssl.SSLCertVerificationError)

    def test_self_test_rejects_removed_or_overridden_trust(self):
        with tempfile.TemporaryDirectory() as directory, \
                frozen_ca(directory, (self.root / "trusted.pem").read_bytes()):
            tls.configure_native_tls()
            os.environ["SSL_CERT_DIR"] = "/nonexistent/external-directory"
            with self.assertRaisesRegex(RuntimeError, "not initialized"):
                tls.audit_native_tls()


if __name__ == "__main__":
    unittest.main()
