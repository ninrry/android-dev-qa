"""
device.py — Universal ADB device management
Cross-platform ADB discovery, emulator detection, and device lifecycle management.
"""
from __future__ import annotations

import logging
import os
import platform
import re
import shlex
import shutil
import subprocess
import time
from typing import Optional

logger = logging.getLogger("android-qa.device")

# ── ADB Discovery ─────────────────────────────────────────────────────────────

_SYSTEM = platform.system()  # "Linux", "Darwin", "Windows"
_IS_WSL = "microsoft" in platform.uname().release.lower() if _SYSTEM == "Linux" else False


def _adb_search_paths() -> list[str]:
    """Build platform-aware ADB candidate paths."""
    paths: list[str] = []

    # 1. Environment variables (highest priority)
    if v := os.environ.get("ADB_PATH", ""):
        paths.append(v)
    if v := os.environ.get("ANDROID_HOME", ""):
        paths.append(os.path.join(v, "platform-tools", "adb" + (".exe" if _SYSTEM == "Windows" or _IS_WSL else "")))
    if v := os.environ.get("ANDROID_SDK_ROOT", ""):
        paths.append(os.path.join(v, "platform-tools", "adb" + (".exe" if _SYSTEM == "Windows" or _IS_WSL else "")))

    # 2. WSL → Windows SDK
    if _IS_WSL:
        win_user = os.environ.get("WINDOWS_USER", "")
        if not win_user:
            # Try to discover Windows username from /mnt/c/Users/
            try:
                for entry in os.listdir("/mnt/c/Users/"):
                    if entry not in ("Public", "Default", "Default User", "All Users",
                                     "desktop.ini", "AppData") \
                            and os.path.isdir(f"/mnt/c/Users/{entry}") \
                            and os.path.exists(f"/mnt/c/Users/{entry}/AppData"):
                        win_user = entry
                        break
            except OSError:
                pass
        if win_user:
            paths.append(f"/mnt/c/Users/{win_user}/AppData/Local/Android/Sdk/platform-tools/adb.exe")

    # 3. macOS
    if _SYSTEM == "Darwin":
        paths.append(os.path.expanduser("~/Library/Android/sdk/platform-tools/adb"))

    # 4. Linux standard paths
    paths.extend([
        "/usr/bin/adb",
        "/usr/local/bin/adb",
        os.path.expanduser("~/.local/bin/adb"),
    ])

    # 5. Windows native
    if _SYSTEM == "Windows":
        local_app_data = os.environ.get("LOCALAPPDATA", "")
        if local_app_data:
            paths.append(os.path.join(local_app_data, "Android", "Sdk", "platform-tools", "adb.exe"))

    # 6. shutil.which fallback
    if found := shutil.which("adb"):
        if found not in paths:
            paths.append(found)

    return [p for p in paths if p]  # filter empty


def _find_adb() -> str:
    """Find the first usable ADB binary."""
    for path in _adb_search_paths():
        if os.path.isfile(path) and os.access(path, os.X_OK if _SYSTEM != "Windows" else os.R_OK):
            logger.debug("ADB found at: %s", path)
            return path
    # Last resort: bare "adb" in PATH
    try:
        subprocess.run(["adb", "version"], capture_output=True, timeout=5, stdin=subprocess.DEVNULL)
        return "adb"
    except (FileNotFoundError, subprocess.TimeoutExpired):
        pass
    raise FileNotFoundError(
        "ADB not found. Install Android SDK platform-tools and/or set ADB_PATH / ANDROID_HOME."
    )


def _find_android_cli() -> Optional[str]:
    """Find the android CLI (for emulator management)."""
    candidates = [
        os.environ.get("ANDROID_CLI_PATH", ""),
        os.path.expanduser("~/.local/bin/android"),
        "/usr/local/bin/android",
    ]
    if _IS_WSL:
        win_user = os.environ.get("WINDOWS_USER", "")
        if not win_user:
            try:
                for entry in os.listdir("/mnt/c/Users/"):
                    if entry not in ("Public", "Default", "Default User", "All Users",
                                     "desktop.ini", "AppData") \
                            and os.path.isdir(f"/mnt/c/Users/{entry}") \
                            and os.path.exists(f"/mnt/c/Users/{entry}/AppData"):
                        win_user = entry
                        break
            except OSError:
                pass
        if win_user:
            candidates.append(f"/mnt/c/Users/{win_user}/AppData/Local/Android/Sdk/cmdline-tools/latest/bin/android")
    for p in candidates:
        if p and os.path.isfile(p):
            return p
    if found := shutil.which("android"):
        return found
    return None


