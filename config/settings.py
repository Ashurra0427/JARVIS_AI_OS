"""
JARVIS AI OS — Centralized Configuration System
================================================
Single source of truth for all runtime configuration.
Supports layered resolution: defaults → YAML files → environment variables → runtime overrides.
"""

from __future__ import annotations

import os
import threading
from dataclasses import dataclass, field
from enum import Enum
from pathlib import Path
from typing import Any

import yaml



# ---------------------------------------------------------------------------
# P-07: Secret Vault — keyring + .env fallback
# ---------------------------------------------------------------------------

def _load_secret(key: str, *, env_var: str | None = None) -> str | None:
    """
    Resolve a secret using a tiered lookup:

    Priority (highest → lowest):
      1. OS keyring  — system credential store (Keychain / GNOME Keyring / Windows Credential Locker)
      2. Environment variable  — set by the shell or loaded from .env by _load_dotenv()
      3. Returns None — caller decides whether to raise or warn

    The keyring service name is always ``"jarvis_ai_os"`` and the
    username is the canonical ``key`` (e.g. ``"GROQ_API_KEY"``).

    Usage::
        api_key = _load_secret("GROQ_API_KEY")
        if api_key is None:
            raise RuntimeError("GROQ_API_KEY not found in keyring or environment")

    To store a key in the OS keyring from a helper script::
        import keyring
        keyring.set_password("jarvis_ai_os", "GROQ_API_KEY", "<your-key>")
    """
    env_var = env_var or key

    # 1. OS keyring
    try:
        import keyring as _keyring  # type: ignore
        value = _keyring.get_password("jarvis_ai_os", key)
        if value:
            return value
    except Exception:
        # keyring not installed, backend unavailable, or locked — fall through
        pass

    # 2. Environment variable (populated from .env by _load_dotenv earlier)
    return os.getenv(env_var)


def store_secret(key: str, value: str) -> bool:
    """
    Persist a secret in the OS keyring.

    Returns True on success, False if keyring is unavailable.
    Call this from a setup wizard or CLI helper — never hard-code secrets.

    Example::
        from config.settings import store_secret
        store_secret("GROQ_API_KEY", "gsk_...")
    """
    try:
        import keyring as _keyring  # type: ignore
        _keyring.set_password("jarvis_ai_os", key, value)
        return True
    except Exception:
        return False


def delete_secret(key: str) -> bool:
    """Remove a secret from the OS keyring. Returns True on success."""
    try:
        import keyring as _keyring  # type: ignore
        _keyring.delete_password("jarvis_ai_os", key)
        return True
    except Exception:
        return False


# Canonical secret names — used throughout the codebase instead of bare strings
class SecretKey:
    GROQ_API_KEY = "GROQ_API_KEY"
    GEMINI_API_KEY = "GEMINI_API_KEY"
    OPENAI_API_KEY = "OPENAI_API_KEY"
    ANTHROPIC_API_KEY = "ANTHROPIC_API_KEY"
    ELEVEN_LABS_API_KEY = "ELEVEN_LABS_API_KEY"
    JARVIS_MASTER_TOKEN = "JARVIS_MASTER_TOKEN"

# ---------------------------------------------------------------------------
# Enumerations
# ---------------------------------------------------------------------------


class Environment(str, Enum):
    DEVELOPMENT = "development"
    STAGING = "staging"
    PRODUCTION = "production"


class LogLevel(str, Enum):
    DEBUG = "DEBUG"
    INFO = "INFO"
    WARNING = "WARNING"
    ERROR = "ERROR"
    CRITICAL = "CRITICAL"


# ---------------------------------------------------------------------------
# Typed sub-configs (dataclasses for IDE completion + validation)
# ---------------------------------------------------------------------------


@dataclass
class LLMProviderConfig:
    name: str
    api_key_env: str
    model: str
    max_tokens: int = 8192
    temperature: float = 0.7
    timeout_s: int = 30
    max_retries: int = 3
    enabled: bool = True


@dataclass
class STTConfig:
    primary: str = "groq_whisper"
    fallback: str = "faster_whisper"
    language: str = "en"
    sample_rate: int = 16000
    chunk_duration_s: int = 30
    silence_thresh_s: float = 1.5


