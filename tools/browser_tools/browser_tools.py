"""
tools/browser_tools/browser_tools.py
──────────────────────────────────────
Browser tool implementations for JARVIS AI OS.

Routes through the existing PlaywrightEngine (actions/browser/playwright_engine.py)
so all browser operations share the same session management and capability layer.
When Playwright is not installed the engine degrades gracefully to stub responses,
meaning tools always return a valid ToolResult — agents never hard-fail.

Provides:
  browser.navigate      — navigate to a URL
  browser.click         — click an element (CSS selector or x/y coordinates)
  browser.type          — type text into an element
  browser.screenshot    — capture a screenshot (base64 PNG or saved file)
  browser.extract       — extract text/HTML from a page element
  browser.execute_js    — run arbitrary JavaScript and return the result
  browser.scroll        — scroll the page by a pixel offset
  browser.wait          — wait for a selector to appear or sleep N ms
  browser.get_text      — convenience: get full page visible text
  browser.get_html      — convenience: get full page outer HTML

  web.open              — navigate to a raw URL (alias: open_url)
  web.site              — navigate to a site by alias from web.yaml
  web.close_tab         — close a tab by title match
  web.close_current     — close the active/current tab
  web.close_all         — close all open tabs (optionally for a named browser)

Architecture:
  Agent
    ↓
  ToolRegistry.invoke("browser.navigate", ...),  or
  ToolRegistry.invoke("web.site", alias="github")
    ↓
  browser_tools.*()           ← this module
    ↓
  PlaywrightEngine             ← actions/browser/playwright_engine.py
    ↓
  Chromium / Firefox / WebKit (headless)
"""

from __future__ import annotations

import asyncio
import logging
import threading
import time
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from tools.registry.tool_registry import ToolRegistry

log = logging.getLogger(__name__)


# ──────────────────────────────────────────────
# Module-level engine + page pool
# ──────────────────────────────────────────────

_engine: Any = None
_page: Any = None
_extra_pages: list[Any] = []  # tracked open pages for close_all


def _get_engine():
    """Lazily initialise and return the PlaywrightEngine singleton.

    Reads browser settings from the same ui_settings.json the Settings panel
    persists, so browser tools and the BrowserWorkspace stay in sync.
    headless=False + a persistent profile directory dramatically reduce
    bot-detection on sites like YouTube:
      - headless Chromium has a distinct, easily-flagged fingerprint and
        often can't play DRM-gated video at all ("Something went wrong").
      - a persistent profile lets cookies/consent/session state survive
        across runs, so the session looks like a returning real browser
        rather than a fresh anonymous one every time.

    Override via env vars if a headless/CI environment is required:
      JARVIS_BROWSER_HEADLESS=1   -> force headless
      JARVIS_BROWSER_PROFILE_DIR  -> custom profile path
    """
    global _engine
    if _engine is None:
        try:
            import os
            from pathlib import Path
            from actions.browser.playwright_engine import PlaywrightEngine

            try:
                from interface.panels.settings_panel import load_settings
                browser_settings = load_settings().get("browser", {})
                headless = browser_settings.get("headless", False)
                browser_type = browser_settings.get("browser_type", "chromium")
                viewport = (
                    browser_settings.get("viewport_w", 1280),
                    browser_settings.get("viewport_h", 900),
                )
            except Exception:
                headless = False
                browser_type = "chromium"
                viewport = (1280, 900)

            env_headless = os.environ.get("JARVIS_BROWSER_HEADLESS")
            if env_headless is not None:
                headless = env_headless == "1"

            profile_dir = os.environ.get(
                "JARVIS_BROWSER_PROFILE_DIR",
                str(Path("datastore") / "browser_profile"),
            )

            _engine = PlaywrightEngine(
                browser_type=browser_type,
                headless=headless,
                viewport=viewport,
                user_data_dir=profile_dir,
                stealth=True,
            )
        except ImportError:
            log.warning(
                "PlaywrightEngine not importable; browser tools will use stub mode"
            )
            _engine = _StubEngine()
    return _engine


