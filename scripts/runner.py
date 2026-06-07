#!/usr/bin/env python3
"""
runner.py — Android Dev QA 主控编排脚本
串联 device → screencap → recorder → logcat → analyzer → reporter
"""
import argparse
import json
import os
import sys
import time
from datetime import datetime
from pathlib import Path

# 添加 scripts 目录到 path
SCRIPT_DIR = Path(__file__).parent
sys.path.insert(0, str(SCRIPT_DIR))

from device import DeviceManager, Device
from recorder import Recorder
from screencap import ScreenCapture, ScreenState
from logcat_capture import LogcatCapture, LogAnalysis
from analyzer import Analyzer, Issue, StepAnalysis, ScenarioAnalysis
from reporter import Reporter, ReportData
from ai_analysis import AIAnalyzer

# ──────────────── YAML 轻量解析 ────────────────
# 不引入 pyyaml 依赖，用 json 替代 test_plan 格式


def load_test_plan(path: str) -> dict:
    """
    加载测试计划。支持 JSON 格式。
    如果是 YAML，尝试用 pyyaml 解析；否则报错。
    """
    with open(path, "r", encoding="utf-8") as f:
        content = f.read()

    # 尝试 JSON
    try:
        return json.loads(content)
    except json.JSONDecodeError:
        pass

    # 尝试 pyyaml
    try:
        import yaml
        return yaml.safe_load(content)
    except ImportError:
        raise ImportError(
            "test_plan 不是 JSON 格式，请安装 pyyaml: pip install pyyaml\n"
            "或将 test_plan 保存为 JSON 格式"
        )


