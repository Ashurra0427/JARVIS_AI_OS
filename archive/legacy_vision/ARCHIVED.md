# perception/vision/screen_vision.py — archived (this pass)
============================================================

## What this module is

`ScreenVision` captures a desktop screenshot (via mss + Pillow) and runs OCR
(pytesseract) into a `ScreenFrame` dataclass. A small standalone capture+OCR
helper.

## Why it was archived

Superseded by the wired perception stack:
  - `perception/vision/screenshot_service.py` — the service that actually
    captures screenshots on demand (fullscreen / region) and is consumed by
    `VisionAgent` and `tools/vision_tools/vision_tools.py`.
  - `perception/vision/vision_pipeline.py` + `image_analysis.py` — capture
    plus AI-powered visual analysis (descriptions + element detection), which
    is what the running system uses for screen-aware context.
  - `perception/ocr/ocr_pipeline.py` — the multi-backend OCR pipeline
    (`extract_from_image_b64`, etc.).

A repo-wide import-graph scan confirmed `ScreenVision` is imported nowhere in
the live system. Moved here rather than deleted.

## To bring it back

1. Move `screen_vision.py` back to `perception/vision/`.
2. Prefer reusing `screenshot_service.py` + `ocr_pipeline.py` for new capture
   needs; only resurrect `ScreenVision` if a bare mss+Pillow capture path is
   explicitly required.
