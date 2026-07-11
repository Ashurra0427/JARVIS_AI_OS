"""
JARVIS AI OS — Browser Manager
=================================
Manages browser sessions and enforces the "no direct agent access" rule.

Architecture rule:
  Agents NEVER call browser methods directly.
  They publish action.browser.request events.
  BrowserManager validates, executes via PlaywrightEngine, and
  reports results back via Event Bus.

Responsibilities:
  - Session lifecycle (open, close, pool)
  - Permission checks before every action
  - Route browser requests to PlaywrightEngine
  - Publish action.browser.result events
"""

from __future__ import annotations

from kernel.event_bus.event_bus import Event

import asyncio
import time
import uuid
from dataclasses import dataclass, field
from enum import Enum
from typing import Any

from observability.logging.logger import get_logger

log = get_logger(__name__)


class BrowserAction(str, Enum):
    NAVIGATE = "navigate"
    CLICK = "click"
    TYPE = "type"
    SCREENSHOT = "screenshot"
    EXTRACT = "extract"
    EXECUTE_JS = "execute_js"
    SCROLL = "scroll"
    WAIT = "wait"
    CLOSE = "close"
    OPEN_URL = "open_url"
    OPEN_SITE = "open_site"
    CLOSE_TAB = "close_tab"
    CLOSE_CURRENT = "close_current"
    CLOSE_ALL = "close_all"


@dataclass
class BrowserRequest:
    request_id: str
    action: BrowserAction
    session_id: str | None
    params: dict = field(default_factory=dict)
    requester: str = "unknown"
    timeout: float = 30.0

    def as_dict(self) -> dict:
        return {
            "request_id": self.request_id,
            "action": self.action.value,
            "session_id": self.session_id,
            "params": self.params,
            "requester": self.requester,
        }


@dataclass
class BrowserResult:
    request_id: str
    success: bool
    session_id: str
    data: Any = None
    error: str = ""
    duration_ms: float = 0.0

    def as_dict(self) -> dict:
        return {
            "request_id": self.request_id,
            "success": self.success,
            "session_id": self.session_id,
            "data": self.data,
            "error": self.error,
            "duration_ms": self.duration_ms,
        }


@dataclass
class BrowserSession:
    session_id: str
    created_at: float = field(default_factory=time.time)
    last_used: float = field(default_factory=time.time)
    url: str = ""
    owner: str = "system"
    page: Any = None


class BrowserPermissionError(Exception):
    pass


class BrowserPermissions:
    def __init__(self) -> None:
        self._allowed_domains: set[str] = set()
        self._blocked_domains: set[str] = set()
        self._allowed_actions: set[BrowserAction] = set(BrowserAction)
        self._require_confirmation: set[BrowserAction] = {BrowserAction.EXECUTE_JS}

    def allow_domain(self, domain: str) -> None:
        self._allowed_domains.add(domain.lower())

    def block_domain(self, domain: str) -> None:
        self._blocked_domains.add(domain.lower())

    def restrict_action(self, action: BrowserAction) -> None:
        self._allowed_actions.discard(action)

    def check(self, request: BrowserRequest) -> None:
        if request.action not in self._allowed_actions:
            raise BrowserPermissionError(
                f"Action '{request.action.value}' is not permitted"
            )
        url = request.params.get("url", "")
        if url:
            import urllib.parse

            domain = urllib.parse.urlparse(url).netloc.lower()
            if domain in self._blocked_domains:
                raise BrowserPermissionError(f"Domain '{domain}' is blocked")
            if self._allowed_domains and domain not in self._allowed_domains:
                raise BrowserPermissionError(
                    f"Domain '{domain}' is not in the allowlist"
                )


