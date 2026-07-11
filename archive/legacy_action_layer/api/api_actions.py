"""
JARVIS AI OS — API Actions
============================
Low-level HTTP execution layer.

Provides raw HTTP request execution with:
  - GET / POST / PUT / DELETE / PATCH
  - Configurable timeouts
  - Automatic retries with exponential backoff
  - Auth header injection
  - JSON / binary response handling
  - Structured response typing

Used exclusively by APIManager. Never called directly from agents.
"""

from __future__ import annotations

import asyncio
import time
import uuid
from dataclasses import dataclass, field
from typing import Any

from observability.logging.logger import get_logger

log = get_logger(__name__)


# ---------------------------------------------------------------------------
# HTTP Methods
# ---------------------------------------------------------------------------

HTTP_METHODS = frozenset({"GET", "POST", "PUT", "DELETE", "PATCH", "HEAD", "OPTIONS"})


# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------


@dataclass
class HTTPRequestConfig:
    """Per-request configuration for APIActions."""

    method: str = "GET"
    url: str = ""
    headers: dict[str, str] = field(default_factory=dict)
    params: dict[str, Any] = field(default_factory=dict)  # query params
    body: Any = None  # dict → JSON, bytes → raw, str → text
    timeout_seconds: float = 30.0
    max_retries: int = 3
    retry_backoff: float = 1.0  # base seconds for exponential backoff
    verify_ssl: bool = True
    allow_redirects: bool = True
    request_id: str = field(default_factory=lambda: str(uuid.uuid4()))


# ---------------------------------------------------------------------------
# Response
# ---------------------------------------------------------------------------


@dataclass
class HTTPResponse:
    """Structured HTTP response returned from APIActions."""

    request_id: str
    url: str
    method: str
    status_code: int
    headers: dict[str, str]
    body: Any  # parsed JSON dict/list, or raw bytes, or str
    duration_ms: float
    attempt: int = 1
    success: bool = True
    error: str = ""

    @property
    def is_2xx(self) -> bool:
        return 200 <= self.status_code < 300

    @property
    def is_4xx(self) -> bool:
        return 400 <= self.status_code < 500

    @property
    def is_5xx(self) -> bool:
        return 500 <= self.status_code < 600

    def as_dict(self) -> dict:
        return {
            "request_id": self.request_id,
            "url": self.url,
            "method": self.method,
            "status_code": self.status_code,
            "duration_ms": self.duration_ms,
            "attempt": self.attempt,
            "success": self.success,
            "error": self.error,
        }


# ---------------------------------------------------------------------------
# APIActions
# ---------------------------------------------------------------------------


