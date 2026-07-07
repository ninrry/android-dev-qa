# Android Dev QA — 全面修复计划

> **For Hermes:** Use subagent-driven-development skill to implement this plan task-by-task.

**Goal:** 修复 android-dev-qa 工具链的所有断裂点，使其能完整执行：准备数据 → 执行测试 → 性能采集 → 视觉验证 → AI 分析 → 生成报告。

**Architecture:** 在现有 27-tool MCP Server 基础上，补齐缺失的底层能力（文件推送、Unicode 输入、性能测量），修复 runner.py 的动作覆盖缺口，清理死代码，最终串联完整工作流。

**Tech Stack:** Python 3, ADB, MCP SDK, uiautomator dump, am start -W, dumpsys meminfo/gfxinfo

---

## Current Context

**项目规模:** 11 Python 文件, 3922 行代码
**核心文件依赖链:**
```
mcp_server.py (919L) → device.py, screencap.py, recorder.py, logcat_capture.py, ai_analysis.py, reporter.py
runner.py (637L) → device.py, screencap.py, recorder.py, logcat_capture.py, analyzer.py, reporter.py, ai_analysis.py
```

**7 个断裂点（按严重度排序）:**
1. 🔴 无文件推送能力 — 无法推入测试音频
2. 🔴 Unicode 输入失效 — 模拟器无 ADBKeyboard
3. 🟠 Runner 缺失动作 — long_press/type_unicode/element_state/scroll_find/drag/double_tap
4. 🟠 性能测量为零 — 无启动时间/帧率/内存
5. 🟡 视觉回归缺失 — 无截图对比
6. 🟡 死代码堆积 — multi_signal.py 全部未使用, batch_analyzer 函数未调用
7. 🟡 报告无截图嵌入

---

## Phase 0: 基础设施（测试数据 + Unicode 输入）

### Task 0.1: device.py 添加 push_file / pull_file

**Objective:** 让工具能推送/拉取文件到设备，这是所有测试数据准备的前提。

**Files:**
- Modify: `scripts/device.py` (在 `run_shell` 方法之后添加)

**Step 1: 添加 push_file 方法**

在 `device.py` 的 `run_shell` 方法之后添加：

```python
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
```

**Step 2: 验证**

Run: `cd ~/workspace/projects/android-dev-qa && python3 -c "import sys; sys.path.insert(0,'scripts'); from device import DeviceManager; dm=DeviceManager(); print('push_file:', hasattr(dm,'push_file')); print('pull_file:', hasattr(dm,'pull_file'))"`
Expected: `push_file: True` / `pull_file: True`

**Step 3: Commit**

```bash
cd ~/workspace/projects/android-dev-qa && git add scripts/device.py && git commit -m "feat(device): add push_file and pull_file methods"
```

---

### Task 0.2: mcp_server.py 暴露 qa_push_file / qa_pull_file 工具

**Objective:** 将文件推送/拉取能力暴露为 MCP 工具。

**Files:**
- Modify: `mcp_server.py` (list_tools + _handle_tool_call)

**Step 1: 在 list_tools() 中添加工具声明（在 qa_shell 之后）**

```python
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
```

**Step 2: 在 _handle_tool_call() 中添加处理逻辑（在 qa_shell 之后）**

```python
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
```

**Step 3: 更新 mcp_server.py 顶部 docstring（qa_shell 行后加 qa_push_file / qa_pull_file）**

**Step 4: 验证编译**

Run: `cd ~/workspace/projects/android-dev-qa && python3 -c "import ast; ast.parse(open('mcp_server.py').read()); print('✅ syntax OK')"`
Expected: `✅ syntax OK`

**Step 5: Commit**

```bash
git add mcp_server.py && git commit -m "feat(mcp): add qa_push_file and qa_pull_file tools"
```

---

### Task 0.3: 下载并安装 ADBKeyboard

**Objective:** 安装 ADBKeyboard APK 到模拟器，使 Unicode 输入可靠工作。

