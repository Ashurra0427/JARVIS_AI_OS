"""
perception/observation/observer.py
────────────────────────────────────
Master perception coordinator for JARVIS_AI_OS.

Orchestrates the full perception pipeline:
  ScreenshotService → ScreenshotAnalyser → ContextClassifier
  ActivityObserver  → ContextClassifier
  OCR pipeline      → ContextClassifier

Publishes unified PerceptionFrame events consumed by the cognition layer.

Architecture
────────────
  Physical world (screen, input, audio)
          ↓
    Observer.start()          ← manages all sub-observers
          ↓
    PerceptionFrame           ← unified snapshot
          ↓
    EventBus → "perception.frame.ready"
          ↓
    ReasoningEngine / DecisionEngine

Design
──────
- Single entry-point to start/stop all perception subsystems
- Produces a typed PerceptionFrame every poll cycle
- Tolerates unavailable subsystems (partial frames still published)
- Configurable poll interval and module enable flags
- Thread-safe async lifecycle
"""

from __future__ import annotations

import asyncio
import logging
import time
import uuid
from dataclasses import dataclass, field
from typing import Any

logger = logging.getLogger(__name__)


# ──────────────────────────────────────────────
# Perception frame
# ──────────────────────────────────────────────


@dataclass
class PerceptionFrame:
    """
    Unified snapshot produced each observation cycle.

    Consumed by the cognition layer as the primary environmental input.
    """

    frame_id: str
    timestamp: float
    active_window: dict[str, Any] | None = None
    activity_state: str = "unknown"
    idle_seconds: float = 0.0
    context_label: str = "unknown"
    context_confidence: float = 0.0
    screen_summary: str = ""
    visible_text: str = ""
    ui_context: str = ""
    top_processes: list[dict[str, Any]] = field(default_factory=list)
    active_app: str = ""
    sub_context: str = ""
    metadata: dict[str, Any] = field(default_factory=dict)

    def as_dict(self) -> dict[str, Any]:
        return {
            "frame_id": self.frame_id,
            "timestamp": self.timestamp,
            "active_window": self.active_window,
            "activity_state": self.activity_state,
            "idle_seconds": self.idle_seconds,
            "context_label": self.context_label,
            "context_confidence": self.context_confidence,
            "screen_summary": self.screen_summary,
            "visible_text": self.visible_text,
            "ui_context": self.ui_context,
            "top_processes": self.top_processes,
            "active_app": self.active_app,
            "sub_context": self.sub_context,
            "metadata": self.metadata,
        }


# ──────────────────────────────────────────────
# Observer configuration
# ──────────────────────────────────────────────


@dataclass
class ObserverConfig:
    """Runtime configuration for the master Observer."""

    poll_interval_s: float = 5.0
    enable_activity: bool = True
    enable_screenshot: bool = True
    enable_context_classify: bool = True
    enable_ocr: bool = False  # OCR is expensive; off by default
    idle_threshold_s: float = 60.0
    track_processes: bool = True
    screenshot_interval_s: float = 10.0  # screenshot less frequently than activity


# ──────────────────────────────────────────────
# Master Observer
# ──────────────────────────────────────────────


