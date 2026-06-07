#!/usr/bin/env python3
"""android-qa-mcp — Android Dev QA MCP Server
通用 Android 测试工具（32 tools），暴露给 Hermes Agent。

工具列表：
  设备管理:  qa_connect, qa_launch, qa_shell, qa_check_app_alive,
             qa_push_file, qa_pull_file
  UI 交互:   qa_tap, qa_long_press, qa_double_tap, qa_swipe, qa_drag
  文本输入:  qa_type, qa_type_unicode, qa_set_clipboard, qa_get_clipboard
  UI 检查:   qa_screenshot, qa_layout_dump, qa_find_element, qa_element_state,
             qa_get_text, qa_wait_element, qa_scroll_find
  系统操作:  qa_press_key, qa_notifications
  性能/诊断: qa_measure_startup, qa_dump_meminfo, qa_dump_gfxinfo
  日志/录屏: qa_logcat_start, qa_logcat_stop, qa_recording_start, qa_recording_stop
  测试编排:  qa_run_test
"""
import asyncio
import threading
import time
import shutil
import json
import os
import sys
import subprocess
import time
from typing import Optional

from mcp.server import Server
from mcp.server.stdio import stdio_server
from mcp.types import Tool, TextContent

# 添加 scripts 目录
SCRIPT_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "scripts")
sys.path.insert(0, SCRIPT_DIR)

from device import DeviceManager, Device
from recorder import Recorder
from screencap import ScreenCapture
from logcat_capture import LogcatCapture
from ai_analysis import AIAnalyzer
from reporter import Reporter, ReportData

# 全局状态
OUTPUT_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "output", "mcp_runs")
os.makedirs(OUTPUT_DIR, exist_ok=True)

device_mgr: Optional[DeviceManager] = None
tool_lock = threading.Lock()
recorder: Optional[Recorder] = None
screencap: Optional[ScreenCapture] = None
logcat: Optional[LogcatCapture] = None
ai_analyzer: Optional[AIAnalyzer] = None
current_device: Optional[Device] = None

server = Server("android-qa")


def _ensure_init():
    global device_mgr
    if device_mgr is None:
        device_mgr = DeviceManager()
        _cleanup_old_files()

def _cleanup_old_files():
    try:
        now = time.time()
        cutoff = now - (3 * 24 * 3600)
        for root, dirs, files in os.walk(OUTPUT_DIR):
            for file in files:
                filepath = os.path.join(root, file)
                if os.path.isfile(filepath):
                    if os.path.getmtime(filepath) < cutoff:
                        os.remove(filepath)
    except Exception:
        pass


def _get_serial() -> str:
    if current_device:
        return current_device.serial
    raise RuntimeError("No device connected. Call qa_connect first.")


