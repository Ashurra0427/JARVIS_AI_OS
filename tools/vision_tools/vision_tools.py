"""
tools/vision_tools/vision_tools.py
────────────────────────────────────
Vision tool implementations for JARVIS AI OS.

Provides:
  vision.analyze         — full analysis of an image file
  vision.describe        — natural language description of an image
  vision.detect_objects  — detect and list objects in an image
  vision.ocr             — extract text from an image (OCR)

All tools accept a local file path or URL.  They route through the
perception.vision pipeline when available, with graceful degradation
to stub responses so agents always receive a ToolResult.
"""

from __future__ import annotations

import base64
import logging
import os
import time
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from tools.registry.tool_registry import ToolRegistry

log = logging.getLogger(__name__)

# ──────────────────────────────────────────────
# Optional deps
# ──────────────────────────────────────────────

try:
    from PIL import Image as _PIL_Image

    _HAS_PIL = True
except ImportError:
    _HAS_PIL = False

try:
    import pytesseract as _tesseract

    _HAS_TESSERACT = True
except ImportError:
    _HAS_TESSERACT = False


# ──────────────────────────────────────────────
# Helpers
# ──────────────────────────────────────────────


def _load_image_bytes(source: str) -> bytes:
    """Load image bytes from a local path or URL."""
    if source.startswith("http://") or source.startswith("https://"):
        try:
            import requests

            resp = requests.get(source, timeout=20)
            resp.raise_for_status()
            return resp.content
        except ImportError:
            import urllib.request

            with urllib.request.urlopen(source, timeout=20) as r:
                return r.read()
    else:
        with open(source, "rb") as f:
            return f.read()


def _image_basic_info(source: str) -> dict:
    """Return basic image metadata using PIL if available."""
    if _HAS_PIL:
        img = _PIL_Image.open(source) if os.path.isfile(source) else None
        if img:
            return {
                "width": img.width,
                "height": img.height,
                "mode": img.mode,
                "format": img.format,
            }
    return {}


def _route_to_vision_pipeline(source: str, operation: str) -> dict | None:
    """
    Attempt to route through perception.vision pipeline.
    Returns result dict or None if pipeline unavailable.
    """
    try:
        from perception.vision.image_analysis import ImageAnalyzer

        analyzer = ImageAnalyzer()
        img_bytes = _load_image_bytes(source)
        b64 = base64.b64encode(img_bytes).decode()

        if operation == "analyze":
            return analyzer.analyze(image_b64=b64, source=source)
        elif operation == "describe":
            return analyzer.describe(image_b64=b64, source=source)
        elif operation == "detect_objects":
            return analyzer.detect_objects(image_b64=b64, source=source)
    except Exception as exc:
        log.debug("vision pipeline unavailable (%s), using fallback", exc)
    return None


# ──────────────────────────────────────────────
# Tool implementations
# ──────────────────────────────────────────────


def vision_analyze(source: str) -> dict:
    """
    Perform a full analysis of an image (dimensions, colors, objects, text).

    Args:
      source — local file path or URL of the image

    Returns:
      source     — original source
      dimensions — {width, height}
      format     — image format (JPEG, PNG, ...)
      analysis   — analysis dict from vision pipeline (or stub)
    """
    if not source:
        raise ValueError("source must be provided")

    result = _route_to_vision_pipeline(source, "analyze")
    if result:
        return result

    # Fallback: basic info only
    info = _image_basic_info(source) if os.path.isfile(source) else {}
    return {
        "source": source,
        "dimensions": {"width": info.get("width"), "height": info.get("height")},
        "format": info.get("format"),
        "analysis": {
            "note": "Full vision pipeline not available; install perception.vision deps."
        },
    }


def vision_describe(source: str) -> dict:
    """
    Generate a natural language description of an image.

    Returns:
      source      — original source
      description — natural language description string
    """
    if not source:
        raise ValueError("source must be provided")

    result = _route_to_vision_pipeline(source, "describe")
    if result:
        return result

    info = _image_basic_info(source) if os.path.isfile(source) else {}
    desc = "An image"
    if info:
        desc = (
            f"A {info.get('format', 'image')} image measuring "
            f"{info.get('width', '?')}×{info.get('height', '?')} pixels "
            f"in {info.get('mode', 'unknown')} colour mode."
        )

    return {"source": source, "description": desc}


def vision_detect_objects(source: str, confidence_threshold: float = 0.5) -> dict:
    """
    Detect and list objects present in an image.

    Returns:
      source    — original source
      objects   — list of {label, confidence, bbox}
      count     — number of detected objects
    """
    if not source:
        raise ValueError("source must be provided")

    result = _route_to_vision_pipeline(source, "detect_objects")
    if result:
        return result

    # Stub: no object detection without model
    return {
        "source": source,
        "objects": [],
        "count": 0,
        "note": "Object detection requires a vision model (e.g. YOLOv8). "
        "Install perception.vision dependencies to enable.",
    }


def vision_ocr(source: str, language: str = "eng") -> dict:
    """
    Extract text from an image using OCR.

    Args:
      source   — local file path or URL of the image
      language — Tesseract language code (default: 'eng')

    Returns:
      source   — original source
      text     — extracted text
      engine   — 'tesseract' | 'stub'
    """
    if not source:
        raise ValueError("source must be provided")

    if _HAS_TESSERACT and _HAS_PIL and os.path.isfile(source):
        try:
            img = _PIL_Image.open(source)
            text = _tesseract.image_to_string(img, lang=language)
            return {"source": source, "text": text.strip(), "engine": "tesseract"}
        except Exception as exc:
            log.warning("tesseract OCR failed: %s", exc)

    # Try perception pipeline
    try:
        from perception.ocr.ocr_pipeline import OCRPipeline

        pipeline = OCRPipeline()
        img_bytes = _load_image_bytes(source)
        text = pipeline.extract_text(img_bytes)
        return {"source": source, "text": text, "engine": "ocr_pipeline"}
    except Exception:
        pass

    return {
        "source": source,
        "text": "",
        "engine": "stub",
        "note": "OCR requires pytesseract+Pillow or the perception.ocr pipeline.",
    }


# ──────────────────────────────────────────────
# Registration
# ──────────────────────────────────────────────


def register_vision_tools(registry: "ToolRegistry", event_bus=None) -> list[str]:
    """Register all vision tools into the provided ToolRegistry."""
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
        ToolDefinition(
            name="vision.analyze",
            handler=_wrap(vision_analyze, "vision.analyze"),
            description="Perform a full analysis of an image (dimensions, colours, objects, text).",
            tags=["vision", "analyze", "image"],
            timeout_s=30.0,
        ),
        ToolDefinition(
            name="vision.describe",
            handler=_wrap(vision_describe, "vision.describe"),
            description="Generate a natural language description of an image.",
            tags=["vision", "describe", "image", "caption"],
            timeout_s=30.0,
        ),
        ToolDefinition(
            name="vision.detect_objects",
            handler=_wrap(vision_detect_objects, "vision.detect_objects"),
            description="Detect and list objects present in an image.",
            tags=["vision", "detect", "objects", "yolo"],
            timeout_s=30.0,
        ),
        ToolDefinition(
            name="vision.ocr",
            handler=_wrap(vision_ocr, "vision.ocr"),
            description="Extract text from an image using OCR.",
            tags=["vision", "ocr", "text", "extract"],
            timeout_s=30.0,
        ),
    ]

    registered = []
    for defn in tools:
        registry.register(defn)
        registered.append(defn.name)
        log.info("Registered tool: %s", defn.name)

    return registered
