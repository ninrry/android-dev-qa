"""
analyzer.py — Gemini AI 分析引擎
负责截图语义分析、视频内容分析、预期vs实际比对
注意：实际 Gemini 调用通过 Hermes 的 vision_analyze / native_gemini_video_analyze 执行
本模块负责构建分析 prompt 和解析结果
"""
import json
import os
from typing import Optional
from dataclasses import dataclass, field


@dataclass
class Issue:
    """发现的问题"""
    severity: str  # "critical", "high", "medium", "low"
    category: str  # "ui_layout", "animation", "functionality", "crash", "performance"
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
class StepAnalysis:
    """单步分析结果"""
    step_index: int
    action: str
    passed: bool
    issues: list[Issue] = field(default_factory=list)
    screenshot_analysis: str = ""
    layout_analysis: str = ""
    notes: str = ""


@dataclass
class ScenarioAnalysis:
    """场景分析结果"""
    name: str
    passed: bool
    steps: list[StepAnalysis] = field(default_factory=list)
    issues: list[Issue] = field(default_factory=list)
    video_analysis: str = ""


# ──────────────── Prompt 模板 ────────────────

SCREENSHOT_UI_PROMPT = """你是一位资深的 Android UI 测试工程师。请仔细分析这张截图，检查以下问题：

## 检查清单
1. **布局问题**：组件是否被截断、重叠、错位？间距是否均匀？
2. **文字问题**：文字是否完整显示？是否被截断？字号是否合适？
3. **组件问题**：按钮/图标是否清晰？是否可识别？触摸区域是否足够？
4. **一致性问题**：颜色/字体/间距是否与 Material Design 一致？
5. **空状态**：空列表/加载中/错误状态是否合理处理？
6. **对齐问题**：左对齐/居中/右对齐是否一致？

## 输出格式
请以 JSON 格式输出，包含：
```json
{
  "overall_score": 1-10,
  "issues": [
    {
      "severity": "critical|high|medium|low",
      "category": "ui_layout|text|component|consistency",
      "title": "问题标题",
      "description": "详细描述",
      "suggestion": "修复建议"
    }
  ],
  "observations": ["观察到的其他信息"]
}
```

如果一切正常，issues 为空数组，overall_score 给出合理评分。不要刻意找问题，也不要忽略明显问题。"""

SCREENSHOT_LAYOUT_PROMPT = """你是一位 Android UI 测试工程师。分析这张带标注的截图：

## 检查清单
1. 每个标注的 UI 元素是否可正常交互（点击/滑动）？
2. 元素之间的层级关系是否合理？
3. 可点击元素的触摸区域是否足够大（≥48dp）？
4. 是否有隐藏或不可见的重要元素？
5. ScrollView 中的内容是否完整？

## 输出格式
```json
{
  "interactable_elements": N,
  "issue_count": N,
  "issues": [...],
  "element_coverage": "good|fair|poor"
}
```

只报告真正的问题，不要凭空捏造。"""

VIDEO_INTERACTION_PROMPT = """你是一位 Android 动画和交互测试专家。分析这段测试录屏视频。

## 分析维度
1. **转场动画**：界面切换是否有动画？动画是否流畅？时长是否合理（200-400ms）？
2. **交互响应**：点击后是否有即时反馈？是否有延迟感？
3. **动画质量**：是否有跳变/闪烁/抖动？缓动曲线是否自然？
4. **弹出/呼出**：BottomSheet/Dialog/Toast 的出现和消失动画是否平滑？
5. **列表滚动**：滚动是否流畅？是否有掉帧？
6. **骨架屏/加载**：加载状态是否有过渡？是否有突然出现的内容？

## 输出格式
```json
{
  "animation_score": 1-10,
  "transition_issues": [
    {
      "severity": "critical|high|medium|low",
      "title": "问题标题",
      "description": "详细描述",
      "timestamp_approx": "视频中的大致时间点",
      "suggestion": "修复建议"
    }
  ],
  "overall_assessment": "整体评价"
}
```

只报告有证据支持的问题。不要因为「可能有问题」就报告。"""

