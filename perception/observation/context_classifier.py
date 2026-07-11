"""
JARVIS AI OS — Context Classifier
====================================
Fuses signals from Vision, OCR, and Activity to classify the
current work context and user intent.

Responsibilities:
  - Aggregate perception signals into a ContextSnapshot
  - Emit perception.context.classified events
  - Maintain a short rolling history for trend detection
  - Provide confidence scores per classification

Rules:
  - Stateless from action perspective — never performs actions
  - Subscribes to perception events via Event Bus
"""

from __future__ import annotations

from kernel.event_bus.event_bus import Event

import time
from collections import deque
from dataclasses import dataclass, field
from enum import Enum
from typing import Any

from observability.logging.logger import get_logger

log = get_logger(__name__)


# ---------------------------------------------------------------------------
# Classification taxonomy
# ---------------------------------------------------------------------------


class WorkContext(str, Enum):
    CODING = "coding"
    DEBUGGING = "debugging"
    CODE_REVIEW = "code_review"
    WEB_BROWSING = "web_browsing"
    WEB_RESEARCH = "web_research"
    WRITING = "writing"
    EMAIL = "email"
    TERMINAL_WORK = "terminal_work"
    FILE_MANAGEMENT = "file_management"
    MEDIA_CONSUMPTION = "media_consumption"
    VIDEO_CALL = "video_call"
    IDLE = "idle"
    UNKNOWN = "unknown"


@dataclass
class ContextSignal:
    """A single input signal for classification."""

    signal_type: str  # vision | ocr | activity | window
    value: str  # raw signal value
    confidence: float = 1.0
    timestamp: float = field(default_factory=time.time)


@dataclass
class ContextClassification:
    """Result of classifying a set of signals."""

    context: WorkContext
    confidence: float
    sub_context: str = ""  # e.g. "python" within coding
    signals_used: list[str] = field(default_factory=list)
    alternatives: list[dict] = field(default_factory=list)

    def as_dict(self) -> dict:
        return {
            "context": self.context.value,
            "confidence": self.confidence,
            "sub_context": self.sub_context,
            "signals_used": self.signals_used,
            "alternatives": self.alternatives,
        }


@dataclass
class ContextSnapshot:
    snapshot_id: str
    timestamp: float
    classification: ContextClassification
    raw_signals: list[ContextSignal] = field(default_factory=list)
    metadata: dict = field(default_factory=dict)

    def as_dict(self) -> dict:
        return {
            "snapshot_id": self.snapshot_id,
            "timestamp": self.timestamp,
            "classification": self.classification.as_dict(),
            "metadata": self.metadata,
        }


# ---------------------------------------------------------------------------
# Context Classifier
# ---------------------------------------------------------------------------


