"""
analysis — Unified AI analysis module for android-dev-qa.

Merges the former analyzer.py + ai_analysis.py into a single coherent module.
Provides:
  - Prompt templates for screenshot / video / logcat analysis
  - AnalysisTask / AnalysisResult data models
  - AIAnalyzer: build tasks, parse responses, merge results
  - Analyzer: legacy prompt builder (backward compat)
"""
from __future__ import annotations

import collections
import json
import logging
import os
import re
from dataclasses import dataclass, field
from typing import Optional

logger = logging.getLogger("android-qa.analysis")

# ══════════════════════════════════════════════════════════════════════════
# Prompt Templates
# ══════════════════════════════════════════════════════════════════════════

SCREENSHOT_UI_PROMPT = """你是 Android UI 测试专家。分析这张应用截图：

## 检查清单
1. **布局**：组件是否截断/重叠/错位？间距均匀？
2. **文字**：是否完整显示？字号合适？
3. **组件**：按钮/图标清晰？触摸区域足够（>=48dp）？
4. **一致性**：颜色/字体/间距是否统一？
5. **空状态**：空列表/加载中是否合理处理？

## 输出 JSON
{"score":1-10,"issues":[{"severity":"critical|high|medium|low","category":"ui_layout|text|component|consistency","title":"问题标题","description":"详细描述","suggestion":"修复建议"}],"observations":["观察到的其他信息"]}

只报告有证据支持的问题。没有问题就 issues 为空数组。"""

SCREENSHOT_LAYOUT_PROMPT = """你是 Android UI 测试工程师。分析这张带标注的截图：

## 检查清单
1. 每个标注的 UI 元素是否可正常交互？
2. 元素之间的层级关系是否合理？
3. 可点击元素的触摸区域是否足够大（>=48dp）？
4. 是否有隐藏或不可见的重要元素？

## 输出 JSON
{"interactable_elements":N,"issue_count":N,"issues":[...],"element_coverage":"good|fair|poor"}

只报告真正的问题。"""

VIDEO_INTERACTION_PROMPT = """你是 Android 动画和交互测试专家。分析这段测试录屏视频。

## 分析维度
1. **转场动画**：界面切换有无动画？是否流畅？时长合理（200-400ms）？
2. **交互响应**：点击后有无即时反馈？是否有延迟？
3. **动画质量**：有无跳变/闪烁/抖动？缓动是否自然？
4. **弹出层**：BottomSheet/Dialog 出现消失是否平滑？
5. **列表滚动**：是否流畅？有无掉帧？

## 输出 JSON
{"animation_score":1-10,"issues":[{"severity":"critical|high|medium|low","title":"问题标题","description":"详细描述","suggestion":"修复建议"}],"assessment":"整体评价"}

只报告有证据支持的问题。"""

LOGCAT_PROMPT = """你是 Android 稳定性专家。分析以下 logcat 日志。

## 分析维度
1. **崩溃**：FATAL EXCEPTION、NullPointerException
2. **ANR**：应用无响应
3. **性能**：Choreographer 跳帧、GC 频繁
4. **功能错误**：网络失败、数据库错误

## 输出 JSON
{"stability_score":1-10,"critical_issues":[{"type":"crash|anr|oom|performance|functional","title":"问题标题","description":"详细描述","log_excerpt":"关键日志行","suggestion":"修复建议"}],"summary":"日志总结"}"""

PLAN_VS_ACTUAL_PROMPT = """你是 QA 测试工程师。对比以下预期行为和实际观察：

## 预期
{expected}

## 实际观察
{actual}

## 截图证据
{screenshot_desc}

## 判断标准
1. 界面是否与预期一致？
2. 功能是否按预期工作？
3. 是否有任何偏差需要关注？

## 输出 JSON
{{"result":"pass|fail|warning","deviation":"偏差描述","severity":"critical|high|medium|low","evidence":"证据描述"}}"""


# ══════════════════════════════════════════════════════════════════════════
# Data Models
# ══════════════════════════════════════════════════════════════════════════

@dataclass
class Issue:
    """A discovered issue."""
    severity: str   # "critical", "high", "medium", "low"
    category: str   # "ui_layout", "animation", "functionality", etc.
    title: str
    description: str
    expected: str = ""
    actual: str = ""
    screenshot: str = ""
    video_segment: str = ""
    log_evidence: str = ""
    suggestion: str = ""
    scenario: str = ""
    step: int = 0


