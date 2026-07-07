"""
adb_backend.py — ADB-based DeviceBackend implementation.

Wraps DeviceManager, ScreenCapture, Recorder, LogcatCapture into the
unified DeviceBackend protocol so the MCP server never touches ADB directly.
"""
from __future__ import annotations

import json
import logging
import os
import time
from typing import Optional

from .base import BackendInfo, DeviceInfo, ElementInfo, DeviceBackend

# Import existing modules (same directory parent)
import sys
_SCRIPT_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..")
if _SCRIPT_DIR not in sys.path:
    sys.path.insert(0, _SCRIPT_DIR)

from device import DeviceManager
from screencap import ScreenCapture
from recorder import Recorder
from logcat_capture import LogcatCapture

logger = logging.getLogger("android-qa.backend.adb")


class AdbBackend(DeviceBackend):
    """ADB-backed device control — the default and most complete backend."""

    def __init__(self, output_dir: Optional[str] = None):
        self._output_dir = output_dir or os.path.join(
            os.path.dirname(os.path.abspath(__file__)), "..", "..", "output", "mcp_runs"
        )
        os.makedirs(self._output_dir, exist_ok=True)
        self._dm = DeviceManager()
        self._serial: Optional[str] = None
        self._screencap = ScreenCapture(
            self._dm._adb,
            getattr(self._dm, "_android_cli", None) or "",
            output_dir=self._output_dir,
        )
        self._recorder = Recorder(self._dm._adb, output_dir=os.path.join(self._output_dir, "video"))
        self._logcat = LogcatCapture(self._dm._adb, output_dir=os.path.join(self._output_dir, "logs"))

    # ── Lifecycle ─────────────────────────────────────────────────────────

    def get_info(self) -> BackendInfo:
        return BackendInfo(
            name="adb",
            capabilities=[
                "tap", "long_press", "double_tap", "swipe", "drag",
                "type_text", "type_unicode", "keyevent",
                "screenshot", "layout_dump", "video_record",
                "clipboard_read", "clipboard_write",
                "shell", "push", "pull",
                "install", "launch", "force_stop",
                "meminfo", "gfxinfo", "startup_time",
                "notifications",
            ],
        )

    def list_devices(self) -> list[DeviceInfo]:
        result = []
        for d in self._dm.list_devices():
            info = self._dm.get_device_info(d.serial) if d.ready else {}
            w, h = self._parse_screen_size(info.get("screen_size", ""))
            result.append(DeviceInfo(
                serial=d.serial,
                model=info.get("model", d.model),
                android_version=info.get("android_version", ""),
                api_level=info.get("api_level", ""),
                screen_width=w,
                screen_height=h,
                is_emulator=d.is_emulator,
                emulator_type=d.emulator_type,
            ))
        return result

    def connect(self, serial: Optional[str] = None) -> DeviceInfo:
        if serial:
            devs = self._dm.list_devices()
            dev = next((d for d in devs if d.serial == serial), None)
        else:
            dev = self._dm.get_ready_device()
        if not dev:
            raise RuntimeError("No device found")
        self._serial = dev.serial
        info = self._dm.get_device_info(dev.serial)
        w, h = self._parse_screen_size(info.get("screen_size", ""))
        return DeviceInfo(
            serial=dev.serial,
            model=info.get("model", dev.model),
            android_version=info.get("android_version", ""),
            api_level=info.get("api_level", ""),
            screen_width=w,
            screen_height=h,
            is_emulator=dev.is_emulator,
            emulator_type=dev.emulator_type,
        )

    @property
    def _s(self) -> str:
        """Current device serial — raises if not connected."""
        if not self._serial:
            raise RuntimeError("No device connected. Call connect() first.")
        return self._serial

    # ── App lifecycle ─────────────────────────────────────────────────────

    def launch(self, package: str, activity: str = "") -> bool:
        return self._dm.launch_app(package, activity, serial=self._s)

    def is_app_alive(self, package: str) -> bool:
        r = self._dm._run(["shell", "dumpsys", "activity", "activities"], serial=self._s)
        return f"mResumedActivity" in r.stdout and package in r.stdout

    def force_stop(self, package: str) -> None:
        self._dm._run(["shell", "am", "force-stop", package], serial=self._s)

    def install(self, apk_path: str) -> bool:
        return self._dm.install_apk(apk_path, serial=self._s)

    # ── Input ─────────────────────────────────────────────────────────────

    def tap(self, x: int, y: int) -> None:
        self._dm.tap(x, y, serial=self._s)

    def long_press(self, x: int, y: int, duration_ms: int = 1000) -> None:
        self._dm._run(["shell", "input", "swipe", str(x), str(y), str(x), str(y), str(duration_ms)],
                      serial=self._s)

    def double_tap(self, x: int, y: int) -> None:
        self._dm.double_tap(x, y, serial=self._s)

    def swipe(self, direction: str, duration_ms: int = 300) -> None:
        w, h = self._dm.get_screen_size(serial=self._s)
        margin = 50
        cx = w // 2
        cy = h // 2
        deltas = {
            "up": (cx, cy + margin, cx, cy - margin),
            "down": (cx, cy - margin, cx, cy + margin),
            "left": (cx + margin, cy, cx - margin, cy),
            "right": (cx - margin, cy, cx + margin, cy),
        }
        coords = deltas.get(direction)
        if not coords:
            raise ValueError(f"Invalid direction: {direction}")
        self._dm.swipe(*coords, duration_ms=duration_ms, serial=self._s)

    def drag(self, x1: int, y1: int, x2: int, y2: int, duration_ms: int = 500) -> None:
        self._dm.drag(x1, y1, x2, y2, duration_ms, serial=self._s)

    def type_text(self, text: str, clear_first: bool = True) -> dict:
        if clear_first:
            self._dm._run(["shell", "input", "keyevent", "KEYCODE_CTRL_LEFT", "KEYCODE_A"], serial=self._s)
            time.sleep(0.05)
            self._dm._run(["shell", "input", "keyevent", "KEYCODE_DEL"], serial=self._s)
            time.sleep(0.05)
        if all(ord(c) < 128 for c in text):
            self._dm.text_input(text, serial=self._s)
            return {"ok": True, "method": "input_text"}
        return self.type_unicode(text, clear_first=False)

    def type_unicode(self, text: str, clear_first: bool = True) -> dict:
        if clear_first:
            self._dm._run(["shell", "input", "keyevent", "KEYCODE_CTRL_LEFT", "KEYCODE_A"], serial=self._s)
            time.sleep(0.05)
            self._dm._run(["shell", "input", "keyevent", "KEYCODE_DEL"], serial=self._s)
            time.sleep(0.05)
        return self._dm.input_text_unicode(text, serial=self._s)

    def keyevent(self, key: str) -> None:
        key_map = {
            "back": "KEYCODE_BACK", "home": "KEYCODE_HOME",
            "recent": "KEYCODE_APP_SWITCH", "enter": "KEYCODE_ENTER",
            "tab": "KEYCODE_TAB", "delete": "KEYCODE_DEL",
        }
        self._dm.keyevent(key_map.get(key, key), serial=self._s)

    # ── Clipboard ─────────────────────────────────────────────────────────

    def clipboard_set(self, text: str) -> dict:
        return self._dm.clipboard_set(text, serial=self._s)

    def clipboard_get(self) -> str:
        return self._dm.clipboard_get(serial=self._s)

    # ── UI inspection ─────────────────────────────────────────────────────

    def screenshot(self, name: str = "") -> str:
        sname = name or f"screenshot_{int(time.time())}"
        return self._screencap.capture_screenshot(self._s, sname)

    def layout_dump(self) -> list[ElementInfo]:
        layout_path = self._screencap.capture_layout(self._s, f"layout_{int(time.time())}")
        try:
            with open(layout_path) as f:
                data = json.load(f)
            return [self._parse_element(e) for e in data]
        except Exception as e:
            logger.error("layout_dump failed: %s", e)
            return []

    def find_element(self, text: str = "", resource_id: str = "") -> Optional[ElementInfo]:
        elements = self.layout_dump()
        for el in elements:
            if text and text in el.text:
                return el
            if resource_id and resource_id in el.resource_id:
                return el
        return None

    def get_text(self, text: str = "") -> Optional[ElementInfo]:
        return self.find_element(text=text)

    def wait_element(self, text: str, timeout_s: int = 10) -> bool:
        deadline = time.time() + timeout_s
        while time.time() < deadline:
            if self.find_element(text=text):
                return True
            time.sleep(0.5)
        return False

    def scroll_find(self, text: str, direction: str = "up", max_scrolls: int = 10,
                    scroll_pause: float = 1.0) -> Optional[ElementInfo]:
        for _ in range(max_scrolls):
            el = self.find_element(text=text)
            if el:
                return el
            self.swipe(direction)
            time.sleep(scroll_pause)
        return None

    def element_state(self, text: str = "", resource_id: str = "") -> Optional[dict]:
        el = self.find_element(text=text, resource_id=resource_id)
        if not el:
            return None
        return {
            "enabled": el.enabled,
            "checked": el.checked,
            "selected": el.selected,
            "scrollable": el.scrollable,
            "focusable": el.focusable,
        }

    # ── System ────────────────────────────────────────────────────────────

    def notifications(self, action: str) -> None:
        if action == "expand":
            self._dm.notifications_expand(serial=self._s)
        else:
            self._dm.notifications_collapse(serial=self._s)

    def shell(self, command: str, timeout_s: int = 30) -> dict:
        return self._dm.run_shell(command, serial=self._s, timeout=timeout_s)

    # ── File transfer ─────────────────────────────────────────────────────

    def push_file(self, local_path: str, device_path: str) -> bool:
        return self._dm.push_file(local_path, device_path, serial=self._s)

    def pull_file(self, device_path: str, local_path: str) -> bool:
        return self._dm.pull_file(device_path, local_path, serial=self._s)

    # ── Performance ───────────────────────────────────────────────────────

    def measure_startup(self, package: str) -> dict:
        return self._dm.measure_startup(package, serial=self._s)

    def dump_meminfo(self, package: str) -> dict:
        return self._dm.dump_meminfo(package, serial=self._s)

    def dump_gfxinfo(self, package: str) -> dict:
        return self._dm.dump_gfxinfo(package, serial=self._s)

    # ── Recording ─────────────────────────────────────────────────────────

    def recording_start(self, filename: str = "recording.mp4") -> str:
        session = self._recorder.start(self._s, filename)
        return session.remote_path

    def recording_stop(self) -> Optional[str]:
        return self._recorder.stop(self._s)

    # ── Logcat ────────────────────────────────────────────────────────────

    def logcat_start(self, watch_package: str = "") -> str:
        return self._logcat.start(serial=self._s, watch_package=watch_package or None)

    def logcat_stop(self) -> dict:
        path = self._logcat.stop()
        analysis = self._logcat.analyze()
        return {
            "log_file": path,
            "total_lines": analysis.total_lines,
            "errors": len(analysis.errors),
            "warnings": len(analysis.warnings),
            "crashes": [{"message": e.message, "tag": e.tag} for e in analysis.crashes],
            "anrs": [{"message": e.message, "tag": e.tag} for e in analysis.anrs],
            "ooms": [{"message": e.message, "tag": e.tag} for e in analysis.ooms],
            "app_alive": self._logcat.is_app_alive() if self._logcat._app_package else None,
        }

    # ── Helpers ───────────────────────────────────────────────────────────

    @staticmethod
    def _parse_screen_size(raw: str) -> tuple[int, int]:
        try:
            w, h = raw.split("x")
            return int(w), int(h)
        except (ValueError, AttributeError):
            return 1080, 1920

    @staticmethod
    def _parse_element(e: dict) -> ElementInfo:
        bounds = e.get("bounds", "")
        cx, cy = 0, 0
        if bounds:
            try:
                import re
                m = re.findall(r"\[(\d+),(\d+)\]", bounds)
                if len(m) == 2:
                    cx = (int(m[0][0]) + int(m[1][0])) // 2
                    cy = (int(m[0][1]) + int(m[1][1])) // 2
            except Exception:
                pass
        return ElementInfo(
            text=e.get("text", ""),
            resource_id=e.get("resource_id", ""),
            class_name=e.get("class_name", ""),
            bounds=bounds,
            center_x=e.get("center_x", cx),
            center_y=e.get("center_y", cy),
            clickable=e.get("clickable", False),
            enabled=e.get("enabled", True),
            checked=e.get("checked", False),
            selected=e.get("selected", False),
            scrollable=e.get("scrollable", False),
            focusable=e.get("focusable", False),
        )
