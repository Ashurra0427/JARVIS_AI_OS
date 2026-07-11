"""
JARVIS AI OS — Unified Startup Script  (P-33)
================================================
Single entry-point launcher that validates the environment, checks
dependencies, and starts whichever interface the user requests.

Usage
-----
    python start.py                   # auto-detect (HUD if display, else console)
    python start.py --mode console    # text-only console REPL
    python start.py --mode hud        # PyQt/PySide6 HUD (requires display)
    python start.py --mode server    # FastAPI backend only (for web HUD)
    python start.py --mode web        # server + open jarvisV3.html in browser
    python start.py --mode server --token 8f3c2a7d91b44e1ab6c5f9d7e8a12345  # with WS auth
    python start.py --check           # environment check only, no launch
    python start.py --no-voice        # disable mic/TTS
    python start.py --debug           # DEBUG log level
    python start.py --config-dir DIR  # override config directory

Exit codes
----------
    0   Normal exit
    1   Environment / dependency error
    2   Startup error
"""

from __future__ import annotations

import argparse
import os
import shutil
import subprocess
import sys
import textwrap
import time
from pathlib import Path

# Load .env so JARVIS_SECRET / JARVIS_PORT (and --token/--port defaults) are
# available. The Qt HUD child process inherits this environment, so it can
# append ?token=<JARVIS_SECRET> to its WebSocket URL.
try:
    from dotenv import load_dotenv
    load_dotenv()
except ImportError:
    pass

# ---------------------------------------------------------------------------
# P-32: install 3.13 compat shim before any other JARVIS import
# ---------------------------------------------------------------------------

_PROJECT_ROOT = Path(__file__).parent
sys.path.insert(0, str(_PROJECT_ROOT))

try:
    from perception.speech.compat_313 import install as _install_compat
    _install_compat()
except ImportError:
    pass  # shim not yet available — first-run before unzip?


# Windows defaults stdout/stderr to cp1252, which cannot encode the box-drawing
# glyphs/emoji used by the banner. Reconfigure to UTF-8 so printing never
# raises UnicodeEncodeError. Best-effort: fall back to errors="replace".
def _configure_streams() -> None:
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8")
        except (AttributeError, ValueError, OSError):
            try:
                stream.reconfigure(errors="replace")
            except Exception:
                pass


_configure_streams()


# ---------------------------------------------------------------------------
# ANSI colour helpers
# ---------------------------------------------------------------------------

_COLOUR = sys.stdout.isatty() and os.name != "nt"

def _c(code: str, t: str) -> str:
    return f"\033[{code}m{t}\033[0m" if _COLOUR else t

def ok(t: str)     -> str: return _c("92", f"  ✓  {t}")
def warn(t: str)   -> str: return _c("93", f"  ⚠  {t}")
def err(t: str)    -> str: return _c("91", f"  ✗  {t}")
def info(t: str)   -> str: return _c("2",  f"  ·  {t}")
def bold(t: str)   -> str: return _c("1",  t)
def cyan(t: str)   -> str: return _c("96", t)
def section(t: str) -> None: print(f"\n{bold(cyan('──'))} {bold(t)} {bold(cyan('──'))}")


def _print(*args, **kwargs): print(*args, **kwargs)


# ---------------------------------------------------------------------------
# Environment checks
# ---------------------------------------------------------------------------

class EnvCheck:
    """Collects environment validation results."""

    def __init__(self) -> None:
        self.errors:   list[str] = []
        self.warnings: list[str] = []
        self.infos:    list[str] = []

    def error(self, msg: str)   -> None: self.errors.append(msg)
    def warning(self, msg: str) -> None: self.warnings.append(msg)
    def info(self, msg: str)    -> None: self.infos.append(msg)

    @property
    def ok(self) -> bool:
        return len(self.errors) == 0


def check_python_version(env: EnvCheck) -> None:
    """Require Python 3.10+; warn on 3.13 about removed modules."""
    v = sys.version_info
    if v < (3, 10):
        env.error(f"Python 3.10+ required — found {v.major}.{v.minor}.{v.micro}")
    elif v >= (3, 13):
        env.warning(
            f"Python {v.major}.{v.minor} detected — audioop removed in 3.13. "
            "JARVIS 3.13 shim is active (perception/speech/compat_313.py)."
        )
    else:
        env.info(f"Python {v.major}.{v.minor}.{v.micro}")


