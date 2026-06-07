"""
ai_analysis.py — Gemini AI 分析协调器
负责：
1. 生成分析任务清单（截图 + 视频 + 日志）
2. 调用 Gemini API 做语义分析
3. 解析 AI 返回的 JSON 结果
4. 汇总为统一分析报告

注意：实际 Gemini 调用通过 Hermes 的 vision_analyze / native_gemini_video_analyze 执行
本模块提供：
- 分析 prompt 模板
- 结果解析
- 报告汇总逻辑
"""
import json
import os
from typing import Optional
from dataclasses import dataclass, field


# ──────────────── Prompt 模板 ────────────────

SCREENSHOT_PROMPT = """你是 Android UI 测试专家。分析这张应用截图：

## 检查清单
1. **布局**：组件是否截断/重叠/错位？间距均匀？
2. **文字**：是否完整显示？字号合适？
3. **组件**：按钮/图标清晰？触摸区域足够（≥48dp）？
4. **一致性**：颜色/字体/间距是否统一？
5. **空状态**：空列表/加载中是否合理处理？

## 输出 JSON
{"score":1-10,"issues":[{"severity":"critical|high|medium|low","category":"ui_layout|text|component|consistency","title":"问题标题","description":"详细描述","suggestion":"修复建议"}],"observations":["观察到的其他信息"]}

只报告有证据支持的问题。没有问题就 issues 为空数组。"""

VIDEO_PROMPT = """你是 Android 动画和交互测试专家。分析这段测试录屏视频。

## 分析维度
1. **转场动画**：界面切换有无动画？是否流畅？时长合理（200-400ms）？
2. **交互响应**：点击后有无即时反馈？是否有延迟？
3. **动画质量**：有无跳变/闪烁/抖动？缓动是否自然？
4. **弹出层**：BottomSheet/Dialog 出现消失是否平滑？
5. **列表滚动**：是否流畅？有无掉帧？

## 输出 JSON
{"animation_score":1-10,"issues":[{"severity":"critical|high|medium|low","title":"问题标题","description":"详细描述","suggestion":"修复建议"}],"assessment":"整体评价"}"""

LOGCAT_PROMPT = """你是 Android 稳定性专家。分析以下 logcat 日志。

## 分析维度
1. **崩溃**：FATAL EXCEPTION、NullPointerException
2. **ANR**：应用无响应
3. **性能**：Choreographer 跳帧、GC 频繁
4. **功能错误**：网络失败、数据库错误

## 输出 JSON
{"stability_score":1-10,"critical_issues":[{"type":"crash|anr|oom|performance|functional","title":"问题标题","description":"详细描述","log_excerpt":"关键日志行","suggestion":"修复建议"}],"summary":"日志总结"}"""


@dataclass
class AnalysisTask:
    """一个分析任务"""
    task_type: str  # "screenshot", "video", "logcat"
    file_path: str
    prompt: str
    step_index: int = 0
    scenario_name: str = ""


@dataclass
class AnalysisResult:
    """一个分析结果"""
    task_type: str
    file_path: str
    raw_response: str = ""
    parsed: dict = field(default_factory=dict)
    error: str = ""


