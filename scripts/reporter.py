"""
reporter.py — 统一报告生成模块
将截图分析、视频分析、日志分析汇总为 Markdown QA 报告
"""
import json
import os
from datetime import datetime
from typing import Optional
from dataclasses import dataclass, field


@dataclass
class ReportData:
    """报告数据"""
    app_name: str = ""
    device_info: str = ""
    test_date: str = ""
    duration: str = ""
    total_scenarios: int = 0
    passed: int = 0
    failed: int = 0
    warnings: int = 0
    issues: list[dict] = field(default_factory=list)
    scenarios: list[dict] = field(default_factory=list)
    log_summary: dict = field(default_factory=dict)
    screenshots_dir: str = ""
    video_dir: str = ""


class Reporter:
    """QA 报告生成器"""

    SEVERITY_EMOJI = {
        "critical": "🔴",
        "high": "🟠",
        "medium": "🟡",
        "low": "🔵",
    }

    SEVERITY_ORDER = ["critical", "high", "medium", "low"]

    def __init__(self, output_dir: str = "output"):
        self._output_dir = output_dir

    def _count_by_severity(self, issues: list[dict]) -> dict:
        counts = {"critical": 0, "high": 0, "medium": 0, "low": 0}
        for issue in issues:
            sev = issue.get("severity", "low").lower()
            if sev in counts:
                counts[sev] += 1
        return counts

    def generate(self, data: ReportData) -> str:
        """生成 Markdown 格式的 QA 报告"""
        lines = []
        sev_counts = self._count_by_severity(data.issues)

        # ── Header ──
        lines.append(f"# QA Report: {data.app_name}")
        lines.append("")
        lines.append(f"**Date:** {data.test_date} | **Device:** {data.device_info} | **Duration:** {data.duration}")
        lines.append("")

        # ── Summary ──
        lines.append("## Summary")
        lines.append("")
        lines.append(f"- **Total scenarios:** {data.total_scenarios}")
        lines.append(f"- **Passed:** {data.passed} ✅ | **Failed:** {data.failed} ❌ | **Warnings:** {data.warnings} ⚠️")
        lines.append(f"- **Issues:** {len(data.issues)} total")
        for sev in self.SEVERITY_ORDER:
            if sev_counts[sev] > 0:
                lines.append(f"  - {self.SEVERITY_EMOJI[sev]} {sev.title()}: {sev_counts[sev]}")
        lines.append("")

        # ── Issues ──
        if data.issues:
            lines.append("## Issues Found")
            lines.append("")
            sorted_issues = sorted(data.issues, key=lambda x: self.SEVERITY_ORDER.index(x.get("severity", "low")))
            for i, issue in enumerate(sorted_issues, 1):
                sev = issue.get("severity", "low")
                emoji = self.SEVERITY_EMOJI.get(sev, "⚪")
                lines.append(f"### Issue #{i} [{sev.upper()}] — {issue.get('title', 'Unknown')}")
                lines.append("")
                lines.append(f"- **Category:** {issue.get('category', 'unknown')}")
                lines.append(f"- **Scenario:** {issue.get('scenario', 'N/A')} > Step {issue.get('step', 'N/A')}")
                lines.append(f"- **Expected:** {issue.get('expected', 'N/A')}")
                lines.append(f"- **Actual:** {issue.get('actual', 'N/A')}")
                lines.append(f"- **Description:** {issue.get('description', '')}")
                if issue.get("screenshot"):
                    lines.append(f"- **Evidence:** `{issue['screenshot']}`")
                if issue.get("video_segment"):
                    lines.append(f"- **Video:** `{issue['video_segment']}`")
                if issue.get("log_evidence"):
                    lines.append(f"- **Log:** `{issue['log_evidence']}`")
                if issue.get("suggestion"):
                    lines.append(f"- **Suggestion:** {issue['suggestion']}")
                lines.append("")

        # ── Scenario Details ──
        lines.append("## Scenario Details")
        lines.append("")
        for scenario in data.scenarios:
            status = "✅" if scenario.get("passed") else "❌"
            lines.append(f"### {status} {scenario.get('name', 'Unnamed')}")
            lines.append("")

            steps = scenario.get("steps", [])
            for step in steps:
                step_status = "✅" if step.get("passed") else "❌"
                lines.append(f"**Step {step.get('index', '?')}:** {step.get('action', 'N/A')} — {step_status}")
                if step.get("screenshot"):
                    lines.append(f"  - Screenshot: `{step['screenshot']}`")
                if step.get("analysis"):
                    lines.append(f"  - Analysis: {step['analysis']}")
                if step.get("issues"):
                    for issue in step["issues"]:
                        lines.append(f"  - ⚠️ {issue.get('title', '')}")
                lines.append("")

            if scenario.get("video_analysis"):
                lines.append(f"**Video Analysis:** {scenario['video_analysis']}")
                lines.append("")

        # ── Log Summary ──
        if data.log_summary:
            lines.append("## Log Analysis Summary")
            lines.append("")
            lines.append(f"- **Total lines captured:** {data.log_summary.get('total_lines', 0)}")
            lines.append(f"- **Errors:** {data.log_summary.get('error_count', 0)}")
            lines.append(f"- **Warnings:** {data.log_summary.get('warning_count', 0)}")
            lines.append(f"- **Crashes:** {data.log_summary.get('crash_count', 0)}")
            lines.append(f"- **ANRs:** {data.log_summary.get('anr_count', 0)}")
            lines.append(f"- **OOMs:** {data.log_summary.get('oom_count', 0)}")
            lines.append("")

            if data.log_summary.get("crash_details"):
                lines.append("### Crash Details")
                for crash in data.log_summary["crash_details"][:5]:
                    lines.append(f"- {crash}")
                lines.append("")

        # ── Artifacts ──
        lines.append("## Test Artifacts")
        lines.append("")
        if data.screenshots_dir:
            lines.append(f"- **Screenshots:** `{data.screenshots_dir}`")
        if data.video_dir:
            lines.append(f"- **Video recordings:** `{data.video_dir}`")
        lines.append(f"- **Full report:** `{self._output_dir}/report.md`")
        lines.append("")

        # ── Footer ──
        lines.append("---")
        lines.append(f"*Generated by android-dev-qa at {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}*")
        lines.append("")

        return "\n".join(lines)

    def save(self, content: str, filename: str = "report.md") -> str:
        """保存报告文件"""
        path = os.path.join(self._output_dir, filename)
        with open(path, "w", encoding="utf-8") as f:
            f.write(content)
        return path
