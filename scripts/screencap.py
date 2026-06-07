import logging
"""
screencap.py — 截图 + UI 布局抓取
支持 android CLI 和 adb fallback，含超时重试
"""
import json
import subprocess
import os
import re
import time
import threading
from typing import Optional


class ScreenCapture:
    """截图和布局抓取管理器"""

    def __init__(self, adb_path: str, android_cli: Optional[str] = None,
                 output_dir: str = "output"):
        self._adb = adb_path
        self._android_cli = android_cli
        base = output_dir
        self._screenshot_dir = os.path.join(base, "screenshots")
        self._layout_dir = os.path.join(base, "layouts")
        self._annotated_dir = os.path.join(base, "annotated")
        self._last_layout_file: Optional[str] = None
        self._layout_cache: dict = {}
        self._cache_package: dict = {}  # serial -> last cached package name

        for d in [self._screenshot_dir, self._layout_dir, self._annotated_dir]:
            os.makedirs(d, exist_ok=True)

    def _adb_cmd(self, serial: str, args: list[str]) -> list[str]:
        return [self._adb, "-s", serial] + args

    def _run(self, serial: str, args: list[str], timeout: int = 15) -> subprocess.CompletedProcess:
        cmd = self._adb_cmd(serial, args)
        return subprocess.run(
            cmd, capture_output=True, timeout=timeout, stdin=subprocess.DEVNULL,
            encoding="utf-8", errors="replace",
        )

    def _run_android_cli(self, args: list[str], timeout: int = 15) -> Optional[subprocess.CompletedProcess]:
        if not self._android_cli:
            return None
        cmd = [self._android_cli] + args
        return subprocess.run(
            cmd, capture_output=True, timeout=timeout, stdin=subprocess.DEVNULL,
            encoding="utf-8", errors="replace",
        )

    def _run_with_timeout(self, cmd: list[str], timeout: int = 10) -> subprocess.CompletedProcess:
        """运行命令，超时后强制终止（兼容 WSL + Windows .exe）"""
        try:
            # 优先用 timeout 命令（Linux 原生，可靠杀进程）
            full_cmd = ["timeout", str(timeout)] + cmd
            return subprocess.run(
                full_cmd, capture_output=True, timeout=timeout + 5,
                stdin=subprocess.DEVNULL, encoding="utf-8", errors="replace",
            )
        except subprocess.TimeoutExpired:
            raise
        except FileNotFoundError:
            # timeout 命令不存在（极少见），用基础方式
            proc = subprocess.Popen(
                cmd, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, stdin=subprocess.DEVNULL,
            )
            try:
                proc.wait(timeout=timeout)
            except subprocess.TimeoutExpired:
                proc.kill()
                proc.wait(timeout=3)
            return subprocess.CompletedProcess(cmd, returncode=proc.returncode)

    def capture_screenshot(self, serial: str, name: str = "screenshot") -> str:
        """截取普通 PNG 截图"""
        timestamp = int(time.time() * 1000)
        filename = f"{name}_{timestamp}.png"
        local_path = os.path.join(self._screenshot_dir, filename)

        # 优先用 android CLI（--device 是命名参数）
        result = self._run_android_cli([
            "screen", "capture", f"--device={serial}", "-o", local_path,
        ])
        if result and result.returncode == 0 and os.path.exists(local_path):
            return local_path

        # fallback: adb exec-out screencap
        remote_tmp = "/sdcard/qa_screenshot.png"
        self._run(serial, ["shell", "screencap", "-p", remote_tmp])
        self._run(serial, ["pull", remote_tmp, local_path])
        self._run(serial, ["shell", "rm", "-f", remote_tmp])

        return local_path

    def capture_annotated(self, serial: str, name: str = "annotated") -> str:
        """截取带标注的截图（UI元素编号）"""
        timestamp = int(time.time() * 1000)
        filename = f"{name}_{timestamp}.png"
        local_path = os.path.join(self._annotated_dir, filename)

        result = self._run_android_cli([
            "screen", "capture", f"--device={serial}", "--annotate", "-o", local_path,
        ])
        if result and result.returncode == 0 and os.path.exists(local_path):
            return local_path

        # fallback: 普通截图
        return self.capture_screenshot(serial, name)

    def capture_layout(self, serial: str, name: str = "layout",
                       watch_package: Optional[str] = None) -> str:
        """抓取 UI 布局树（XML/JSON），含超时重试"""
        
        # 检查缓存（2s TTL + 包名一致性）
        cached = self._layout_cache.get(serial)
        if cached:
            ts, path = cached
            if time.time() - ts < 2.0 and os.path.exists(path):
                if not watch_package or self._cache_package.get(serial) == watch_package:
                    return path
                # 包名变了，缓存失效
                self._layout_cache.pop(serial, None)
                self._cache_package.pop(serial, None)

        # 确保目标 app 在前台
        if watch_package:
            self._ensure_foreground(serial, watch_package)

        timestamp = int(time.time() * 1000)
        filename = f"{name}_{timestamp}.json"
        local_path = os.path.join(self._layout_dir, filename)

        # 重试最多 2 次（每次 8s 超时，强制终止）
        for attempt in range(2):
            try:
                cmd = self._android_cli or self._adb
                args = (["layout", f"--device={serial}", "-o", local_path]
                        if self._android_cli
                        else ["-s", serial, "shell", "uiautomator", "dump", "/sdcard/qa_layout.xml"])
                result = self._run_with_timeout(
                    [cmd] + args, timeout=8,
                )
                if result.returncode == 0:
                    # android CLI 路径：直接检查 json 文件
                    if self._android_cli and os.path.exists(local_path) and os.path.getsize(local_path) > 50:
                        self._last_layout_file = local_path
                        self._layout_cache[serial] = (time.time(), local_path)
                        if watch_package:
                            self._cache_package[serial] = watch_package
                        return local_path
                    # adb fallback：pull xml
                    if not self._android_cli:
                        xml_path = local_path.replace(".json", ".xml")
                        self._run(serial, ["pull", "/sdcard/qa_layout.xml", xml_path])
                        self._run(serial, ["shell", "rm", "-f", "/sdcard/qa_layout.xml"])
                        if os.path.exists(xml_path):
                            self._layout_cache[serial] = (time.time(), xml_path)
                            if watch_package:
                                self._cache_package[serial] = watch_package
                            return xml_path
            except (subprocess.TimeoutExpired, Exception):
                pass
            time.sleep(0.5)

        # 最终 fallback：快速 uiautomator dump（单次 5s 超时）
        if self._android_cli:
            try:
                result = self._run_with_timeout(
                    [self._adb, "-s", serial, "shell", "uiautomator", "dump", "/sdcard/qa_layout.xml"],
                    timeout=5,
                )
                if result.returncode == 0:
                    xml_path = local_path.replace(".json", ".xml")
                    self._run(serial, ["pull", "/sdcard/qa_layout.xml", xml_path])
                    self._run(serial, ["shell", "rm", "-f", "/sdcard/qa_layout.xml"])
                    if os.path.exists(xml_path):
                        return xml_path
            except Exception as e:
                logging.debug(e)

        return None

    def _ensure_foreground(self, serial: str, package: str):
        """确保指定包名的应用在前台"""
        try:
            result = subprocess.run(
                [self._adb, "-s", serial, "shell", "dumpsys", "activity", "activities"],
                capture_output=True, timeout=5, stdin=subprocess.DEVNULL,
                encoding="utf-8", errors="replace",
            )
            if package not in result.stdout:
                # app 不在前台，重新启动
                subprocess.run(
                    [self._adb, "-s", serial, "shell", "monkey", "-p", package,
                     "-c", "android.intent.category.LAUNCHER", "1"],
                    capture_output=True, timeout=5, stdin=subprocess.DEVNULL,
                    encoding="utf-8", errors="replace",
                )
                time.sleep(1)
        except Exception:
            pass
    def _parse_layout_file(self, layout_path: str) -> list[dict]:
        """Auto-detect layout file format (XML from uiautomator dump vs JSON from android CLI) and return normalized element list.
        Each element dict has: text, resource_id, class_name, bounds, center, clickable, enabled, checked, selected, focusable, focused, scrollable, password, content_desc
        """
        if not layout_path or not os.path.exists(layout_path):
            return []
        try:
            with open(layout_path, 'r', encoding='utf-8', errors='replace') as f:
                content = f.read().strip()
            # Try XML first (uiautomator dump output)
            if content.startswith('<?xml') or content.startswith('<hierarchy'):
                return self._parse_uiautomator_xml(content)
            # Try JSON (android CLI output)
            data = json.loads(content)
            if isinstance(data, list):
                return [ScreenCapture._normalize_json_element(e) for e in data]
            return []
        except Exception:
            return []

    @staticmethod
    def _parse_uiautomator_xml(xml_content: str) -> list[dict]:
        """Parse uiautomator dump XML into normalized element list."""
        import xml.etree.ElementTree as ET
        elements = []
        try:
            root = ET.fromstring(xml_content)
        except ET.ParseError:
            return []
        for node in root.iter('node'):
            text = node.get('text', '')
            resource_id = node.get('resource-id', '')
            class_name = node.get('class', '')
            bounds_str = node.get('bounds', '')
            clickable = node.get('clickable', 'false') == 'true'
            enabled = node.get('enabled', 'false') == 'true'
            checked = node.get('checked', 'false') == 'true'
            selected = node.get('selected', 'false') == 'true'
            focusable = node.get('focusable', 'false') == 'true'
            focused = node.get('focused', 'false') == 'true'
            scrollable = node.get('scrollable', 'false') == 'true'
            password = node.get('password', 'false') == 'true'
            content_desc = node.get('content-desc', '')
            # Parse bounds
            center = ''
            m = re.search(r'\[(\d+),(\d+)\]\[(\d+),(\d+)\]', bounds_str)
            if m:
                x1, y1, x2, y2 = int(m.group(1)), int(m.group(2)), int(m.group(3)), int(m.group(4))
                cx, cy = (x1 + x2) // 2, (y1 + y2) // 2
                center = f'[{cx},{cy}]'
            elements.append({
                'text': text,
                'resource_id': resource_id,
                'class_name': class_name,
                'bounds': bounds_str,
                'center': center,
                'clickable': clickable,
                'enabled': enabled,
                'checked': checked,
                'selected': selected,
                'focusable': focusable,
                'focused': focused,
                'scrollable': scrollable,
                'password': password,
                'content_desc': content_desc,
            })
        return elements

    @staticmethod
    def _normalize_json_element(elem: dict) -> dict:
        """Normalize android CLI JSON element to match XML attribute format.
        JSON has: interactions=[clickable,focusable], state=[selected,checked]
        XML has: clickable=true/false, enabled=true/false, etc.
        """
        interactions = elem.get('interactions', [])
        state = elem.get('state', [])
        # Map interactions to boolean attributes
        if 'clickable' not in elem:
            elem['clickable'] = 'clickable' in interactions
        if 'focusable' not in elem:
            elem['focusable'] = 'focusable' in interactions
        if 'scrollable' not in elem:
            elem['scrollable'] = 'scrollable' in interactions
        # Map state to boolean attributes
        if 'selected' not in elem:
            elem['selected'] = 'selected' in state
        if 'checked' not in elem:
            elem['checked'] = 'checked' in state
        # Default enabled to True if not present
        if 'enabled' not in elem:
            elem['enabled'] = True
        # Ensure center/bounds exist
        if 'center' not in elem:
            elem['center'] = ''
        if 'bounds' not in elem:
            elem['bounds'] = ''
        if 'text' not in elem:
            elem['text'] = ''
        if 'content_desc' not in elem:
            elem['content_desc'] = elem.get('content-desc', '')
        return elem

    @staticmethod
    def compare_screenshots(path_a: str, path_b: str) -> dict:
        """比较两张截图，返回相似度和差异信息。使用 MD5 哈希和文件大小比较。"""
        import hashlib
        if not os.path.exists(path_a) or not os.path.exists(path_b):
            return {"error": "File not found", "similar": False, "similarity": 0.0}
        size_a = os.path.getsize(path_a)
        size_b = os.path.getsize(path_b)
        size_ratio = min(size_a, size_b) / max(size_a, size_b) if max(size_a, size_b) > 0 else 0
        with open(path_a, "rb") as f:
            hash_a = hashlib.md5(f.read()).hexdigest()
        with open(path_b, "rb") as f:
            hash_b = hashlib.md5(f.read()).hexdigest()
        exact_match = hash_a == hash_b
        similar = exact_match or size_ratio > 0.95
        return {
            "similar": similar,
            "exact_match": exact_match,
            "size_a": size_a,
            "size_b": size_b,
            "size_ratio": round(size_ratio, 3),
            "hash_a": hash_a[:8],
            "hash_b": hash_b[:8],
        }

    def find_element_by_text(self, layout_path: str, text: str) -> Optional[dict]:
        """在布局文件中按文本查找元素（支持 XML 和 JSON）"""
        elements = self._parse_layout_file(layout_path)
        # Exact match first
        for elem in elements:
            if elem.get('text') == text:
                return elem
        # Partial match
        for elem in elements:
            if text in (elem.get('text') or ''):
                return elem
        return None

    def find_element_by_resource(self, layout_path: str, resource_id: str) -> Optional[dict]:
        """在布局文件中按 resource-id 查找元素（支持 XML 和 JSON）"""
        elements = self._parse_layout_file(layout_path)
        for elem in elements:
            rid = elem.get('resource_id') or elem.get('resource-id') or ''
            if rid == resource_id:
                return elem
        return None

    @staticmethod
    def get_center(coord_str: str, screen_w: int = 1344, screen_h: int = 2992) -> Optional[tuple[int, int]]:
        """从坐标字符串提取中心点，含屏幕边界校验"""
        import re
        # 匹配 [x1,y1][x2,y2] bounds 格式
        m = re.search(r'\[(\d+),(\d+)\]\[(\d+),(\d+)\]', coord_str)
        if m:
            x1, y1, x2, y2 = int(m.group(1)), int(m.group(2)), int(m.group(3)), int(m.group(4))
            cx, cy = (x1 + x2) // 2, (y1 + y2) // 2
        else:
            # 匹配 [x,y] 单坐标格式
            m = re.search(r'\[(\d+),(\d+)\]', coord_str)
            if m:
                cx, cy = int(m.group(1)), int(m.group(2))
            else:
                return None
        # 边界校验：坐标必须在屏幕范围内（留 10px 边距防误触状态栏/导航栏）
        MARGIN = 10
        if not (MARGIN <= cx <= screen_w - MARGIN and MARGIN <= cy <= screen_h - MARGIN):
            return None
        return cx, cy

    def capture_full_state(self, serial: str, step: int, name: str,
                           watch_package: Optional[str] = None) -> "ScreenState":
        """一次操作后抓取完整屏幕状态（截图 + 布局 dump）"""
        screenshot = self.capture_screenshot(serial, f"step_{step:03d}_{name}")
        layout = self.capture_layout(serial, f"layout_{step:03d}_{name}",
                                     watch_package=watch_package)
        elements = []
        if layout:
            elements = self._parse_layout_file(layout)
        return ScreenState(
            screenshot_path=screenshot,
            layout_json=layout or '',
            layout_elements=elements,
            step_index=step,
        )


class ScreenState:
    """一次完整的屏幕状态快照（截图 + 布局）"""
    def __init__(self, screenshot_path: str = '', layout_json: str = '',
                 layout_elements: list = None, step_index: int = 0):
        self.screenshot_path = screenshot_path
        self.layout_json = layout_json
        self.layout_elements = layout_elements or []
        self.step_index = step_index