class _StubEngine:
    """Fallback when actions.browser is not importable."""

    _available = False

    async def start(self) -> None:
        """No-op: Playwright engine not available."""
        log.debug("_StubEngine.start() called — Playwright not installed")

    async def stop(self) -> None:
        """No-op: Playwright engine not available."""
        log.debug("_StubEngine.stop() called — Playwright not installed")

    async def new_page(self):
        return _StubPage()

    async def close_page(self, page) -> None:
        """No-op: close the stub page object."""
        if hasattr(page, "url"):
            page.url = "about:blank"
        log.debug("_StubEngine.close_page() — no real page to close")

    async def navigate(self, page, url, **kw):
        return {
            "url": url,
            "title": "stub",
            "status": 0,
            "note": "Playwright not available. Run: pip install playwright && playwright install chromium",
        }

    async def click(self, page, selector="", **kw):
        return {"clicked": selector, "note": "stub mode"}

    async def type_text(self, page, selector, text, **kw):
        return {"typed": f"{len(text)} chars (stub)"}

    async def screenshot(self, page, path=None):
        return {"b64": "", "path": path, "note": "stub mode"}

    async def extract_content(self, page, selector="body", attribute=None):
        return {"text": "", "html": "", "note": "stub mode"}

    async def execute_js(self, page, script):
        return {"result": None, "note": "stub mode"}

    async def scroll(self, page, x=0, y=500):
        return {"scrolled": True, "delta_x": x, "delta_y": y, "note": "stub mode"}

    async def wait(self, page, selector=None, ms=1000):
        return {"waited_ms": ms, "note": "stub mode"}

    async def get_page_url(self, page):
        return ""

    async def get_page_title(self, page):
        return ""


class _StubPage:
    url: str = "about:blank"

    async def close(self) -> None:
        """No-op: nothing to close in stub mode."""
        log.debug("_StubEngine.close() — Playwright not installed")


# ──────────────────────────────────────────────
# Persistent background event loop for Playwright
# ──────────────────────────────────────────────
#
# Playwright's async API binds the Browser/BrowserContext/Page objects (and
# the underlying pipe/websocket connection to the Chromium driver process)
# to the asyncio event loop they were created on.
#
# The OLD _run() did:
#     loop = asyncio.get_running_loop()
#     if loop.is_running():
#         future = ThreadPoolExecutor().submit(asyncio.run, coro)
#
# `asyncio.run(coro)` creates a BRAND NEW event loop, runs the coroutine,
# then CLOSES that loop — every single call. So:
#   1st call (e.g. web.youtube_search): engine.start() + new_page() +
#      navigate() all happen on loop #1. Works, search results page loads.
#      loop #1 is then destroyed, silently tearing down Playwright's
#      connection to the browser process.
#   2nd call (e.g. web.youtube_play, clicking a result): runs on a brand
#      new loop #2. `_page`/`_engine` are still the same Python objects
#      (module globals persist), but their connection was bound to the now
#      -dead loop #1 -> Playwright calls on `_page` fail or hang against a
#      severed connection, so the click/goto never actually happens and the
#      visible browser window is left wherever it was (often the initial
#      "about:blank" tab from launch_persistent_context).
#
# Fix: run ALL Playwright work on ONE persistent event loop that lives in a
# dedicated background thread for the lifetime of the process, so the
# engine/context/page objects (and their connection) stay valid across
# every tool call.
_bg_loop: asyncio.AbstractEventLoop | None = None
_bg_thread: threading.Thread | None = None
_bg_lock = threading.Lock()


def _get_bg_loop() -> asyncio.AbstractEventLoop:
    global _bg_loop, _bg_thread
    with _bg_lock:
        if _bg_loop is None or _bg_thread is None or not _bg_thread.is_alive():
            new_loop = asyncio.new_event_loop()

            def _runner(loop: asyncio.AbstractEventLoop) -> None:
                asyncio.set_event_loop(loop)
                loop.run_forever()

            t = threading.Thread(
                target=_runner, args=(new_loop,), daemon=True, name="jarvis-browser-loop"
            )
            t.start()
            _bg_loop = new_loop
            _bg_thread = t
    return _bg_loop


def _run(coro, timeout=90):
    """Run an async coroutine on the persistent browser event loop.

    Blocks the calling thread until the coroutine completes (same external
    behavior as before), but — unlike the old asyncio.run()-per-call
    approach — never tears down the loop Playwright's Browser/Page objects
    are bound to.
    """
    loop = _get_bg_loop()
    future = asyncio.run_coroutine_threadsafe(coro, loop)
    return future.result(timeout=timeout)


async def _ensure_page():
    """Ensure engine is started and a page is open; return the page."""
    global _page
    engine = _get_engine()
    if not engine._available:
        await engine.start()
    if _page is None:
        _page = await engine.new_page()
    return _page


# ──────────────────────────────────────────────
# Original browser.* tools
# ──────────────────────────────────────────────


