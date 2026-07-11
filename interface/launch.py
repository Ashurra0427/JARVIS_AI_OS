#!/usr/bin/env python3
"""
interface/launch.py
────────────────────
JARVIS AI OS — Desktop Interface Entry Point (PySide6)

Usage (kernel mode — NO server.py required):
    python interface/launch.py --kernel

Usage (WebSocket mode — server.py must be running):
    python interface/launch.py
    python interface/launch.py --url ws://localhost:7788/ws

Requires:
    pip install PySide6
    pip install websocket-client   # only needed for WebSocket mode

P4-E: --kernel flag starts the full Bootstrap in-process and connects
the Qt HUD directly to the Kernel EventBus via ServerAdapter.from_kernel().
No server.py required in this mode.
"""
from __future__ import annotations

import argparse
import asyncio
import logging
import os
import sys
import threading
import traceback
from datetime import datetime

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [UI] %(levelname)-8s  %(name)s — %(message)s",
)

log = logging.getLogger(__name__)

# ── P13: global exception hook ───────────────────────────────────────────
# PySide6 routes exceptions raised inside Qt slots through sys.excepthook.
# Without overriding it, the default hook just prints a traceback and — on
# recent PySide6/Qt builds — aborts the whole process, so a single bad
# signal handler (a None the code didn't expect, a KeyError from a stale
# panel reference, etc.) takes the entire desktop app down mid-session.
# This installs a hook that logs the crash to a file the user can attach
# to a bug report, and — if a window is already up — shows a toast instead
# of dying, so one bad event doesn't nuke an otherwise-working session.
_CRASH_LOG_DIR = os.path.join(os.path.expanduser("~"), ".jarvis", "crash_logs")


def _install_global_excepthook() -> None:
    def _hook(exc_type, exc_value, exc_tb) -> None:
        if issubclass(exc_type, KeyboardInterrupt):
            sys.__excepthook__(exc_type, exc_value, exc_tb)
            return
        tb_text = "".join(traceback.format_exception(exc_type, exc_value, exc_tb))
        log.critical("UNHANDLED EXCEPTION (caught by global hook):\n%s", tb_text)
        try:
            os.makedirs(_CRASH_LOG_DIR, exist_ok=True)
            fname = f"crash_{datetime.now():%Y%m%d_%H%M%S}.log"
            with open(os.path.join(_CRASH_LOG_DIR, fname), "w") as f:
                f.write(tb_text)
        except Exception:
            log.exception("Failed to write crash log")
        # Try to surface this in the UI instead of silently dying. If the
        # app/window isn't up yet (crash during boot), this is a no-op and
        # the process falls through to whatever Qt does next.
        try:
            from PySide6.QtWidgets import QApplication
            app = QApplication.instance()
            if app is not None:
                for w in app.topLevelWidgets():
                    if hasattr(w, "_toasts"):
                        w._toasts.show_toast(
                            "Internal Error",
                            f"{exc_type.__name__}: {str(exc_value)[:80]} "
                            f"— logged to {_CRASH_LOG_DIR}",
                            "ERROR",
                        )
                        break
        except Exception:
            pass  # never let the crash handler itself crash the app

    sys.excepthook = _hook

from PySide6.QtWidgets import QApplication
from PySide6.QtCore import Qt
from PySide6.QtGui import QIcon

from interface.hud.main_window import JarvisWindow


def _apply_dark_palette(app: QApplication) -> None:
    from PySide6.QtGui import QPalette, QColor
    palette = QPalette()
    palette.setColor(QPalette.ColorRole.Window,          QColor("#050d1a"))
    palette.setColor(QPalette.ColorRole.WindowText,      QColor("#d8eeff"))
    palette.setColor(QPalette.ColorRole.Base,            QColor("#060f1e"))
    palette.setColor(QPalette.ColorRole.AlternateBase,   QColor("#091525"))
    palette.setColor(QPalette.ColorRole.Text,            QColor("#d8eeff"))
    palette.setColor(QPalette.ColorRole.Button,          QColor("#091525"))
    palette.setColor(QPalette.ColorRole.ButtonText,      QColor("#d8eeff"))
    palette.setColor(QPalette.ColorRole.Highlight,       QColor("#00c8ff"))
    palette.setColor(QPalette.ColorRole.HighlightedText, QColor("#000000"))
    app.setPalette(palette)


