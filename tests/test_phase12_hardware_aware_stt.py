"""
PHASE 12 — hardware-aware local STT sizing tests (roadmap item 8: reliable
on low-resource hardware).

Covers:
  - _detect_hardware_tier() pure logic (cpu_threads capping, model tier
    selection by RAM/CPU) — no faster_whisper needed
  - STTEngine._init_local() actually uses the detected tier, and still
    respects an explicit user override of local_model — faster_whisper is
    faked via sys.modules since it's a heavy optional dependency not
    installed in this environment
"""

from __future__ import annotations

import sys
from types import ModuleType
from unittest.mock import MagicMock, patch

import pytest

from perception.speech.stt import _detect_hardware_tier, _LOCAL_WHISPER_MODEL, STTConfig


class TestDetectHardwareTier:
    def test_cpu_threads_leaves_one_core_free(self):
        with patch("os.cpu_count", return_value=4), \
             patch.dict(sys.modules, {"psutil": None}):
            tier = _detect_hardware_tier()
        assert tier["cpu_threads"] <= 3

    def test_cpu_threads_never_zero_on_single_core(self):
        with patch("os.cpu_count", return_value=1):
            tier = _detect_hardware_tier()
        assert tier["cpu_threads"] >= 1

    def test_cpu_threads_capped_at_four(self):
        with patch("os.cpu_count", return_value=32):
            tier = _detect_hardware_tier()
        assert tier["cpu_threads"] == 4

    def test_two_cores_selects_tiny_model(self):
        with patch("os.cpu_count", return_value=2):
            tier = _detect_hardware_tier()
        assert tier["model"] == "tiny"

    def test_high_ram_high_cpu_selects_default_small(self):
        fake_psutil = ModuleType("psutil")
        fake_psutil.virtual_memory = lambda: MagicMock(total=16 * 1024**3)
        with patch("os.cpu_count", return_value=8), \
             patch.dict(sys.modules, {"psutil": fake_psutil}):
            tier = _detect_hardware_tier()
        assert tier["model"] == _LOCAL_WHISPER_MODEL == "small"

    def test_low_ram_selects_tiny_model(self):
        fake_psutil = ModuleType("psutil")
        fake_psutil.virtual_memory = lambda: MagicMock(total=3 * 1024**3)
        with patch("os.cpu_count", return_value=8), \
             patch.dict(sys.modules, {"psutil": fake_psutil}):
            tier = _detect_hardware_tier()
        assert tier["model"] == "tiny"

    def test_mid_ram_selects_base_model(self):
        fake_psutil = ModuleType("psutil")
        fake_psutil.virtual_memory = lambda: MagicMock(total=6 * 1024**3)
        with patch("os.cpu_count", return_value=8), \
             patch.dict(sys.modules, {"psutil": fake_psutil}):
            tier = _detect_hardware_tier()
        assert tier["model"] == "base"

    def test_psutil_missing_falls_back_gracefully(self):
        """psutil import failure must never raise — degrade to CPU-count-only."""
        real_import = __import__

        def _blocked_import(name, *a, **kw):
            if name == "psutil":
                raise ImportError("no psutil")
            return real_import(name, *a, **kw)

        with patch("os.cpu_count", return_value=8), \
             patch("builtins.__import__", side_effect=_blocked_import):
            tier = _detect_hardware_tier()  # must not raise
        assert tier["ram_gb"] is None
        assert tier["model"] in ("small", "base", "tiny")


class TestInitLocalUsesHardwareTier:
    def _make_engine_with_fake_faster_whisper(self, captured_kwargs: dict):
        fake_module = ModuleType("faster_whisper")

        class _FakeWhisperModel:
            def __init__(self, model_name, **kwargs):
                captured_kwargs["model_name"] = model_name
                captured_kwargs.update(kwargs)

        fake_module.WhisperModel = _FakeWhisperModel

        from perception.speech.stt import STTEngine
        bus = MagicMock()
        engine = STTEngine.__new__(STTEngine)
        engine._bus = bus
        engine._cfg = STTConfig()
        return engine, fake_module

    def test_default_config_uses_detected_tier_model(self):
        captured = {}
        engine, fake_module = self._make_engine_with_fake_faster_whisper(captured)

        with patch.dict(sys.modules, {"faster_whisper": fake_module}), \
             patch("os.cpu_count", return_value=2):
            engine._init_local()

        assert captured["model_name"] == "tiny"
        assert captured["cpu_threads"] == 1

    def test_explicit_local_model_override_is_respected(self):
        captured = {}
        engine, fake_module = self._make_engine_with_fake_faster_whisper(captured)
        engine._cfg = STTConfig(local_model="medium")  # explicit non-default override

        with patch.dict(sys.modules, {"faster_whisper": fake_module}), \
             patch("os.cpu_count", return_value=2):  # even on a 2-core box
            engine._init_local()

        assert captured["model_name"] == "medium"  # override wins over auto-downgrade

    def test_high_end_hardware_keeps_original_default(self):
        captured = {}
        engine, fake_module = self._make_engine_with_fake_faster_whisper(captured)

        fake_psutil = ModuleType("psutil")
        fake_psutil.virtual_memory = lambda: MagicMock(total=32 * 1024**3)

        with patch.dict(sys.modules, {"faster_whisper": fake_module, "psutil": fake_psutil}), \
             patch("os.cpu_count", return_value=16):
            engine._init_local()

        assert captured["model_name"] == "small"
        assert captured["cpu_threads"] == 4

    def test_missing_faster_whisper_returns_none_not_raise(self):
        engine = self._make_engine_with_fake_faster_whisper({})[0]
        with patch.dict(sys.modules, {"faster_whisper": None}):
            result = engine._init_local()
        assert result is None