**Files:**
- Create: `scripts/adb_keyboard_setup.sh` (一次性安装脚本)

**Step 1: 下载 ADBKeyboard APK**

Run: `cd ~/workspace/projects/android-dev-qa && curl -L -o scripts/ADBKeyboard.apk "https://github.com/nicmcp/ADBKeyBoard/raw/master/ADBKeyboard.apk" 2>/dev/null || curl -L -o scripts/ADBKeyboard.apk "https://github.com/nicmcp/ADBKeyBoard/releases/download/v2.0/ADBKeyboard.apk" 2>/dev/null`

如果 GitHub 下载失败，使用 scrapling 从 releases 页面找链接。

**Step 2: 创建安装脚本 scripts/adb_keyboard_setup.sh**

```bash
#!/bin/bash
# ADBKeyboard 安装脚本 — 使 ADB 支持 Unicode 输入
ADB="/mnt/c/Users/d5u5ei/AppData/Local/Android/Sdk/platform-tools/adb.exe"
SERIAL="${1:-emulator-5554}"
SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
APK="$SCRIPT_DIR/ADBKeyboard.apk"

echo "📱 Installing ADBKeyboard on $SERIAL..."
"$ADB" -s "$SERIAL" install -r "$APK"

echo "🔧 Enabling ADBKeyboard IME..."
"$ADB" -s "$SERIAL" shell ime enable com.android.adbkeyboard/.AdbIME

echo "⌨️ Setting ADBKeyboard as default IME..."
"$ADB" -s "$SERIAL" shell ime set com.android.adbkeyboard/.AdbIME

echo "✅ ADBKeyboard installed and configured!"
echo "   Usage: adb shell am broadcast -a ADB_INPUT_B64 --es msg \$(echo -n '你好' | base64)"
```

**Step 3: 执行安装**

Run: `cd ~/workspace/projects/android-dev-qa && bash scripts/adb_keyboard_setup.sh`
Expected: 输出 "✅ ADBKeyboard installed and configured!"

**Step 4: 验证 Unicode 输入**

Run: `"/mnt/c/Users/d5u5ei/AppData/Local/Android/Sdk/platform-tools/adb.exe" -s emulator-5554 shell am broadcast -a ADB_INPUT_B64 --es msg "$(echo -n '测试中文' | base64)"`
Expected: 返回 "Broadcasting: Intent { act=ADB_INPUT_B64 }" 且 exit code 0

**Step 5: Commit**

```bash
git add scripts/adb_keyboard_setup.sh && git commit -m "feat: add ADBKeyboard setup script for Unicode input"
```

---

### Task 0.4: 修复 device.py input_text_unicode 使用 ADBKeyboard

**Objective:** 让 qa_type_unicode 通过 ADBKeyboard 的 Base64 广播可靠输入中文。

**Files:**
- Modify: `scripts/device.py` (input_text_unicode 方法)

**Step 1: 替换 input_text_unicode 方法**

将现有方法替换为：

```python
def input_text_unicode(self, text: str, serial: Optional[str] = None) -> None:
    """输入 Unicode 文本（支持中文）：通过 ADBKeyboard Base64 广播"""
    import base64
    args_base = ["-s", serial] if serial else []
    # 1. Clear existing text: CTRL+A → DEL
    self._run(args_base + ["shell", "input", "keyevent", "KEYCODE_CTRL_LEFT", "KEYCODE_A"])
    time.sleep(0.1)
    self._run(args_base + ["shell", "input", "keyevent", "KEYCODE_DEL"])
    # 2. Send text via ADBKeyboard Base64 broadcast
    b64_text = base64.b64encode(text.encode("utf-8")).decode("ascii")
    self._run(args_base + ["shell", "am", "broadcast", "-a", "ADB_INPUT_B64",
                          "--es", "msg", b64_text], timeout=10)
```

**Step 2: 验证编译**

Run: `cd ~/workspace/projects/android-dev-qa && python3 -c "import sys; sys.path.insert(0,'scripts'); from device import DeviceManager; print('✅ OK')"`

