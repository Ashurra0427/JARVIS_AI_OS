"""
JARVIS AI OS — Browser Actions
================================
Low-level browser operation execution layer.

Provides atomic browser operations implemented via Playwright.
All methods accept a Playwright Page object and operate directly on it.

Supported operations:
  open_url        — navigate to a URL
  click           — click a selector or coordinate
  type_text       — type text into a selector
  scroll          — scroll the page or an element
  extract_text    — extract visible text content
  take_screenshot — capture a screenshot

Used exclusively by BrowserManager and PlaywrightEngine.
Never called directly from agents.
"""

from __future__ import annotations

import base64
import time
from dataclasses import dataclass
from typing import Any

from observability.logging.logger import get_logger

log = get_logger(__name__)


# ---------------------------------------------------------------------------
# Result type
# ---------------------------------------------------------------------------


@dataclass
class BrowserActionResult:
    """Structured result from a browser action."""

    success: bool
    data: Any = None
    error: str = ""
    duration_ms: float = 0.0

    def as_dict(self) -> dict:
        return {
            "success": self.success,
            "data": self.data,
            "error": self.error,
            "duration_ms": self.duration_ms,
        }


# ---------------------------------------------------------------------------
# BrowserActions
# ---------------------------------------------------------------------------


class BrowserActions:
    """
    Low-level browser action executor.

    Operates on Playwright Page objects. Does NOT own sessions or manage
    lifecycles — that is BrowserManager's responsibility.

    Usage:
        ba = BrowserActions()
        result = await ba.open_url(page, "https://example.com")
        result = await ba.click(page, selector="#submit-btn")
        result = await ba.extract_text(page, selector="main")
    """

    # Default timeouts (milliseconds for Playwright, seconds for our layer)
    DEFAULT_NAVIGATION_TIMEOUT_MS = 30_000
    DEFAULT_ACTION_TIMEOUT_MS = 10_000
    DEFAULT_WAIT_AFTER_ACTION_MS = 200  # brief settle time

    def __init__(
        self,
        navigation_timeout_ms: int = DEFAULT_NAVIGATION_TIMEOUT_MS,
        action_timeout_ms: int = DEFAULT_ACTION_TIMEOUT_MS,
        wait_after_action_ms: int = DEFAULT_WAIT_AFTER_ACTION_MS,
    ) -> None:
        self._nav_timeout = navigation_timeout_ms
        self._act_timeout = action_timeout_ms
        self._wait_after = wait_after_action_ms

    # ------------------------------------------------------------------
    # open_url
    # ------------------------------------------------------------------

    async def open_url(
        self,
        page,
        url: str,
        wait_until: str = "domcontentloaded",
        timeout_ms: int | None = None,
    ) -> BrowserActionResult:
        """
        Navigate a page to a URL.

        wait_until options: "load", "domcontentloaded", "networkidle", "commit"
        """
        t0 = time.monotonic()
        timeout = timeout_ms or self._nav_timeout
        try:
            response = await page.goto(
                url,
                wait_until=wait_until,
                timeout=timeout,
            )
            duration_ms = (time.monotonic() - t0) * 1000
            status = response.status if response else 0

            if status >= 400:
                return BrowserActionResult(
                    success=False,
                    error=f"HTTP {status} for URL: {url}",
                    duration_ms=round(duration_ms, 1),
                )

            final_url = page.url
            title = await page.title()
            log.info("Browser navigated", url=url, final_url=final_url, status=status)
            return BrowserActionResult(
                success=True,
                data={"url": final_url, "title": title, "status": status},
                duration_ms=round(duration_ms, 1),
            )
        except Exception as exc:
            duration_ms = (time.monotonic() - t0) * 1000
            log.warning("Browser navigation failed", url=url, error=str(exc))
            return BrowserActionResult(
                success=False,
                error=str(exc),
                duration_ms=round(duration_ms, 1),
            )

    # ------------------------------------------------------------------
    # click
    # ------------------------------------------------------------------

    async def click(
        self,
        page,
        selector: str | None = None,
        x: float | None = None,
        y: float | None = None,
        button: str = "left",
        click_count: int = 1,
        timeout_ms: int | None = None,
    ) -> BrowserActionResult:
        """
        Click a selector or screen coordinate.
        Provide either selector OR (x, y) coordinates.
        """
        t0 = time.monotonic()
        timeout = timeout_ms or self._act_timeout
        try:
            if selector:
                await page.click(
                    selector,
                    button=button,
                    click_count=click_count,
                    timeout=timeout,
                )
                target = f"selector={selector}"
            elif x is not None and y is not None:
                await page.mouse.click(x, y, button=button, click_count=click_count)
                target = f"({x}, {y})"
            else:
                return BrowserActionResult(
                    success=False,
                    error="click requires either selector or (x, y) coordinates",
                )

            await page.wait_for_timeout(self._wait_after)
            duration_ms = (time.monotonic() - t0) * 1000
            log.debug("Browser click", target=target)
            return BrowserActionResult(
                success=True,
                data={"target": target},
                duration_ms=round(duration_ms, 1),
            )
        except Exception as exc:
            duration_ms = (time.monotonic() - t0) * 1000
            log.warning("Browser click failed", selector=selector, error=str(exc))
            return BrowserActionResult(
                success=False,
                error=str(exc),
                duration_ms=round(duration_ms, 1),
            )

    # ------------------------------------------------------------------
    # type_text
    # ------------------------------------------------------------------

    async def type_text(
        self,
        page,
        selector: str,
        text: str,
        delay_ms: int = 30,  # delay between keystrokes (ms)
        clear_first: bool = True,
        timeout_ms: int | None = None,
    ) -> BrowserActionResult:
        """
        Type text into an input element identified by selector.
        Optionally clears the field before typing.
        """
        t0 = time.monotonic()
        timeout = timeout_ms or self._act_timeout
        try:
            # Wait for element and ensure it's editable
            locator = page.locator(selector)
            await locator.wait_for(state="visible", timeout=timeout)

            if clear_first:
                await locator.clear()

            await locator.type(text, delay=delay_ms)

            await page.wait_for_timeout(self._wait_after)
            duration_ms = (time.monotonic() - t0) * 1000
            log.debug("Browser typed", selector=selector, length=len(text))
            return BrowserActionResult(
                success=True,
                data={"selector": selector, "length": len(text)},
                duration_ms=round(duration_ms, 1),
            )
        except Exception as exc:
            duration_ms = (time.monotonic() - t0) * 1000
            log.warning("Browser type failed", selector=selector, error=str(exc))
            return BrowserActionResult(
                success=False,
                error=str(exc),
                duration_ms=round(duration_ms, 1),
            )

    # ------------------------------------------------------------------
    # scroll
    # ------------------------------------------------------------------

    async def scroll(
        self,
        page,
        direction: str = "down",  # up | down | left | right
        amount: int = 500,  # pixels
        selector: str | None = None,  # scroll inside element if given
    ) -> BrowserActionResult:
        """
        Scroll the page or a specific element.
        """
        t0 = time.monotonic()
        try:
            delta_x, delta_y = 0, 0
            if direction == "down":
                delta_y = amount
            elif direction == "up":
                delta_y = -amount
            elif direction == "right":
                delta_x = amount
            elif direction == "left":
                delta_x = -amount
            else:
                return BrowserActionResult(
                    success=False,
                    error=f"Unknown scroll direction: '{direction}'. Use up/down/left/right.",
                )

            if selector:
                await page.locator(selector).evaluate(
                    "(el, [dx, dy]) => el.scrollBy(dx, dy)", [delta_x, delta_y]
                )
            else:
                await page.mouse.wheel(delta_x, delta_y)

            await page.wait_for_timeout(self._wait_after)
            duration_ms = (time.monotonic() - t0) * 1000
            return BrowserActionResult(
                success=True,
                data={"direction": direction, "amount": amount},
                duration_ms=round(duration_ms, 1),
            )
        except Exception as exc:
            duration_ms = (time.monotonic() - t0) * 1000
            log.warning("Browser scroll failed", error=str(exc))
            return BrowserActionResult(
                success=False,
                error=str(exc),
                duration_ms=round(duration_ms, 1),
            )

    # ------------------------------------------------------------------
    # extract_text
    # ------------------------------------------------------------------

    async def extract_text(
        self,
        page,
        selector: str | None = None,  # None = full page
        max_length: int = 50_000,
        include_html: bool = False,
    ) -> BrowserActionResult:
        """
        Extract visible text from the page or a specific element.
        Optionally includes the raw HTML.
        """
        t0 = time.monotonic()
        try:
            if selector:
                locator = page.locator(selector)
                text = await locator.inner_text()
                html = await locator.inner_html() if include_html else None
            else:
                # Full page visible text
                text = await page.evaluate("() => document.body.innerText")
                html = await page.content() if include_html else None

            # Truncate if necessary
            if len(text) > max_length:
                text = text[:max_length] + f"\n[... truncated at {max_length} chars]"

            duration_ms = (time.monotonic() - t0) * 1000
            data: dict[str, Any] = {
                "text": text,
                "length": len(text),
                "url": page.url,
            }
            if html:
                data["html"] = html[:max_length] if len(html) > max_length else html

            return BrowserActionResult(
                success=True,
                data=data,
                duration_ms=round(duration_ms, 1),
            )
        except Exception as exc:
            duration_ms = (time.monotonic() - t0) * 1000
            log.warning(
                "Browser extract_text failed", selector=selector, error=str(exc)
            )
            return BrowserActionResult(
                success=False,
                error=str(exc),
                duration_ms=round(duration_ms, 1),
            )

    # ------------------------------------------------------------------
    # take_screenshot
    # ------------------------------------------------------------------

    async def take_screenshot(
        self,
        page,
        selector: str | None = None,
        full_page: bool = False,
        format: str = "png",  # png | jpeg
        quality: int | None = None,  # jpeg only, 0–100
    ) -> BrowserActionResult:
        """
        Capture a screenshot of the full page, viewport, or a specific element.
        Returns base64-encoded image data.
        """
        t0 = time.monotonic()
        try:
            kwargs: dict[str, Any] = {"type": format}
            if full_page:
                kwargs["full_page"] = True
            if quality is not None and format == "jpeg":
                kwargs["quality"] = quality

            if selector:
                locator = page.locator(selector)
                raw_bytes = await locator.screenshot(**kwargs)
            else:
                raw_bytes = await page.screenshot(**kwargs)

            b64 = base64.b64encode(raw_bytes).decode("utf-8")
            duration_ms = (time.monotonic() - t0) * 1000

            return BrowserActionResult(
                success=True,
                data={
                    "image_b64": b64,
                    "format": format,
                    "size_bytes": len(raw_bytes),
                    "url": page.url,
                },
                duration_ms=round(duration_ms, 1),
            )
        except Exception as exc:
            duration_ms = (time.monotonic() - t0) * 1000
            log.warning("Browser screenshot failed", error=str(exc))
            return BrowserActionResult(
                success=False,
                error=str(exc),
                duration_ms=round(duration_ms, 1),
            )

    # ------------------------------------------------------------------
    # wait_for_selector
    # ------------------------------------------------------------------

    async def wait_for_selector(
        self,
        page,
        selector: str,
        state: str = "visible",  # attached | detached | visible | hidden
        timeout_ms: int | None = None,
    ) -> BrowserActionResult:
        """Wait for an element to reach a specific state."""
        t0 = time.monotonic()
        timeout = timeout_ms or self._act_timeout
        try:
            await page.wait_for_selector(selector, state=state, timeout=timeout)
            duration_ms = (time.monotonic() - t0) * 1000
            return BrowserActionResult(
                success=True,
                data={"selector": selector, "state": state},
                duration_ms=round(duration_ms, 1),
            )
        except Exception as exc:
            duration_ms = (time.monotonic() - t0) * 1000
            return BrowserActionResult(
                success=False,
                error=str(exc),
                duration_ms=round(duration_ms, 1),
            )

    # ------------------------------------------------------------------
    # execute_script
    # ------------------------------------------------------------------

    async def execute_script(
        self,
        page,
        script: str,
        arg: Any = None,
    ) -> BrowserActionResult:
        """Execute JavaScript on the page and return the result."""
        t0 = time.monotonic()
        try:
            if arg is not None:
                result = await page.evaluate(script, arg)
            else:
                result = await page.evaluate(script)
            duration_ms = (time.monotonic() - t0) * 1000
            return BrowserActionResult(
                success=True,
                data={"result": result},
                duration_ms=round(duration_ms, 1),
            )
        except Exception as exc:
            duration_ms = (time.monotonic() - t0) * 1000
            log.warning("Browser script execution failed", error=str(exc))
            return BrowserActionResult(
                success=False,
                error=str(exc),
                duration_ms=round(duration_ms, 1),
            )
