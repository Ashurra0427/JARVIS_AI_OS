"""
config/app_registry.py
──────────────────────
Loads and indexes app definitions from config/apps.yaml.

Schema (apps.yaml):
  apps:
    <key>:
      name: str
      executable: str
      aliases: list[str]   (optional)

Public API:
  load_apps()        → None          Loads YAML; call once at startup.
  get_app(name)      → dict | None   Exact key lookup.
  find_app(alias)    → dict | None   Fuzzy / alias lookup.
  list_apps()        → list[dict]    All registered apps.
"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Any

import yaml

log = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Internal state
# ---------------------------------------------------------------------------

_apps: dict[str, dict[str, Any]] = {}  # canonical_key → entry
_alias_index: dict[str, str] = {}  # alias (lower) → canonical_key
_loaded: bool = False

_DEFAULT_YAML = Path(__file__).parent / "apps.yaml"


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


def load_apps(yaml_path: str | Path | None = None) -> None:
    """
    Parse apps.yaml and populate the in-memory registry.

    Raises:
        FileNotFoundError   if the YAML file does not exist.
        ValueError          if the YAML schema is invalid.
    """
    global _loaded

    path = Path(yaml_path) if yaml_path else _DEFAULT_YAML
    if not path.exists():
        raise FileNotFoundError(f"[AppRegistry] apps.yaml not found at: {path}")

    with path.open("r", encoding="utf-8") as fh:
        raw = yaml.safe_load(fh)

    if not isinstance(raw, dict) or "apps" not in raw:
        raise ValueError(
            f"[AppRegistry] Invalid schema in {path}: "
            "expected top-level 'apps' mapping."
        )

    apps_raw = raw["apps"]
    if not isinstance(apps_raw, dict):
        raise ValueError(
            f"[AppRegistry] 'apps' key in {path} must be a mapping, got {type(apps_raw).__name__}."
        )

    _apps.clear()
    _alias_index.clear()

    for key, entry in apps_raw.items():
        if not isinstance(entry, dict):
            log.warning("Skipping malformed app entry '%s': not a dict.", key)
            continue
        if "executable" not in entry:
            log.warning("Skipping app '%s': missing 'executable' field.", key)
            continue

        record = {
            "key": key,
            "name": entry.get("name", key),
            "executable": entry["executable"],
            "aliases": entry.get("aliases", []),
            "path": entry.get("path", ""),  # optional full path override
            "args": entry.get("args", []),  # optional default args
        }
        _apps[key.lower()] = record

        # Index key itself
        _alias_index[key.lower()] = key.lower()

        # Index name (lower)
        _alias_index[record["name"].lower()] = key.lower()

        # Index each alias
        for alias in record["aliases"]:
            _alias_index[alias.lower()] = key.lower()

    _loaded = True
    log.info("[OK] App Registry Loaded  (%d apps)", len(_apps))


def get_app(name: str) -> dict[str, Any] | None:
    """
    Exact key lookup (case-insensitive).

    Returns the app record dict or None if not found.
    """
    _ensure_loaded()
    return _apps.get(name.lower())


def find_app(alias: str) -> dict[str, Any] | None:
    """
    Resolve an alias, name, or key to an app record.

    Tries:
      1. Exact alias / name / key index.
      2. Executable filename prefix (without extension).
    Returns None if no match.
    """
    _ensure_loaded()
    key = _alias_index.get(alias.lower())
    if key:
        return _apps.get(key)

    # Fallback: match executable prefix
    stub = alias.lower().removesuffix(".exe")
    for record in _apps.values():
        exe_stub = record["executable"].lower().removesuffix(".exe")
        if exe_stub == stub:
            return record

    return None


def list_apps() -> list[dict[str, Any]]:
    """Return a copy of all registered app records."""
    _ensure_loaded()
    return list(_apps.values())


# ---------------------------------------------------------------------------
# Private helpers
# ---------------------------------------------------------------------------


def _ensure_loaded() -> None:
    if not _loaded:
        load_apps()
