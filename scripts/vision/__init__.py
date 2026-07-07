"""
vision — Built-in vision analysis engine for android-dev-qa.

Direct Google AI API integration (no LiteLLM dependency):
  - gemma-4-31b-it  → screenshot / image analysis
  - gemini-3.1-flash-lite → video understanding

Features:
  - Multi-key rotation (round-robin with per-key rate tracking)
  - Automatic retry on 429 / 503 with exponential backoff
  - Base64 inline image encoding (no URL fetch needed)
  - Structured JSON output parsing
  - Configurable via environment variables or constructor params
"""
from __future__ import annotations

import asyncio
import base64
import json
import logging
import mimetypes
import os
import time
import threading
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional

from google import genai
from google.genai import types

logger = logging.getLogger("android-qa.vision")

# ══════════════════════════════════════════════════════════════════════════
# Configuration
# ══════════════════════════════════════════════════════════════════════════

DEFAULT_IMAGE_MODEL = "gemma-4-31b-it"
DEFAULT_VIDEO_MODEL = "gemini-3.1-flash-lite"
DEFAULT_MAX_RETRIES = 6  # enough to try all keys with some re-attempts
DEFAULT_BASE_DELAY = 2.0  # seconds, doubled each retry


@dataclass
class VisionConfig:
    """Vision engine configuration."""
    image_model: str = DEFAULT_IMAGE_MODEL
    video_model: str = DEFAULT_VIDEO_MODEL
    api_keys: list[str] = field(default_factory=list)
    rpm_per_key: int = 15       # requests per minute per key
    max_retries: int = DEFAULT_MAX_RETRIES
    base_delay: float = DEFAULT_BASE_DELAY
    max_output_tokens: int = 4096
    temperature: float = 0.2

    @classmethod
    def from_env(cls) -> "VisionConfig":
        """Build config from environment variables.

        Env vars:
          QA_VISION_IMAGE_MODEL  (default: gemma-4-31b-it)
          QA_VISION_VIDEO_MODEL  (default: gemini-3.1-flash-lite)
          QA_VISION_API_KEYS     (comma-separated, or auto-discover from litellm env)
          QA_VISION_RPM          (default: 15)
        """
        image_model = os.environ.get("QA_VISION_IMAGE_MODEL", DEFAULT_IMAGE_MODEL)
        video_model = os.environ.get("QA_VISION_VIDEO_MODEL", DEFAULT_VIDEO_MODEL)
        rpm = int(os.environ.get("QA_VISION_RPM", "15"))

        # API keys: explicit > litellm env > hermes env
        keys_str = os.environ.get("QA_VISION_API_KEYS", "")
        if keys_str:
            keys = [k.strip() for k in keys_str.split(",") if k.strip()]
        else:
            keys = _discover_api_keys()

        return cls(
            image_model=image_model,
            video_model=video_model,
            api_keys=keys,
            rpm_per_key=rpm,
        )


def _discover_api_keys() -> list[str]:
    """Auto-discover Google API keys from LiteLLM / Hermes env files."""
    keys: list[str] = []

    # 1. Check current process environment
    for i in range(1, 7):
        k = os.environ.get(f"GOOGLE_API_KEY_{i}", "")
        if k and k not in keys:
            keys.append(k)
    k = os.environ.get("GOOGLE_API_KEY", "")
    if k and k not in keys:
        keys.append(k)
    if keys:
        return keys

    # 2. Try LiteLLM env file
    env_paths = [
        os.path.expanduser("~/.config/litellm/gemini-router.env"),
        os.path.expanduser("~/.hermes/.env"),
    ]
    for env_path in env_paths:
        if not os.path.isfile(env_path):
            continue
        try:
            with open(env_path) as f:
                for line in f:
                    line = line.strip()
                    if not line or line.startswith("#"):
                        continue
                    if "=" not in line:
                        continue
                    name, value = line.split("=", 1)
                    name = name.strip()
                    value = value.strip().strip("'\"")
                    if "GOOGLE_API_KEY" in name and value and value not in keys:
                        keys.append(value)
        except OSError:
            pass
        if keys:
            break

    if not keys:
        logger.warning("No Google API keys found. Vision analysis will be unavailable.")

    return keys