def check_project_structure(env: EnvCheck) -> None:
    """Verify key project files and directories are present."""
    required_files = [
        "main.py",
        "server.py",
        "config/settings.py",
        "kernel/event_bus/event_bus.py",
        "memory/router/memory_router.py",
        "boot/dependency_container.py",
    ]
    required_dirs = [
        "config",
        "kernel",
        "memory",
        "agents",
        "perception",
        "boot",
        "models",
    ]

    root = _PROJECT_ROOT
    for f in required_files:
        path = root / f
        if not path.exists():
            env.error(f"Missing required file: {f}")
        else:
            env.info(f"Found {f}")

    for d in required_dirs:
        path = root / d
        if not path.is_dir():
            env.error(f"Missing required directory: {d}/")


def check_config(env: EnvCheck, config_dir: str = "config") -> None:
    """Validate config directory exists and contains at least one YAML file."""
    cfg_path = _PROJECT_ROOT / config_dir
    if not cfg_path.is_dir():
        env.warning(f"Config directory not found: {config_dir}/  (using defaults)")
        return

    yaml_files = list(cfg_path.glob("*.yaml"))
    if not yaml_files:
        env.warning(f"No YAML files in {config_dir}/ — using built-in defaults")
    else:
        env.info(f"Config: {len(yaml_files)} YAML files in {config_dir}/")

    env_file = _PROJECT_ROOT / ".env"
    if not env_file.exists():
        env.warning(".env not found — API keys must be in environment or OS keyring")
    else:
        env.info(".env found")


def check_critical_imports(env: EnvCheck) -> None:
    """Verify core Python packages can be imported."""
    critical = {
        "yaml":       "pyyaml",
        "structlog":  "structlog",
        "aiosqlite":  "aiosqlite",
    }
    recommended = {
        "groq":       "groq",
        "google.genai": "google-genai",
        "dotenv":     "python-dotenv",
        "chromadb":   "chromadb",
        "faster_whisper": "faster-whisper",
        "edge_tts":   "edge-tts",
    }
    optional_audio = {
        "pyaudio":    "PyAudio",
        "sounddevice": "sounddevice",
        "webrtcvad":  "webrtcvad (or webrtcvad-wheels on Windows)",
    }

    for mod, pkg in critical.items():
        try:
            __import__(mod)
            env.info(f"✓ {pkg}")
        except ImportError:
            env.error(f"Missing critical package: {pkg}  →  pip install {pkg}")

    for mod, pkg in recommended.items():
        try:
            __import__(mod.split(".")[0])
            env.info(f"✓ {pkg}")
        except ImportError:
            env.warning(f"Recommended package missing: {pkg}  →  pip install {pkg}")

    for mod, pkg in optional_audio.items():
        try:
            __import__(mod)
            env.info(f"✓ {pkg} (audio)")
        except ImportError:
            env.info(f"  {pkg} not installed — voice features may be limited")


def check_api_keys(env: EnvCheck) -> None:
    """Check for at least one LLM provider API key."""
    has_key = False

    # Try vault first (P-07), then env var
    try:
        from config.settings import _load_secret
        groq_key   = _load_secret("GROQ_API_KEY")
        gemini_key = _load_secret("GEMINI_API_KEY")
    except ImportError:
        groq_key   = os.getenv("GROQ_API_KEY")
        gemini_key = os.getenv("GEMINI_API_KEY")

    if groq_key:
        env.info("GROQ_API_KEY found")
        has_key = True
    if gemini_key:
        env.info("GEMINI_API_KEY found")
        has_key = True

    if not has_key:
        env.warning(
            "No LLM API key found (GROQ_API_KEY / GEMINI_API_KEY). "
            "Set via .env or OS keyring. Local Ollama model will be used as fallback."
        )

    # Warn about optional keys
    if not os.getenv("PICOVOICE_ACCESS_KEY"):
        env.info("PICOVOICE_ACCESS_KEY not set — Porcupine wake word disabled (energy VAD used)")


