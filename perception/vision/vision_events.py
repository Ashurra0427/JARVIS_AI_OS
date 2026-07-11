"""
JARVIS AI OS — Vision Event Definitions
=========================================
Single source of truth for all perception.vision.* event type strings
and their canonical payload schemas.

Events flow:
  ScreenshotService → VisionPipeline → ContextClassifier
                    → OCRPipeline
                    → ScreenshotAnalyzer
  All results emitted onto EventBus as vision.* events.

Rules:
  - Only constants and lightweight payload dataclasses here.
  - No business logic; no imports from action layer.
"""

from __future__ import annotations

from dataclasses import dataclass, field


# ---------------------------------------------------------------------------
# Event type constants
# ---------------------------------------------------------------------------


class VisionEvent:
    """Namespace for all vision event type strings."""

    # ScreenshotService
    CAPTURE_COMPLETED = "vision.capture.completed"
    CAPTURE_FAILED = "vision.capture.failed"

    # OCRPipeline
    OCR_COMPLETED = "vision.ocr.completed"
    OCR_FAILED = "vision.ocr.failed"

    # VisionPipeline / image analysis
    ANALYSIS_COMPLETED = "vision.analysis.completed"
    ANALYSIS_FAILED = "vision.analysis.failed"
    OBJECT_DETECTED = "vision.object.detected"

    # ContextClassifier / ScreenshotAnalyzer
    SCREEN_CHANGED = "vision.screen.changed"
    CONTEXT_CLASSIFIED = "vision.context.classified"


# ---------------------------------------------------------------------------
# Payload schemas (dataclasses, not frozen — allow mutation during build)
# ---------------------------------------------------------------------------


@dataclass
class CapturePayload:
    """Payload for vision.capture.completed."""

    capture_id: str
    image_b64: str  # base64-encoded PNG/JPEG
    width: int
    height: int
    region: dict | None = None  # {"x","y","w","h"} or None = full screen
    window_title: str | None = None
    timestamp: float = 0.0
    source: str = "screenshot_service"

    def as_dict(self) -> dict:
        return {
            "capture_id": self.capture_id,
            "image_b64": self.image_b64,
            "width": self.width,
            "height": self.height,
            "region": self.region,
            "window_title": self.window_title,
            "timestamp": self.timestamp,
            "source": self.source,
        }


@dataclass
class OCRPayload:
    """Payload for vision.ocr.completed."""

    capture_id: str
    text: str
    lines: list[str] = field(default_factory=list)
    confidence: float = 1.0
    language: str = "en"
    duration_ms: float = 0.0

    def as_dict(self) -> dict:
        return {
            "capture_id": self.capture_id,
            "text": self.text,
            "lines": self.lines,
            "confidence": self.confidence,
            "language": self.language,
            "duration_ms": self.duration_ms,
        }


@dataclass
class AnalysisPayload:
    """Payload for vision.analysis.completed."""

    capture_id: str
    elements: list[dict] = field(default_factory=list)  # DetectedElement.as_dict()
    context_type: str = "unknown"
    summary: str = ""
    metadata: dict = field(default_factory=dict)
    duration_ms: float = 0.0

    def as_dict(self) -> dict:
        return {
            "capture_id": self.capture_id,
            "elements": self.elements,
            "context_type": self.context_type,
            "summary": self.summary,
            "metadata": self.metadata,
            "duration_ms": self.duration_ms,
        }


@dataclass
class ObjectDetectedPayload:
    """Payload for vision.object.detected (per-object events)."""

    capture_id: str
    object_type: str
    label: str
    bbox: dict  # {"x","y","width","height"}
    confidence: float = 1.0
    attributes: dict = field(default_factory=dict)

    def as_dict(self) -> dict:
        return {
            "capture_id": self.capture_id,
            "object_type": self.object_type,
            "label": self.label,
            "bbox": self.bbox,
            "confidence": self.confidence,
            "attributes": self.attributes,
        }


@dataclass
class ScreenChangedPayload:
    """Payload for vision.screen.changed."""

    previous_context: str
    current_context: str
    change_score: float  # 0.0–1.0; 1.0 = completely different
    diff_regions: list[dict] = field(default_factory=list)
    timestamp: float = 0.0

    def as_dict(self) -> dict:
        return {
            "previous_context": self.previous_context,
            "current_context": self.current_context,
            "change_score": self.change_score,
            "diff_regions": self.diff_regions,
            "timestamp": self.timestamp,
        }
