# Android Dev QA 🤖

**通用 Android 开发测试能力工具**

通过截图 + 视频 + 日志三维度，对任意 Android 应用进行自动化 UI/动画/功能测试与智能分析。

## 核心特性

- 📸 **截图分析** — UI 布局、组件形状、文字显示、对齐检查
- 🎬 **视频分析** — 交互动画、界面切换、过渡效果、响应延迟
- 📋 **日志分析** — crash、ANR、OOM、异常警告实时检测
- 🧠 **AI 驱动** — Gemini 语义理解（非像素对比），发现真实问题
- 📊 **统一报告** — Markdown 格式，截图证据 + 日志摘要 + Bug 列表

## 快速开始

```bash
# 1. 确保设备连接
adb devices

# 2. 编辑测试计划
cp templates/test_plan.json output/my_app_plan.json
# 按照你的应用界面和交互流程编辑

# 3. 运行
cd scripts
python runner.py ../output/my_app_plan.json

# 4. 查看报告
cat ../output/run_*/report.md
```

## 架构

```
Planning → test_plan.json（界面/转场/弹出/场景定义）
    ↓
Execution → ADB 驱动操作 + 录屏 + 截图 + UI dump + logcat
    ↓
Analysis → Gemini 截图语义分析 + 视频动画分析 + 日志异常检测
    ↓
Report → 预期vs实际比对 → 统一 QA 报告
```

## 项目结构

```
android-dev-qa/
├── scripts/
│   ├── runner.py           # 主控编排（入口）
│   ├── device.py           # ADB 设备管理
│   ├── recorder.py         # 录屏管理
│   ├── screencap.py        # 截图 + 布局 dump
│   ├── logcat_capture.py   # 日志捕获
│   ├── analyzer.py         # Gemini 分析 prompt
│   └── reporter.py         # 报告生成
├── templates/
│   └── test_plan.json      # 测试计划模板
├── output/                 # 测试输出（gitignored）
├── project.yaml
├── AGENTS.md
└── README.md
```

## 设计原则

1. **事前规划** — 不是"看看有没有问题"，而是"比对预期和实际"
2. **多维取证** — 截图（静态）+ 视频（动态）+ 日志（后台）三者关联
3. **有据有节** — 基于证据报告问题，不凭空捏造，也不视而不见
4. **通用适用** — 通过 test_plan.json 适配任意 Android 应用

## License

MIT
