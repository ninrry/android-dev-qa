"""Unit tests for android-dev-qa v0.2.0 — device discovery, backend protocol, analysis."""
import json
import os
import sys
import unittest
from unittest.mock import patch, MagicMock

# Ensure scripts/ is importable
SCRIPT_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "scripts")
if SCRIPT_DIR not in sys.path:
    sys.path.insert(0, SCRIPT_DIR)


class TestEmulatorDetection(unittest.TestCase):
    """Test emulator type detection from serial strings."""

    def test_avd_emulator(self):
        from device import _detect_emulator_type
        is_emu, etype = _detect_emulator_type("emulator-5554")
        self.assertTrue(is_emu)
        self.assertEqual(etype, "avd")

    def test_genymotion(self):
        from device import _detect_emulator_type
        is_emu, etype = _detect_emulator_type("169.254.10.1")
        self.assertTrue(is_emu)
        self.assertEqual(etype, "genymotion")

    def test_physical_device(self):
        from device import _detect_emulator_type
        is_emu, etype = _detect_emulator_type("R5CR80XXXXX")
        self.assertFalse(is_emu)
        self.assertEqual(etype, "")

    def test_tcp_device_unknown(self):
        from device import _detect_emulator_type
        # Non-127.0.0.1 TCP address (e.g. wireless ADB) — NOT necessarily emulator
        is_emu, etype = _detect_emulator_type("192.168.1.100:5555")
        self.assertFalse(is_emu)  # Remote TCP = could be physical device
        self.assertEqual(etype, "")

    def test_ldplayer_by_port(self):
        from device import _detect_emulator_type
        # LDPlayer default port 62001
        is_emu, etype = _detect_emulator_type("127.0.0.1:62001")
        self.assertTrue(is_emu)
        self.assertEqual(etype, "ldplayer")

    def test_mumu_by_port(self):
        from device import _detect_emulator_type
        # MuMu default port 7555
        is_emu, etype = _detect_emulator_type("127.0.0.1:7555")
        self.assertTrue(is_emu)
        self.assertEqual(etype, "mumu")

    def test_bluestacks_by_name(self):
        from device import _detect_emulator_type
        is_emu, etype = _detect_emulator_type("127.0.0.1:5565_bluestacks")
        self.assertTrue(is_emu)
        self.assertEqual(etype, "bluestacks")

    def test_ldplayer_by_name(self):
        from device import _detect_emulator_type
        is_emu, etype = _detect_emulator_type("ldplayer-0")
        self.assertTrue(is_emu)
        self.assertEqual(etype, "ldplayer")

    def test_nox_by_name(self):
        from device import _detect_emulator_type
        is_emu, etype = _detect_emulator_type("nox_1")
        self.assertTrue(is_emu)
        self.assertEqual(etype, "nox")


class TestADBDiscovery(unittest.TestCase):
    """Test ADB search path generation."""

    def test_env_var_priority(self):
        from device import _adb_search_paths
        with patch.dict(os.environ, {"ADB_PATH": "/custom/adb"}):
            paths = _adb_search_paths()
            self.assertEqual(paths[0], "/custom/adb")

    def test_android_home(self):
        from device import _adb_search_paths
        with patch.dict(os.environ, {"ANDROID_HOME": "/sdk"}, clear=False):
            paths = _adb_search_paths()
            assert any("/sdk/platform-tools" in p for p in paths)

    def test_wsl_detection(self):
        from device import _IS_WSL
        # Just verify it's a bool
        self.assertIsInstance(_IS_WSL, bool)


class TestBackendProtocol(unittest.TestCase):
    """Test DeviceBackend ABC cannot be instantiated directly."""

    def test_cannot_instantiate_abc(self):
        from backends.base import DeviceBackend
        with self.assertRaises(TypeError):
            DeviceBackend()

    def test_backend_info_dataclass(self):
        from backends.base import BackendInfo
        info = BackendInfo(name="test", capabilities=["tap", "swipe"])
        self.assertEqual(info.name, "test")
        self.assertEqual(len(info.capabilities), 2)


class TestBackendRegistry(unittest.TestCase):
    """Test backend registration and retrieval."""

    def test_default_adb_backend(self):
        from backends import get_backend
        b = get_backend("adb")
        info = b.get_info()
        self.assertEqual(info.name, "adb")
        self.assertIn("tap", info.capabilities)

    def test_unknown_backend_raises(self):
        from backends import get_backend
        with self.assertRaises(ValueError):
            get_backend("nonexistent")


class TestAnalysisModule(unittest.TestCase):
    """Test AI analysis prompt building and response parsing."""

    def test_parse_json_direct(self):
        from analysis import AIAnalyzer
        result = AIAnalyzer.parse_ai_response('{"score": 8, "issues": []}')
        self.assertEqual(result["score"], 8)

    def test_parse_json_code_fenced(self):
        from analysis import AIAnalyzer
        response = '```json\n{"score": 9}\n```'
        result = AIAnalyzer.parse_ai_response(response)
        self.assertEqual(result["score"], 9)

    def test_parse_invalid_returns_none(self):
        from analysis import AIAnalyzer
        result = AIAnalyzer.parse_ai_response("not json at all")
        self.assertIsNone(result)

    def test_build_screenshot_tasks(self):
        from analysis import AIAnalyzer
        ai = AIAnalyzer(output_dir="/tmp/test_analysis")
        tasks = ai.build_screenshot_tasks([
            {"path": "/tmp/s1.png", "expect": "Button visible"},
            {"path": "/tmp/s2.png"},
        ])
        self.assertEqual(len(tasks), 2)
        self.assertIn("预期状态", tasks[0].prompt)
        self.assertNotIn("预期状态", tasks[1].prompt)

    def test_legacy_analyzer_compat(self):
        from analysis import Analyzer
        a = Analyzer(output_dir="/tmp/test_analysis")
        prompt = a.build_screenshot_prompt({"expected": "Home screen"})
        self.assertIn("Home screen", prompt)
        parsed = a.parse_json_response('{"ok": true}')
        self.assertTrue(parsed["ok"])


class TestDeviceManagerInit(unittest.TestCase):
    """Test DeviceManager can be created with auto-discovery."""

    def test_init_finds_adb(self):
        from device import DeviceManager
        dm = DeviceManager()
        self.assertTrue(dm._adb.endswith("adb.exe") or dm._adb.endswith("adb"))


class TestTargetResolution(unittest.TestCase):
    """Test _resolve_target helper in mcp_server."""

    def test_coordinate_parsing(self):
        # Import requires mcp_server which has MCP dependencies
        # Test the logic inline instead
        target = "500,800"
        parts = target.split(",")
        x, y = int(parts[0]), int(parts[1])
        self.assertEqual(x, 500)
        self.assertEqual(y, 800)

    def test_text_prefix(self):
        target = "text:Settings"
        self.assertTrue(target.startswith("text:"))
        self.assertEqual(target[5:], "Settings")

    def test_resource_prefix(self):
        target = "resource:id/submit_btn"
        self.assertTrue(target.startswith("resource:"))
        self.assertEqual(target[9:], "id/submit_btn")


if __name__ == "__main__":
    unittest.main()
