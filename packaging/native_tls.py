"""Pinned public CA trust for the frozen provider, independent of the host."""

import hashlib
import importlib.metadata
import os
from pathlib import Path
import ssl
import sys


CERTIFI_VERSION = "2026.7.22"
CA_SHA256 = "9cc2a774b5198dcff14d9be1e66091f538975d867ce029a96bce15a55dfd730f"


def _bundle_ca():
    import certifi
    from packaging.version import Version

    pinned_version = Version(CERTIFI_VERSION)
    if (Version(certifi.__version__) != pinned_version
            or Version(importlib.metadata.version("certifi")) != pinned_version):
        raise RuntimeError("The bundled CA package version is not pinned.")
    bundle = Path(sys._MEIPASS).resolve(strict=True)
    ca = Path(certifi.where()).resolve(strict=True)
    try:
        # PyInstaller's PYZ modules have a bundle-relative virtual __file__;
        # only the separately collected CA resource must physically exist.
        Path(certifi.__file__).resolve().relative_to(bundle)
        ca.relative_to(bundle)
    except ValueError as error:
        raise RuntimeError("CA trust must come from the frozen bundle.") from error
    if not ca.is_file() or hashlib.sha256(ca.read_bytes()).hexdigest() != CA_SHA256:
        raise RuntimeError("The bundled CA file does not match its pinned digest.")
    return ca


def _verified_context(ca):
    context = ssl.create_default_context(cafile=str(ca))
    if context.verify_mode != ssl.CERT_REQUIRED or not context.check_hostname:
        raise RuntimeError("TLS certificate and hostname verification are required.")
    if not context.get_ca_certs(binary_form=True):
        raise RuntimeError("The bundled CA trust store is empty.")
    return context


def configure_native_tls():
    """Configure OpenSSL defaults before imports can create HTTPS contexts.

    Empty SSL_CERT_DIR explicitly disables the compiled-in directory fallback;
    deleting the variable would re-enable a build-machine OpenSSL path. This
    candidate intentionally does not merge user, enterprise or Keychain CAs.
    """
    if not getattr(sys, "frozen", False):
        return {"checked": False, "reason": "source-runtime"}
    ca = _bundle_ca()
    _verified_context(ca)
    os.environ["SSL_CERT_FILE"] = str(ca)
    os.environ["SSL_CERT_DIR"] = ""
    return audit_native_tls()


def audit_native_tls():
    """Offline proof that urllib's default context uses only the pinned roots."""
    if not getattr(sys, "frozen", False):
        return {"checked": False, "reason": "source-runtime"}
    ca = _bundle_ca()
    if os.environ.get("SSL_CERT_FILE") != str(ca) or os.environ.get("SSL_CERT_DIR") != "":
        raise RuntimeError("Frozen TLS trust was not initialized.")
    pinned = _verified_context(ca)
    default = ssl.create_default_context()
    if default.verify_mode != ssl.CERT_REQUIRED or not default.check_hostname:
        raise RuntimeError("Default TLS certificate and hostname verification are required.")
    roots = set(pinned.get_ca_certs(binary_form=True))
    if set(default.get_ca_certs(binary_form=True)) != roots:
        raise RuntimeError("Default TLS trust differs from the bundled CA store.")
    return {"checked": True, "provider": "certifi", "version": CERTIFI_VERSION,
            "opensslVersion": ssl.OPENSSL_VERSION,
            "caSha256": CA_SHA256, "trustedCertificates": len(roots),
            "certificateRequired": True, "hostnameChecked": True,
            "externalTrustLoaded": False, "networkRequests": 0}
