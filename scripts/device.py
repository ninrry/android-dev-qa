"""
device.py — ADB 设备管理模块
负责设备发现、连接状态、模拟器生命周期管理
"""
import subprocess
import os
import shlex
import re
import time
from typing import Optional
from dataclasses import dataclass

# Windows 侧 ADB 路径（WSL 环境）
ADB_PATHS = [
    "/mnt/c/Users/d5u5ei/AppData/Local/Android/Sdk/platform-tools/adb.exe",
    "/usr/bin/adb",
    os.path.expanduser("~/.local/bin/adb"),
]

ANDROID_CLI_PATHS = [
    os.path.expanduser("~/.local/bin/android"),
    "/usr/local/bin/android",
]


@dataclass
class Device:
    serial: str
    state: str  # "device", "offline", "unauthorized"
    model: str = ""
    api_level: str = ""
    is_emulator: bool = False

    @property
    def ready(self) -> bool:
        return self.state == "device"


class DeviceManager:
    """ADB 设备管理器"""

    def __init__(self):
        self._adb = self._find_adb()
        self._android_cli = self._find_android_cli()

    def _find_adb(self) -> str:
        """查找可用的 adb 可执行文件"""
        for path in ADB_PATHS:
            if os.path.exists(path):
                return path
        # 尝试 PATH 中的 adb
        try:
            subprocess.run(["adb", "version"], capture_output=True, timeout=5, stdin=subprocess.DEVNULL)
            return "adb"
        except (FileNotFoundError, subprocess.TimeoutExpired):
            pass
        raise FileNotFoundError(
            "ADB not found. Install Android SDK platform-tools or set ADB_PATH."
        )

    def _find_android_cli(self) -> Optional[str]:
        """查找 android CLI"""
        for path in ANDROID_CLI_PATHS:
            if os.path.exists(path):
                return path
        return None

    def _run(self, args: list[str], timeout: int = 30) -> subprocess.CompletedProcess:
        """执行 adb 命令"""
        cmd = [self._adb] + args
        return subprocess.run(
            cmd, capture_output=True, timeout=timeout, stdin=subprocess.DEVNULL,
            encoding="utf-8", errors="replace",
        )

    def _run_android_cli(self, args: list[str], timeout: int = 30) -> Optional[subprocess.CompletedProcess]:
        """执行 android CLI 命令"""
        if not self._android_cli:
            return None
        cmd = [self._android_cli] + args
        return subprocess.run(
            cmd, capture_output=True, timeout=timeout, stdin=subprocess.DEVNULL,
            encoding="utf-8", errors="replace",
        )

    def list_devices(self) -> list[Device]:
        """列出所有已连接设备"""
        result = self._run(["devices", "-l"])
        devices = []
        for line in result.stdout.strip().split("\n")[1:]:
            if not line.strip():
                continue
            parts = line.split()
            if len(parts) >= 2:
                serial = parts[0]
                state = parts[1]
                model = ""
                for p in parts[2:]:
                    if p.startswith("model:"):
                        model = p.split(":", 1)[1]
                is_emulator = "emulator" in serial or "169.254" in serial
                devices.append(Device(
                    serial=serial, state=state, model=model,
                    is_emulator=is_emulator,
                ))
        return devices

    def get_ready_device(self) -> Optional[Device]:
        """获取第一个就绪的设备"""
        for dev in self.list_devices():
            if dev.ready:
                return dev
        return None

    def ensure_device(self) -> Device:
        """确保有可用设备，否则报错"""
        dev = self.get_ready_device()
        if not dev:
            raise RuntimeError(
                "No ready device found. Start an emulator or connect a device.\n"
                f"  android emulator start --name <avd_name>\n"
                f"  adb devices"
            )
        return dev

    def get_device_info(self, serial: Optional[str] = None) -> dict:
        """获取设备详细信息"""
        args = []
        if serial:
            args = ["-s", serial]

        info = {}
        # API level
        r = self._run(args + ["shell", "getprop", "ro.build.version.sdk"])
        info["api_level"] = r.stdout.strip()

        # Android version
        r = self._run(args + ["shell", "getprop", "ro.build.version.release"])
        info["android_version"] = r.stdout.strip()

        # Screen resolution
        r = self._run(args + ["shell", "wm", "size"])
        info["screen_size"] = r.stdout.strip().split(":")[-1].strip() if ":" in r.stdout else ""

        # Screen density
        r = self._run(args + ["shell", "wm", "density"])
        info["density"] = r.stdout.strip().split(":")[-1].strip() if ":" in r.stdout else ""

        # Model
        r = self._run(args + ["shell", "getprop", "ro.product.model"])
        info["model"] = r.stdout.strip()

        return info

    def start_emulator(self, avd_name: str, wait_timeout: int = 120) -> Device:
        """启动模拟器并等待就绪"""
        # 使用 android CLI 启动
        result = self._run_android_cli(
            ["emulator", "start", "--name", avd_name],
            timeout=wait_timeout,
        )
        if result and result.returncode != 0:
            raise RuntimeError(f"Failed to start emulator: {result.stderr}")

        # 等待设备就绪
        start = time.time()
        while time.time() - start < wait_timeout:
            dev = self.get_ready_device()
            if dev:
                # 额外等待 boot 完成
                self._run(["-s", dev.serial, "shell", "wait-for-device"])
                self._run(["-s", dev.serial, "shell", "getprop", "sys.boot_completed"], timeout=30)
                return dev
            time.sleep(3)

        raise TimeoutError(f"Emulator did not become ready within {wait_timeout}s")

    def stop_emulator(self, serial: Optional[str] = None) -> None:
        """停止模拟器"""
        dev = Device(serial=serial, state="device") if serial else self.get_ready_device()
        if dev:
            self._run(["-s", dev.serial, "emu", "kill"])

    def list_avds(self) -> list[str]:
        """列出所有可用的 AVD"""
        result = self._run_android_cli(["emulator", "list"])
        if not result:
            # fallback: 用 emulator -list-avds
            result = self._run(["emulator", "-list-avds"])
            return [line.strip() for line in result.stdout.strip().split("\n") if line.strip()]
        avds = []
        for line in result.stdout.strip().split("\n"):
            line = line.strip()
            if line and not line.startswith("Available") and not line.startswith("Name"):
                avds.append(line.split()[0] if line.split() else line)
        return avds

    def install_apk(self, apk_path: str, serial: Optional[str] = None) -> bool:
        """安装 APK 到设备"""
        args = ["install", "-r", apk_path]
        if serial:
            args = ["-s", serial] + args
        result = self._run(args, timeout=120)
        return result.returncode == 0 and "Success" in result.stdout

    def launch_app(self, package: str, activity: str = "", serial: Optional[str] = None) -> bool:
        """启动应用"""
        # 如果没有指定 activity，先自动查询 launcher activity
        if not activity:
            activity = self._find_launcher_activity(package, serial)
        if activity:
            target = f"{package}/{activity}"
            args = ["shell", "am", "start", "-n", target]
        else:
            # fallback: 用 monkey 启动（最可靠）
            args = ["shell", "monkey", "-p", package, "-c", "android.intent.category.LAUNCHER", "1"]
        if serial:
            args = ["-s", serial] + args
        result = self._run(args)
        return result.returncode == 0
    def _find_launcher_activity(self, package: str, serial: Optional[str] = None) -> Optional[str]:
        """自动查询应用的 launcher activity"""
        args = ["shell", "cmd", "package", "resolve-activity", "--brief", "-c", "android.intent.category.LAUNCHER", package]
        if serial:
            args = ["-s", serial] + args
        result = self._run(args, timeout=10)
        if result.returncode == 0 and result.stdout.strip():
            for line in result.stdout.strip().split("\n"):
                line = line.strip()
                if "/" in line and package in line:
                    component = line.split("component=")[-1].strip() if "component=" in line else line
                    if "/" in component:
                        return component.split("/", 1)[1]
        return None

    def tap(self, x: int, y: int, serial: Optional[str] = None) -> None:
        """点击屏幕坐标"""
        args = ["shell", "input", "tap", str(x), str(y)]
        if serial:
            args = ["-s", serial] + args
        self._run(args)

    def swipe(self, x1: int, y1: int, x2: int, y2: int, duration_ms: int = 300, serial: Optional[str] = None) -> None:
        """滑动操作"""
        args = ["shell", "input", "swipe", str(x1), str(y1), str(x2), str(y2), str(duration_ms)]
        if serial:
            args = ["-s", serial] + args
        self._run(args)

    def text(self, content: str, serial: Optional[str] = None) -> None:
        """输入文本"""
        args = ["shell", "input", "text", content]
        if serial:
            args = ["-s", serial] + args
        self._run(args)

    def keyevent(self, keycode: str, serial: Optional[str] = None) -> None:
        """发送按键事件"""
        args = ["shell", "input", "keyevent", keycode]
        if serial:
            args = ["-s", serial] + args
        self._run(args)

    def back(self, serial: Optional[str] = None) -> None:
        """按返回键"""
        self.keyevent("KEYCODE_BACK", serial)

    def home(self, serial: Optional[str] = None) -> None:
        """按 Home 键"""
        self.keyevent("KEYCODE_HOME", serial)

    # ── 剪贴板操作 ──────────────────────────────────────────────

    def clipboard_set(self, text: str, serial: Optional[str] = None) -> None:
        """通过 am broadcast 设置剪贴板文本（兼容 Android 10+）"""
        # Method 1: service call clipboard (Android 10-12)
        escaped = text.replace('"', '\\"')
        args_base = ["-s", serial] if serial else []
        # Try service call first
        self._run(args_base + ["shell", "service", "call", "clipboard", "2", "i32", "1",
                              "s16", escaped])

    def clipboard_get(self, serial: Optional[str] = None) -> str:
        """读取剪贴板内容"""
        args_base = ["-s", serial] if serial else []
        result = self._run(args_base + ["shell", "service", "call", "clipboard", "1", "i32", "1"], timeout=5)
        return result.stdout.strip() if result.returncode == 0 else ""

    def input_text_unicode(self, text: str, serial: Optional[str] = None) -> None:
        """输入文本。ASCII 用 input text，CJK 暂不支持（Android 16 限制）"""
        args_base = ["-s", serial] if serial else []
        # Clear existing text: CTRL+A → DEL
        self._run(args_base + ["shell", "input", "keyevent", "KEYCODE_CTRL_LEFT", "KEYCODE_A"])
        time.sleep(0.1)
        self._run(args_base + ["shell", "input", "keyevent", "KEYCODE_DEL"])
        # Input text (ASCII only on Android 16+)
        if all(ord(c) < 128 for c in text):
            self._run(args_base + ["shell", "input", "text", text], timeout=10)
        else:
            # CJK: ADBKeyboard broadcasts don't work on Android 14+
            # Log warning and attempt best-effort
            logging.warning("CJK input not supported on Android 14+. Use ASCII or manual input.")

    # ── 拖拽 & 双击 ────────────────────────────────────────────

    def drag(self, x1: int, y1: int, x2: int, y2: int, duration_ms: int = 500,
             serial: Optional[str] = None) -> None:
        """拖拽操作"""
        args = ["shell", "input", "draganddrop", str(x1), str(y1), str(x2), str(y2), str(duration_ms)]
        if serial:
            args = ["-s", serial] + args
        self._run(args, timeout=10)

    def double_tap(self, x: int, y: int, serial: Optional[str] = None) -> None:
        """双击"""
        args_base = ["-s", serial] if serial else []
        self._run(args_base + ["shell", "input", "tap", str(x), str(y)])
        time.sleep(0.1)
        self._run(args_base + ["shell", "input", "tap", str(x), str(y)])

    # ── 通知栏 ──────────────────────────────────────────────────

    def notifications_expand(self, serial: Optional[str] = None) -> None:
        """展开通知栏"""
        args = ["shell", "cmd", "statusbar", "expand-notifications"]
        if serial:
            args = ["-s", serial] + args
        self._run(args)

    def notifications_collapse(self, serial: Optional[str] = None) -> None:
        """收起通知栏"""
        args = ["shell", "cmd", "statusbar", "collapse"]
        if serial:
            args = ["-s", serial] + args
        self._run(args)

    # ── Shell 命令 ──────────────────────────────────────────────

    def run_shell(self, command: str, serial: Optional[str] = None, timeout: int = 30) -> dict:
        """执行任意 shell 命令"""
        args = ["shell"] + shlex.split(command)
        if serial:
            args = ["-s", serial] + args
        result = self._run(args, timeout=timeout)
        return {"stdout": result.stdout.strip(), "returncode": result.returncode}

    # ── 性能测量 ────────────────────────────────────────────────

    def measure_startup(self, package: str, serial: Optional[str] = None) -> dict:
        """测量应用冷启动时间（am start -W）"""
        import re as _re
        args_base = ["-s", serial] if serial else []
        # 先查询 launcher activity
        activity = None
        r = self._run(args_base + ["shell", "cmd", "package", "resolve-activity", "--brief",
                                   "-c", "android.intent.category.LAUNCHER", package], timeout=10)
        for line in r.stdout.split("\n"):
            if "/" in line and package in line:
                comp = line.strip()
                if "component=" in comp:
                    comp = comp.split("component=")[-1].strip()
                if "/" in comp:
                    activity = comp.split("/", 1)[1]
                    break
        # force-stop for cold start
        self._run(args_base + ["shell", "am", "force-stop", package])
        time.sleep(1)
        # launch with timing
        if activity:
            target = f"{package}/{activity}"
            result = self._run(args_base + ["shell", "am", "start", "-W", "-n", target], timeout=30)
        else:
            result = self._run(args_base + ["shell", "am", "start", "-W", package], timeout=30)
        # Normalize \r\n → \n
        output = result.stdout.replace("\r\n", "\n").replace("\r", "\n")
        timing = {}
        for line in output.split("\n"):
            line = line.strip()
            if "TotalTime" in line:
                try: timing["total_time_ms"] = int(line.split(":")[-1].strip())
                except: pass
            elif "WaitTime" in line:
                try: timing["wait_time_ms"] = int(line.split(":")[-1].strip())
                except: pass
            elif "ThisTime" in line:
                try: timing["this_time_ms"] = int(line.split(":")[-1].strip())
                except: pass
            elif line.startswith("Status:"):
                timing["status"] = line.split(":")[-1].strip()
        return timing

    def dump_meminfo(self, package: str, serial: Optional[str] = None) -> dict:
        """获取应用内存信息"""
        args_base = ["-s", serial] if serial else []
        result = self._run(args_base + ["shell", "dumpsys", "meminfo", package], timeout=10)
        output = result.stdout.replace("\r\n", "\n").replace("\r", "\n")
        info = {}
        for line in output.split("\n"):
            line = line.strip()
            if "TOTAL PSS:" in line:
                # Format: "TOTAL PSS:    39595            TOTAL RSS:   159744"
                try:
                    parts = line.split()
                    pss_idx = parts.index("PSS:") + 1
                    info["total_pss_kb"] = int(parts[pss_idx])
                except: pass
                try:
                    rss_idx = parts.index("RSS:") + 1
                    info["total_rss_kb"] = int(parts[rss_idx])
                except: pass
            elif line.startswith("TOTAL") and not "TOTAL PSS" in line:
                # Format: "TOTAL    39595    27628     8000 ..."
                try:
                    parts = line.split()
                    if len(parts) >= 2:
                        info["total_pss_kb"] = int(parts[1])
                except: pass
        return info

    def dump_gfxinfo(self, package: str, serial: Optional[str] = None) -> dict:
        """获取应用帧渲染信息"""
        args_base = ["-s", serial] if serial else []
        result = self._run(args_base + ["shell", "dumpsys", "gfxinfo", package], timeout=10)
        output = result.stdout.replace("\r\n", "\n").replace("\r", "\n")
        info = {"total_frames": 0, "janky_frames": 0, "percentile_50": 0, "percentile_90": 0}
        for line in output.split("\n"):
            line = line.strip()
            if "Total frames rendered:" in line:
                try: info["total_frames"] = int(line.split(":")[-1].strip())
                except: pass
            elif line.startswith("Janky frames:") and "legacy" not in line:
                # Format: "Janky frames: 5 (35.71%)"
                try: info["janky_frames"] = int(line.split(":")[-1].strip().split()[0])
                except: pass
            elif "50th percentile:" in line and "gpu" not in line:
                try: info["percentile_50"] = int(line.split(":")[-1].strip().replace("ms", ""))
                except: pass
            elif "90th percentile:" in line and "gpu" not in line:
                try: info["percentile_90"] = int(line.split(":")[-1].strip().replace("ms", ""))
                except: pass
        return info

    # ── 文件传输 ────────────────────────────────────────────────

    def push_file(self, local_path: str, device_path: str, serial: Optional[str] = None) -> bool:
        """推送文件到设备"""
        args = ["push", local_path, device_path]
        if serial:
            args = ["-s", serial] + args
        result = self._run(args, timeout=120)
        return result.returncode == 0

    def pull_file(self, device_path: str, local_path: str, serial: Optional[str] = None) -> bool:
        """从设备拉取文件"""
        args = ["pull", device_path, local_path]
        if serial:
            args = ["-s", serial] + args
        result = self._run(args, timeout=120)
        return result.returncode == 0