@server.list_tools()
async def list_tools():
    return [
        Tool(
            name="qa_connect",
            description="连接 Android 设备或模拟器。返回设备信息。",
            inputSchema={
                "type": "object",
                "properties": {
                    "serial": {"type": "string", "description": "设备序列号（可选，默认自动发现）"}
                },
            }
        ),
        Tool(
            name="qa_screenshot",
            description="截取当前屏幕截图。返回截图路径和 UI 元素列表。",
            inputSchema={
                "type": "object",
                "properties": {
                    "name": {"type": "string", "description": "截图名称"},
                    "with_layout": {"type": "boolean", "description": "是否同时抓取 layout dump", "default": True}
                },
            }
        ),
        Tool(
            name="qa_layout_dump",
            description="获取当前屏幕的 UI 布局树（JSON 格式），包含所有元素的文本、坐标、交互能力。",
            inputSchema={"type": "object", "properties": {}}
        ),
        Tool(
            name="qa_tap",
            description="点击屏幕上的元素。支持文本查找、resource ID 查找、或直接坐标。",
            inputSchema={
                "type": "object",
                "properties": {
                    "target": {"type": "string", "description": "点击目标：'text:文字'、'resource:id/xxx'、或 'x,y' 坐标"}
                },
                "required": ["target"]
            }
        ),
        Tool(
            name="qa_swipe",
            description="在屏幕上滑动。",
            inputSchema={
                "type": "object",
                "properties": {
                    "direction": {"type": "string", "enum": ["up", "down", "left", "right"], "description": "滑动方向"},
                    "duration_ms": {"type": "integer", "description": "滑动时长（毫秒）", "default": 300}
                },
                "required": ["direction"]
            }
        ),
        Tool(
            name="qa_launch",
            description="启动指定应用。",
            inputSchema={
                "type": "object",
                "properties": {
                    "package": {"type": "string", "description": "应用包名"},
                    "activity": {"type": "string", "description": "Activity 名称（可选）"}
                },
                "required": ["package"]
            }
        ),
        Tool(
            name="qa_logcat_start",
            description="开始捕获 logcat 日志，支持实时异常检测。",
            inputSchema={
                "type": "object",
                "properties": {
                    "watch_package": {"type": "string", "description": "要监控的应用包名"}
                }
            }
        ),
        Tool(
            name="qa_logcat_stop",
            description="停止 logcat 捕获，返回分析结果。",
            inputSchema={"type": "object", "properties": {}}
        ),
        Tool(
            name="qa_recording_start",
            description="开始屏幕录制。",
            inputSchema={
                "type": "object",
                "properties": {
                    "filename": {"type": "string", "description": "录屏文件名", "default": "recording.mp4"}
                }
            }
        ),
        Tool(
            name="qa_recording_stop",
            description="停止屏幕录制，返回录屏文件路径。",
            inputSchema={"type": "object", "properties": {}}
        ),
        Tool(
            name="qa_run_test",
            description="运行完整的 Android 测试。输入 test_plan.json 路径，自动执行所有场景并生成报告。",
            inputSchema={
                "type": "object",
                "properties": {
                    "test_plan_path": {"type": "string", "description": "测试计划文件路径"},
                    "skip_recording": {"type": "boolean", "description": "是否跳过录屏", "default": True}
                },
                "required": ["test_plan_path"]
            }
        ),
        Tool(
            name="qa_find_element",
            description="在当前 UI 中查找元素，返回坐标和属性。不执行点击。",
            inputSchema={
                "type": "object",
                "properties": {
                    "text": {"type": "string", "description": "要查找的文本"},
                    "resource_id": {"type": "string", "description": "要查找的 resource ID"}
                }
            }
        ),
        Tool(
            name="qa_check_app_alive",
            description="检查指定应用是否在前台运行。用于验证操作是否导致 crash。",
            inputSchema={
                "type": "object",
                "properties": {
                    "package": {"type": "string", "description": "应用包名"}
                },
                "required": ["package"]
            }
        ),
        Tool(
            name="qa_long_press",
            description="长按屏幕上的元素（用于打开上下文菜单）。支持文本查找或坐标。",
            inputSchema={
                "type": "object",
                "properties": {
                    "target": {"type": "string", "description": "长按目标：'text:文字' 或 'x,y' 坐标"},
                    "duration_ms": {"type": "integer", "description": "长按时长（毫秒）", "default": 1000}
                },
                "required": ["target"]
            }
        ),
        Tool(
            name="qa_type",
            description="在当前焦点输入框中输入文本。先点击输入框获得焦点，再输入。",
            inputSchema={
                "type": "object",
                "properties": {
                    "text": {"type": "string", "description": "要输入的文本"},
                    "clear_first": {"type": "boolean", "description": "是否先清空输入框", "default": True}
                },
                "required": ["text"]
            }
        ),
        Tool(
            name="qa_press_key",
            description="按下系统按键（返回、主页、最近任务等）。",
            inputSchema={
                "type": "object",
                "properties": {
                    "key": {"type": "string", "enum": ["back", "home", "recent", "enter", "tab", "delete"], "description": "按键名称"}
                },
                "required": ["key"]
            }
        ),
        Tool(
            name="qa_wait_element",
            description="等待某个文本元素出现在屏幕上。超时返回未找到。",
            inputSchema={
                "type": "object",
                "properties": {
                    "text": {"type": "string", "description": "要等待出现的文本"},
                    "timeout_s": {"type": "integer", "description": "超时秒数", "default": 10}
                },
                "required": ["text"]
            }
        ),
        Tool(
            name="qa_get_text",
            description="获取屏幕上指定区域的文本内容（通过布局 dump）。",
            inputSchema={
                "type": "object",
                "properties": {
                    "text": {"type": "string", "description": "要查找的文本（返回包含该文本的元素信息）"}
                }
            }
        ),
        Tool(
            name="qa_scroll_find",
            description="滚动屏幕直到找到指定文本元素。返回是否找到及坐标。",
            inputSchema={
                "type": "object",
                "properties": {
                    "text": {"type": "string", "description": "要查找的文本"},
                    "direction": {"type": "string", "enum": ["up", "down"], "description": "滚动方向（默认 up=向下滚动内容）", "default": "up"},
                    "max_scrolls": {"type": "integer", "description": "最大滚动次数", "default": 10},
                    "scroll_pause": {"type": "number", "description": "每次滚动后等待秒数", "default": 1.0}
                },
                "required": ["text"]
            }
        ),
        Tool(
            name="qa_element_state",
            description="检查 UI 元素的状态属性（enabled/checked/selected/scrollable/focusable）。返回元素详细状态。",
            inputSchema={
                "type": "object",
                "properties": {
                    "text": {"type": "string", "description": "按文本查找元素"},
                    "resource_id": {"type": "string", "description": "按 resource ID 查找元素"}
                }
            }
        ),
        Tool(
            name="qa_type_unicode",
            description="输入 Unicode 文本（支持中文等非 ASCII 字符）。通过剪贴板粘贴实现。",
            inputSchema={
                "type": "object",
                "properties": {
                    "text": {"type": "string", "description": "要输入的文本（支持中文）"},
                    "clear_first": {"type": "boolean", "description": "是否先清空输入框", "default": True}
                },
                "required": ["text"]
            }
        ),
        Tool(
            name="qa_set_clipboard",
            description="设置设备剪贴板文本。",
            inputSchema={
                "type": "object",
                "properties": {
                    "text": {"type": "string", "description": "要设置的文本"}
                },
                "required": ["text"]
            }
        ),
        Tool(
            name="qa_get_clipboard",
            description="获取设备剪贴板文本。",
            inputSchema={"type": "object", "properties": {}}
        ),
        Tool(
            name="qa_notifications",
            description="展开或收起通知栏。",
            inputSchema={
                "type": "object",
                "properties": {
                    "action": {"type": "string", "enum": ["expand", "collapse"], "description": "操作：expand 或 collapse"}
                },
                "required": ["action"]
            }
        ),
        Tool(
            name="qa_drag",
            description="拖拽操作：从一个位置拖到另一个位置。",
            inputSchema={
                "type": "object",
                "properties": {
                    "from": {"type": "string", "description": "起点：'x,y' 坐标或 'text:文字'"},
                    "to": {"type": "string", "description": "终点：'x,y' 坐标或 'text:文字'"},
                    "duration_ms": {"type": "integer", "description": "拖拽时长（毫秒）", "default": 500}
                },
                "required": ["from", "to"]
            }
        ),
        Tool(
            name="qa_double_tap",
            description="双击屏幕元素。",
            inputSchema={
                "type": "object",
                "properties": {
                    "target": {"type": "string", "description": "双击目标：'text:文字' 或 'x,y' 坐标"}
                },
                "required": ["target"]
            }
        ),
        Tool(
            name="qa_shell",
            description="在设备上执行任意 ADB shell 命令。高级用法，慎用。",
            inputSchema={
                "type": "object",
                "properties": {
                    "command": {"type": "string", "description": "要执行的 shell 命令"},
                    "timeout_s": {"type": "integer", "description": "超时秒数", "default": 30}
                },
                "required": ["command"]
            }
        ),
        Tool(
            name="qa_push_file",
            description="推送本地文件到设备存储。",
            inputSchema={
                "type": "object",
                "properties": {
                    "local_path": {"type": "string", "description": "本地文件路径"},
                    "device_path": {"type": "string", "description": "设备目标路径（如 /sdcard/Download/）"}
                },
                "required": ["local_path", "device_path"]
            }
        ),
        Tool(
            name="qa_pull_file",
            description="从设备拉取文件到本地。",
            inputSchema={
                "type": "object",
                "properties": {
                    "device_path": {"type": "string", "description": "设备文件路径"},
                    "local_path": {"type": "string", "description": "本地保存路径"}
                },
                "required": ["device_path", "local_path"]
            }
        ),
        Tool(
            name="qa_measure_startup",
            description="测量应用冷启动时间。返回 TotalTime/WaitTime/ThisTime（毫秒）。",
            inputSchema={
                "type": "object",
                "properties": {
                    "package": {"type": "string", "description": "应用包名"}
                },
                "required": ["package"]
            }
        ),
        Tool(
            name="qa_dump_meminfo",
            description="获取应用内存使用信息（PSS/RSS）。",
            inputSchema={
                "type": "object",
                "properties": {
                    "package": {"type": "string", "description": "应用包名"}
                },
                "required": ["package"]
            }
        ),
        Tool(
            name="qa_dump_gfxinfo",
            description="获取应用帧渲染信息（总帧数/卡顿帧/百分位延迟）。",
            inputSchema={
                "type": "object",
                "properties": {
                    "package": {"type": "string", "description": "应用包名"}
                },
                "required": ["package"]
            }
        ),
    ]


