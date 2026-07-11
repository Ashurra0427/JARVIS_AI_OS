"""
JARVIS AI OS — API Manager
==============================
External API execution orchestrator.

Architecture rule:
  Agents NEVER make HTTP calls directly.
  They publish action requests; APIManager resolves the endpoint
  via APIRegistry, executes via APIExecutor, and publishes results
  via EventBus.

Responsibilities:
  - Lookup API configs from APIRegistry
  - Delegate HTTP execution to APIExecutor
  - Publish api.request.* events
  - Retry coordination (delegated to executor)
  - Register with ServiceRegistry
"""

from __future__ import annotations

import uuid
from typing import Any

from observability.logging.logger import get_logger
from actions.api.api_registry import APIRegistry, APIEndpointConfig
from actions.api.api_executor import APIExecutor, APIResponse
from actions.api.api_events import APIEvents, APICallPayload

log = get_logger(__name__)


class APIManager:
    """
    Production API call manager.

    Usage:
        mgr = APIManager(event_bus=bus, service_registry=registry)
        await mgr.start()

        # Via ActionCoordinator:
        response = await mgr.call(
            api_name="openai",
            method="POST",
            path="/chat/completions",
            body={"model": "gpt-4o", ...},
            requester="agent.research",
        )
    """

    SERVICE_NAME = "actions.api_manager"

    def __init__(
        self,
        event_bus=None,
        service_registry=None,
        api_registry: APIRegistry | None = None,
        register_defaults: bool = True,
    ) -> None:
        self._bus = event_bus
        self._registry = service_registry
        self._running = False

        self._api_registry = api_registry or APIRegistry()
        if register_defaults:
            self._api_registry.register_defaults()

        self._executor = APIExecutor()
        self._stats = {
            "started": 0,
            "completed": 0,
            "failed": 0,
            "retried": 0,
            "rate_limited": 0,
        }

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------

    async def start(self) -> None:
        if self._running:
            return
        self._running = True
        if self._bus:
            self._bus.subscribe("action.api.*", self._handle_action_request)
        if self._registry:
            await self._registry.set_running(self.SERVICE_NAME)
        log.info("APIManager started", registered_apis=self._api_registry.list_names())

    async def stop(self) -> None:
        if not self._running:
            return
        self._running = False
        await self._executor.close()
        if self._registry:
            await self._registry.set_stopped(self.SERVICE_NAME)
        log.info("APIManager stopped", stats=self._stats)

    async def health(self) -> dict:
        return {
            "running": self._running,
            "registered_apis": self._api_registry.list_names(),
            "stats": self._stats,
        }

    # ------------------------------------------------------------------
    # Public API (used by ActionCoordinator)
    # ------------------------------------------------------------------

    async def call(
        self,
        api_name: str,
        method: str,
        path: str = "",
        *,
        body: Any = None,
        params: dict | None = None,
        headers: dict | None = None,
        requester: str = "unknown",
        request_id: str | None = None,
        timeout: float | None = None,
        max_retries: int | None = None,
    ) -> APIResponse:
        """
        Execute an API call to a named, registered endpoint.

        Emits api.request.started → api.request.completed | api.request.failed.
        """
        rid = request_id or str(uuid.uuid4())

        # Resolve config
        config = self._api_registry.get(api_name)
        if config is None:
            error = f"API '{api_name}' not registered. Available: {self._api_registry.list_names()}"
            log.error("APIManager: unknown API", api_name=api_name, requester=requester)
            await self._emit_failed(rid, api_name, "", method, error, requester)
            return APIResponse(
                request_id=rid,
                api_name=api_name,
                endpoint="",
                method=method,
                status_code=0,
                success=False,
                error=error,
            )

        url = config.resolve_url(path)
        self._stats["started"] += 1

        await self._emit_started(rid, api_name, url, method, requester)

        response = await self._executor.request(
            config=config,
            method=method,
            path=path,
            json_body=body,
            params=params,
            headers=headers,
            timeout=timeout,
            max_retries=max_retries,
            request_id=rid,
        )

        if response.attempt > 1:
            self._stats["retried"] += response.attempt - 1

        if response.success:
            self._stats["completed"] += 1
            await self._emit_completed(response, requester)
        else:
            self._stats["failed"] += 1
            if response.status_code == 429:
                self._stats["rate_limited"] += 1
            await self._emit_failed(
                rid, api_name, url, method, response.error, requester, response
            )

        return response

    async def call_raw(
        self,
        *,
        url: str,
        method: str,
        body: Any = None,
        headers: dict | None = None,
        timeout: float = 30.0,
        requester: str = "unknown",
        request_id: str | None = None,
    ) -> APIResponse:
        """Execute an HTTP call to an arbitrary URL (no registry lookup)."""
        from actions.api.api_registry import AuthConfig

        config = APIEndpointConfig(
            name="_raw",
            base_url=url,
            auth=AuthConfig(type="none"),
            default_timeout=timeout,
            max_retries=0,
        )
        rid = request_id or str(uuid.uuid4())
        return await self._executor.request(
            config=config,
            method=method,
            path="",
            json_body=body,
            headers=headers or {},
            timeout=timeout,
            request_id=rid,
        )

    def register_api(self, config: APIEndpointConfig) -> None:
        """Register an additional API config at runtime."""
        self._api_registry.register(config)

    # ------------------------------------------------------------------
    # EventBus handler
    # ------------------------------------------------------------------

    async def _handle_action_request(self, event) -> None:
        payload = event.payload
        api_name = payload.get("api_name", "")
        method = payload.get("method", "GET")
        path = payload.get("path", "")
        body = payload.get("body")
        params = payload.get("params")
        headers = payload.get("headers")
        requester = payload.get("requester", event.source)
        rid = payload.get("request_id", event.event_id)

        if not api_name:
            log.warning(
                "APIManager: missing api_name in action request", source=event.source
            )
            return

        await self.call(
            api_name,
            method,
            path,
            body=body,
            params=params,
            headers=headers,
            requester=requester,
            request_id=rid,
        )

    # ------------------------------------------------------------------
    # Event emission helpers
    # ------------------------------------------------------------------

    async def _emit_started(
        self, rid: str, api_name: str, url: str, method: str, source: str
    ) -> None:
        await self._emit(
            APIEvents.REQUEST_STARTED,
            APICallPayload(
                request_id=rid, endpoint=url, method=method, api_name=api_name
            ).as_dict(),
            source,
        )

    async def _emit_completed(self, response: APIResponse, source: str) -> None:
        await self._emit(
            APIEvents.REQUEST_COMPLETED,
            APICallPayload(
                request_id=response.request_id,
                endpoint=response.endpoint,
                method=response.method,
                status_code=response.status_code,
                response=response.data,
                duration_ms=response.duration_ms,
                attempt=response.attempt,
                api_name=response.api_name,
            ).as_dict(),
            source,
        )

    async def _emit_failed(
        self,
        rid: str,
        api_name: str,
        url: str,
        method: str,
        error: str,
        source: str,
        response: APIResponse | None = None,
    ) -> None:
        await self._emit(
            APIEvents.REQUEST_FAILED,
            APICallPayload(
                request_id=rid,
                endpoint=url,
                method=method,
                status_code=response.status_code if response else 0,
                error=error,
                duration_ms=response.duration_ms if response else 0.0,
                attempt=response.attempt if response else 1,
                api_name=api_name,
            ).as_dict(),
            source,
        )

    async def _emit(self, event_type: str, payload: dict, source: str) -> None:
        if not self._bus:
            return
        from kernel.event_bus.event_bus import Event

        await self._bus.publish(
            Event(
                event_type=event_type,
                source=source or self.SERVICE_NAME,
                payload=payload,
            )
        )
