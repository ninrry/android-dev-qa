#!/usr/bin/env python3
"""android-qa-mcp — Universal Android Dev QA MCP Server

Pluggable device control backend (ADB by default) + built-in vision analysis + 34 MCP tools.

Tools:
  Device:       qa_connect, qa_launch, qa_shell, qa_check_app_alive,
                qa_push_file, qa_pull_file
  UI interaction: qa_tap, qa_long_press, qa_double_tap, qa_swipe, qa_drag
  Text input:  qa_type, qa_type_unicode, qa_set_clipboard, qa_get_clipboard
  UI inspection: qa_screenshot, qa_layout_dump, qa_find_element,
                qa_element_state, qa_get_text, qa_wait_element, qa_scroll_find
  System:      qa_press_key, qa_notifications
  Performance: qa_measure_startup, qa_dump_meminfo, qa_dump_gfxinfo
  Logging/rec: qa_logcat_start, qa_logcat_stop,
                qa_recording_start, qa_recording_stop
  Vision:      qa_analyze_screenshot, qa_analyze_video, qa_analyze_logcat
  Test runner: qa_run_test
"""
import asyncio
import json
import logging
import os
import sys
import time
import threading
from typing import Optional

from mcp.server import Server
from mcp.server.stdio import stdio_server
from mcp.types import Tool, TextContent

# ── Module path setup ────────────────────────────────────────────────────
SCRIPT_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "scripts")
if SCRIPT_DIR not in sys.path:
    sys.path.insert(0, SCRIPT_DIR)

from backends import get_backend, DeviceBackend
from backends.base import DeviceInfo
from vision import get_vision_engine, VisionEngine
from analysis import SCREENSHOT_UI_PROMPT, VIDEO_INTERACTION_PROMPT, LOGCAT_PROMPT

logger = logging.getLogger("android-qa")
logging.basicConfig(level=logging.INFO, format="%(name)s %(levelname)s: %(message)s")

# ── Global state ──────────────────────────────────────────────────────────
# Minimal: just the backend instance and connected device info.
# No global lock — each tool call runs independently via asyncio executor.
OUTPUT_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "output", "mcp_runs")
os.makedirs(OUTPUT_DIR, exist_ok=True)

_backend: Optional[DeviceBackend] = None
_device_info: Optional[DeviceInfo] = None
_backend_lock = threading.Lock()  # Only protects _backend/_device_info writes


def _ensure_backend() -> DeviceBackend:
    """Lazy-initialize the backend on first call."""
    global _backend
    if _backend is None:
        with _backend_lock:
            if _backend is None:
                backend_name = os.environ.get("QA_BACKEND", "adb")
                _backend = get_backend(backend_name, output_dir=OUTPUT_DIR)
                logger.info("Backend initialized: %s", _backend.get_info().name)
    return _backend


def _get_device() -> DeviceInfo:
    """Get connected device info — raises if not connected."""
    global _device_info
    if _device_info is None:
        raise RuntimeError("No device connected. Call qa_connect first.")
    return _device_info


def _cleanup_old_files():
    """Remove output files older than 3 days."""
    try:
        now = time.time()
        cutoff = now - 3 * 24 * 3600
        for root, dirs, files in os.walk(OUTPUT_DIR):
            for f in files:
                fp = os.path.join(root, f)
                try:
                    if os.path.getmtime(fp) < cutoff:
                        os.remove(fp)
                except OSError as e:
                    logger.warning("Cleanup failed for %s: %s", fp, e)
    except Exception as e:
        logger.warning("Cleanup sweep failed: %s", e)


# ── MCP Server ────────────────────────────────────────────────────────────

server = Server("android-qa")


@server.list_tools()
async def list_tools():
    return _TOOL_DEFINITIONS


@server.call_tool()
async def call_tool(name: str, arguments: dict) -> list[TextContent]:
    """Route tool calls to the backend. Each runs in a separate thread
    so I/O-bound ADB calls don't block the MCP event loop."""
    try:
        result = await asyncio.get_event_loop().run_in_executor(
            None, _handle_tool, name, arguments
        )
        return [TextContent(type="text", text=json.dumps(result, ensure_ascii=False))]
    except Exception as e:
        logger.error("Tool %s failed: %s", name, e)
        return [TextContent(type="text", text=json.dumps({"error": str(e)}, ensure_ascii=False))]


