"""Bounded frozen-startup evidence; never log input, paths, or exception text."""

import sys
import time


STAGES = (
    "bootstrap", "entry", "entry_imports_complete", "freeze_support_complete",
    "tls_start", "tls_complete", "server_import_start", "server_import_complete",
    "config_start", "config_complete", "bind_start", "bind_complete", "loop_ready",
)
_emitted = set()


def emit(stage):
    """The first hook observes bootstrap, not bootloader extraction completion."""
    try:
        if not getattr(sys, "frozen", False) or stage not in STAGES or stage in _emitted:
            return
        # Do not retry a failed diagnostic write or let logging stop the service.
        _emitted.add(stage)
        stamp = time.monotonic_ns()
        if not isinstance(stamp, int) or isinstance(stamp, bool) or not 0 <= stamp < 10**20:
            return
        sys.stderr.write(f"[sya-startup-v1] {stage} {stamp}\n")
        sys.stderr.flush()
    except Exception:
        pass
