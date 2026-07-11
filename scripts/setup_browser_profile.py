"""
scripts/setup_browser_profile.py
─────────────────────────────────────────────────────────────────
One-time interactive script to sign in to Google (and YouTube)
using the same persistent Chromium profile that JARVIS uses at
runtime.

Run this ONCE before starting JARVIS, or whenever your Google
session expires:

    python scripts/setup_browser_profile.py

What it does
────────────
1. Launches a VISIBLE (non-headless) Chromium window using
   exactly the same profile directory and settings that
   browser_tools.py uses at runtime.
2. Navigates to accounts.google.com so you can sign in manually.
3. Waits until you press Enter in the terminal (giving you as
   much time as you need to complete 2FA, captchas, etc.).
4. Saves a CONSENT cookie for google.com and youtube.com so the
   "Before you continue" consent wall is suppressed on first run.
5. Exits — leaving the profile with your session cookies baked in.

After this, every time JARVIS starts browser_tools, it picks up
the same profile with your Google session already active.

Environment overrides (same as browser_tools.py):
    JARVIS_BROWSER_PROFILE_DIR  — custom profile path (default:
                                   datastore/browser_profile)
    JARVIS_BROWSER_HEADLESS     — set to "1" to skip this script
                                   (headless can't do GUI sign-in)

Usage examples
──────────────
# Default profile location (datastore/browser_profile):
python scripts/setup_browser_profile.py

# Custom location:
JARVIS_BROWSER_PROFILE_DIR=/home/bikash/.jarvis_chrome_profile \\
    python scripts/setup_browser_profile.py

# Sign in to a specific additional account (e.g. work Gmail):
python scripts/setup_browser_profile.py --url https://mail.google.com
"""

from __future__ import annotations

import argparse
import asyncio
import logging
import os
import sys
from pathlib import Path

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%H:%M:%S",
)
log = logging.getLogger("setup_browser_profile")


# ── Resolve the same profile dir that browser_tools.py uses ──────────────────

def _get_profile_dir() -> Path:
    raw = os.environ.get(
        "JARVIS_BROWSER_PROFILE_DIR",
        str(Path("datastore") / "browser_profile"),
    )
    return Path(raw).expanduser().resolve()


# ── The same stealth init script as PlaywrightEngine ──────────────────────────

_STEALTH_INIT = """
Object.defineProperty(navigator, 'webdriver', {get: () => undefined});
window.chrome = { runtime: {} };
Object.defineProperty(navigator, 'plugins', {get: () => [1, 2, 3, 4, 5]});
Object.defineProperty(navigator, 'languages', {get: () => ['en-US', 'en']});
"""

_DEFAULT_UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/124.0.0.0 Safari/537.36"
)


# ── Main ──────────────────────────────────────────────────────────────────────

async def _run(start_url: str, extra_urls: list[str]) -> None:
    try:
        from playwright.async_api import async_playwright
    except ImportError:
        log.error(
            "Playwright is not installed.\n"
            "Fix: pip install playwright && playwright install chromium"
        )
        sys.exit(1)

    profile_dir = _get_profile_dir()
    profile_dir.mkdir(parents=True, exist_ok=True)
    log.info("Using profile directory: %s", profile_dir)

    launch_args = [
        "--disable-blink-features=AutomationControlled",
        "--disable-features=IsolateOrigins,site-per-process",
        "--no-first-run",
        "--no-default-browser-check",
    ]

    async with async_playwright() as p:
        log.info("Launching Chromium (headless=False) …")
        context = await p.chromium.launch_persistent_context(
            str(profile_dir),
            headless=False,          # MUST be visible for manual sign-in
            slow_mo=50,
            viewport={"width": 1280, "height": 800},
            user_agent=_DEFAULT_UA,
            locale="en-US",
            timezone_id="Asia/Kathmandu",
            args=launch_args,
        )

        # Apply stealth overrides
        await context.add_init_script(_STEALTH_INIT)

        # Pre-seed consent cookies (same as PlaywrightEngine.start())
        try:
            await context.add_cookies([
                {"name": "CONSENT", "value": "YES+1", "domain": ".youtube.com", "path": "/"},
                {"name": "CONSENT", "value": "YES+1", "domain": ".google.com",  "path": "/"},
            ])
            log.info("CONSENT cookies pre-seeded for google.com + youtube.com")
        except Exception as exc:
            log.warning("Could not pre-seed CONSENT cookies: %s", exc)

        # Open the sign-in page
        page = context.pages[0] if context.pages else await context.new_page()
        log.info("Navigating to: %s", start_url)
        await page.goto(start_url, wait_until="domcontentloaded", timeout=30_000)

        # Open any extra URLs in new tabs
        for url in extra_urls:
            tab = await context.new_page()
            log.info("Opening extra tab: %s", url)
            await tab.goto(url, wait_until="domcontentloaded", timeout=30_000)

        # ── Interactive pause ─────────────────────────────────────────────────
        print()
        print("=" * 60)
        print("  Chromium is open. Please:")
        print("  1. Sign in to your Google account in the browser.")
        print("  2. Complete any 2FA / captcha prompts.")
        print("  3. Navigate to YouTube and confirm you are signed in.")
        print("  4. Come back here and press Enter when done.")
        print()
        print("  The window will close and your session will be saved")
        print(f"  to: {profile_dir}")
        print("=" * 60)
        print()

        try:
            input("  Press Enter when you have finished signing in …")
        except EOFError:
            # Non-interactive environment — just wait 5 minutes
            log.warning(
                "Non-interactive terminal detected. Waiting 300s for sign-in."
            )
            await asyncio.sleep(300)

        # Save an explicit SOCS cookie that newer Google UI uses instead of
        # CONSENT (belt-and-suspenders — suppresses any remaining consent wall)
        try:
            await context.add_cookies([
                {"name": "SOCS",    "value": "CAESEwgDEgk...",
                 "domain": ".google.com",  "path": "/"},
                {"name": "SOCS",    "value": "CAESEwgDEgk...",
                 "domain": ".youtube.com", "path": "/"},
            ])
        except Exception:
            pass  # non-fatal

        log.info("Closing browser and saving session …")
        await context.close()

    print()
    print("Done! Profile saved to:")
    print(f"  {profile_dir}")
    print()
    print("JARVIS will now use this profile when browser_tools start.")
    print("Run this script again if your session expires.")


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Sign in to Google using JARVIS's Playwright profile."
    )
    parser.add_argument(
        "--url",
        default="https://accounts.google.com",
        help="Starting URL (default: https://accounts.google.com)",
    )
    parser.add_argument(
        "--also",
        metavar="URL",
        nargs="*",
        default=[],
        help="Additional URLs to open in new tabs (e.g. https://youtube.com)",
    )
    args = parser.parse_args()

    try:
        asyncio.run(_run(start_url=args.url, extra_urls=args.also))
    except KeyboardInterrupt:
        print("\nInterrupted. Profile state up to this point has been saved.")
        sys.exit(0)


if __name__ == "__main__":
    main()
