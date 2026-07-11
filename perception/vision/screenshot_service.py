"""
JARVIS AI OS — Screenshot Capture Service
==========================================
Low-level, platform-aware screenshot capture.

Responsibilities:
  - Full-screen capture
  - Window capture (by title or PID)
  - Region capture (x, y, width, height)
  - Event emission: vision.capture.completed / vision.capture.failed

Rules:
  - No AI analysis here — raw pixels + base64 only
  - Emits events; never returns data directly to callers
  - Register with ServiceRegistry on startup

Used by:
  OCRPipeline, VisionPipeline, ContextClassifier
"""

from __future__ import annotations

import asyncio
import base64
import io
import platform
import time
import uuid
from dataclasses import dataclass

from observability.logging.logger import get_logger
from perception.vision.vision_events import VisionEvent, CapturePayload

log = get_logger(__name__)

_OS = platform.system()


# ---------------------------------------------------------------------------
# Capture request model
# ---------------------------------------------------------------------------


@dataclass
class CaptureRequest:
    mode: str  # "fullscreen" | "window" | "region"
    region: dict | None = None  # {"x","y","w","h"} for mode=region
    window_title: str | None = None  # for mode=window
    window_pid: int | None = None
    format: str = "PNG"  # PNG | JPEG
    quality: int = 90  # JPEG quality
    requester: str = "unknown"
    request_id: str = ""

    def __post_init__(self) -> None:
        if not self.request_id:
            self.request_id = str(uuid.uuid4())


# ---------------------------------------------------------------------------
# ScreenshotService
# ---------------------------------------------------------------------------


