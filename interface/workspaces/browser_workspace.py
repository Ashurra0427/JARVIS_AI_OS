"""
interface/workspaces/browser_workspace.py
══════════════════════════════════════════════════════════════════════════════
JARVIS AI OS — Browser Workspace

Full-featured in-app browser panel powered by the existing Playwright engine.

Layout:
  ┌─ BrowserWorkspace ──────────────────────────────────────────────────────┐
  │  ┌─ Toolbar ──────────────────────────────────────────────────────────┐ │
  │  │  [←][→][⟳]  [URL / Search bar]  [🔍 Search]  [📸 Screenshot]     │ │
  │  └────────────────────────────────────────────────────────────────────┘ │
  │  ┌─ Tabs row ─────────────────────────────────────────────────────────┐ │
  │  │  [Tab 1 ×]  [Tab 2 ×]  [+ New Tab]                                │ │
  │  └────────────────────────────────────────────────────────────────────┘ │
  │  ┌─ LEFT sidebar ──┐  ┌─ Page content / status area ─────────────────┐ │
  │  │  Quick Links     │  │                                               │ │
  │  │  ─────────────   │  │   Page title, URL, screenshot preview,        │ │
  │  │  ● Google        │  │   extracted text output, status log           │ │
  │  │  ● GitHub        │  │                                               │ │
  │  │  ● Docs          │  └───────────────────────────────────────────────┘ │
  │  │  ─────────────   │                                                   │
  │  │  History         │                                                   │
  │  └─────────────────┘                                                   │
  └─────────────────────────────────────────────────────────────────────────┘

Wire-up:
  - navigate_requested(url)  → ServerAdapter or BrowserManager via EventBus
  - search_requested(query)  → triggers web search via agent
  - Screenshots placed in BG_CARD panels; text extraction shown inline
  - All blocking Playwright calls run in a QThread worker
"""
from __future__ import annotations

import asyncio
import base64
import logging
import threading
import time
from typing import Optional, List
from urllib.parse import quote_plus, urlparse

from PySide6.QtCore import (
    Qt, Signal, Slot, QTimer, QThread, QObject, QSize,
)
from PySide6.QtGui import (
    QColor, QFont, QPainter, QPen, QBrush, QLinearGradient,
    QPixmap, QImage,
)
from PySide6.QtWidgets import (
    QWidget, QVBoxLayout, QHBoxLayout, QLabel,
    QFrame, QScrollArea, QSizePolicy, QPushButton,
    QLineEdit, QTextEdit, QSplitter, QTabBar,
    QProgressBar,
)

from interface.themes.palette import (
    BG_WINDOW, BG_SURFACE, BG_ELEVATED, BG_CARD, BG_INPUT, BG_HIGHLIGHT,
    BORDER_DEFAULT, BORDER_ACCENT, BORDER_ACTIVE,
    TEXT_PRIMARY, TEXT_SECONDARY, TEXT_MUTED, TEXT_ACCENT,
    ACCENT_CYAN, ACCENT_GREEN, ACCENT_YELLOW, ACCENT_RED,
    ACCENT_PURPLE, ACCENT_ORANGE,
    STATUS_RUNNING, STATUS_IDLE, STATUS_ERROR,
    q,
)
from interface.panels.settings_panel import load_settings

log = logging.getLogger(__name__)

# ── Quick-access bookmarks ─────────────────────────────────────────────────────

QUICK_LINKS = [
    ("🌐", "Google",      "https://www.google.com"),
    ("🐙", "GitHub",      "https://github.com"),
    ("📖", "Wikipedia",   "https://en.wikipedia.org"),
    ("📰", "Hacker News", "https://news.ycombinator.com"),
    ("🤖", "Anthropic",   "https://anthropic.com"),
    ("📚", "arXiv",       "https://arxiv.org"),
    ("🎯", "YouTube",     "https://youtube.com"),
    ("🐦", "Twitter/X",   "https://x.com"),
]


# ─────────────────────────────────────────────────────────────────────────────
# Background Playwright worker thread
# ─────────────────────────────────────────────────────────────────────────────

