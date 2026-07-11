"""
JARVIS AI OS — Screenshot Analysis
=====================================
Deep semantic analysis of captured screenshots.

Builds on VisionPipeline (raw capture) and OCRPipeline (text extraction)
to produce rich ScreenState objects that agents can reason over.

Responsibilities:
  - Combine vision + OCR into a unified screen state
  - Classify UI context (ide, browser, terminal, document, etc.)
  - Detect changes between frames (diff analysis)
  - Publish perception.screenshot.analysed events

Rules:
  - No side effects — read-only analysis, events only
"""

from __future__ import annotations

import hashlib
import time
from dataclasses import dataclass, field
from typing import Any

from observability.logging.logger import get_logger
from kernel.event_bus.event_bus import Event
from perception.vision.vision_pipeline import VisionFrame, DetectedElement
from perception.ocr.ocr_pipeline import OCRResult

log = get_logger(__name__)


# ---------------------------------------------------------------------------
# Data models
# ---------------------------------------------------------------------------


class UIContext:
    """Constants for UI context types."""

    BROWSER = "browser"
    TERMINAL = "terminal"
    IDE = "ide"
    DOCUMENT = "document"
    SPREADSHEET = "spreadsheet"
    FILE_MANAGER = "file_manager"
    SETTINGS = "settings"
    MEDIA = "media"
    UNKNOWN = "unknown"


@dataclass
class ScreenChange:
    change_type: str  # new_text | element_added | element_removed | layout_shift
    description: str
    region: dict = field(default_factory=dict)

    def as_dict(self) -> dict:
        return {
            "change_type": self.change_type,
            "description": self.description,
            "region": self.region,
        }


@dataclass
class ScreenState:
    """Complete semantic state of the current screen."""

    state_id: str
    timestamp: float
    frame: VisionFrame
    ocr_result: OCRResult | None
    ui_context: str = UIContext.UNKNOWN
    active_app: str = ""
    window_title: str = ""
    visible_text: str = ""
    interactive_elements: list[DetectedElement] = field(default_factory=list)
    changes_from_previous: list[ScreenChange] = field(default_factory=list)
    content_hash: str = ""
    summary: str = ""
    metadata: dict = field(default_factory=dict)

    def as_dict(self) -> dict:
        return {
            "state_id": self.state_id,
            "timestamp": self.timestamp,
            "ui_context": self.ui_context,
            "active_app": self.active_app,
            "window_title": self.window_title,
            "visible_text": self.visible_text[:2000],  # truncate for events
            "interactive_elements": [e.as_dict() for e in self.interactive_elements],
            "changes": [c.as_dict() for c in self.changes_from_previous],
            "content_hash": self.content_hash,
            "summary": self.summary,
            "metadata": self.metadata,
        }


# ---------------------------------------------------------------------------
# Screenshot Analyser
# ---------------------------------------------------------------------------


