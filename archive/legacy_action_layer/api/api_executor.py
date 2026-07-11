"""
JARVIS AI OS — API Executor
==============================
Low-level async HTTP executor with retry, timeout, and response normalization.

Responsibilities:
  - Execute HTTP requests (GET, POST, PUT, PATCH, DELETE)
  - Exponential backoff retry on transient errors
  - Timeout enforcement
  - Response normalization to APIResponse
  - Rate limiting (per-API token bucket)

Rules:
  - No event emission here — APIManager handles that
  - No permission checks — APIManager handles that
  - Returns structured APIResponse; never raises
"""

from __future__ import annotations

import asyncio
import time
import uuid
from dataclasses import dataclass, field
from typing import Any

from observability.logging.logger import get_logger
from actions.api.api_registry import APIEndpointConfig

log = get_logger(__name__)

# Transient HTTP status codes that warrant a retry
_RETRY_STATUS_CODES: frozenset[int] = frozenset({429, 500, 502, 503, 504})
_MAX_RETRY_DELAY = 60.0  # seconds


# ---------------------------------------------------------------------------
# Response model
# ---------------------------------------------------------------------------


@dataclass
class APIResponse:
    request_id: str
    api_name: str
    endpoint: str
    method: str
    status_code: int
    success: bool
    data: Any = None
    raw_text: str = ""
    error: str = ""
    duration_ms: float = 0.0
    attempt: int = 1
    headers: dict = field(default_factory=dict)

    def as_dict(self) -> dict:
        return {
            "request_id": self.request_id,
            "api_name": self.api_name,
            "endpoint": self.endpoint,
            "method": self.method,
            "status_code": self.status_code,
            "success": self.success,
            "data": self.data,
            "error": self.error,
            "duration_ms": self.duration_ms,
            "attempt": self.attempt,
        }


# ---------------------------------------------------------------------------
# Rate limiter (simple token bucket per API)
# ---------------------------------------------------------------------------


class _TokenBucket:
    """Minimal async token bucket for per-API rate limiting."""

    def __init__(self, rate_rps: float) -> None:
        self._rate = rate_rps
        self._tokens = rate_rps  # start full
        self._last_fill = time.monotonic()
        self._lock = asyncio.Lock()

    async def acquire(self) -> None:
        async with self._lock:
            now = time.monotonic()
            elapsed = now - self._last_fill
            self._tokens = min(self._rate, self._tokens + elapsed * self._rate)
            self._last_fill = now

            if self._tokens >= 1.0:
                self._tokens -= 1.0
                return

            wait = (1.0 - self._tokens) / self._rate
        await asyncio.sleep(wait)
        async with self._lock:
            self._tokens = max(0.0, self._tokens - 1.0)


# ---------------------------------------------------------------------------
# APIExecutor
# ---------------------------------------------------------------------------


