"""
logcat_capture.py — 日志实时捕获模块
负责 logcat 的启动、停止、过滤、异常检测、App 存活监控
"""
import subprocess
import os
import re
import time
import threading
from typing import Optional
from dataclasses import dataclass, field


# 异常模式
ERROR_PATTERNS = [
    re.compile(r"FATAL EXCEPTION", re.IGNORECASE),
    re.compile(r"AndroidRuntime.*E/", re.IGNORECASE),
    re.compile(r"ANR in ", re.IGNORECASE),
    re.compile(r"OutOfMemoryError", re.IGNORECASE),
    re.compile(r"NullPointerException", re.IGNORECASE),
    re.compile(r"ClassNotFoundException", re.IGNORECASE),
    re.compile(r"SecurityException", re.IGNORECASE),
    re.compile(r"IllegalArgumentException", re.IGNORECASE),
    re.compile(r"Caused by:.*Error", re.IGNORECASE),
]

WARNING_PATTERNS = [
    re.compile(r"W/.*deprecated", re.IGNORECASE),
    re.compile(r"Choreographer.*Skipped.*frames", re.IGNORECASE),
    re.compile(r"GC_|Low.*Memory", re.IGNORECASE),
]

# App 进程死亡检测
PROCESS_DIED_PATTERNS = [
    re.compile(r"Process .* has died", re.IGNORECASE),
    re.compile(r"Force finishing activity", re.IGNORECASE),
    re.compile(r"has been killed", re.IGNORECASE),
]


@dataclass
class LogEntry:
    """单条日志"""
    timestamp: str = ""
    pid: str = ""
    tid: str = ""
    level: str = ""  # V/D/I/W/E/F
    tag: str = ""
    message: str = ""
    raw: str = ""


@dataclass
class LogAnalysis:
    """日志分析结果"""
    total_lines: int = 0
    errors: list = field(default_factory=list)
    warnings: list = field(default_factory=list)
    crashes: list = field(default_factory=list)
    anrs: list = field(default_factory=list)
    ooms: list = field(default_factory=list)