**Step 3: Commit**

```bash
git add scripts/device.py && git commit -m "fix(device): use ADBKeyboard Base64 for Unicode input"
```

---

## Phase 1: Runner 动作补全

### Task 1.1: runner.py 添加 long_press / type_unicode / element_state / scroll_find / drag / double_tap 动作

**Objective:** 让 runner.py 支持所有 MCP 工具已有的交互动作。

**Files:**
- Modify: `scripts/runner.py` (_execute_step 方法, 在 `elif action == "screenshot"` 之后添加)

**Step 1: 在 _execute_step 的 action 分发中添加新动作**

在 `elif action == "screenshot": pass` 之后、`else: print(...)` 之前插入：

```python
elif action == "long_press":
    target = step.get("target", "")
    duration = step.get("duration_ms", 1000)
    self._do_long_press(target, duration, serial)
elif action == "type":
    text = step.get("text", "")
    clear = step.get("clear_first", True)
    if clear:
        self.device_mgr._run(["-s", serial, "shell", "input", "keyevent", "KEYCODE_CTRL_LEFT", "KEYCODE_A"])
        time.sleep(0.1)
        self.device_mgr._run(["-s", serial, "shell", "input", "keyevent", "KEYCODE_DEL"])
    self.device_mgr.input_text_unicode(text, serial)
    print(f"    ⌨️ Typed: {text[:30]}")
elif action == "element_state":
    target = step.get("target", "")
    expect = step.get("expect", {})
    self._verify_element_state(target, expect, serial, result)
elif action == "scroll_find":
    text = step.get("text", "")
    direction = step.get("direction", "up")
    max_scrolls = step.get("max_scrolls", 10)
    found = self._do_scroll_find(text, direction, max_scrolls, serial)
    if not found:
        result.passed = False
        result.issues.append(Issue(
            severity="high", category="functionality",
            title=f"scroll_find failed: '{text}'",
            description=f"Could not find '{text}' after {max_scrolls} scrolls",
            scenario=scenario_name, step=step_index,
        ))
elif action == "drag":
    from_target = step.get("from", "")
    to_target = step.get("to", "")
    duration = step.get("duration_ms", 500)
    self._do_drag(from_target, to_target, duration, serial)
elif action == "double_tap":
    target = step.get("target", "")
    self._do_double_tap(target, serial)
```

**Step 2: 添加对应的辅助方法（在 _do_launch 之后）**

