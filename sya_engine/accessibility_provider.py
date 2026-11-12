"""
Accessibility Provider for SYA Engine.
Extracts live foreground application, window title, and semantic accessibility text
from macOS AppKit, SQLite work-context store, and optional live JXA probes.
"""

import os
import json
import sqlite3
import subprocess
import time
from typing import Optional, Dict, Any
from dataclasses import dataclass


@dataclass
class AccessibilitySnapshot:
    app_name: str
    bundle_id: str
    window_title: str
    page_url: Optional[str]
    semantic_text: str
    timestamp: float
    source: str
    is_live: bool

    def to_dict(self) -> Dict[str, Any]:
        return {
            "app_name": self.app_name,
            "bundle_id": self.bundle_id,
            "window_title": self.window_title,
            "page_url": self.page_url,
            "semantic_text": self.semantic_text,
            "timestamp": self.timestamp,
            "source": self.source,
            "is_live": self.is_live
        }


def find_ax_reader_bin() -> Optional[str]:
    """Locate the native Apple Accessibility reader binary."""
    base_dir = os.path.dirname(os.path.abspath(__file__))
    candidates = [
        os.path.join(base_dir, "..", "bin", "sya_ax_reader"),
        os.path.join(base_dir, "..", "..", "app", "electron", "bin", "sya_ax_reader"),
        os.path.join(base_dir, "..", "..", "app", "bin", "sya_ax_reader"),
        "/Applications/Save Your Attention.app/Contents/Resources/bin/sya_ax_reader",
        "/tmp/sya_ax_reader"
    ]
    for c in candidates:
        p = os.path.abspath(c)
        if os.path.isfile(p) and os.access(p, os.X_OK):
            return p
    return None


