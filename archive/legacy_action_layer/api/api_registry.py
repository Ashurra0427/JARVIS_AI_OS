"""
JARVIS AI OS — API Registry
==============================
Production registry for external API endpoint configurations.

Architecture role:
  APIRegistry is the single source of truth for all named external APIs.
  APIManager resolves configs from this registry before delegating
  execution to APIExecutor. No component calls an external API without
  a config retrieved through APIRegistry.

Responsibilities:
  - Define AuthConfig (auth strategy per API)
  - Define APIEndpointConfig (full endpoint specification)
  - Maintain a named registry of APIEndpointConfig instances
  - Provide register / unregister / get / exists / list_names
  - Ship a register_defaults() that wires well-known public APIs

Design conventions:
  - Consistent with ServiceRegistry (kernel/registry/service_registry.py)
    and AgentRegistry (kernel/registry/agent_registry.py) patterns:
    dataclasses for descriptors, plain dict storage, clean public API.
  - Thread-safe via threading.RLock (sync registry; no async required
    because registration is a startup / configuration concern).
  - AuthConfig is a dataclass, not a sub-class hierarchy, keeping
    serialisation and introspection trivial.
  - register_defaults() is idempotent; calling it twice is safe.
"""

from __future__ import annotations

import os
import threading
from dataclasses import dataclass, field
from typing import Any

from observability.logging.logger import get_logger

log = get_logger(__name__)


# ---------------------------------------------------------------------------
# AuthConfig
# ---------------------------------------------------------------------------


@dataclass
class AuthConfig:
    """
    Authentication strategy for a single API endpoint.

    Supported types
    ---------------
    "none"        — No authentication.
    "bearer"      — Authorization: Bearer <token>
    "api_key"     — A named header carries the key, e.g. X-Api-Key.
    "basic"       — HTTP Basic Auth (username + password).
    "oauth2"      — Bearer token obtained via OAuth2 client-credentials
                    flow; token is refreshed automatically by the executor.

    Fields
    ------
    type          Required.  One of the strings above.
    token         Bearer / OAuth2 access token (or static API key value
                  when type is "bearer"/"oauth2" and the token is known
                  at config time).
    api_key       Raw key value for type "api_key".
    header_name   Header that carries the api_key (default: "X-Api-Key").
    username      HTTP Basic username.
    password      HTTP Basic password.
    token_url     OAuth2 token endpoint URL.
    client_id     OAuth2 client identifier.
    client_secret OAuth2 client secret.
    scopes        OAuth2 requested scopes.
    extra         Catch-all for provider-specific fields.
    """

    type: str = "none"

    # bearer / oauth2 / static token
    token: str | None = None

    # api_key
    api_key: str | None = None
    header_name: str = "X-Api-Key"

    # basic
    username: str | None = None
    password: str | None = None

    # oauth2
    token_url: str | None = None
    client_id: str | None = None
    client_secret: str | None = None
    scopes: list[str] = field(default_factory=list)

    # provider-specific overrides
    extra: dict[str, Any] = field(default_factory=dict)

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------

    def build_auth_headers(self) -> dict[str, str]:
        """
        Return the HTTP headers required to authenticate this request.
        For OAuth2, assumes the token field has already been populated
        (token refresh is the executor's responsibility).
        """
        t = self.type.lower()

        if t == "none":
            return {}

        if t == "bearer":
            if self.token:
                return {"Authorization": f"Bearer {self.token}"}
            return {}

        if t == "api_key":
            key = self.api_key or self.token or ""
            if key:
                return {self.header_name: key}
            return {}

        if t == "basic":
            import base64

            creds = f"{self.username or ''}:{self.password or ''}"
            encoded = base64.b64encode(creds.encode()).decode()
            return {"Authorization": f"Basic {encoded}"}

        if t == "oauth2":
            if self.token:
                return {"Authorization": f"Bearer {self.token}"}
            return {}

        log.warning("AuthConfig.build_auth_headers: unknown auth type", type=t)
        return {}

    def as_dict(self) -> dict[str, Any]:
        return {
            "type": self.type,
            "header_name": self.header_name,
            "has_token": bool(self.token),
            "has_api_key": bool(self.api_key),
            "has_basic": bool(self.username),
            "token_url": self.token_url,
            "client_id": self.client_id,
            "scopes": self.scopes,
        }