def browser_navigate(url: str, wait_until: str = "load") -> dict:
    """
    Navigate the browser to a URL.

    Args:
      url        — URL to navigate to (must include scheme, e.g. https://)
      wait_until — Playwright wait condition: 'load' | 'domcontentloaded' | 'networkidle'

    Returns:
      url    — final URL after navigation (may differ due to redirects)
      title  — page title
      status — HTTP status code
    """
    if not url:
        raise ValueError("url must be provided")
    if not url.startswith(("http://", "https://")):
        url = "https://" + url

    async def _run_navigate():
        page = await _ensure_page()
        return await _get_engine().navigate(page, url, wait_until=wait_until)

    return _run(_run_navigate())


def browser_click(selector: str = "", x: float = None, y: float = None) -> dict:
    """
    Click an element on the current page.

    Args:
      selector — CSS selector (e.g. 'button#submit', 'a.nav-link')
      x, y     — absolute pixel coordinates (used if selector is empty)

    Returns:
      clicked — selector or coordinate string that was clicked
    """
    if not selector and (x is None or y is None):
        raise ValueError("provide either selector or both x and y coordinates")

    async def _run_click():
        page = await _ensure_page()
        return await _get_engine().click(page, selector=selector, x=x, y=y)

    return _run(_run_click())


def browser_type(selector: str, text: str, delay_ms: int = 30) -> dict:
    """
    Type text into a form field or element.

    Args:
      selector — CSS selector of the target input element
      text     — text to type
      delay_ms — milliseconds between keystrokes (simulates human typing)

    Returns:
      typed — summary of what was typed
    """
    if not selector:
        raise ValueError("selector must be provided")
    if text is None:
        raise ValueError("text must be provided")

    async def _run_type():
        page = await _ensure_page()
        return await _get_engine().type_text(page, selector, text, delay=delay_ms)

    return _run(_run_type())


def browser_screenshot(save_path: str = "") -> dict:
    """
    Capture a screenshot of the current page.

    Args:
      save_path — local file path to save PNG; if empty, returns base64-encoded PNG

    Returns:
      b64  — base64 PNG string (empty if save_path provided)
      path — file path (empty if base64 returned)
    """

    async def _run_screenshot():
        page = await _ensure_page()
        return await _get_engine().screenshot(page, path=save_path or None)

    return _run(_run_screenshot())


def browser_extract(selector: str = "body", attribute: str = "") -> dict:
    """
    Extract text content or an attribute from a page element.

    Args:
      selector  — CSS selector of the element (default: 'body' = full page)
      attribute — HTML attribute to extract (e.g. 'href', 'src'); if empty, returns text+html

    Returns:
      text    — visible text content (when no attribute specified)
      html    — inner HTML (when no attribute specified)
      content — attribute value (when attribute specified)
    """

    async def _run_extract():
        page = await _ensure_page()
        return await _get_engine().extract_content(
            page, selector=selector, attribute=attribute or None
        )

    return _run(_run_extract())


def browser_execute_js(script: str) -> dict:
    """
    Execute JavaScript in the context of the current page.

    Args:
      script — JavaScript expression or statement (return value is captured)

    Returns:
      result — serialised return value from the script
    """
    if not script:
        raise ValueError("script must be provided")

    async def _run_js():
        page = await _ensure_page()
        return await _get_engine().execute_js(page, script)

    return _run(_run_js())


def browser_scroll(x: int = 0, y: int = 500) -> dict:
    """
    Scroll the current page by a pixel offset.

    Args:
      x — horizontal scroll delta in pixels
      y — vertical scroll delta in pixels (positive = scroll down)

    Returns:
      scrolled  — True
      delta_x   — horizontal pixels scrolled
      delta_y   — vertical pixels scrolled
    """

    async def _run_scroll():
        page = await _ensure_page()
        return await _get_engine().scroll(page, x=x, y=y)

    return _run(_run_scroll())


def browser_wait(selector: str = "", ms: int = 1000) -> dict:
    """
    Wait for a CSS selector to appear, or sleep for ms milliseconds.

    Args:
      selector — CSS selector to wait for (e.g. '#result-table')
      ms       — fallback sleep duration in milliseconds if selector is empty

    Returns:
      waited_for — selector (if provided)
      waited_ms  — milliseconds slept (if no selector)
    """

    async def _run_wait():
        page = await _ensure_page()
        return await _get_engine().wait(page, selector=selector or None, ms=ms)

    return _run(_run_wait())