def check_display(env: EnvCheck, mode: str) -> None:
    """Warn if HUD mode is selected but no display is available."""
    if mode not in ("hud", "web", "auto"):
        return
    if sys.platform == "linux" and not os.getenv("DISPLAY") and not os.getenv("WAYLAND_DISPLAY"):
        if mode == "hud":
            env.error("HUD mode requires a display (DISPLAY or WAYLAND_DISPLAY not set)")
        else:
            env.warning("No display detected — HUD unavailable; defaulting to console mode")


def run_all_checks(mode: str, config_dir: str) -> EnvCheck:
    env = EnvCheck()
    check_python_version(env)
    check_project_structure(env)
    check_config(env, config_dir)
    check_critical_imports(env)
    check_api_keys(env)
    check_display(env, mode)
    return env


def print_check_results(env: EnvCheck) -> None:
    for msg in env.infos:
        _print(info(msg))
    for msg in env.warnings:
        _print(warn(msg))
    for msg in env.errors:
        _print(err(msg))


# ---------------------------------------------------------------------------
# Auto-detect mode
# ---------------------------------------------------------------------------

def detect_mode() -> str:
    """
    Pick the best default mode:
      - If a display is available and PySide6/PyQt6 is installed → hud
      - Otherwise → console
    """
    has_display = bool(
        os.getenv("DISPLAY") or
        os.getenv("WAYLAND_DISPLAY") or
        sys.platform == "darwin" or
        sys.platform == "win32"
    )
    if not has_display:
        return "console"

    for qt in ("PySide6", "PyQt6"):
        try:
            __import__(qt)
            return "hud"
        except ImportError:
            pass

    return "console"


# ---------------------------------------------------------------------------
# Launch functions
# ---------------------------------------------------------------------------

def launch_console(args: argparse.Namespace) -> int:
    """Start the text-mode console REPL (main.py)."""
    section("Starting Console Mode")
    _print(info("Entry point: main.py"))

    cmd = [sys.executable, str(_PROJECT_ROOT / "main.py")]
    if args.no_voice:
        cmd.append("--no-voice")
    if args.debug:
        cmd.append("--debug")
    if args.config_dir != "config":
        cmd += ["--config-dir", args.config_dir]
    if hasattr(args, "agent") and args.agent:
        cmd += ["--agent", args.agent]

    try:
        result = subprocess.run(cmd, cwd=str(_PROJECT_ROOT))
        return result.returncode
    except KeyboardInterrupt:
        return 0
    except FileNotFoundError:
        _print(err("main.py not found"))
        return 2


def launch_hud(args: argparse.Namespace) -> int:
    """Start the Qt HUD interface.

    Runs the launcher as a module (``python -m interface.launch``) so the
    project root — not the ``interface/`` directory — is what ends up on
    ``sys.path``. Running ``python interface/launch.py`` directly puts the
    ``interface`` folder on the path, which breaks ``import interface.hud...``
    inside the launcher (ModuleNotFoundError).

    The HUD connects to a running backend over WebSocket, so voice/debug
    behaviour is controlled by the backend (server.py), not the launcher.

    The Qt HUD does NOT start the backend itself, so we launch it here in a
    background thread (listening on ws://localhost:<port>/ws) before starting
    the HUD client.
    """
    section("Starting HUD Mode")

    # interface/launch.py is the only Qt HUD entry point in this repo.
    launcher = _PROJECT_ROOT / "interface" / "launch.py"
    if not launcher.exists():
        _print(err("No HUD launcher found (interface/launch.py is missing)"))
        return 2

    # Start the backend that the HUD connects to.
    start_backend_thread(args)
    time.sleep(1.5)

    _print(info(f"Entry point: {launcher.relative_to(_PROJECT_ROOT)}"))
    cmd = [sys.executable, "-m", "interface.launch"]

    try:
        result = subprocess.run(cmd, cwd=str(_PROJECT_ROOT))
        return result.returncode
    except KeyboardInterrupt:
        return 0
    except FileNotFoundError:
        _print(err(f"Launcher not found: {launcher}"))
        return 2