class _BrowserWorker(QObject):
    """
    Runs Playwright operations in a background thread / asyncio loop.
    All results are emitted as Qt signals back to the UI thread.
    """

    page_loaded     = Signal(str, str, str)   # url, title, screenshot_b64
    text_extracted  = Signal(str, str)         # url, text
    search_done     = Signal(str, str, str)    # query, result_url, screenshot_b64
    error_occurred  = Signal(str)              # error message
    status_changed  = Signal(str)              # status text
    screenshot_done = Signal(str, str)         # url, screenshot_b64

    def __init__(self, parent=None):
        super().__init__(parent)
        self._loop: Optional[asyncio.AbstractEventLoop] = None
        self._engine = None
        self._page = None
        self._ready  = False

        # Pull current Browser settings from the Settings panel (persisted JSON)
        settings = load_settings().get("browser", {})
        self._browser_type   = settings.get("browser_type", "chromium")
        self._headless       = settings.get("headless", False)
        self._viewport       = (
            settings.get("viewport_w", 1280),
            settings.get("viewport_h", 900),
        )
        self._search_engine  = settings.get(
            "search_engine", "https://duckduckgo.com/?q={query}"
        )
        # Persistent browser profile — keeps YouTube/site cookies, consent
        # choices, and sign-in state across runs (greatly reduces bot
        # detection / "Something went wrong" playback failures).
        from pathlib import Path
        default_profile = str(Path("datastore") / "browser_profile")
        self._user_data_dir = settings.get("profile_dir", default_profile)

    def apply_settings(self, settings: dict):
        """
        Apply a freshly-saved Browser settings dict (from SettingsPanel).
        Search-engine template applies immediately; browser_type/headless/
        viewport require an engine restart to take effect.
        """
        browser = settings.get("browser", {})
        if "search_engine" in browser and browser["search_engine"]:
            self._search_engine = browser["search_engine"]
        new_type     = browser.get("browser_type", self._browser_type)
        new_headless = browser.get("headless", self._headless)
        new_viewport = (
            browser.get("viewport_w", self._viewport[0]),
            browser.get("viewport_h", self._viewport[1]),
        )
        if (new_type, new_headless, new_viewport) != (
            self._browser_type, self._headless, self._viewport
        ):
            self._browser_type, self._headless, self._viewport = (
                new_type, new_headless, new_viewport,
            )
            if self._loop:
                asyncio.run_coroutine_threadsafe(self._restart_engine(), self._loop)

    async def _restart_engine(self):
        self.status_changed.emit("🔄  Restarting browser engine with new settings…")
        try:
            if self._engine:
                if self._page is not None:
                    await self._engine.close_page(self._page)
                    self._page = None
                await self._engine.stop()
        except Exception:
            pass
        await self._init_engine()

    def start_engine(self):
        """Called from the worker thread to initialise the event loop + Playwright.

        FIX(phase12): The original code used run_until_complete(_init_engine()) then
        returned, leaving the event loop in a STOPPED state.  All subsequent
        asyncio.run_coroutine_threadsafe() calls (navigate, search, screenshot, …)
        posted coroutines to a dead loop — they were queued but never executed,
        causing every browser action to silently hang forever.

        Fix: schedule _init_engine() as a startup task then call run_forever() so
        the loop stays alive for the lifetime of the worker thread.  All subsequent
        run_coroutine_threadsafe() calls now execute correctly.
        """
        self._loop = asyncio.new_event_loop()
        asyncio.set_event_loop(self._loop)

        # Schedule engine init as the first task on the live loop.
        async def _boot():
            await self._init_engine()
            self._ready = True

        self._loop.call_soon_threadsafe(
            lambda: asyncio.ensure_future(_boot(), loop=self._loop)
        )
        # Keep the loop running permanently — all navigate/search/screenshot
        # coroutines are dispatched here via run_coroutine_threadsafe().
        # loop.stop() is called by the stop() method when the workspace closes.
        self._loop.run_forever()

    async def _init_engine(self):
        try:
            from actions.browser.playwright_engine import PlaywrightEngine
            self._engine = PlaywrightEngine(
                browser_type=self._browser_type,
                headless=self._headless,
                viewport=self._viewport,
                user_data_dir=self._user_data_dir,
            )
            await self._engine.start()
            if self._engine._available:
                self._page = await self._engine.new_page()
                self.status_changed.emit(
                    f"✅  Browser engine ready ({self._browser_type}, "
                    f"{'headless' if self._headless else 'windowed'})"
                )
            else:
                detail = getattr(self._engine, "_last_error", None)
                if detail:
                    self.status_changed.emit(f"⚠️  Browser engine unavailable: {detail}")
                else:
                    self.status_changed.emit(
                        "⚠️  Playwright not installed — install with: "
                        "pip install playwright && playwright install chromium"
                    )
        except Exception as exc:
            log.warning("Playwright not available: %s", exc)
            self._engine = None
            self._page = None
            self.status_changed.emit(f"⚠️  Browser engine error: {exc}")

    def navigate(self, url: str):
        if not self._loop:
            return
        asyncio.run_coroutine_threadsafe(self._do_navigate(url), self._loop)

    def search(self, query: str):
        try:
            url = self._search_engine.format(query=quote_plus(query))
        except Exception:
            url = f"https://duckduckgo.com/?q={quote_plus(query)}"
        if not self._loop:
            self.search_done.emit(query, url, "")
            return
        asyncio.run_coroutine_threadsafe(self._do_search(query, url), self._loop)

    def take_screenshot(self):
        if not self._loop:
            return
        asyncio.run_coroutine_threadsafe(self._do_screenshot(), self._loop)

    def extract_text(self, url: str):
        if not self._loop:
            return
        asyncio.run_coroutine_threadsafe(self._do_extract(url), self._loop)

    async def _ensure_page(self):
        if self._engine and self._engine._available and self._page is None:
            self._page = await self._engine.new_page()
        return self._page

    async def _do_navigate(self, url: str):
        self.status_changed.emit(f"🔄  Loading {url}…")
        try:
            if self._engine and self._engine._available:
                page = await self._ensure_page()
                result = await self._engine.navigate(page, url)
                title = result.get("title", url) if isinstance(result, dict) else url
                ss_b64 = await self._do_screenshot_raw()
                self.page_loaded.emit(url, title, ss_b64)
                self.status_changed.emit(f"✅  Loaded: {title}")
            else:
                # Stub mode — just report the navigation
                title = urlparse(url).netloc or url
                self.page_loaded.emit(url, title, "")
                self.status_changed.emit(f"📄  [Stub] Would navigate to: {url}")
        except Exception as exc:
            self.error_occurred.emit(str(exc))
            self.status_changed.emit(f"❌  Error: {exc}")

    async def _do_search(self, query: str, url: str):
        self.status_changed.emit(f"🔍  Searching: {query}…")
        try:
            if self._engine and self._engine._available:
                page = await self._ensure_page()
                await self._engine.navigate(page, url)
                ss_b64 = await self._do_screenshot_raw()
                self.search_done.emit(query, url, ss_b64)
                self.status_changed.emit(f"✅  Search results for: {query}")
            else:
                self.search_done.emit(query, url, "")
                self.status_changed.emit(f"🔍  [Stub] Search: {query} → {url}")
        except Exception as exc:
            self.error_occurred.emit(str(exc))
            self.status_changed.emit(f"❌  Search error: {exc}")

    async def _do_screenshot_raw(self) -> str:
        try:
            if self._engine and self._engine._available and self._page is not None:
                result = await self._engine.screenshot(self._page)
                return result.get("b64", "") if isinstance(result, dict) else ""
        except Exception:
            pass
        return ""

    async def _do_screenshot(self):
        ss = await self._do_screenshot_raw()
        if ss:
            url = ""
            try:
                if self._engine and self._page is not None:
                    url = await self._engine.get_page_url(self._page)
            except Exception:
                pass
            self.screenshot_done.emit(url, ss)

    async def _do_extract(self, url: str):
        try:
            if self._engine and self._engine._available:
                page = await self._ensure_page()
                # Navigate first if the requested URL differs from the current page
                current = await self._engine.get_page_url(page)
                if url and url != current:
                    await self._engine.navigate(page, url)
                result = await self._engine.extract_content(page, "body")
                text = result.get("text", "") if isinstance(result, dict) else str(result)
                self.text_extracted.emit(url, text[:4000])
            else:
                self.text_extracted.emit(url, f"[Stub] Text from {url}")
        except Exception as exc:
            self.text_extracted.emit(url, f"Error extracting text: {exc}")

    def stop(self):
        """Cleanly shut down the browser engine and its event loop.

        P13 bug fix: this used to fire engine.stop() (an async coroutine)
        and loop.stop() via two independent call_soon_threadsafe /
        run_coroutine_threadsafe calls at nearly the same instant — racing
        the event loop's shutdown against Playwright's own async teardown.
        If the loop stopped first, the engine.stop() coroutine was
        abandoned mid-flight (visible as "Task was destroyed but it is
        pending" warnings), which can leave the underlying headless
        Chromium subprocess orphaned instead of properly closed. Now
        loop.stop() only runs after engine.stop() has actually finished
        (success or failure), as part of the same scheduled coroutine —
        no more race.
        """
        if not self._loop:
            return

        async def _shutdown():
            try:
                if self._engine:
                    await self._engine.stop()
            except Exception:
                log.debug("Error stopping Playwright engine", exc_info=True)
            finally:
                self._loop.stop()

        if self._loop.is_running():
            asyncio.run_coroutine_threadsafe(_shutdown(), self._loop)
        else:
            self._loop.call_soon_threadsafe(self._loop.stop)