# ---------------------------------------------------------------------------
# APIEndpointConfig
# ---------------------------------------------------------------------------


@dataclass
class APIEndpointConfig:
    """
    Full configuration for a named external API.

    Fields
    ------
    name              Unique registry key  (e.g. "openai", "github").
    base_url          Root URL             (e.g. "https://api.openai.com/v1").
    auth              AuthConfig instance that describes how to authenticate.
    default_headers   Static headers sent with every request to this API.
    default_timeout   Per-request timeout in seconds (default 30 s).
    max_retries       How many times to retry transient failures (default 3).
    retry_backoff     Base back-off in seconds for exponential retry (default 1.0).
    rate_limit_rps    Max sustained requests per second (default 10).
    tags              Free-form labels for grouping / lookup.
    metadata          Arbitrary provider-specific data.
    """

    name: str
    base_url: str
    auth: AuthConfig = field(default_factory=AuthConfig)

    default_headers: dict[str, str] = field(default_factory=dict)
    default_timeout: float = 30.0
    max_retries: int = 3
    retry_backoff: float = 1.0
    rate_limit_rps: float = 10.0

    tags: list[str] = field(default_factory=list)
    metadata: dict[str, Any] = field(default_factory=dict)

    # ------------------------------------------------------------------
    # URL helpers
    # ------------------------------------------------------------------

    def resolve_url(self, path: str = "") -> str:
        """
        Construct the full request URL from base_url and a relative path.

        Examples
        --------
        config.resolve_url("/chat/completions")
        → "https://api.openai.com/v1/chat/completions"

        config.resolve_url("")
        → "https://api.openai.com/v1"
        """
        base = self.base_url.rstrip("/")
        if not path:
            return base
        if path.startswith("http://") or path.startswith("https://"):
            return path  # caller supplied an absolute URL
        return f"{base}/{path.lstrip('/')}"

    # ------------------------------------------------------------------
    # Header helpers
    # ------------------------------------------------------------------

    def build_headers(self, extra: dict[str, str] | None = None) -> dict[str, str]:
        """
        Merge default_headers, auth headers, and any caller-supplied extra headers.
        Caller headers take precedence over defaults; auth headers take lowest
        precedence so they can be overridden in edge cases.
        """
        headers: dict[str, str] = {}
        headers.update(self.auth.build_auth_headers())  # lowest priority
        headers.update(self.default_headers)  # registry defaults
        if extra:
            headers.update(extra)  # caller overrides
        return headers

    # ------------------------------------------------------------------
    # Serialisation
    # ------------------------------------------------------------------

    def as_dict(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "base_url": self.base_url,
            "auth": self.auth.as_dict(),
            "default_timeout": self.default_timeout,
            "max_retries": self.max_retries,
            "retry_backoff": self.retry_backoff,
            "rate_limit_rps": self.rate_limit_rps,
            "tags": self.tags,
        }


# ---------------------------------------------------------------------------
# APIRegistry
# ---------------------------------------------------------------------------