class AccessibilityProvider:
    """Provides foreground application and accessibility semantics."""

    def __init__(self, db_path: Optional[str] = None):
        self.db_path = db_path or os.path.expanduser(
            "~/Library/Application Support/sya-ui/monitor/work-context-history.sqlite"
        )
        self.last_snapshot: Optional[AccessibilitySnapshot] = None

    def get_live_accessibility(self, target_pid: Optional[int] = None) -> Optional[Dict[str, Any]]:
        """
        Query live macOS Accessibility attributes using native Apple Developer API (AXUIElement):
        - Active focused window (kAXFocusedWindowAttribute / kAXMainWindowAttribute)
        - Window topic / subject (kAXTitleAttribute / kAXDescriptionAttribute)
        - Document URL / file path (kAXDocumentAttribute / kAXURLAttribute)
        - Focused semantic content (kAXFocusedUIElementAttribute -> kAXSelectedTextAttribute / kAXValueAttribute)
        - Visible text hierarchy
        """
        bin_path = find_ax_reader_bin()
        if not bin_path:
            return None
        cmd = [bin_path]
        if target_pid and target_pid > 0:
            cmd.append(str(target_pid))
        try:
            res = subprocess.run(cmd, capture_output=True, text=True, timeout=1.0)
            if res.returncode == 0 and res.stdout.strip():
                data = json.loads(res.stdout.strip())
                if data.get("ok"):
                    return data
        except Exception:
            pass
        return None

    def get_live_foreground_app(self) -> Dict[str, Any]:
        """
        Fast (~50ms) zero-permission probe of the frontmost application via AppKit JXA.
        """
        # Try native AX reader first
        live = self.get_live_accessibility()
        if live and live.get("appName") and live.get("appName") != "Unknown":
            return {
                "pid": live.get("pid", 0),
                "name": live.get("appName"),
                "bundleId": live.get("bundleId", "")
            }

        script = (
            'ObjC.import("AppKit"); '
            'var a = $.NSWorkspace.sharedWorkspace.frontmostApplication; '
            'JSON.stringify({pid: Number(a.processIdentifier), name: ObjC.unwrap(a.localizedName) || "", bundleId: ObjC.unwrap(a.bundleIdentifier) || ""});'
        )
        try:
            res = subprocess.run(
                ["osascript", "-l", "JavaScript", "-e", script],
                capture_output=True,
                text=True,
                timeout=1.5
            )
            if res.returncode == 0 and res.stdout.strip():
                return json.loads(res.stdout.strip())
        except Exception:
            pass
        return {"pid": 0, "name": "Unknown", "bundleId": ""}

    def get_latest_stored_record(self, max_age_sec: float = 60.0) -> Optional[Dict[str, Any]]:
        """
        Query the SQLite work-context database populated by SYA-UI's accessibility reader.
        """
        if not os.path.isfile(self.db_path):
            return None

        try:
            conn = sqlite3.connect(f"file:{self.db_path}?mode=ro", uri=True)
            cursor = conn.cursor()
            cursor.execute(
                "SELECT at, payload FROM records ORDER BY at DESC LIMIT 1;"
            )
            row = cursor.fetchone()
            conn.close()

            if row:
                at_ms, payload_str = row
                at_sec = at_ms / 1000.0
                now_sec = time.time()
                # Check age if needed
                payload = json.loads(payload_str)
                payload["_at_sec"] = at_sec
                return payload
        except Exception:
            pass
        return None

    def capture(self, injected_metadata: Optional[Dict[str, Any]] = None) -> AccessibilitySnapshot:
        """
        Retrieve accessibility snapshot using the best available source:
        1. Injected metadata from frontend if provided
        2. Native Apple Developer Accessibility API probe (AXUIElement)
        3. Live AppKit probe combined with SQLite work-context semantics
        """
        t_now = time.time()

        if injected_metadata and isinstance(injected_metadata, dict):
            snap = AccessibilitySnapshot(
                app_name=injected_metadata.get("appName", "Unknown"),
                bundle_id=injected_metadata.get("bundleId", ""),
                window_title=injected_metadata.get("windowTitle", ""),
                page_url=injected_metadata.get("pageUrl"),
                semantic_text=injected_metadata.get("text", "")[:1200],
                timestamp=t_now,
                source="injected_electron_bridge",
                is_live=True
            )
            self.last_snapshot = snap
            return snap

        # 1. Native Accessibility API probe (AXUIElement)
        live_data = self.get_live_accessibility()
        if live_data and live_data.get("appName") and live_data.get("appName") != "Unknown":
            app_name = live_data.get("appName") or "Unknown"
            bundle_id = live_data.get("bundleId") or ""
            window_title = live_data.get("windowTitle") or ""
            doc_url = live_data.get("documentUrl") or ""
            page_url = live_data.get("pageUrl") or (doc_url if doc_url.startswith("http") or doc_url.startswith("file") else None)
            semantic_text = live_data.get("semanticText") or ""

            if not window_title and doc_url:
                clean_doc = doc_url.replace("file://", "")
                window_title = os.path.basename(clean_doc)

            if window_title or semantic_text:
                snap = AccessibilitySnapshot(
                    app_name=app_name,
                    bundle_id=bundle_id,
                    window_title=window_title,
                    page_url=page_url,
                    semantic_text=semantic_text[:1200],
                    timestamp=t_now,
                    source="native_apple_ax_probe",
                    is_live=True
                )
                self.last_snapshot = snap
                return snap

        # 2. Fast live probe of foreground app
        fg = self.get_live_foreground_app()
        live_app = fg.get("name") or "Unknown"
        live_bundle = fg.get("bundleId") or ""

        # 3. Check recent SQLite record
        stored = self.get_latest_stored_record(max_age_sec=300.0)
        stored_app = ""
        stored_title = ""
        stored_url = None
        stored_text = ""

        if stored:
            app_meta = stored.get("app", {})
            stored_app = app_meta.get("name", "")
            stored_title = stored.get("windowTitle", "")
            stored_url = stored.get("pageUrl")
            # If text is available
            stored_text = stored.get("text", "") or ""

        # Fuse live probe with stored record if same app
        window_title = stored_title
        if live_app != stored_app and live_app != "Unknown":
            # App changed more recently than last stored record
            app_name = live_app
            bundle_id = live_bundle
            window_title = f"{live_app} Active Window"
            semantic_text = f"Foreground Application: {live_app} (Bundle: {live_bundle})"
        else:
            app_name = stored_app or live_app
            bundle_id = live_bundle
            semantic_text = stored_text or f"Application: {app_name}, Window: {window_title}"

        snap = AccessibilitySnapshot(
            app_name=app_name,
            bundle_id=bundle_id,
            window_title=window_title,
            page_url=stored_url,
            semantic_text=semantic_text[:1200],
            timestamp=t_now,
            source="hybrid_macos_probe",
            is_live=True
        )
        self.last_snapshot = snap
        return snap