class QARunner:
    """QA 测试主控"""

    def __init__(self, test_plan_path: str, output_base: str = "output"):
        self.plan = load_test_plan(test_plan_path)
        self.app_name = self.plan.get("app", "Unknown App")

        # 创建带时间戳的输出目录
        timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        self.output_dir = os.path.join(output_base, f"run_{timestamp}")
        os.makedirs(self.output_dir, exist_ok=True)

        # 初始化各模块
        self.device_mgr = DeviceManager()
        self.recorder = Recorder(
            adb_path=self.device_mgr._adb,
            output_dir=os.path.join(self.output_dir, "video"),
        )
        self.screencap = ScreenCapture(
            adb_path=self.device_mgr._adb,
            android_cli=self.device_mgr._android_cli,
            output_dir=self.output_dir,
        )
        self.logcat = LogcatCapture(
            adb_path=self.device_mgr._adb,
            output_dir=os.path.join(self.output_dir, "logs"),
        )
        self.analyzer = Analyzer(
            output_dir=os.path.join(self.output_dir, "analysis"),
        )
        self.ai_analyzer = AIAnalyzer(
            output_dir=os.path.join(self.output_dir, "analysis"),
        )
        self.reporter = Reporter(output_dir=self.output_dir)

        self.device: Device = None  # type: ignore
        self.step_counter = 0
        self._screenshots_for_ai: list[dict] = []  # 待 AI 分析的截图

    def setup(self):
        """初始化：连接设备，记录设备信息"""
        print(f"\n{'='*60}")
        print(f"  Android Dev QA — {self.app_name}")
        print(f"{'='*60}\n")

        # 连接设备
        self.device = self.device_mgr.ensure_device()
        info = self.device_mgr.get_device_info(self.device.serial)
        print(f"✅ Device connected: {self.device.serial}")
        print(f"   Model: {info.get('model', 'unknown')}")
        print(f"   Android: {info.get('android_version', '?')} (API {info.get('api_level', '?')})")
        print(f"   Screen: {info.get('screen_size', '?')} @ {info.get('density', '?')}dpi")
        print()

    def execute_scenario(self, scenario: dict) -> ScenarioAnalysis:
        """执行单个测试场景"""
        name = scenario.get("name", "Unnamed")
        print(f"\n{'─'*40}")
        print(f"📋 Scenario: {name}")
        print(f"{'─'*40}")

        scenario_result = ScenarioAnalysis(name=name, passed=True)
        steps = scenario.get("steps", [])

        for i, step in enumerate(steps):
            self.step_counter += 1
            action = step.get("action", "unknown")
            print(f"\n  Step {i+1}/{len(steps)}: {action}")

            step_result = self._execute_step(step, i, name)

            if not step_result.passed:
                scenario_result.passed = False
                scenario_result.issues.extend(step_result.issues)

            scenario_result.steps.append(step_result)

        status = "✅ PASSED" if scenario_result.passed else "❌ FAILED"
        print(f"\n  {status}: {name}")
        return scenario_result

    def _execute_step(self, step: dict, step_index: int, scenario_name: str) -> StepAnalysis:
        """执行单个测试步骤"""
        action = step.get("action", "unknown")
        result = StepAnalysis(
            step_index=step_index,
            action=action,
            passed=True,
        )
        serial = self.device.serial

        try:
            # ══════════════════════════════════════════════
            # 第一优先级：检查 app 是否存活（最廉价的检查）
            # ══════════════════════════════════════════════
            if not self.logcat.is_app_alive():
                crash = self.logcat.get_crash_info()
                crash_msg = f"{crash.tag}: {crash.message}" if crash else "App process died"
                result.passed = False
                result.issues.append(Issue(
                    severity="critical",
                    category="crash",
                    title=f"App crashed/exited before step: {action}",
                    description=crash_msg,
                    log_evidence=crash.raw[:300] if crash else "",
                    scenario=scenario_name,
                    step=step_index,
                ))
                print(f"    💀 APP CRASHED/EXITED — skipping step")
                if crash:
                    print(f"       {crash.tag}: {crash.message[:100]}")
                return result

            # ── 执行操作 ──
            if action == "tap":
                if not self._do_tap(step, serial):
                    result.passed = False
                    result.issues.append(Issue(
                        severity="high",
                        category="functionality",
                        title=f"Element not found: {step.get('target', '?')}",
                        description=f"Could not locate target '{step.get('target', '')}' in current UI",
                        scenario=scenario_name,
                        step=step_index,
                    ))
            elif action == "swipe":
                self._do_swipe(step, serial)
            elif action == "text":
                self.device_mgr.text(step.get("text", ""), serial)
            elif action == "back":
                self.device_mgr.back(serial)
            elif action == "home":
                self.device_mgr.home(serial)
            elif action == "wait":
                time.sleep(step.get("duration", 1000) / 1000)
            elif action == "navigate":
                self._do_navigate(step, serial)
            elif action == "launch":
                self._do_launch(step, serial)
            elif action == "screenshot":
                pass  # 纯截图操作，不执行额外动作
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
            else:
                print(f"    ⚠️ Unknown action: {action}")

            # 等待 UI 稳定
            wait_after = step.get("wait_after", 1000)
            time.sleep(wait_after / 1000)

            # ══════════════════════════════════════════════
            # 操作后再次检查 app 存活（操作可能触发了 crash）
            # ══════════════════════════════════════════════
            if not self.logcat.is_app_alive():
                crash = self.logcat.get_crash_info()
                crash_msg = f"{crash.tag}: {crash.message}" if crash else "App process died"
                result.passed = False
                result.issues.append(Issue(
                    severity="critical",
                    category="crash",
                    title=f"App crashed after action: {action}",
                    description=crash_msg,
                    log_evidence=crash.raw[:300] if crash else "",
                    scenario=scenario_name,
                    step=step_index,
                ))
                print(f"    💀 APP CRASHED after {action}")
                if crash:
                    print(f"       {crash.tag}: {crash.message[:100]}")
                return result

            # ── 截图（只在 app 存活时才截图）──
            if step.get("screenshot", True):
                name = step.get("screenshot_name", f"step_{self.step_counter:03d}")
                state = self.screencap.capture_full_state(
                    serial, self.step_counter, name,
                    watch_package=self.plan.get("package"),
                )
                result.screenshot_analysis = state.screenshot_path
                print(f"    📸 Screenshot: {os.path.basename(state.screenshot_path)}")

                # 记录到 AI 分析队列
                self._screenshots_for_ai.append({
                    "path": state.screenshot_path,
                    "step_index": self.step_counter,
                    "scenario": scenario_name,
                    "expect": step.get("expect", ""),
                })

                # 检查预期界面
                expect_screen = step.get("expect_screen")
                expect = step.get("expect", "")
                if expect_screen or expect:
                    layout_path = state.layout_json
                    if expect_screen:
                        self._verify_screen(expect_screen, layout_path, result)
                    if expect:
                        self._verify_expectation(expect, layout_path, result)

            # ── 检查 logcat 异常（增量检查）──
            new_errors = self.logcat.get_new_errors()
            if new_errors:
                for err in new_errors[-3:]:
                    if err.message:
                        result.issues.append(Issue(
                            severity="medium",
                            category="functionality",
                            title=f"Logcat error in {err.tag}",
                            description=err.message,
                            log_evidence=err.raw[:200],
                            scenario=scenario_name,
                            step=step_index,
                        ))

        except Exception as e:
            result.passed = False
            result.issues.append(Issue(
                severity="critical",
                category="functionality",
                title=f"Step execution failed: {action}",
                description=str(e),
                scenario=scenario_name,
                step=step_index,
            ))
            print(f"    ❌ Error: {e}")

        return result

    def _do_tap(self, step: dict, serial: str) -> bool:
        """执行点击操作。返回是否成功找到并点击了元素。"""
        target = step.get("target", "")
        # 尝试从布局中查找元素
        if target.startswith("resource:"):
            res_id = target.split(":", 1)[1]
            layout = self.screencap.capture_layout(serial, "tap_lookup",
                                                   watch_package=self.plan.get("package"))
            elem = self.screencap.find_element_by_resource(layout, res_id)
            if elem:
                center = self.screencap.get_center(elem.get("bounds", "") or elem.get("center", ""))
                if center:
                    self.device_mgr.tap(center[0], center[1], serial)
                    print(f"    👆 Tap on {res_id} at ({center[0]}, {center[1]})")
                    return True

        elif target.startswith("text:"):
            text = target.split(":", 1)[1]
            layout = self.screencap.capture_layout(serial, "tap_lookup",
                                                   watch_package=self.plan.get("package"))
            elem = self.screencap.find_element_by_text(layout, text)
            if elem:
                center = self.screencap.get_center(elem.get("bounds", "") or elem.get("center", ""))
                if center:
                    self.device_mgr.tap(center[0], center[1], serial)
                    print(f"    👆 Tap on text '{text}' at ({center[0]}, {center[1]})")
                    return True

        elif "," in str(target):
            # 直接坐标: "100,200"
            parts = str(target).split(",")
            x, y = int(parts[0].strip()), int(parts[1].strip())
            self.device_mgr.tap(x, y, serial)
            print(f"    👆 Tap at ({x}, {y})")
            return True

        # fallback: 尝试用坐标
        print(f"    ⚠️ Could not resolve target: {target}")
        return False

    def _do_swipe(self, step: dict, serial: str):
        """执行滑动操作"""
        direction = step.get("direction", "up")
        # 获取屏幕尺寸
        info = self.device_mgr.get_device_info(serial)
        size = info.get("screen_size", "1080x1920")
        try:
            w, h = [int(x) for x in size.split("x")]
        except ValueError:
            w, h = 1080, 1920

        cx, cy = w // 2, h // 2
        duration = step.get("duration", 300)

        if direction == "up":
            self.device_mgr.swipe(cx, cy + 300, cx, cy - 300, duration, serial)
        elif direction == "down":
            self.device_mgr.swipe(cx, cy - 300, cx, cy + 300, duration, serial)
        elif direction == "left":
            self.device_mgr.swipe(cx + 300, cy, cx - 300, cy, duration, serial)
        elif direction == "right":
            self.device_mgr.swipe(cx - 300, cy, cx + 300, cy, duration, serial)

        print(f"    🔄 Swipe {direction}")

    def _do_navigate(self, step: dict, serial: str):
        """导航到指定界面（通过 back + 重新启动）"""
        target = step.get("to", "")
        # 简单实现：先 back 到 home，再 launch
        self.device_mgr.home(serial)
        time.sleep(1)
        package = self.plan.get("package", "")
        if package:
            self.device_mgr.launch_app(package, serial=serial)
            time.sleep(2)

    def _do_launch(self, step: dict, serial: str):
        """启动应用"""
        package = step.get("package", self.plan.get("package", ""))
        activity = step.get("activity", "")
        if package:
            self.device_mgr.launch_app(package, activity, serial)
            time.sleep(3)
            print(f"    🚀 Launched {package}")

    def _do_long_press(self, target: str, duration_ms: int, serial: str):
        """长按操作"""
        if target.startswith("text:"):
            text = target.split(":", 1)[1]
            layout = self.screencap.capture_layout(serial, "longpress",
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
            print(f"    ⚠️ Could not resolve drag targets")

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

    def _verify_screen(self, expected_screen: str, layout_path: str, result: StepAnalysis):
        """验证当前界面是否与预期一致"""
        # 简单验证：检查布局中是否有预期界面的特征元素
        screens = self.plan.get("screens", [])
        expected = None
        for s in screens:
            if s["id"] == expected_screen:
                expected = s
                break

        if expected:
            elements = expected.get("elements", [])
            found = 0
            for elem_id in elements:
                if self.screencap.find_element_by_text(layout_path, elem_id):
                    found += 1
                elif self.screencap.find_element_by_resource(layout_path, elem_id):
                    found += 1

            if found == 0 and elements:
                result.issues.append(Issue(
                    severity="high",
                    category="functionality",
                    title=f"Wrong screen: expected '{expected_screen}'",
                    description=f"Expected elements not found: {elements}",
                    expected=f"Screen: {expected_screen}",
                    actual="Screen: unknown",
                    scenario="",
                    step=result.step_index,
                ))
                result.passed = False
                print(f"    ❌ Expected screen '{expected_screen}' not detected")
            else:
                print(f"    ✅ Screen '{expected_screen}' confirmed ({found}/{len(elements)} elements)")

    def _verify_expectation(self, expectation: str, layout_path: str, result: StepAnalysis):
        """验证文字预期"""
        # 检查预期文字是否出现在布局中
        words = [w.strip() for w in expectation.split(",") if w.strip()]
        for word in words:
            if len(word) > 2:  # 忽略太短的词
                elem = self.screencap.find_element_by_text(layout_path, word)
                if elem:
                    print(f"    ✅ Found expected: '{word}'")
                else:
                    print(f"    ⚠️ Not found: '{word}'")

    def run(self):
        """执行完整测试流程"""
        self.setup()

        scenarios = self.plan.get("scenarios", [])
        if not scenarios:
            print("❌ No scenarios defined in test plan!")
            return

        print(f"\n📱 Test plan: {len(scenarios)} scenarios")
        print(f"📁 Output: {self.output_dir}")

        # ── 开始录屏（可选）──
        skip_recording = self.plan.get("skip_recording", False)
        if not skip_recording:
            print("\n🎬 Starting recording...")
            self.recorder.start(self.device.serial, "full_test.mp4")
        else:
            print("\n⏩ Recording skipped (skip_recording=true)")

        # ── 开始 logcat（实时监控 app 存活）──
        print("📋 Starting logcat capture...")
        self.logcat.start(self.device.serial, filename="logcat.txt",
                          watch_package=self.plan.get("package"))

        # ── 执行场景 ──
        start_time = time.time()
        all_scenarios = []

        for scenario in scenarios:
            result = self.execute_scenario(scenario)
            all_scenarios.append(result)

        duration = time.time() - start_time

        # ── 停止录屏和日志 ──
        print("\n🛑 Stopping...")
        video_path = None
        if not skip_recording:
            print("   Stopping recording...")
            video_path = self.recorder.stop(self.device.serial)
        print("   Stopping logcat...")
        log_path = self.logcat.stop()

        # ── 分析日志 ──
        print("\n🔍 Analyzing logs...")
        log_analysis = self.logcat.analyze()

        # ── 汇总结果 ──
        all_issues = []
        total_passed = 0
        total_failed = 0
        total_warnings = 0

        for scenario in all_scenarios:
            if scenario.passed:
                total_passed += 1
            else:
                total_failed += 1
            all_issues.extend([
                {
                    "severity": iss.severity,
                    "category": iss.category,
                    "title": iss.title,
                    "description": iss.description,
                    "expected": iss.expected,
                    "actual": iss.actual,
                    "screenshot": iss.screenshot,
                    "video_segment": iss.video_segment,
                    "log_evidence": iss.log_evidence,
                    "suggestion": iss.suggestion,
                    "scenario": iss.scenario,
                    "step": iss.step,
                }
                for iss in scenario.issues
            ])

        # 添加日志发现的问题
        for crash in log_analysis.crashes:
            all_issues.append({
                "severity": "critical",
                "category": "crash",
                "title": f"Crash: {crash.tag}",
                "description": crash.message[:200],
                "log_evidence": crash.raw[:300],
                "suggestion": "Check crash stack trace in full logcat output",
            })

        for oom in log_analysis.ooms:
            all_issues.append({
                "severity": "high",
                "category": "performance",
                "title": "Out of Memory detected",
                "description": oom.message[:200],
                "log_evidence": oom.raw[:300],
                "suggestion": "Check memory usage and optimize",
            })

        # ── 生成报告 ──
        print("\n📝 Generating report...")
        info = self.device_mgr.get_device_info(self.device.serial)

        # ── 生成 AI 分析清单 ──
        print("🧠 Generating AI analysis manifest...")
        video_path_final = video_path if video_path else ""
        logcat_path_final = log_path if log_path else ""
        manifest_path = self.ai_analyzer.generate_analysis_manifest(
            run_dir=self.output_dir,
            screenshots=self._screenshots_for_ai,
            video_path=video_path_final,
            logcat_path=logcat_path_final,
            scenarios=scenarios,
        )
        print(f"   AI manifest: {manifest_path}")

        report_data = ReportData(
            app_name=self.app_name,
            device_info=f"{info.get('model', '?')} (API {info.get('api_level', '?')})",
            test_date=datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
            duration=f"{duration:.0f}s",
            total_scenarios=len(scenarios),
            passed=total_passed,
            failed=total_failed,
            warnings=total_warnings,
            issues=all_issues,
            scenarios=[
                {
                    "name": s.name,
                    "passed": s.passed,
                    "steps": [
                        {
                            "index": step.step_index,
                            "action": step.action,
                            "passed": step.passed,
                            "screenshot": step.screenshot_analysis,
                            "analysis": step.layout_analysis,
                            "issues": [
                                {"title": iss.title} for iss in step.issues
                            ],
                        }
                        for step in s.steps
                    ],
                    "video_analysis": s.video_analysis,
                }
                for s in all_scenarios
            ],
            log_summary={
                "total_lines": log_analysis.total_lines,
                "error_count": len(log_analysis.errors),
                "warning_count": len(log_analysis.warnings),
                "crash_count": len(log_analysis.crashes),
                "anr_count": len(log_analysis.anrs),
                "oom_count": len(log_analysis.ooms),
                "crash_details": [c.raw[:200] for c in log_analysis.crashes[:5]],
            },
            screenshots_dir=os.path.join(self.output_dir, "screenshots"),
            video_dir=os.path.join(self.output_dir, "video"),
        )

        report_content = self.reporter.generate(report_data)
        report_path = self.reporter.save(report_content)

        # 保存原始数据
        manifest = {
            "app": self.app_name,
            "device": self.device.serial,
            "device_info": info,
            "timestamp": datetime.now().isoformat(),
            "duration_seconds": duration,
            "scenarios_total": len(scenarios),
            "scenarios_passed": total_passed,
            "scenarios_failed": total_failed,
            "issues_total": len(all_issues),
            "issues_by_severity": self.reporter._count_by_severity(all_issues),
            "log_summary": {
                "total_lines": log_analysis.total_lines,
                "errors": len(log_analysis.errors),
                "warnings": len(log_analysis.warnings),
                "crashes": len(log_analysis.crashes),
                "anrs": len(log_analysis.anrs),
                "ooms": len(log_analysis.ooms),
            },
            "artifacts": {
                "report": report_path,
                "video": video_path,
                "logcat": log_path,
                "screenshots": os.path.join(self.output_dir, "screenshots"),
                "ai_manifest": manifest_path,
            },
        }
        manifest_path = os.path.join(self.output_dir, "manifest.json")
        with open(manifest_path, "w", encoding="utf-8") as f:
            json.dump(manifest, f, ensure_ascii=False, indent=2)

        # ── 最终输出 ──
        print(f"\n{'='*60}")
        print(f"  TEST COMPLETE — {self.app_name}")
        print(f"{'='*60}")
        print(f"  Duration: {duration:.0f}s")
        print(f"  Scenarios: {total_passed} passed, {total_failed} failed")
        print(f"  Issues: {len(all_issues)} found")
        sev_counts = self.reporter._count_by_severity(all_issues)
        for sev in ["critical", "high", "medium", "low"]:
            if sev_counts[sev] > 0:
                emoji = self.reporter.SEVERITY_EMOJI[sev]
                print(f"    {emoji} {sev.title()}: {sev_counts[sev]}")
        print(f"\n  📄 Report: {report_path}")
        print(f"  📁 Output: {self.output_dir}")
        print(f"{'='*60}\n")

        return report_path


def main():
    parser = argparse.ArgumentParser(description="Android Dev QA Runner")
    parser.add_argument("test_plan", help="Path to test_plan.json")
    parser.add_argument("-o", "--output", default="output", help="Output directory")
    args = parser.parse_args()

    runner = QARunner(args.test_plan, args.output)
    runner.run()


if __name__ == "__main__":
    main()
