# AGENTS.md — android-dev-qa

## 项目概述

通用 Android 开发测试能力工具，通过截图+视频+日志三维度，对任意 Android 应用进行自动化 UI/动画/功能测试。

## 架构

```
scripts/
├── runner.py           # 主控编排（入口）
├── device.py           # ADB 设备管理
├── recorder.py         # 录屏管理
├── screencap.py        # 截图 + 布局 dump
├── logcat_capture.py   # 日志实时捕获
├── analyzer.py         # Gemini AI 分析
└── reporter.py         # 报告生成
```

## 核心流程

1. **Planning** — 解析项目/定义 test_plan.yaml
2. **Execution** — ADB 驱动操作 + 录屏 + 截图 + logcat
3. **Analysis** — Gemini 视觉/视频分析 + 日志异常检测
4. **Report** — Markdown 统一报告（截图证据 + 视频片段 + Bug 列表）

## 工具依赖

- `adb` — Android Debug Bridge（通过 Windows SDK）
- `android` CLI — 设备交互和截图
- `maestro` — 可选，声明式 UI 测试
- Gemini API — 截图/视频语义分析（通过 Hermes vision_analyze）

## 边界

### ✅ 允许
- 执行测试和分析
- 修改 scripts/ 下的脚本
- 生成报告到 output/

### ⚠️ 需确认
- 修改 test_plan.yaml 模板
- 添加新的分析引擎

### 🚫 禁止
- 自动修复发现的问题（只报告不修复）
- 在真机上执行未经确认的测试