def _handle_tool_call(name: str, arguments: dict) -> list[TextContent]:
    """工具调用的实际逻辑（同步，在线程中执行）"""
    _ensure_init()
    global current_device

    # ── qa_connect ──
    if name == "qa_connect":
        serial = arguments.get("serial")
        if serial:
            devs = device_mgr.list_devices()
            current_device = next((d for d in devs if d.serial == serial), None)
        else:
            current_device = device_mgr.get_ready_device()
        if not current_device:
            return [TextContent(type="text", text=json.dumps({"error": "No device found"}))]
        info = device_mgr.get_device_info(current_device.serial)
        return [TextContent(type="text", text=json.dumps({
            "serial": current_device.serial,
            "model": info.get("model", ""),
            "android_version": info.get("android_version", ""),
            "api_level": info.get("api_level", ""),
            "screen_size": info.get("screen_size", ""),
        }, ensure_ascii=False))]

    # ── qa_screenshot ──
    elif name == "qa_screenshot":
        serial = _get_serial()
        with_layout = arguments.get("with_layout", True)
        sname = arguments.get("name", f"screenshot_{int(time.time())}")

        screenshot_path = screencap.capture_screenshot(serial, sname)
        result = {"screenshot": screenshot_path}

        if with_layout:
            layout_path = screencap.capture_layout(serial, f"layout_{sname}")
            result["layout"] = layout_path
            try:
                with open(layout_path) as f:
                    layout_data = json.load(f)
                elements = [e for e in layout_data if e.get("text")]
                result["elements"] = [{"text": e["text"], "center": e.get("center", "")} for e in elements]
            except Exception:
                result["elements"] = []

        return [TextContent(type="text", text=json.dumps(result, ensure_ascii=False))]

    # ── qa_layout_dump ──
    elif name == "qa_layout_dump":
        serial = _get_serial()
        layout_path = screencap.capture_layout(serial, "manual_dump")
        try:
            with open(layout_path) as f:
                data = json.load(f)
            return [TextContent(type="text", text=json.dumps(data, ensure_ascii=False, indent=2))]
        except Exception as e:
            return [TextContent(type="text", text=json.dumps({"error": str(e)}))]

    # ── qa_tap ──
    elif name == "qa_tap":
        serial = _get_serial()
        target = arguments.get("target", "")
        result = _do_tap_sync(serial, target)
        return [TextContent(type="text", text=json.dumps(result, ensure_ascii=False))]

    # ── qa_swipe ──
    elif name == "qa_swipe":
        serial = _get_serial()
        direction = arguments.get("direction", "up")
        duration = arguments.get("duration_ms", 300)
        info = device_mgr.get_device_info(serial)
        try:
            w, h = [int(x) for x in info.get("screen_size", "1080x1920").split("x")]
        except ValueError:
            w, h = 1080, 1920
        cx, cy = w // 2, h // 2
        if direction == "up":
            device_mgr.swipe(cx, cy + 300, cx, cy - 300, duration, serial)
        elif direction == "down":
            device_mgr.swipe(cx, cy - 300, cx, cy + 300, duration, serial)
        elif direction == "left":
            device_mgr.swipe(cx - 300, cy, cx + 300, cy, duration, serial)
        elif direction == "right":
            device_mgr.swipe(cx + 300, cy, cx - 300, cy, duration, serial)
        return [TextContent(type="text", text=json.dumps({"ok": True, "direction": direction}))]

    # ── qa_launch ──
    elif name == "qa_launch":
        serial = _get_serial()
        package = arguments.get("package", "")
        activity = arguments.get("activity", "")
        ok = device_mgr.launch_app(package, activity, serial)
        time.sleep(2)
        return [TextContent(type="text", text=json.dumps({"ok": ok, "package": package}))]

    # ── qa_logcat_start ──
    elif name == "qa_logcat_start":
        serial = _get_serial()
        watch = arguments.get("watch_package")
        logcat.start(serial, watch_package=watch)
        return [TextContent(type="text", text=json.dumps({"ok": True, "watch_package": watch}))]

    # ── qa_logcat_stop ──
    elif name == "qa_logcat_stop":
        if not logcat.is_running():
            return [TextContent(type="text", text=json.dumps({"error": "logcat is not capturing"}))]
        log_path = logcat.stop()
        analysis = logcat.analyze()
        return [TextContent(type="text", text=json.dumps({
            "log_path": log_path,
            "total_lines": analysis.total_lines,
            "errors": len(analysis.errors),
            "warnings": len(analysis.warnings),
            "crashes": len(analysis.crashes),
            "anrs": len(analysis.anrs),
            "crash_details": [{"tag": c.tag, "message": c.message[:200]} for c in analysis.crashes[:5]],
        }, ensure_ascii=False))]

    # ── qa_recording_start ──
    elif name == "qa_recording_start":
        serial = _get_serial()
        filename = arguments.get("filename", "recording.mp4")
        recorder.start(serial, filename)
        return [TextContent(type="text", text=json.dumps({"ok": True, "filename": filename}))]

    # ── qa_recording_stop ──
    elif name == "qa_recording_stop":
        serial = _get_serial()
        path = recorder.stop(serial)
        return [TextContent(type="text", text=json.dumps({"ok": True, "path": path}))]

    # ── qa_run_test ──
    elif name == "qa_run_test":
        test_plan = arguments.get("test_plan_path", "")
        if not os.path.isabs(test_plan):
            test_plan = os.path.abspath(test_plan)
        result = subprocess.run(
            [sys.executable, os.path.join(SCRIPT_DIR, "runner.py"),
             test_plan, "-o", OUTPUT_DIR],
            capture_output=True, text=True, timeout=300, stdin=subprocess.DEVNULL,
        )
        return [TextContent(type="text", text=json.dumps({
            "stdout": result.stdout[-2000:],
            "returncode": result.returncode,
        }))]

    # ── qa_find_element ──
    elif name == "qa_find_element":
        serial = _get_serial()
        text = arguments.get("text", "")
        resource_id = arguments.get("resource_id", "")
        layout_path = screencap.capture_layout(serial, "find_elem")
        if not layout_path:
            return [TextContent(type="text", text=json.dumps({"found": False, "error": "Failed to dump layout"}))]
        result = {"found": False}
        if text:
            elem = screencap.find_element_by_text(layout_path, text)
            if elem:
                center = screencap.get_center(elem.get("center", "") or elem.get("bounds", ""))
                result = {"found": True, "text": text, "center": center, "element": elem}
        if resource_id:
            elem = screencap.find_element_by_resource(layout_path, resource_id)
            if elem:
                center = screencap.get_center(elem.get("center", "") or elem.get("bounds", ""))
                result = {"found": True, "resource_id": resource_id, "center": center, "element": elem}
        return [TextContent(type="text", text=json.dumps(result, ensure_ascii=False))]

    # ── qa_check_app_alive ──
    elif name == "qa_check_app_alive":
        serial = _get_serial()
        package = arguments.get("package", "")
        is_alive = logcat.is_app_alive() if logcat._is_capturing else True
        output = subprocess.run(
            [device_mgr._adb, "-s", serial, "shell", "dumpsys", "window", "windows"],
            capture_output=True, timeout=5, stdin=subprocess.DEVNULL,
            encoding="utf-8", errors="replace",
        ).stdout
        in_foreground = package in output if package else False
        return [TextContent(type="text", text=json.dumps({
            "alive": is_alive,
            "in_foreground": in_foreground,
            "package": package,
        }))]

    # ── qa_long_press ──
    elif name == "qa_long_press":
        serial = _get_serial()
        target = arguments.get("target", "")
        duration = arguments.get("duration_ms", 1000)
        result = _do_long_press_sync(serial, target, duration)
        return [TextContent(type="text", text=json.dumps(result, ensure_ascii=False))]

    # ── qa_type (legacy, ASCII-only) ──
    elif name == "qa_type":
        serial = _get_serial()
        text = arguments.get("text", "")
        clear = arguments.get("clear_first", True)
        if clear:
            device_mgr._run(["-s", serial, "shell", "input", "keyevent", "KEYCODE_MOVE_HOME"])
            device_mgr._run(["-s", serial, "shell", "input", "keyevent", "--longpress"] + ["KEYCODE_DEL"] * 20)
        # Use unicode-capable method
        device_mgr.input_text_unicode(text, serial)
        return [TextContent(type="text", text=json.dumps({"ok": True, "text": text}))]

    # ── qa_press_key ──
    elif name == "qa_press_key":
        serial = _get_serial()
        key = arguments.get("key", "back")
        key_map = {
            "back": "KEYCODE_BACK", "home": "KEYCODE_HOME",
            "recent": "KEYCODE_APP_SWITCH", "enter": "KEYCODE_ENTER",
            "tab": "KEYCODE_TAB", "delete": "KEYCODE_DEL",
        }
        keycode = key_map.get(key, "KEYCODE_BACK")
        subprocess.run(
            [device_mgr._adb, "-s", serial, "shell", "input", "keyevent", keycode],
            capture_output=True, timeout=5, stdin=subprocess.DEVNULL,
        )
        return [TextContent(type="text", text=json.dumps({"ok": True, "key": key}))]

    # ── qa_wait_element ──
    elif name == "qa_wait_element":
        serial = _get_serial()
        text = arguments.get("text", "")
        timeout_s = arguments.get("timeout_s", 10)
        deadline = time.time() + timeout_s
        while time.time() < deadline:
            layout_path = screencap.capture_layout(serial, "wait_elem")
            if layout_path:
                elem = screencap.find_element_by_text(layout_path, text)
                if elem:
                    center = screencap.get_center(elem.get("center", "") or elem.get("bounds", ""))
                    return [TextContent(type="text", text=json.dumps({"found": True, "text": text, "center": center}))]
            time.sleep(1)
        return [TextContent(type="text", text=json.dumps({"found": False, "text": text, "timeout_s": timeout_s}))]

    # ── qa_get_text ──
    elif name == "qa_get_text":
        serial = _get_serial()
        text_filter = arguments.get("text", "")
        layout_path = screencap.capture_layout(serial, "get_text")
        if not layout_path:
            return [TextContent(type="text", text=json.dumps({"error": "Failed to dump layout"}))]
        try:
            with open(layout_path) as f:
                data = json.load(f)
            if text_filter:
                matches = [e for e in data if text_filter in (e.get("text") or "")]
                return [TextContent(type="text", text=json.dumps({"elements": matches, "count": len(matches)}, ensure_ascii=False))]
            else:
                texts = [{"text": e.get("text", ""), "center": e.get("center", "")} for e in data if e.get("text")]
                return [TextContent(type="text", text=json.dumps({"elements": texts, "count": len(texts)}, ensure_ascii=False))]
        except Exception as e:
            return [TextContent(type="text", text=json.dumps({"error": str(e)}))]

    # ── qa_scroll_find ──
    elif name == "qa_scroll_find":
        serial = _get_serial()
        text = arguments.get("text", "")
        direction = arguments.get("direction", "up")
        max_scrolls = arguments.get("max_scrolls", 10)
        scroll_pause = arguments.get("scroll_pause", 1.0)
        info = device_mgr.get_device_info(serial)
        try:
            w, h = [int(x) for x in info.get("screen_size", "1080x1920").split("x")]
        except ValueError:
            w, h = 1080, 1920
        cx, cy = w // 2, h // 2

        for i in range(max_scrolls):
            layout_path = screencap.capture_layout(serial, f"scroll_{i}")
            if layout_path:
                elem = screencap.find_element_by_text(layout_path, text)
                if elem:
                    center = screencap.get_center(elem.get("center", "") or elem.get("bounds", ""))
                    return [TextContent(type="text", text=json.dumps({
                        "found": True, "text": text, "scrolled": i,
                        "center": center
                    }))]
            # Scroll: up = swipe up (content moves up, reveals items below)
            if direction == "up":
                device_mgr.swipe(cx, cy + 300, cx, cy - 300, 300, serial)
            else:
                device_mgr.swipe(cx, cy - 300, cx, cy + 300, 300, serial)
            time.sleep(scroll_pause)

        return [TextContent(type="text", text=json.dumps({
            "found": False, "text": text, "scrolled": max_scrolls
        }))]

    # ── qa_element_state ──
    elif name == "qa_element_state":
        serial = _get_serial()
        text = arguments.get("text", "")
        resource_id = arguments.get("resource_id", "")
        layout_path = screencap.capture_layout(serial, "elem_state")
        if not layout_path:
            return [TextContent(type="text", text=json.dumps({"error": "Failed to dump layout"}))]
        elem = None
        if text:
            elem = screencap.find_element_by_text(layout_path, text)
        elif resource_id:
            elem = screencap.find_element_by_resource(layout_path, resource_id)
        if not elem:
            return [TextContent(type="text", text=json.dumps({"found": False, "text": text, "resource_id": resource_id}))]
        return [TextContent(type="text", text=json.dumps({
            "found": True,
            "text": elem.get("text", ""),
            "resource_id": elem.get("resource_id", "") or elem.get("resource-id", ""),
            "class": elem.get("class_name", "") or elem.get("class", ""),
            "enabled": elem.get("enabled", "unknown"),
            "checked": elem.get("checked", "unknown"),
            "selected": elem.get("selected", "unknown"),
            "clickable": elem.get("clickable", "unknown"),
            "focusable": elem.get("focusable", "unknown"),
            "focused": elem.get("focused", "unknown"),
            "scrollable": elem.get("scrollable", "unknown"),
            "bounds": elem.get("bounds", ""),
            "center": elem.get("center", ""),
        }, ensure_ascii=False))]

    # ── qa_type_unicode ──
    elif name == "qa_type_unicode":
        serial = _get_serial()
        text = arguments.get("text", "")
        clear = arguments.get("clear_first", True)
        if clear:
            device_mgr._run(["-s", serial, "shell", "input", "keyevent", "KEYCODE_MOVE_HOME"])
            device_mgr._run(["-s", serial, "shell", "input", "keyevent", "--longpress"] + ["KEYCODE_DEL"] * 20)
        device_mgr.input_text_unicode(text, serial)
        return [TextContent(type="text", text=json.dumps({"ok": True, "text": text}))]

    # ── qa_set_clipboard ──
    elif name == "qa_set_clipboard":
        serial = _get_serial()
        text = arguments.get("text", "")
        device_mgr.clipboard_set(text, serial)
        return [TextContent(type="text", text=json.dumps({"ok": True, "text": text}))]

    # ── qa_get_clipboard ──
    elif name == "qa_get_clipboard":
        serial = _get_serial()
        text = device_mgr.clipboard_get(serial)
        return [TextContent(type="text", text=json.dumps({"text": text}))]

    # ── qa_notifications ──
    elif name == "qa_notifications":
        serial = _get_serial()
        action = arguments.get("action", "expand")
        if action == "expand":
            device_mgr.notifications_expand(serial)
        else:
            device_mgr.notifications_collapse(serial)
        return [TextContent(type="text", text=json.dumps({"ok": True, "action": action}))]

    # ── qa_drag ──
    elif name == "qa_drag":
        serial = _get_serial()
        from_target = arguments.get("from", "")
        to_target = arguments.get("to", "")
        duration = arguments.get("duration_ms", 500)
        from_xy = _resolve_target_to_xy(from_target, serial)
        to_xy = _resolve_target_to_xy(to_target, serial)
        if not from_xy or not to_xy:
            return [TextContent(type="text", text=json.dumps({
                "ok": False,
                "error": f"Could not resolve: from={from_xy}, to={to_xy}"
            }))]
        device_mgr.drag(from_xy[0], from_xy[1], to_xy[0], to_xy[1], duration, serial)
        return [TextContent(type="text", text=json.dumps({
            "ok": True, "from": from_xy, "to": to_xy, "duration_ms": duration
        }))]

    # ── qa_double_tap ──
    elif name == "qa_double_tap":
        serial = _get_serial()
        target = arguments.get("target", "")
        xy = _resolve_target_to_xy(target, serial)
        if not xy:
            return [TextContent(type="text", text=json.dumps({"ok": False, "error": f"Target not found: {target}"}))]
        device_mgr.double_tap(xy[0], xy[1], serial)
        return [TextContent(type="text", text=json.dumps({"ok": True, "target": target, "coordinates": xy}))]

    # ── qa_shell ──
    elif name == "qa_shell":
        serial = _get_serial()
        command = arguments.get("command", "")
        timeout = arguments.get("timeout_s", 30)
        result = device_mgr.run_shell(command, serial, timeout)
        return [TextContent(type="text", text=json.dumps({
            "stdout": result["stdout"][:5000],
            "returncode": result["returncode"]
        }, ensure_ascii=False))]

    # ── qa_push_file ──
    elif name == "qa_push_file":
        serial = _get_serial()
        local = arguments.get("local_path", "")
        device = arguments.get("device_path", "")
        if not os.path.exists(local):
            return [TextContent(type="text", text=json.dumps({"ok": False, "error": f"Local file not found: {local}"}))]
        ok = device_mgr.push_file(local, device, serial)
        return [TextContent(type="text", text=json.dumps({"ok": ok, "local": local, "device": device}))]

    # ── qa_pull_file ──
    elif name == "qa_pull_file":
        serial = _get_serial()
        device = arguments.get("device_path", "")
        local = arguments.get("local_path", "")
        ok = device_mgr.pull_file(device, local, serial)
        return [TextContent(type="text", text=json.dumps({"ok": ok, "device": device, "local": local}))]

    # ── qa_measure_startup ──
    elif name == "qa_measure_startup":
        serial = _get_serial()
        package = arguments.get("package", "")
        timing = device_mgr.measure_startup(package, serial)
        return [TextContent(type="text", text=json.dumps({"ok": True, "package": package, **timing}))]

    # ── qa_dump_meminfo ──
    elif name == "qa_dump_meminfo":
        serial = _get_serial()
        package = arguments.get("package", "")
        info = device_mgr.dump_meminfo(package, serial)
        return [TextContent(type="text", text=json.dumps({"ok": True, "package": package, **info}))]

    # ── qa_dump_gfxinfo ──
    elif name == "qa_dump_gfxinfo":
        serial = _get_serial()
        package = arguments.get("package", "")
        info = device_mgr.dump_gfxinfo(package, serial)
        return [TextContent(type="text", text=json.dumps({"ok": True, "package": package, **info}))]

    else:
        return [TextContent(type="text", text=json.dumps({"error": f"Unknown tool: {name}"}))]


