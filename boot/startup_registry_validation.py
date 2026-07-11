"""
boot/startup_registry_validation.py
─────────────────────────────────────
Startup validation for the App and Web registries.

Validates:
  1. config/apps.yaml  exists and has valid schema
  2. config/web.yaml   exists and has valid schema
  3. AppRegistry loads without error
  4. WebRegistry loads without error
  5. AppsTool can be imported
  6. Browser web.* tools can be imported

Call validate_registries() early in bootstrap (before any agent is started).
Raises RuntimeError on any hard failure so the boot sequence can abort cleanly.

Emits to EventBus (non-fatal):
  system.registry.validated
  system.registry.failed

Logs:
  [OK] App Registry Loaded
  [OK] Web Registry Loaded
"""

from __future__ import annotations

from kernel.event_bus.event_bus import Event

import logging
import time
from pathlib import Path
from typing import Any

log = logging.getLogger(__name__)

# ── YAML paths (resolved relative to this file) ───────────────────────────
_CONFIG_DIR = Path(__file__).parent.parent / "config"
_APPS_YAML = _CONFIG_DIR / "apps.yaml"
_WEB_YAML = _CONFIG_DIR / "web.yaml"


# ──────────────────────────────────────────────────────────────────────────
# Public API
# ──────────────────────────────────────────────────────────────────────────


def validate_registries(
    event_bus: Any = None,
    raise_on_failure: bool = True,
) -> dict:
    """
    Run all startup registry validations.

    Args:
        event_bus:        Optional EventBus; used to publish validation events.
        raise_on_failure: If True (default) raise RuntimeError on any failure.

    Returns:
        dict with keys:
          success   — bool
          errors    — list of error strings (empty on success)
          elapsed_s — wall-clock time for the validation run
    """
    t0 = time.monotonic()
    errors: list[str] = []

    # ── 1. File existence ─────────────────────────────────────────────
    _check_file(_APPS_YAML, "apps.yaml", errors)
    _check_file(_WEB_YAML, "web.yaml", errors)

    # ── 2. App Registry load ──────────────────────────────────────────
    if not errors or not any("apps.yaml" in e for e in errors):
        _check_app_registry(errors)

    # ── 3. Web Registry load ──────────────────────────────────────────
    if not errors or not any("web.yaml" in e for e in errors):
        _check_web_registry(errors)

    # ── 4. Module imports ─────────────────────────────────────────────
    _check_import("tools.system_tools.apps_tool", "AppsTool", errors)
    _check_import("tools.browser_tools.browser_tools", "browser web tools", errors)

    elapsed = time.monotonic() - t0
    success = len(errors) == 0

    _emit(event_bus, success, errors, elapsed)

    if not success:
        msg = "Registry validation failed:\n" + "\n".join(f"  • {e}" for e in errors)
        log.critical(msg)
        if raise_on_failure:
            raise RuntimeError(msg)
    else:
        log.info("Registry startup validation passed (%.3f s)", elapsed)

    return {"success": success, "errors": errors, "elapsed_s": round(elapsed, 4)}


# ──────────────────────────────────────────────────────────────────────────
# Private helpers
# ──────────────────────────────────────────────────────────────────────────


def _check_file(path: Path, label: str, errors: list[str]) -> None:
    if not path.exists():
        errors.append(f"[MISSING] {label} not found at {path}")
    elif not path.is_file():
        errors.append(f"[INVALID] {label} path exists but is not a file: {path}")
    else:
        log.debug("[OK] %s exists at %s", label, path)


def _check_app_registry(errors: list[str]) -> None:
    try:
        import importlib

        # Force reload to pick up any file changes
        reg_mod = importlib.import_module("config.app_registry")

        # Reset internal state so load_apps() re-reads from disk
        reg_mod._apps.clear()
        reg_mod._alias_index.clear()
        reg_mod._loaded = False

        reg_mod.load_apps(yaml_path=_APPS_YAML)
        apps = reg_mod.list_apps()
        # Message already emitted by load_apps() itself:  [OK] App Registry Loaded
        log.debug("AppRegistry: %d apps registered.", len(apps))
    except FileNotFoundError as exc:
        errors.append(f"[ERROR] AppRegistry: {exc}")
    except ValueError as exc:
        errors.append(f"[SCHEMA] AppRegistry: {exc}")
    except Exception as exc:
        errors.append(f"[ERROR] AppRegistry load failed: {exc}")


def _check_web_registry(errors: list[str]) -> None:
    try:
        import importlib

        reg_mod = importlib.import_module("config.web_registry")

        reg_mod._urls.clear()
        reg_mod._loaded = False

        reg_mod.load_urls(yaml_path=_WEB_YAML)
        urls = reg_mod.list_urls()
        # Message already emitted by load_urls() itself:  [OK] Web Registry Loaded
        log.debug("WebRegistry: %d URLs registered.", len(urls))
    except FileNotFoundError as exc:
        errors.append(f"[ERROR] WebRegistry: {exc}")
    except ValueError as exc:
        errors.append(f"[SCHEMA] WebRegistry: {exc}")
    except Exception as exc:
        errors.append(f"[ERROR] WebRegistry load failed: {exc}")


def _check_import(module_path: str, label: str, errors: list[str]) -> None:
    try:
        import importlib

        importlib.import_module(module_path)
        log.debug("[OK] %s importable (%s)", label, module_path)
    except ImportError as exc:
        errors.append(f"[IMPORT] {label} ({module_path}): {exc}")
    except Exception as exc:
        errors.append(f"[ERROR] {label} ({module_path}): {exc}")


def _emit(bus: Any, success: bool, errors: list, elapsed: float) -> None:
    """Publish a non-fatal event to the EventBus."""
    if bus is None:
        return
    try:
        import asyncio

        payload = {
            "success": success,
            "errors": errors,
            "elapsed_s": round(elapsed, 4),
        }
        event_type = (
            "system.registry.validated" if success else "system.registry.failed"
        )
        try:
            loop = asyncio.get_running_loop()
            if loop.is_running():
                loop.create_task(
                    bus.publish(
                        Event(event_type=event_type, source="startup_registry_validation", payload=payload)
                    )
                )
            else:
                loop.run_until_complete(
                    bus.publish(
                        Event(event_type=event_type, source="startup_registry_validation", payload=payload)
                    )
                )
        except RuntimeError:
            asyncio.run(
                bus.publish(
                    Event(event_type=event_type, source="startup_registry_validation", payload=payload)
                )
            )
    except Exception as exc:
        log.debug("EventBus emit failed (non-fatal): %s", exc)