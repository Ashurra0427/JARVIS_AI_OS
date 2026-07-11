"""
tools/web_tools/web_tools.py
─────────────────────────────
Web tool implementations for JARVIS AI OS.

Provides:
  web.search        — search the web via DuckDuckGo (no API key required)
  web.scrape        — fetch raw HTML from a URL
  web.extract_text  — fetch URL and return clean text (strips HTML)
  web.download      — download a binary file to local disk
  web.summarize     — fetch URL and return a brief text summary

All tools register through ToolRegistry and return ToolResult objects.
EventBus events are emitted for every invocation.
"""

from __future__ import annotations

import logging
import os
import re
import time
import urllib.request
import urllib.parse
import urllib.error
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from tools.registry.tool_registry import ToolRegistry

log = logging.getLogger(__name__)

# ──────────────────────────────────────────────
# Optional dependencies (graceful degradation)
# ──────────────────────────────────────────────

try:
    import requests as _requests

    _HAS_REQUESTS = True
except ImportError:
    _HAS_REQUESTS = False

try:
    from bs4 import BeautifulSoup as _BS

    _HAS_BS4 = True
except ImportError:
    _HAS_BS4 = False


# ──────────────────────────────────────────────
# Helpers
# ──────────────────────────────────────────────

_DEFAULT_HEADERS = {
    "User-Agent": "JARVIS-AI-OS/1.0 (compatible; research bot)",
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
}
_MAX_RESPONSE_BYTES = 5 * 1024 * 1024  # 5 MB


def _fetch(url: str, timeout: int = 20) -> str:
    """Fetch a URL and return response text (stdlib fallback if requests absent)."""
    if _HAS_REQUESTS:
        resp = _requests.get(url, headers=_DEFAULT_HEADERS, timeout=timeout)
        resp.raise_for_status()
        return resp.text[:_MAX_RESPONSE_BYTES]
    # stdlib fallback
    req = urllib.request.Request(url, headers=_DEFAULT_HEADERS)
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return r.read(_MAX_RESPONSE_BYTES).decode("utf-8", errors="replace")


def _strip_html(html: str) -> str:
    """Remove HTML tags; use BeautifulSoup if available, else regex."""
    if _HAS_BS4:
        soup = _BS(html, "html.parser")
        # remove script/style
        for tag in soup(["script", "style", "nav", "footer"]):
            tag.decompose()
        return soup.get_text(separator="\n", strip=True)
    # simple regex fallback
    text = re.sub(r"<script[^>]*>.*?</script>", "", html, flags=re.S | re.I)
    text = re.sub(r"<style[^>]*>.*?</style>", "", text, flags=re.S | re.I)
    text = re.sub(r"<[^>]+>", " ", text)
    text = re.sub(r"\s{2,}", "\n", text)
    return text.strip()


# ──────────────────────────────────────────────
# Tool implementations
# ──────────────────────────────────────────────


def web_search(query: str, max_results: int = 10) -> dict:
    """
    Search the web using DuckDuckGo.

    Tries Playwright first (if available) to bypass bot-detection / CAPTCHA
    pages DuckDuckGo serves to raw HTTP clients. Falls back to a direct
    HTTP request if Playwright is not installed or fails.

    Returns a dict with:
      query       — original query
      results     — list of {title, url, snippet}
      result_count — number of results found
    """
    if not query or not query.strip():
        raise ValueError("query must be a non-empty string")

    encoded = urllib.parse.quote_plus(query)

    # ── Try Playwright-backed search first ──────────────────────────────
    try:
        from tools.browser_tools.browser_tools import _run, _ensure_page
        engine = None
        try:
            from tools.browser_tools.browser_tools import _get_engine
            engine = _get_engine()
        except Exception:
            pass

        async def _browser_search():
            page = await _ensure_page()
            search_url = f"https://html.duckduckgo.com/html/?q={encoded}"
            if engine and getattr(engine, "_available", False):
                await engine.navigate(page, search_url, wait_until="domcontentloaded")
                await engine.wait(page, selector=".result", ms=10_000)
            results = []
            if hasattr(page, "eval_on_selector_all"):
                raw = await page.eval_on_selector_all(
                    ".result",
                    "els => els.map(e => {"
                    "  const t = e.querySelector('.result__title a');"
                    "  const s = e.querySelector('.result__snippet');"
                    "  return {"
                    "    title: t ? t.innerText.trim() : '',"
                    "    href: t ? t.href : '',"
                    "    snippet: s ? s.innerText.trim() : ''"
                    "  };"
                    "}).filter(r => r.title)",
                )
                for r in raw[:max_results]:
                    href = r.get("href", "")
                    if "uddg=" in href:
                        href = urllib.parse.unquote(
                            urllib.parse.parse_qs(
                                urllib.parse.urlparse(href).query
                            ).get("uddg", [href])[0]
                        )
                    results.append(
                        {
                            "title": r.get("title", ""),
                            "url": href,
                            "snippet": r.get("snippet", ""),
                        }
                    )
            return results

        results = _run(_browser_search(), timeout=60)
        if results:
            log.debug("web.search (playwright): query=%r → %d results", query, len(results))
            return {
                "query": query,
                "results": results,
                "result_count": len(results),
            }
    except Exception as exc:
        log.debug("web.search playwright fallback failed: %s", exc)

    # ── Fallback: direct HTTP request ───────────────────────────────────
    url = f"https://html.duckduckgo.com/html/?q={encoded}"
    html = _fetch(url)

    results = []
    if _HAS_BS4:
        soup = _BS(html, "html.parser")
        for r in soup.select(".result")[:max_results]:
            title_el = r.select_one(".result__title a")
            snippet_el = r.select_one(".result__snippet")
            if title_el:
                href = title_el.get("href", "")
                # DDG wraps URLs — extract real URL
                if "uddg=" in href:
                    href = urllib.parse.unquote(
                        urllib.parse.parse_qs(urllib.parse.urlparse(href).query).get(
                            "uddg", [href]
                        )[0]
                    )
                results.append(
                    {
                        "title": title_el.get_text(strip=True),
                        "url": href,
                        "snippet": snippet_el.get_text(strip=True)
                        if snippet_el
                        else "",
                    }
                )
    else:
        # regex fallback
        titles = re.findall(r'class="result__title"[^>]*><a[^>]+>([^<]+)', html)
        snippets = re.findall(r'class="result__snippet"[^>]*>(.*?)</a', html, re.S)
        urls = re.findall(r'href="(https?://[^"]+)"', html)
        for i, title in enumerate(titles[:max_results]):
            results.append(
                {
                    "title": title.strip(),
                    "url": urls[i] if i < len(urls) else "",
                    "snippet": _strip_html(snippets[i]) if i < len(snippets) else "",
                }
            )

    log.debug("web.search: query=%r → %d results", query, len(results))
    return {"query": query, "results": results, "result_count": len(results)}


