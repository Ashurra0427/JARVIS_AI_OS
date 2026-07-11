"""
JARVIS AI OS — Playwright Engine
===================================
Low-level Playwright wrapper. Called exclusively by BrowserManager.

Responsibilities:
  - Launch / manage Playwright browser & context
  - Implement atomic browser operations
  - Return structured results
  - Handle Playwright errors with meaningful messages

Rules:
  - Never called directly by agents
  - No event emission — that is BrowserManager's job
"""

from __future__ import annotations

import asyncio
import base64
from typing import Any

from observability.logging.logger import get_logger

log = get_logger(__name__)


class PlaywrightEngine:
    """
    Thin async wrapper around Playwright.

    Falls back to a no-op stub when playwright is not installed so the
    rest of the system can still boot and test.
    """

    def __init__(
        self,
        browser_type: str = "chromium",  # chromium | firefox | webkit
        headless: bool = True,
        slow_mo: float = 0,
        viewport: tuple[int, int] = (1280, 900),
        user_data_dir: str | None = None,
        stealth: bool = True,
    ) -> None:
        self._browser_type = browser_type
        self._headless = headless
        self._slow_mo = slow_mo
        self._viewport = {"width": viewport[0], "height": viewport[1]}
        self._user_data_dir = user_data_dir
        self._stealth = stealth
        self._playwright: Any = None
        self._browser: Any = None
        self._context: Any = None
        self._available = False
        self._last_error: str | None = None
        self._shutting_down = False  # suppresses spurious disconnect warning during stop()

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------

    # JS injected into every page BEFORE any site script runs.
    # Patches the most common automation-detection signals so sites like
    # YouTube don't treat the session as a headless/automated bot.
    _STEALTH_INIT_SCRIPT = """
    // navigator.webdriver -> undefined (Playwright/Selenium tell)
    Object.defineProperty(Navigator.prototype, 'webdriver', {get: () => undefined});

    // Realistic plugins/mimeTypes (headless Chrome reports empty arrays)
    Object.defineProperty(navigator, 'plugins', {
        get: () => [1, 2, 3, 4, 5].map(() => ({}))
    });
    Object.defineProperty(navigator, 'mimeTypes', {
        get: () => [1, 2].map(() => ({}))
    });

    // languages
    Object.defineProperty(navigator, 'languages', {get: () => ['en-US', 'en']});

    // chrome.runtime stub (absent in headless)
    window.chrome = window.chrome || { runtime: {} };

    // Permissions.query for notifications -> avoid 'denied' anomaly
    const origQuery = window.navigator.permissions && window.navigator.permissions.query;
    if (origQuery) {
        window.navigator.permissions.query = (parameters) => (
            parameters && parameters.name === 'notifications'
                ? Promise.resolve({ state: Notification.permission })
                : origQuery(parameters)
        );
    }

    // WebGL vendor/renderer spoof (headless reports 'Google SwiftShader')
    const getParameter = WebGLRenderingContext.prototype.getParameter;
    WebGLRenderingContext.prototype.getParameter = function (parameter) {
        if (parameter === 37445) return 'Intel Inc.';                 // UNMASKED_VENDOR_WEBGL
        if (parameter === 37446) return 'Intel Iris OpenGL Engine';   // UNMASKED_RENDERER_WEBGL
        return getParameter.call(this, parameter);
    };
    """

    # A realistic, current desktop UA matching real Chrome on Windows.
    # IMPORTANT: must match the OS Playwright is actually running on —
    # mismatched UA/platform pairs are themselves a detection signal.
    _DEFAULT_UA = (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
    )

    async def start(self) -> None:
        self._last_error: str | None = None
        try:
            from playwright.async_api import async_playwright
            from pathlib import Path
        except ImportError as exc:
            self._last_error = (
                f"playwright package not importable in this Python "
                f"environment ({exc}). If you installed it in a venv, make "
                f"sure JARVIS is launched with that venv's python."
            )
            log.warning(
                "Playwright not installed — BrowserManager will simulate actions (%s)",
                self._last_error,
            )
            return

        try:
            self._playwright = await async_playwright().start()

            # Automation-flag-suppressing launch args. --disable-blink-features
            # removes the "Chrome is being controlled by automated software"
            # banner and the associated navigator.webdriver=true behaviour.
            launch_args = [
                "--disable-blink-features=AutomationControlled",
                "--disable-features=IsolateOrigins,site-per-process",
                "--no-first-run",
                "--no-default-browser-check",
            ]

            context_kwargs = dict(
                viewport=self._viewport,
                user_agent=self._DEFAULT_UA,
                locale="en-US",
                timezone_id="Asia/Kathmandu",
                args=launch_args,
            )

            if self._user_data_dir:
                # Persistent profile: cookies, YouTube sign-in, consent
                # choices, and watch history survive across runs, which
                # makes the session look like a real returning user
                # rather than a fresh anonymous bot every time.
                profile_dir = Path(self._user_data_dir)
                profile_dir.mkdir(parents=True, exist_ok=True)
                launcher = getattr(self._playwright, self._browser_type)
                self._context = await launcher.launch_persistent_context(
                    str(profile_dir),
                    headless=self._headless,
                    slow_mo=self._slow_mo,
                    **context_kwargs,
                )
                self._browser = self._context.browser
            else:
                launcher = getattr(self._playwright, self._browser_type)
                self._browser = await launcher.launch(
                    headless=self._headless,
                    slow_mo=self._slow_mo,
                    args=launch_args,
                )
                self._context = await self._browser.new_context(**context_kwargs)

            if self._stealth:
                await self._context.add_init_script(self._STEALTH_INIT_SCRIPT)

            # ── Suppress Google/YouTube cookie-consent wall ──────────────
            # On a fresh persistent profile, the FIRST visit to youtube.com
            # (or google.com) shows a full-page "Before you continue to
            # YouTube — Accept all / Reject all" consent screen instead of
            # the actual page. ytd-video-renderer / search results never
            # render behind/under it, so eval_on_selector_all() correctly
            # finds 0 results — which is what produced "No YouTube results
            # found for '<query>'" even though the query itself was fine.
            # Pre-seeding the CONSENT cookie (a long-documented, widely-used
            # trick) makes Google treat consent as already given.
            try:
                await self._context.add_cookies([
                    {"name": "CONSENT", "value": "YES+1", "domain": ".youtube.com", "path": "/"},
                    {"name": "CONSENT", "value": "YES+1", "domain": ".google.com", "path": "/"},
                ])
            except Exception as exc:
                log.debug("Could not pre-seed CONSENT cookie: %s", exc)

            self._available = True

            # Guard against browser process being killed externally (e.g. user
            # closes the headless/headed window).  Without this, a Playwright
            # TargetClosedError propagates as an unhandled asyncio task exception
            # which — on Windows with some uvicorn versions — terminates the server.
            def _on_browser_disconnected():
                if self._shutting_down:
                    # Normal shutdown via stop() — not unexpected, don't warn
                    return
                log.warning(
                    "PlaywrightEngine: browser disconnected unexpectedly — "
                    "browser tools unavailable until next warm-up. "
                    "Set JARVIS_BROWSER_HEADLESS=1 to prevent accidental closure."
                )
                self._available = False
                self._context = None
                self._browser = None

            if self._browser is not None:
                self._browser.on("disconnected", lambda _: _on_browser_disconnected())
            elif self._context is not None:
                # Persistent context owns the browser — listen at context level
                self._context.on("close", lambda: _on_browser_disconnected())

            log.info(
                "PlaywrightEngine started (%s, headless=%s, persistent=%s, stealth=%s)",
                self._browser_type,
                self._headless,
                bool(self._user_data_dir),
                self._stealth,
            )
        except Exception as exc:
            self._last_error = str(exc)
            msg = str(exc)
            if "Executable doesn't exist" in msg or "playwright install" in msg:
                self._last_error = (
                    f"Playwright is installed but the browser binaries are "
                    f"missing. Run: playwright install {self._browser_type}\n"
                    f"({msg})"
                )
            elif "ProcessSingleton" in msg or "lock" in msg.lower() or "in use" in msg.lower():
                self._last_error = (
                    f"Could not launch persistent browser profile at "
                    f"'{self._user_data_dir}' — it may be locked by another "
                    f"running browser instance. Close any other JARVIS "
                    f"browser windows / leftover chromium processes and "
                    f"retry, or delete the lock file in that profile dir.\n"
                    f"({msg})"
                )
            log.error("Playwright start failed: %s", self._last_error)
            # Clean up partial state so retries start fresh
            try:
                if self._context:
                    await self._context.close()
            except Exception:
                pass
            try:
                if self._playwright:
                    await self._playwright.stop()
            except Exception:
                pass
            self._context = None
            self._browser = None
            self._playwright = None


    async def stop(self) -> None:
        self._shutting_down = True  # prevent spurious "disconnected unexpectedly" warning
        try:
            if self._context:
                await self._context.close()
            if self._browser and self._user_data_dir is None:
                # Persistent contexts own/close their browser implicitly;
                # only close explicitly for the non-persistent launch path.
                await self._browser.close()
            if self._playwright:
                await self._playwright.stop()
        except Exception as exc:
            log.warning(f"Playwright stop error: {exc}")

        self._available = False
        log.info("PlaywrightEngine stopped")

    # ------------------------------------------------------------------
    # Page management
    # ------------------------------------------------------------------

    async def new_page(self) -> Any:
        if not self._available:
            return _StubPage()

        # `launch_persistent_context` opens with one tab already present
        # (usually "about:blank" or a restored "New Tab" page). If we
        # blindly call context.new_page() here, that creates a SECOND
        # tab in the background — Playwright drives/navigates THAT tab,
        # but the window the user is looking at keeps showing the first
        # "about:blank" tab, making it look like "nothing happened" /
        # "only opens about:blank".
        #
        # Fix: reuse an existing blank/new-tab page if one exists and
        # has no other content, otherwise open a fresh tab — and in
        # either case bring it to the foreground.
        page = None
        for existing in list(self._context.pages):
            try:
                url = existing.url
            except Exception:
                url = ""
            if url in ("about:blank", "", "chrome://new-tab-page/"):
                page = existing
                break

        if page is None:
            page = await self._context.new_page()

        try:
            await page.bring_to_front()
        except Exception:
            pass

        log.debug("New Playwright page created/reused")
        return page

    async def close_page(self, page: Any) -> None:
        if self._available and hasattr(page, "close"):
            try:
                await page.close()
            except Exception as exc:
                log.warning(f"Page close error: {exc}")


    # ------------------------------------------------------------------
    # Atomic browser operations
    # ------------------------------------------------------------------

    async def navigate(self, page: Any, url: str, wait_until: str = "load") -> dict:
        """Navigate to URL. Returns {url, title, status}."""
        log.debug(f"navigate → {url}")

        if not self._available or isinstance(page, _StubPage):
            return {"url": url, "title": "stub", "status": 200}
        try:
            await page.bring_to_front()
        except Exception:
            pass
        response = await page.goto(url, wait_until=wait_until, timeout=30_000)
        return {
            "url": page.url,
            "title": await page.title(),
            "status": response.status if response else 0,
        }

    async def click(
        self, page: Any, selector: str, x: float | None = None, y: float | None = None
    ) -> dict:
        """Click element by selector or absolute coordinates."""
        log.debug(f"click selector={selector} x={x} y={y}")

        if not self._available or isinstance(page, _StubPage):
            return {"clicked": selector or f"({x},{y})"}
        if selector:
            await page.click(selector, timeout=10_000)
        elif x is not None and y is not None:
            await page.mouse.click(x, y)
        else:
            raise ValueError("click requires selector or (x, y)")
        return {"clicked": selector or f"({x},{y})"}

    async def type_text(
        self, page: Any, selector: str, text: str, delay: int = 30
    ) -> dict:
        """Type text into an element."""
        log.debug(f"type_text selector={selector} len={len(text)}")

        if not self._available or isinstance(page, _StubPage):
            return {"typed": text[:20] + "…"}
        await page.click(selector, timeout=5_000)
        await page.type(selector, text, delay=delay)
        return {"typed": f"{len(text)} chars into {selector}"}

    async def screenshot(self, page: Any, path: str | None = None) -> dict:
        """Take a screenshot. Returns base64 PNG."""
        log.debug(f"screenshot path={path}")

        if not self._available or isinstance(page, _StubPage):
            return {"b64": "", "path": path}
        options: dict = {"type": "png", "full_page": False}
        if path:
            options["path"] = path
            await page.screenshot(**options)
            return {"path": path}
        png_bytes = await page.screenshot(**options)
        b64 = base64.b64encode(png_bytes).decode()
        return {"b64": b64}

    async def extract_content(
        self, page: Any, selector: str = "body", attribute: str | None = None
    ) -> dict:
        """Extract text or attribute from an element."""
        log.debug(f"extract selector={selector} attribute={attribute}")

        if not self._available or isinstance(page, _StubPage):
            return {"content": ""}
        if attribute:
            val = await page.get_attribute(selector, attribute)
            return {"content": val or ""}
        text = await page.inner_text(selector)
        html = await page.inner_html(selector)
        return {"text": text, "html": html}

    async def execute_js(self, page: Any, script: str) -> dict:
        """Execute arbitrary JavaScript and return the result."""
        log.debug(f"execute_js script_len={len(script)}")

        if not self._available or isinstance(page, _StubPage):
            return {"result": None}
        result = await page.evaluate(script)
        return {"result": result}

    async def scroll(self, page: Any, x: int = 0, y: int = 500) -> dict:
        """Scroll the page by (x, y) pixels."""
        if not self._available or isinstance(page, _StubPage):
            return {"scrolled": True}
        await page.evaluate(f"window.scrollBy({x}, {y})")
        return {"scrolled": True, "delta_x": x, "delta_y": y}

    async def wait(
        self,
        page: Any,
        selector: str | None = None,
        ms: int = 1000,
    ) -> dict:
        """Wait for a selector to appear OR sleep ms milliseconds."""
        if not self._available or isinstance(page, _StubPage):
            await asyncio.sleep(min(ms, 100) / 1000)
            return {"waited": ms}
        if selector:
            await page.wait_for_selector(selector, timeout=ms)
            return {"waited_for": selector}
        await page.wait_for_timeout(ms)
        return {"waited_ms": ms}

    async def get_page_url(self, page: Any) -> str:
        if not self._available or isinstance(page, _StubPage):
            return ""
        return page.url

    async def get_page_title(self, page: Any) -> str:
        if not self._available or isinstance(page, _StubPage):
            return ""
        return await page.title()


# ---------------------------------------------------------------------------
# Stub for headless / no-playwright environments
# ---------------------------------------------------------------------------


class _StubPage:
    """No-op page used when Playwright is unavailable."""

    url: str = "about:blank"

    async def close(self) -> None:
        """No-op: Playwright is unavailable; nothing to close."""
        self.url = "about:blank"