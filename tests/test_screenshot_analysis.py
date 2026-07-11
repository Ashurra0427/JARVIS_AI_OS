"""
tests/test_screenshot_analysis.py
==================================
Headless unit tests for perception/vision/screenshot_analysis.py.

These deliberately use fake VisionPipeline + OCR inputs so they run in CI
without a display or a real screen capture (mirroring the guarding style of
the requires_display marker used elsewhere in tests/, but NOT marked
requires_display so they still execute headless in CI). The ScreenshotAnalyser
is on-demand: analyse_current_screen() pulls frames from its injected
vision_pipeline, so we never need mss / a monitor.
"""

from __future__ import annotations

import pytest

from kernel.event_bus.event_bus import Event
from perception.ocr.ocr_pipeline import OCRResult
from perception.vision.screenshot_analysis import (
    ScreenshotAnalyser,
    ScreenChange,
    ScreenState,
    UIContext,
)
from perception.vision.vision_pipeline import (
    BoundingBox,
    DetectedElement,
    VisionFrame,
)


# ── Fakes ────────────────────────────────────────────────────────────────────

class FakeEventBus:
    """Collects published events so tests can assert on them."""

    def __init__(self) -> None:
        self.events: list[Event] = []

    async def publish(self, event: Event) -> None:
        self.events.append(event)


class FakeVision:
    """Returns a scripted sequence of VisionFrames from capture_frame()."""

    def __init__(self, frames: list[VisionFrame]) -> None:
        self._frames = list(frames)
        self._i = 0

    async def capture_frame(self, region=None, analyze: bool = True) -> VisionFrame:
        f = self._frames[self._i % len(self._frames)]
        self._i += 1
        return f


class FakeOCR:
    """Returns a scripted sequence of OCRResults from extract_from_image_b64()."""

    def __init__(self, results: list[OCRResult]) -> None:
        self._results = list(results)
        self._i = 0

    async def extract_from_image_b64(self, image_b64: str, source_id=None) -> OCRResult:
        r = self._results[self._i % len(self._results)]
        self._i += 1
        return r


def _frame(description: str = "", active_window: str = "", elements=None,
           metadata=None, frame_id: str = "frame_x") -> VisionFrame:
    return VisionFrame(
        frame_id=frame_id,
        timestamp=0.0,
        width=1920,
        height=1080,
        image_b64="iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAAC0lEQVR42mNk+M9QDwADhgGAWjR9awAAAABJRU5ErkJggg==",
        elements=elements or [],
        description=description,
        active_window=active_window,
        metadata=metadata or {},
    )


def _ocr(full_text: str) -> OCRResult:
    return OCRResult(
        source_id="frame_x",
        timestamp=0.0,
        full_text=full_text,
        blocks=[],
        language="en",
        backend="fake",
    )


def _make_analyser(frames, ocr_texts, bus=None):
    bus = bus or FakeEventBus()
    vision = FakeVision(frames)
    ocr = FakeOCR([_ocr(t) for t in ocr_texts])
    return ScreenshotAnalyser(
        event_bus=bus,
        vision_pipeline=vision,
        ocr_pipeline=ocr,
        model_router=None,  # force heuristic classification (headless, no model)
    ), bus, vision, ocr


# ── Tests ────────────────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_screenshot_state_construction():
    """analyse_current_screen builds a ScreenState and emits analysed event."""
    analyser, bus, _, _ = _make_analyser(
        [_frame(description="browser window", active_window="Edge")],
        ["https://example.com address bar"],
    )

    state = await analyser.analyse_current_screen()

    assert isinstance(state, ScreenState)
    assert state.frame is not None
    assert state.ocr_result is not None
    assert state.visible_text == "https://example.com address bar"
    assert state.active_app == "Edge"
    assert state.content_hash  # md5 of visible text
    assert state.summary == "browser window"  # description preferred over text
    # First frame has no previous to diff against -> no changes.
    assert state.changes_from_previous == []
    assert bus.events  # at least the analysed event was published
    assert bus.events[-1].event_type == ScreenshotAnalyser.EVT_ANALYSED
    assert bus.events[-1].payload["state_id"] == state.state_id


@pytest.mark.asyncio
async def test_ui_context_classification_browser():
    analyser, _, _, _ = _make_analyser(
        [_frame(description="web page")],
        ["https://github.com address bar back forward"],
    )
    state = await analyser.analyse_current_screen()
    assert state.ui_context == UIContext.BROWSER