class Observer:
    """
    Master perception coordinator.

    Manages ActivityObserver, ContextClassifier, and optionally
    screenshot/OCR pipelines. Aggregates their outputs into
    PerceptionFrames published on the event bus.

    Usage
    ─────
    observer = Observer(event_bus=bus, config=ObserverConfig())
    await observer.start()

    # Frames arrive via event bus: "perception.frame.ready"
    # Or poll directly:
    frame = await observer.get_latest_frame()

    await observer.stop()
    """

    EVT_FRAME_READY = "perception.frame.ready"
    EVT_OBSERVER_UP = "perception.observer.started"
    EVT_OBSERVER_DOWN = "perception.observer.stopped"

    def __init__(
        self,
        event_bus: Any | None = None,
        config: ObserverConfig | None = None,
        activity_observer: Any | None = None,
        context_classifier: Any | None = None,
        screenshot_service: Any | None = None,
        model_router: Any | None = None,
    ) -> None:
        self._bus = event_bus
        self._config = config or ObserverConfig()
        self._model_router = model_router

        # Sub-observer injection (allows DI) or lazy construction
        self._activity_observer = activity_observer
        self._context_classifier = context_classifier
        self._screenshot_service = screenshot_service

        self._running = False
        self._task: asyncio.Task | None = None
        self._ss_task: asyncio.Task | None = None

        self._latest_frame: PerceptionFrame | None = None
        self._frame_count = 0

        # Screenshot cache (updated on slower interval)
        self._last_screen_state: Any | None = None
        self._last_ss_time: float = 0.0

    # ═══════════════════════════════════════════
    # Lifecycle
    # ═══════════════════════════════════════════

    async def start(self) -> None:
        """Start all perception subsystems and the main observation loop."""
        if self._running:
            logger.warning("Observer.start() called but already running.")
            return

        self._running = True

        # Initialise sub-observers that weren't injected
        await self._init_subsystems()

        # Start sub-observers
        if self._activity_observer and self._config.enable_activity:
            try:
                await self._activity_observer.start()
            except Exception as exc:
                logger.warning("ActivityObserver start failed: %s", exc)

        if self._context_classifier and self._config.enable_context_classify:
            try:
                await self._context_classifier.start()
            except Exception as exc:
                logger.warning("ContextClassifier start failed: %s", exc)

        # Main poll loop
        self._task = asyncio.create_task(self._poll_loop(), name="observer-poll")

        # Screenshot loop (separate, slower interval)
        if self._screenshot_service and self._config.enable_screenshot:
            self._ss_task = asyncio.create_task(
                self._screenshot_loop(), name="observer-screenshot"
            )

        await self._emit(
            self.EVT_OBSERVER_UP,
            {
                "config": {
                    "poll_interval_s": self._config.poll_interval_s,
                    "enable_activity": self._config.enable_activity,
                    "enable_screenshot": self._config.enable_screenshot,
                }
            },
        )

        logger.info(
            "Observer started (poll=%.1fs, activity=%s, screenshot=%s).",
            self._config.poll_interval_s,
            self._config.enable_activity,
            self._config.enable_screenshot,
        )

    async def stop(self) -> None:
        """Stop all subsystems gracefully."""
        if not self._running:
            return

        self._running = False

        for task in (self._task, self._ss_task):
            if task:
                task.cancel()
                try:
                    await task
                except asyncio.CancelledError:
                    pass

        if self._activity_observer:
            try:
                await self._activity_observer.stop()
            except Exception:
                pass

        if self._context_classifier:
            try:
                await self._context_classifier.stop()
            except Exception:
                pass

        await self._emit(self.EVT_OBSERVER_DOWN, {})
        logger.info("Observer stopped after %d frames.", self._frame_count)

    # ═══════════════════════════════════════════
    # Public API
    # ═══════════════════════════════════════════

    async def get_latest_frame(self) -> PerceptionFrame | None:
        """Return the most recently built PerceptionFrame."""
        return self._latest_frame

    async def force_observation(self) -> PerceptionFrame:
        """Trigger an immediate observation cycle and return the frame."""
        return await self._build_frame()

    def is_running(self) -> bool:
        return self._running

    @property
    def frame_count(self) -> int:
        return self._frame_count

    # ═══════════════════════════════════════════
    # Internal loops
    # ═══════════════════════════════════════════

    async def _poll_loop(self) -> None:
        """Main observation poll loop."""
        while self._running:
            try:
                frame = await self._build_frame()
                self._latest_frame = frame
                self._frame_count += 1
                await self._emit(self.EVT_FRAME_READY, frame.as_dict())
                logger.debug(
                    "Observer: frame #%d — context=%s (%.2f)",
                    self._frame_count,
                    frame.context_label,
                    frame.context_confidence,
                )
            except Exception as exc:
                logger.error("Observer poll error: %s", exc)

            await asyncio.sleep(self._config.poll_interval_s)

    async def _screenshot_loop(self) -> None:
        """Slower screenshot capture loop."""
        while self._running:
            try:
                await self._capture_screenshot()
            except Exception as exc:
                logger.debug("Screenshot capture error: %s", exc)
            await asyncio.sleep(self._config.screenshot_interval_s)

    # ═══════════════════════════════════════════
    # Frame construction
    # ═══════════════════════════════════════════

    async def _build_frame(self) -> PerceptionFrame:
        """Aggregate all available perception signals into a PerceptionFrame."""
        frame = PerceptionFrame(
            frame_id=str(uuid.uuid4()),
            timestamp=time.time(),
        )

        # Activity signals
        if self._activity_observer and self._config.enable_activity:
            await self._enrich_activity(frame)

        # Screen state signals
        if self._last_screen_state is not None:
            await self._enrich_screen(frame, self._last_screen_state)

        # Context classification
        if self._context_classifier and self._config.enable_context_classify:
            await self._enrich_context(frame)

        return frame

    async def _enrich_activity(self, frame: PerceptionFrame) -> None:
        """Populate frame with activity observer data."""
        try:
            snapshot = await self._activity_observer.take_snapshot()
            if snapshot:
                frame.activity_state = (
                    snapshot.state.value
                    if hasattr(snapshot.state, "value")
                    else str(snapshot.state)
                )
                frame.idle_seconds = snapshot.idle_seconds
                if snapshot.active_window:
                    frame.active_window = (
                        snapshot.active_window.as_dict()
                        if hasattr(snapshot.active_window, "as_dict")
                        else vars(snapshot.active_window)
                    )
                    frame.active_app = (
                        snapshot.active_window.app_name
                        if hasattr(snapshot.active_window, "app_name")
                        else ""
                    )
                frame.top_processes = [
                    p.as_dict() if hasattr(p, "as_dict") else vars(p)
                    for p in snapshot.top_processes
                ]
        except Exception as exc:
            logger.debug("Activity enrichment failed: %s", exc)

    async def _enrich_screen(self, frame: PerceptionFrame, screen_state: Any) -> None:
        """Populate frame from a cached screen state object."""
        try:
            frame.screen_summary = getattr(screen_state, "summary", "")
            frame.visible_text = getattr(screen_state, "visible_text", "")[:1000]
            frame.ui_context = getattr(screen_state, "ui_context", "")
            if not frame.active_app:
                frame.active_app = getattr(screen_state, "active_app", "")
        except Exception as exc:
            logger.debug("Screen enrichment failed: %s", exc)

    async def _enrich_context(self, frame: PerceptionFrame) -> None:
        """Populate context classification fields from ContextClassifier."""
        try:
            ctx_snap = await self._context_classifier.get_current_context()
            if ctx_snap and ctx_snap.classification:
                cl = ctx_snap.classification
                frame.context_label = (
                    cl.context.value
                    if hasattr(cl.context, "value")
                    else str(cl.context)
                )
                frame.context_confidence = cl.confidence
                frame.sub_context = cl.sub_context
        except Exception as exc:
            logger.debug("Context enrichment failed: %s", exc)

    async def _capture_screenshot(self) -> None:
        """Capture and analyse screenshot, updating cached screen state."""
        if not self._screenshot_service:
            return
        try:
            # Support both take_screenshot() → path and analyse() flows
            if hasattr(self._screenshot_service, "capture_and_analyse"):
                state = await self._screenshot_service.capture_and_analyse()
            elif hasattr(self._screenshot_service, "take_screenshot"):
                path = await self._screenshot_service.take_screenshot()
                state = path  # may be just a path; enrichment handles both
            else:
                return

            self._last_screen_state = state
            self._last_ss_time = time.time()

            # Feed into context classifier directly
            if (
                self._context_classifier
                and hasattr(self._context_classifier, "classify_from_screen_state")
                and hasattr(state, "visible_text")
            ):
                await self._context_classifier.classify_from_screen_state(state)

        except Exception as exc:
            logger.debug("Screenshot pipeline error: %s", exc)

    # ═══════════════════════════════════════════
    # Subsystem initialisation
    # ═══════════════════════════════════════════

    async def _init_subsystems(self) -> None:
        """Lazily construct sub-observers if not injected."""
        if self._activity_observer is None and self._config.enable_activity:
            try:
                from perception.observation.activity_observer import ActivityObserver

                self._activity_observer = ActivityObserver(
                    event_bus=self._bus,
                    poll_interval=self._config.poll_interval_s,
                    idle_threshold=self._config.idle_threshold_s,
                    track_processes=self._config.track_processes,
                )
            except Exception as exc:
                logger.warning("Cannot initialise ActivityObserver: %s", exc)

        if self._context_classifier is None and self._config.enable_context_classify:
            try:
                from perception.observation.context_classifier import ContextClassifier

                self._context_classifier = ContextClassifier(
                    event_bus=self._bus,
                    model_router=self._model_router,
                )
            except Exception as exc:
                logger.warning("Cannot initialise ContextClassifier: %s", exc)

    # ═══════════════════════════════════════════
    # Event helper
    # ═══════════════════════════════════════════

    async def _emit(self, event_type: str, payload: dict) -> None:
        if not self._bus:
            return
        try:
            from kernel.event_bus.event_bus import Event

            event = Event(
                event_type=event_type,
                source="observer",
                payload=payload,
            )
            if asyncio.iscoroutinefunction(self._bus.publish):
                await self._bus.publish(event)
            else:
                self._bus.publish_sync(event)
        except Exception as exc:
            logger.debug("Observer emit failed: %s", exc)