class AIAnalyzer:
    """Gemini AI 分析协调器"""

    def __init__(self, output_dir: str = "output/analysis"):
        self._output_dir = output_dir
        os.makedirs(output_dir, exist_ok=True)

    def build_screenshot_tasks(self, screenshots: list[dict]) -> list[AnalysisTask]:
        """构建截图分析任务列表"""
        tasks = []
        for ss in screenshots:
            prompt = SCREENSHOT_PROMPT
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
        """构建视频分析任务"""
        if not video_path or not os.path.exists(video_path):
            return None
        prompt = VIDEO_PROMPT
        transitions = []
        for s in scenarios:
            for step in s.get("steps", []):
                if step.get("expect_screen"):
                    transitions.append(f"{step.get('action', '?')} → {step['expect_screen']}")
        if transitions:
            prompt += f"\n\n## 预期转场\n" + "\n".join(transitions)
        return AnalysisTask(
            task_type="video",
            file_path=video_path,
            prompt=prompt,
        )

    def build_logcat_task(self, logcat_path: str) -> Optional[AnalysisTask]:
        """构建日志分析任务"""
        if not logcat_path or not os.path.exists(logcat_path):
            return None
        # 只取最后 500 行（避免太长）
        try:
            import collections
            with open(logcat_path) as f:
                lines = collections.deque(f, maxlen=500)
            excerpt = "".join(lines)
        except OSError:
            return None
        prompt = LOGCAT_PROMPT + f"\n\n## 日志内容\n```\n{excerpt}\n```"
        return AnalysisTask(
            task_type="logcat",
            file_path=logcat_path,
            prompt=prompt,
        )

    def generate_analysis_manifest(self, run_dir: str,
                                    screenshots: list[dict],
                                    video_path: str = "",
                                    logcat_path: str = "",
                                    scenarios: list[dict] = None) -> str:
        """
        生成分析任务清单文件，供 Hermes agent 消费。
        返回 manifest 路径。
        """
        manifest = {
            "run_dir": run_dir,
            "screenshot_tasks": [],
            "video_task": None,
            "logcat_task": None,
        }

        # 截图任务
        for ss in screenshots:
            expected = ss.get("expect", "")
            prompt = SCREENSHOT_PROMPT
            if expected:
                prompt += f"\n\n## 预期状态\n{expected}"
            manifest["screenshot_tasks"].append({
                "file_path": ss["path"],
                "prompt": prompt,
                "step_index": ss.get("step_index", 0),
                "scenario": ss.get("scenario", ""),
            })

        # 视频任务
        if video_path and os.path.exists(video_path):
            manifest["video_task"] = {
                "file_path": video_path,
                "prompt": VIDEO_PROMPT,
            }

        # 日志任务
        if logcat_path and os.path.exists(logcat_path):
            try:
                with open(logcat_path) as f:
                    lines = f.readlines()
                excerpt = "".join(lines[-500:])
            except OSError:
                excerpt = ""
            if excerpt:
                manifest["logcat_task"] = {
                    "file_path": logcat_path,
                    "prompt": LOGCAT_PROMPT + f"\n\n## 日志内容\n```\n{excerpt}\n```",
                }

        # 保存 manifest
        manifest_path = os.path.join(self._output_dir, "analysis_manifest.json")
        with open(manifest_path, "w", encoding="utf-8") as f:
            json.dump(manifest, f, ensure_ascii=False, indent=2)

        return manifest_path

    def parse_ai_response(self, response: str) -> Optional[dict]:
        """从 AI 响应中提取 JSON"""
        import re
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

    def merge_results(self, manifest_path: str, results: list[dict]) -> dict:
        """
        合并所有 AI 分析结果为统一报告数据。
        results: [{"type": "screenshot|video|logcat", "file_path": "...", "response": "..."}]
        """
        merged = {
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

            if result["type"] == "screenshot":
                merged["screenshot_analyses"].append({
                    "file": result["file_path"],
                    "analysis": parsed,
                })
                for issue in parsed.get("issues", []):
                    issue["source"] = f"screenshot:{os.path.basename(result['file_path'])}"
                    merged["all_issues"].append(issue)
                if "score" in parsed:
                    merged["scores"][f"screenshot_{os.path.basename(result['file_path'])}"] = parsed["score"]

            elif result["type"] == "video":
                merged["video_analysis"] = parsed
                for issue in parsed.get("issues", []):
                    issue["source"] = "video"
                    merged["all_issues"].append(issue)
                if "animation_score" in parsed:
                    merged["scores"]["animation"] = parsed["animation_score"]

            elif result["type"] == "logcat":
                merged["logcat_analysis"] = parsed
                for issue in parsed.get("critical_issues", []):
                    issue["source"] = "logcat"
                    issue["category"] = issue.get("type", "unknown")
                    merged["all_issues"].append(issue)
                if "stability_score" in parsed:
                    merged["scores"]["stability"] = parsed["stability_score"]

        return merged