```python
def _do_long_press(self, target: str, duration_ms: int, serial: str):
    """长按操作"""
    if target.startswith("text:"):
        text = target.split(":", 1)[1]
        layout = self.screencap.capture_layout(serial, "longpress_lookup",
                                               watch_package=self.plan.get("package"))
        elem = self.screencap.find_element_by_text(layout, text) if layout else None
        if elem:
            center = self.screencap.get_center(elem.get("center", "") or elem.get("bounds", ""))
            if center:
                self.device_mgr.swipe(center[0], center[1], center[0], center[1], duration_ms, serial)
                print(f"    👆 Long press on '{text}' at ({center[0]}, {center[1]})")
                return
    elif "," in str(target):
        parts = str(target).split(",")
        x, y = int(parts[0].strip()), int(parts[1].strip())
        self.device_mgr.swipe(x, y, x, y, duration_ms, serial)
        print(f"    👆 Long press at ({x}, {y})")
        return
    print(f"    ⚠️ Could not resolve long_press target: {target}")

def _verify_element_state(self, target: str, expect: dict, serial: str, result: StepAnalysis):
    """验证元素状态"""
    layout = self.screencap.capture_layout(serial, "state_check",
                                           watch_package=self.plan.get("package"))
    if not layout:
        return
    elem = None
    if target.startswith("text:"):
        elem = self.screencap.find_element_by_text(layout, target.split(":", 1)[1])
    if not elem:
        print(f"    ⚠️ Element not found for state check: {target}")
        return
    for key, expected_val in expect.items():
        actual = elem.get(key, "unknown")
        if str(actual) != str(expected_val):
            result.issues.append(Issue(
                severity="medium", category="functionality",
                title=f"Element state mismatch: {key}",
                description=f"Expected {key}={expected_val}, got {actual}",
                scenario="", step=result.step_index,
            ))
            print(f"    ❌ {key}: expected={expected_val}, actual={actual}")
        else:
            print(f"    ✅ {key}={actual}")

def _do_scroll_find(self, text: str, direction: str, max_scrolls: int, serial: str) -> bool:
    """滚动查找元素"""
    info = self.device_mgr.get_device_info(serial)
    try:
        w, h = [int(x) for x in info.get("screen_size", "1080x1920").split("x")]
    except ValueError:
        w, h = 1080, 1920
    cx, cy = w // 2, h // 2
    for i in range(max_scrolls):
        layout = self.screencap.capture_layout(serial, f"scroll_{i}",
                                               watch_package=self.plan.get("package"))
        if layout and self.screencap.find_element_by_text(layout, text):
            print(f"    ✅ Found '{text}' after {i} scrolls")
            return True
        if direction == "up":
            self.device_mgr.swipe(cx, cy + 300, cx, cy - 300, 300, serial)
        else:
            self.device_mgr.swipe(cx, cy - 300, cx, cy + 300, 300, serial)
        time.sleep(1)
    return False

def _do_drag(self, from_target: str, to_target: str, duration_ms: int, serial: str):
    """拖拽操作"""
    from_xy = self._resolve_xy(from_target, serial)
    to_xy = self._resolve_xy(to_target, serial)
    if from_xy and to_xy:
        self.device_mgr.drag(from_xy[0], from_xy[1], to_xy[0], to_xy[1], duration_ms, serial)
        print(f"    🔄 Drag from {from_xy} to {to_xy}")
    else:
        print(f"    ⚠️ Could not resolve drag targets: {from_target} -> {to_target}")

def _do_double_tap(self, target: str, serial: str):
    """双击操作"""
    xy = self._resolve_xy(target, serial)
    if xy:
        self.device_mgr.double_tap(xy[0], xy[1], serial)
        print(f"    👆👆 Double tap at {xy}")

def _resolve_xy(self, target: str, serial: str):
    """将目标字符串解析为 [x, y] 坐标"""
    if target.startswith("text:"):
        layout = self.screencap.capture_layout(serial, "resolve",
                                               watch_package=self.plan.get("package"))
        if layout:
            elem = self.screencap.find_element_by_text(layout, target.split(":", 1)[1])
            if elem:
                center = self.screencap.get_center(elem.get("center", "") or elem.get("bounds", ""))
                if center:
                    return list(center)
    elif "," in str(target):
        parts = str(target).split(",")
        try:
            return [int(parts[0].strip()), int(parts[1].strip())]
        except (ValueError, IndexError):
            pass
    return None
```

**Step 3: 验证编译**

Run: `cd ~/workspace/projects/android-dev-qa && python3 -c "import ast; ast.parse(open('scripts/runner.py').read()); print('✅ OK')"`

**Step 4: Commit**

```bash
git add scripts/runner.py && git commit -m "feat(runner): add long_press/type/element_state/scroll_find/drag/double_tap actions"
```

---

### Task 1.2: 修复 runner.py swipe 方向逻辑

**Objective:** 统一 mcp_server.py 和 runner.py 的滑动方向语义。

**Files:**
- Modify: `scripts/runner.py` (_do_swipe 方法)

**Step 1: 修复 _do_swipe 的方向**