def browser_get_text(url: str = "", selector: str = "body") -> dict:
    """
    Get the visible text of the current page, or navigate to a URL first.

    Args:
      url       — optional URL to navigate to before extracting text
      selector  — CSS selector for the element to extract (default: 'body' = full page)

    Returns:
      text       — visible text content
      char_count — text length
      url        — final URL (after navigation, if any)
    """

    async def _run_get_text():
        page = await _ensure_page()
        engine = _get_engine()
        if url:
            await engine.navigate(page, url, wait_until="domcontentloaded")
        result = await engine.extract_content(page, selector=selector)
        text = result.get("text", "")
        return {"text": text, "char_count": len(text), "url": url}

    return _run(_run_get_text(), timeout=120)


def browser_get_html(selector: str = "body") -> dict:
    """
    Convenience tool: get the raw HTML of the entire page (or a selector).

    Returns:
      html       — inner HTML string
      char_count — HTML length
    """

    async def _run_get_html():
        page = await _ensure_page()
        result = await _get_engine().extract_content(page, selector=selector)
        html = result.get("html", "")
        return {"html": html, "char_count": len(html)}

    return _run(_run_get_html())


# ──────────────────────────────────────────────
# New web.* tools
# ──────────────────────────────────────────────


def _get_web_registry():
    import config.web_registry as _reg

    return _reg


def _publish_web_event(event_type: str, payload: dict) -> None:
    """Fire-and-forget EventBus publish via publish_sync; never raises."""
    try:
        from boot.dependency_container import get_container
        bus = get_container().try_resolve("event_bus")
        if bus is None:
            return
        from kernel.event_bus.event_bus import Event
        evt = Event(event_type=event_type, source="browser_tools", payload=payload)
        bus.publish_sync(evt)
    except Exception as exc:
        log.debug("EventBus publish failed (non-fatal): %s", exc)


def open_url(url: str) -> dict:
    """
    Open a raw URL in the browser.

    Args:
        url — fully-qualified URL (scheme added automatically if missing).

    Returns:
        dict with keys: success, url, title, status, timestamp.
    """
    if not url:
        raise ValueError("url must be provided")
    if not url.startswith(("http://", "https://")):
        url = "https://" + url

    # Open in the system default browser (visible to the user)
    try:
        import webbrowser
        webbrowser.open(url)
        log.info("Opened URL in system browser: %s", url)
    except Exception as exc:
        log.warning("webbrowser.open failed: %s", exc)

    # Also navigate via PlaywrightEngine for programmatic tab control
    result = browser_navigate(url)
    payload = {
        "success": True,
        "target": url,
        "url": result.get("url", url),
        "title": result.get("title", ""),
        "pid": None,
        "timestamp": time.time(),
    }
    _publish_web_event("action.web.opened", payload)
    return payload


def open_site(alias: str) -> dict:
    """
    Open a website by alias as defined in web.yaml.

    Args:
        alias — site alias (e.g. "github", "youtube", "gmail").

    Returns:
        dict with keys: success, alias, url, title, timestamp.
    """
    reg = _get_web_registry()
    url = reg.get_url(alias)
    if url is None:
        available = [r["alias"] for r in reg.list_urls()]
        return {
            "success": False,
            "alias": alias,
            "url": None,
            "title": None,
            "pid": None,
            "timestamp": time.time(),
            "message": (
                f"Site alias '{alias}' not found in web.yaml. Available: {available}"
            ),
        }

    result = browser_navigate(url)
    # Also open in system browser for visibility
    try:
        import webbrowser
        webbrowser.open(url)
    except Exception:
        pass
    payload = {
        "success": True,
        "alias": alias,
        "target": alias,
        "url": result.get("url", url),
        "title": result.get("title", ""),
        "pid": None,
        "timestamp": time.time(),
    }
    _publish_web_event("action.web.opened", payload)
    return payload


def close_tab(title: str = "") -> dict:
    """
    Close an open browser tab by matching its title substring.

    Args:
        title — substring to match against open tab titles (case-insensitive).
                Closes the first matching tab.

    Returns:
        dict with keys: success, target, closed_url, timestamp, message.
    """
    if not title:
        raise ValueError(
            "title must be provided for close_tab; use close_current_tab() instead."
        )

    async def _do_close():
        global _page, _extra_pages
        engine = _get_engine()
        pages_to_check = ([_page] if _page else []) + _extra_pages
        for page in pages_to_check:
            try:
                page_title = await engine.get_page_title(page)
                if title.lower() in (page_title or "").lower():
                    page_url = await engine.get_page_url(page)
                    await engine.close_page(page)
                    if page is _page:
                        _page = None
                    else:
                        _extra_pages = [p for p in _extra_pages if p is not page]
                    return {
                        "success": True,
                        "target": title,
                        "closed_url": page_url,
                        "pid": None,
                        "timestamp": time.time(),
                        "message": f"Closed tab: '{page_title}'",
                    }
            except Exception:
                continue
        return {
            "success": False,
            "target": title,
            "closed_url": None,
            "pid": None,
            "timestamp": time.time(),
            "message": f"No open tab matched title: '{title}'",
        }

    result = _run(_do_close())
    payload = {k: result[k] for k in ("success", "target", "pid", "timestamp")}
    _publish_web_event("action.web.closed", payload)
    return result