def _resolve_target_to_xy(target: str, serial: str) -> Optional[list[int]]:
    """Resolve a target string (text:文字, x,y, resource:id/xxx) to [x, y] coordinates."""
    if target.startswith("text:"):
        text = target.split(":", 1)[1]
        layout_path = screencap.capture_layout(serial, "resolve_target")
        if layout_path:
            elem = screencap.find_element_by_text(layout_path, text)
            if elem:
                center = screencap.get_center(elem.get("center", "") or elem.get("bounds", ""))
                if center:
                    return list(center)
    elif target.startswith("resource:"):
        res_id = target.split(":", 1)[1]
        layout_path = screencap.capture_layout(serial, "resolve_target")
        if layout_path:
            elem = screencap.find_element_by_resource(layout_path, res_id)
            if elem:
                center = screencap.get_center(elem.get("center", "") or elem.get("bounds", ""))
                if center:
                    return list(center)
    elif "," in target:
        parts = target.split(",")
        try:
            return [int(parts[0].strip()), int(parts[1].strip())]
        except (ValueError, IndexError):
            pass
    return None


def _do_tap_sync(serial: str, target: str) -> dict:
    """tap 实现（同步，在线程中运行）"""
    if target.startswith("text:"):
        text = target.split(":", 1)[1]
        layout_path = screencap.capture_layout(serial, "tap_find")
        elem = screencap.find_element_by_text(layout_path, text)
        if elem:
            center = screencap.get_center(elem.get("center", "") or elem.get("bounds", ""))
            if center:
                device_mgr.tap(center[0], center[1], serial)
                return {"ok": True, "text": text, "coordinates": list(center)}
    elif target.startswith("resource:"):
        res_id = target.split(":", 1)[1]
        layout_path = screencap.capture_layout(serial, "tap_find")
        elem = screencap.find_element_by_resource(layout_path, res_id)
        if elem:
            center = screencap.get_center(elem.get("center", "") or elem.get("bounds", ""))
            if center:
                device_mgr.tap(center[0], center[1], serial)
                return {"ok": True, "resource_id": res_id, "coordinates": list(center)}
    elif "," in target:
        parts = target.split(",")
        x, y = int(parts[0].strip()), int(parts[1].strip())
        device_mgr.tap(x, y, serial)
        return {"ok": True, "coordinates": [x, y]}

    return {"ok": False, "error": f"Element not found: {target}"}