class LogcatCapture:
    """Logcat 实时捕获和分析器"""

    def __init__(self, adb_path: str, output_dir: str = "output/logs"):
        self._adb = adb_path
        self._output_dir = output_dir
        self._process: Optional[subprocess.Popen] = None
        self._log_file: Optional[str] = None
        self._lines: list[str] = []
        self._lock = threading.Lock()
        self._is_capturing = False
        self._app_package: Optional[str] = None
        self._app_died = False
        self._crash_info: Optional[LogEntry] = None
        self._last_error_line = 0
        os.makedirs(output_dir, exist_ok=True)

    def _adb_cmd(self, args: list[str]) -> list[str]:
        return [self._adb] + args

    def start(self, serial: Optional[str] = None,
              filters: Optional[list[str]] = None,
              filename: str = "logcat.txt",
              watch_package: Optional[str] = None) -> str:
        """
        开始捕获 logcat。
        watch_package: 要监控的应用包名（用于实时检测 app 退出）
        返回日志文件路径。
        """
        self._log_file = os.path.join(self._output_dir, filename)
        self._app_package = watch_package
        self._app_died = False
        self._crash_info = None
        self._last_error_line = 0

        # 清空旧日志
        args = ["logcat", "-c"]
        if serial:
            args = ["-s", serial] + args
        subprocess.run(self._adb_cmd(args), capture_output=True, timeout=10, stdin=subprocess.DEVNULL)

        # 启动 logcat（W/E/F 级别，减少噪音）
        args = ["logcat", "-v", "threadtime", "*:W"]
        if serial:
            args = ["-s", serial] + args
        if filters:
            args.extend(filters)

        self._process = subprocess.Popen(
            self._adb_cmd(args),
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            stdin=subprocess.DEVNULL,
            text=True,
        )

        self._is_capturing = True
        self._lines = []

        self._reader_thread = threading.Thread(
            target=self._read_loop, daemon=True
        )
        self._reader_thread.start()

        return self._log_file

    def _read_loop(self):
        """后台读取 logcat 输出，实时检测异常"""
        if not self._process or not self._process.stdout:
            return
        try:
            for line in self._process.stdout:
                if not self._is_capturing:
                    break
                line = line.rstrip("\n")
                with self._lock:
                    self._lines.append(line)
                if self._log_file:
                    try:
                        with open(self._log_file, "a") as f:
                            f.write(line + "\n")
                    except OSError:
                        pass
                # 实时检测 app 崩溃
                self._check_app_health(line)
        except (ValueError, OSError):
            pass

    def _check_app_health(self, line: str):
        """实时检测 app 是否崩溃/退出（非阻塞，由后台线程调用）"""
        if not self._app_package:
            return

        # 检测 crash
        if "FATAL EXCEPTION" in line or "AndroidRuntime" in line:
            entry = self.parse_line(line)
            if entry:
                with self._lock:
                    self._app_died = True
                    self._crash_info = entry

        # 检测进程死亡
        if self._app_package in line:
            for pattern in PROCESS_DIED_PATTERNS:
                if pattern.search(line):
                    entry = self.parse_line(line)
                    if entry:
                        with self._lock:
                            self._app_died = True
                            if not self._crash_info:
                                self._crash_info = entry
                    break

        # 检测 ANR
        if "ANR in" in line and self._app_package in line:
            entry = self.parse_line(line)
            if entry:
                with self._lock:
                    self._app_died = True
                    self._crash_info = entry

    def is_running(self) -> bool:
        return self._is_capturing

    def is_app_alive(self) -> bool:
        """检查 app 是否仍然存活（非阻塞，立即返回）"""
        with self._lock:
            return not self._app_died

    def get_crash_info(self) -> Optional[LogEntry]:
        """获取崩溃信息"""
        with self._lock:
            return self._crash_info

    def get_new_errors(self) -> list[LogEntry]:
        """获取自上次调用以来的新错误日志"""
        with self._lock:
            lines = list(self._lines)
            new_lines = lines[self._last_error_line:]
            self._last_error_line = len(lines)

        results = []
        for line in new_lines:
            entry = self.parse_line(line)
            if entry and entry.level in ("E", "F"):
                results.append(entry)
        return results

    def stop(self) -> str:
        """停止捕获，返回日志文件路径"""
        self._is_capturing = False
        if self._process:
            self._process.terminate()
            try:
                self._process.wait(timeout=5)
            except subprocess.TimeoutExpired:
                self._process.kill()
            self._process = None

        if hasattr(self, "_reader_thread") and self._reader_thread.is_alive():
            self._reader_thread.join(timeout=3)

        return self._log_file or ""

    def get_lines(self) -> list[str]:
        """获取所有已捕获的日志行"""
        with self._lock:
            return list(self._lines)

    def parse_line(self, line: str) -> Optional[LogEntry]:
        """解析一行 threadtime 格式的日志"""
        match = re.match(
            r'(\d{2}-\d{2}\s+\d{2}:\d{2}:\d{2}\.\d{3})\s+(\d+)\s+(\d+)\s+([VDIWEF])\s+(.+?):\s(.*)',
            line
        )
        if match:
            return LogEntry(
                timestamp=match.group(1),
                pid=match.group(2),
                tid=match.group(3),
                level=match.group(4),
                tag=match.group(5),
                message=match.group(6),
                raw=line,
            )
        return None

    def analyze(self) -> LogAnalysis:
        """分析所有已捕获日志，检测异常"""
        analysis = LogAnalysis()
        lines = self.get_lines()

        for line in lines:
            entry = self.parse_line(line)
            if not entry:
                continue

            analysis.total_lines += 1

            if "FATAL EXCEPTION" in line:
                analysis.crashes.append(entry)
            elif "ANR in" in line:
                analysis.anrs.append(entry)
            elif "OutOfMemoryError" in line:
                analysis.ooms.append(entry)
            else:
                for pattern in ERROR_PATTERNS:
                    if pattern.search(line):
                        analysis.errors.append(entry)
                        break

            for pattern in WARNING_PATTERNS:
                if pattern.search(line):
                    analysis.warnings.append(entry)
                    break

        return analysis