@dataclass
class TTSConfig:
    primary: str = "edge_tts"
    fallback: str = "kokoro"
    voice: str = "en-US-AndrewMultilingualNeural"
    rate: str = "+0%"
    pitch: str = "+0Hz"


@dataclass
class EventBusConfig:
    max_queue_size: int = 10_000
    worker_threads: int = 4
    deadletter_enabled: bool = True
    replay_enabled: bool = True
    max_replay_age_s: int = 3600


@dataclass
class HealthConfig:
    check_interval_s: int = 30
    degraded_threshold: float = 0.8  # fraction of checks passing
    unhealthy_threshold: float = 0.5
    history_window: int = 10  # last N checks kept


@dataclass
class AgentDefaultsConfig:
    """Typed form of agents.yaml agent_defaults section."""
    max_tokens: int = 1200
    system_prefix: str = (
        "You are a specialist module of JARVIS AI OS, an intelligent "
        "personal assistant. Be concise, accurate, and actionable."
    )
    working_memory_ttl_s: int = 3600
    working_memory_max_items: int = 100


@dataclass
class ModelRouterConfig:
    """Typed form of models.yaml model_router section."""
    default_provider: str = "groq"
    fallback_chain: list = None       # type: ignore[assignment]
    task_routing: dict = None         # type: ignore[assignment]
    max_context_tokens: int = 6000

    def __post_init__(self) -> None:
        if self.fallback_chain is None:
            self.fallback_chain = ["groq", "qwen_local", "gemini", "local"]
        if self.task_routing is None:
            self.task_routing = {}


@dataclass
class LoggingConfig:
    level: str = LogLevel.INFO
    format: str = "json"  # "json" | "text"
    file_enabled: bool = True
    file_path: str = "logs/jarvis.log"
    max_bytes: int = 10 * 1024 * 1024  # 10 MB
    backup_count: int = 5
    console: bool = True


@dataclass
class SystemConfig:
    environment: Environment = Environment.DEVELOPMENT
    project_root: Path = field(default_factory=lambda: Path(__file__).parents[1])
    data_dir: Path = field(default_factory=lambda: Path("datastore"))
    startup_timeout_s: int = 60
    shutdown_timeout_s: int = 30


# ---------------------------------------------------------------------------
# Root config container
# ---------------------------------------------------------------------------


@dataclass
class JarvisConfig:
    system: SystemConfig = field(default_factory=SystemConfig)
    logging: LoggingConfig = field(default_factory=LoggingConfig)
    event_bus: EventBusConfig = field(default_factory=EventBusConfig)
    health: HealthConfig = field(default_factory=HealthConfig)
    stt: STTConfig = field(default_factory=STTConfig)
    tts: TTSConfig = field(default_factory=TTSConfig)

    # Provider map — keyed by provider name
    llm_providers: dict[str, LLMProviderConfig] = field(default_factory=dict)
    agent_defaults: AgentDefaultsConfig = field(default_factory=AgentDefaultsConfig)
    model_router: ModelRouterConfig = field(default_factory=ModelRouterConfig)

    # P2-F: Configurable file size limit (MB) — used by FilePermissions.
    # Override via config YAML (files.max_file_size_mb) or env JARVIS_MAX_FILE_SIZE_MB.
    max_file_size_mb: int = 50


# ---------------------------------------------------------------------------
# ConfigManager — singleton, thread-safe
# ---------------------------------------------------------------------------