def _handle_tool(name: str, arguments: dict) -> dict:
    """Synchronous tool handler — runs in executor thread."""
    # ── Vision tools (no device required) ──────────────────────────────────
    if name in ("qa_analyze_screenshot", "qa_analyze_video", "qa_analyze_logcat"):
        engine = get_vision_engine()
        if not engine.available:
            return {"error": "Vision analysis unavailable: no Google API keys configured. Set QA_VISION_API_KEYS or GOOGLE_API_KEY env var."}

        if name == "qa_analyze_screenshot":
            image_path = arguments.get("image_path", "")
            if not os.path.isfile(image_path):
                return {"error": f"Image file not found: {image_path}"}
            custom_prompt = arguments.get("prompt", "")
            prompt = custom_prompt or SCREENSHOT_UI_PROMPT
            result = engine.analyze_screenshot(image_path, prompt)
            if result is None:
                return {"error": "Analysis failed — check logs for details"}
            return result

        if name == "qa_analyze_video":
            video_path = arguments.get("video_path", "")
            if not os.path.isfile(video_path):
                return {"error": f"Video file not found: {video_path}"}
            custom_prompt = arguments.get("prompt", "")
            prompt = custom_prompt or VIDEO_INTERACTION_PROMPT
            result = engine.analyze_video(video_path, prompt)
            if result is None:
                return {"error": "Analysis failed — check logs for details"}
            return result

        if name == "qa_analyze_logcat":
            logcat_path = arguments.get("logcat_path", "")
            custom_prompt = arguments.get("prompt", "")
            prompt = custom_prompt or LOGCAT_PROMPT
            if not os.path.isfile(logcat_path):
                return {"error": f"Logcat file not found: {logcat_path}"}
            try:
                with open(logcat_path, encoding="utf-8", errors="replace") as f:
                    lines = f.readlines()
                excerpt = "".join(lines[-500:])
            except OSError as e:
                return {"error": f"Failed to read logcat: {e}"}
            result = engine.analyze_logcat(excerpt, prompt)
            if result is None:
                return {"error": "Analysis failed — check logs for details"}
            return result

    # ── Device tools (require backend + connected device) ──────────────────
    _ensure_backend()
    _cleanup_old_files()  # lightweight sweep on each call
    b = _backend

    # ── qa_connect ───────────────────────────────────────────────────────
    if name == "qa_connect":
        global _device_info
        serial = arguments.get("serial")
        dev = b.connect(serial or None)
        with _backend_lock:
            _device_info = dev
        return {
            "serial": dev.serial,
            "model": dev.model,
            "android_version": dev.android_version,
            "api_level": dev.api_level,
            "screen_size": f"{dev.screen_width}x{dev.screen_height}",
            "is_emulator": dev.is_emulator,
            "emulator_type": dev.emulator_type,
        }

    # All other tools require a connected device
    _get_device()  # raises if not connected

    # ── qa_screenshot ───────────────────────────────────────────────────
    if name == "qa_screenshot":
        sname = arguments.get("name", f"screenshot_{int(time.time())}")
        with_layout = arguments.get("with_layout", True)
        path = b.screenshot(sname)
        result = {"screenshot": path}
        if with_layout:
            layout = b.layout_dump()
            result["elements"] = [{"text": e.text, "center": f"{e.center_x},{e.center_y}"} for e in layout if e.text]
        return result

    # ── qa_layout_dump ──────────────────────────────────────────────────
    if name == "qa_layout_dump":
        elements = b.layout_dump()
        return [{"text": e.text, "resource_id": e.resource_id, "bounds": e.bounds,
                 "center": f"{e.center_x},{e.center_y}", "clickable": e.clickable}
                for e in elements]

    # ── qa_tap ──────────────────────────────────────────────────────────
    if name == "qa_tap":
        target = arguments.get("target", "")
        xy = _resolve_target(b, target)
        if xy is None:
            return {"ok": False, "error": f"Target not found: {target}"}
        b.tap(xy[0], xy[1])
        return {"ok": True, "x": xy[0], "y": xy[1]}

    # ── qa_long_press ──────────────────────────────────────────────────
    if name == "qa_long_press":
        target = arguments.get("target", "")
        duration = arguments.get("duration_ms", 1000)
        xy = _resolve_target(b, target)
        if xy is None:
            return {"ok": False, "error": f"Target not found: {target}"}
        b.long_press(xy[0], xy[1], duration)
        return {"ok": True}

    # ── qa_double_tap ──────────────────────────────────────────────────
    if name == "qa_double_tap":
        target = arguments.get("target", "")
        xy = _resolve_target(b, target)
        if xy is None:
            return {"ok": False, "error": f"Target not found: {target}"}
        b.double_tap(xy[0], xy[1])
        return {"ok": True}

    # ── qa_swipe ────────────────────────────────────────────────────────
    if name == "qa_swipe":
        direction = arguments.get("direction", "up")
        duration = arguments.get("duration_ms", 300)
        b.swipe(direction, duration)
        return {"ok": True}

    # ── qa_drag ──────────────────────────────────────────────────────────
    if name == "qa_drag":
        from_target = arguments.get("from", "")
        to_target = arguments.get("to", "")
        duration = arguments.get("duration_ms", 500)
        xy1 = _resolve_target(b, from_target)
        xy2 = _resolve_target(b, to_target)
        if xy1 is None:
            return {"ok": False, "error": f"From target not found: {from_target}"}
        if xy2 is None:
            return {"ok": False, "error": f"To target not found: {to_target}"}
        b.drag(xy1[0], xy1[1], xy2[0], xy2[1], duration)
        return {"ok": True}

    # ── qa_launch ────────────────────────────────────────────────────────
    if name == "qa_launch":
        package = arguments.get("package", "")
        activity = arguments.get("activity", "")
        ok = b.launch(package, activity)
        return {"ok": ok, "package": package}

    # ── qa_check_app_alive ──────────────────────────────────────────────
    if name == "qa_check_app_alive":
        package = arguments.get("package", "")
        alive = b.is_app_alive(package)
        return {"alive": alive, "package": package}

    # ── qa_type ──────────────────────────────────────────────────────────
    if name == "qa_type":
        text = arguments.get("text", "")
        clear = arguments.get("clear_first", True)
        result = b.type_text(text, clear)
        return result

    # ── qa_type_unicode ──────────────────────────────────────────────────
    if name == "qa_type_unicode":
        text = arguments.get("text", "")
        clear = arguments.get("clear_first", True)
        result = b.type_unicode(text, clear)
        return result

    # ── qa_press_key ─────────────────────────────────────────────────────
    if name == "qa_press_key":
        key = arguments.get("key", "back")
        b.keyevent(key)
        return {"ok": True}

    # ── qa_set_clipboard ──────────────────────────────────────────────────
    if name == "qa_set_clipboard":
        text = arguments.get("text", "")
        return b.clipboard_set(text)

    # ── qa_get_clipboard ──────────────────────────────────────────────────
    if name == "qa_get_clipboard":
        content = b.clipboard_get()
        return {"content": content}

    # ── qa_find_element ──────────────────────────────────────────────────
    if name == "qa_find_element":
        text = arguments.get("text", "")
        resource_id = arguments.get("resource_id", "")
        el = b.find_element(text=text, resource_id=resource_id)
        if el is None:
            return {"found": False}
        return {"found": True, "text": el.text, "resource_id": el.resource_id,
                "center": f"{el.center_x},{el.center_y}", "clickable": el.clickable}

    # ── qa_element_state ──────────────────────────────────────────────────
    if name == "qa_element_state":
        text = arguments.get("text", "")
        resource_id = arguments.get("resource_id", "")
        state = b.element_state(text=text, resource_id=resource_id)
        if state is None:
            return {"found": False}
        return {"found": True, **state}

    # ── qa_get_text ──────────────────────────────────────────────────────
    if name == "qa_get_text":
        text = arguments.get("text", "")
        el = b.get_text(text=text)
        if el is None:
            return {"found": False}
        return {"found": True, "text": el.text, "center": f"{el.center_x},{el.center_y}"}

    # ── qa_wait_element ──────────────────────────────────────────────────
    if name == "qa_wait_element":
        text = arguments.get("text", "")
        timeout = arguments.get("timeout_s", 10)
        found = b.wait_element(text, timeout)
        return {"found": found, "text": text}

    # ── qa_scroll_find ───────────────────────────────────────────────────
    if name == "qa_scroll_find":
        text = arguments.get("text", "")
        direction = arguments.get("direction", "up")
        max_scrolls = arguments.get("max_scrolls", 10)
        scroll_pause = arguments.get("scroll_pause", 1.0)
        el = b.scroll_find(text, direction, max_scrolls, scroll_pause)
        if el is None:
            return {"found": False, "text": text}
        return {"found": True, "text": el.text, "center": f"{el.center_x},{el.center_y}"}

    # ── qa_notifications ──────────────────────────────────────────────────
    if name == "qa_notifications":
        action = arguments.get("action", "expand")
        b.notifications(action)
        return {"ok": True}

    # ── qa_shell ──────────────────────────────────────────────────────────
    if name == "qa_shell":
        command = arguments.get("command", "")
        timeout = arguments.get("timeout_s", 30)
        return b.shell(command, timeout)

    # ── qa_push_file ──────────────────────────────────────────────────────
    if name == "qa_push_file":
        local_path = arguments.get("local_path", "")
        device_path = arguments.get("device_path", "")
        ok = b.push_file(local_path, device_path)
        return {"ok": ok}

    # ── qa_pull_file ──────────────────────────────────────────────────────
    if name == "qa_pull_file":
        device_path = arguments.get("device_path", "")
        local_path = arguments.get("local_path", "")
        ok = b.pull_file(device_path, local_path)
        return {"ok": ok}

    # ── qa_measure_startup ────────────────────────────────────────────────
    if name == "qa_measure_startup":
        package = arguments.get("package", "")
        return b.measure_startup(package)

    # ── qa_dump_meminfo ───────────────────────────────────────────────────
    if name == "qa_dump_meminfo":
        package = arguments.get("package", "")
        return b.dump_meminfo(package)

    # ── qa_dump_gfxinfo ───────────────────────────────────────────────────
    if name == "qa_dump_gfxinfo":
        package = arguments.get("package", "")
        return b.dump_gfxinfo(package)

    # ── qa_logcat_start ───────────────────────────────────────────────────
    if name == "qa_logcat_start":
        watch_package = arguments.get("watch_package", "")
        path = b.logcat_start(watch_package)
        return {"log_file": path, "watching": watch_package}

    # ── qa_logcat_stop ────────────────────────────────────────────────────
    if name == "qa_logcat_stop":
        return b.logcat_stop()

    # ── qa_recording_start ────────────────────────────────────────────────
    if name == "qa_recording_start":
        filename = arguments.get("filename", "recording.mp4")
        path = b.recording_start(filename)
        return {"recording": True, "remote_path": path}

    # ── qa_recording_stop ─────────────────────────────────────────────────
    if name == "qa_recording_stop":
        path = b.recording_stop()
        return {"recording": False, "local_path": path}

    # ── qa_run_test ────────────────────────────────────────────────────────
    if name == "qa_run_test":
        test_plan_path = arguments.get("test_plan_path", "")
        skip_recording = arguments.get("skip_recording", True)
        # Run test plan via runner.py subprocess
        cmd = [sys.executable, os.path.join(SCRIPT_DIR, "runner.py"), test_plan_path]
        if skip_recording:
            cmd.append("--skip-recording")
        proc = subprocess.run(cmd, capture_output=True, timeout=300,
                              encoding="utf-8", errors="replace")
        return {"exit_code": proc.returncode, "stdout": proc.stdout[:2000], "stderr": proc.stderr[:500]}

    return {"error": f"Unknown tool: {name}"}


