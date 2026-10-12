#!/usr/bin/env python3
"""Interactive command-line control for monitor mock attention status.

Run this next to the mock server. Each keypress posts a new attention status
to the mock API, and the server emits an SSE event to connected UI clients.
"""

from __future__ import annotations

import argparse
import ipaddress
import json
import os
import select
import sys
import termios
import time
import tty
import urllib.error
import urllib.parse
import urllib.request


KEYMAP = {
    "f": ("focused", 0.95),
    "d": ("distracted", 0.35),
    "a": ("away", 0.1),
    "u": ("unknown", 0.0),
}


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


def _authenticated_base_url(base_url: str) -> str:
    try:
        parsed = urllib.parse.urlsplit(base_url)
        host = "127.0.0.1" if parsed.hostname == "localhost" else parsed.hostname
        if (parsed.scheme != "http" or not host or parsed.username is not None or
                parsed.password is not None or parsed.query or parsed.fragment or
                not ipaddress.ip_address(host).is_loopback):
            raise ValueError
        port = parsed.port or 80
        authority = f"[{host}]:{port}" if ":" in host else f"{host}:{port}"
        return urllib.parse.urlunsplit(("http", authority, parsed.path, "", ""))
    except ValueError:
        raise urllib.error.URLError("Authenticated control requires a loopback HTTP base URL without credentials, query or fragment.") from None


def _post_status(base_url: str, status: str, confidence: float) -> dict:
    token = os.environ.get("FUNCTION_RUNTIME_TOKEN", "").strip()
    headers = {"Content-Type": "application/json"}
    open_request = urllib.request.urlopen
    if token:
        base_url = _authenticated_base_url(base_url)
        if not token.isascii():
            raise urllib.error.URLError("Runtime token must be ASCII.")
        headers["Authorization"] = f"Bearer {token}"
        # No proxy or redirect may forward the per-runtime secret elsewhere.
        open_request = urllib.request.build_opener(urllib.request.ProxyHandler({}), _NoRedirect()).open
    payload = {
        "attentionStatus": status,
        "confidence": confidence,
        "source": "mock-cmd",
        "configured": True,
        "reason": "set from attention_control.py",
    }
    request = urllib.request.Request(
        f"{base_url.rstrip('/')}/api/monitor/mock/attention/status",
        data=json.dumps(payload).encode("utf-8"),
        headers=headers,
        method="POST",
    )
    with open_request(request, timeout=3) as response:
        return json.loads(response.read().decode("utf-8"))


def _print_help(base_url: str) -> None:
    print(f"Monitor mock attention control: {base_url.rstrip('/')}")
    print("Keys: f=focused, d=distracted, a=away, u=unknown, h=help, q=quit")
    print("Each status update is posted to /api/monitor/mock/attention/status.")


def _read_key(timeout: float = 0.1) -> str | None:
    ready, _, _ = select.select([sys.stdin], [], [], timeout)
    if not ready:
        return None
    return sys.stdin.read(1)


def main() -> int:
    parser = argparse.ArgumentParser(description="Control monitor mock attention status from the shell.")
    parser.add_argument("--base-url", default="http://127.0.0.1:8770", help="Monitor mock server base URL.")
    args = parser.parse_args()

    _print_help(args.base_url)
    old_settings = termios.tcgetattr(sys.stdin)
    try:
        tty.setcbreak(sys.stdin.fileno())
        while True:
            key = _read_key()
            if key is None:
                continue
            key = key.lower()
            if key == "q":
                print("\nquit")
                return 0
            if key == "h":
                print()
                _print_help(args.base_url)
                continue
            if key not in KEYMAP:
                print(f"\nignored key: {key!r}")
                continue

            status, confidence = KEYMAP[key]
            try:
                result = _post_status(args.base_url, status, confidence)
                data = result.get("data", {})
                updated_at = data.get("lastUpdatedAt", "")
                print(f"\n{time.strftime('%H:%M:%S')} attentionStatus={status} confidence={confidence} updatedAt={updated_at}")
            except urllib.error.URLError as error:
                print(f"\nrequest failed: {error}")
            except json.JSONDecodeError as error:
                print(f"\ninvalid JSON response: {error}")
    except KeyboardInterrupt:
        print("\nquit")
        return 0
    finally:
        termios.tcsetattr(sys.stdin, termios.TCSADRAIN, old_settings)


if __name__ == "__main__":
    raise SystemExit(main())