LOGCAT_PROMPT = """你是一位 Android 性能和稳定性专家。分析以下 logcat 日志片段。

## 分析维度
1. **崩溃**：FATAL EXCEPTION、NullPointerException、OOM
2. **ANR**：应用无响应
3. **性能**：Choreographer 跳帧、GC 频繁、内存压力
4. **警告**：API 废弃、兼容性问题
5. **功能性错误**：网络失败、数据库错误、权限问题

## 输出格式
```json
{
  "stability_score": 1-10,
  "critical_issues": [
    {
      "type": "crash|anr|oom|performance|functional",
      "title": "问题标题",
      "description": "详细描述",
      "log_excerpt": "关键日志行",
      "suggestion": "修复建议"
    }
  ],
  "warnings": [...],
  "summary": "日志总结"
}
```

只报告真正需要关注的问题。常见的低优先级警告可以汇总提及。"""

PLAN_vs_ACTUAL_PROMPT = """你是一位 QA 测试工程师。对比以下预期行为和实际观察：

## 预期
{expected}

## 实际观察
{actual}

## 截图证据
{screenshot_desc}

## 判断标准
1. 界面是否与预期一致？
2. 功能是否按预期工作？
3. 动画/转场是否按预期发生？
4. 是否有任何偏差需要关注？

## 输出格式
```json
{{
  "result": "pass|fail|warning",
  "deviation": "偏差描述（如有）",
  "severity": "critical|high|medium|low",
  "evidence": "证据描述"
}}
```"""


class Analyzer:
    """AI 分析引擎 — 构建 prompt 并格式化结果"""

    def __init__(self, output_dir: str = "output/analysis"):
        self._output_dir = output_dir
        os.makedirs(output_dir, exist_ok=True)

    def build_screenshot_prompt(self, test_step: dict) -> str:
        """构建截图分析 prompt"""
        prompt = SCREENSHOT_UI_PROMPT
        if test_step.get("expected"):
            prompt += f"\n\n## 预期状态\n{test_step['expected']}"
        return prompt

    def build_annotated_prompt(self, test_step: dict) -> str:
        """构建标注截图分析 prompt"""
        return SCREENSHOT_LAYOUT_PROMPT

    def build_video_prompt(self, scenario: dict) -> str:
        """构建视频分析 prompt"""
        prompt = VIDEO_INTERACTION_PROMPT
        if scenario.get("expected_transitions"):
            prompt += f"\n\n## 预期转场\n{json.dumps(scenario['expected_transitions'], ensure_ascii=False, indent=2)}"
        return prompt

    def build_logcat_prompt(self, log_excerpt: str) -> str:
        """构建日志分析 prompt"""
        return f"{LOGCAT_PROMPT}\n\n## 日志内容\n```\n{log_excerpt}\n```"

    def build_comparison_prompt(self, expected: str, actual: str,
                                 screenshot_desc: str = "") -> str:
        """构建预期vs实际比对 prompt"""
        return PLAN_vs_ACTUAL_PROMPT.format(
            expected=expected,
            actual=actual,
            screenshot_desc=screenshot_desc or "无额外截图描述",
        )

    def parse_json_response(self, response: str) -> Optional[dict]:
        """从 AI 响应中提取 JSON"""
        # 尝试直接解析
        try:
            return json.loads(response)
        except json.JSONDecodeError:
            pass

        # 尝试从 markdown code block 提取
        import re
        match = re.search(r'```(?:json)?\s*\n?(.*?)\n?```', response, re.DOTALL)
        if match:
            try:
                return json.loads(match.group(1))
            except json.JSONDecodeError:
                pass

        return None

    def save_analysis(self, analysis: dict, filename: str) -> str:
        """保存分析结果到文件"""
        path = os.path.join(self._output_dir, filename)
        with open(path, "w", encoding="utf-8") as f:
            json.dump(analysis, f, ensure_ascii=False, indent=2)
        return path