class _BrowserThread(QThread):
    def __init__(self, worker: _BrowserWorker, parent=None):
        super().__init__(parent)
        self._worker = worker

    def run(self):
        self._worker.start_engine()
        # Keep the thread alive — the event loop runs inside start_engine()
        while not self.isInterruptionRequested():
            self.msleep(200)


# ─────────────────────────────────────────────────────────────────────────────
# Tab bar item
# ─────────────────────────────────────────────────────────────────────────────

class _TabItem(QWidget):
    close_clicked = Signal(int)  # tab index
    activated     = Signal(int)

    def __init__(self, title: str, idx: int, parent=None):
        super().__init__(parent)
        self._idx = idx
        self._active = False
        self.setFixedHeight(32)
        self.setMinimumWidth(120)
        self.setMaximumWidth(200)
        self.setCursor(Qt.CursorShape.PointingHandCursor)

        lay = QHBoxLayout(self)
        lay.setContentsMargins(10, 0, 6, 0)
        lay.setSpacing(6)

        self._title = QLabel(title)
        self._title.setStyleSheet(f"color: {TEXT_SECONDARY}; font-size: 10px; background: transparent;")
        self._title.setMaximumWidth(140)
        lay.addWidget(self._title, 1)

        close = QPushButton("×")
        close.setFixedSize(16, 16)
        close.setStyleSheet(f"""
            QPushButton {{
                background: transparent; color: {TEXT_MUTED};
                border: none; font-size: 13px; font-weight: 700;
            }}
            QPushButton:hover {{ color: {ACCENT_RED}; }}
        """)
        close.clicked.connect(lambda: self.close_clicked.emit(self._idx))
        lay.addWidget(close)
        self._refresh()

    def set_title(self, t: str):
        self._title.setText(t[:24] + "…" if len(t) > 24 else t)

    def set_active(self, active: bool):
        self._active = active
        self._refresh()

    def _refresh(self):
        if self._active:
            self.setStyleSheet(f"""
                QWidget {{
                    background: {BG_ELEVATED};
                    border-top: 2px solid {ACCENT_CYAN};
                    border-right: 1px solid {BORDER_DEFAULT};
                    border-left: 1px solid {BORDER_DEFAULT};
                }}
            """)
            self._title.setStyleSheet(f"color: {TEXT_PRIMARY}; font-size: 10px; font-weight: 600; background: transparent;")
        else:
            self.setStyleSheet(f"""
                QWidget {{
                    background: transparent;
                    border-top: 2px solid transparent;
                    border-right: 1px solid {BORDER_DEFAULT};
                }}
                QWidget:hover {{ background: {BG_ELEVATED}44; }}
            """)
            self._title.setStyleSheet(f"color: {TEXT_MUTED}; font-size: 10px; background: transparent;")

    def mousePressEvent(self, _e):
        self.activated.emit(self._idx)


