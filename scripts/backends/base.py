"""
base.py — DeviceBackend protocol definition.

All device control backends must implement this interface.
The MCP server calls only these methods — never backend internals.
"""
from __future__ import annotations

import abc
from dataclasses import dataclass, field
from typing import Optional


@dataclass
class BackendInfo:
    """Static metadata about a backend."""
    name: str           # e.g. "adb", "scrcpy"
    version: str = ""   # backend tool version
    capabilities: list[str] = field(default_factory=list)
    # Known capabilities:
    #   "tap", "long_press", "double_tap", "swipe", "drag",
    #   "type_text", "type_unicode", "keyevent",
    #   "screenshot", "layout_dump", "video_record",
    #   "clipboard_read", "clipboard_write",
    #   "shell", "push", "pull",
    #   "install", "launch", "force_stop",
    #   "meminfo", "gfxinfo", "startup_time",
    #   "notifications"


@dataclass
class DeviceInfo:
    """Information about a connected device."""
    serial: str
    model: str = ""
    android_version: str = ""
    api_level: str = ""
    screen_width: int = 1080
    screen_height: int = 1920
    density: int = 480
    is_emulator: bool = False
    emulator_type: str = ""  # "avd", "genymotion", etc.


@dataclass
class ElementInfo:
    """A UI element found on screen."""
    text: str = ""
    resource_id: str = ""
    class_name: str = ""
    bounds: str = ""       # "[x1,y1][x2,y2]"
    center_x: int = 0
    center_y: int = 0
    clickable: bool = False
    enabled: bool = True
    checked: bool = False
    selected: bool = False
    scrollable: bool = False
    focusable: bool = False


class DeviceBackend(abc.ABC):
    """Protocol that every device backend must implement.

    Methods raise RuntimeError on connectivity issues and return
    structured results on success.  The MCP server wraps all calls
    in try/except and converts exceptions to error JSON.
    """

    # ── Lifecycle ─────────────────────────────────────────────────────────

    @abc.abstractmethod
    def get_info(self) -> BackendInfo:
        """Return static backend metadata."""

    @abc.abstractmethod
    def list_devices(self) -> list[DeviceInfo]:
        """List all connected devices."""

    @abc.abstractmethod
    def connect(self, serial: Optional[str] = None) -> DeviceInfo:
        """Connect to a device (or auto-discover). Returns device info."""

    # ── App lifecycle ─────────────────────────────────────────────────────

    @abc.abstractmethod
    def launch(self, package: str, activity: str = "") -> bool:
        """Launch an app. Returns True on success."""

    @abc.abstractmethod
    def is_app_alive(self, package: str) -> bool:
        """Check if app is in foreground."""

    @abc.abstractmethod
    def force_stop(self, package: str) -> None:
        """Force-stop an app."""

    @abc.abstractmethod
    def install(self, apk_path: str) -> bool:
        """Install an APK. Returns True on success."""

    # ── Input ─────────────────────────────────────────────────────────────

    @abc.abstractmethod
    def tap(self, x: int, y: int) -> None:
        """Tap at coordinates."""

    @abc.abstractmethod
    def long_press(self, x: int, y: int, duration_ms: int = 1000) -> None:
        """Long-press at coordinates."""

    @abc.abstractmethod
    def double_tap(self, x: int, y: int) -> None:
        """Double-tap at coordinates."""

    @abc.abstractmethod
    def swipe(self, direction: str, duration_ms: int = 300) -> None:
        """Swipe in a direction: up/down/left/right."""

    @abc.abstractmethod
    def drag(self, x1: int, y1: int, x2: int, y2: int, duration_ms: int = 500) -> None:
        """Drag from (x1,y1) to (x2,y2)."""

    @abc.abstractmethod
    def type_text(self, text: str, clear_first: bool = True) -> dict:
        """Type text. Returns {ok, method, error?}."""

    @abc.abstractmethod
    def type_unicode(self, text: str, clear_first: bool = True) -> dict:
        """Type Unicode text. Returns {ok, method, error?}."""

    @abc.abstractmethod
    def keyevent(self, key: str) -> None:
        """Send a key event (e.g. 'back', 'home', 'enter')."""

    # ── Clipboard ─────────────────────────────────────────────────────────

    @abc.abstractmethod
    def clipboard_set(self, text: str) -> dict:
        """Set clipboard. Returns {ok, method, error?}."""

    @abc.abstractmethod
    def clipboard_get(self) -> str:
        """Get clipboard text (best-effort)."""

    # ── UI inspection ─────────────────────────────────────────────────────

    @abc.abstractmethod
    def screenshot(self, name: str = "") -> str:
        """Take a screenshot. Returns local file path."""

    @abc.abstractmethod
    def layout_dump(self) -> list[ElementInfo]:
        """Dump UI hierarchy. Returns list of elements."""

    @abc.abstractmethod
    def find_element(self, text: str = "", resource_id: str = "") -> Optional[ElementInfo]:
        """Find a UI element by text or resource_id."""

    @abc.abstractmethod
    def get_text(self, text: str = "") -> Optional[ElementInfo]:
        """Get element info by text (alias for find_element with text focus)."""

    @abc.abstractmethod
    def wait_element(self, text: str, timeout_s: int = 10) -> bool:
        """Wait for element to appear. Returns True if found."""

    @abc.abstractmethod
    def scroll_find(self, text: str, direction: str = "up", max_scrolls: int = 10,
                    scroll_pause: float = 1.0) -> Optional[ElementInfo]:
        """Scroll until text found. Returns element or None."""

    @abc.abstractmethod
    def element_state(self, text: str = "", resource_id: str = "") -> Optional[dict]:
        """Get element state (enabled/checked/selected/scrollable/focusable)."""

    # ── System ────────────────────────────────────────────────────────────

    @abc.abstractmethod
    def notifications(self, action: str) -> None:
        """Expand or collapse notification shade. action='expand'|'collapse'."""

    @abc.abstractmethod
    def shell(self, command: str, timeout_s: int = 30) -> dict:
        """Run a shell command. Returns {stdout, returncode}."""

    # ── File transfer ─────────────────────────────────────────────────────

    @abc.abstractmethod
    def push_file(self, local_path: str, device_path: str) -> bool:
        """Push a file to device."""

    @abc.abstractmethod
    def pull_file(self, device_path: str, local_path: str) -> bool:
        """Pull a file from device."""

    # ── Performance ───────────────────────────────────────────────────────

    @abc.abstractmethod
    def measure_startup(self, package: str) -> dict:
        """Measure cold-start time. Returns timing dict."""

    @abc.abstractmethod
    def dump_meminfo(self, package: str) -> dict:
        """Get memory info (PSS/RSS)."""

    @abc.abstractmethod
    def dump_gfxinfo(self, package: str) -> dict:
        """Get frame rendering info."""

    # ── Recording ─────────────────────────────────────────────────────────

    @abc.abstractmethod
    def recording_start(self, filename: str = "recording.mp4") -> str:
        """Start screen recording. Returns session id or path."""

    @abc.abstractmethod
    def recording_stop(self) -> Optional[str]:
        """Stop recording. Returns local file path or None."""

    # ── Logcat ────────────────────────────────────────────────────────────

    @abc.abstractmethod
    def logcat_start(self, watch_package: str = "") -> str:
        """Start logcat capture. Returns log file path."""

    @abc.abstractmethod
    def logcat_stop(self) -> dict:
        """Stop logcat. Returns analysis dict."""