def close_current_tab() -> dict:
    """
    Close the currently active browser tab.

    Returns:
        dict with keys: success, closed_url, timestamp, message.
    """

    async def _do_close():
        global _page
        engine = _get_engine()
        if _page is None:
            return {
                "success": False,
                "target": "current",
                "closed_url": None,
                "pid": None,
                "timestamp": time.time(),
                "message": "No active browser tab.",
            }
        try:
            page_url = await engine.get_page_url(_page)
            page_title = await engine.get_page_title(_page)
            await engine.close_page(_page)
            _page = None
            return {
                "success": True,
                "target": "current",
                "closed_url": page_url,
                "pid": None,
                "timestamp": time.time(),
                "message": f"Closed tab: '{page_title}' ({page_url})",
            }
        except Exception as exc:
            return {
                "success": False,
                "target": "current",
                "closed_url": None,
                "pid": None,
                "timestamp": time.time(),
                "message": str(exc),
            }

    result = _run(_do_close())
    payload = {k: result[k] for k in ("success", "target", "pid", "timestamp")}
    _publish_web_event("action.web.closed", payload)
    return result


def close_all_tabs(browser: str = "") -> dict:
    """
    Close all open browser tabs managed by this engine.

    Args:
        browser — optional browser name hint for logging (e.g. "chrome").
                  Does not filter — all engine-managed pages are closed.

    Returns:
        dict with keys: success, closed_count, timestamp, message.
    """

    async def _do_close_all():
        global _page, _extra_pages
        engine = _get_engine()
        pages = ([_page] if _page else []) + list(_extra_pages)
        closed = 0
        errors = []
        for page in pages:
            try:
                await engine.close_page(page)
                closed += 1
            except Exception as exc:
                errors.append(str(exc))
        _page = None
        _extra_pages = []
        return {
            "success": len(errors) == 0,
            "target": browser or "all",
            "closed_count": closed,
            "pid": None,
            "timestamp": time.time(),
            "message": (
                f"Closed {closed} tab(s)." + (f" Errors: {errors}" if errors else "")
            ),
        }

    result = _run(_do_close_all())
    payload = {k: result[k] for k in ("success", "target", "pid", "timestamp")}
    _publish_web_event("action.web.closed", payload)
    return result


# ──────────────────────────────────────────────
# YouTube search + play
# ──────────────────────────────────────────────

_YOUTUBE_RESULT_SELECTOR = "ytd-video-renderer a#video-title, ytd-video-renderer a#thumbnail"
_DDG_RESULT_SELECTOR = ".result"
_DDG_LINK_SELECTOR = ".result__title a"
_DDG_SNIPPET_SELECTOR = ".result__snippet"

# Buttons seen on Google/YouTube's "Before you continue" consent
# interstitial, across locales/layouts. Best-effort, short-timeout —
# the CONSENT cookie seeded in PlaywrightEngine.start() should make this a
# no-op most of the time, but covers the case where it still appears
# (different domain, signed-in state, locale variant, etc).
_CONSENT_BUTTON_SELECTORS = [
    "button:has-text('Accept all')",
    "button:has-text('I agree')",
    "form[action*='consent'] button[aria-label*='Accept']",
    "tp-yt-paper-button:has-text('Accept all')",
    "ytd-button-renderer:has-text('Accept all')",
]


async def _dismiss_consent_if_present(page, timeout_ms: int = 2500) -> bool:
    """Best-effort click of a cookie-consent 'Accept all' button, if one is
    blocking the page. Returns True if something was clicked."""
    for sel in _CONSENT_BUTTON_SELECTORS:
        try:
            btn = await page.wait_for_selector(sel, timeout=timeout_ms, state="visible")
        except Exception:
            continue
        if btn:
            try:
                await btn.click()
                await page.wait_for_timeout(500)
                return True
            except Exception:
                pass
    return False


def _extract_youtube_link_from_ddg(href: str) -> str:
    """Extract real URL from DuckDuckGo's wrapped redirect link."""
    if "uddg=" in href:
        import urllib.parse
        return urllib.parse.unquote(
            urllib.parse.parse_qs(urllib.parse.urlparse(href).query).get(
                "uddg", [href]
            )[0]
        )
    return href