# ─────────────────────────────────────────────────────────────────────────────
# Screenshot preview widget
# ─────────────────────────────────────────────────────────────────────────────

class _ScreenshotView(QLabel):
    def __init__(self, parent=None):
        super().__init__(parent)
        self.setMinimumHeight(200)
        self.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.setStyleSheet(f"""
            QLabel {{
                background: {BG_CARD};
                border: 1px solid {BORDER_DEFAULT};
                border-radius: 6px;
                color: {TEXT_MUTED};
                font-size: 11px;
            }}
        """)
        self.setText("📸  Screenshot will appear here after navigation")
        self._pixmap: Optional[QPixmap] = None

    def set_screenshot(self, b64: str):
        if not b64:
            return
        try:
            img_bytes = base64.b64decode(b64)
            img = QImage.fromData(img_bytes)
            if not img.isNull():
                self._pixmap = QPixmap.fromImage(img)
                self._update_scaled()
                self.setText("")
        except Exception as exc:
            log.warning("Screenshot decode error: %s", exc)

    def _update_scaled(self):
        if self._pixmap:
            scaled = self._pixmap.scaled(
                self.width() - 4, 360,
                Qt.AspectRatioMode.KeepAspectRatio,
                Qt.TransformationMode.SmoothTransformation,
            )
            self.setPixmap(scaled)

    def resizeEvent(self, e):
        self._update_scaled()
        super().resizeEvent(e)


# ─────────────────────────────────────────────────────────────────────────────
# Main Browser Workspace
# ─────────────────────────────────────────────────────────────────────────────