# ══════════════════════════════════════════════════════════════════════════
# Key Rotation
# ══════════════════════════════════════════════════════════════════════════

class _KeyRotator:
    """Round-robin key rotation with per-key rate tracking."""

    def __init__(self, keys: list[str], rpm: int):
        self._keys = keys
        self._rpm = rpm
        self._index = 0
        self._lock = threading.Lock()
        # Per-key: list of recent request timestamps
        self._timestamps: dict[str, list[float]] = {k: [] for k in keys}

    @property
    def has_keys(self) -> bool:
        return bool(self._keys)

    def next_key(self) -> Optional[str]:
        """Get the next available key that hasn't exceeded RPM."""
        if not self._keys:
            return None

        now = time.time()
        with self._lock:
            # Try each key starting from current index
            for _ in range(len(self._keys)):
                key = self._keys[self._index % len(self._keys)]
                self._index += 1

                # Clean old timestamps
                ts = self._timestamps[key]
                self._timestamps[key] = [t for t in ts if now - t < 60]

                # Check RPM
                if len(self._timestamps[key]) < self._rpm:
                    self._timestamps[key].append(now)
                    return key

            # All keys at RPM limit — return least-recently-used key anyway
            # (caller will handle 429 retry)
            key = self._keys[self._index % len(self._keys)]
            self._index += 1
            self._timestamps[key].append(now)
            return key


# ══════════════════════════════════════════════════════════════════════════
# VisionEngine
# ══════════════════════════════════════════════════════════════════════════