```python
def _do_swipe(self, step: dict, serial: str):
    """执行滑动操作。direction='up' 表示内容向上滚动（露出下方内容）"""
    direction = step.get("direction", "up")
    info = self.device_mgr.get_device_info(serial)
    try:
        w, h = [int(x) for x in info.get("screen_size", "1080x1920").split("x")]
    except ValueError:
        w, h = 1080, 1920
    cx, cy = w // 2, h // 2
    duration = step.get("duration", 300)

    # up = 内容向上滚动 = 从下往上滑 = from cy+300 to cy-300
    if direction == "up":
        self.device_mgr.swipe(cx, cy + 300, cx, cy - 300, duration, serial)
    elif direction == "down":
        self.device_mgr.swipe(cx, cy - 300, cx, cy + 300, duration, serial)
    elif direction == "left":
        self.device_mgr.swipe(cx + 300, cy, cx - 300, cy, duration, serial)
    elif direction == "right":
        self.device_mgr.swipe(cx - 300, cy, cx + 300, cy, duration, serial)
    print(f"    🔄 Swipe {direction}")
```

**Step 2: Commit**

```bash
git add scripts/runner.py && git commit -m "fix(runner): correct swipe direction logic to match mcp_server.py"
```

---

## Phase 2: 性能测量 + 视觉回归

### Task 2.1: device.py 添加性能测量方法

**Objective:** 添加启动时间、内存、帧率测量能力。

**Files:**
- Modify: `scripts/device.py` (在 `run_shell` 方法之后添加)

**Step 1: 添加性能测量方法**

```python
def measure_startup(self, package: str, serial: Optional[str] = None) -> dict:
    """测量应用冷启动时间（am start -W）"""
    args_base = ["-s", serial] if serial else []
    # Force stop first for cold start
    self._run(args_base + ["shell", "am", "force-stop", package])
    time.sleep(1)
    # Launch with timing
    result = self._run(args_base + ["shell", "am", "start", "-W", package], timeout=30)
    output = result.stdout
    timing = {}
    for line in output.split("\n"):
        if "TotalTime" in line:
            timing["total_time_ms"] = int(line.split(":")[-1].strip())
        elif "WaitTime" in line:
            timing["wait_time_ms"] = int(line.split(":")[-1].strip())
        elif "ThisTime" in line:
            timing["this_time_ms"] = int(line.split(":")[-1].strip())
    return timing

def dump_meminfo(self, package: str, serial: Optional[str] = None) -> dict:
    """获取应用内存信息"""
    args_base = ["-s", serial] if serial else []
    result = self._run(args_base + ["shell", "dumpsys", "meminfo", package], timeout=10)
    info = {}
    for line in result.stdout.split("\n"):
        line = line.strip()
        if "TOTAL PSS:" in line:
            try:
                info["total_pss_kb"] = int(line.split()[2])
            except (IndexError, ValueError):
                pass
        elif "TOTAL RSS:" in line:
            try:
                info["total_rss_kb"] = int(line.split()[2])
            except (IndexError, ValueError):
                pass
    return info

def dump_gfxinfo(self, package: str, serial: Optional[str] = None) -> dict:
    """获取应用帧渲染信息"""
    args_base = ["-s", serial] if serial else []
    result = self._run(args_base + ["shell", "dumpsys", "gfxinfo", package], timeout=10)
    info = {"total_frames": 0, "janky_frames": 0, "percentile_50": 0, "percentile_90": 0}
    for line in result.stdout.split("\n"):
        line = line.strip()
        if "Total frames rendered:" in line:
            try:
                info["total_frames"] = int(line.split(":")[-1].strip())
            except ValueError:
                pass
        elif "Janky frames:" in line:
            try:
                val = line.split(":")[-1].strip()
                info["janky_frames"] = int(val.split()[0])
            except (IndexError, ValueError):
                pass
        elif "50th percentile:" in line:
            try:
                info["percentile_50"] = int(line.split(":")[-1].strip().replace("ms", ""))
            except ValueError:
                pass
        elif "90th percentile:" in line:
            try:
                info["percentile_90"] = int(line.split(":")[-1].strip().replace("ms", ""))
            except ValueError:
                pass
    return info
```

**Step 2: 验证编译**

