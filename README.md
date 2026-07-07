# android-dev-qa

Universal Android device control & QA toolkit — MCP server, pluggable backends, AI vision analysis.

## Version History

### v0.3.0 (2026-07-07)
- Fix Compose layout coordinate resolution — merge text + clickable nodes by proximity
- Fix qa_connect fast path — skip port scan when devices already visible in adb devices
- Fix MCP timeout — raised from 10s to 60s for AI analysis calls
- Fix path hint — qa_screenshot returns hint for direct analyze_screenshot workflow
- Add android CLI JSON format parsing (interactions, center, content-desc)
- Add _merge_interactive_with_text — position-based text-to-clickable merge for Compose apps
- Add _parse_xml_layout — uiautomator XML dump with parent-child text propagation
- qa_screenshot returns full element info (clickable, bounds, resource_id)
- find_element prefers clickable matches first

### v0.2.0 (2026-07-06)
- Add built-in vision analysis engine — direct Google AI API (no LiteLLM proxy)
- qa_analyze_screenshot / qa_analyze_video / qa_analyze_logcat tools
- Multi API key rotation (KEY2/4/6), vision tools bypass device check
- No global lock — tools run concurrently via asyncio executor

### v0.1.0
- Initial release — 31 MCP tools, pluggable backends, cross-platform ADB

## Architecture

```
┌─────────────────────────────────────────────────┐
│  Agent (Hermes / OpenCode / any MCP client)     │
│  calls qa_* tools via MCP protocol              │
└──────────────┬──────────────────────────────────┘
               │
┌──────────────▼──────────────────────────────────┐
│  mcp_server.py  (35 tools, no global lock)      │
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
│  ├── vision/         (AI vision analysis)       │
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

**Hermes** (`config.yaml`):

```yaml
android-qa:
  command: /path/to/android-dev-qa/.venv/bin/python
  args:
    - /path/to/android-dev-qa/mcp_server.py
  timeout: 120
```

**OpenCode** (`opencode.json`, WSL):

```json
{
  "mcp": {
    "android-qa": {
      "command": ["wsl", "-e", "/path/to/android-dev-qa/.venv/bin/python3", "/path/to/android-dev-qa/mcp_server.py"],
      "timeout": 60000,
      "environment": {
        "QA_VISION_API_KEYS": "key1,key2,..."
      }
    }
  }
}
```

### 3. Environment Variables

| Variable | Purpose | Default |
|---|---|---|
| `ADB_PATH` | Path to `adb` binary | Auto-discovered |
| `ANDROID_HOME` | Android SDK root | Auto-discovered |
| `QA_BACKEND` | Backend name (`adb`, future: `scrcpy`) | `adb` |
| `QA_VISION_API_KEYS` | Google AI API keys (comma-separated) | — |
| `QA_VISION_IMAGE_MODEL` | Image analysis model | `gemma-4-31b-it` |
| `QA_VISION_VIDEO_MODEL` | Video analysis model | `gemini-3.1-flash-lite` |
| `WINDOWS_USER` | Windows username (WSL only) | Auto-detected |

### 4. Connect & Use

```
qa_connect                → Auto-discover device
qa_screenshot             → Capture screen + layout elements
qa_tap target="text:Settings"  → Tap by text (Compose-aware)
qa_tap target="500,800"        → Tap by coordinates
qa_type text="hello"           → Type text
qa_scroll_find text="Submit"   → Scroll until found
qa_analyze_screenshot image_path="/path/to/screenshot.png"  → AI vision analysis
qa_run_test test_plan_path="plan.json"  → Run test suite
```

## Compose Layout Support

Jetpack Compose apps separate text nodes from clickable regions in the UI hierarchy.
This toolkit automatically merges them by coordinate proximity:

1. Parse android CLI JSON or uiautomator XML layout dump
2. Identify clickable nodes (no text) and text nodes (no clickable flag)
3. Match by position: dx < 60px, dy < 200px
4. Merge text into clickable region so `find_element("text:X")` returns the clickable area

This means `qa_tap target="text:翻译"` correctly hits the button's clickable center,
not the text label's center.

## AI Vision Analysis

Built-in Google AI API integration for screenshot/video/logcat analysis:

- **qa_analyze_screenshot** — UI layout issues, text truncation, component defects (gemma-4-31b-it)
- **qa_analyze_video** — animation fluidity, interaction response, transition quality (gemini-3.1-flash-lite)
- **qa_analyze_logcat** — crash detection, ANR, performance issues (gemma-4-31b-it)

Multi API key rotation with automatic dead-key detection at startup.
Vision tools work without device connection — pass local file paths directly.

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

## License

MIT
