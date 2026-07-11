"""
JARVIS AI OS — OCR Pipeline
=============================
Extracts text from images, screen regions, and files.

Responsibilities:
  - Run OCR on PIL images, base64 strings, or file paths
  - Return structured text blocks with bounding boxes
  - Publish perception.ocr.text_extracted events
  - Support multiple backends: pytesseract, easyocr, model-router

Rules:
  - Never executes actions — emits events only
  - Gracefully degrades when OCR libs are missing
"""

from __future__ import annotations

from kernel.event_bus.event_bus import Event

import asyncio
import base64
import io
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from observability.logging.logger import get_logger

log = get_logger(__name__)


# ---------------------------------------------------------------------------
# Data models
# ---------------------------------------------------------------------------


@dataclass
class TextBlock:
    text: str
    x: int = 0
    y: int = 0
    width: int = 0
    height: int = 0
    confidence: float = 1.0
    language: str = "en"

    def as_dict(self) -> dict:
        return {
            "text": self.text,
            "x": self.x,
            "y": self.y,
            "width": self.width,
            "height": self.height,
            "confidence": self.confidence,
            "language": self.language,
        }


@dataclass
class OCRResult:
    source_id: str
    timestamp: float
    full_text: str
    blocks: list[TextBlock] = field(default_factory=list)
    language: str = "en"
    backend: str = "unknown"
    metadata: dict = field(default_factory=dict)

    def as_dict(self) -> dict:
        return {
            "source_id": self.source_id,
            "timestamp": self.timestamp,
            "full_text": self.full_text,
            "blocks": [b.as_dict() for b in self.blocks],
            "language": self.language,
            "backend": self.backend,
            "metadata": self.metadata,
        }


# ---------------------------------------------------------------------------
# OCR Pipeline
# ---------------------------------------------------------------------------