# ── Data Models ───────────────────────────────────────────────────────────────

from dataclasses import dataclass, field


@dataclass
class Device:
    serial: str
    state: str  # "device", "offline", "unauthorized"
    model: str = ""
    api_level: str = ""
    is_emulator: bool = False
    emulator_type: str = ""  # "avd", "genymotion", "bluestacks", "ldplayer", "unknown", ""

    @property
    def ready(self) -> bool:
        return self.state == "device"


# ── Emulator Detection ────────────────────────────────────────────────────────

_EMULATOR_PATTERNS: dict[str, re.Pattern] = {
    "avd": re.compile(r"^emulator-\d+$"),
    "genymotion": re.compile(r"^169\.254\.\d+\.\d+$|^vbox\d+$"),
    "bluestacks": re.compile(r"bluestacks", re.IGNORECASE),
    "ldplayer": re.compile(r"ldplayer", re.IGNORECASE),
    "mumu": re.compile(r"mumu|MuMuPlayer", re.IGNORECASE),
    "nox": re.compile(r"nox|NoxPlayer", re.IGNORECASE),
}

# TCP-connected emulators default ADB ports
_EMULATOR_PORTS: dict[str, list[int]] = {
    "ldplayer": [5555, 5556, 5557, 5558, 62001, 62025, 62026],
    "bluestacks": [5555, 5565, 5575, 5585],
    "mumu": [7555],
    "nox": [62001, 62025, 62026],
}


def _detect_emulator_type(serial: str) -> tuple[bool, str]:
    """Detect emulator type from serial number.

    Strategy:
    1. Match known name patterns in serial (e.g. 'ldplayer', 'bluestacks')
    2. Match standard format (emulator-N, 169.254.x.x)
    3. For 127.0.0.1:PORT, infer type from well-known ports
    4. Fallback: any TCP address = unknown emulator
    """
    # 1. Name-based detection (most reliable)
    for etype, pattern in _EMULATOR_PATTERNS.items():
        if pattern.search(serial):
            return True, etype

    # 2. Standard AVD format
    if re.match(r"^emulator-\d+$", serial):
        return True, "avd"

    # 3. TCP address — try to infer from port
    tcp_match = re.match(r"^127\.0\.0\.1:(\d+)$", serial)
    if tcp_match:
        port = int(tcp_match.group(1))
        for etype, ports in _EMULATOR_PORTS.items():
            if port in ports:
                return True, etype
        return True, "unknown"  # TCP-connected, likely emulator

    # 4. 169.254.x.x = Genymotion
    if re.match(r"^169\.254\.\d+\.\d+$", serial):
        return True, "genymotion"

    return False, ""


# ── DeviceManager ─────────────────────────────────────────────────────────────