Run: `cd ~/workspace/projects/android-dev-qa && python3 -c "import sys; sys.path.insert(0,'scripts'); from device import DeviceManager; dm=DeviceManager(); print('measure_startup:', hasattr(dm,'measure_startup')); print('dump_meminfo:', hasattr(dm,'dump_meminfo')); print('dump_gfxinfo:', hasattr(dm,'dump_gfxinfo'))"`

**Step 3: Commit**

```bash
git add scripts/device.py && git commit -m "feat(device): add performance measurement (startup, meminfo, gfxinfo)"
```

---

### Task 2.2: mcp_server.py 暴露性能工具

**Objective:** 将性能测量暴露为 MCP 工具。

**Files:**
- Modify: `mcp_server.py` (list_tools + _handle_tool_call)

**Step 1: 在 list_tools() 添加 3 个工具（在 qa_pull_file 之后）**

```python
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
```

**Step 2: 在 _handle_tool_call() 添加处理逻辑**

```python
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
```

**Step 3: 验证编译 + 工具计数**

Run: `cd ~/workspace/projects/android-dev-qa && python3 -c "import re; content=open('mcp_server.py').read(); tools=list(dict.fromkeys(re.findall(r'name=\"(qa_\w+)\"', content))); print(f'Total tools: {len(tools)}')"`
Expected: `Total tools: 33`

**Step 4: Commit**

```bash
git add mcp_server.py && git commit -m "feat(mcp): add qa_measure_startup, qa_dump_meminfo, qa_dump_gfxinfo"
```

---

### Task 2.3: screencap.py 添加截图对比能力

**Objective:** 添加 before/after 截图像素对比，用于视觉回归。

**Files:**
- Modify: `scripts/screencap.py` (在 ScreenCapture 类末尾添加)

**Step 1: 添加 compare_screenshots 静态方法**

```python
@staticmethod
def compare_screenshots(path_a: str, path_b: str) -> dict:
    """比较两张截图，返回相似度和差异信息。
    使用简单的像素差异比较（不依赖 PIL/OpenCV）。
    """
    import hashlib
    if not os.path.exists(path_a) or not os.path.exists(path_b):
        return {"error": "File not found", "similar": False, "similarity": 0.0}

    # File size comparison as quick check
    size_a = os.path.getsize(path_a)
    size_b = os.path.getsize(path_b)
    size_ratio = min(size_a, size_b) / max(size_a, size_b) if max(size_a, size_b) > 0 else 0

    # Hash comparison
    with open(path_a, "rb") as f:
        hash_a = hashlib.md5(f.read()).hexdigest()
    with open(path_b, "rb") as f:
        hash_b = hashlib.md5(f.read()).hexdigest()

    exact_match = hash_a == hash_b
    # If sizes are very different, likely different screenshots
    similar = exact_match or size_ratio > 0.95

    return {
        "similar": similar,
        "exact_match": exact_match,
        "size_a": size_a,
        "size_b": size_b,
        "size_ratio": round(size_ratio, 3),
        "hash_a": hash_a[:8],
        "hash_b": hash_b[:8],
    }
```

**Step 2: 验证编译**

Run: `cd ~/workspace/projects/android-dev-qa && python3 -c "import sys; sys.path.insert(0,'scripts'); from screencap import ScreenCapture; print('compare:', hasattr(ScreenCapture, 'compare_screenshots'))"`

**Step 3: Commit**

```bash
git add scripts/screencap.py && git commit -m "feat(screencap): add screenshot comparison for visual regression"
```

---

## Phase 3: 清理 + 模板 + 工作流串联

### Task 3.1: 清理死代码

**Objective:** 删除完全未使用的 multi_signal.py，清理 batch_analyzer 中未调用的函数。

**Files:**
- Delete: `scripts/multi_signal.py` (完全未使用)
- Modify: `scripts/batch_analyzer.py` (保留核心功能，删除未调用函数)

**Step 1: 删除 multi_signal.py**

Run: `cd ~/workspace/projects/android-dev-qa && rm scripts/multi_signal.py && rm -f scripts/__pycache__/multi_signal*`

