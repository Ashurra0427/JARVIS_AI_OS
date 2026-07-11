# actions/browser/browser_actions.py — archived (this pass)
============================================================

## What this module is

`BrowserActions` is a low-level browser-operation executor: open_url, click,
type_text, scroll, extract_text, take_screenshot, wait_for_selector,
execute_script — all operating directly on a Playwright `Page` object.

## Why it was archived

Superseded by `actions/browser/playwright_engine.py`, which is the
fully-wired engine that actually backs the browser action layer
(`ActionCoordinator` → `BrowserManager`/PlaywrightEngine). Confirmed via a
repo-wide import-graph scan (excluding archive/, tests/, and dynamically
loaded agent classes): nothing in the live system imports `BrowserActions`
— its own header states it was "used exclusively by BrowserManager and
PlaywrightEngine", both of which now call into `playwright_engine.py`
directly. Moved here rather than deleted in case the atomic primitives are
useful again.

## To bring it back

1. Move `browser_actions.py` back to `actions/browser/`.
2. Have `playwright_engine.py` (or `BrowserManager`) import and delegate to
   `BrowserActions` for the atomic ops, or keep it as a standalone helper
   module if a thinner action surface is wanted.