class ConfigManager:
    """
    Layered config resolver.

    Resolution order (later wins):
      1. Hardcoded dataclass defaults
      2. YAML files under config/
      3. Environment variables  (prefix JARVIS_)
      4. Runtime overrides via set()
    """

    _instance: "ConfigManager | None" = None
    _lock = threading.Lock()

    def __new__(cls) -> "ConfigManager":
        with cls._lock:
            if cls._instance is None:
                inst = super().__new__(cls)
                inst._config = JarvisConfig()
                inst._raw = {}
                inst._overrides = {}
                inst._loaded = False
                cls._instance = inst
            return cls._instance

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def load(self, config_dir: Path | str = "config") -> None:
        """Load all YAML files, then apply env-var overrides."""
        config_dir = Path(config_dir)
        self._config_dir = config_dir  # retained for save()

        # ── Load .env file BEFORE reading os.getenv() anywhere ──────────
        # Search order: project root .env → config_dir/.env → config_dir/.env.example
        # This is the ONLY place load_dotenv must be called so that all
        # os.getenv() calls in bootstrap, providers, and stt_router see the keys.
        self._load_dotenv(config_dir)

        self._load_yaml_files(config_dir)
        self._apply_env_overrides()
        self._hydrate_providers()
        self._loaded = True

    def save(self, filename: str = "system.yaml") -> None:
        """Persist runtime changes in ``_raw`` back to disk.

        Writes the ``wake_word`` section (and any other runtime mutations to
        ``_raw``) into *config_dir/filename*.  Only keys already present in
        that file are updated; the remaining YAML files are left untouched.
        Falls back silently if the config directory was never set (i.e. if
        ``load()`` was not called first).
        """
        config_dir: Path | None = getattr(self, "_config_dir", None)
        if config_dir is None:
            return
        fpath = config_dir / filename
        try:
            # Read current on-disk content so we only update, not replace
            existing: dict = {}
            if fpath.exists():
                with open(fpath) as fh:
                    existing = yaml.safe_load(fh) or {}
            self._deep_merge(existing, self._raw)
            with open(fpath, "w") as fh:
                yaml.safe_dump(existing, fh, default_flow_style=False, allow_unicode=True)
        except Exception as exc:  # pragma: no cover
            import logging
            logging.getLogger(__name__).warning("ConfigManager.save() failed: %s", exc)

    @staticmethod
    def _load_dotenv(config_dir: Path) -> None:
        """Load .env into os.environ. Silent no-op if python-dotenv not installed."""
        try:
            from dotenv import load_dotenv  # type: ignore
        except ImportError:
            return  # python-dotenv not installed — skip silently

        # Try candidate paths in priority order
        candidates = [
            config_dir.parent / ".env",   # project root (most common)
            config_dir / ".env",           # inside config/
        ]
        for env_file in candidates:
            if env_file.exists():
                load_dotenv(dotenv_path=env_file, override=False)
                # Don't break — let all files contribute (root .env wins via override=False)

    def get(self, key: str, default: Any = None) -> Any:
        """
        Dot-notation key access.
        E.g. get("logging.level"), get("event_bus.max_queue_size")
        """
        # Runtime overrides take precedence
        if key in self._overrides:
            return self._overrides[key]

        parts = key.split(".")
        node: Any = self._raw
        for part in parts:
            if isinstance(node, dict) and part in node:
                node = node[part]
            else:
                return default
        return node

    def set(self, key: str, value: Any) -> None:
        """Runtime override — does not persist to disk."""
        with self._lock:
            self._overrides[key] = value

    @property
    def config(self) -> JarvisConfig:
        if not self._loaded:
            raise RuntimeError(
                "ConfigManager.load() must be called before accessing .config"
            )
        return self._config

    # ------------------------------------------------------------------
    # Internal
    # ------------------------------------------------------------------

    def _load_yaml_files(self, config_dir: Path) -> None:
        yaml_files = [
            "system.yaml",
            "models.yaml",
            "agents.yaml",
            "memory.yaml",
            "tools.yaml",
            "ui.yaml",
        ]
        for fname in yaml_files:
            fpath = config_dir / fname
            if fpath.exists():
                with open(fpath) as fh:
                    data = yaml.safe_load(fh) or {}
                self._deep_merge(self._raw, data)

    def _apply_env_overrides(self) -> None:
        """Map JARVIS_SECTION__KEY env vars into raw dict."""
        prefix = "JARVIS_"
        for env_key, env_val in os.environ.items():
            if not env_key.startswith(prefix):
                continue
            path = env_key[len(prefix) :].lower().split("__")
            node = self._raw
            for segment in path[:-1]:
                node = node.setdefault(segment, {})
            node[path[-1]] = env_val

        # Promote critical env vars with known names
        # P-07: resolve provider keys via vault (keyring → env fallback)
        _env_map = {
            "GEMINI_API_KEY": ("llm_providers", "gemini", "api_key"),
            "GROQ_API_KEY": ("llm_providers", "groq", "api_key"),
        }
        for env_var, dotpath in _env_map.items():
            val = _load_secret(env_var)
            if val:
                node = self._raw
                for seg in dotpath[:-1]:
                    node = node.setdefault(seg, {})
                node[dotpath[-1]] = val

    def _hydrate_providers(self) -> None:
        """Build typed LLMProviderConfig objects from raw dict."""
        providers_raw = self._raw.get("llm_providers", {})
        defaults_map = {
            "gemini": LLMProviderConfig(
                name="gemini",
                api_key_env="GEMINI_API_KEY",
                model="gemini-2.5-flash",
            ),
            "groq": LLMProviderConfig(
                name="groq",
                api_key_env="GROQ_API_KEY",
                model="llama-3.3-70b-versatile",
                max_tokens=4096,
                temperature=0.3,
                timeout_s=15,
            ),
            "local": LLMProviderConfig(
                name="local",
                api_key_env="",
                model="qwen2.5:1.5b",  # PATCHED: llama3→qwen2.5:1.5b (reliable on MX350)
                timeout_s=180,          # PATCHED: give local Ollama headroom
                enabled=True,           # PATCHED: enabled so fallback chain works
            ),
            "qwen_local": LLMProviderConfig(
                name="qwen_local",
                api_key_env="",
                model="phi3:mini",  # PATCHED: openvino dir empty (no IR files) -> use Ollama phi3:mini instead
                timeout_s=45,
                enabled=False,  # PATCHED: disabled until qwen_coder OpenVINO IR files are actually placed
            ),
        }
        for name, defaults in defaults_map.items():
            overrides = providers_raw.get(name, {})
            for k, v in overrides.items():
                if hasattr(defaults, k):
                    setattr(defaults, k, v)
            self._config.llm_providers[name] = defaults

        # Hydrate other typed sections
        log_raw = self._raw.get("logging", {})
        for k, v in log_raw.items():
            if hasattr(self._config.logging, k):
                setattr(self._config.logging, k, v)

        eb_raw = self._raw.get("event_bus", {})
        for k, v in eb_raw.items():
            if hasattr(self._config.event_bus, k):
                setattr(self._config.event_bus, k, v)

        # ── Hydrate stt / tts sections (were silently ignored before) ──────
        stt_raw = self._raw.get("stt", {})
        for k, v in stt_raw.items():
            if hasattr(self._config.stt, k):
                setattr(self._config.stt, k, v)

        tts_raw = self._raw.get("tts", {})
        for k, v in tts_raw.items():
            if hasattr(self._config.tts, k):
                setattr(self._config.tts, k, v)

        # ── health (system.yaml) ── was defined but never hydrated ──────
        health_raw = self._raw.get("health", {})
        for k, v in health_raw.items():
            if hasattr(self._config.health, k):
                setattr(self._config.health, k, v)

        # ── system (system.yaml) ───────────────────────────────────────
        system_raw = self._raw.get("system", {})
        for k, v in system_raw.items():
            if k == "environment":
                try:
                    self._config.system.environment = Environment(v)
                except ValueError:
                    pass
            elif k == "data_dir":
                self._config.system.data_dir = Path(v)
            elif hasattr(self._config.system, k):
                setattr(self._config.system, k, v)

        # ── agent_defaults (agents.yaml) ───────────────────────────────
        ad_raw = self._raw.get("agent_defaults", {})
        for k, v in ad_raw.items():
            if hasattr(self._config.agent_defaults, k):
                setattr(self._config.agent_defaults, k, v)

        # ── model_router (models.yaml) ─────────────────────────────────
        mr_raw = self._raw.get("model_router", {})
        for k, v in mr_raw.items():
            if hasattr(self._config.model_router, k):
                setattr(self._config.model_router, k, v)

    @staticmethod
    def _deep_merge(base: dict, override: dict) -> None:
        for k, v in override.items():
            if k in base and isinstance(base[k], dict) and isinstance(v, dict):
                ConfigManager._deep_merge(base[k], v)
            else:
                base[k] = v


# Module-level singleton accessor
def get_config() -> ConfigManager:
    return ConfigManager()  # Alias: settings.py is the canonical module name