"""Native provider entry point; diagnostics never import the HTTP service."""

import sys

if getattr(sys, "frozen", False):
    from startup_trace import emit
else:
    def emit(_stage):
        pass

emit("entry")
import json
import multiprocessing
from pathlib import Path

emit("entry_imports_complete")


def initialize_tls():
    emit("tls_start")
    if getattr(sys, "frozen", False):
        from native_tls import configure_native_tls

        configure_native_tls()
    emit("tls_complete")


def main():
    multiprocessing.freeze_support()
    emit("freeze_support_complete")
    if not getattr(sys, "frozen", False):
        sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
    if sys.argv[1:] == ["--self-test"]:
        try:
            initialize_tls()
            from native_self_test import run_self_test

            result = run_self_test()
        except Exception as error:
            # No configuration or credential values are involved in this path.
            print(json.dumps({"ok": False, "errorType": type(error).__name__,
                              "error": "Native self-test failed."}), flush=True)
            return 1
        print(json.dumps(result, sort_keys=True), flush=True)
        return 0
    if "--self-test" in sys.argv[1:]:
        print("--self-test takes no additional arguments.", file=sys.stderr)
        return 2
    initialize_tls()
    emit("server_import_start")
    import server

    emit("server_import_complete")
    server.main()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