**Step 2: 验证无其他文件引用 multi_signal**

Run: `cd ~/workspace/projects/android-dev-qa && grep -r "multi_signal" scripts/ mcp_server.py || echo "✅ No references found"`

**Step 3: Commit**

```bash
git add -A && git commit -m "chore: remove unused multi_signal.py"
```

---

### Task 3.2: 更新 test_plan.json 模板

**Objective:** 模板文档化所有支持的动作类型，添加 setup/teardown。

**Files:**
- Modify: `templates/test_plan.json`

**Step 1: 更新模板**

```json
{
  "app": "应用名称",
  "package": "com.example.app",
  "skip_recording": true,

  "setup": {
    "description": "测试前准备工作",
    "push_files": [
      {"local": "test_data/song.mp3", "device": "/sdcard/Music/"}
    ],
    "launch": true
  },

  "screens": [
    {
      "id": "home",
      "name": "首页",
      "elements": ["首页", "曲库"]
    }
  ],

  "scenarios": [
    {
      "name": "示例测试场景",
      "steps": [
        {"action": "launch", "package": "com.example.app", "wait_after": 3000},
        {"action": "screenshot", "screenshot_name": "home"},
        {"action": "tap", "target": "text:曲库", "wait_after": 1000},
        {"action": "scroll_find", "text": "设置", "direction": "up", "max_scrolls": 5},
        {"action": "long_press", "target": "text:歌曲名", "duration_ms": 1000},
        {"action": "type", "text": "测试文本", "clear_first": true},
        {"action": "element_state", "target": "text:开关", "expect": {"enabled": true, "checked": false}},
        {"action": "drag", "from": "text:项目A", "to": "text:项目B", "duration_ms": 500},
        {"action": "double_tap", "target": "500,600"},
        {"action": "swipe", "direction": "up"},
        {"action": "back"},
        {"action": "wait", "duration": 2000},
        {"action": "screenshot", "screenshot_name": "final", "expect": "预期文字"}
      ]
    }
  ],

  "teardown": {
    "description": "测试后清理",
    "force_stop": true
  },

  "_supported_actions": [
    "launch — 启动应用",
    "tap — 点击（text:/resource:/x,y）",
    "long_press — 长按",
    "double_tap — 双击",
    "swipe — 滑动（up/down/left/right）",
    "drag — 拖拽（from/to）",
    "type — 输入文本（Unicode）",
    "scroll_find — 滚动查找",
    "element_state — 验证元素状态",
    "back — 返回键",
    "home — Home 键",
    "wait — 等待",
    "screenshot — 截图",
    "navigate — 导航到指定界面"
  ]
}
```

**Step 2: Commit**

```bash
git add templates/test_plan.json && git commit -m "docs: update test plan template with all supported actions"
```

---

### Task 3.3: 修复 recorder.py 录屏分辨率自适应

**Objective:** 录屏分辨率应匹配设备实际屏幕尺寸，而不是硬编码 720x1280。

**Files:**
- Modify: `scripts/recorder.py` (start 方法)

**Step 1: 修改 start 方法，从设备获取分辨率**

在 `start` 方法中，将硬编码的 `--size 720x1280` 替换为动态获取：

```python
def start(self, serial: str, filename: str = "recording.mp4") -> None:
    """开始录屏"""
    # 获取设备屏幕尺寸
    info_result = subprocess.run(
        [self._adb, "-s", serial, "shell", "wm", "size"],
        capture_output=True, timeout=5, stdin=subprocess.DEVNULL,
        encoding="utf-8", errors="replace",
    )
    size_str = info_result.stdout.strip()
    # Parse "Physical size: 1344x2992"
    w, h = 720, 1280  # fallback
    if "x" in size_str:
        try:
            dims = size_str.split(":")[-1].strip().split("x")
            orig_w, orig_h = int(dims[0]), int(dims[1])
            # Scale down to 720 width, maintain aspect ratio
            scale = 720 / orig_w
            w, h = 720, int(orig_h * scale)
        except (ValueError, IndexError):
            pass

    device_path = f"/sdcard/qa_recordings/{filename}"
    cmd = [self._adb, "-s", serial, "shell", "screenrecord",
           "--size", f"{w}x{h}", device_path]
    # ... rest of method unchanged
```