def start_backend_thread(args: argparse.Namespace) -> "threading.Thread":
    """Start the FastAPI backend (server.py) in a daemon thread.

    Both the Qt HUD and the web HUD connect to the backend over WebSocket, but
    neither launches it — so for ``hud``/``web``/``auto`` modes we spin the
    backend up here (non-blocking) and let the interface connect to it.
    """
    import threading

    def _run():
        try:
            launch_server(args)
        except Exception as exc:  # keep the UI thread alive if server dies
            _print(err(f"Backend server stopped: {exc}"))

    t = threading.Thread(target=_run, daemon=True)
    t.start()
    return t


def launch_server(args: argparse.Namespace) -> int:
    """Start the FastAPI backend server (server.py)."""
    section("Starting Backend Server")
    server_path = _PROJECT_ROOT / "server.py"
    if not server_path.exists():
        _print(err("server.py not found"))
        return 2

    _print(info("Entry point: server.py"))
    _print(info(f"Web HUD available at: http://localhost:{args.port}"))

    try:
        import uvicorn  # noqa: F401
    except ImportError:
        _print(err("uvicorn not installed. Run: pip install uvicorn[standard]"))
        return 1

    # Pass ngrok / port / token config via environment so server.py picks it up
    env = os.environ.copy()
    env["JARVIS_PORT"] = str(args.port)
    if args.token:
        env["JARVIS_SECRET"] = args.token
        _print(info("WebSocket auth enabled — HUD must connect with ?token=<SECRET>"))
    if getattr(args, "ngrok_url", ""):
        env["NGROK_STATIC_URL"] = args.ngrok_url
        _print(info(f"Ngrok static URL: {args.ngrok_url}"))
    elif getattr(args, "ngrok", False):
        if not env.get("NGROK_AUTHTOKEN"):
            _print(warn("--ngrok requires NGROK_AUTHTOKEN in .env or environment"))
        else:
            _print(info("Ngrok tunnel will be opened by server.py on startup"))

    cmd = [
        sys.executable, "-m", "uvicorn",
        "server:app",
        "--host", "0.0.0.0",
        "--port", str(args.port),
        "--reload" if args.debug else "--no-access-log",
    ]
    if args.debug:
        cmd += ["--log-level", "debug"]

    _print(info(f"WebSocket URL: ws://localhost:{args.port}/ws"
                + (f"?token={args.token}" if args.token else "")))

    try:
        result = subprocess.run(cmd, cwd=str(_PROJECT_ROOT), env=env)
        return result.returncode
    except KeyboardInterrupt:
        return 0


def launch_web(args: argparse.Namespace) -> int:
    """Start backend server and open the web HUD in the default browser."""
    import webbrowser

    section("Starting Web HUD Mode")

    # Start the server in a daemon thread, then open the browser
    t = start_backend_thread(args)

    # Give the server a moment to start
    time.sleep(1.5)
    url = f"http://localhost:{args.port}"
    web_hud = _PROJECT_ROOT / "webpage" / "jarvisV3.html"
    if web_hud.exists():
        url = web_hud.as_uri()
        # If a WS auth token is configured, pass it along to the HUD so it can
        # append ?token=<SECRET> to its WebSocket URL (file:// pages can't read
        # the server's /config/ws_url with an embedded token).
        if args.token:
            sep = "&" if "?" in url else "?"
            url = f"{url}{sep}token={args.token}"

    _print(info(f"Opening: {url}"))
    webbrowser.open(url)

    _print(info(f"Press Ctrl+C to stop the server (ws://localhost:{args.port}/ws)"))
    try:
        t.join()
    except KeyboardInterrupt:
        return 0
    return 0


# ---------------------------------------------------------------------------
# Argument parsing
# ---------------------------------------------------------------------------

