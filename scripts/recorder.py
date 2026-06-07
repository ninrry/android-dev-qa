"""
recorder.py — 录屏管理模块
负责 ADB screenrecord 的启动、停止、分段、拉取
"""
import subprocess
import os
import time
import signal
from typing import Optional
from dataclasses import dataclass, field


@dataclass
class RecordingSession:
    """一次录屏会话"""
    device_serial: str
    remote_path: str = "/sdcard/qa_recording.mp4"
    local_path: str = ""
    is_recording: bool = False
    start_time: float = 0.0
    process: Optional[subprocess.Popen] = field(default=None, repr=False)
    segments: list[str] = field(default_factory=list)


class Recorder:
    """ADB 录屏管理器"""
    REMOTE_TMP = "/sdcard/qa_recordings"

    def __init__(self, adb_path: str, output_dir: str = "output/video"):
        self._adb = adb_path
        self._output_dir = output_dir
        self._sessions: dict[str, RecordingSession] = {}
        os.makedirs(output_dir, exist_ok=True)

    def _adb_cmd(self, serial: str, args: list[str]) -> list[str]:
        return [self._adb, "-s", serial] + args

    def _run(self, serial: str, args: list[str], timeout: int = 10) -> subprocess.CompletedProcess:
        cmd = self._adb_cmd(serial, args)
        return subprocess.run(cmd, capture_output=True, text=True, timeout=timeout, stdin=subprocess.DEVNULL)

    def start(self, serial: str, filename: str = "recording.mp4",
              time_limit: int = 180) -> RecordingSession:
        """
        开始录屏。
        time_limit: 最大录制秒数（ADB 硬限制 180s）
        """
        remote_path = f"{self.REMOTE_TMP}/{filename}"
        local_path = os.path.join(self._output_dir, filename)

        # 确保远程目录存在
        self._run(serial, ["shell", "mkdir", "-p", self.REMOTE_TMP])

        # 获取设备屏幕尺寸，按比例缩放到 720 宽度
        info_result = subprocess.run(
            [self._adb, "-s", serial, "shell", "wm", "size"],
            capture_output=True, timeout=5, stdin=subprocess.DEVNULL,
            encoding="utf-8", errors="replace",
        )
        w, h = 720, 1280  # fallback
        try:
            dims = info_result.stdout.strip().split(":")[-1].strip().split("x")
            orig_w, orig_h = int(dims[0]), int(dims[1])
            scale = 720 / orig_w
            w, h = 720, int(orig_h * scale)
        except (ValueError, IndexError, AttributeError):
            pass

        device_path = f"/sdcard/qa_recordings/{filename}"
        cmd = [self._adb, "-s", serial, "shell", "screenrecord",
               "--time-limit", str(time_limit),
               "--size", f"{w}x{h}", device_path]
        process = subprocess.Popen(
            cmd, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, stdin=subprocess.DEVNULL
        )

        session = RecordingSession(
            device_serial=serial,
            remote_path=remote_path,
            local_path=local_path,
            is_recording=True,
            start_time=time.time(),
            process=process,
        )
        self._sessions[serial] = session
        return session

    def stop(self, serial: str) -> Optional[str]:
        """
        停止录屏并拉取文件。
        返回本地文件路径，或 None（无录制）
        """
        session = self._sessions.get(serial)
        if not session or not session.is_recording:
            return None

        # 步骤 1: 发送 SIGINT 让 screenrecord 正确写入 moov atom
        # 使用列表形式避免 shell=True 的阻塞风险
        try:
            # 先获取 screenrecord PID
            pid_result = subprocess.run(
                self._adb_cmd(serial, ["shell", "pidof", "screenrecord"]),
                capture_output=True, text=True, timeout=5, stdin=subprocess.DEVNULL,
            )
            pid = pid_result.stdout.strip()
            if pid:
                # 发送 SIGINT (-2)
                subprocess.run(
                    self._adb_cmd(serial, ["shell", "kill", "-2", pid]),
                    capture_output=True, text=True, timeout=5, stdin=subprocess.DEVNULL,
                )
        except Exception:
            pass

        # 步骤 2: 等待 screenrecord 写入 moov atom
        try:
            if session.process:
                session.process.wait(timeout=5)
        except Exception:
            pass

        # 步骤 3: 终止本地 adb 进程
        if session.process:
            try:
                # 先尝试 SIGTERM，再 SIGKILL
                session.process.terminate()
                try:
                    session.process.wait(timeout=3)
                except subprocess.TimeoutExpired:
                    session.process.kill()
                    session.process.wait(timeout=2)
            except OSError:
                pass
            session.process = None

        # 步骤 4: 等待文件系统同步
        time.sleep(1)

        # 步骤 5: 拉取文件
        pulled = False
        try:
            result = subprocess.run(
                self._adb_cmd(serial, ["pull", session.remote_path, session.local_path]),
                capture_output=True, text=True, timeout=15, stdin=subprocess.DEVNULL,
            )
            pulled = result.returncode == 0 and os.path.exists(session.local_path)
        except Exception:
            pass

        # 步骤 6: 清理远程文件
        try:
            subprocess.run(
                self._adb_cmd(serial, ["shell", "rm", "-f", session.remote_path]),
                capture_output=True, timeout=5, stdin=subprocess.DEVNULL,
            )
        except Exception:
            pass

        session.is_recording = False
        if pulled:
            session.segments.append(session.local_path)
            return session.local_path
        return None

    def start_segment(self, serial: str, segment_name: str) -> RecordingSession:
        """开始一个新的录屏段（用于分场景录制）"""
        filename = f"{segment_name}_{int(time.time())}.mp4"
        return self.start(serial, filename)

    def stop_segment(self, serial: str) -> Optional[str]:
        """停止当前录屏段"""
        return self.stop(serial)

    def get_duration(self, serial: str) -> float:
        """获取当前录制时长（秒）"""
        session = self._sessions.get(serial)
        if session and session.is_recording:
            return time.time() - session.start_time
        return 0.0

    def list_recordings(self) -> list[str]:
        """列出所有已录制的文件"""
        recordings = []
        if os.path.exists(self._output_dir):
            for f in sorted(os.listdir(self._output_dir)):
                if f.endswith(".mp4"):
                    recordings.append(os.path.join(self._output_dir, f))
        return recordings