class OCRPipeline:
    """
    Multi-backend OCR pipeline.

    Supported backends (in priority order):
      1. pytesseract  — fast, local, requires Tesseract binary
      2. easyocr      — accurate, GPU-friendly, heavier
      3. model_router — LLM vision fallback
    """

    EVT_TEXT_EXTRACTED = "perception.ocr.text_extracted"
    EVT_OCR_ERROR = "perception.ocr.error"

    def __init__(
        self,
        event_bus: Any,
        model_router: Any | None = None,
        preferred_backend: str = "auto",  # auto | tesseract | easyocr | model
        language: str = "en",
    ) -> None:
        self._bus = event_bus
        self._model_router = model_router
        self._preferred = preferred_backend
        self._language = language
        self._backend_cache: str | None = None
        self._easy_reader: Any = None  # lazy-loaded EasyOCR reader
        self._result_counter = 0

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    async def extract_from_image_b64(
        self, image_b64: str, source_id: str | None = None
    ) -> OCRResult:
        """Run OCR on a base64-encoded PNG/JPEG."""
        sid = source_id or f"b64_{int(time.time())}"
        try:
            image_bytes = base64.b64decode(image_b64)
            return await self._run_ocr_bytes(image_bytes, sid)
        except Exception as exc:
            log.exception("OCR b64 failed: %s", exc)
            await self._emit_error(str(exc))
            raise

    async def extract_from_file(self, path: str | Path) -> OCRResult:
        """Run OCR on an image file."""
        p = Path(path)
        try:
            image_bytes = p.read_bytes()
            return await self._run_ocr_bytes(image_bytes, str(p))
        except Exception as exc:
            log.exception("OCR file failed (%s): %s", path, exc)
            await self._emit_error(str(exc))
            raise

    async def extract_from_pil(
        self, pil_image: Any, source_id: str = "pil"
    ) -> OCRResult:
        """Run OCR on a PIL Image object."""
        try:
            buf = io.BytesIO()
            pil_image.save(buf, format="PNG")
            return await self._run_ocr_bytes(buf.getvalue(), source_id)
        except Exception as exc:
            log.exception("OCR PIL failed: %s", exc)
            await self._emit_error(str(exc))
            raise

    async def extract_from_screen_region(
        self, x: int, y: int, width: int, height: int
    ) -> OCRResult:
        """Capture a screen region and run OCR on it."""
        from perception.vision.vision_pipeline import VisionPipeline, BoundingBox

        vp = VisionPipeline(event_bus=self._bus)
        await vp.start()
        frame = await vp.capture_frame(
            region=BoundingBox(x=x, y=y, width=width, height=height),
            analyze=False,
        )
        await vp.stop()
        return await self.extract_from_image_b64(
            frame.image_b64, source_id=f"screen_{x}_{y}"
        )

    # ------------------------------------------------------------------
    # Internal — backend selection & execution
    # ------------------------------------------------------------------

    async def _run_ocr_bytes(self, image_bytes: bytes, source_id: str) -> OCRResult:
        backend = await self._select_backend()
        self._result_counter += 1

        if backend == "tesseract":
            result = await asyncio.get_running_loop().run_in_executor(
                None, self._ocr_tesseract, image_bytes, source_id
            )
        elif backend == "easyocr":
            result = await asyncio.get_running_loop().run_in_executor(
                None, self._ocr_easyocr, image_bytes, source_id
            )
        elif backend == "model":
            result = await self._ocr_model(image_bytes, source_id)
        else:
            result = OCRResult(
                source_id=source_id,
                timestamp=time.time(),
                full_text="[OCR unavailable — no backend installed]",
                backend="none",
            )

        await self._emit(self.EVT_TEXT_EXTRACTED, result.as_dict())
        log.debug(
            "OCR [%s] → %d chars from %s", backend, len(result.full_text), source_id
        )
        return result

    async def _select_backend(self) -> str:
        if self._preferred != "auto":
            return self._preferred
        if self._backend_cache:
            return self._backend_cache

        # Probe availability once
        try:
            import pytesseract  # noqa: F401

            self._backend_cache = "tesseract"
            return "tesseract"
        except ImportError:
            pass

        try:
            import easyocr  # noqa: F401

            self._backend_cache = "easyocr"
            return "easyocr"
        except ImportError:
            pass

        if self._model_router:
            self._backend_cache = "model"
            return "model"

        self._backend_cache = "none"
        return "none"

    # ------------------------------------------------------------------
    # Backend implementations
    # ------------------------------------------------------------------

    def _ocr_tesseract(self, image_bytes: bytes, source_id: str) -> OCRResult:
        import pytesseract
        from PIL import Image

        img = Image.open(io.BytesIO(image_bytes))
        data = pytesseract.image_to_data(
            img,
            lang=self._language,
            output_type=pytesseract.Output.DICT,
        )
        blocks: list[TextBlock] = []
        full_parts: list[str] = []

        for i, word in enumerate(data["text"]):
            word = word.strip()
            if not word:
                continue
            conf = float(data["conf"][i])
            if conf < 0:
                continue
            blocks.append(
                TextBlock(
                    text=word,
                    x=int(data["left"][i]),
                    y=int(data["top"][i]),
                    width=int(data["width"][i]),
                    height=int(data["height"][i]),
                    confidence=conf / 100.0,
                    language=self._language,
                )
            )
            full_parts.append(word)

        return OCRResult(
            source_id=source_id,
            timestamp=time.time(),
            full_text=" ".join(full_parts),
            blocks=blocks,
            language=self._language,
            backend="tesseract",
        )

    def _ocr_easyocr(self, image_bytes: bytes, source_id: str) -> OCRResult:
        import easyocr
        import numpy as np
        from PIL import Image

        if self._easy_reader is None:
            self._easy_reader = easyocr.Reader([self._language], gpu=False)

        img = Image.open(io.BytesIO(image_bytes))
        np_img = np.array(img)
        raw = self._easy_reader.readtext(np_img)

        blocks: list[TextBlock] = []
        full_parts: list[str] = []

        for bbox_pts, text, conf in raw:
            xs = [p[0] for p in bbox_pts]
            ys = [p[1] for p in bbox_pts]
            blocks.append(
                TextBlock(
                    text=text,
                    x=int(min(xs)),
                    y=int(min(ys)),
                    width=int(max(xs) - min(xs)),
                    height=int(max(ys) - min(ys)),
                    confidence=float(conf),
                    language=self._language,
                )
            )
            full_parts.append(text)

        return OCRResult(
            source_id=source_id,
            timestamp=time.time(),
            full_text=" ".join(full_parts),
            blocks=blocks,
            language=self._language,
            backend="easyocr",
        )

    async def _ocr_model(self, image_bytes: bytes, source_id: str) -> OCRResult:
        b64 = base64.b64encode(image_bytes).decode()
        try:
            result = await self._model_router.complete(
                messages=[
                    {
                        "role": "user",
                        "content": [
                            {
                                "type": "text",
                                "text": (
                                    "Extract ALL text visible in this image. "
                                    "Return only the raw text, preserving line breaks. "
                                    "Do not add any commentary."
                                ),
                            },
                            {
                                "type": "image",
                                "source": {
                                    "type": "base64",
                                    "media_type": "image/png",
                                    "data": b64,
                                },
                            },
                        ],
                    }
                ]
            )
            text = result.get("text", "")
        except Exception as exc:
            log.warning("Model OCR failed: %s", exc)
            text = ""

        return OCRResult(
            source_id=source_id,
            timestamp=time.time(),
            full_text=text,
            blocks=[TextBlock(text=text)] if text else [],
            backend="model",
        )

    # ------------------------------------------------------------------
    # Event helpers
    # ------------------------------------------------------------------

    async def _emit(self, event_type: str, payload: dict) -> None:
        if self._bus:
            try:
                await self._bus.publish(
                    Event(event_type=event_type, source="ocr_pipeline", payload=payload)
                )
            except Exception as exc:
                log.warning("Event publish failed: %s", exc)

    async def _emit_error(self, error: str) -> None:
        await self._emit(self.EVT_OCR_ERROR, {"error": error})