class BrowserWorkspace(QWidget):
    """
    Full browser workspace with Playwright backend.

    Signals
    -------
    navigate_requested(url)     relay to ServerAdapter if desired
    search_requested(query)     relay to ResearchAgent
    """

    navigate_requested = Signal(str)
    search_requested   = Signal(str)

    def __init__(self, parent=None):
        super().__init__(parent)
        self._tabs: List[dict] = []          # {title, url}
        self._active_tab = 0
        self._history: List[str] = []
        self._worker: Optional[_BrowserWorker] = None
        self._thread: Optional[_BrowserThread] = None
        self._build()
        self._start_worker()

    # ── Build UI ──────────────────────────────────────────────────────────

    def _build(self):
        root = QVBoxLayout(self)
        root.setContentsMargins(0, 0, 0, 0)
        root.setSpacing(0)
        self.setStyleSheet(f"background: {BG_WINDOW};")

        # ── Toolbar ───────────────────────────────────────────────────
        toolbar = self._build_toolbar()
        root.addWidget(toolbar)

        # ── Tabs row ──────────────────────────────────────────────────
        self._tabs_bar_widget = self._build_tab_bar()
        root.addWidget(self._tabs_bar_widget)

        # ── Divider ───────────────────────────────────────────────────
        div = QFrame()
        div.setFixedHeight(1)
        div.setStyleSheet(f"background: {BORDER_DEFAULT};")
        root.addWidget(div)

        # ── Body: sidebar + content ───────────────────────────────────
        body = QSplitter(Qt.Orientation.Horizontal)
        body.setStyleSheet("QSplitter { background: transparent; } QSplitter::handle { background: transparent; }")

        # Left sidebar
        sidebar = self._build_sidebar()
        sidebar.setMinimumWidth(160)
        sidebar.setMaximumWidth(220)
        body.addWidget(sidebar)

        # Main content
        content = self._build_content()
        body.addWidget(content)
        body.setStretchFactor(0, 0)
        body.setStretchFactor(1, 1)
        body.setSizes([180, 800])

        root.addWidget(body, 1)

        # ── Status bar ────────────────────────────────────────────────
        status_bar = self._build_status_bar()
        root.addWidget(status_bar)

    def _build_toolbar(self) -> QFrame:
        bar = QFrame()
        bar.setFixedHeight(48)
        bar.setStyleSheet(f"background: {BG_ELEVATED}; border-bottom: 1px solid {BORDER_DEFAULT};")
        lay = QHBoxLayout(bar)
        lay.setContentsMargins(12, 0, 12, 0)
        lay.setSpacing(8)

        def _icon_btn(label: str, tip: str) -> QPushButton:
            b = QPushButton(label)
            b.setFixedSize(32, 32)
            b.setToolTip(tip)
            b.setCursor(Qt.CursorShape.PointingHandCursor)
            b.setStyleSheet(f"""
                QPushButton {{
                    background: {BG_CARD}; color: {TEXT_PRIMARY};
                    border: 1px solid {BORDER_DEFAULT}; border-radius: 6px;
                    font-size: 14px;
                }}
                QPushButton:hover {{ background: {BG_HIGHLIGHT}; border-color: {BORDER_ACCENT}; }}
            """)
            return b

        self._btn_back    = _icon_btn("←", "Back")
        self._btn_forward = _icon_btn("→", "Forward")
        self._btn_reload  = _icon_btn("⟳", "Reload")
        lay.addWidget(self._btn_back)
        lay.addWidget(self._btn_forward)
        lay.addWidget(self._btn_reload)

        # Separator
        sep = QFrame()
        sep.setFixedSize(1, 24)
        sep.setStyleSheet(f"background: {BORDER_DEFAULT};")
        lay.addWidget(sep)

        # URL / search bar
        self._url_bar = QLineEdit()
        self._url_bar.setPlaceholderText("Enter URL or search query…")
        self._url_bar.setStyleSheet(f"""
            QLineEdit {{
                background: {BG_INPUT}; color: {TEXT_PRIMARY};
                border: 1px solid {BORDER_DEFAULT}; border-radius: 6px;
                padding: 6px 14px; font-size: 11px;
            }}
            QLineEdit:focus {{ border: 1px solid {BORDER_ACTIVE}; }}
        """)
        self._url_bar.returnPressed.connect(self._on_url_submitted)
        lay.addWidget(self._url_bar, 1)

        # Search button
        search_btn = QPushButton("🔍  SEARCH")
        search_btn.setFixedHeight(32)
        search_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        search_btn.setStyleSheet(f"""
            QPushButton {{
                background: {ACCENT_CYAN}22; color: {ACCENT_CYAN};
                border: 1px solid {ACCENT_CYAN}55; border-radius: 6px;
                padding: 0 14px; font-size: 10px; font-weight: 700;
            }}
            QPushButton:hover {{ background: {ACCENT_CYAN}44; border-color: {ACCENT_CYAN}; }}
        """)
        search_btn.clicked.connect(self._on_search_clicked)
        lay.addWidget(search_btn)

        # Screenshot button
        ss_btn = QPushButton("📸")
        ss_btn.setFixedSize(32, 32)
        ss_btn.setToolTip("Take screenshot")
        ss_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        ss_btn.setStyleSheet(f"""
            QPushButton {{
                background: {BG_CARD}; color: {TEXT_PRIMARY};
                border: 1px solid {BORDER_DEFAULT}; border-radius: 6px; font-size: 14px;
            }}
            QPushButton:hover {{ background: {BG_HIGHLIGHT}; }}
        """)
        ss_btn.clicked.connect(self._on_screenshot)
        lay.addWidget(ss_btn)

        # Extract text
        ext_btn = QPushButton("📄")
        ext_btn.setFixedSize(32, 32)
        ext_btn.setToolTip("Extract page text")
        ext_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        ext_btn.setStyleSheet(f"""
            QPushButton {{
                background: {BG_CARD}; color: {TEXT_PRIMARY};
                border: 1px solid {BORDER_DEFAULT}; border-radius: 6px; font-size: 14px;
            }}
            QPushButton:hover {{ background: {BG_HIGHLIGHT}; }}
        """)
        ext_btn.clicked.connect(self._on_extract)
        lay.addWidget(ext_btn)

        self._btn_back.clicked.connect(lambda: self._log_action("← Back"))
        self._btn_forward.clicked.connect(lambda: self._log_action("→ Forward"))
        self._btn_reload.clicked.connect(self._on_reload)

        return bar

    def _build_tab_bar(self) -> QWidget:
        w = QWidget()
        w.setFixedHeight(34)
        w.setStyleSheet(f"background: {BG_SURFACE}; border-bottom: 1px solid {BORDER_DEFAULT};")
        self._tab_lay = QHBoxLayout(w)
        self._tab_lay.setContentsMargins(8, 2, 8, 0)
        self._tab_lay.setSpacing(0)

        self._tab_widgets: List[_TabItem] = []

        # Add first default tab
        self._add_tab("New Tab", "about:blank")

        # New tab button
        new_tab_btn = QPushButton("＋")
        new_tab_btn.setFixedSize(30, 28)
        new_tab_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        new_tab_btn.setStyleSheet(f"""
            QPushButton {{
                background: transparent; color: {TEXT_MUTED};
                border: none; font-size: 16px;
            }}
            QPushButton:hover {{ color: {ACCENT_CYAN}; }}
        """)
        new_tab_btn.clicked.connect(lambda: self._add_tab("New Tab", "about:blank"))
        self._tab_lay.addWidget(new_tab_btn)
        self._tab_lay.addStretch()

        return w

    def _add_tab(self, title: str, url: str):
        idx = len(self._tab_widgets)
        tab = _TabItem(title, idx)
        tab.activated.connect(self._on_tab_activated)
        tab.close_clicked.connect(self._on_tab_closed)
        # Insert before stretch/new-tab button
        insert_pos = max(0, self._tab_lay.count() - 2)
        self._tab_lay.insertWidget(insert_pos, tab)
        self._tab_widgets.append(tab)
        self._tabs.append({"title": title, "url": url})
        self._set_active_tab(idx)

    def _build_sidebar(self) -> QFrame:
        sidebar = QFrame()
        sidebar.setStyleSheet(f"background: {BG_SURFACE}; border-right: 1px solid {BORDER_DEFAULT};")
        lay = QVBoxLayout(sidebar)
        lay.setContentsMargins(0, 0, 0, 0)
        lay.setSpacing(0)

        # Quick links header
        hdr = QLabel("  🔖  QUICK LINKS")
        hdr.setFixedHeight(36)
        hdr.setStyleSheet(f"color: {ACCENT_CYAN}; font-size: 9px; font-weight: 700; letter-spacing: 2px; background: {BG_ELEVATED}; border-bottom: 1px solid {BORDER_DEFAULT};")
        lay.addWidget(hdr)

        for icon, name, url in QUICK_LINKS:
            btn = QPushButton(f"  {icon}  {name}")
            btn.setFixedHeight(34)
            btn.setCursor(Qt.CursorShape.PointingHandCursor)
            btn.setStyleSheet(f"""
                QPushButton {{
                    background: transparent; color: {TEXT_SECONDARY};
                    border: none; border-bottom: 1px solid {BORDER_DEFAULT};
                    text-align: left; font-size: 10px; padding-left: 4px;
                }}
                QPushButton:hover {{
                    background: {ACCENT_CYAN}11; color: {ACCENT_CYAN};
                }}
            """)
            _url = url  # capture
            btn.clicked.connect(lambda _, u=_url: self._navigate_to(u))
            lay.addWidget(btn)

        # History section
        lay.addSpacing(8)
        hist_hdr = QLabel("  📜  HISTORY")
        hist_hdr.setFixedHeight(30)
        hist_hdr.setStyleSheet(f"color: {TEXT_MUTED}; font-size: 8px; font-weight: 700; letter-spacing: 2px; background: {BG_ELEVATED}; border-bottom: 1px solid {BORDER_DEFAULT};")
        lay.addWidget(hist_hdr)

        hist_scroll = QScrollArea()
        hist_scroll.setWidgetResizable(True)
        hist_scroll.setStyleSheet(f"QScrollArea {{ background: transparent; border: none; }}")
        hist_w = QWidget()
        hist_w.setStyleSheet(f"background: transparent;")
        self._hist_lay = QVBoxLayout(hist_w)
        self._hist_lay.setContentsMargins(0, 0, 0, 0)
        self._hist_lay.setSpacing(0)
        self._hist_lay.addStretch()
        hist_scroll.setWidget(hist_w)
        lay.addWidget(hist_scroll, 1)

        return sidebar

    def _build_content(self) -> QWidget:
        w = QWidget()
        w.setStyleSheet(f"background: {BG_WINDOW};")
        lay = QVBoxLayout(w)
        lay.setContentsMargins(16, 12, 16, 12)
        lay.setSpacing(12)

        # Current page info header
        self._page_header = QFrame()
        self._page_header.setFixedHeight(54)
        self._page_header.setStyleSheet(f"background: {BG_ELEVATED}; border: 1px solid {BORDER_DEFAULT}; border-radius: 6px;")
        ph_lay = QHBoxLayout(self._page_header)
        ph_lay.setContentsMargins(14, 0, 14, 0)
        ph_lay.setSpacing(12)

        self._page_icon = QLabel("🌐")
        self._page_icon.setStyleSheet("font-size: 18px; background: transparent;")
        ph_lay.addWidget(self._page_icon)

        info_col = QVBoxLayout()
        info_col.setSpacing(2)
        self._page_title = QLabel("No page loaded")
        self._page_title.setStyleSheet(f"color: {TEXT_PRIMARY}; font-size: 11px; font-weight: 600; background: transparent;")
        info_col.addWidget(self._page_title)
        self._page_url = QLabel("Navigate using the toolbar above or click a quick link")
        self._page_url.setStyleSheet(f"color: {TEXT_MUTED}; font-size: 9px; background: transparent;")
        info_col.addWidget(self._page_url)
        ph_lay.addLayout(info_col, 1)

        # Status pill
        self._status_pill = QLabel("● READY")
        self._status_pill.setFixedHeight(20)
        self._status_pill.setStyleSheet(f"""
            QLabel {{
                color: {ACCENT_GREEN}; background: {ACCENT_GREEN}22;
                border: 1px solid {ACCENT_GREEN}55; border-radius: 10px;
                padding: 0 10px; font-size: 9px; font-weight: 700;
            }}
        """)
        ph_lay.addWidget(self._status_pill)
        lay.addWidget(self._page_header)

        # Progress bar
        self._progress = QProgressBar()
        self._progress.setFixedHeight(3)
        self._progress.setRange(0, 0)
        self._progress.setVisible(False)
        self._progress.setStyleSheet(f"""
            QProgressBar {{ background: {BG_SURFACE}; border: none; border-radius: 1px; }}
            QProgressBar::chunk {{ background: {ACCENT_CYAN}; }}
        """)
        lay.addWidget(self._progress)

        # Screenshot area
        ss_hdr = QLabel("SCREENSHOT")
        ss_hdr.setStyleSheet(f"color: {TEXT_MUTED}; font-size: 8px; letter-spacing: 2px; background: transparent;")
        lay.addWidget(ss_hdr)
        self._screenshot_view = _ScreenshotView()
        lay.addWidget(self._screenshot_view)

        # Extracted text / output
        text_hdr_row = QHBoxLayout()
        text_hdr = QLabel("PAGE TEXT / OUTPUT")
        text_hdr.setStyleSheet(f"color: {TEXT_MUTED}; font-size: 8px; letter-spacing: 2px; background: transparent;")
        text_hdr_row.addWidget(text_hdr)
        text_hdr_row.addStretch()
        clr_btn = QPushButton("CLEAR")
        clr_btn.setFixedHeight(18)
        clr_btn.setStyleSheet(f"""
            QPushButton {{
                background: transparent; color: {TEXT_MUTED};
                border: 1px solid {BORDER_DEFAULT}; border-radius: 3px;
                font-size: 8px; padding: 0 8px;
            }}
            QPushButton:hover {{ color: {ACCENT_CYAN}; border-color: {ACCENT_CYAN}; }}
        """)
        clr_btn.clicked.connect(lambda: self._output_area.clear())
        text_hdr_row.addWidget(clr_btn)
        lay.addLayout(text_hdr_row)

        self._output_area = QTextEdit()
        self._output_area.setReadOnly(True)
        self._output_area.setMinimumHeight(120)
        self._output_area.setStyleSheet(f"""
            QTextEdit {{
                background: {BG_CARD}; color: {TEXT_PRIMARY};
                border: 1px solid {BORDER_DEFAULT}; border-radius: 6px;
                padding: 10px; font-size: 10px; font-family: monospace;
            }}
        """)
        self._output_area.setPlaceholderText("Page content and extracted text will appear here…")
        lay.addWidget(self._output_area, 1)

        return w

    def _build_status_bar(self) -> QFrame:
        bar = QFrame()
        bar.setFixedHeight(26)
        bar.setStyleSheet(f"background: {BG_SURFACE}; border-top: 1px solid {BORDER_DEFAULT};")
        lay = QHBoxLayout(bar)
        lay.setContentsMargins(12, 0, 12, 0)
        lay.setSpacing(12)

        self._status_lbl = QLabel("🌐  Browser workspace ready")
        self._status_lbl.setStyleSheet(f"color: {TEXT_MUTED}; font-size: 9px; background: transparent;")
        lay.addWidget(self._status_lbl)
        lay.addStretch()

        self._engine_badge = QLabel("⚪  Playwright: initialising…")
        self._engine_badge.setStyleSheet(f"color: {TEXT_MUTED}; font-size: 9px; background: transparent;")
        lay.addWidget(self._engine_badge)

        return bar

    # ── Worker setup ──────────────────────────────────────────────────────

    def _start_worker(self):
        self._worker = _BrowserWorker()
        self._thread = _BrowserThread(self._worker)
        # Connect worker signals
        self._worker.page_loaded.connect(self._on_page_loaded)
        self._worker.text_extracted.connect(self._on_text_extracted)
        self._worker.search_done.connect(self._on_search_done)
        self._worker.error_occurred.connect(self._on_error)
        self._worker.status_changed.connect(self._on_worker_status)
        self._worker.screenshot_done.connect(self._on_screenshot_done)
        self._thread.start()

    # ── Actions ───────────────────────────────────────────────────────────

    def _on_url_submitted(self):
        text = self._url_bar.text().strip()
        if not text:
            return
        # Decide: URL or search query
        if text.startswith(("http://", "https://", "www.")):
            url = text if text.startswith("http") else f"https://{text}"
            self._navigate_to(url)
        else:
            self._do_search(text)

    def _navigate_to(self, url: str):
        self._url_bar.setText(url)
        self._set_loading(True)
        self._add_history(url)
        if self._worker:
            self._worker.navigate(url)
        self.navigate_requested.emit(url)

    def _do_search(self, query: str):
        self._set_loading(True)
        self._log_action(f"🔍 Searching: {query}")
        if self._worker:
            self._worker.search(query)
        self.search_requested.emit(query)

    def _on_search_clicked(self):
        text = self._url_bar.text().strip()
        if text:
            self._do_search(text)

    def _on_reload(self):
        url = self._url_bar.text().strip()
        if url and url != "about:blank":
            self._navigate_to(url)

    def _on_screenshot(self):
        self._log_action("📸 Taking screenshot…")
        if self._worker:
            self._worker.take_screenshot()

    def _on_extract(self):
        url = self._url_bar.text().strip()
        if url and url != "about:blank":
            self._log_action(f"📄 Extracting text from {url}…")
            if self._worker:
                self._worker.extract_text(url)

    # ── Worker slots ──────────────────────────────────────────────────────

    @Slot(str, str, str)
    def _on_page_loaded(self, url: str, title: str, ss_b64: str):
        self._set_loading(False)
        self._page_title.setText(title or url)
        self._page_url.setText(url)
        self._url_bar.setText(url)
        self._set_status_pill("✅ LOADED", ACCENT_GREEN)
        if ss_b64:
            self._screenshot_view.set_screenshot(ss_b64)
        # Update active tab
        if self._tab_widgets and self._active_tab < len(self._tab_widgets):
            self._tab_widgets[self._active_tab].set_title(title or url)
        if self._active_tab < len(self._tabs):
            self._tabs[self._active_tab]["url"]   = url
            self._tabs[self._active_tab]["title"] = title
        self._log_action(f"✅ Loaded: {title} — {url}")

    @Slot(str, str)
    def _on_text_extracted(self, url: str, text: str):
        self._output_area.setPlainText(text)
        self._log_action(f"📄 Text extracted ({len(text)} chars)")

    @Slot(str, str, str)
    def _on_search_done(self, query: str, url: str, ss_b64: str):
        self._set_loading(False)
        self._page_title.setText(f"Search: {query}")
        self._page_url.setText(url)
        self._set_status_pill("🔍 RESULTS", ACCENT_CYAN)
        if ss_b64:
            self._screenshot_view.set_screenshot(ss_b64)
        self._log_action(f"🔍 Search results loaded: {query}")

    @Slot(str)
    def _on_error(self, msg: str):
        self._set_loading(False)
        self._set_status_pill("❌ ERROR", ACCENT_RED)
        self._log_action(f"❌ Error: {msg}")

    @Slot(str)
    def _on_worker_status(self, msg: str):
        self._status_lbl.setText(msg)
        if "ready" in msg.lower():
            self._engine_badge.setText("🟢  Playwright: ready")
            self._engine_badge.setStyleSheet(f"color: {ACCENT_GREEN}; font-size: 9px; background: transparent;")
        elif "not installed" in msg.lower():
            self._engine_badge.setText("🔴  Playwright: not installed")
            self._engine_badge.setStyleSheet(f"color: {ACCENT_RED}; font-size: 9px; background: transparent;")
            self._output_area.setPlainText(
                "⚠️  Playwright is not installed.\n\n"
                "Install it with:\n"
                "  pip install playwright\n"
                "  playwright install chromium\n\n"
                "Then restart JARVIS."
            )

    @Slot(str, str)
    def _on_screenshot_done(self, url: str, ss_b64: str):
        self._screenshot_view.set_screenshot(ss_b64)
        self._log_action("📸 Screenshot captured")

    # ── Tab management ────────────────────────────────────────────────────

    def _on_tab_activated(self, idx: int):
        self._set_active_tab(idx)
        if idx < len(self._tabs):
            url = self._tabs[idx]["url"]
            if url and url != "about:blank":
                self._url_bar.setText(url)
                self._page_title.setText(self._tabs[idx]["title"])
                self._page_url.setText(url)

    def _on_tab_closed(self, idx: int):
        if len(self._tab_widgets) <= 1:
            return  # keep at least one tab
        tab = self._tab_widgets.pop(idx)
        tab.setParent(None)
        tab.deleteLater()
        self._tabs.pop(idx)
        # Re-index remaining tabs
        for i, t in enumerate(self._tab_widgets):
            t._idx = i
        new_active = min(idx, len(self._tab_widgets) - 1)
        self._set_active_tab(new_active)

    def _set_active_tab(self, idx: int):
        self._active_tab = idx
        for i, t in enumerate(self._tab_widgets):
            t.set_active(i == idx)

    # ── History ───────────────────────────────────────────────────────────

    def _add_history(self, url: str):
        self._history.append(url)
        domain = urlparse(url).netloc or url[:30]
        btn = QPushButton(f"  🌐  {domain}")
        btn.setFixedHeight(28)
        btn.setCursor(Qt.CursorShape.PointingHandCursor)
        btn.setStyleSheet(f"""
            QPushButton {{
                background: transparent; color: {TEXT_MUTED};
                border: none; border-bottom: 1px solid {BORDER_DEFAULT};
                text-align: left; font-size: 9px;
            }}
            QPushButton:hover {{ color: {ACCENT_CYAN}; background: {ACCENT_CYAN}0a; }}
        """)
        _url = url
        btn.clicked.connect(lambda _, u=_url: self._navigate_to(u))
        # Insert before stretch
        self._hist_lay.insertWidget(self._hist_lay.count() - 1, btn)
        # Keep max 20 history items
        if self._hist_lay.count() > 22:
            item = self._hist_lay.takeAt(0)
            if item.widget():
                item.widget().deleteLater()

    # ── Helpers ───────────────────────────────────────────────────────────

    def _set_loading(self, loading: bool):
        self._progress.setVisible(loading)
        if loading:
            self._set_status_pill("🔄 LOADING", ACCENT_YELLOW)
        else:
            self._set_status_pill("✅ DONE", ACCENT_GREEN)

    def _set_status_pill(self, text: str, color: str):
        self._status_pill.setText(text)
        self._status_pill.setStyleSheet(f"""
            QLabel {{
                color: {color}; background: {color}22;
                border: 1px solid {color}55; border-radius: 10px;
                padding: 0 10px; font-size: 9px; font-weight: 700;
            }}
        """)

    def _log_action(self, msg: str):
        ts = time.strftime("%H:%M:%S")
        self._status_lbl.setText(f"[{ts}] {msg}")

    # ── Cleanup ───────────────────────────────────────────────────────────

    def shutdown(self) -> None:
        """Stop the Playwright worker thread.

        P13 bug fix: this used to live only in closeEvent(), but
        BrowserWorkspace is a *child* widget living inside JarvisWindow's
        QStackedWidget — Qt only delivers closeEvent to actual top-level
        windows, so this cleanup never ran in practice. The background
        _BrowserThread (an infinite `while not isInterruptionRequested()`
        loop) kept running past app shutdown, which is why the process
        could abort/hang on exit ("QThread: Destroyed while thread is
        still running"). main_window.py's closeEvent now calls this
        explicitly. closeEvent is kept below as a harmless no-op fallback
        for the rare case this widget is ever shown standalone.
        """
        if self._worker:
            self._worker.stop()
        if self._thread:
            self._thread.requestInterruption()
            self._thread.wait(1000)

    def closeEvent(self, e):
        self.shutdown()
        super().closeEvent(e)
        super().closeEvent(e)