@dataclass
class AnalysisTask:
    """A single analysis task."""
    task_type: str  # "screenshot", "video", "logcat"
    file_path: str
    prompt: str
    step_index: int = 0
    scenario_name: str = ""


@dataclass
class AnalysisResult:
    """A single analysis result."""
    task_type: str
    file_path: str
    raw_response: str = ""
    parsed: dict = field(default_factory=dict)
    error: str = ""


# ══════════════════════════════════════════════════════════════════════════
# AIAnalyzer — Task building + result merging
# ══════════════════════════════════════════════════════════════════════════

class AIAnalyzer:
    """AI analysis coordinator — build tasks, parse responses, merge results."""

    def __init__(self, output_dir: str = "output/analysis"):
        self._output_dir = output_dir
        os.makedirs(output_dir, exist_ok=True)

    # ── Task builders ────────────────────────────────────────────────────

    def build_screenshot_tasks(self, screenshots: list[dict]) -> list[AnalysisTask]:
        tasks = []
        for ss in screenshots:
            prompt = SCREENSHOT_UI_PROMPT
            expected = ss.get("expect", "")
            if expected:
                prompt += f"\n\n## 预期状态\n{expected}"
            tasks.append(AnalysisTask(
                task_type="screenshot",
                file_path=ss["path"],
                prompt=prompt,
                step_index=ss.get("step_index", 0),
                scenario_name=ss.get("scenario", ""),
            ))
        return tasks

    def build_video_task(self, video_path: str, scenarios: list[dict]) -> Optional[AnalysisTask]:
        if not video_path or not os.path.exists(video_path):
            return None
        prompt = VIDEO_INTERACTION_PROMPT
        transitions = []
        for s in scenarios:
            for step in s.get("steps", []):
                if step.get("expect_screen"):
                    transitions.append(f"{step.get('action', '?')} -> {step['expect_screen']}")
        if transitions:
            prompt += "\n\n## 预期转场\n" + "\n".join(transitions)
        return AnalysisTask(task_type="video", file_path=video_path, prompt=prompt)

    def build_logcat_task(self, logcat_path: str, max_lines: int = 500) -> Optional[AnalysisTask]:
        if not logcat_path or not os.path.exists(logcat_path):
            return None
        try:
            with open(logcat_path) as f:
                lines = collections.deque(f, maxlen=max_lines)
            excerpt = "".join(lines)
        except OSError:
            return None
        prompt = LOGCAT_PROMPT + f"\n\n## 日志内容\n```\n{excerpt}\n```"
        return AnalysisTask(task_type="logcat", file_path=logcat_path, prompt=prompt)

    # ── Manifest generation ──────────────────────────────────────────────

    def generate_analysis_manifest(self, run_dir: str,
                                    screenshots: list[dict],
                                    video_path: str = "",
                                    logcat_path: str = "",
                                    scenarios: list[dict] | None = None) -> str:
        """Generate analysis manifest JSON for agent consumption."""
        manifest: dict = {
            "run_dir": run_dir,
            "screenshot_tasks": [],
            "video_task": None,
            "logcat_task": None,
        }
        for ss in screenshots:
            prompt = SCREENSHOT_UI_PROMPT
            if ss.get("expect"):
                expected_text = ss["expect"]
                prompt += f"\n\n## 预期状态\n{expected_text}"
            manifest["screenshot_tasks"].append({
                "file_path": ss["path"],
                "prompt": prompt,
                "step_index": ss.get("step_index", 0),
                "scenario": ss.get("scenario", ""),
            })
        if video_path and os.path.exists(video_path):
            manifest["video_task"] = {"file_path": video_path, "prompt": VIDEO_INTERACTION_PROMPT}
        if logcat_path and os.path.exists(logcat_path):
            try:
                with open(logcat_path) as f:
                    excerpt = "".join(f.readlines()[-500:])
            except OSError:
                excerpt = ""
            if excerpt:
                manifest["logcat_task"] = {
                    "file_path": logcat_path,
                    "prompt": LOGCAT_PROMPT + f"\n\n## 日志内容\n```\n{excerpt}\n```",
                }
        manifest_path = os.path.join(self._output_dir, "analysis_manifest.json")
        with open(manifest_path, "w", encoding="utf-8") as f:
            json.dump(manifest, f, ensure_ascii=False, indent=2)
        return manifest_path

    # ── Response parsing ─────────────────────────────────────────────────

    @staticmethod
    def parse_ai_response(response: str) -> Optional[dict]:
        """Extract JSON from AI response (direct or code-fenced)."""
        try:
            return json.loads(response)
        except json.JSONDecodeError:
            pass
        match = re.search(r'```(?:json)?\s*\n?(.*?)\n?```', response, re.DOTALL)
        if match:
            try:
                return json.loads(match.group(1))
            except json.JSONDecodeError:
                pass
        return None

    # ── Result merging ───────────────────────────────────────────────────

    def merge_results(self, manifest_path: str, results: list[dict]) -> dict:
        """Merge all AI analysis results into a unified report."""
        merged: dict = {
            "screenshot_analyses": [],
            "video_analysis": None,
            "logcat_analysis": None,
            "all_issues": [],
            "scores": {},
        }
        for result in results:
            parsed = self.parse_ai_response(result.get("response", ""))
            if not parsed:
                continue
            rtype = result.get("type", "")
            fpath = result.get("file_path", "")
            if rtype == "screenshot":
                merged["screenshot_analyses"].append({"file": fpath, "analysis": parsed})
                for issue in parsed.get("issues", []):
                    issue["source"] = f"screenshot:{os.path.basename(fpath)}"
                    merged["all_issues"].append(issue)
                if "score" in parsed:
                    merged["scores"][f"screenshot_{os.path.basename(fpath)}"] = parsed["score"]
            elif rtype == "video":
                merged["video_analysis"] = parsed
                for issue in parsed.get("issues", []):
                    issue["source"] = "video"
                    merged["all_issues"].append(issue)
                if "animation_score" in parsed:
                    merged["scores"]["animation"] = parsed["animation_score"]
            elif rtype == "logcat":
                merged["logcat_analysis"] = parsed
                for issue in parsed.get("critical_issues", []):
                    issue["source"] = "logcat"
                    issue["category"] = issue.get("type", "unknown")
                    merged["all_issues"].append(issue)
                if "stability_score" in parsed:
                    merged["scores"]["stability"] = parsed["stability_score"]
        return merged


