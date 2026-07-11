"""
perception/vision/image_analysis.py
──────────────────────────────────────
Image analysis helpers used by VisionAgent and VisionWorkspace.
Wraps PIL + pytesseract for basic OCR and image metadata.
"""
from __future__ import annotations
import logging
from pathlib import Path
from typing import Any

log = logging.getLogger(__name__)


def analyse_image(image_path: str | Path) -> dict[str, Any]:
    """Return basic metadata and OCR text from an image file."""
    result: dict[str, Any] = {
        "path": str(image_path),
        "width": None,
        "height": None,
        "mode": None,
        "ocr_text": "",
        "error": None,
    }
    try:
        from PIL import Image
        img = Image.open(image_path)
        result["width"], result["height"] = img.size
        result["mode"] = img.mode
    except Exception as exc:
        result["error"] = f"PIL failed: {exc}"
        log.debug("analyse_image PIL error: %s", exc)
        return result

    try:
        import pytesseract
        result["ocr_text"] = pytesseract.image_to_string(img).strip()
    except Exception as exc:
        log.debug("analyse_image OCR error: %s", exc)
        result["ocr_text"] = ""

    return result


def analyse_bytes(image_bytes: bytes) -> dict[str, Any]:
    """Same as analyse_image but accepts raw bytes instead of a file path."""
    import io
    try:
        from PIL import Image
        img = Image.open(io.BytesIO(image_bytes))
    except Exception as exc:
        return {"error": str(exc), "ocr_text": ""}

    result: dict[str, Any] = {
        "width": img.width,
        "height": img.height,
        "mode": img.mode,
        "ocr_text": "",
        "error": None,
    }
    try:
        import pytesseract
        result["ocr_text"] = pytesseract.image_to_string(img).strip()
    except Exception as exc:
        log.debug("analyse_bytes OCR error: %s", exc)
    return result
