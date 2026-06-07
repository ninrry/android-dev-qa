"""
batch_analyzer.py — 批量分析引擎
解决"每步单独调 Gemini 太慢"的问题

核心策略：
1. 截图分组：按场景分组，每组 3-5 张截图合并分析
2. 多图输入：一次 Gemini 调用传入多张截图
3. 渐进分析：粗筛（layout dump）→ 精查（Gemini 截图）
4. Layout 优先：layout dump 有结论的不需要 Gemini，只把不确定的送 Gemini
"""
import json
import os
from typing import Optional
from dataclasses import dataclass, field


# ──────────────── 批量分析 Prompt ────────────────

BATCH_SCREENSHOT_PROMPT = """你是 Android QA 专家。以下是同一个应用在一次测试中的 {count} 张截图，按操作顺序排列。

## 截图信息
{screenshots_info}

## 分析要求
对比这些截图，检查：

1. **界面切换正确性**：每张截图的界面是否与上一张有合理变化？
2. **动画/过渡痕迹**：切换是否自然？有无跳变？
3. **UI 一致性**：同一元素在不同页面是否保持一致？
4. **布局问题**：文字截断、组件重叠、间距异常？
5. **状态变化**：按钮状态、加载状态、播放状态是否正确？

## 输出 JSON
```json
{{
  "flow_score": 1-10,
  "transitions": [
    {{
      "from": "截图编号",
      "to": "截图编号",
      "result": "correct|unexpected|missing",
      "note": "说明"
    }}
  ],
  "issues": [
    {{
      "severity": "critical|high|medium|low",
      "title": "问题标题",
      "description": "详细描述",
      "screenshots": ["涉及的截图编号"],
      "suggestion": "修复建议"
    }}
  ],
  "observations": ["观察到的其他信息"]
}}
```

只报告有证据支持的问题。对比多张截图后才能得出的结论优先。"""

BATCH_LOGCAT_PROMPT = """你是 Android 稳定性和性能专家。以下是测试期间的 logcat 日志摘要。

## 日志概况
- 总行数: {total_lines}
- 错误数: {error_count}
- 警告数: {warning_count}

## 错误日志（去重后）
{error_summary}

## 警告日志（去重后）
{warning_summary}

## 分析要求
1. **崩溃分析**：是否有 FATAL EXCEPTION？根因是什么？
2. **ANR 检测**：是否有应用无响应？
3. **性能问题**：Choreographer 跳帧、频繁 GC？
4. **功能错误**：网络失败、数据库错误、权限问题？
5. **模式识别**：同一错误反复出现？有规律？

## 输出 JSON
```json
{{
  "stability_score": 1-10,
  "critical_issues": [
    {{
      "type": "crash|anr|oom|performance|functional",
      "title": "问题标题",
      "description": "详细描述",
      "frequency": "once|repeated|continuous",
      "log_excerpt": "关键日志行（最多3行）",
      "suggestion": "修复建议"
    }}
  ],
  "warnings_summary": "警告汇总",
  "pattern": "发现的重复模式（如有）",
  "summary": "一句话总结"
}}
```

重点关注反复出现的问题和潜在的性能瓶颈。"""


@dataclass
class BatchGroup:
    """一组待分析的截图"""
    scenario_name: str
    screenshots: list[dict] = field(default_factory=list)  # [{"path": ..., "step": ..., "expect": ...}]