# ══════════════════════════════════════════════════════════════════════════
# Analyzer — Legacy prompt builder (backward compat)
# ══════════════════════════════════════════════════════════════════════════

class Analyzer:
    """Legacy prompt builder — kept for backward compatibility with runner.py."""

    def __init__(self, output_dir: str = "output/analysis"):
        self._output_dir = output_dir
        os.makedirs(output_dir, exist_ok=True)

    def build_screenshot_prompt(self, test_step: dict) -> str:
        prompt = SCREENSHOT_UI_PROMPT
        if test_step.get("expected"):
            prompt += f"\n\n## 预期状态\n{test_step['expected']}"
        return prompt

    def build_annotated_prompt(self, test_step: dict) -> str:
        return SCREENSHOT_LAYOUT_PROMPT

    def build_video_prompt(self, scenario: dict) -> str:
        prompt = VIDEO_INTERACTION_PROMPT
        if scenario.get("expected_transitions"):
            prompt += f"\n\n## 预期转场\n{json.dumps(scenario['expected_transitions'], ensure_ascii=False, indent=2)}"
        return prompt

    def build_logcat_prompt(self, log_excerpt: str) -> str:
        return f"{LOGCAT_PROMPT}\n\n## 日志内容\n```\n{log_excerpt}\n```"

    def build_comparison_prompt(self, expected: str, actual: str,
                                 screenshot_desc: str = "") -> str:
        return PLAN_VS_ACTUAL_PROMPT.format(
            expected=expected,
            actual=actual,
            screenshot_desc=screenshot_desc or "无额外截图描述",
        )

    @staticmethod
    def parse_json_response(response: str) -> Optional[dict]:
        return AIAnalyzer.parse_ai_response(response)

    def save_analysis(self, analysis: dict, filename: str) -> str:
        path = os.path.join(self._output_dir, filename)
        with open(path, "w", encoding="utf-8") as f:
            json.dump(analysis, f, ensure_ascii=False, indent=2)
        return path