def _parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(
        prog="start",
        description="JARVIS AI OS — Unified Launcher",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=textwrap.dedent("""\
            Modes:
              auto     Auto-detect best interface (default)
              console  Text-mode console REPL
              hud      Qt HUD (requires display + PySide6/PyQt6)
               server   FastAPI backend only (port 7788)
               web      Backend + open web HUD in browser

            Examples:
              python start.py                  # auto-detect
              python start.py --mode console   # text-only
              python start.py --check          # env check only
              python start.py --mode server    # API backend
        """),
    )
    p.add_argument(
        "--mode", default="auto",
        choices=["auto", "console", "hud", "server", "web"],
        help="Interface mode to start (default: auto)",
    )
    p.add_argument(
        "--ngrok",
        action="store_true",
        default=False,
        help="Start an ngrok tunnel so the web HUD is accessible remotely "
             "(requires NGROK_AUTHTOKEN in .env and: pip install pyngrok)",
    )
    p.add_argument(
        "--ngrok-url",
        default="",
        metavar="URL",
        help="Use a fixed ngrok/cloudflare URL instead of auto-tunnelling "
             "(e.g. --ngrok-url wss://xxxx.ngrok-free.app/ws). "
             "Equivalent to setting NGROK_STATIC_URL in .env.",
    )
    p.add_argument("--check",      action="store_true", help="Run environment check and exit")
    p.add_argument("--no-voice",   action="store_true", help="Disable mic and TTS")
    p.add_argument("--debug",      action="store_true", help="DEBUG log level")
    p.add_argument("--config-dir", default="config",    help="Config directory (default: config/)")
    p.add_argument("--agent",      default="oracle",    help="Default agent context (console mode)")
    # Port must match server.py's JARVIS_PORT (default 7788) — the HUD/WebSocket
    # URL is ws://localhost:7788/ws. Do NOT use 8000 here.
    p.add_argument(
        "--port", type=int, default=int(os.getenv("JARVIS_PORT", "7788")),
        help="Backend port for the server/web modes (default: 7788)",
    )
    p.add_argument(
        "--token", default=os.getenv("JARVIS_SECRET", ""),
        metavar="SECRET",
        help="WebSocket auth token (sets JARVIS_SECRET). The HUD must connect "
             "with ?token=<SECRET>. Matches ws://localhost:7788/ws?token=...",
    )
    return p.parse_args()


# ---------------------------------------------------------------------------
# Banner
# ---------------------------------------------------------------------------

_BANNER = """\
   ██╗ █████╗ ██████╗ ██╗   ██╗██╗███████╗
   ██║██╔══██╗██╔══██╗██║   ██║██║██╔════╝
   ██║███████║██████╔╝██║   ██║██║███████╗
██ ██║██╔══██║██╔══██╗╚██╗ ██╔╝██║╚════██║
╚████╔╝██║  ██║██║  ██║ ╚████╔╝ ██║███████║
 ╚═══╝ ╚═╝  ╚═╝╚═╝  ╚═╝  ╚═══╝  ╚═╝╚══════╝"""


def print_banner(mode: str) -> None:
    _print(bold(cyan(_BANNER)))
    _print(_c("2", f"  Just A Rather Very Intelligent System — {mode.upper()} mode\n"))


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main() -> int:
    args = _parse_args()

    mode = args.mode
    if mode == "auto":
        mode = detect_mode()

    print_banner(mode)

    # ── Environment check ─────────────────────────────────────────────
    section("Environment Check")
    env = run_all_checks(mode=mode, config_dir=args.config_dir)
    print_check_results(env)

    if not env.ok:
        _print(f"\n{err('Environment check failed — fix the errors above before starting.')}")
        return 1

    n_warn = len(env.warnings)
    if n_warn:
        _print(warn(f"{n_warn} warning(s) above — JARVIS may run with reduced functionality"))

    if args.check:
        _print(ok("Environment check passed"))
        return 0

    _print(ok(f"Environment OK — launching {mode} mode"))

    # ── Launch ────────────────────────────────────────────────────────
    launchers = {
        "console": launch_console,
        "hud":     launch_hud,
        "server":  launch_server,
        "web":     launch_web,
    }

    launch_fn = launchers.get(mode)
    if launch_fn is None:
        _print(err(f"Unknown mode: {mode}"))
        return 2

    try:
        return launch_fn(args)
    except Exception as exc:
        _print(err(f"Launch error: {exc}"))
        import traceback
        traceback.print_exc()
        return 2


if __name__ == "__main__":
    sys.exit(main())