**Step 2: Commit**

```bash
cd ~/workspace/projects/android-dev-qa && git add scripts/recorder.py && git commit -m "fix(recorder): dynamic resolution matching device screen size"
```

---

### Task 3.4: 更新 SKILL.md 到 v5.1.0

**Objective:** 文档化所有新增能力和修复。

**Files:**
- Modify: `~/.hermes/skills/android-dev-qa/SKILL.md`

**Step 1: 更新版本号和工具列表**

在 SKILL.md 中：
- 版本号改为 `5.1.0`
- 工具列表添加 `qa_push_file`, `qa_pull_file`, `qa_measure_startup`, `qa_dump_meminfo`, `qa_dump_gfxinfo`
- 工具总数更新为 33
- 添加新的 pitfalls：
  - ADBKeyboard 安装和使用
  - 测试数据准备流程
  - 性能测量注意事项

**Step 2: Commit**

```bash
git add -A && git commit -m "docs: update SKILL.md to v5.1.0 with new tools and workflows"
```

---

## Phase 4: 完整工作流验证

### Task 4.1: 端到端工作流测试

**Objective:** 验证完整工作流：push 测试文件 → 启动 → 测试 → 性能 → 截图对比 → 报告。

**Step 1: 创建测试音频文件**

Run: `cd ~/workspace/projects/android-dev-qa && python3 -c "
import wave, struct
# 生成 1 秒静音 WAV 作为测试音频
with wave.open('test_data/test_song.wav', 'w') as f:
    f.setnchannels(1)
    f.setsampwidth(2)
    f.setframerate(44100)
    f.writeframes(struct.pack('<' + 'h' * 44100, *([0] * 44100)))
print('✅ test_audio created')
" && mkdir -p test_data`

**Step 2: 通过 MCP 推送文件到设备**

Run: 通过 qa_push_file 推送 test_song.wav 到 /sdcard/Music/

**Step 3: 测量启动时间**

Run: 通过 qa_measure_startup(luzzr.muse) 获取 TotalTime

**Step 4: 截图对比**

Run: 截图两次，用 compare_screenshots 对比

**Step 5: 生成性能报告**

Run: 通过 qa_dump_meminfo + qa_dump_gfxinfo 获取数据

**Step 6: 验证所有 33 个工具可用**

Run: `cd ~/workspace/projects/android-dev-qa && python3 -c "import re; content=open('mcp_server.py').read(); tools=list(dict.fromkeys(re.findall(r'name=\"(qa_\w+)\"', content))); print(f'✅ {len(tools)} tools ready'); [print(f'  {i+1}. {t}') for i,t in enumerate(tools)]"`

---

## 风险与注意事项

| 风险 | 缓解措施 |
|---|---|
| ADBKeyboard APK 下载失败 | 使用 scrapling 从 GitHub releases 页面找备用链接 |
| ADBKeyboard 在 Android 16 上不兼容 | 备选：用 `service call clipboard` + `KEYCODE_PASTE` |
| 录屏分辨率计算错误 | 保留 720x1280 fallback |
| push_file 超时（大文件） | timeout 设为 120s |
| 测试计划执行超过 300s | 已有 qa_run_test 的 300s 超时，复杂测试需分段 |

---

## 完成标准

- [ ] 33 个 MCP 工具全部注册且编译通过
- [ ] ADBKeyboard 安装并验证 Unicode 输入
- [ ] runner.py 支持 14 种动作类型
- [ ] 性能测量返回有效数据
- [ ] 截图对比功能可用
- [ ] 死代码已清理
- [ ] 测试计划模板文档化所有动作
- [ ] SKILL.md v5.1.0 更新
- [ ] 端到端工作流通过验证