def youtube_search(query: str) -> dict:
    """
    Search for videos via DuckDuckGo (site:youtube.com) and return results.

    This routes through DuckDuckGo instead of loading YouTube's search page
    directly, avoiding Playwright/Chromium timeouts and the
    'Something went wrong' interstitial on youtube.com.

    Args:
        query — search text (e.g. "back to black ironman").

    Returns:
        dict with keys: success, query, url, title, result_count, timestamp.
    """
    if not query or not query.strip():
        raise ValueError("query must be provided")

    import urllib.parse
    search_url = "https://html.duckduckgo.com/html/?q=site:youtube.com+" + urllib.parse.quote(query.strip())

    async def _do_search():
        page = await _ensure_page()
        nav = await _get_engine().navigate(page, search_url, wait_until="domcontentloaded")
        if _get_engine()._available and not isinstance(page, _StubPage):
            await _dismiss_consent_if_present(page)
        await _get_engine().wait(page, selector=_DDG_RESULT_SELECTOR, ms=10_000)
        count = 0
        youtube_links = []
        if _get_engine()._available and not isinstance(page, _StubPage):
            count = await page.eval_on_selector_all(
                _DDG_RESULT_SELECTOR, "els => els.length"
            )
            if count == 0:
                if await _dismiss_consent_if_present(page):
                    await _get_engine().wait(page, selector=_DDG_RESULT_SELECTOR, ms=10_000)
                    count = await page.eval_on_selector_all(
                        _DDG_RESULT_SELECTOR, "els => els.length"
                    )
            if count > 0:
                raw_links = await page.eval_on_selector_all(
                    _DDG_LINK_SELECTOR,
                    "els => els.map(e => e.href).filter(h => h)",
                )
                youtube_links = [
                    _extract_youtube_link_from_ddg(h)
                    for h in raw_links
                    if "youtube.com" in h or "youtu.be" in h
                ]
        return nav, count, youtube_links

    nav, count, youtube_links = _run(_do_search(), timeout=90)
    payload = {
        "success": True,
        "query": query.strip(),
        "url": nav.get("url", search_url),
        "title": nav.get("title", ""),
        "result_count": count,
        "youtube_links": youtube_links,
        "pid": None,
        "timestamp": time.time(),
    }
    _publish_web_event("action.web.youtube_search", payload)
    return payload


def youtube_play(query: str, index: int = 1) -> dict:
    """
    Search DuckDuckGo for a query, find a YouTube result, and play it.

    This routes through DuckDuckGo instead of loading YouTube's search page
    directly, avoiding Playwright/Chromium timeouts and the
    'Something went wrong' interstitial on youtube.com. The first matching
    YouTube link from DuckDuckGo results is opened directly.

    Args:
        query — search text, e.g. "back to black ironman" (any video name).
        index — 1-based position of the result to play (1 = first/top
                result, 2 = second, etc). Defaults to 1.

    Returns:
        dict with keys: success, query, index, video_url, video_title,
        result_count, timestamp, message.
    """
    if not query or not query.strip():
        raise ValueError("query must be provided")
    if index < 1:
        raise ValueError("index must be 1 or greater (1 = first result)")

    import urllib.parse
    search_url = "https://html.duckduckgo.com/html/?q=site:youtube.com+" + urllib.parse.quote(query.strip())

    async def _do_play():
        page = await _ensure_page()
        engine = _get_engine()

        nav = await engine.navigate(page, search_url, wait_until="domcontentloaded")
        if engine._available and not isinstance(page, _StubPage):
            await _dismiss_consent_if_present(page)
        await engine.wait(page, selector=_DDG_RESULT_SELECTOR, ms=10_000)

        if not engine._available or isinstance(page, _StubPage):
            return nav, 0, "", "", False

        count = await page.eval_on_selector_all(_DDG_RESULT_SELECTOR, "els => els.length")
        if count == 0:
            if await _dismiss_consent_if_present(page):
                await engine.wait(page, selector=_DDG_RESULT_SELECTOR, ms=10_000)
                count = await page.eval_on_selector_all(_DDG_RESULT_SELECTOR, "els => els.length")
        if count == 0:
            return nav, 0, "", "", False
        if index > count:
            return nav, count, "", "", False

        raw_links = await page.eval_on_selector_all(
            _DDG_LINK_SELECTOR,
            "els => els.map(e => e.href).filter(h => h)",
        )
        youtube_links = [
            _extract_youtube_link_from_ddg(h)
            for h in raw_links
            if "youtube.com" in h or "youtu.be" in h
        ]
        if not youtube_links:
            return nav, count, "", "", False
        if index > len(youtube_links):
            return nav, count, "", "", False

        target_idx = index - 1
        video_href = youtube_links[target_idx]
        video_title = ""

        snippets = await page.eval_on_selector_all(
            _DDG_RESULT_SELECTOR,
            "els => els.map(e => { const t = e.querySelector('.result__title'); return t ? t.innerText.trim() : ''; })",
        )
        if target_idx < len(snippets):
            video_title = snippets[target_idx]

        await engine.navigate(page, video_href, wait_until="domcontentloaded")
        if engine._available and not isinstance(page, _StubPage):
            await _dismiss_consent_if_present(page)
        import random
        await page.wait_for_timeout(random.randint(300, 700))
        await engine.wait(page, selector="video.html5-main-video, video", ms=15_000)
        try:
            await page.evaluate(
                "() => { const v = document.querySelector('video.html5-main-video'); "
                "if (v && v.paused) { v.play(); } }"
            )
        except Exception:
            pass

        return nav, count, video_title, video_href, True

    nav, count, video_title, video_href, played = _run(_do_play(), timeout=120)

    if count == 0:
        message = f"No DuckDuckGo results found for '{query.strip()}'."
    elif not played and index > count:
        message = (
            f"Only {count} result(s) found for '{query.strip()}'; "
            f"cannot play result #{index}."
        )
    elif played:
        message = f"Playing result #{index} for '{query.strip()}': {video_title or video_href}"
    else:
        message = "Playback unavailable in stub browser mode."

    payload = {
        "success": played,
        "query": query.strip(),
        "index": index,
        "video_title": video_title,
        "video_url": video_href or nav.get("url", search_url),
        "result_count": count,
        "pid": None,
        "timestamp": time.time(),
        "message": message,
    }
    _publish_web_event("action.web.youtube_play", payload)
    return payload


