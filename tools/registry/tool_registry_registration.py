"""
tools/registry/tool_registry_registration.py
─────────────────────────────────────────────
Centralised registration entry-point for ALL tool modules.

Call register_all_tools(registry, event_bus) once during bootstrap
(after the EventBus is live and before any agent is started).

Registers:
  file_tools      — file read/write/search
  code_tools      — code execution/analysis
  memory_tools    — memory store/retrieve
  vision_tools    — screenshot/image analysis
  utility_tools   — text processing, math, date/time
  web_tools       — HTTP requests, web search (web.search, web.scrape, …)
  system_tools    — process management, system info
  apps.*          — launch/close desktop apps
  browser.*/web.* — browser automation + URL tools
  desktop_tools   — mouse/keyboard/window/clipboard control
  media_tools     — system media playback + volume control (media.*)  ← NEW

Architecture:
  bootstrap.py / on_startup()
    ↓
  register_all_tools(registry, event_bus, media_service=STATE.media_service)
    ↓
  ToolRegistry  (all tools registered)
    ↓
  Agent.invoke("web.search", query="hello world")
  Agent.invoke("media.volume_up", step=10)
"""

from __future__ import annotations

import logging
from typing import Any

log = logging.getLogger(__name__)


def register_all_tools(
    registry,                       # ToolRegistry instance
    event_bus:    Any = None,
    media_service: Any = None,      # MediaService instance (optional)
) -> dict[str, list[str]]:
    """
    Register every tool module in one call.

    Args:
        registry:       A ToolRegistry instance.
        event_bus:      Optional EventBus; forwarded to each tool module.
        media_service:  Optional MediaService; required for media.* tools.
                        If None, media tools are still registered but return
                        an error result at call-time (non-fatal boot).

    Returns:
        dict mapping module name → list of registered tool names.
    """
    registered: dict[str, list[str]] = {}

    # Inject event_bus onto registry so invoke() can emit tool.invoked telemetry
    if event_bus is not None:
        registry._event_bus = event_bus

    # ── file_tools ────────────────────────────────────────────────────
    try:
        from tools.file_tools.file_tools import register_file_tools
        names = register_file_tools(registry, event_bus=event_bus)
        registered["file_tools"] = names
        log.info("[OK] File tools registered: %s", names)
    except Exception as exc:
        log.error("[FAIL] File tools registration failed: %s", exc, exc_info=True)
        registered["file_tools"] = []

    # ── code_tools ────────────────────────────────────────────────────
    try:
        from tools.code_tools.code_tools import register_code_tools
        names = register_code_tools(registry, event_bus=event_bus)
        registered["code_tools"] = names
        log.info("[OK] Code tools registered: %s", names)
    except Exception as exc:
        log.error("[FAIL] Code tools registration failed: %s", exc, exc_info=True)
        registered["code_tools"] = []

    # ── memory_tools ──────────────────────────────────────────────────
    try:
        from tools.memory_tools.memory_tools import register_memory_tools
        names = register_memory_tools(registry, event_bus=event_bus)
        registered["memory_tools"] = names
        log.info("[OK] Memory tools registered: %s", names)
    except Exception as exc:
        log.error("[FAIL] Memory tools registration failed: %s", exc, exc_info=True)
        registered["memory_tools"] = []

    # ── vision_tools ──────────────────────────────────────────────────
    try:
        from tools.vision_tools.vision_tools import register_vision_tools
        names = register_vision_tools(registry, event_bus=event_bus)
        registered["vision_tools"] = names
        log.info("[OK] Vision tools registered: %s", names)
    except Exception as exc:
        log.error("[FAIL] Vision tools registration failed: %s", exc, exc_info=True)
        registered["vision_tools"] = []

    # ── utility_tools ─────────────────────────────────────────────────
    try:
        from tools.utility_tools.utility_tools import register_utility_tools
        names = register_utility_tools(registry, event_bus=event_bus)
        registered["utility_tools"] = names
        log.info("[OK] Utility tools registered: %s", names)
    except Exception as exc:
        log.error("[FAIL] Utility tools registration failed: %s", exc, exc_info=True)
        registered["utility_tools"] = []

    # ── web_tools ─────────────────────────────────────────────────────
    # web.search, web.scrape, web.extract_text, web.download, web.summarize
    # Already fully built and correct — verified against zip in Phase 4 review.
    try:
        from tools.web_tools.web_tools import register_web_tools
        names = register_web_tools(registry, event_bus=event_bus)
        registered["web_tools"] = names
        log.info("[OK] Web tools registered: %s", names)
    except Exception as exc:
        log.error("[FAIL] Web tools registration failed: %s", exc, exc_info=True)
        registered["web_tools"] = []

    # ── system_tools ──────────────────────────────────────────────────
    try:
        from tools.system_tools.system_tools import register_system_tools
        names = register_system_tools(registry, event_bus=event_bus)
        registered["system_tools"] = names
        log.info("[OK] System tools registered: %s", names)
    except Exception as exc:
        log.error("[FAIL] System tools registration failed: %s", exc, exc_info=True)
        registered["system_tools"] = []

    # ── apps.* tools ──────────────────────────────────────────────────
    try:
        from tools.system_tools.apps_tool import register_apps_tools
        names = register_apps_tools(registry, event_bus=event_bus)
        registered["apps"] = names
        log.info("[OK] Apps tools registered: %s", names)
    except Exception as exc:
        log.error("[FAIL] Apps tools registration failed: %s", exc, exc_info=True)
        registered["apps"] = []

    # ── browser.* + web.* tools ───────────────────────────────────────
    try:
        from tools.browser_tools.browser_tools import register_browser_tools
        names = register_browser_tools(registry, event_bus=event_bus)
        registered["browser"] = names
        log.info("[OK] Browser/Web tools registered: %s", names)
    except Exception as exc:
        log.error("[FAIL] Browser/Web tools registration failed: %s", exc, exc_info=True)
        registered["browser"] = []

    # ── desktop_tools ─────────────────────────────────────────────────
    try:
        from tools.desktop_tools.desktop_tools import register_desktop_tools
        names = register_desktop_tools(registry, event_bus=event_bus)
        registered["desktop_tools"] = names
        log.info("[OK] Desktop tools registered: %s", names)
    except Exception as exc:
        log.error("[FAIL] Desktop tools registration failed: %s", exc, exc_info=True)
        registered["desktop_tools"] = []

    # ── media_tools ───────────────────────────────────────────────────
    # media.play, media.pause, media.stop, media.next_track,
    # media.previous_track, media.volume_up, media.volume_down,
    # media.set_volume, media.mute, media.unmute, media.get_state
    #
    # Requires MediaService to be started before this call so that
    # the service instance can be passed in.  If media_service is None,
    # tools are registered but return an error result at call-time.
    try:
        from actions.media.media_service import register_media_tools
        names = register_media_tools(
            registry,
            service=   media_service,
            event_bus= event_bus,
        )
        registered["media_tools"] = names
        if media_service is not None:
            log.info("[OK] Media tools registered (service live): %s", names)
        else:
            log.warning(
                "[OK] Media tools registered (no service — tools will error at call-time): %s",
                names,
            )
    except Exception as exc:
        log.error("[FAIL] Media tools registration failed: %s", exc, exc_info=True)
        registered["media_tools"] = []

    # ── agro_tools ────────────────────────────────────────────────────────
    # agro.log_job, agro.update_job, agro.log_fuel, agro.log_expense,
    # agro.daily_report, agro.get_jobs, agro.get_stats,
    # agro.analytics, agro.top_customers, agro.outstanding
    try:
        from tools.agro_tools.agro_tools import register_agro_tools
        names = register_agro_tools(registry, event_bus=event_bus)
        registered["agro_tools"] = names
        log.info("[OK] Agro tools registered: %s", names)
    except Exception as exc:
        log.error("[FAIL] Agro tools registration failed: %s", exc, exc_info=True)
        registered["agro_tools"] = []

    total = sum(len(v) for v in registered.values())
    log.info(
        "Tool registration complete — %d tools registered across %d modules.",
        total, len(registered),
    )
    return registered