class VisionEngine:
    """Built-in vision analysis — direct Google AI API with key rotation.

    Usage:
        engine = VisionEngine()  # auto-config from env
        result = engine.analyze_screenshot("/path/to/screenshot.png", prompt)
        result = engine.analyze_video("/path/to/recording.mp4", prompt)
    """

    def __init__(self, config: Optional[VisionConfig] = None):
        self._config = config or VisionConfig.from_env()
        # Filter out dead keys (403 PERMISSION_DENIED) at init time
        live_keys = self._probe_keys(self._config.api_keys) if self._config.api_keys else []
        self._config.api_keys = live_keys
        self._rotator = _KeyRotator(live_keys, self._config.rpm_per_key)
        self._clients: dict[str, genai.Client] = {}
        # Track keys that fail with 403 at runtime to skip them
        self._dead_keys: set[str] = set()
        if self._rotator.has_keys:
            logger.info(
                "VisionEngine initialized: %d/%d keys live, image=%s, video=%s",
                len(live_keys),
                len(config.api_keys) if config else len(live_keys),
                self._config.image_model,
                self._config.video_model,
            )
        else:
            logger.warning("VisionEngine: no live API keys — vision analysis disabled")

    @property
    def available(self) -> bool:
        """Whether vision analysis is available (has API keys)."""
        return self._rotator.has_keys

    def _probe_keys(self, keys: list[str]) -> list[str]:
        """Quick health check — send a tiny request to each key, return only live ones.

        A key is considered dead only on 403 PERMISSION_DENIED.
        All other outcomes (empty response, 429, 503) are treated as live —
        the key may work for a different request or later.
        """
        live = []
        for key in keys:
            client = genai.Client(api_key=key)
            try:
                resp = client.models.generate_content(
                    model=self._config.image_model,
                    contents=[types.Content(role="user", parts=[types.Part.from_text(text="Say OK")])],
                    config=types.GenerateContentConfig(max_output_tokens=50),
                )
                # Any non-403 response = key is alive
                live.append(key)
            except Exception as e:
                err = str(e)
                if "403" in err or "PERMISSION_DENIED" in err:
                    logger.debug("Key denied (403), skipping")
                else:
                    # 429, 503, etc. — key exists, just rate-limited
                    live.append(key)
        return live

    def _get_client(self, api_key: str) -> genai.Client:
        if api_key not in self._clients:
            self._clients[api_key] = genai.Client(api_key=api_key)
        return self._clients[api_key]

    # ── Core API call with retry ─────────────────────────────────────────

    def _call_with_retry(self, model: str, contents: list[types.Content],
                          system_instruction: Optional[str] = None) -> Optional[str]:
        """Make a generate_content call with key rotation and retry."""
        if not self.available:
            logger.error("VisionEngine: no API keys available")
            return None

        last_error = None
        for attempt in range(self._config.max_retries):
            key = self._rotator.next_key()
            if not key:
                break

            client = self._get_client(key)
            try:
                config = types.GenerateContentConfig(
                    max_output_tokens=self._config.max_output_tokens,
                    temperature=self._config.temperature,
                )
                if system_instruction:
                    config.system_instruction = system_instruction

                response = client.models.generate_content(
                    model=model,
                    contents=contents,
                    config=config,
                )

                if response.text:
                    return response.text
                # Empty response — might be safety filter
                if response.candidates and response.candidates[0].finish_reason:
                    reason = response.candidates[0].finish_reason
                    logger.warning("Empty response, finish_reason=%s", reason)
                return None

            except Exception as e:
                error_str = str(e)
                last_error = e
                # 403 = key dead — mark and try next key
                if "403" in error_str or "PERMISSION_DENIED" in error_str:
                    logger.warning("Key denied (403), marking dead and trying next key")
                    self._dead_keys.add(key)
                    continue
                # 429 = rate limited, 503 = overloaded — retry with backoff
                if "429" in error_str or "RESOURCE_EXHAUSTED" in error_str:
                    delay = self._config.base_delay * (2 ** attempt)
                    logger.warning("Rate limited (attempt %d), retrying in %.1fs", attempt + 1, delay)
                    time.sleep(delay)
                    continue
                if "503" in error_str or "OVERLOADED" in error_str:
                    delay = self._config.base_delay * (2 ** attempt)
                    logger.warning("Service overloaded (attempt %d), retrying in %.1fs", attempt + 1, delay)
                    time.sleep(delay)
                    continue
                # Non-retryable error
                logger.error("Vision API error: %s", error_str)
                break

        if last_error and "response_mime_type" in str(last_error).lower():
            # Fallback: try without JSON mode if structured output fails
            logger.info("Retrying without JSON mode constraint")
            return self._call_text_fallback(model, contents, system_instruction)

        return None

    def _call_text_fallback(self, model: str, contents: list[types.Content],
                             system_instruction: Optional[str] = None) -> Optional[str]:
        """Fallback: call without JSON mode, parse response manually."""
        key = self._rotator.next_key()
        if not key:
            return None
        client = self._get_client(key)
        try:
            config = types.GenerateContentConfig(
                max_output_tokens=self._config.max_output_tokens,
                temperature=self._config.temperature,
            )
            if system_instruction:
                config.system_instruction = system_instruction
            response = client.models.generate_content(
                model=model, contents=contents, config=config,
            )
            return response.text
        except Exception as e:
            logger.error("Vision API fallback error: %s", e)
            return None

    # ── Image encoding ───────────────────────────────────────────────────

    @staticmethod
    def _encode_image(path: str) -> types.Part:
        """Encode a local image file as inline base64 Part."""
        mime_type, _ = mimetypes.guess_type(path)
        if not mime_type:
            mime_type = "image/png"
        with open(path, "rb") as f:
            data = f.read()
        return types.Part.from_bytes(data=data, mime_type=mime_type)

    @staticmethod
    def _encode_video(path: str) -> types.Part:
        """Encode a local video file as inline Part for Gemini."""
        mime_type, _ = mimetypes.guess_type(path)
        if not mime_type:
            mime_type = "video/mp4"
        with open(path, "rb") as f:
            data = f.read()
        return types.Part.from_bytes(data=data, mime_type=mime_type)

    # ── Public API ───────────────────────────────────────────────────────

    def analyze_screenshot(self, image_path: str, prompt: str,
                            system_instruction: Optional[str] = None) -> Optional[dict]:
        """Analyze a screenshot with the image model (gemma-4-31b-it).

        Returns parsed JSON dict or None on failure.
        """
        if not self.available:
            return None
        if not os.path.isfile(image_path):
            logger.error("Image not found: %s", image_path)
            return None

        logger.info("Analyzing screenshot: %s (%.1f KB)",
                     os.path.basename(image_path),
                     os.path.getsize(image_path) / 1024)

        image_part = self._encode_image(image_path)
        contents = [
            types.Content(role="user", parts=[
                image_part,
                types.Part.from_text(text=prompt),
            ]),
        ]

        raw = self._call_with_retry(
            model=self._config.image_model,
            contents=contents,
            system_instruction=system_instruction,
        )

        if not raw:
            return None

        # Parse JSON
        from analysis import AIAnalyzer
        parsed = AIAnalyzer.parse_ai_response(raw)
        if parsed:
            return parsed
        # If parsing failed, wrap raw text
        return {"raw_response": raw, "parse_error": True}

    def analyze_video(self, video_path: str, prompt: str,
                       system_instruction: Optional[str] = None) -> Optional[dict]:
        """Analyze a video with the video model (gemini-3.1-flash-lite).

        Returns parsed JSON dict or None on failure.
        """
        if not self.available:
            return None
        if not os.path.isfile(video_path):
            logger.error("Video not found: %s", video_path)
            return None

        size_mb = os.path.getsize(video_path) / (1024 * 1024)
        logger.info("Analyzing video: %s (%.1f MB)",
                     os.path.basename(video_path), size_mb)

        # Gemini has a ~20MB limit for inline video
        if size_mb > 20:
            logger.warning("Video %.1f MB exceeds 20 MB inline limit, may fail", size_mb)

        video_part = self._encode_video(video_path)
        contents = [
            types.Content(role="user", parts=[
                video_part,
                types.Part.from_text(text=prompt),
            ]),
        ]

        raw = self._call_with_retry(
            model=self._config.video_model,
            contents=contents,
            system_instruction=system_instruction,
        )

        if not raw:
            return None

        from analysis import AIAnalyzer
        parsed = AIAnalyzer.parse_ai_response(raw)
        if parsed:
            return parsed
        return {"raw_response": raw, "parse_error": True}

    def analyze_logcat(self, log_excerpt: str, prompt: str) -> Optional[dict]:
        """Analyze logcat text with the image model (text-only, cheaper)."""
        if not self.available:
            return None

        contents = [
            types.Content(role="user", parts=[
                types.Part.from_text(text=f"{prompt}\n\n## 日志内容\n```\n{log_excerpt}\n```"),
            ]),
        ]

        raw = self._call_with_retry(
            model=self._config.image_model,  # Use image model for text too (cheaper)
            contents=contents,
        )

        if not raw:
            return None

        from analysis import AIAnalyzer
        parsed = AIAnalyzer.parse_ai_response(raw)
        if parsed:
            return parsed
        return {"raw_response": raw, "parse_error": True}

    def batch_analyze_screenshots(self, tasks: list[dict]) -> list[dict]:
        """Analyze multiple screenshots sequentially (respects rate limits).

        Each task: {"path": str, "prompt": str, "step_index": int, "scenario": str}
        Returns list of results with task metadata.
        """
        results = []
        for task in tasks:
            result = self.analyze_screenshot(
                image_path=task["path"],
                prompt=task["prompt"],
            )
            results.append({
                "type": "screenshot",
                "file_path": task["path"],
                "step_index": task.get("step_index", 0),
                "scenario": task.get("scenario", ""),
                "analysis": result,
            })
            # Small delay between requests to be gentle on rate limits
            time.sleep(0.5)
        return results


# ══════════════════════════════════════════════════════════════════════════
# Convenience singleton
# ══════════════════════════════════════════════════════════════════════════

_engine: Optional[VisionEngine] = None
_engine_lock = threading.Lock()


def get_vision_engine() -> VisionEngine:
    """Get or create the global VisionEngine singleton."""
    global _engine
    if _engine is None:
        with _engine_lock:
            if _engine is None:
                _engine = VisionEngine()
    return _engine