class APIExecutor:
    """
    Async HTTP executor with retry and rate limiting.

    Usage:
        executor = APIExecutor()
        response = await executor.request(
            config=api_registry.require("openai"),
            method="POST",
            path="/chat/completions",
            json_body={"model": "gpt-4o", "messages": [...]},
        )
    """

    def __init__(self) -> None:
        self._rate_limiters: dict[str, _TokenBucket] = {}
        self._session = None  # aiohttp.ClientSession, lazily created

    async def request(
        self,
        *,
        config: APIEndpointConfig,
        method: str,
        path: str = "",
        json_body: Any = None,
        params: dict | None = None,
        headers: dict | None = None,
        timeout: float | None = None,
        max_retries: int | None = None,
        request_id: str | None = None,
    ) -> APIResponse:
        """
        Execute an HTTP request with retry and rate limiting.

        Args:
            config:      APIEndpointConfig from the registry.
            method:      HTTP method string (GET, POST, …).
            path:        Path relative to config.base_url.
            json_body:   JSON-serialisable body (for POST/PUT/PATCH).
            params:      URL query parameters.
            headers:     Extra headers (merged with config defaults).
            timeout:     Request timeout in seconds (overrides config).
            max_retries: Override config.max_retries.
            request_id:  Caller-supplied correlation ID.

        Returns:
            APIResponse — always populated, never raises.
        """
        rid = request_id or str(uuid.uuid4())
        url = config.resolve_url(path)
        all_headers = config.build_headers(headers)
        effective_timeout = timeout or config.default_timeout
        effective_retries = (
            max_retries if max_retries is not None else config.max_retries
        )
        method = method.upper()
        t0 = time.time()

        # Rate limiting
        limiter = self._get_limiter(config)
        await limiter.acquire()

        last_error = ""
        last_status = 0

        for attempt in range(1, effective_retries + 2):  # +2 so 0 retries → 1 attempt
            try:
                response = await self._do_request(
                    method, url, all_headers, json_body, params, effective_timeout
                )
                duration = (time.time() - t0) * 1000

                if (
                    response["status"] not in _RETRY_STATUS_CODES
                    or attempt > effective_retries
                ):
                    return self._build_response(
                        rid,
                        config.name,
                        url,
                        method,
                        response,
                        duration,
                        attempt,
                    )

                # Transient error — retry
                last_status = response["status"]
                last_error = response.get("error", f"HTTP {last_status}")
                delay = min(
                    _MAX_RETRY_DELAY, config.retry_backoff * (2 ** (attempt - 1))
                )
                log.warning(
                    "API transient error, retrying",
                    api=config.name,
                    status=last_status,
                    attempt=attempt,
                    delay=delay,
                )
                await asyncio.sleep(delay)

            except asyncio.TimeoutError:
                last_error = f"Request timed out after {effective_timeout}s"
                log.warning("API timeout", api=config.name, attempt=attempt, url=url)
                if attempt > effective_retries:
                    break
                await asyncio.sleep(config.retry_backoff * attempt)

            except Exception as exc:
                last_error = str(exc)
                log.error(
                    "API request exception",
                    api=config.name,
                    attempt=attempt,
                    error=last_error,
                )
                if attempt > effective_retries:
                    break
                await asyncio.sleep(config.retry_backoff * attempt)

        duration = (time.time() - t0) * 1000
        return APIResponse(
            request_id=rid,
            api_name=config.name,
            endpoint=url,
            method=method,
            status_code=last_status,
            success=False,
            error=last_error,
            duration_ms=duration,
            attempt=effective_retries + 1,
        )

    async def close(self) -> None:
        """Close the underlying HTTP session."""
        if self._session:
            await self._session.close()
            self._session = None

    # ------------------------------------------------------------------
    # Internal
    # ------------------------------------------------------------------

    async def _do_request(
        self,
        method: str,
        url: str,
        headers: dict,
        body: Any,
        params: dict | None,
        timeout: float,
    ) -> dict:
        """Execute a single HTTP request. Returns a raw response dict."""
        try:
            import aiohttp
        except ImportError:
            # Fallback: use urllib (no streaming, less robust)
            return await self._urllib_fallback(
                method, url, headers, body, params, timeout
            )

        session = await self._get_session()
        async with session.request(
            method,
            url,
            headers=headers,
            json=body if body is not None else None,
            params=params,
            timeout=aiohttp.ClientTimeout(total=timeout),
            ssl=True,
        ) as resp:
            try:
                data = await resp.json(content_type=None)
            except Exception:
                data = None
            raw = await resp.text() if data is None else ""
            return {
                "status": resp.status,
                "data": data,
                "raw_text": raw,
                "headers": dict(resp.headers),
            }

    async def _urllib_fallback(
        self,
        method: str,
        url: str,
        headers: dict,
        body: Any,
        params: dict | None,
        timeout: float,
    ) -> dict:
        """Minimal urllib fallback when aiohttp is unavailable."""
        import json as _json
        import urllib.request
        import urllib.parse

        if params:
            url = url + "?" + urllib.parse.urlencode(params)

        data = _json.dumps(body).encode() if body else None
        req = urllib.request.Request(url, data=data, headers=headers, method=method)

        loop = asyncio.get_running_loop()

        def _do():
            try:
                with urllib.request.urlopen(req, timeout=timeout) as resp:
                    raw = resp.read().decode()
                    try:
                        return {
                            "status": resp.status,
                            "data": _json.loads(raw),
                            "raw_text": "",
                            "headers": {},
                        }
                    except Exception:
                        return {
                            "status": resp.status,
                            "data": None,
                            "raw_text": raw,
                            "headers": {},
                        }
            except urllib.error.HTTPError as e:
                return {
                    "status": e.code,
                    "data": None,
                    "raw_text": str(e),
                    "headers": {},
                    "error": str(e),
                }

        return await loop.run_in_executor(None, _do)

    async def _get_session(self):
        if self._session is None or self._session.closed:
            import aiohttp

            self._session = aiohttp.ClientSession()
        return self._session

    def _get_limiter(self, config: APIEndpointConfig) -> _TokenBucket:
        if config.name not in self._rate_limiters:
            self._rate_limiters[config.name] = _TokenBucket(config.rate_limit_rps)
        return self._rate_limiters[config.name]

    def _build_response(
        self,
        rid: str,
        api_name: str,
        url: str,
        method: str,
        raw: dict,
        duration: float,
        attempt: int,
    ) -> APIResponse:
        status = raw.get("status", 0)
        return APIResponse(
            request_id=rid,
            api_name=api_name,
            endpoint=url,
            method=method,
            status_code=status,
            success=200 <= status < 300,
            data=raw.get("data"),
            raw_text=raw.get("raw_text", ""),
            error=raw.get("error", "") if status >= 400 else "",
            duration_ms=duration,
            attempt=attempt,
            headers=raw.get("headers", {}),
        )