def web_scrape(url: str) -> dict:
    """
    Fetch raw HTML from a URL.

    Returns:
      url        — requested URL
      html       — raw HTML content
      byte_count — size of response
    """
    if not url:
        raise ValueError("url must be provided")

    html = _fetch(url)
    return {"url": url, "html": html, "byte_count": len(html)}


def web_extract_text(url: str) -> dict:
    """
    Fetch a URL and return clean, readable text (HTML stripped).

    Returns:
      url       — requested URL
      text      — clean text content
      char_count — length of extracted text
    """
    if not url:
        raise ValueError("url must be provided")

    html = _fetch(url)
    text = _strip_html(html)
    return {"url": url, "text": text, "char_count": len(text)}


def web_download(url: str, dest_path: str = "") -> dict:
    """
    Download a binary or text file from a URL to local disk.

    Args:
      url       — URL to download
      dest_path — local file path; auto-generated in /tmp if omitted

    Returns:
      url       — original URL
      dest_path — where the file was saved
      byte_count — file size
    """
    if not url:
        raise ValueError("url must be provided")

    if not dest_path:
        filename = os.path.basename(urllib.parse.urlparse(url).path) or "download"
        dest_path = os.path.join("/tmp", f"jarvis_{filename}")

    if _HAS_REQUESTS:
        resp = _requests.get(url, headers=_DEFAULT_HEADERS, timeout=70, stream=True)
        resp.raise_for_status()
        total = 0
        with open(dest_path, "wb") as f:
            for chunk in resp.iter_content(chunk_size=65536):
                f.write(chunk)
                total += len(chunk)
                if total > _MAX_RESPONSE_BYTES:
                    break
    else:
        req = urllib.request.Request(url, headers=_DEFAULT_HEADERS)
        with urllib.request.urlopen(req, timeout=60) as r:
            data = r.read(_MAX_RESPONSE_BYTES)
        with open(dest_path, "wb") as f:
            f.write(data)
        total = len(data)

    log.debug("web.download: %s → %s (%d bytes)", url, dest_path, total)
    return {"url": url, "dest_path": dest_path, "byte_count": total}


def web_summarize(url: str, max_chars: int = 2000) -> dict:
    """
    Fetch a URL and return a condensed text summary (first max_chars chars of content).

    Returns:
      url     — requested URL
      summary — condensed text content
      full_char_count — length before truncation
    """
    if not url:
        raise ValueError("url must be provided")

    html = _fetch(url)
    text = _strip_html(html)
    full_len = len(text)
    summary = text[:max_chars]
    if full_len > max_chars:
        summary += f"\n\n[... {full_len - max_chars} more characters truncated]"

    return {"url": url, "summary": summary, "full_char_count": full_len}


# ──────────────────────────────────────────────
# Registration
# ──────────────────────────────────────────────


def register_web_tools(registry: "ToolRegistry", event_bus=None) -> list[str]:
    """
    Register all web tools into the provided ToolRegistry.

    Returns list of registered tool names.
    """
    from tools.registry.tool_registry import ToolDefinition

    def _wrap(fn, name: str):
        """Wrap a tool function to emit EventBus events and log invocations."""
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
        ToolDefinition(
            name="web.search",
            handler=_wrap(web_search, "web.search"),
            description="Search the web and return a list of results (title, URL, snippet).",
            tags=["web", "search", "research"],
            aliases=["search_web"],
            timeout_s=30.0,
        ),
        ToolDefinition(
            name="web.scrape",
            handler=_wrap(web_scrape, "web.scrape"),
            description="Fetch raw HTML from a URL.",
            tags=["web", "scrape", "html"],
            timeout_s=30.0,
        ),
        ToolDefinition(
            name="web.extract_text",
            handler=_wrap(web_extract_text, "web.extract_text"),
            description="Fetch a URL and return clean readable text (HTML stripped).",
            tags=["web", "extract", "text"],
            timeout_s=30.0,
        ),
        ToolDefinition(
            name="web.download",
            handler=_wrap(web_download, "web.download"),
            description="Download a file from a URL to local disk.",
            tags=["web", "download", "file"],
            timeout_s=120.0,
        ),
        ToolDefinition(
            name="web.summarize",
            handler=_wrap(web_summarize, "web.summarize"),
            description="Fetch a URL and return a condensed text summary.",
            tags=["web", "summarize", "research"],
            timeout_s=30.0,
        ),
    ]

    registered = []
    for defn in tools:
        registry.register(defn)
        registered.append(defn.name)
        log.info("Registered tool: %s", defn.name)

    return registered
