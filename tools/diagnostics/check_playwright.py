"""
tools/diagnostics/check_playwright.py
────────────────────────────────────────
Quick standalone check: run this with the SAME python/venv that launches
JARVIS to verify Playwright is correctly installed and browser binaries
are present.

Usage:
    python tools/diagnostics/check_playwright.py
"""
from __future__ import annotations

import sys


def main() -> int:
    print(f"Python executable: {sys.executable}")
    print(f"Python version:    {sys.version.split()[0]}")
    print()

    try:
        import playwright
        print(f"[OK] playwright package importable (version {playwright.__version__})")
    except ImportError as exc:
        print(f"[FAIL] playwright package NOT importable: {exc}")
        print()
        print("Fix: activate the venv JARVIS uses, then run:")
        print(f"     {sys.executable} -m pip install playwright")
        return 1

    try:
        from playwright.sync_api import sync_playwright
    except ImportError as exc:
        print(f"[FAIL] playwright.sync_api not importable: {exc}")
        return 1

    print()
    print("Checking browser binaries (this launches/closes chromium briefly)...")
    try:
        with sync_playwright() as p:
            browser = p.chromium.launch(headless=True)
            version = browser.version
            browser.close()
        print(f"[OK] chromium launches successfully (version: {version})")
    except Exception as exc:
        msg = str(exc)
        print(f"[FAIL] chromium failed to launch: {msg}")
        print()
        if "Executable doesn't exist" in msg:
            print("Fix: install the browser binaries for this environment:")
            print(f"     {sys.executable} -m playwright install chromium")
        else:
            print("This may be a sandboxing/permissions issue. Try running:")
            print(f"     {sys.executable} -m playwright install --with-deps chromium")
        return 1

    print()
    print("All checks passed — Playwright is correctly set up in this environment.")
    print("If JARVIS still reports 'Playwright not installed', make sure")
    print("interface/launch.py and server.py are started with THIS SAME")
    print(f"interpreter: {sys.executable}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