class ScreenshotAnalyser:
    """
    Orchestrates VisionPipeline + OCRPipeline for full screen analysis.
    """

    EVT_ANALYSED = "perception.screenshot.analysed"
    EVT_CHANGE_DETECTED = "perception.screenshot.change_detected"
    EVT_ERROR = "perception.screenshot.error"

    def __init__(
        self,
        event_bus: Any,
        vision_pipeline: Any,
        ocr_pipeline: Any,
        model_router: Any | None = None,
    ) -> None:
        self._bus = event_bus
        self._vision = vision_pipeline
        self._ocr = ocr_pipeline
        self._model_router = model_router
        self._previous_state: ScreenState | None = None
        self._state_counter = 0

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    async def analyse_current_screen(self) -> ScreenState:
        """Capture and fully analyse the current screen."""
        try:
            # 1. Capture frame
            frame = await self._vision.capture_frame(analyze=False)

            # 2. OCR
            ocr_result = await self._ocr.extract_from_image_b64(
                frame.image_b64, source_id=frame.frame_id
            )

            # 3. Build state
            state = await self._build_state(frame, ocr_result)

            # 4. Diff against previous
            if self._previous_state:
                state.changes_from_previous = self._diff_states(
                    self._previous_state, state
                )
                if state.changes_from_previous:
                    await self._emit(
                        self.EVT_CHANGE_DETECTED,
                        {
                            "state_id": state.state_id,
                            "changes": [
                                c.as_dict() for c in state.changes_from_previous
                            ],
                        },
                    )

            self._previous_state = state
            await self._emit(self.EVT_ANALYSED, state.as_dict())
            return state

        except Exception as exc:
            log.exception("Screenshot analysis failed: %s", exc)
            await self._emit(self.EVT_ERROR, {"error": str(exc)})
            raise

    async def analyse_frame(
        self, frame: VisionFrame, run_ocr: bool = True
    ) -> ScreenState:
        """Analyse a pre-captured frame."""
        ocr_result = None
        if run_ocr:
            ocr_result = await self._ocr.extract_from_image_b64(
                frame.image_b64, source_id=frame.frame_id
            )
        return await self._build_state(frame, ocr_result)

    # ------------------------------------------------------------------
    # State construction
    # ------------------------------------------------------------------

    async def _build_state(
        self, frame: VisionFrame, ocr_result: OCRResult | None
    ) -> ScreenState:
        self._state_counter += 1
        state_id = f"state_{self._state_counter}_{int(time.time())}"

        visible_text = ocr_result.full_text if ocr_result else frame.description

        # Compute content hash for change detection
        content_hash = hashlib.md5(visible_text.encode()).hexdigest()

        # Classify UI context
        ui_context = await self._classify_context(frame, visible_text)

        # Detect interactive elements
        interactive = [
            e
            for e in frame.elements
            if e.element_type in ("button", "input", "link", "dropdown", "checkbox")
        ]

        # Build summary
        summary = await self._summarise(frame, visible_text, ui_context)

        state = ScreenState(
            state_id=state_id,
            timestamp=time.time(),
            frame=frame,
            ocr_result=ocr_result,
            ui_context=ui_context,
            active_app=frame.active_window,
            window_title=frame.metadata.get("window_title", ""),
            visible_text=visible_text,
            interactive_elements=interactive,
            content_hash=content_hash,
            summary=summary,
        )
        return state

    async def _classify_context(self, frame: VisionFrame, text: str) -> str:
        """Heuristic + optional model-based UI context classification."""
        text_lower = text.lower()

        # Quick heuristic
        if any(
            kw in text_lower
            for kw in ["http://", "https://", "www.", "back", "forward", "address bar"]
        ):
            return UIContext.BROWSER
        if any(
            kw in text_lower for kw in ["$", "bash", "zsh", "fish", "powershell", "cmd"]
        ):
            return UIContext.TERMINAL
        if any(
            kw in text_lower
            for kw in ["def ", "class ", "import ", "function", "const ", "var "]
        ):
            return UIContext.IDE
        if any(
            kw in text_lower for kw in [".docx", ".pdf", ".doc", "word", "document"]
        ):
            return UIContext.DOCUMENT
        if any(
            kw in text_lower
            for kw in [".xlsx", ".csv", "spreadsheet", "cell", "formula"]
        ):
            return UIContext.SPREADSHEET
        if any(kw in text_lower for kw in ["settings", "preferences", "configuration"]):
            return UIContext.SETTINGS

        if self._model_router and frame.description:
            try:
                result = await self._model_router.complete(
                    messages=[
                        {
                            "role": "user",
                            "content": (
                                f"Screen description: {frame.description}\n"
                                "Classify the UI context as exactly one of: "
                                "browser, terminal, ide, document, spreadsheet, "
                                "file_manager, settings, media, unknown. "
                                "Reply with only the single word."
                            ),
                        }
                    ]
                )
                ctx = result.get("text", "").strip().lower()
                if ctx in vars(UIContext).values():
                    return ctx
            except Exception:
                pass

        return UIContext.UNKNOWN

    async def _summarise(self, frame: VisionFrame, text: str, ui_context: str) -> str:
        if frame.description:
            return frame.description
        if text:
            return text[:200]
        return f"Screen captured ({ui_context})"

    def _diff_states(self, prev: ScreenState, curr: ScreenState) -> list[ScreenChange]:
        """Detect meaningful changes between two screen states."""
        changes: list[ScreenChange] = []

        if prev.content_hash != curr.content_hash:
            # Text diff
            prev_words = set(prev.visible_text.split())
            curr_words = set(curr.visible_text.split())
            new_words = curr_words - prev_words
            if new_words:
                changes.append(
                    ScreenChange(
                        change_type="new_text",
                        description=f"New text appeared: {' '.join(list(new_words)[:10])}",
                    )
                )

        # Element count diff
        prev_cnt = len(prev.interactive_elements)
        curr_cnt = len(curr.interactive_elements)
        if curr_cnt > prev_cnt:
            changes.append(
                ScreenChange(
                    change_type="element_added",
                    description=f"{curr_cnt - prev_cnt} new interactive element(s) appeared",
                )
            )
        elif curr_cnt < prev_cnt:
            changes.append(
                ScreenChange(
                    change_type="element_removed",
                    description=f"{prev_cnt - curr_cnt} interactive element(s) disappeared",
                )
            )

        # Context shift
        if prev.ui_context != curr.ui_context:
            changes.append(
                ScreenChange(
                    change_type="layout_shift",
                    description=f"UI context changed from {prev.ui_context} to {curr.ui_context}",
                )
            )

        return changes

    # ------------------------------------------------------------------
    # Event helpers
    # ------------------------------------------------------------------

    async def _emit(self, event_type: str, payload: dict) -> None:
        if self._bus:
            try:
                await self._bus.publish(
                    Event(event_type=event_type, source="screenshot_analyser", payload=payload)
                )
            except Exception as exc:
                log.warning("Event publish failed: %s", exc)