class BatchAnalyzer:
    """批量分析引擎"""

    def __init__(self, output_dir: str = "output/analysis"):
        self._output_dir = output_dir
        os.makedirs(output_dir, exist_ok=True)

    def group_screenshots(self, screenshots: list[dict]) -> list[BatchGroup]:
        """按场景分组截图"""
        groups: dict[str, BatchGroup] = {}
        for ss in screenshots:
            scenario = ss.get("scenario", "unknown")
            if scenario not in groups:
                groups[scenario] = BatchGroup(scenario_name=scenario)
            groups[scenario].screenshots.append(ss)
        return list(groups.values())

    def build_batch_screenshot_prompt(self, group: BatchGroup) -> str:
        """构建批量截图分析 prompt"""
        screenshots_info = []
        for i, ss in enumerate(group.screenshots, 1):
            path = ss.get("path", "")
            expect = ss.get("expect", "")
            info = f"截图 {i}: {os.path.basename(path)}"
            if expect:
                info += f"（预期: {expect}）"
            screenshots_info.append(info)

        return BATCH_SCREENSHOT_PROMPT.format(
            count=len(group.screenshots),
            screenshots_info="\n".join(screenshots_info),
        )

    def build_logcat_batch_prompt(self, logcat_path: str,
                                   error_lines: list[str] = None,
                                   warning_lines: list[str] = None) -> str:
        """构建批量日志分析 prompt"""
        total_lines = 0
        error_count = 0
        warning_count = 0

        if logcat_path and os.path.exists(logcat_path):
            try:
                with open(logcat_path) as f:
                    lines = f.readlines()
                total_lines = len(lines)
            except OSError:
                pass

        # 去重错误日志
        error_summary = "无"
        if error_lines:
            error_count = len(error_lines)
            # 按 tag 去重，只保留每种错误的前 3 条
            seen = {}
            for line in error_lines:
                tag = line.split()[-1] if line.split() else line[:50]
                if tag not in seen:
                    seen[tag] = []
                if len(seen[tag]) < 3:
                    seen[tag].append(line[:200])
            error_summary = "\n".join(
                f"[{tag}] {lines[0][:100]}" for tag, lines in list(seen.items())[:10]
            )

        warning_summary = "无"
        if warning_lines:
            warning_count = len(warning_lines)
            seen = {}
            for line in warning_lines:
                tag = line.split()[-1] if line.split() else line[:50]
                if tag not in seen:
                    seen[tag] = []
                if len(seen[tag]) < 2:
                    seen[tag].append(line[:150])
            warning_summary = "\n".join(
                f"[{tag}] {lines[0][:80]}" for tag, lines in list(seen.items())[:8]
            )

        return BATCH_LOGCAT_PROMPT.format(
            total_lines=total_lines,
            error_count=error_count,
            warning_count=warning_count,
            error_summary=error_summary,
            warning_summary=warning_summary,
        )

    def smart_select_screenshots(self, group: BatchGroup,
                                  layout_results: dict = None,
                                  max_screenshots: int = 4) -> list[dict]:
        """
        智能选择需要 Gemini 分析的截图。
        
        策略：
        - layout dump 已确认的步骤 → 跳过（不需要 Gemini）
        - layout dump 未确认的步骤 → 必须送 Gemini
        - 每组最多 max_screenshots 张
        """
        selected = []

        for ss in group.screenshots:
            step = ss.get("step_index", 0)
            # 如果 layout dump 已确认，跳过
            if layout_results and step in layout_results:
                if layout_results[step].get("verified", False):
                    continue

            selected.append(ss)
            if len(selected) >= max_screenshots:
                break

        # 确保至少有第一张和最后一张
        if group.screenshots and group.screenshots[0] not in selected:
            selected.insert(0, group.screenshots[0])
        if group.screenshots and group.screenshots[-1] not in selected:
            selected.append(group.screenshots[-1])

        return selected[:max_screenshots]

    def merge_with_layout(self, gemini_results: list[dict],
                           layout_verifications: dict) -> dict:
        """
        合并 Gemini 分析结果和 layout dump 验证结果。
        优先信任 layout dump（结构化），Gemini 作为补充。
        """
        merged = {
            "layout_verified_steps": [],
            "gemini_analyzed_steps": [],
            "combined_issues": [],
            "confidence_summary": {},
        }

        for step_id, layout_result in layout_verifications.items():
            if layout_result.get("verified"):
                merged["layout_verified_steps"].append(step_id)

        for gemini_result in gemini_results:
            merged["gemini_analyzed_steps"].append(gemini_result.get("file", ""))
            for issue in gemini_result.get("issues", []):
                # 标记来源
                issue["source"] = "gemini"
                merged["combined_issues"].append(issue)

        return merged
