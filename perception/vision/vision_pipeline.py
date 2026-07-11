"""
JARVIS AI OS — Vision Pipeline
================================
Captures the screen and runs AI-powered visual analysis.

Responsibilities:
  - Periodic / on-demand screenshot capture
  - Base64 encoding for model ingestion
  - Bounding-box & element detection metadata
  - Publishes perception.vision.frame events on the Event Bus

Rules:
  - No direct action execution — emits events only
  - Agents request captures via the Event Bus
  - All results flow back through Event Bus
"""

from __future__ import annotations

import asyncio
import base64
import io
import time
from dataclasses import dataclass, field
from typing import Any

from observability.logging.logger import get_logger

log = get_logger(__name__)


# ---------------------------------------------------------------------------
# Data models
# ---------------------------------------------------------------------------


@dataclass
class BoundingBox:
    x: int
    y: int
    width: int
    height: int

    @property
    def center(self) -> tuple[int, int]:
        return (self.x + self.width // 2, self.y + self.height // 2)

    def as_dict(self) -> dict:
        return {"x": self.x, "y": self.y, "width": self.width, "height": self.height}


@dataclass
class DetectedElement:
    element_type: str  # button, input, text, image, link …
    label: str
    bbox: BoundingBox
    confidence: float = 1.0
    attributes: dict = field(default_factory=dict)

    def as_dict(self) -> dict:
        return {
            "type": self.element_type,
            "label": self.label,
            "bbox": self.bbox.as_dict(),
            "confidence": self.confidence,
            "attributes": self.attributes,
        }


@dataclass
class VisionFrame:
    """A single captured & analysed screen frame."""

    frame_id: str
    timestamp: float
    width: int
    height: int
    image_b64: str  # PNG base64
    elements: list[DetectedElement] = field(default_factory=list)
    description: str = ""
    active_window: str = ""
    metadata: dict = field(default_factory=dict)

    def as_dict(self) -> dict:
        return {
            "frame_id": self.frame_id,
            "timestamp": self.timestamp,
            "width": self.width,
            "height": self.height,
            "image_b64": self.image_b64,
            "elements": [e.as_dict() for e in self.elements],
            "description": self.description,
            "active_window": self.active_window,
            "metadata": self.metadata,
        }


# ---------------------------------------------------------------------------
# Vision Pipeline
# ---------------------------------------------------------------------------


class VisionPipeline:
    """
    Captures screen frames and publishes vision events.

    Usage:
        pipeline = VisionPipeline(event_bus=bus, model_router=router)
        await pipeline.start()
        frame = await pipeline.capture_frame()
        await pipeline.stop()
    """

    # Event types this pipeline emits
    EVT_FRAME_CAPTURED = "perception.vision.frame_captured"
    EVT_ELEMENT_DETECTED = "perception.vision.element_detected"
    EVT_VISION_ERROR = "perception.vision.error"

    def __init__(
        self,
        event_bus: Any,
        model_router: Any | None = None,
        capture_interval: float = 2.0,  # seconds between auto-captures
        auto_capture: bool = False,
    ) -> None:
        self._bus = event_bus
        self._model_router = model_router
        self._interval = capture_interval
        self._auto_capture = auto_capture
        self._running = False
        self._task: asyncio.Task | None = None
        self._last_frame: VisionFrame | None = None
        self._frame_counter = 0

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------

    async def start(self) -> None:
        self._running = True
        if self._auto_capture:
            self._task = asyncio.create_task(self._capture_loop())
        log.info(
            "VisionPipeline started (auto=%s, interval=%.1fs)",
            self._auto_capture,
            self._interval,
        )

    async def stop(self) -> None:
        self._running = False
        if self._task:
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass
        log.info("VisionPipeline stopped")

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    async def capture_frame(
        self,
        region: BoundingBox | None = None,
        analyze: bool = True,
    ) -> VisionFrame:
        """Capture a frame, optionally analyse it, publish event, return frame."""
        try:
            image_b64, width, height = await self._take_screenshot(region)
            self._frame_counter += 1
            frame_id = f"frame_{self._frame_counter}_{int(time.time())}"

            frame = VisionFrame(
                frame_id=frame_id,
                timestamp=time.time(),
                width=width,
                height=height,
                image_b64=image_b64,
            )

            if analyze and self._model_router:
                frame = await self._analyse_frame(frame)

            self._last_frame = frame
            await self._emit(self.EVT_FRAME_CAPTURED, frame.as_dict())
            return frame

        except Exception as exc:
            log.exception("Vision capture failed: %s", exc)
            await self._emit(self.EVT_VISION_ERROR, {"error": str(exc)})
            raise

    async def get_last_frame(self) -> VisionFrame | None:
        return self._last_frame

    async def detect_elements(
        self, frame: VisionFrame, element_types: list[str] | None = None
    ) -> list[DetectedElement]:
        """Run element detection on an existing frame."""
        if not self._model_router:
            log.warning("No model_router — element detection skipped")
            return []

        elements = await self._run_element_detection(frame, element_types)
        frame.elements = elements

        for el in elements:
            await self._emit(
                self.EVT_ELEMENT_DETECTED,
                {
                    "frame_id": frame.frame_id,
                    "element": el.as_dict(),
                },
            )

        return elements

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    async def _capture_loop(self) -> None:
        while self._running:
            try:
                await self.capture_frame()
            except Exception as exc:
                log.error("Auto-capture error: %s", exc)
            await asyncio.sleep(self._interval)

    async def _take_screenshot(
        self, region: BoundingBox | None
    ) -> tuple[str, int, int]:
        """
        Take a screenshot.  Returns (base64_png, width, height).

        Tries mss → Pillow → blank fallback.
        """
        try:
            import mss
            import mss.tools

            with mss.mss() as sct:
                if region:
                    monitor = {
                        "top": region.y,
                        "left": region.x,
                        "width": region.width,
                        "height": region.height,
                    }
                else:
                    monitor = sct.monitors[1]  # primary screen

                sct_img = sct.grab(monitor)
                png_bytes = mss.tools.to_png(sct_img.rgb, sct_img.size)
                b64 = base64.b64encode(png_bytes).decode()
                return b64, sct_img.width, sct_img.height

        except ImportError:
            pass  # fall through to Pillow

        try:
            from PIL import ImageGrab

            img = ImageGrab.grab()
            buf = io.BytesIO()
            img.save(buf, format="PNG")
            b64 = base64.b64encode(buf.getvalue()).decode()
            return b64, img.width, img.height
        except Exception:
            pass

        # Headless fallback — return a 1×1 transparent PNG
        BLANK_PNG = (
            "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAAC0lEQVQI12NgAAIABQ"
            "AABjkB6QAAAABJRU5ErkJggg=="
        )
        log.warning("Screenshot not available — returning blank frame")
        return BLANK_PNG, 1, 1

    async def _analyse_frame(self, frame: VisionFrame) -> VisionFrame:
        """Ask the model router to describe the screen."""
        try:
            prompt = (
                "You are analysing a screenshot. "
                "In one sentence describe what is on the screen. "
                "Then list any visible interactive UI elements as JSON array with "
                "fields: type, label, approximate_position."
            )
            result = await self._model_router.complete(
                messages=[
                    {
                        "role": "user",
                        "content": [
                            {"type": "text", "text": prompt},
                            {
                                "type": "image",
                                "source": {
                                    "type": "base64",
                                    "media_type": "image/png",
                                    "data": frame.image_b64,
                                },
                            },
                        ],
                    }
                ]
            )
            frame.description = result.get("text", "")
        except Exception as exc:
            log.warning("Frame analysis failed: %s", exc)
        return frame

    async def _run_element_detection(
        self, frame: VisionFrame, element_types: list[str] | None
    ) -> list[DetectedElement]:
        """Detect UI elements via model router."""
        type_filter = ", ".join(element_types) if element_types else "all"
        try:
            prompt = (
                f"Detect {type_filter} UI elements in this screenshot. "
                "Return JSON array: [{type, label, x, y, width, height, confidence}]"
            )
            result = await self._model_router.complete(
                messages=[
                    {
                        "role": "user",
                        "content": [
                            {"type": "text", "text": prompt},
                            {
                                "type": "image",
                                "source": {
                                    "type": "base64",
                                    "media_type": "image/png",
                                    "data": frame.image_b64,
                                },
                            },
                        ],
                    }
                ]
            )
            import json

            raw = result.get("text", "[]")
            items = json.loads(raw)
            return [
                DetectedElement(
                    element_type=it.get("type", "unknown"),
                    label=it.get("label", ""),
                    bbox=BoundingBox(
                        x=it.get("x", 0),
                        y=it.get("y", 0),
                        width=it.get("width", 0),
                        height=it.get("height", 0),
                    ),
                    confidence=float(it.get("confidence", 1.0)),
                )
                for it in items
            ]
        except Exception as exc:
            log.warning("Element detection failed: %s", exc)
            return []

    async def _emit(self, event_type: str, payload: dict) -> None:
        if self._bus:
            try:
                await self._bus.publish(
                    Event(event_type=event_type, source="vision_pipeline", payload=payload)
                )
            except Exception as exc:
                log.warning("Event publish failed: %s", exc)