def _do_long_press_sync(serial: str, target: str, duration_ms: int = 1000) -> dict:
    """long_press 实现（同步）"""
    if target.startswith("text:"):
        text = target.split(":", 1)[1]
        layout_path = screencap.capture_layout(serial, "longpress_find")
        elem = screencap.find_element_by_text(layout_path, text)
        if elem:
            center = screencap.get_center(elem.get("center", "") or elem.get("bounds", ""))
            if center:
                # 用 input swipe 模拟长按（起点=终点，持续 duration_ms）
                subprocess.run(
                    [device_mgr._adb, "-s", serial, "shell", "input", "swipe",
                     str(center[0]), str(center[1]), str(center[0]), str(center[1]), str(duration_ms)],
                    capture_output=True, timeout=5, stdin=subprocess.DEVNULL,
                )
                return {"ok": True, "text": text, "coordinates": list(center), "duration_ms": duration_ms}
    elif "," in target:
        parts = target.split(",")
        x, y = int(parts[0].strip()), int(parts[1].strip())
        subprocess.run(
            [device_mgr._adb, "-s", serial, "shell", "input", "swipe",
             str(x), str(y), str(x), str(y), str(duration_ms)],
            capture_output=True, timeout=5, stdin=subprocess.DEVNULL,
        )
        return {"ok": True, "coordinates": [x, y], "duration_ms": duration_ms}

    return {"ok": False, "error": f"Element not found for long_press: {target}"}