# ── Target Resolution ────────────────────────────────────────────────────
# Single unified function — eliminates the ~80 lines of duplication.

def _resolve_target(b: DeviceBackend, target: str) -> Optional[tuple[int, int]]:
    """Resolve a target string to (x, y) coordinates.

    Formats:
      "x,y"          → direct coordinates
      "text:Label"   → find by text, return center
      "resource:id"  → find by resource_id, return center
    """
    # Direct coordinates
    if "," in target and not target.startswith(("text:", "resource:")):
        try:
            parts = target.split(",")
            return int(parts[0]), int(parts[1])
        except ValueError:
            return None

    # Text-based lookup
    if target.startswith("text:"):
        text = target[5:]
        el = b.find_element(text=text)
        if el and el.center_x and el.center_y:
            return el.center_x, el.center_y
        return None

    # Resource ID lookup
    if target.startswith("resource:"):
        rid = target[9:]
        el = b.find_element(resource_id=rid)
        if el and el.center_x and el.center_y:
            return el.center_x, el.center_y
        return None

    # Fallback: try as bare text
    el = b.find_element(text=target)
    if el and el.center_x and el.center_y:
        return el.center_x, el.center_y
    return None


# ── Tool Definitions ─────────────────────────────────────────────────────

_TOOL_DEFINITIONS = [
    Tool(name="qa_connect", description="连接 Android 设备或模拟器。返回设备信息。",
         inputSchema={"type": "object", "properties": {"serial": {"type": "string", "description": "设备序列号（可选，默认自动发现）"}}}),
    Tool(name="qa_screenshot", description="截取当前屏幕截图。返回截图路径和 UI 元素列表。",
         inputSchema={"type": "object", "properties": {"name": {"type": "string", "description": "截图名称"}, "with_layout": {"type": "boolean", "description": "是否同时抓取 layout dump", "default": True}}}),
    Tool(name="qa_layout_dump", description="获取当前屏幕的 UI 布局树（JSON 格式），包含所有元素的文本、坐标、交互能力。",
         inputSchema={"type": "object", "properties": {}}),
    Tool(name="qa_tap", description="点击屏幕上的元素。支持文本查找、resource ID 查找、或直接坐标。",
         inputSchema={"type": "object", "properties": {"target": {"type": "string", "description": "点击目标：'text:文字'、'resource:id/xxx'、或 'x,y' 坐标"}}, "required": ["target"]}),
    Tool(name="qa_long_press", description="长按屏幕上的元素（用于打开上下文菜单）。支持文本查找或坐标。",
         inputSchema={"type": "object", "properties": {"target": {"type": "string", "description": "长按目标：'text:文字' 或 'x,y' 坐标"}, "duration_ms": {"type": "integer", "description": "长按时长（毫秒）", "default": 1000}}, "required": ["target"]}),
    Tool(name="qa_double_tap", description="双击屏幕元素。",
         inputSchema={"type": "object", "properties": {"target": {"type": "string", "description": "双击目标：'text:文字' 或 'x,y' 坐标"}}, "required": ["target"]}),
    Tool(name="qa_swipe", description="在屏幕上滑动。",
         inputSchema={"type": "object", "properties": {"direction": {"type": "string", "enum": ["up", "down", "left", "right"], "description": "滑动方向"}, "duration_ms": {"type": "integer", "description": "滑动时长（毫秒）", "default": 300}}, "required": ["direction"]}),
    Tool(name="qa_drag", description="拖拽操作：从一个位置拖到另一个位置。",
         inputSchema={"type": "object", "properties": {"from": {"type": "string", "description": "起点：'x,y' 坐标或 'text:文字'"}, "to": {"type": "string", "description": "终点：'x,y' 坐标或 'text:文字'"}, "duration_ms": {"type": "integer", "description": "拖拽时长（毫秒）", "default": 500}}, "required": ["from", "to"]}),
    Tool(name="qa_launch", description="启动指定应用。",
         inputSchema={"type": "object", "properties": {"package": {"type": "string", "description": "应用包名"}, "activity": {"type": "string", "description": "Activity 名称（可选）"}}, "required": ["package"]}),
    Tool(name="qa_logcat_start", description="开始捕获 logcat 日志，支持实时异常检测。",
         inputSchema={"type": "object", "properties": {"watch_package": {"type": "string", "description": "要监控的应用包名"}}}),
    Tool(name="qa_logcat_stop", description="停止 logcat 捕获，返回分析结果。",
         inputSchema={"type": "object", "properties": {}}),
    Tool(name="qa_recording_start", description="开始屏幕录制。",
         inputSchema={"type": "object", "properties": {"filename": {"type": "string", "description": "录屏文件名", "default": "recording.mp4"}}}),
    Tool(name="qa_recording_stop", description="停止屏幕录制，返回录屏文件路径。",
         inputSchema={"type": "object", "properties": {}}),
    Tool(name="qa_run_test", description="运行完整的 Android 测试。输入 test_plan.json 路径，自动执行所有场景并生成报告。",
         inputSchema={"type": "object", "properties": {"test_plan_path": {"type": "string", "description": "测试计划文件路径"}, "skip_recording": {"type": "boolean", "description": "是否跳过录屏", "default": True}}, "required": ["test_plan_path"]}),
    Tool(name="qa_find_element", description="在当前 UI 中查找元素，返回坐标和属性。不执行点击。",
         inputSchema={"type": "object", "properties": {"text": {"type": "string", "description": "要查找的文本"}, "resource_id": {"type": "string", "description": "要查找的 resource ID"}}}),
    Tool(name="qa_check_app_alive", description="检查指定应用是否在前台运行。用于验证操作是否导致 crash。",
         inputSchema={"type": "object", "properties": {"package": {"type": "string", "description": "应用包名"}}, "required": ["package"]}),
    Tool(name="qa_type", description="在当前焦点输入框中输入文本。先点击输入框获得焦点，再输入。",
         inputSchema={"type": "object", "properties": {"text": {"type": "string", "description": "要输入的文本"}, "clear_first": {"type": "boolean", "description": "是否先清空输入框", "default": True}}, "required": ["text"]}),
    Tool(name="qa_type_unicode", description="输入 Unicode 文本（支持中文等非 ASCII 字符）。通过剪贴板粘贴实现。",
         inputSchema={"type": "object", "properties": {"text": {"type": "string", "description": "要输入的文本（支持中文）"}, "clear_first": {"type": "boolean", "description": "是否先清空输入框", "default": True}}, "required": ["text"]}),
    Tool(name="qa_press_key", description="按下系统按键（返回、主页、最近任务等）。",
         inputSchema={"type": "object", "properties": {"key": {"type": "string", "enum": ["back", "home", "recent", "enter", "tab", "delete"], "description": "按键名称"}}, "required": ["key"]}),
    Tool(name="qa_wait_element", description="等待某个文本元素出现在屏幕上。超时返回未找到。",
         inputSchema={"type": "object", "properties": {"text": {"type": "string", "description": "要等待出现的文本"}, "timeout_s": {"type": "integer", "description": "超时秒数", "default": 10}}, "required": ["text"]}),
    Tool(name="qa_get_text", description="获取屏幕上指定区域的文本内容（通过布局 dump）。",
         inputSchema={"type": "object", "properties": {"text": {"type": "string", "description": "要查找的文本（返回包含该文本的元素信息）"}}}),
    Tool(name="qa_scroll_find", description="滚动屏幕直到找到指定文本元素。返回是否找到及坐标。",
         inputSchema={"type": "object", "properties": {"text": {"type": "string", "description": "要查找的文本"}, "direction": {"type": "string", "enum": ["up", "down"], "description": "滚动方向（默认 up=向下滚动内容）", "default": "up"}, "max_scrolls": {"type": "integer", "description": "最大滚动次数", "default": 10}, "scroll_pause": {"type": "number", "description": "每次滚动后等待秒数", "default": 1.0}}, "required": ["text"]}),
    Tool(name="qa_element_state", description="检查 UI 元素的状态属性（enabled/checked/selected/scrollable/focusable）。返回元素详细状态。",
         inputSchema={"type": "object", "properties": {"text": {"type": "string", "description": "按文本查找元素"}, "resource_id": {"type": "string", "description": "按 resource ID 查找元素"}}}),
    Tool(name="qa_set_clipboard", description="设置设备剪贴板文本。",
         inputSchema={"type": "object", "properties": {"text": {"type": "string", "description": "要设置的文本"}}, "required": ["text"]}),
    Tool(name="qa_get_clipboard", description="获取设备剪贴板文本。",
         inputSchema={"type": "object", "properties": {}}),
    Tool(name="qa_notifications", description="展开或收起通知栏。",
         inputSchema={"type": "object", "properties": {"action": {"type": "string", "enum": ["expand", "collapse"], "description": "操作：expand 或 collapse"}}, "required": ["action"]}),
    Tool(name="qa_shell", description="在设备上执行任意 ADB shell 命令。高级用法，慎用。",
         inputSchema={"type": "object", "properties": {"command": {"type": "string", "description": "要执行的 shell 命令"}, "timeout_s": {"type": "integer", "description": "超时秒数", "default": 30}}, "required": ["command"]}),
    Tool(name="qa_push_file", description="推送本地文件到设备存储。",
         inputSchema={"type": "object", "properties": {"local_path": {"type": "string", "description": "本地文件路径"}, "device_path": {"type": "string", "description": "设备目标路径（如 /sdcard/Download/）"}}, "required": ["local_path", "device_path"]}),
    Tool(name="qa_pull_file", description="从设备拉取文件到本地。",
         inputSchema={"type": "object", "properties": {"device_path": {"type": "string", "description": "设备文件路径"}, "local_path": {"type": "string", "description": "本地保存路径"}}, "required": ["device_path", "local_path"]}),
    Tool(name="qa_measure_startup", description="测量应用冷启动时间。返回 TotalTime/WaitTime/ThisTime（毫秒）。",
         inputSchema={"type": "object", "properties": {"package": {"type": "string", "description": "应用包名"}}, "required": ["package"]}),
    Tool(name="qa_dump_meminfo", description="获取应用内存使用信息（PSS/RSS）。",
         inputSchema={"type": "object", "properties": {"package": {"type": "string", "description": "应用包名"}}, "required": ["package"]}),
    Tool(name="qa_dump_gfxinfo", description="获取应用帧渲染信息（总帧数/卡顿帧/百分位延迟）。",
         inputSchema={"type": "object", "properties": {"package": {"type": "string", "description": "应用包名"}}, "required": ["package"]}),
    Tool(name="qa_analyze_screenshot", description="使用 AI 视觉模型分析截图。检测布局问题、文字截断、组件缺陷等。使用 gemma-4-31b-it 模型。",
         inputSchema={"type": "object", "properties": {"image_path": {"type": "string", "description": "截图文件路径"}, "prompt": {"type": "string", "description": "自定义分析提示词（可选，默认使用内置 UI 检查清单）"}}, "required": ["image_path"]}),
    Tool(name="qa_analyze_video", description="使用 AI 视觉模型分析测试录屏。检测动画流畅度、交互响应、转场质量等。使用 gemini-3.1-flash-lite 模型。",
         inputSchema={"type": "object", "properties": {"video_path": {"type": "string", "description": "录屏文件路径"}, "prompt": {"type": "string", "description": "自定义分析提示词（可选，默认使用内置交互检查清单）"}}, "required": ["video_path"]}),
    Tool(name="qa_analyze_logcat", description="使用 AI 模型分析 logcat 日志。检测崩溃、ANR、性能问题等。使用 gemma-4-31b-it 模型。",
         inputSchema={"type": "object", "properties": {"logcat_path": {"type": "string", "description": "logcat 日志文件路径"}, "prompt": {"type": "string", "description": "自定义分析提示词（可选，默认使用内置稳定性检查清单）"}}, "required": ["logcat_path"]}),
]

# qa_run_test needs subprocess — import it only when used
import subprocess  # noqa: E402 — delayed import for the single tool that needs it


# ── Entry Point ──────────────────────────────────────────────────────────

async def main():
    async with stdio_server() as (read_stream, write_stream):
        await server.run(read_stream, write_stream, server.create_initialization_options())


def run():
    asyncio.run(main())


if __name__ == "__main__":
    run()