class ScreenshotService:
    """
    Production-grade screenshot capture service.

    Usage:
        svc = ScreenshotService(event_bus=bus, service_registry=registry)
        await svc.start()
        await svc.capture(CaptureRequest(mode="fullscreen", requester="vision_pipeline"))
    """

    SERVICE_NAME = "perception.screenshot_service"

    def __init__(
        self,
        event_bus=None,
        service_registry=None,
    ) -> None:
        self._bus = event_bus
        self._registry = service_registry
        self._running = False
        self._stats = {"captured": 0, "failed": 0}

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------

    async def start(self) -> None:
        if self._running:
            return
        self._running = True
        if self._registry:
            await self._registry.set_running(self.SERVICE_NAME)
        log.info("ScreenshotService started", os=_OS)

    async def stop(self) -> None:
        if not self._running:
            return
        self._running = False
        if self._registry:
            await self._registry.set_stopped(self.SERVICE_NAME)
        log.info("ScreenshotService stopped", stats=self._stats)

    async def health(self) -> dict:
        return {"running": self._running, "stats": self._stats, "os": _OS}

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    async def capture(self, request: CaptureRequest) -> CapturePayload | None:
        """
        Capture a screenshot and publish a vision.capture.completed event.
        Returns the CapturePayload on success; None on failure.
        """
        t0 = time.time()
        try:
            image_bytes, width, height = await self._do_capture(request)
            image_b64 = base64.b64encode(image_bytes).decode("utf-8")

            payload = CapturePayload(
                capture_id=request.request_id,
                image_b64=image_b64,
                width=width,
                height=height,
                region=request.region,
                window_title=request.window_title,
                timestamp=t0,
                source=request.requester,
            )
            self._stats["captured"] += 1
            await self._emit(
                VisionEvent.CAPTURE_COMPLETED, payload.as_dict(), request.requester
            )
            log.debug(
                "Screenshot captured",
                mode=request.mode,
                size=f"{width}x{height}",
                kb=len(image_bytes) // 1024,
            )
            return payload

        except Exception as exc:
            self._stats["failed"] += 1
            log.error("Screenshot capture failed", mode=request.mode, error=str(exc))
            await self._emit(
                VisionEvent.CAPTURE_FAILED,
                {
                    "request_id": request.request_id,
                    "error": str(exc),
                    "mode": request.mode,
                },
                request.requester,
            )
            return None

    async def capture_fullscreen(
        self, requester: str = "system"
    ) -> CapturePayload | None:
        return await self.capture(
            CaptureRequest(mode="fullscreen", requester=requester)
        )

    async def capture_region(
        self, x: int, y: int, width: int, height: int, requester: str = "system"
    ) -> CapturePayload | None:
        return await self.capture(
            CaptureRequest(
                mode="region",
                region={"x": x, "y": y, "w": width, "h": height},
                requester=requester,
            )
        )

    async def capture_window(
        self,
        title: str | None = None,
        pid: int | None = None,
        requester: str = "system",
    ) -> CapturePayload | None:
        return await self.capture(
            CaptureRequest(
                mode="window",
                window_title=title,
                window_pid=pid,
                requester=requester,
            )
        )

    # ------------------------------------------------------------------
    # Internal capture dispatch
    # ------------------------------------------------------------------

    async def _do_capture(self, request: CaptureRequest) -> tuple[bytes, int, int]:
        """Dispatch to platform capture; run in executor to avoid blocking."""
        loop = asyncio.get_running_loop()
        return await loop.run_in_executor(None, self._capture_sync, request)

    def _capture_sync(self, request: CaptureRequest) -> tuple[bytes, int, int]:
        """Synchronous capture — called in thread executor."""
        try:
            import PIL.ImageGrab as ImageGrab  # noqa: F401
            import PIL.Image as Image  # noqa: F401
        except ImportError:
            # Graceful degradation: return a 1×1 transparent pixel
            log.warning("Pillow not available — returning placeholder image")
            return self._placeholder_image()

        try:
            if request.mode == "region" and request.region:
                r = request.region
                img = ImageGrab.grab(
                    bbox=(r["x"], r["y"], r["x"] + r["w"], r["y"] + r["h"])
                )
            elif request.mode == "window" and (
                request.window_title or request.window_pid
            ):
                img = self._capture_window_platform(request)
            else:
                img = ImageGrab.grab()

            buf = io.BytesIO()
            fmt = request.format.upper()
            if fmt == "JPEG":
                img.save(buf, format="JPEG", quality=request.quality)
            else:
                img.save(buf, format="PNG")
            return buf.getvalue(), img.width, img.height

        except Exception as exc:
            log.warning("Platform capture failed, using placeholder", error=str(exc))
            return self._placeholder_image()

    def _capture_window_platform(self, request: CaptureRequest):
        """Platform-specific window capture (best-effort)."""
        try:
            import PIL.ImageGrab as ImageGrab  # noqa: F401

            if _OS == "Windows":
                import pygetwindow as gw

                wins = gw.getWindowsWithTitle(request.window_title or "")
                if wins:
                    w = wins[0]
                    return ImageGrab.grab(bbox=(w.left, w.top, w.right, w.bottom))
            elif _OS == "Darwin":
                import subprocess
                import PIL.Image as Image  # noqa: F401

                subprocess.run(
                    ["screencapture", "-x", "-t", "png", "/tmp/jarvis_win_cap.png"],
                    capture_output=True,
                )
                return Image.open("/tmp/jarvis_win_cap.png")
        except Exception as exc:
            log.debug("Window capture fallback to fullscreen", error=str(exc))
        import PIL.ImageGrab as ImageGrab  # noqa: F401

        return ImageGrab.grab()

    @staticmethod
    def _placeholder_image() -> tuple[bytes, int, int]:
        """Return a 640×480 grey placeholder when real capture is unavailable."""
        try:
            import PIL.Image as Image  # noqa: F401

            img = Image.new("RGB", (640, 480), color=(128, 128, 128))
            buf = io.BytesIO()
            img.save(buf, format="PNG")
            return buf.getvalue(), 640, 480
        except ImportError:
            # Absolute fallback: minimal valid 1×1 PNG
            import struct
            import zlib

            def png_chunk(name, data):
                c = struct.pack(">I", len(data)) + name + data
                return c + struct.pack(">I", zlib.crc32(name + data) & 0xFFFFFFFF)

            header = b"\x89PNG\r\n\x1a\n"
            ihdr = png_chunk(b"IHDR", struct.pack(">IIBBBBB", 1, 1, 8, 2, 0, 0, 0))
            idat = png_chunk(b"IDAT", zlib.compress(b"\x00\x80\x80\x80"))
            iend = png_chunk(b"IEND", b"")
            return header + ihdr + idat + iend, 1, 1

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------

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
