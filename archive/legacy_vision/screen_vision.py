"""
JARVIS AI OS — ScreenVision
============================
Captures screenshots of the desktop and provides OCR text extraction.
Integrates with the vision pipeline for screen-aware context.
"""

from __future__ import annotations

import asyncio
import base64
import io
import time
from dataclasses import dataclass, field
from typing import Optional

from observability.logging.logger import get_logger

log = get_logger(__name__)

try:
    import mss
    _MSS_AVAILABLE = True
except ImportError:
    _MSS_AVAILABLE = False
    log.warning("mss not installed — screenshot capture unavailable")

try:
    from PIL import Image
    _PIL_AVAILABLE = True
except ImportError:
    _PIL_AVAILABLE = False
    log.warning("Pillow not installed — image processing unavailable")

try:
    import pytesseract
    _OCR_AVAILABLE = True
except ImportError:
    _OCR_AVAILABLE = False
    log.debug("pytesseract not installed — OCR unavailable")


@dataclass
class ScreenFrame:
    """A single captured screen frame."""
    width: int
    height: int
    timestamp: float = field(default_factory=time.time)
    png_bytes: bytes = b""
    ocr_text: str = ""
    monitor_index: int = 0

    @property
    def base64_png(self) -> str:
        return base64.b64encode(self.png_bytes).decode() if self.png_bytes else ""


class ScreenVision:
    """
    Captures screenshots and extracts text via OCR.

    Usage:
        sv = ScreenVision()
        frame = await sv.capture()
        print(frame.ocr_text)
    """

    def __init__(self, monitor: int = 1, ocr_lang: str = "eng") -> None:
        self._monitor = monitor
        self._ocr_lang = ocr_lang
        self._last_frame: Optional[ScreenFrame] = None

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    async def capture(self, ocr: bool = True) -> ScreenFrame:
        """Capture a screenshot and optionally run OCR."""
        loop = asyncio.get_event_loop()
        frame = await loop.run_in_executor(None, self._capture_sync, ocr)
        self._last_frame = frame
        return frame

    async def capture_region(
        self,
        left: int,
        top: int,
        width: int,
        height: int,
        ocr: bool = True,
    ) -> ScreenFrame:
        """Capture a specific screen region."""
        loop = asyncio.get_event_loop()
        frame = await loop.run_in_executor(
            None, self._capture_region_sync, left, top, width, height, ocr
        )
        self._last_frame = frame
        return frame

    @property
    def last_frame(self) -> Optional[ScreenFrame]:
        return self._last_frame

    # ------------------------------------------------------------------
    # Internal sync helpers (run in executor to avoid blocking)
    # ------------------------------------------------------------------

    def _capture_sync(self, ocr: bool) -> ScreenFrame:
        if not _MSS_AVAILABLE or not _PIL_AVAILABLE:
            log.warning("Screenshot dependencies missing — returning empty frame")
            return ScreenFrame(width=0, height=0)

        with mss.mss() as sct:
            monitors = sct.monitors
            monitor = monitors[self._monitor] if self._monitor < len(monitors) else monitors[1]
            raw = sct.grab(monitor)
            img = Image.frombytes("RGB", raw.size, raw.bgra, "raw", "BGRX")
            buf = io.BytesIO()
            img.save(buf, format="PNG")
            png_bytes = buf.getvalue()

        ocr_text = self._run_ocr(img) if ocr else ""
        return ScreenFrame(
            width=raw.size[0],
            height=raw.size[1],
            png_bytes=png_bytes,
            ocr_text=ocr_text,
            monitor_index=self._monitor,
        )

    def _capture_region_sync(
        self, left: int, top: int, width: int, height: int, ocr: bool
    ) -> ScreenFrame:
        if not _MSS_AVAILABLE or not _PIL_AVAILABLE:
            return ScreenFrame(width=0, height=0)

        region = {"left": left, "top": top, "width": width, "height": height}
        with mss.mss() as sct:
            raw = sct.grab(region)
            img = Image.frombytes("RGB", raw.size, raw.bgra, "raw", "BGRX")
            buf = io.BytesIO()
            img.save(buf, format="PNG")
            png_bytes = buf.getvalue()

        ocr_text = self._run_ocr(img) if ocr else ""
        return ScreenFrame(
            width=width,
            height=height,
            png_bytes=png_bytes,
            ocr_text=ocr_text,
        )

    def _run_ocr(self, img: "Image.Image") -> str:
        if not _OCR_AVAILABLE:
            return ""
        try:
            return pytesseract.image_to_string(img, lang=self._ocr_lang).strip()
        except Exception as exc:
            log.warning("OCR failed", error=str(exc))
            return ""