class ContextClassifier:
    """
    Fuses perception events into a classified context snapshot.

    Can be used in two modes:
      1. Standalone: call classify_from_signals() directly
      2. Event-driven: subscribe to perception events and auto-classify
    """

    EVT_CLASSIFIED = "perception.context.classified"
    EVT_CONTEXT_CHANGED = "perception.context.changed"

    # Subscribed event types (from other perception modules)
    _SUBSCRIBED = [
        "perception.screenshot.analysed",
        "perception.activity.window_changed",
        "perception.activity.snapshot",
        "perception.ocr.text_extracted",
    ]

    def __init__(
        self,
        event_bus: Any,
        model_router: Any | None = None,
        history_size: int = 20,
    ) -> None:
        self._bus = event_bus
        self._model_router = model_router
        self._history: deque[ContextSnapshot] = deque(maxlen=history_size)
        self._current: ContextSnapshot | None = None
        self._snap_counter = 0
        self._subscribed = False

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------

    async def start(self) -> None:
        if self._bus and not self._subscribed:
            for evt in self._SUBSCRIBED:
                self._bus.subscribe(evt, self._on_perception_event)
            self._subscribed = True
        log.info("ContextClassifier started")

    async def stop(self) -> None:
        log.info("ContextClassifier stopped")

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    async def classify_from_signals(
        self, signals: list[ContextSignal]
    ) -> ContextClassification:
        """Run classification on a set of signals."""
        classification = await self._classify(signals)
        return classification

    async def classify_from_screen_state(self, state: Any) -> ContextSnapshot:
        """
        Classify from a ScreenState object (from ScreenshotAnalyser).
        """
        signals: list[ContextSignal] = []

        if state.visible_text:
            signals.append(
                ContextSignal(
                    signal_type="ocr",
                    value=state.visible_text[:500],
                )
            )
        if state.ui_context:
            signals.append(
                ContextSignal(
                    signal_type="vision",
                    value=state.ui_context,
                )
            )
        if state.active_app:
            signals.append(
                ContextSignal(
                    signal_type="window",
                    value=state.active_app,
                )
            )
        if state.summary:
            signals.append(
                ContextSignal(
                    signal_type="vision",
                    value=state.summary,
                    confidence=0.8,
                )
            )

        return await self._build_snapshot(signals)

    async def get_current_context(self) -> ContextSnapshot | None:
        return self._current

    async def get_context_history(self) -> list[ContextSnapshot]:
        return list(self._history)

    # ------------------------------------------------------------------
    # Event-driven handler
    # ------------------------------------------------------------------

    async def _on_perception_event(self, event: Any) -> None:
        try:
            signals = self._event_to_signals(event)
            if signals:
                await self._build_snapshot(signals)
        except Exception as exc:
            log.warning("Context classification error: %s", exc)

    def _event_to_signals(self, event: Any) -> list[ContextSignal]:
        signals: list[ContextSignal] = []
        payload = event.payload if hasattr(event, "payload") else {}

        if event.event_type == "perception.screenshot.analysed":
            if payload.get("ui_context"):
                signals.append(
                    ContextSignal(
                        signal_type="vision",
                        value=payload["ui_context"],
                    )
                )
            if payload.get("visible_text"):
                signals.append(
                    ContextSignal(
                        signal_type="ocr",
                        value=payload["visible_text"][:300],
                    )
                )
            if payload.get("active_app"):
                signals.append(
                    ContextSignal(
                        signal_type="window",
                        value=payload["active_app"],
                    )
                )

        elif event.event_type == "perception.activity.window_changed":
            to_info = payload.get("to", {})
            signals.append(
                ContextSignal(
                    signal_type="window",
                    value=f"{to_info.get('app_name', '')} {to_info.get('title', '')}".strip(),
                )
            )

        elif event.event_type == "perception.ocr.text_extracted":
            text = payload.get("full_text", "")
            if text:
                signals.append(
                    ContextSignal(
                        signal_type="ocr",
                        value=text[:300],
                        confidence=0.9,
                    )
                )

        return signals

    # ------------------------------------------------------------------
    # Core classification logic
    # ------------------------------------------------------------------

    async def _build_snapshot(self, signals: list[ContextSignal]) -> ContextSnapshot:
        self._snap_counter += 1
        classification = await self._classify(signals)

        snap = ContextSnapshot(
            snapshot_id=f"ctx_{self._snap_counter}_{int(time.time())}",
            timestamp=time.time(),
            classification=classification,
            raw_signals=signals,
        )

        prev_context = self._current.classification.context if self._current else None
        self._current = snap
        self._history.append(snap)

        await self._emit(self.EVT_CLASSIFIED, snap.as_dict())

        if prev_context and prev_context != classification.context:
            await self._emit(
                self.EVT_CONTEXT_CHANGED,
                {
                    "from": prev_context.value,
                    "to": classification.context.value,
                    "confidence": classification.confidence,
                },
            )

        return snap

    async def _classify(self, signals: list[ContextSignal]) -> ContextClassification:
        """Heuristic rules → optional model refinement."""

        # Aggregate signal text
        combined = " ".join(s.value.lower() for s in signals)
        used_signals = [s.signal_type for s in signals]

        # ----- Heuristic classification -----
        rules: list[tuple[WorkContext, float, list[str]]] = [
            # (context, confidence, keywords)
            (
                WorkContext.TERMINAL_WORK,
                0.90,
                ["bash", "zsh", "terminal", "powershell", "$ ", "# "],
            ),
            (
                WorkContext.DEBUGGING,
                0.90,
                [
                    "breakpoint",
                    "debugger",
                    "traceback",
                    "error:",
                    "exception",
                    "stack trace",
                ],
            ),
            (
                WorkContext.CODING,
                0.85,
                [
                    "def ",
                    "class ",
                    "import ",
                    "function(",
                    "const ",
                    "return ",
                    "if __name__",
                ],
            ),
            (
                WorkContext.CODE_REVIEW,
                0.80,
                ["diff", "pull request", "github", "gitlab", "review", "merge"],
            ),
            (
                WorkContext.WEB_RESEARCH,
                0.80,
                [
                    "google",
                    "search results",
                    "wikipedia",
                    "documentation",
                    "how to",
                    "stackoverflow",
                ],
            ),
            (
                WorkContext.WEB_BROWSING,
                0.70,
                ["http", "www.", "browser", "chrome", "firefox", "safari"],
            ),
            (
                WorkContext.EMAIL,
                0.85,
                ["inbox", "compose", "reply", "gmail", "outlook", "subject:"],
            ),
            (
                WorkContext.VIDEO_CALL,
                0.90,
                ["zoom", "teams", "meet", "hangout", "webex", "meeting"],
            ),
            (
                WorkContext.WRITING,
                0.70,
                ["word", "google docs", "notion", "document", "essay", "paragraph"],
            ),
            (
                WorkContext.FILE_MANAGEMENT,
                0.80,
                ["finder", "explorer", "nautilus", "files", "folder"],
            ),
            (
                WorkContext.MEDIA_CONSUMPTION,
                0.80,
                ["youtube", "netflix", "spotify", "vlc", "mpv", "player"],
            ),
        ]

        best_ctx = WorkContext.UNKNOWN
        best_conf = 0.0
        alternatives: list[dict] = []

        for ctx, base_conf, keywords in rules:
            matches = sum(1 for kw in keywords if kw in combined)
            if matches > 0:
                conf = min(1.0, base_conf + 0.02 * (matches - 1))
                alternatives.append({"context": ctx.value, "confidence": conf})
                if conf > best_conf:
                    best_ctx = ctx
                    best_conf = conf

        # Sort alternatives descending
        alternatives.sort(key=lambda x: x["confidence"], reverse=True)
        alts = [a for a in alternatives if a["context"] != best_ctx.value][:3]

        # Sub-context for coding
        sub_context = ""
        if best_ctx == WorkContext.CODING:
            for lang, kws in [
                ("python", ["def ", "import ", ".py", "print("]),
                ("javascript", ["const ", "let ", "function(", "console.log"]),
                ("typescript", ["interface ", "type ", "readonly", ": string"]),
                ("rust", ["fn ", "let mut", "impl ", "pub "]),
                ("go", ["func ", "package ", "import (\n"]),
                ("java", ["public class", "System.out", "void main"]),
            ]:
                if any(kw in combined for kw in kws):
                    sub_context = lang
                    break

        # Model refinement if uncertain
        if best_conf < 0.6 and self._model_router:
            try:
                sample = combined[:400]
                result = await self._model_router.complete(
                    messages=[
                        {
                            "role": "user",
                            "content": (
                                f"Classify this work context from screen signals:\n{sample}\n\n"
                                "Pick ONE from: coding, debugging, code_review, web_browsing, "
                                "web_research, writing, email, terminal_work, file_management, "
                                "media_consumption, video_call, idle, unknown.\n"
                                'Reply with JSON: {"context": "...", "confidence": 0.0-1.0}'
                            ),
                        }
                    ]
                )
                import json

                data = json.loads(result.get("text", "{}"))
                model_ctx = data.get("context", "")
                model_conf = float(data.get("confidence", 0.5))
                try:
                    best_ctx = WorkContext(model_ctx)
                    best_conf = model_conf
                except ValueError:
                    pass
            except Exception as exc:
                log.debug("Model classification failed: %s", exc)

        return ContextClassification(
            context=best_ctx,
            confidence=best_conf,
            sub_context=sub_context,
            signals_used=list(set(used_signals)),
            alternatives=alts,
        )

    # ------------------------------------------------------------------
    # Event helper
    # ------------------------------------------------------------------

    async def _emit(self, event_type: str, payload: dict) -> None:
        if self._bus:
            try:
                await self._bus.publish(
                    Event(event_type=event_type, source="context_classifier", payload=payload)
                )
            except Exception as exc:
                log.warning("Event publish failed: %s", exc)