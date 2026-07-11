"""
JARVIS AI OS — Vision Agent  (FIX 7)
Capabilities: screen capture, image analysis, OCR, visual interaction.

Wired (FIX 7):
  - ocr_pipeline:      injected from orchestrator (OCRPipeline instance)
  - screenshot_service: injected from orchestrator (ScreenshotService instance)
  Subscribes to vision.analyze_request EventBus events.
  Publishes vision.analysis_result with extracted text + description.
"""

from __future__ import annotations
from typing import Any
from agents.base.base_agent import BaseAgent, AgentCapability
from agents.metrics_publisher import MetricsPublisherMixin
from memory.working.context import WorkingMemoryTag


class VisionAgent(MetricsPublisherMixin, BaseAgent):
    def __init__(
        self,
        memory_router,
        event_bus,
        model_router=None,
        registry=None,
        tool_registry=None,      # FIX 5-C
        ocr_pipeline=None,       # FIX 7
        screenshot_service=None, # FIX 7
        embedding_service=None,  # Phase 8.4: align with orchestrator common dict
    ):
        super().__init__("vision", memory_router, event_bus, model_router, registry, tool_registry, embedding_service=embedding_service)
        self._ocr = ocr_pipeline
        self._screenshot = screenshot_service
        self._screens_captured: int = 0
        self._texts_extracted: int = 0
        self._current_task_desc: str = ""

    def capabilities(self) -> list[AgentCapability]:
        return [
            AgentCapability(
                "screenshot",
                "Capture and analyse screenshots",
                ["screenshot", "screen", "capture"],
            ),
            AgentCapability(
                "ocr", "Extract text from images", ["ocr", "text_extraction"]
            ),
            AgentCapability(
                "image", "Analyse and describe images", ["image", "visual", "describe"]
            ),
        ]

    def _metrics_payload(self) -> dict:
        return {
            "screens_captured": self._screens_captured,
            "texts_extracted":  self._texts_extracted,
        }

    async def _on_start(self) -> None:
        self._subscribe(f"agent.request.{self.name}", self._on_request)
        self._subscribe("vision.screenshot_requested", self._on_screenshot_request)
        # FIX 7: subscribe to vision.analyze_request
        self._subscribe("vision.analyze_request", self._on_analyze_request)
        self._start_metrics_loop()  # Phase 8.4: publish live metrics

    async def _on_request(self, event) -> None:
        await self._run_goal("", event.payload.get("data", {}))

    async def _on_screenshot_request(self, event) -> None:
        await self._run_goal(
            "",
            {"description": "Capture and analyse screenshot", "context": event.payload},
        )

    async def _on_analyze_request(self, event) -> None:
        """FIX 7: Handle vision.analyze_request events."""
        request_id = event.payload.get("request_id", "")
        image_path = event.payload.get("image_path", "")
        region = event.payload.get("region", None)  # {x, y, w, h} or None for fullscreen
        goal_text = event.payload.get("description", "Analyse and describe what you see")

        ocr_text = ""
        screenshot_b64 = ""

        # Step 1: take screenshot if no image_path given
        if not image_path and self._screenshot:
            try:
                from perception.vision.screenshot_service import CaptureRequest
                if region:
                    payload = await self._screenshot.capture_region(
                        x=region["x"], y=region["y"],
                        width=region["w"], height=region["h"]
                    )
                else:
                    payload = await self._screenshot.capture_fullscreen()
                if payload:
                    screenshot_b64 = payload.image_b64 or ""
                    image_path = payload.file_path or ""
            except Exception as exc:
                self._log.warning("Screenshot failed", error=str(exc))

        # Step 2: OCR
        if (image_path or screenshot_b64) and self._ocr:
            try:
                if screenshot_b64:
                    result = await self._ocr.extract_from_image_b64(screenshot_b64)
                elif image_path:
                    result = await self._ocr.extract_from_file(image_path)
                else:
                    result = None
                if result and result.full_text:
                    ocr_text = result.full_text
            except Exception as exc:
                self._log.warning("OCR failed", error=str(exc))

        # Step 3: Model description
        prompt = f"Vision task: {goal_text}"
        if ocr_text:
            prompt += f"\n\nText visible on screen (via OCR):\n{ocr_text[:800]}"
        description = await self.complete(
            prompt,
            system="You are a computer vision specialist. Describe what you observe precisely and concisely.",
            task_type="agent_vision",
        )

        await self._emit(
            "vision.analysis_result",
            {
                "request_id": request_id,
                "description": description,
                "ocr_text": ocr_text,
                "image_path": image_path,
            },
        )
        self._log.info("vision.analyze_request handled", request_id=request_id, ocr_len=len(ocr_text))

    async def handle_goal(self, goal: Any) -> dict[str, Any]:
        description = goal.get("description", goal.get("title", ""))
        image_path = goal.get("context", {}).get("image_path", "")
        self._current_task_desc = description[:60]
        self._log.info("Vision goal", description=description[:80])

        ocr_text = ""

        # P1-C: If no image_path supplied and we have a tool registry,
        # take a screenshot via the vision.screenshot tool.
        if not image_path and self._tool_registry is not None:
            try:
                tr = await self._tool_registry.invoke("vision.screenshot")
                if tr.success and tr.value:
                    image_path = tr.value.get("path", "") if isinstance(tr.value, dict) else str(tr.value)
                    self._log.info("vision.screenshot tool captured image", path=image_path)
            except Exception as exc:
                self._log.warning("vision.screenshot tool failed", error=str(exc))

        if image_path and self._ocr:
            try:
                result = await self._ocr.extract_from_file(image_path)
                if result:
                    ocr_text = result.full_text
            except Exception as exc:
                self._log.warning("OCR failed in handle_goal", error=str(exc))

        # P1-C: Try vision.ocr_screen tool if we still have no OCR text
        if not ocr_text and self._tool_registry is not None:
            try:
                tr = await self._tool_registry.invoke("vision.ocr_screen")
                if tr.success and tr.value:
                    ocr_text = str(tr.value.get("text", tr.value))[:2000]
                    self._log.info("vision.ocr_screen tool returned text", chars=len(ocr_text))
            except Exception as exc:
                self._log.warning("vision.ocr_screen tool failed", error=str(exc))

        prompt = f"Analyse the image at: {image_path}\nTask: {description}"
        if ocr_text:
            prompt += f"\n\nOCR text found:\n{ocr_text[:600]}"
        analysis = await self.complete(
            prompt,
            system="You are a computer vision specialist. Describe what you observe precisely.",
            task_type="agent_vision",
        )

        if analysis != "[Model router not available]":
            await self.remember(
                f"Vision analysis: {analysis[:200]}",
                tag=WorkingMemoryTag.OBSERVATION,
            )
        await self._emit(
            "vision.analysis_complete", {"analysis": analysis, "image_path": image_path}
        )
        self._screens_captured += 1
        if ocr_text:
            self._texts_extracted += 1
        self._current_task_desc = ""
        return {"analysis": analysis, "image_path": image_path, "ocr_text": ocr_text}