class DeviceManager:
    """ADB device manager — cross-platform, auto-discovery."""

    def __init__(self, adb_path: Optional[str] = None, android_cli: Optional[str] = None):
        self._adb = adb_path or _find_adb()
        self._android_cli = android_cli or _find_android_cli()
        logger.info("ADB: %s | Android CLI: %s", self._adb, self._android_cli or "(not found)")

    # ── Low-level ─────────────────────────────────────────────────────────

    def _run(self, args: list[str], timeout: int = 30, serial: Optional[str] = None) -> subprocess.CompletedProcess:
        cmd = [self._adb]
        if serial:
            cmd += ["-s", serial]
        cmd += args
        return subprocess.run(
            cmd, capture_output=True, timeout=timeout, stdin=subprocess.DEVNULL,
            encoding="utf-8", errors="replace",
        )

    def _run_android_cli(self, args: list[str], timeout: int = 30) -> Optional[subprocess.CompletedProcess]:
        if not self._android_cli:
            return None
        return subprocess.run(
            [self._android_cli] + args, capture_output=True, timeout=timeout,
            stdin=subprocess.DEVNULL, encoding="utf-8", errors="replace",
        )

    # ── Device Discovery ──────────────────────────────────────────────────

    def list_devices(self) -> list[Device]:
        result = self._run(["devices", "-l"])
        devices = []
        for line in result.stdout.strip().split("\n")[1:]:
            if not line.strip():
                continue
            parts = line.split()
            if len(parts) < 2:
                continue
            serial = parts[0]
            state = parts[1]
            model = ""
            for p in parts[2:]:
                if p.startswith("model:"):
                    model = p.split(":", 1)[1]
            is_emu, emu_type = _detect_emulator_type(serial)
            devices.append(Device(
                serial=serial, state=state, model=model,
                is_emulator=is_emu, emulator_type=emu_type,
            ))
        return devices

    def auto_connect_emulators(self) -> list[Device]:
        """Try adb connect for known emulator ports.

        Third-party emulators (LDPlayer, BlueStacks, MuMu, Nox) expose ADB
        on 127.0.0.1:PORT but don't always show up in `adb devices` until
        explicitly connected. This method attempts common ports.

        Returns list of newly connected devices.
        """
        existing = {d.serial for d in self.list_devices()}
        newly_connected: list[Device] = []

        # Collect all candidate ports
        candidate_ports: set[int] = set()
        for ports in _EMULATOR_PORTS.values():
            candidate_ports.update(ports)

        for port in sorted(candidate_ports):
            addr = f"127.0.0.1:{port}"
            if addr in existing:
                continue
            r = self._run(["connect", addr], timeout=5)
            if r.returncode == 0 and "connected" in r.stdout.lower():
                logger.info("Auto-connected emulator at %s", addr)
                time.sleep(0.3)
                # Verify it appears in device list
                for dev in self.list_devices():
                    if dev.serial == addr and dev.ready:
                        newly_connected.append(dev)
                        break

        return newly_connected

    def get_ready_device(self) -> Optional[Device]:
        for dev in self.list_devices():
            if dev.ready:
                return dev
        return None

    def ensure_device(self) -> Device:
        dev = self.get_ready_device()
        if not dev:
            raise RuntimeError(
                "No ready device found. Start an emulator or connect a device.\n"
                "  android emulator start --name <avd_name>\n"
                "  adb devices"
            )
        return dev

    # ── Device Info ───────────────────────────────────────────────────────

    def get_device_info(self, serial: Optional[str] = None) -> dict:
        info: dict = {}
        for prop, key in [
            ("ro.build.version.sdk", "api_level"),
            ("ro.build.version.release", "android_version"),
            ("ro.product.model", "model"),
        ]:
            r = self._run(["shell", "getprop", prop], serial=serial)
            info[key] = r.stdout.strip()

        r = self._run(["shell", "wm", "size"], serial=serial)
        info["screen_size"] = r.stdout.strip().split(":")[-1].strip() if ":" in r.stdout else ""

        r = self._run(["shell", "wm", "density"], serial=serial)
        info["density"] = r.stdout.strip().split(":")[-1].strip() if ":" in r.stdout else ""

        return info

    def get_screen_size(self, serial: Optional[str] = None) -> tuple[int, int]:
        """Get screen width x height as ints."""
        info = self.get_device_info(serial)
        raw = info.get("screen_size", "")
        try:
            w, h = raw.split("x")
            return int(w), int(h)
        except (ValueError, AttributeError):
            return 1080, 1920  # safe default

    # ── Emulator Management ───────────────────────────────────────────────

    def start_emulator(self, avd_name: str, wait_timeout: int = 120) -> Device:
        result = self._run_android_cli(["emulator", "start", "--name", avd_name], timeout=wait_timeout)
        if result and result.returncode != 0:
            raise RuntimeError(f"Failed to start emulator: {result.stderr}")
        start = time.time()
        while time.time() - start < wait_timeout:
            dev = self.get_ready_device()
            if dev:
                self._run(["shell", "wait-for-device"], serial=dev.serial)
                self._run(["shell", "getprop", "sys.boot_completed"], serial=dev.serial, timeout=30)
                return dev
            time.sleep(3)
        raise TimeoutError(f"Emulator did not become ready within {wait_timeout}s")

    def stop_emulator(self, serial: Optional[str] = None) -> None:
        dev = Device(serial=serial, state="device") if serial else self.get_ready_device()
        if dev:
            self._run(["emu", "kill"], serial=dev.serial)

    def list_avds(self) -> list[str]:
        result = self._run_android_cli(["emulator", "list"])
        if not result:
            result = self._run(["emulator", "-list-avds"])
            return [line.strip() for line in result.stdout.strip().split("\n") if line.strip()]
        avds = []
        for line in result.stdout.strip().split("\n"):
            line = line.strip()
            if line and not line.startswith(("Available", "Name")):
                avds.append(line.split()[0] if line.split() else line)
        return avds

    # ── App Lifecycle ─────────────────────────────────────────────────────

    def install_apk(self, apk_path: str, serial: Optional[str] = None) -> bool:
        result = self._run(["install", "-r", apk_path], timeout=120, serial=serial)
        return result.returncode == 0 and "Success" in result.stdout

    def launch_app(self, package: str, activity: str = "", serial: Optional[str] = None) -> bool:
        if not activity:
            activity = self._find_launcher_activity(package, serial)
        if activity:
            args = ["shell", "am", "start", "-n", f"{package}/{activity}"]
        else:
            args = ["shell", "monkey", "-p", package, "-c", "android.intent.category.LAUNCHER", "1"]
        result = self._run(args, serial=serial)
        return result.returncode == 0

    def _find_launcher_activity(self, package: str, serial: Optional[str] = None) -> Optional[str]:
        r = self._run(
            ["shell", "cmd", "package", "resolve-activity", "--brief",
             "-c", "android.intent.category.LAUNCHER", package],
            timeout=10, serial=serial,
        )
        if r.returncode == 0 and r.stdout.strip():
            for line in r.stdout.strip().split("\n"):
                line = line.strip()
                if "/" in line and package in line:
                    component = line.split("component=")[-1].strip() if "component=" in line else line
                    if "/" in component:
                        return component.split("/", 1)[1]
        return None

    # ── Input Operations ──────────────────────────────────────────────────

    def tap(self, x: int, y: int, serial: Optional[str] = None) -> None:
        self._run(["shell", "input", "tap", str(x), str(y)], serial=serial)

    def swipe(self, x1: int, y1: int, x2: int, y2: int, duration_ms: int = 300,
              serial: Optional[str] = None) -> None:
        self._run(["shell", "input", "swipe", str(x1), str(y1), str(x2), str(y2), str(duration_ms)],
                  serial=serial)

    def text_input(self, content: str, serial: Optional[str] = None) -> None:
        """Input ASCII text via `adb shell input text`."""
        self._run(["shell", "input", "text", content], serial=serial)

    def keyevent(self, keycode: str, serial: Optional[str] = None) -> None:
        self._run(["shell", "input", "keyevent", keycode], serial=serial)

    def back(self, serial: Optional[str] = None) -> None:
        self.keyevent("KEYCODE_BACK", serial)

    def home(self, serial: Optional[str] = None) -> None:
        self.keyevent("KEYCODE_HOME", serial)

    # ── Clipboard ─────────────────────────────────────────────────────────

    def clipboard_set(self, text: str, serial: Optional[str] = None) -> dict:
        """Set clipboard text. Returns {ok, method, error?}.

        Tries multiple methods in order:
        1. am broadcast (Android 10+, most reliable)
        2. service call clipboard (Android 10-13)
        3. ADBKeyboard broadcast (if installed)
        """
        # Method 1: am broadcast — works on most Android versions
        escaped = text.replace("\\", "\\\\").replace('"', '\\"').replace("$", "\\$").replace("'", "\\'")
        r = self._run(
            ["shell", "am", "broadcast", "-a", "com.android.intent.action.SET_CLIPBOARD",
             "--es", "text", escaped],
            timeout=5, serial=serial,
        )
        if r.returncode == 0 and "result=0" in r.stdout:
            return {"ok": True, "method": "am_broadcast"}

        # Method 2: service call clipboard
        r2 = self._run(
            ["shell", "service", "call", "clipboard", "2", "i32", "1", "s16", escaped],
            timeout=5, serial=serial,
        )
        if r2.returncode == 0:
            return {"ok": True, "method": "service_call"}

        # Method 3: ADBKeyboard
        r3 = self._run(
            ["shell", "am", "broadcast", "-a", "com.android.adbkeyboard.SET_CLIPBOARD",
             "--es", "text", escaped],
            timeout=5, serial=serial,
        )
        if r3.returncode == 0 and "result=0" in r3.stdout:
            return {"ok": True, "method": "adbkeyboard"}

        return {"ok": False, "error": "All clipboard methods failed (am broadcast + service call + ADBKeyboard)"}

    def clipboard_get(self, serial: Optional[str] = None) -> str:
        """Read clipboard content (best-effort)."""
        r = self._run(["shell", "service", "call", "clipboard", "1", "i32", "1"], timeout=5, serial=serial)
        if r.returncode == 0:
            return r.stdout.strip()
        return ""

    # ── Unicode Input ─────────────────────────────────────────────────────

    def input_text_unicode(self, text: str, serial: Optional[str] = None) -> dict:
        """Input text with Unicode support. Returns {ok, method, error?}.

        Strategy:
        1. ASCII-only → `adb shell input text`
        2. Unicode → set clipboard + paste (CTRL+V)
        """
        args_base_serial = serial
        # Clear existing text: CTRL+A → DEL
        self._run(["shell", "input", "keyevent", "KEYCODE_CTRL_LEFT", "KEYCODE_A"], serial=serial)
        time.sleep(0.05)
        self._run(["shell", "input", "keyevent", "KEYCODE_DEL"], serial=serial)
        time.sleep(0.05)

        if all(ord(c) < 128 for c in text):
            self._run(["shell", "input", "text", text], timeout=10, serial=serial)
            return {"ok": True, "method": "input_text"}

        # Unicode: clipboard + paste
        clip_result = self.clipboard_set(text, serial=serial)
        if clip_result.get("ok"):
            time.sleep(0.1)
            self._run(["shell", "input", "keyevent", "KEYCODE_CTRL_LEFT", "KEYCODE_V"], serial=serial)
            time.sleep(0.1)
            return {"ok": True, "method": f"clipboard_paste({clip_result['method']})"}

        return {"ok": False, "error": "Unicode input failed: clipboard set failed", "detail": clip_result}

    # ── Drag & Double-tap ─────────────────────────────────────────────────

    def drag(self, x1: int, y1: int, x2: int, y2: int, duration_ms: int = 500,
             serial: Optional[str] = None) -> None:
        # Android 8+ supports draganddrop, fallback to swipe
        r = self._run(["shell", "input", "draganddrop", str(x1), str(y1), str(x2), str(y2), str(duration_ms)],
                      timeout=10, serial=serial)
        if r.returncode != 0:
            # Fallback: swipe (works on all versions)
            self.swipe(x1, y1, x2, y2, duration_ms, serial=serial)

    def double_tap(self, x: int, y: int, serial: Optional[str] = None) -> None:
        self._run(["shell", "input", "tap", str(x), str(y)], serial=serial)
        time.sleep(0.1)
        self._run(["shell", "input", "tap", str(x), str(y)], serial=serial)

    # ── Notifications ─────────────────────────────────────────────────────

    def notifications_expand(self, serial: Optional[str] = None) -> None:
        self._run(["shell", "cmd", "statusbar", "expand-notifications"], serial=serial)

    def notifications_collapse(self, serial: Optional[str] = None) -> None:
        self._run(["shell", "cmd", "statusbar", "collapse"], serial=serial)

    # ── Shell ─────────────────────────────────────────────────────────────

    def run_shell(self, command: str, serial: Optional[str] = None, timeout: int = 30) -> dict:
        args = ["shell"] + shlex.split(command)
        result = self._run(args, timeout=timeout, serial=serial)
        return {"stdout": result.stdout.strip(), "returncode": result.returncode}

    # ── Performance ───────────────────────────────────────────────────────

    def measure_startup(self, package: str, serial: Optional[str] = None) -> dict:
        activity = self._find_launcher_activity(package, serial)
        self._run(["shell", "am", "force-stop", package], serial=serial)
        time.sleep(1)
        if activity:
            result = self._run(["shell", "am", "start", "-W", "-n", f"{package}/{activity}"], timeout=30, serial=serial)
        else:
            result = self._run(["shell", "am", "start", "-W", package], timeout=30, serial=serial)
        output = result.stdout.replace("\r\n", "\n").replace("\r", "\n")
        timing: dict = {}
        for line in output.split("\n"):
            line = line.strip()
            for key, label in [("total_time_ms", "TotalTime"), ("wait_time_ms", "WaitTime"), ("this_time_ms", "ThisTime")]:
                if label in line:
                    try:
                        timing[key] = int(line.split(":")[-1].strip())
                    except ValueError:
                        pass
            if line.startswith("Status:"):
                timing["status"] = line.split(":")[-1].strip()
        return timing

    def dump_meminfo(self, package: str, serial: Optional[str] = None) -> dict:
        result = self._run(["shell", "dumpsys", "meminfo", package], timeout=10, serial=serial)
        output = result.stdout.replace("\r\n", "\n").replace("\r", "\n")
        info: dict = {}
        for line in output.split("\n"):
            line = line.strip()
            if "TOTAL PSS:" in line:
                parts = line.split()
                try:
                    info["total_pss_kb"] = int(parts[parts.index("PSS:") + 1])
                except (ValueError, IndexError):
                    pass
                try:
                    info["total_rss_kb"] = int(parts[parts.index("RSS:") + 1])
                except (ValueError, IndexError):
                    pass
            elif line.startswith("TOTAL") and "TOTAL PSS" not in line:
                parts = line.split()
                if len(parts) >= 2:
                    try:
                        info["total_pss_kb"] = int(parts[1])
                    except ValueError:
                        pass
        return info

    def dump_gfxinfo(self, package: str, serial: Optional[str] = None) -> dict:
        result = self._run(["shell", "dumpsys", "gfxinfo", package], timeout=10, serial=serial)
        output = result.stdout.replace("\r\n", "\n").replace("\r", "\n")
        info: dict = {"total_frames": 0, "janky_frames": 0, "percentile_50": 0, "percentile_90": 0}
        for line in output.split("\n"):
            line = line.strip()
            if "Total frames rendered:" in line:
                try:
                    info["total_frames"] = int(line.split(":")[-1].strip())
                except ValueError:
                    pass
            elif line.startswith("Janky frames:") and "legacy" not in line:
                try:
                    info["janky_frames"] = int(line.split(":")[-1].strip().split()[0])
                except ValueError:
                    pass
            elif "50th percentile:" in line and "gpu" not in line:
                try:
                    info["percentile_50"] = int(line.split(":")[-1].strip().replace("ms", ""))
                except ValueError:
                    pass
            elif "90th percentile:" in line and "gpu" not in line:
                try:
                    info["percentile_90"] = int(line.split(":")[-1].strip().replace("ms", ""))
                except ValueError:
                    pass
        return info

    # ── File Transfer ─────────────────────────────────────────────────────

    def push_file(self, local_path: str, device_path: str, serial: Optional[str] = None) -> bool:
        result = self._run(["push", local_path, device_path], timeout=120, serial=serial)
        return result.returncode == 0

    def pull_file(self, device_path: str, local_path: str, serial: Optional[str] = None) -> bool:
        result = self._run(["pull", device_path, local_path], timeout=120, serial=serial)
        return result.returncode == 0