class APIRegistry:
    """
    Named registry of APIEndpointConfig instances.

    Thread-safe.  Designed for use during startup (register_defaults) and
    at runtime (dynamic registration by plugins or the APIManager).

    Usage
    -----
        registry = APIRegistry()
        registry.register_defaults()
        config = registry.get("openai")
        response = await executor.request(config=config, ...)

    Consistency with peer registries
    ---------------------------------
    Mirrors the public surface of ServiceRegistry and AgentRegistry:
      register / unregister / get / exists / list_names
    Uses threading.RLock (not asyncio.Lock) because registration is a
    synchronous, startup-time concern — consistent with ServiceRegistry.
    """

    def __init__(self) -> None:
        self._configs: dict[str, APIEndpointConfig] = {}
        self._lock = threading.RLock()
        self._defaults_registered = False

    # ------------------------------------------------------------------
    # Registration
    # ------------------------------------------------------------------

    def register(self, config: APIEndpointConfig) -> None:
        """
        Register an APIEndpointConfig under config.name.
        Silently replaces any existing entry with the same name.
        """
        with self._lock:
            existed = config.name in self._configs
            self._configs[config.name] = config
        action = "updated" if existed else "registered"
        log.info(
            f"APIRegistry: endpoint {action}",
            name=config.name,
            base_url=config.base_url,
            auth_type=config.auth.type,
            tags=config.tags,
        )

    def unregister(self, name: str) -> None:
        """Remove a registered API by name. No-op if not found."""
        with self._lock:
            removed = self._configs.pop(name, None)
        if removed:
            log.info("APIRegistry: endpoint unregistered", name=name)
        else:
            log.debug("APIRegistry.unregister: name not found", name=name)

    # ------------------------------------------------------------------
    # Lookup
    # ------------------------------------------------------------------

    def get(self, name: str) -> APIEndpointConfig | None:
        """Return the config for *name*, or None if not registered."""
        with self._lock:
            return self._configs.get(name)

    def require(self, name: str) -> APIEndpointConfig:
        """
        Return the config for *name* or raise KeyError.
        Prefer this over get() in contexts where a missing entry is a
        programming error, not a recoverable runtime condition.
        """
        config = self.get(name)
        if config is None:
            raise KeyError(
                f"APIRegistry: '{name}' is not registered. "
                f"Available: {self.list_names()}"
            )
        return config

    def exists(self, name: str) -> bool:
        """Return True if *name* is registered."""
        with self._lock:
            return name in self._configs

    def list_names(self) -> list[str]:
        """Return a sorted list of all registered API names."""
        with self._lock:
            return sorted(self._configs.keys())

    def snapshot(self) -> dict[str, dict[str, Any]]:
        """Serialisable view of all registered endpoints (no secrets)."""
        with self._lock:
            return {name: cfg.as_dict() for name, cfg in self._configs.items()}

    # ------------------------------------------------------------------
    # Default endpoints
    # ------------------------------------------------------------------

    def register_defaults(self) -> None:
        """
        Register well-known external API endpoints.

        Credentials are resolved from environment variables at call time;
        missing credentials result in type="none" auth so the registry
        remains fully functional even in environments without API keys.

        Idempotent — safe to call more than once.
        """
        with self._lock:
            if self._defaults_registered:
                return
            self._defaults_registered = True

        defaults: list[APIEndpointConfig] = [
            # ----------------------------------------------------------
            # OpenAI
            # ----------------------------------------------------------
            APIEndpointConfig(
                name="openai",
                base_url="https://api.openai.com/v1",
                auth=AuthConfig(
                    type="bearer",
                    token=os.environ.get("OPENAI_API_KEY"),
                ),
                default_headers={"Content-Type": "application/json"},
                default_timeout=60.0,
                max_retries=3,
                retry_backoff=1.0,
                rate_limit_rps=20.0,
                tags=["llm", "openai", "chat", "embeddings"],
            ),
            # ----------------------------------------------------------
            # Anthropic
            # ----------------------------------------------------------
            APIEndpointConfig(
                name="anthropic",
                base_url="https://api.anthropic.com/v1",
                auth=AuthConfig(
                    type="api_key",
                    api_key=os.environ.get("ANTHROPIC_API_KEY"),
                    header_name="x-api-key",
                ),
                default_headers={
                    "Content-Type": "application/json",
                    "anthropic-version": "2023-06-01",
                },
                default_timeout=120.0,
                max_retries=3,
                retry_backoff=1.0,
                rate_limit_rps=10.0,
                tags=["llm", "anthropic", "claude", "chat"],
            ),
            # ----------------------------------------------------------
            # GitHub REST API
            # ----------------------------------------------------------
            APIEndpointConfig(
                name="github",
                base_url="https://api.github.com",
                auth=AuthConfig(
                    type="bearer",
                    token=os.environ.get("GITHUB_TOKEN"),
                ),
                default_headers={
                    "Accept": "application/vnd.github+json",
                    "X-GitHub-Api-Version": "2022-11-28",
                },
                default_timeout=30.0,
                max_retries=2,
                retry_backoff=1.0,
                rate_limit_rps=5.0,
                tags=["vcs", "github", "code"],
            ),
            # ----------------------------------------------------------
            # Serper (web search)
            # ----------------------------------------------------------
            APIEndpointConfig(
                name="serper",
                base_url="https://google.serper.dev",
                auth=AuthConfig(
                    type="api_key",
                    api_key=os.environ.get("SERPER_API_KEY"),
                    header_name="X-API-KEY",
                ),
                default_headers={"Content-Type": "application/json"},
                default_timeout=15.0,
                max_retries=2,
                retry_backoff=0.5,
                rate_limit_rps=5.0,
                tags=["search", "web", "serper"],
            ),
            # ----------------------------------------------------------
            # ElevenLabs (text-to-speech)
            # ----------------------------------------------------------
            APIEndpointConfig(
                name="elevenlabs",
                base_url="https://api.elevenlabs.io/v1",
                auth=AuthConfig(
                    type="api_key",
                    api_key=os.environ.get("ELEVENLABS_API_KEY"),
                    header_name="xi-api-key",
                ),
                default_headers={"Content-Type": "application/json"},
                default_timeout=45.0,
                max_retries=2,
                retry_backoff=1.0,
                rate_limit_rps=3.0,
                tags=["tts", "audio", "elevenlabs"],
            ),
            # ----------------------------------------------------------
            # Weather (Open-Meteo — no auth required)
            # ----------------------------------------------------------
            APIEndpointConfig(
                name="open_meteo",
                base_url="https://api.open-meteo.com/v1",
                auth=AuthConfig(type="none"),
                default_timeout=10.0,
                max_retries=2,
                retry_backoff=0.5,
                rate_limit_rps=10.0,
                tags=["weather", "open_meteo"],
            ),
            # ----------------------------------------------------------
            # Wolfram Alpha
            # ----------------------------------------------------------
            APIEndpointConfig(
                name="wolfram",
                base_url="https://api.wolframalpha.com/v2",
                auth=AuthConfig(
                    type="api_key",
                    api_key=os.environ.get("WOLFRAM_APP_ID"),
                    header_name="X-App-Id",
                ),
                default_timeout=20.0,
                max_retries=2,
                retry_backoff=1.0,
                rate_limit_rps=2.0,
                tags=["math", "knowledge", "wolfram"],
            ),
            # ----------------------------------------------------------
            # NewsAPI
            # ----------------------------------------------------------
            APIEndpointConfig(
                name="newsapi",
                base_url="https://newsapi.org/v2",
                auth=AuthConfig(
                    type="api_key",
                    api_key=os.environ.get("NEWS_API_KEY"),
                    header_name="X-Api-Key",
                ),
                default_timeout=15.0,
                max_retries=2,
                retry_backoff=0.5,
                rate_limit_rps=2.0,
                tags=["news", "media", "newsapi"],
            ),
        ]

        for cfg in defaults:
            self.register(cfg)

        log.info(
            "APIRegistry: defaults registered",
            count=len(defaults),
            names=[c.name for c in defaults],
        )


# ---------------------------------------------------------------------------
# Module-level convenience — mirrors tool_registry.py pattern
# ---------------------------------------------------------------------------

_default_registry: APIRegistry | None = None
_registry_lock = threading.Lock()


def get_registry() -> APIRegistry:
    """Return the process-wide singleton APIRegistry, creating it if needed."""
    global _default_registry
    with _registry_lock:
        if _default_registry is None:
            _default_registry = APIRegistry()
    return _default_registry


__all__ = [
    "AuthConfig",
    "APIEndpointConfig",
    "APIRegistry",
    "get_registry",
]