@pytest.mark.asyncio
async def test_ui_context_classification_terminal():
    analyser, _, _, _ = _make_analyser(
        [_frame(description="shell")],
        ["user@host:~$ bash ls -la"],
    )
    state = await analyser.analyse_current_screen()
    assert state.ui_context == UIContext.TERMINAL


@pytest.mark.asyncio
async def test_ui_context_classification_ide():
    analyser, _, _, _ = _make_analyser(
        [_frame(description="editor")],
        ["def foo():\n    return 42\nimport os"],
    )
    state = await analyser.analyse_current_screen()
    assert state.ui_context == UIContext.IDE


@pytest.mark.asyncio
async def test_ui_context_classification_document():
    analyser, _, _, _ = _make_analyser(
        [_frame(description="reader")],
        ["quarterly report .docx word document"],
    )
    state = await analyser.analyse_current_screen()
    assert state.ui_context == UIContext.DOCUMENT


@pytest.mark.asyncio
async def test_ui_context_classification_unknown_when_no_signal():
    analyser, _, _, _ = _make_analyser(
        [_frame(description="")],
        ["the lamp on the desk is warm and the cat is asleep"],
    )
    state = await analyser.analyse_current_screen()
    assert state.ui_context == UIContext.UNKNOWN


@pytest.mark.asyncio
async def test_frame_diff_new_text_and_context_shift():
    """Second frame with different text + context yields change events."""
    analyser, bus, _, _ = _make_analyser(
        [
            _frame(description="web", active_window="Edge"),
            _frame(description="terminal", active_window="Windows Terminal"),
        ],
        [
            "https://example.com address bar",
            "user@host:~$ bash echo hello world",
        ],
    )

    first = await analyser.analyse_current_screen()
    assert first.changes_from_previous == []
    assert bus.events[-1].event_type == ScreenshotAnalyser.EVT_ANALYSED

    second = await analyser.analyse_current_screen()
    # Different content hash -> new_text change; different context -> layout_shift.
    types = {c.change_type for c in second.changes_from_previous}
    assert "new_text" in types
    assert "layout_shift" in types
    # The change_detected event should also have been published.
    assert any(e.event_type == ScreenshotAnalyser.EVT_CHANGE_DETECTED for e in bus.events)


@pytest.mark.asyncio
async def test_frame_diff_element_added():
    base = _frame(elements=[
        DetectedElement("button", "OK", BoundingBox(0, 0, 10, 10)),
    ])
    extra = _frame(elements=[
        DetectedElement("button", "OK", BoundingBox(0, 0, 10, 10)),
        DetectedElement("link", "Docs", BoundingBox(20, 20, 10, 10)),
    ])
    analyser, _, _, _ = _make_analyser(
        [base, extra],
        ["same text", "same text"],
    )
    await analyser.analyse_current_screen()
    second = await analyser.analyse_current_screen()
    assert any(
        c.change_type == "element_added" for c in second.changes_from_previous
    )


@pytest.mark.asyncio
async def test_analyse_frame_prefers_ocr_full_text():
    analyser, _, vision, ocr = _make_analyser(
        [_frame(description="d")],
        ["ocr extracted caption"],
    )
    frame = _frame(description="d", active_window="App")
    state = await analyser.analyse_frame(frame, run_ocr=True)
    assert isinstance(state, ScreenState)
    assert state.visible_text == "ocr extracted caption"
    assert state.ocr_result is not None


@pytest.mark.asyncio
async def test_analyse_frame_without_ocr_falls_back_to_description():
    analyser, _, vision, ocr = _make_analyser(
        [_frame(description="d")],
        [""],
    )
    frame = _frame(description="fallback description", active_window="App")
    state = await analyser.analyse_frame(frame, run_ocr=False)
    assert state.visible_text == "fallback description"
    assert state.ocr_result is None


@pytest.mark.asyncio
async def test_error_event_on_capture_failure():
    """If the vision pipeline raises, an error event is published and it propagates."""
    bus = FakeEventBus()

    class BoomVision:
        async def capture_frame(self, region=None, analyze: bool = True):
            raise RuntimeError("no display")

    class NoOCR:
        async def extract_from_image_b64(self, image_b64, source_id=None):
            raise RuntimeError("nope")

    analyser = ScreenshotAnalyser(
        event_bus=bus, vision_pipeline=BoomVision(), ocr_pipeline=NoOCR(),
        model_router=None,
    )
    with pytest.raises(RuntimeError):
        await analyser.analyse_current_screen()
    assert bus.events[-1].event_type == ScreenshotAnalyser.EVT_ERROR