# ──────────────────────────────────────────────
# Registration
# ──────────────────────────────────────────────


def register_browser_tools(registry: "ToolRegistry", event_bus=None) -> list[str]:
    """Register all browser.* and web.* tools into the provided ToolRegistry."""
    from tools.registry.tool_registry import ToolDefinition

    def _wrap(fn, name: str):
        if event_bus is None:
            return fn
        import functools

        @functools.wraps(fn)
        def wrapper(*args, **kwargs):
            t0 = time.monotonic()
            try:
                result = fn(*args, **kwargs)
                latency = time.monotonic() - t0
                try:
                    from kernel.event_bus.event_bus import Event

                    event_bus.publish_sync(
                        Event(
                            event_type="tool.invoked",
                            source=name,
                            payload={
                                "tool": name,
                                "success": True,
                                "latency_s": round(latency, 4),
                            },
                        )
                    )
                except Exception:
                    pass
                return result
            except Exception as exc:
                latency = time.monotonic() - t0
                try:
                    from kernel.event_bus.event_bus import Event

                    event_bus.publish_sync(
                        Event(
                            event_type="tool.failed",
                            source=name,
                            payload={
                                "tool": name,
                                "error": str(exc),
                                "latency_s": round(latency, 4),
                            },
                        )
                    )
                except Exception:
                    pass
                raise

        return wrapper

    tools = [
        # ── Original browser.* tools ────────────────────────────────────
        ToolDefinition(
            name="browser.navigate",
            handler=_wrap(browser_navigate, "browser.navigate"),
            description="Navigate the browser to a URL and return title and status code.",
            tags=["browser", "navigate", "web", "automation"],
            timeout_s=60.0,
        ),
        ToolDefinition(
            name="browser.click",
            handler=_wrap(browser_click, "browser.click"),
            description="Click an element on the current page by CSS selector or x/y coordinates.",
            tags=["browser", "click", "interact", "automation"],
            timeout_s=30.0,
        ),
        ToolDefinition(
            name="browser.type",
            handler=_wrap(browser_type, "browser.type"),
            description="Type text into a form field or element on the current page.",
            tags=["browser", "type", "input", "form", "automation"],
            timeout_s=30.0,
        ),
        ToolDefinition(
            name="browser.screenshot",
            handler=_wrap(browser_screenshot, "browser.screenshot"),
            description="Capture a screenshot of the current page as base64 PNG or saved file.",
            tags=["browser", "screenshot", "capture", "vision"],
            timeout_s=30.0,
        ),
        ToolDefinition(
            name="browser.extract",
            handler=_wrap(browser_extract, "browser.extract"),
            description="Extract text content or an HTML attribute from a page element.",
            tags=["browser", "extract", "scrape", "content"],
            timeout_s=30.0,
        ),
        ToolDefinition(
            name="browser.execute_js",
            handler=_wrap(browser_execute_js, "browser.execute_js"),
            description="Execute JavaScript in the current page context and return the result.",
            tags=["browser", "javascript", "js", "execute", "automation"],
            timeout_s=30.0,
        ),
        ToolDefinition(
            name="browser.scroll",
            handler=_wrap(browser_scroll, "browser.scroll"),
            description="Scroll the current page by a horizontal and/or vertical pixel offset.",
            tags=["browser", "scroll", "interact", "automation"],
            timeout_s=15.0,
        ),
        ToolDefinition(
            name="browser.wait",
            handler=_wrap(browser_wait, "browser.wait"),
            description="Wait for a CSS selector to appear on the page, or sleep N milliseconds.",
            tags=["browser", "wait", "selector", "automation"],
            timeout_s=60.0,
        ),
                ToolDefinition(
            name="browser.get_text",
            handler=_wrap(browser_get_text, "browser.get_text"),
            description="Get visible text from the current page or navigate to a URL and extract text (single-call browse).",
            tags=["browser", "text", "content", "extract"],
            timeout_s=120.0,
        ),
        ToolDefinition(
            name="browser.get_html",
            handler=_wrap(browser_get_html, "browser.get_html"),
            description="Get the raw inner HTML of the current page (or a selector).",
            tags=["browser", "html", "source", "extract"],
            timeout_s=30.0,
        ),
        # ── New web.* tools ─────────────────────────────────────────────
        ToolDefinition(
            name="web.open",
            handler=_wrap(open_url, "web.open"),
            description=(
                "Open a raw URL in the browser. "
                "Scheme (https://) is added automatically if missing."
            ),
            tags=["web", "browser", "open", "navigate", "url"],
            timeout_s=60.0,
        ),
        ToolDefinition(
            name="web.site",
            handler=_wrap(open_site, "web.site"),
            description=(
                "Open a website by alias defined in web.yaml "
                "(e.g. 'github', 'youtube', 'gmail'). "
                "Resolves the alias to its URL, then navigates."
            ),
            tags=["web", "browser", "site", "alias", "open"],
            timeout_s=60.0,
        ),
        ToolDefinition(
            name="web.close_tab",
            handler=_wrap(close_tab, "web.close_tab"),
            description=(
                "Close an open browser tab by matching its page title (case-insensitive substring). "
                "Closes the first matching tab."
            ),
            tags=["web", "browser", "close", "tab"],
            timeout_s=15.0,
        ),
        ToolDefinition(
            name="web.close_current",
            handler=_wrap(close_current_tab, "web.close_current"),
            description="Close the currently active browser tab.",
            tags=["web", "browser", "close", "tab", "current"],
            timeout_s=15.0,
        ),
        ToolDefinition(
            name="web.close_all",
            handler=_wrap(close_all_tabs, "web.close_all"),
            description=(
                "Close all open browser tabs managed by the current engine session. "
                "Optionally pass a browser name for logging purposes."
            ),
            tags=["web", "browser", "close", "all", "tabs"],
            timeout_s=30.0,
        ),
        ToolDefinition(
            name="web.youtube_search",
            handler=_wrap(youtube_search, "web.youtube_search"),
            description=(
                "Open YouTube and search for a query (does not play anything). "
                "Returns the result count and search page title/url."
            ),
            tags=["web", "browser", "youtube", "search", "video"],
            # FIX(phase12): was 30.0s — engine.start() (3-10s) + navigate + 2x 10s
            # selector waits = up to 35s worst-case; 90s gives comfortable headroom.
            timeout_s=90.0,
        ),
        ToolDefinition(
            name="web.youtube_play",
            handler=_wrap(youtube_play, "web.youtube_play"),
            description=(
                "Open YouTube, search for a query, and play a result video. "
                "Defaults to playing the FIRST (top) result if 'index' is not "
                "given — e.g. 'play back to black ironman on youtube' plays "
                "result #1. Pass index=2/3/4... to play the 2nd, 3rd, 4th "
                "result etc. Works with any video name/search phrase."
            ),
            tags=["web", "browser", "youtube", "play", "video", "automation"],
            # FIX(phase12): was 60.0s — search (35s worst) + click + 15s video wait
            # + human pauses ≈ 55-65s; 120s gives comfortable headroom.
            timeout_s=120.0,
        ),
    ]

    registered = []
    for defn in tools:
        registry.register(defn)
        registered.append(defn.name)
        log.info("Registered tool: %s", defn.name)

    return registered