class BrowserManager:
    """
    Central broker for all browser interactions.
    Agents post events; BrowserManager checks permissions, dispatches to
    PlaywrightEngine, and emits result events.
    """

    EVT_REQUEST = "action.browser.request"
    EVT_RESULT = "action.browser.result"
    EVT_SESSION_OPEN = "action.browser.session_opened"
    EVT_SESSION_CLOSE = "action.browser.session_closed"
    EVT_ERROR = "action.browser.error"
    SESSION_TTL = 300.0

    def __init__(
        self,
        event_bus: Any,
        playwright_engine: Any | None = None,
        permissions: BrowserPermissions | None = None,
    ) -> None:
        self._bus = event_bus
        self._engine = playwright_engine
        self._permissions = permissions or BrowserPermissions()
        self._sessions: dict[str, BrowserSession] = {}
        self._cleanup_task: asyncio.Task | None = None
        self._running = False

    async def start(self) -> None:
        self._running = True
        if self._bus:
            self._bus.subscribe(self.EVT_REQUEST, self._on_request)
        self._cleanup_task = asyncio.create_task(self._session_cleanup_loop())
        if self._engine:
            await self._engine.start()
        log.info("BrowserManager started")

    async def stop(self) -> None:
        self._running = False
        if self._cleanup_task:
            self._cleanup_task.cancel()
        for sid in list(self._sessions):
            await self._close_session(sid)
        if self._engine:
            await self._engine.stop()
        log.info("BrowserManager stopped")

    def configure_permissions(self, permissions: BrowserPermissions) -> None:
        self._permissions = permissions

    async def execute(self, request: BrowserRequest) -> BrowserResult:
        return await self._execute_request(request)

    async def _on_request(self, event: Any) -> None:
        payload = event.payload if hasattr(event, "payload") else {}
        try:
            req = BrowserRequest(
                request_id=payload.get("request_id", str(uuid.uuid4())),
                action=BrowserAction(payload["action"]),
                session_id=payload.get("session_id"),
                params=payload.get("params", {}),
                requester=payload.get(
                    "requester", event.source if hasattr(event, "source") else "unknown"
                ),
                timeout=float(payload.get("timeout", 30.0)),
            )
            await self._execute_request(req)
        except Exception as exc:
            log.exception(f"Browser request handling failed: {exc}")

            await self._emit(
                self.EVT_ERROR,
                {"request_id": payload.get("request_id"), "error": str(exc)},
            )

    async def _execute_request(self, req: BrowserRequest) -> BrowserResult:
        start = time.time()
        try:
            self._permissions.check(req)
            session = await self._get_or_create_session(req.session_id, req.requester)
            result_data = await self._dispatch(req, session)
            session.last_used = time.time()
            result = BrowserResult(
                request_id=req.request_id,
                success=True,
                session_id=session.session_id,
                data=result_data,
                duration_ms=(time.time() - start) * 1000,
            )
        except BrowserPermissionError as exc:
            log.warning(f"Permission denied [{req.action.value}]: {exc}")

            result = BrowserResult(
                request_id=req.request_id,
                success=False,
                session_id=req.session_id or "",
                error=f"Permission denied: {exc}",
                duration_ms=(time.time() - start) * 1000,
            )
        except Exception as exc:
            log.exception(f"Browser action failed: {exc}")

            result = BrowserResult(
                request_id=req.request_id,
                success=False,
                session_id=req.session_id or "",
                error=str(exc),
                duration_ms=(time.time() - start) * 1000,
            )
        await self._emit(self.EVT_RESULT, result.as_dict())
        return result

    async def _get_or_create_session(
        self, session_id: str | None, owner: str
    ) -> BrowserSession:
        if session_id and session_id in self._sessions:
            return self._sessions[session_id]
        sid = session_id or str(uuid.uuid4())
        page = await self._engine.new_page() if self._engine else None
        session = BrowserSession(session_id=sid, owner=owner, page=page)
        self._sessions[sid] = session
        await self._emit(self.EVT_SESSION_OPEN, {"session_id": sid, "owner": owner})
        log.debug(f"Browser session opened: {sid}")

        return session

    async def _close_session(self, session_id: str) -> None:
        session = self._sessions.pop(session_id, None)
        if session and session.page and self._engine:
            try:
                await self._engine.close_page(session.page)
            except Exception as exc:
                log.debug("Browser close_page error", session_id=session_id, error=str(exc))
        await self._emit(self.EVT_SESSION_CLOSE, {"session_id": session_id})

    async def _session_cleanup_loop(self) -> None:
        while self._running:
            await asyncio.sleep(60)
            now = time.time()
            stale = [
                sid
                for sid, s in self._sessions.items()
                if (now - s.last_used) > self.SESSION_TTL
            ]
            for sid in stale:
                await self._close_session(sid)

    async def _dispatch(self, req: BrowserRequest, session: BrowserSession) -> Any:
        if not self._engine:
            return {"simulated": True, "action": req.action.value}
        page = session.page
        p = req.params
        if req.action == BrowserAction.NAVIGATE:
            return await self._engine.navigate(page, p["url"])
        elif req.action == BrowserAction.CLICK:
            return await self._engine.click(
                page, p.get("selector", ""), p.get("x"), p.get("y")
            )
        elif req.action == BrowserAction.TYPE:
            return await self._engine.type_text(page, p["selector"], p["text"])
        elif req.action == BrowserAction.SCREENSHOT:
            return await self._engine.screenshot(page, p.get("path"))
        elif req.action == BrowserAction.EXTRACT:
            return await self._engine.extract_content(
                page, p.get("selector", "body"), p.get("attribute")
            )
        elif req.action == BrowserAction.EXECUTE_JS:
            return await self._engine.execute_js(page, p["script"])
        elif req.action == BrowserAction.SCROLL:
            return await self._engine.scroll(page, p.get("x", 0), p.get("y", 500))
        elif req.action == BrowserAction.WAIT:
            return await self._engine.wait(page, p.get("selector"), p.get("ms", 1000))
        elif req.action == BrowserAction.CLOSE:
            await self._close_session(session.session_id)
            return {"closed": True}
        elif req.action == BrowserAction.OPEN_URL:
            return self._invoke_web_tool("web.open", url=p["url"])
        elif req.action == BrowserAction.OPEN_SITE:
            return self._invoke_web_tool("web.site", alias=p["alias"])
        elif req.action == BrowserAction.CLOSE_TAB:
            return self._invoke_web_tool("web.close_tab", title=p.get("title", ""))
        elif req.action == BrowserAction.CLOSE_CURRENT:
            return self._invoke_web_tool("web.close_current")
        elif req.action == BrowserAction.CLOSE_ALL:
            return self._invoke_web_tool("web.close_all", browser=p.get("browser", ""))
        else:
            raise ValueError(f"Unknown browser action: {req.action}")

    # ------------------------------------------------------------------
    # Web tool integration  (app/web open+close patch)
    # ------------------------------------------------------------------

    def _invoke_web_tool(self, tool_name: str, **kwargs) -> dict:
        """
        Invoke a registered web.* tool through the ToolRegistry.
        Falls back gracefully if ToolRegistry is not wired.
        """
        try:
            from tools.registry.tool_registry import get_registry

            registry = get_registry()
            result = registry.invoke_sync(tool_name, **kwargs)
            if result.success:
                return result.value or {}
            log.warning(f"web tool '{tool_name}' failed: {result.error}")

            return {"success": False, "error": result.error}
        except Exception as exc:
            log.exception(f"_invoke_web_tool('{tool_name}') error: {exc}")

            return {"success": False, "error": str(exc)}

    async def open_url(self, url: str) -> "BrowserResult":
        """Open a raw URL via web.open tool; publishes action.web.opened."""
        import uuid

        return await self.execute(
            BrowserRequest(
                request_id=str(uuid.uuid4()),
                action=BrowserAction.OPEN_URL,
                session_id=None,
                params={"url": url},
                requester="browser_manager",
            )
        )

    async def open_site(self, alias: str) -> "BrowserResult":
        """Open a site by alias from web.yaml via web.site tool."""
        import uuid

        return await self.execute(
            BrowserRequest(
                request_id=str(uuid.uuid4()),
                action=BrowserAction.OPEN_SITE,
                session_id=None,
                params={"alias": alias},
                requester="browser_manager",
            )
        )

    async def close_tab(self, title: str) -> "BrowserResult":
        """Close a tab by title match via web.close_tab tool."""
        import uuid

        return await self.execute(
            BrowserRequest(
                request_id=str(uuid.uuid4()),
                action=BrowserAction.CLOSE_TAB,
                session_id=None,
                params={"title": title},
                requester="browser_manager",
            )
        )

    async def close_current_tab(self) -> "BrowserResult":
        """Close the currently active tab via web.close_current tool."""
        import uuid

        return await self.execute(
            BrowserRequest(
                request_id=str(uuid.uuid4()),
                action=BrowserAction.CLOSE_CURRENT,
                session_id=None,
                params={},
                requester="browser_manager",
            )
        )

    async def close_all_tabs(self, browser: str = "") -> "BrowserResult":
        """Close all managed tabs via web.close_all tool."""
        import uuid

        return await self.execute(
            BrowserRequest(
                request_id=str(uuid.uuid4()),
                action=BrowserAction.CLOSE_ALL,
                session_id=None,
                params={"browser": browser},
                requester="browser_manager",
            )
        )

    async def _emit(self, event_type: str, payload: dict) -> None:
        if self._bus:
            try:
                await self._bus.publish(
                    Event(event_type=event_type, source="browser_manager", payload=payload)
                )
            except Exception as exc:
                log.warning(f"Event publish failed: {exc}")