@server.call_tool()
async def call_tool(name: str, arguments: dict) -> list[TextContent]:
    """MCP tool handler — 把阻塞逻辑放到线程池，不冻结事件循环"""
    loop = asyncio.get_event_loop()
    
    timeout_s = 15.0
    if name == "qa_run_test":
        timeout_s = 300.0
    elif name == "qa_logcat_stop":
        timeout_s = 30.0
    elif name == "qa_scroll_find":
        timeout_s = 60.0  # up to 10 scrolls * 1s each + overhead
    elif name == "qa_shell":
        timeout_s = 60.0  # user-configurable, but cap at 60s

    try:
        def _locked_call():
            with tool_lock:
                return _handle_tool_call(name, arguments)
        return await asyncio.wait_for(loop.run_in_executor(None, _locked_call), timeout=timeout_s)
    except asyncio.TimeoutError:
        return [TextContent(type="text", text=json.dumps({"error": f"Tool timed out after {timeout_s} seconds"}))]
    except Exception as e:
        return [TextContent(type="text", text=json.dumps({"error": str(e)}))]


async def main():
    global screencap, logcat, recorder, ai_analyzer
    _ensure_init()

    recorder = Recorder(
        adb_path=device_mgr._adb,
        output_dir=os.path.join(OUTPUT_DIR, "video"),
    )
    screencap = ScreenCapture(
        adb_path=device_mgr._adb,
        android_cli=device_mgr._android_cli,
        output_dir=OUTPUT_DIR,
    )
    logcat = LogcatCapture(
        adb_path=device_mgr._adb,
        output_dir=os.path.join(OUTPUT_DIR, "logs"),
    )
    ai_analyzer = AIAnalyzer(output_dir=os.path.join(OUTPUT_DIR, "analysis"))

    # 优雅关闭处理
    import signal
    def shutdown_handler(sig, frame):
        if current_device:
            try:
                recorder.stop(current_device.serial)
            except Exception:
                pass
            try:
                logcat.stop()
            except Exception:
                pass
    signal.signal(signal.SIGTERM, shutdown_handler)
    signal.signal(signal.SIGINT, shutdown_handler)

    async with stdio_server() as (read_stream, write_stream):
        await server.run(read_stream, write_stream, server.create_initialization_options())


if __name__ == "__main__":
    asyncio.run(main())
