# android-dev-qa

Universal Android device control & QA toolkit — MCP server, pluggable backends, AI analysis.

## Architecture

```
┌─────────────────────────────────────────────────┐
│  Agent (Hermes / Claude / any MCP client)       │
│  calls qa_* tools via MCP protocol              │
└──────────────┬──────────────────────────────────┘
               │
┌──────────────▼──────────────────────────────────┐
│  mcp_server.py  (31 tools, no global lock)      │
│  Routes to DeviceBackend protocol               │
└──────────────┬──────────────────────────────────┘
               │
┌──────────────▼──────────────────────────────────┐
│  backends/                                      │
│  ├── base.py       (DeviceBackend ABC)          │
│  ├── adb_backend.py (ADB — default)             │
│  └── (future: scrcpy, appium, ...)              │
└──────────────┬──────────────────────────────────┘
               │
┌──────────────▼──────────────────────────────────┐
│  scripts/                                       │
│  ├── device.py       (ADB auto-discovery)       │
│  ├── screencap.py    (Screenshot + layout)      │
│  ├── recorder.py     (Screen recording)         │
│  ├── logcat_capture.py (Logcat + crash detect)  │
│  ├── analysis/       (AI prompts + result merge)│
│  └── runner.py       (Test plan runner)         │
└─────────────────────────────────────────────────┘
```

## Quick Start

### 1. Install

```bash
cd android-dev-qa
python -m venv .venv
source .venv/bin/activate
pip install -e ".[dev]"
```

### 2. Configure Agent

Add to your MCP client config (e.g. Hermes `config.yaml`):

```yaml
android-qa:
  command: /path/to/android-dev-qa/.venv/bin/python
  args:
    - /path/to/android-dev-qa/mcp_server.py
  timeout: 120
```

### 3. Environment Variables

| Variable | Purpose | Default |
|---|---|---|
| `ADB_PATH` | Path to `adb` binary | Auto-discovered |
| `ANDROID_HOME` | Android SDK root | Auto-discovered |
| `QA_BACKEND` | Backend name (`adb`, future: `scrcpy`) | `adb` |
| `WINDOWS_USER` | Windows username (WSL only) | Auto-detected |

### 4. Connect & Use

```
qa_connect                → Auto-discover device
qa_screenshot             → Capture screen
qa_tap target="text:Settings"  → Tap by text
qa_tap target="500,800"        → Tap by coordinates
qa_type text="hello"           → Type text
qa_scroll_find text="Submit"   → Scroll until found
qa_run_test test_plan_path="plan.json"  → Run test suite
```

## Backends

### ADB (default)

Cross-platform ADB auto-discovery:
- **WSL**: Searches `/mnt/c/Users/<user>/AppData/Local/Android/Sdk/`
- **macOS**: `~/Library/Android/sdk/`
- **Linux**: `/usr/bin/adb`, `~/.local/bin/adb`
- **Windows**: `%LOCALAPPDATA%\Android\Sdk\`
- **Fallback**: `ADB_PATH` env var or `PATH`

Emulator type detection: AVD, Genymotion, BlueStacks, LDPlayer.

### Adding a New Backend

1. Create `scripts/backends/your_backend.py`
2. Subclass `DeviceBackend` from `base.py`
3. Implement all abstract methods
4. Register in `backends/__init__.py`:
   ```python
   register_backend("your_backend", YourBackend)
   ```
5. Set `QA_BACKEND=your_backend`

## AI Analysis (Optional)

The `analysis/` module provides prompt templates and result merging for:
- **Screenshot UI analysis** — layout, text, component issues
- **Video interaction analysis** — animations, transitions, responsiveness
- **Logcat stability analysis** — crashes, ANRs, OOMs

AI calls are NOT made by the MCP server itself. Instead:
1. `qa_run_test` generates an `analysis_manifest.json`
2. The agent reads the manifest and calls its own vision/LLM tools
3. Results are fed back through `AIAnalyzer.merge_results()`

This keeps the MCP server agent-agnostic.

## Test Plans

See `templates/test_plan.json` for the full schema. Supported actions:

| Action | Description |
|---|---|
| `launch` | Start app |
| `tap` | Tap element |
| `long_press` | Long press |
| `double_tap` | Double tap |
| `swipe` | Swipe direction |
| `drag` | Drag between elements |
| `type` | Input text (ASCII) |
| `type_unicode` | Input Unicode (clipboard paste) |
| `scroll_find` | Scroll until text found |
| `element_state` | Verify element state |
| `back` / `home` | Navigation keys |
| `wait` | Delay |
| `screenshot` | Capture + optional expect |

## Changes from v0.1.0

- **No global lock**: Tools run concurrently via asyncio executor
- **Pluggable backends**: Swap ADB for Scrcpy/Appium without code changes
- **Cross-platform ADB**: Auto-discovers on WSL/macOS/Linux/Windows
- **Unified analysis**: `analyzer.py` + `ai_analysis.py` merged into `analysis/`
- **Clipboard fix**: Multi-method fallback (am broadcast → service call → ADBKeyboard)
- **Unicode input**: Clipboard + paste strategy for CJK on Android 14+

## License

MIT
