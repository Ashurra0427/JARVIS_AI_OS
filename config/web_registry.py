"""
config/web_registry.py
──────────────────────
Loads and indexes URL aliases from config/web.yaml.

Schema (web.yaml):
  urls:
    <alias>:
      url: str

Public API:
  load_urls()        → None          Loads YAML; call once at startup.
  get_url(name)      → str | None    Resolve alias → URL.
  list_urls()        → list[dict]    All registered URL entries.
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

_urls: dict[str, dict[str, Any]] = {}  # alias (lower) → {alias, url}
_loaded: bool = False

_DEFAULT_YAML = Path(__file__).parent / "web.yaml"


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


def load_urls(yaml_path: str | Path | None = None) -> None:
    """
    Parse web.yaml and populate the in-memory URL registry.

    Raises:
        FileNotFoundError   if the YAML file does not exist.
        ValueError          if the YAML schema is invalid.
    """
    global _loaded

    path = Path(yaml_path) if yaml_path else _DEFAULT_YAML
    if not path.exists():
        raise FileNotFoundError(f"[WebRegistry] web.yaml not found at: {path}")

    with path.open("r", encoding="utf-8") as fh:
        raw = yaml.safe_load(fh)

    if not isinstance(raw, dict) or "urls" not in raw:
        raise ValueError(
            f"[WebRegistry] Invalid schema in {path}: "
            "expected top-level 'urls' mapping."
        )

    urls_raw = raw["urls"]
    if not isinstance(urls_raw, dict):
        raise ValueError(
            f"[WebRegistry] 'urls' key in {path} must be a mapping, "
            f"got {type(urls_raw).__name__}."
        )

    _urls.clear()

    for alias, entry in urls_raw.items():
        if not isinstance(entry, dict) or "url" not in entry:
            log.warning("Skipping malformed URL entry '%s': missing 'url' key.", alias)
            continue

        url = entry["url"].strip()
        if not url.startswith(("http://", "https://")):
            log.warning("URL for alias '%s' has no scheme — adding https://", alias)
            url = "https://" + url

        _urls[alias.lower()] = {
            "alias": alias,
            "url": url,
        }

    _loaded = True
    log.info("[OK] Web Registry Loaded  (%d URLs)", len(_urls))


def get_url(name: str) -> str | None:
    """
    Resolve an alias to its URL string.

    Returns None if the alias is not registered.
    """
    _ensure_loaded()
    record = _urls.get(name.lower())
    return record["url"] if record else None


def list_urls() -> list[dict[str, Any]]:
    """Return a copy of all registered URL records."""
    _ensure_loaded()
    return list(_urls.values())


# ---------------------------------------------------------------------------
# Private helpers
# ---------------------------------------------------------------------------


def _ensure_loaded() -> None:
    if not _loaded:
        load_urls()