def _start_kernel_in_thread() -> "Orchestrator":
    """
    Boot the JARVIS kernel in a background asyncio thread.
    Returns the live Orchestrator once all bootstrap phases complete.
    P4-E fix: allows the Qt main thread and the kernel event loop to coexist.
    """
    import concurrent.futures
    from pathlib import Path

    ready_event = threading.Event()
    orchestrator_box: list = []
    error_box: list = []

    def _kernel_thread() -> None:
        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)

        async def _boot() -> None:
            try:
                from boot.bootstrap import Bootstrap
                bs = Bootstrap()
                await bs.start()
                orch = bs._container.try_resolve("orchestrator")
                orchestrator_box.append(orch)
                log.info("Kernel bootstrap complete — HUD connecting")
            except Exception as exc:
                log.error("Kernel bootstrap failed: %s", exc, exc_info=True)
                error_box.append(exc)
            finally:
                ready_event.set()
            # Keep the kernel loop alive
            while True:
                await asyncio.sleep(3600)

        loop.run_until_complete(_boot())

    t = threading.Thread(target=_kernel_thread, daemon=True, name="jarvis-kernel")
    t.start()

    log.info("Waiting for kernel bootstrap to complete…")
    ready_event.wait(timeout=120)

    if error_box:
        raise RuntimeError(f"Kernel failed to start: {error_box[0]}") from error_box[0]
    if not orchestrator_box:
        raise RuntimeError("Kernel started but Orchestrator not resolved — check bootstrap logs")

    return orchestrator_box[0]


def main() -> None:
    parser = argparse.ArgumentParser(description="JARVIS AI OS Desktop UI")
    parser.add_argument(
        "--kernel",
        action="store_true",
        default=False,
        help="Start in kernel mode: boot JARVIS in-process, no server.py needed (P4-E)",
    )
    parser.add_argument(
        "--url",
        default="ws://localhost:7788/ws",
        help="WebSocket URL of server.py — only used without --kernel (default: ws://localhost:7788/ws)",
    )
    parser.add_argument(
        "--scale",
        type=float,
        default=1.0,
        help="HiDPI scale factor (default: 1.0)",
    )
    args = parser.parse_args()

    _install_global_excepthook()

    # ── Qt application ────────────────────────────────────────────────
    app = QApplication(sys.argv)
    app.setApplicationName("JARVIS AI OS")
    app.setOrganizationName("Stark Industries")
    app.setApplicationVersion("2.4.0")
    app.setHighDpiScaleFactorRoundingPolicy(
        Qt.HighDpiScaleFactorRoundingPolicy.PassThrough
    )
    _apply_dark_palette(app)

    # ── Backend setup ─────────────────────────────────────────────────
    server_adapter = None

    if args.kernel:
        log.info("Kernel mode selected — booting JARVIS in-process (P4-E)")
        try:
            orchestrator = _start_kernel_in_thread()
            from interface.adapters.ws_client import ServerAdapter
            server_adapter = ServerAdapter.from_kernel(orchestrator)
            log.info("Kernel mode: ServerAdapter wired to in-process EventBus")
        except Exception as exc:
            log.error("Kernel boot failed — falling back to WebSocket mode: %s", exc)
            server_adapter = None
    else:
        log.info("WebSocket mode — connecting to %s", args.url)

    # ── Main window ───────────────────────────────────────────────────
    window = JarvisWindow(
        server_url=args.url,
        server_adapter=server_adapter,
    )
    window.show()

    sys.exit(app.exec())


if __name__ == "__main__":
    main()