class APIActions:
    """
    Low-level HTTP execution layer.

    All HTTP operations go through this class.
    Handles connection pooling via a shared aiohttp session,
    retries, timeouts, and response parsing.

    Usage:
        actions = APIActions()
        await actions.start()

        response = await actions.execute(HTTPRequestConfig(
            method="GET",
            url="https://api.example.com/data",
            headers={"Authorization": "Bearer token"},
            timeout_seconds=10.0,
        ))
    """

    # Maximum retries regardless of per-request config
    GLOBAL_MAX_RETRIES = 5
    # Status codes that are retryable
    RETRYABLE_STATUS = frozenset({429, 500, 502, 503, 504})
    # Status codes that should never be retried
    NEVER_RETRY_STATUS = frozenset({400, 401, 403, 404, 405, 422})

    def __init__(
        self,
        default_timeout: float = 30.0,
        max_connections: int = 100,
        max_per_host: int = 30,
        default_user_agent: str = "JARVIS-AI-OS/1.0",
    ) -> None:
        self._default_timeout = default_timeout
        self._max_connections = max_connections
        self._max_per_host = max_per_host
        self._default_ua = default_user_agent
        self._session = None
        self._session_lock = asyncio.Lock()
        self._running = False

        self._stats = {
            "requests": 0,
            "success": 0,
            "failure": 0,
            "retried": 0,
            "total_ms": 0.0,
        }

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------

    async def start(self) -> None:
        if self._running:
            return
        self._running = True
        await self._get_session()
        log.info("APIActions started", max_connections=self._max_connections)

    async def stop(self) -> None:
        if not self._running:
            return
        self._running = False
        async with self._session_lock:
            if self._session and not self._session.closed:
                await self._session.close()
                self._session = None
        log.info("APIActions stopped", stats=self._stats)

    async def _get_session(self):
        import aiohttp

        async with self._session_lock:
            if self._session is None or self._session.closed:
                connector = aiohttp.TCPConnector(
                    limit=self._max_connections,
                    limit_per_host=self._max_per_host,
                    ssl=True,
                )
                self._session = aiohttp.ClientSession(
                    connector=connector,
                    headers={"User-Agent": self._default_ua},
                )
        return self._session

    # ------------------------------------------------------------------
    # Primary execution entry point
    # ------------------------------------------------------------------

    async def execute(self, config: HTTPRequestConfig) -> HTTPResponse:
        """
        Execute an HTTP request with retry logic.
        Returns HTTPResponse on success or failure (never raises).
        """
        method = config.method.upper()
        if method not in HTTP_METHODS:
            return HTTPResponse(
                request_id=config.request_id,
                url=config.url,
                method=method,
                status_code=0,
                headers={},
                body=None,
                duration_ms=0.0,
                success=False,
                error=f"Unsupported HTTP method: {method}",
            )

        self._stats["requests"] += 1
        max_attempts = min(config.max_retries + 1, self.GLOBAL_MAX_RETRIES + 1)

        last_response: HTTPResponse | None = None
        for attempt in range(1, max_attempts + 1):
            if attempt > 1:
                backoff = config.retry_backoff * (2 ** (attempt - 2))
                log.debug(
                    "Retrying API request",
                    request_id=config.request_id,
                    attempt=attempt,
                    backoff_s=round(backoff, 2),
                )
                await asyncio.sleep(backoff)
                self._stats["retried"] += 1

            response = await self._execute_once(config, attempt)
            last_response = response

            if response.success:
                self._stats["success"] += 1
                self._stats["total_ms"] += response.duration_ms
                return response

            # Determine if we should retry
            if response.status_code in self.NEVER_RETRY_STATUS:
                break  # client error — no point retrying
            if (
                response.status_code > 0
                and response.status_code not in self.RETRYABLE_STATUS
            ):
                break  # unexpected status — stop
            if attempt == max_attempts:
                break

        self._stats["failure"] += 1
        if last_response:
            self._stats["total_ms"] += last_response.duration_ms
        return last_response or HTTPResponse(
            request_id=config.request_id,
            url=config.url,
            method=method,
            status_code=0,
            headers={},
            body=None,
            duration_ms=0.0,
            success=False,
            error="All retry attempts exhausted",
        )

    async def _execute_once(
        self, config: HTTPRequestConfig, attempt: int
    ) -> HTTPResponse:
        """Single HTTP request execution attempt."""
        import aiohttp

        t0 = time.monotonic()
        session = await self._get_session()

        # Build request kwargs
        kwargs: dict[str, Any] = {
            "url": config.url,
            "params": config.params or None,
            "headers": config.headers or {},
            "ssl": config.verify_ssl,
            "allow_redirects": config.allow_redirects,
            "timeout": aiohttp.ClientTimeout(total=config.timeout_seconds),
        }

        # Body encoding
        if config.body is not None:
            if isinstance(config.body, dict) or isinstance(config.body, list):
                kwargs["json"] = config.body
            elif isinstance(config.body, bytes):
                kwargs["data"] = config.body
            elif isinstance(config.body, str):
                kwargs["data"] = config.body.encode("utf-8")
            else:
                kwargs["json"] = config.body

        try:
            method = config.method.upper()
            async with session.request(method, **kwargs) as resp:
                duration_ms = (time.monotonic() - t0) * 1000
                resp_headers = dict(resp.headers)
                status_code = resp.status

                # Parse body
                content_type = resp_headers.get("Content-Type", "")
                if "application/json" in content_type:
                    try:
                        body = await resp.json(content_type=None)
                    except Exception:
                        body = await resp.text()
                elif "text/" in content_type:
                    body = await resp.text()
                else:
                    body = await resp.read()

                success = 200 <= status_code < 300
                error = "" if success else f"HTTP {status_code}: {resp.reason}"

                if not success:
                    log.warning(
                        "API request failed",
                        request_id=config.request_id,
                        url=config.url,
                        status=status_code,
                        attempt=attempt,
                    )
                else:
                    log.debug(
                        "API request success",
                        request_id=config.request_id,
                        url=config.url,
                        status=status_code,
                        duration_ms=round(duration_ms, 1),
                    )

                return HTTPResponse(
                    request_id=config.request_id,
                    url=config.url,
                    method=config.method,
                    status_code=status_code,
                    headers=resp_headers,
                    body=body,
                    duration_ms=round(duration_ms, 1),
                    attempt=attempt,
                    success=success,
                    error=error,
                )

        except asyncio.TimeoutError:
            duration_ms = (time.monotonic() - t0) * 1000
            log.warning(
                "API request timed out",
                request_id=config.request_id,
                url=config.url,
                timeout=config.timeout_seconds,
                attempt=attempt,
            )
            return HTTPResponse(
                request_id=config.request_id,
                url=config.url,
                method=config.method,
                status_code=0,
                headers={},
                body=None,
                duration_ms=round(duration_ms, 1),
                attempt=attempt,
                success=False,
                error=f"Request timed out after {config.timeout_seconds:.1f}s",
            )
        except Exception as exc:
            duration_ms = (time.monotonic() - t0) * 1000
            log.error(
                "API request exception",
                request_id=config.request_id,
                url=config.url,
                error=str(exc),
                attempt=attempt,
            )
            return HTTPResponse(
                request_id=config.request_id,
                url=config.url,
                method=config.method,
                status_code=0,
                headers={},
                body=None,
                duration_ms=round(duration_ms, 1),
                attempt=attempt,
                success=False,
                error=str(exc),
            )

    # ------------------------------------------------------------------
    # Convenience methods
    # ------------------------------------------------------------------

    async def get(
        self,
        url: str,
        headers: dict[str, str] | None = None,
        params: dict[str, Any] | None = None,
        timeout: float = 30.0,
        request_id: str = "",
    ) -> HTTPResponse:
        return await self.execute(
            HTTPRequestConfig(
                method="GET",
                url=url,
                headers=headers or {},
                params=params or {},
                timeout_seconds=timeout,
                request_id=request_id or str(uuid.uuid4()),
            )
        )

    async def post(
        self,
        url: str,
        body: Any = None,
        headers: dict[str, str] | None = None,
        timeout: float = 30.0,
        request_id: str = "",
    ) -> HTTPResponse:
        return await self.execute(
            HTTPRequestConfig(
                method="POST",
                url=url,
                headers=headers or {},
                body=body,
                timeout_seconds=timeout,
                request_id=request_id or str(uuid.uuid4()),
            )
        )

    async def put(
        self,
        url: str,
        body: Any = None,
        headers: dict[str, str] | None = None,
        timeout: float = 30.0,
        request_id: str = "",
    ) -> HTTPResponse:
        return await self.execute(
            HTTPRequestConfig(
                method="PUT",
                url=url,
                headers=headers or {},
                body=body,
                timeout_seconds=timeout,
                request_id=request_id or str(uuid.uuid4()),
            )
        )

    async def delete(
        self,
        url: str,
        headers: dict[str, str] | None = None,
        timeout: float = 30.0,
        request_id: str = "",
    ) -> HTTPResponse:
        return await self.execute(
            HTTPRequestConfig(
                method="DELETE",
                url=url,
                headers=headers or {},
                timeout_seconds=timeout,
                request_id=request_id or str(uuid.uuid4()),
            )
        )

    async def patch(
        self,
        url: str,
        body: Any = None,
        headers: dict[str, str] | None = None,
        timeout: float = 30.0,
        request_id: str = "",
    ) -> HTTPResponse:
        return await self.execute(
            HTTPRequestConfig(
                method="PATCH",
                url=url,
                headers=headers or {},
                body=body,
                timeout_seconds=timeout,
                request_id=request_id or str(uuid.uuid4()),
            )
        )

    # ------------------------------------------------------------------
    # Diagnostics
    # ------------------------------------------------------------------

    def stats(self) -> dict:
        total = self._stats["requests"] or 1
        return {
            **self._stats,
            "avg_ms": round(self._stats["total_ms"] / total, 1),
            "success_rate": round(self._stats["success"] / total, 3),
        }
