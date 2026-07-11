#!/usr/bin/env python3
"""
JARVIS AI OS — Console Entry Point
====================================
Pure terminal interface. No GUI, no Qt dependencies.

Pipeline (correct boot order):
  Phase 0  Config & Logging
  Phase 1  Kernel  (EventBus · EventRouter · ServiceRegistry · DI Container)
  Phase 2  Observability  (HealthMonitor)
  Phase 3  Models  (ModelRouter — Groq → Gemini → Local fallback)
  Phase 4  Perception  (Mic · Hotword · STT · TTS · VoiceCoordinator · Ack)
  Phase 5  Memory  (Working · Episodic · Semantic · Vector · MemoryRouter)
  Phase 6  Cognition  (StateManager · Scheduler · Debugger)
  Phase 7  Actions  (ToolRegistry · Desktop · Browser · Filesystem · Terminal)
  Phase 8  Agents  (Orchestrator → CoordinatorAgent + all specialists)
  [READY]  Console REPL  (text input → voice pipeline bypass → TTS output)

Usage
-----
  python main.py                   # full pipeline, voice enabled
  python main.py --no-voice        # skip mic/TTS, text-only REPL
  python main.py --text-only       # alias for --no-voice
  python main.py --agent oracle    # start with a specific agent context
  python main.py --debug           # DEBUG log level
  python main.py --log-file        # also write logs to file
"""

from __future__ import annotations

import argparse
import asyncio
import os
import signal
import sys
import textwrap
import time
import threading
from pathlib import Path
from typing import Any
import logging

log = logging.getLogger("jarvis.main")

# ── Load .env before anything else ──────────────────────────────────────────
try:
    from dotenv import load_dotenv
    load_dotenv(dotenv_path=Path(__file__).parent / ".env", override=True)
except ImportError:
    pass  # python-dotenv optional — keys can come from the shell environment


# ── Windows console encoding ────────────────────────────────────────────────
# Windows defaults stdout/stderr to the cp1252 codepage, which cannot encode
# the box-drawing glyphs (██) and emoji used by the banner/output. Reconfigure
# to UTF-8 so printing never raises UnicodeEncodeError. Best-effort: some old
# terminals don't support reconfigure(), but the fallback (errors="replace")
# keeps the app from crashing.
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


# ═══════════════════════════════════════════════════════════════════════════
# ANSI colour helpers (degrade gracefully on non-TTY)
# ═══════════════════════════════════════════════════════════════════════════

_USE_COLOUR = sys.stdout.isatty() and os.name != "nt"


def _c(code: str, text: str) -> str:
    return f"\033[{code}m{text}\033[0m" if _USE_COLOUR else text


def cyan(t: str)    -> str: return _c("96", t)
def green(t: str)   -> str: return _c("92", t)
def yellow(t: str)  -> str: return _c("93", t)
def red(t: str)     -> str: return _c("91", t)
def bold(t: str)    -> str: return _c("1",  t)
def dim(t: str)     -> str: return _c("2",  t)
def blue(t: str)    -> str: return _c("94", t)
def magenta(t: str) -> str: return _c("95", t)


# ═══════════════════════════════════════════════════════════════════════════
# Argument parsing
# ═══════════════════════════════════════════════════════════════════════════

def _parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(
        prog="jarvis",
        description="JARVIS AI OS — Console Mode",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=textwrap.dedent("""\
            In-session commands:
              /help          Show this help
              /status        System health & agent status
              /memory        Show recent working memory
              /agents        List registered agents + status
              /agent <name>  Switch active agent context
              /voice on|off  Toggle voice (mic+TTS) at runtime
              /clear         Clear the terminal
              /quit          Graceful shutdown
        """),
    )
    p.add_argument("--no-voice",   action="store_true",
                   help="Skip microphone and TTS — text-only REPL")
    p.add_argument("--text-only",  action="store_true",
                   help="Alias for --no-voice")
    p.add_argument("--agent",      default="oracle",
                   help="Default agent context [oracle|athena|coder|friday|herald|memory|vision]")
    p.add_argument("--debug",      action="store_true",
                   help="Set log level to DEBUG")
    p.add_argument("--log-file",   action="store_true",
                   help="Enable rotating log file (jarvis.log)")
    p.add_argument("--config-dir", default="config",
                   help="Path to config directory (default: config/)")
    return p.parse_args()


# ═══════════════════════════════════════════════════════════════════════════
# Console printer — all UI output goes through here
# ═══════════════════════════════════════════════════════════════════════════

class Console:
    """Thread-safe terminal output with structured formatting."""

    _lock = threading.Lock()

    @classmethod
    def _print(cls, *args, **kwargs):
        with cls._lock:
            print(*args, **kwargs)

    @classmethod
    def banner(cls):
        cls._print(bold(cyan("""
   ██╗ █████╗ ██████╗ ██╗   ██╗██╗███████╗
   ██║██╔══██╗██╔══██╗██║   ██║██║██╔════╝
   ██║███████║██████╔╝██║   ██║██║███████╗
██ ██║██╔══██║██╔══██╗╚██╗ ██╔╝██║╚════██║
╚████╔╝██║  ██║██║  ██║ ╚████╔╝ ██║███████║
 ╚═══╝ ╚═╝  ╚═╝╚═╝  ╚═╝  ╚═══╝  ╚═╝╚══════╝""")))
        cls._print(dim("  Just A Rather Very Intelligent System — Console Mode\n"))

    @classmethod
    def phase(cls, name: str, detail: str = ""):
        tag = cyan(f"[{name}]")
        cls._print(f"  {tag} {detail}")

    @classmethod
    def ok(cls, msg: str):
        cls._print(f"  {green('✓')} {msg}")

    @classmethod
    def warn(cls, msg: str):
        cls._print(f"  {yellow('⚠')} {msg}")

    @classmethod
    def err(cls, msg: str):
        cls._print(f"  {red('✗')} {msg}", file=sys.stderr)

    @classmethod
    def info(cls, msg: str):
        cls._print(f"  {dim('·')} {msg}")

    @classmethod
    def section(cls, title: str):
        cls._print(f"\n{bold(blue('──'))} {bold(title)} {bold(blue('──'))}")

    @classmethod
    def jarvis_reply(cls, text: str, agent: str = "JARVIS"):
        tag = cyan(f"[{agent.upper()}]")
        cls._print(f"\n{tag} {text}\n")

    @classmethod
    def user_prompt(cls, agent: str = "oracle") -> str:
        tag = green(f"[YOU→{agent.upper()}]")
        # CRITICAL FIX: do NOT hold cls._lock across the blocking input() call.
        # input() can block for many seconds (or indefinitely) while the voice
        # pipeline runs concurrently in the background and needs to print via
        # Console.info()/newline() (e.g. JarvisConsole._on_stt_final showing
        # "You said: ..."). Those calls acquire the SAME non-reentrant lock —
        # holding it here deadlocked the EventBus worker thread running that
        # handler, which in turn prevented VoiceCoordinator._on_stt_final from
        # ever running, causing "STT transcription timeout" even though STT
        # succeeded.
        try:
            return input(f"{tag} ")
        except (EOFError, KeyboardInterrupt):
            return "/quit"

    @classmethod
    def voice_state(cls, state: str):
        icons = {
            "LISTENING":  ("🎙", "cyan"),
            "PROCESSING": ("⚙", "yellow"),
            "SPEAKING":   ("🔊", "green"),
            "IDLE":       ("·", "dim"),
        }
        icon, colour = icons.get(state, ("·", "dim"))
        coloured = {"cyan": cyan, "yellow": yellow, "green": green,
                    "dim": dim}.get(colour, dim)
        with cls._lock:
            print(f"\r  {coloured(icon)} Voice: {coloured(state)}          ",
                  end="", flush=True)

    @classmethod
    def newline(cls):
        cls._print()


# ═══════════════════════════════════════════════════════════════════════════
# Phase boot helpers
# ═══════════════════════════════════════════════════════════════════════════

class _PhaseTimer:
    def __init__(self, name: str):
        self._name = name
        self._t0 = time.monotonic()

    def done(self, detail: str = "") -> float:
        elapsed = (time.monotonic() - self._t0) * 1000
        suffix = f"  {dim(f'{elapsed:.0f}ms')}"
        if detail:
            Console.ok(f"{self._name} — {detail}{suffix}")
        else:
            Console.ok(f"{self._name}{suffix}")
        return elapsed


# ═══════════════════════════════════════════════════════════════════════════
# JARVIS Console Application
# ═══════════════════════════════════════════════════════════════════════════

class JarvisConsole:
    """
    Drives the full JARVIS boot sequence and hosts the interactive REPL.

    Architecture contract (same as bootstrap.py but console-adapted):
      - All services constructed here (no globals, no singletons except ModelRouter)
      - All inter-service communication via EventBus
      - Console REPL bypasses voice pipeline for text input but uses TTS for output

    NOTE (Phase 4.3 — boot implementation resync status):
      This is a deliberately simpler, dev-only/terminal boot path and is NOT
      guaranteed to stay in lockstep with boot/bootstrap.py's phase logic.
      boot/bootstrap.py (used by the Qt HUD via interface/launch.py) is the
      canonical, full boot-phase implementation. As of this audit pass,
      JarvisConsole is known to be missing two things bootstrap.py has:
        1. The OPENVINO_DEVICE env-var override for local model device
           selection (bootstrap.py checks os.getenv("OPENVINO_DEVICE") before
           falling back to config/"AUTO" — see boot/bootstrap.py ~line 348).
        2. Registration of agent_defaults (from config/agents.yaml) into the
           DI container, and application of task_routing (from
           config/models.yaml) into the model router's route table — see
           boot/bootstrap.py ~lines 364-392.
      This is a known, accepted gap for the terminal/dev path, not an
      oversight to silently "fix" by copying code — if you need full parity
      here, resync deliberately against bootstrap.py's current phase logic
      rather than hand-copying again, since hand-copying is how this drift
      happened in the first place. server.py remains the canonical brain for
      anything network-facing; this console path is for local dev/debug only.
    """

    def __init__(self, args: argparse.Namespace) -> None:
        self._args = args
        self._no_voice: bool = args.no_voice or args.text_only
        self._active_agent: str = args.agent
        self._running = False
        self._shutdown_event = asyncio.Event()

        # Service references — populated during boot phases
        self._config       = None
        self._bus          = None
        self._router       = None   # EventRouter
        self._registry     = None   # ServiceRegistry
        self._container    = None   # DI container
        self._health       = None   # HealthMonitor
        self._model_router = None
        self._memory_router = None
        self._orchestrator  = None
        self._tts_router    = None
        self._voice_coord   = None
        self._tool_registry = None  # FIX 5: tool dispatch

        # Pending response future for console → agent round-trip
        self._pending_response: asyncio.Future | None = None
        self._pending_lock = asyncio.Lock()

    # ──────────────────────────────────────────────────────────────────────
    # Entry point
    # ──────────────────────────────────────────────────────────────────────

    async def run(self) -> int:
        """Boot, run REPL, shutdown. Returns exit code."""
        Console.banner()
        Console.section("BOOT SEQUENCE")

        t_boot = time.monotonic()
        try:
            await self._boot()
        except Exception as exc:
            Console.err(f"Fatal boot error: {exc}")
            import traceback; traceback.print_exc()
            return 1

        elapsed = (time.monotonic() - t_boot) * 1000
        Console.section("READY")
        Console.ok(f"JARVIS AI OS online in {elapsed:.0f}ms")
        if not self._no_voice:
            Console.info("Say 'Hey JARVIS' or press Enter to activate voice")
        Console.info("Type /help for commands, /quit to exit")
        Console.newline()

        self._running = True
        self._install_signal_handlers()

        try:
            await self._repl()
        except Exception as exc:
            Console.err(f"REPL error: {exc}")
            import traceback; traceback.print_exc()
        finally:
            await self._shutdown()

        return 0

    # ──────────────────────────────────────────────────────────────────────
    # Boot phases
    # ──────────────────────────────────────────────────────────────────────

    async def _boot(self) -> None:
        self._phase0_config()
        await self._phase1_kernel()
        await self._phase2_observability()
        await self._phase3_models()
        await self._phase4_perception()
        await self._phase5_memory()
        await self._phase6_cognition()
        await self._phase7_actions()
        await self._phase8_agents()
        self._wire_console_bridge()

    # ── Phase 0: Config & Logging ─────────────────────────────────────────

    def _phase0_config(self) -> None:
        t = _PhaseTimer("Config")
        from config.settings import ConfigManager
        from observability.logging.logger import LoggerFactory

        self._config = ConfigManager()
        self._config.load(self._args.config_dir)
        cfg = self._config.config

        log_level = "DEBUG" if self._args.debug else cfg.logging.level
        LoggerFactory.reconfigure(
            level=log_level,
            fmt=cfg.logging.format,
            file_enabled=self._args.log_file,
            file_path=cfg.logging.file_path,
            max_bytes=cfg.logging.max_bytes,
            backup_count=cfg.logging.backup_count,
            console=False,   # suppress logger → stdout; Console class handles output
        )
        t.done(f"env={cfg.system.environment.value}  log={log_level}")

    # ── Phase 1: Kernel ───────────────────────────────────────────────────

    async def _phase1_kernel(self) -> None:
        t = _PhaseTimer("Kernel")
        from kernel.event_bus.event_bus import EventBus
        from kernel.event_bus.event_router import EventRouter
        from kernel.registry.service_registry import ServiceRegistry
        from boot.dependency_container import DependencyContainer

        cfg = self._config.config

        self._bus = EventBus(
            max_queue_size=cfg.event_bus.max_queue_size,
            worker_count=cfg.event_bus.worker_threads,
            deadletter_enabled=cfg.event_bus.deadletter_enabled,
        )
        await self._bus.start()

        self._router = EventRouter(self._bus)

        self._registry = ServiceRegistry()
        self._registry.set_bus(self._bus)

        self._container = DependencyContainer()
        (self._container
            .register_instance("config",           self._config)
            .register_instance("event_bus",        self._bus)
            .register_instance("event_router",     self._router)
            .register_instance("service_registry", self._registry)
            .register_instance("container",        self._container)
        )
        t.done("EventBus · EventRouter · ServiceRegistry · DI")

    # ── Phase 2: Observability ────────────────────────────────────────────

    async def _phase2_observability(self) -> None:
        t = _PhaseTimer("Observability")
        from observability.health.health_monitor import HealthCheck, HealthMonitor

        cfg = self._config.config
        self._health = HealthMonitor(
            bus=self._bus,
            check_interval_s=cfg.health.check_interval_s,
            degraded_threshold=cfg.health.degraded_threshold,
            unhealthy_threshold=cfg.health.unhealthy_threshold,
            window_size=cfg.health.history_window,
        )
        self._health.register(HealthCheck(
            name="event_bus",
            check_fn=lambda: self._bus._running if self._bus else False,
            critical=True,
        ))
        await self._health.start()
        self._container.register_instance("health_monitor", self._health)
        t.done("HealthMonitor online")

    # ── Phase 3: Models ───────────────────────────────────────────────────

    async def _phase3_models(self) -> None:
        t = _PhaseTimer("Models")
        from models.router.model_router import init_router
        from kernel.registry.service_registry import ServiceDescriptor

        cfg_llm = getattr(self._config.config, "llm_providers", {})
        gemini_cfg = cfg_llm.get("gemini")
        groq_cfg   = cfg_llm.get("groq")
        gemini_key = (
            os.getenv(gemini_cfg.api_key_env, "") if gemini_cfg
            else os.getenv("GEMINI_API_KEY", "")
        )
        groq_key = (
            os.getenv(groq_cfg.api_key_env, "") if groq_cfg
            else os.getenv("GROQ_API_KEY", "")
        )

        # Qwen local fast-path (OpenVINO IR / ONNX) — raw config since
        # engine/device/model_dir aren't part of the typed LLMProviderConfig.
        _qwen_local_raw = self._config._raw.get("llm_providers", {}).get("qwen_local", {})
        _qwen_local_engine = _qwen_local_raw.get("engine", "openvino")
        _qwen_local_device = _qwen_local_raw.get("device", "AUTO")

        self._model_router = init_router(
            gemini_api_key=gemini_key or None,
            groq_api_key=groq_key   or None,
            qwen_local_engine=_qwen_local_engine,
            qwen_local_device=_qwen_local_device,
            emergency_model=os.getenv("OLLAMA_EMERGENCY_MODEL", "qwen3:4b"),
        )
        self._container.register_instance("model_router", self._model_router)

        self._registry.register(ServiceDescriptor(
            name="models.router", tags=["llm"],
            dependencies=["kernel"],
            start_fn=self._noop("models.router"),
            stop_fn=self._noop("models.router"),
            health_fn=lambda: self._model_router is not None,
        ))
        await self._registry.start_service("models.router")

        # Route handler (informational only — ModelRouter called directly)
        self._router.register_handler("models.router", lambda e: None)

        providers_up = []
        if groq_key:   providers_up.append("Groq")
        if gemini_key: providers_up.append("Gemini")
        providers_up.append("Local/Ollama")
        t.done(" → ".join(providers_up))

        # Warm up the local Ollama model (qwen2.5:1.5b) in the background.
        # CHAT/FAST_TOOL chains now try "local" FIRST with only an 8s
        # per-provider timeout. Ollama's cold-start (loading the model into
        # memory) can take 10-30s+ on this hardware, which would cause the
        # first voice query to fail through to Groq unnecessarily. Pre-loading
        # here runs concurrently with the rest of boot (~7-10s remaining) so
        # the model is usually warm before the user's first request.
        try:
            local_provider = self._model_router.get_provider("local")
            if local_provider is not None and hasattr(local_provider, "load_model"):
                asyncio.create_task(local_provider.load_model())
        except Exception as exc:
            log.debug("Local model warm-up skipped", error=str(exc))

    # ── Phase 4: Perception ───────────────────────────────────────────────

    async def _phase4_perception(self) -> None:
        t = _PhaseTimer("Perception")
        from kernel.registry.service_registry import ServiceDescriptor

        if self._no_voice:
            Console.info("Perception: voice skipped (--no-voice)")
            t.done("text-only mode")
            return

        # ── Microphone ────────────────────────────────────────────────────
        from perception.speech.microphone import MicrophoneEngine
        mic = MicrophoneEngine(bus=self._bus)
        self._container.register_instance("microphone", mic)
        await self._svc("perception.microphone", mic.start, mic.stop,
                        deps=["models.router"], optional=True)

        # ── HotwordDetector ───────────────────────────────────────────────
        from perception.speech.hotword import HotwordDetector, HotwordConfig
        _ww = self._config._raw.get("wake_word", {}) if self._config else {}
        hotword = HotwordDetector(
            bus=self._bus,
            audio_queue=mic.audio_queue,
            config=HotwordConfig(
                keywords=[_ww.get("phrase", "hey jarvis"), "jarvis"],
                stage2_threshold=float(_ww.get("sensitivity", 0.62)),
                # LiveKit / Silero VAD is the primary wake-word engine
                # (Porcupine was removed). Map any legacy porcupine keyword
                # list onto the livekit keyword matcher.
                use_livekit=bool(_ww.get("use_livekit", True)),
                livekit_vad_threshold=float(_ww.get("livekit_vad_threshold", 0.35)),
                livekit_keywords=_ww.get(
                    "porcupine_keywords", ["hey jarvis", "jarvis"]
                ),
                livekit_keyword_threshold=float(
                    _ww.get("livekit_keyword_threshold", 0.70)
                ),
            ),
        )
        self._container.register_instance("hotword_detector", hotword)
        await self._svc("perception.hotword", hotword.start, hotword.stop,
                        deps=["perception.microphone"], optional=True)

        # ── WakeListener ─────────────────────────────────────────────────
        from perception.speech.wake_listener import WakeListener
        wake = WakeListener(bus=self._bus, audio_queue=mic.audio_queue)
        self._container.register_instance("wake_listener", wake)
        await self._svc("perception.wake_listener", wake.start, wake.stop,
                        deps=["perception.microphone"], optional=True)

        # ── STTEngine ────────────────────────────────────────────────────
        from perception.speech.stt import STTEngine, STTConfig as SttCfg
        _stt = self._config._raw.get("stt", {}) if self._config else {}
        stt = STTEngine(bus=self._bus, config=SttCfg(
            groq_api_key=os.getenv("GROQ_API_KEY", ""),
            language=_stt.get("language", "en"),
            sample_rate=int(_stt.get("sample_rate", 16000)),
        ))
        self._container.register_instance("stt_engine", stt)
        # Feed the STT engine into the hotword detector so "hey jarvis" is
        # confirmed via real transcription instead of a fragile acoustic guess.
        hotword.attach_stt_engine(stt)
        await self._svc("perception.stt", stt.start, stt.stop,
                        deps=["perception.wake_listener"], optional=True)

        # ── LiveSTT ──────────────────────────────────────────────────────
        from perception.speech.live_stt import LiveSTT
        live_stt = LiveSTT(bus=self._bus)
        self._container.register_instance("live_stt", live_stt)
        await self._svc("perception.live_stt", live_stt.start, live_stt.stop,
                        deps=["perception.stt"], optional=True)

        # ── TTSEngine ────────────────────────────────────────────────────
        from perception.voice.tts import TTSEngine, TTSConfig as TtsCfg
        _tts = self._config._raw.get("tts", {}) if self._config else {}
        tts_engine = TTSEngine(bus=self._bus, config=TtsCfg(
            primary_provider=_tts.get("primary", "edge_tts"),
            fallback_provider=_tts.get("fallback", "kokoro"),
            voice=_tts.get("voice", "en-US-AndrewMultilingualNeural"),
            rate=_tts.get("rate", "+0%"),
            pitch=_tts.get("pitch", "+0Hz"),
        ))
        tts_engine.start()
        self._container.register_instance("tts_engine", tts_engine)
        await self._svc("perception.tts_engine",
                        self._noop("perception.tts_engine"),
                        lambda: tts_engine.stop(),
                        deps=["models.router"], optional=True)

        # ── TTSRouter ────────────────────────────────────────────────────
        from perception.speech.tts_router import TTSRouter
        self._tts_router = TTSRouter(
            event_bus=self._bus,
            default_engine=tts_engine,
            service_registry=self._registry,
        )
        self._container.register_instance("tts_router", self._tts_router)
        await self._svc("perception.tts",
                        self._tts_router.start, self._tts_router.stop,
                        deps=["perception.tts_engine"], optional=True)

        # ── STTRouter ────────────────────────────────────────────────────
        from perception.speech.stt_router import STTRouter
        stt_router = STTRouter(
            event_bus=self._bus,
            engine=stt,
            service_registry=self._registry,
        )
        self._container.register_instance("stt_router", stt_router)
        await self._svc("perception.stt_router",
                        stt_router.start, stt_router.stop,
                        deps=["perception.live_stt"], optional=True)

        # ── InterruptDetector ────────────────────────────────────────────
        from perception.speech.interrupt_detector import InterruptDetector
        interrupt = InterruptDetector(
            event_bus=self._bus,
            service_registry=self._registry,
            system_health=self._health,
        )
        self._container.register_instance("interrupt_detector", interrupt)
        await self._svc("perception.interrupt_detector",
                        interrupt.start, interrupt.stop,
                        deps=["perception.stt_router", "perception.tts"],
                        optional=True)

        # ── VoiceCoordinator ─────────────────────────────────────────────
        from perception.speech.voice_coordinator import VoiceCoordinator
        self._voice_coord = VoiceCoordinator(
            event_bus=self._bus,
            tts_router=self._tts_router,
            interrupt_detector=interrupt,
            service_registry=self._registry,
            system_health=self._health,
        )
        self._container.register_instance("voice_coordinator", self._voice_coord)
        await self._svc("perception.voice_coordinator",
                        self._voice_coord.start, self._voice_coord.stop,
                        deps=["perception.stt_router", "perception.tts"],
                        optional=True)

        # ── AcknowledgementEngine ─────────────────────────────────────────
        try:
            from perception.speech.acknowledgement import (
                AcknowledgementEngine, AcknowledgementConfig,
            )
            _ack = self._config._raw.get("acknowledgement", {}) if self._config else {}
            ack = AcknowledgementEngine(
                bus=self._bus,
                tts_router=self._tts_router,
                config=AcknowledgementConfig(
                    enabled=bool(_ack.get("enabled", True)),
                    probability=float(_ack.get("probability", 0.82)),
                ),
            )
            self._container.register_instance("acknowledgement_engine", ack)
        except Exception as exc:
            Console.warn(f"AcknowledgementEngine skipped: {exc}")

        # ── ObserverEngine ────────────────────────────────────────────────
        try:
            from perception.observation.observer import Observer
            observer = Observer(event_bus=self._bus)
            self._container.register_instance("observer", observer)
            await observer.start()
        except Exception as exc:
            Console.warn(f"ObserverEngine skipped: {exc}")

        # ── OCRPipeline ───────────────────────────────────────────────────
        try:
            from perception.ocr.ocr_pipeline import OCRPipeline
            ocr = OCRPipeline(event_bus=self._bus, model_router=self._model_router)
            self._container.register_instance("ocr_pipeline", ocr)
        except Exception as exc:
            Console.warn(f"OCRPipeline skipped: {exc}")

        # ── ScreenshotService ─────────────────────────────────────────────
        try:
            from perception.vision.screenshot_service import ScreenshotService
            ss = ScreenshotService(event_bus=self._bus)
            self._container.register_instance("screenshot_service", ss)
            await ss.start()
        except Exception as exc:
            Console.warn(f"ScreenshotService skipped: {exc}")

        # Subscribe console voice-state display to bus
        self._bus.subscribe("voice.state.changed",  self._on_voice_state)
        self._bus.subscribe("voice.stt.transcription_final", self._on_stt_final)

        t.done("Mic · Hotword · STT · TTS · VoiceCoordinator · Ack")

    # ── Phase 5: Memory ───────────────────────────────────────────────────

    async def _phase5_memory(self) -> None:
        t = _PhaseTimer("Memory")
        from kernel.registry.service_registry import ServiceDescriptor
        from memory.working.context import WorkingMemory
        from memory.episodic.episodic_memory import EpisodicMemory
        from memory.semantic.semantic_memory import SemanticMemory
        from memory.vector.vector_memory import VectorMemory
        from memory.router.memory_router import MemoryRouter

        self._memory_router = MemoryRouter(
            working=WorkingMemory(),
            episodic=EpisodicMemory(),
            semantic=SemanticMemory(),
            vector=VectorMemory(),
        )
        self._memory_router.inject(
            event_bus=self._bus,
            model_router=self._model_router,
        )
        await self._memory_router.start()
        self._container.register_instance("memory_router", self._memory_router)

        for svc in [
            "memory.router", "memory.working_context",
            "memory.episodic", "memory.semantic", "memory.vector",
        ]:
            self._registry.register(ServiceDescriptor(
                name=svc, tags=["memory"],
                dependencies=["models.router"],
                start_fn=self._noop(svc), stop_fn=self._noop(svc),
            ))
            await self._registry.start_service(svc)

        # Route memory events
        async def _mem_handler(event) -> None:
            try:
                content = event.payload.get("content", str(event.payload))
                await self._memory_router.remember(content=content)
            except Exception as exc:
                log.warning("Memory handler failed", error=str(exc))

        self._router.register_handler("memory.router",          _mem_handler)
        self._router.register_handler("memory.working_context", _mem_handler)
        self._router.register_handler("memory.episodic",        lambda e: None)
        t.done("Working · Episodic · Semantic · Vector")

    # ── Phase 6: Cognition ────────────────────────────────────────────────

    async def _phase6_cognition(self) -> None:
        t = _PhaseTimer("Cognition")
        from kernel.registry.service_registry import ServiceDescriptor
        try:
            from boot.startup import start_kernel
            kh = start_kernel()
            self._container.register_instance("state_manager", kh["state_manager"])
            self._container.register_instance("scheduler",     kh["scheduler"])
            self._container.register_instance("debugger",      kh["debugger"])
        except Exception as exc:
            Console.warn(f"Kernel runtime partial: {exc}")

        for svc in [
            "cognition.decision_engine", "kernel.orchestrator",
            "kernel.scheduler", "kernel.state_manager",
        ]:
            self._registry.register(ServiceDescriptor(
                name=svc, tags=["cognition", "kernel"],
                dependencies=["memory.router", "models.router"],
                start_fn=self._noop(svc), stop_fn=self._noop(svc),
            ))
            await self._registry.start_service(svc)

        self._router.register_handler("cognition.decision_engine", lambda e: None)
        self._router.register_handler("kernel.state_manager",      lambda e: None)
        t.done("StateManager · Scheduler · Debugger")

    # ── Phase 7: Actions / Tools ──────────────────────────────────────────

    async def _phase7_actions(self) -> None:
        t = _PhaseTimer("Actions")
        from kernel.registry.service_registry import ServiceDescriptor
        try:
            from tools.registry.tool_registry import get_registry
            from tools.registry.tool_registry_registration import register_all_tools
            tool_reg = get_registry()
            registered = register_all_tools(tool_reg, event_bus=self._bus)
            self._container.register_instance("tool_registry", tool_reg)
            self._tool_registry = tool_reg  # FIX 5: expose for _try_tool_dispatch
            counts = {k: len(v) for k, v in registered.items()}
            Console.info(f"Tools: {counts}")
        except Exception as exc:
            Console.warn(f"ToolRegistry partial: {exc}")

        for svc in [
            "actions.desktop", "actions.browser",
            "actions.filesystem", "actions.terminal",
        ]:
            self._registry.register(ServiceDescriptor(
                name=svc, tags=["action"],
                dependencies=["kernel.orchestrator"],
                start_fn=self._noop(svc), stop_fn=self._noop(svc),
                optional=True,
            ))
            await self._registry.start_service(svc)

        self._router.register_handler("actions.desktop",    lambda e: None)
        self._router.register_handler("actions.browser",    lambda e: None)
        self._router.register_handler("actions.filesystem", lambda e: None)
        t.done("ToolRegistry · Desktop · Browser · Filesystem · Terminal")

    # ── Phase 8: Agents ───────────────────────────────────────────────────

    async def _phase8_agents(self) -> None:
        t = _PhaseTimer("Agents")
        from kernel.registry.service_registry import ServiceDescriptor
        from kernel.orchestrator.orchestrator import Orchestrator

        tool_reg = self._container.try_resolve("tool_registry")
        self._orchestrator = Orchestrator(
            event_bus=self._bus,
            model_router=self._model_router,
            memory_router=self._memory_router,
            tool_registry=tool_reg,
        )

        ocr = self._container.try_resolve("ocr_pipeline")
        ss  = self._container.try_resolve("screenshot_service")
        if hasattr(self._orchestrator, "inject_perception"):
            self._orchestrator.inject_perception(
                ocr_pipeline=ocr,
                screenshot_service=ss,
            )

        await self._orchestrator.start()
        self._container.register_instance("orchestrator",  self._orchestrator)
        self._container.register_instance("agent_registry",
                                          self._orchestrator.agent_registry)

        for svc in [
            "agents.coordinator", "agents.analysis", "agents.planning",
            "agents.research",    "agents.automation", "agents.engineering",
        ]:
            self._registry.register(ServiceDescriptor(
                name=svc, tags=["agent"],
                dependencies=["kernel.orchestrator", "memory.router"],
                start_fn=self._noop(svc), stop_fn=self._noop(svc),
            ))
            await self._registry.start_service(svc)

        # Wire EventRouter handlers
        coord = self._orchestrator._coordinator
        mem   = self._orchestrator.memory_router

        async def _intent_handler(event) -> None:
            source = getattr(event, "source", "") or ""
            if "voice" in source or "console" in source:
                return   # voice/console bypass already submitted directly
            text       = event.payload.get("text", str(event.payload))
            session_id = event.payload.get("session_id", "")
            await self._orchestrator.submit_intent(text, session_id)

        async def _mem_handler(event) -> None:
            try:
                content = event.payload.get("content", str(event.payload))
                await mem.remember(content=content)
            except Exception as exc:
                log.warning("Memory handler (phase-8) failed", error=str(exc))

        async def _coord_handler(event) -> None:
            if coord:
                await coord.on_event(event)

        async def _orch_handler(event) -> None:
            text = event.payload.get("text", "")
            if text:
                await self._orchestrator.submit_intent(text)

        self._router.register_handler("cognition.decision_engine", _intent_handler)
        self._router.register_handler("agents.coordinator",        _coord_handler)
        self._router.register_handler("kernel.orchestrator",       _orch_handler)
        self._router.register_handler("memory.working_context",    _mem_handler)
        self._router.register_handler("memory.router",             _mem_handler)
        self._router.register_handler("memory.episodic",           lambda e: None)
        self._router.register_handler("observability.health",      lambda e: None)
        self._router.register_handler("kernel.state_manager",      lambda e: None)
        self._router.register_handler("perception.speech",         lambda e: None)
        self._router.register_handler("perception.voice.tts",      lambda e: None)
        self._router.register_handler(
            "boot.shutdown",
            lambda e: asyncio.create_task(self._initiate_shutdown()),
        )
        self._router.install_routes()

        agents = list(self._orchestrator.agents.keys())
        t.done(f"Coordinator + {len(agents)-1} specialists: {agents}")

    # ──────────────────────────────────────────────────────────────────────
    # Console ↔ Agent bridge
    # ──────────────────────────────────────────────────────────────────────

    def _wire_console_bridge(self) -> None:
        """
        Connect the EventBus to the console.

        Voice pipeline:
          voice.utterance.received → model_router.chat() → voice.response.ready
          (VoiceCoordinator then speaks via TTS)

        Text pipeline:
          submit_intent() → user.intent → CoordinatorAgent → (agents execute)
          agent.goal_completed  → console output
          plan.completed        → console output
        """
        if self._bus is None:
            return

        # Voice utterance → fast TTS reply + console display
        # NOTE: In console mode there is no Bootstrap instance, so the
        # bootstrap._voice_utterance_handler (which generates the model
        # reply and publishes voice.response.ready) is NOT wired. We handle
        # it here directly so the voice pipeline doesn't time out at 35s.
        if not self._no_voice:
            from kernel.event_bus.event_bus import Event, Priority

            async def _voice_handler(event) -> None:
                run_id     = event.payload.get("run_id", "")
                text       = event.payload.get("text", "")
                session_id = event.payload.get("session_id", "")
                if not text:
                    return

                response_text = None

                # Tool dispatch first (open X, close X, etc.)
                try:
                    tool_result = await self._try_tool_dispatch(text)
                    if tool_result is not None:
                        response_text = tool_result
                except Exception:
                    pass

                # Fallback: direct model call for a concise spoken reply
                if not response_text and self._model_router:
                    try:
                        resp = await self._model_router.complete(
                            text,
                            system_override=(
                                "You are JARVIS, an AI assistant. "
                                "Give concise spoken answers — 1-3 sentences. "
                                "No markdown, no bullet points, no lists. "
                                "Natural speech only."
                            ),
                            timeout_s=45,
                        )
                        response_text = (resp.content or "").strip() or None
                    except Exception as exc:
                        log.warning("Voice model call failed", error=str(exc))

                if not response_text:
                    response_text = (
                        "I'm sorry, I wasn't able to process that in time. Please try again."
                    )

                # Publish so VoiceCoordinator can speak the reply
                await self._bus.publish(
                    Event(
                        event_type="voice.response.ready",
                        source="jarvis.main.console",
                        payload={
                            "run_id": run_id,
                            "text": response_text,
                            "session_id": session_id,
                            "source": "jarvis.main.console",
                        },
                        priority=Priority.HIGH,
                    )
                )

                # Background: submit to orchestrator for planning + memory
                if self._orchestrator and text:
                    asyncio.create_task(
                        self._orchestrator.submit_intent(text, session_id)
                    )

            self._bus.subscribe("voice.utterance.received", _voice_handler)

            # Single display handler for voice responses.
            async def _voice_reply_display(event) -> None:
                reply = event.payload.get("text", "")
                if reply:
                    Console.newline()
                    Console.jarvis_reply(reply)

            self._bus.subscribe("voice.response.ready", _voice_reply_display)

        # Agent completions → console output
        self._bus.subscribe("agent.goal_completed", self._on_agent_goal_completed)
        self._bus.subscribe("plan.completed",       self._on_plan_completed)

    # ──────────────────────────────────────────────────────────────────────
    # Interactive REPL
    # ──────────────────────────────────────────────────────────────────────

    async def _repl(self) -> None:
        loop = asyncio.get_running_loop()

        while self._running and not self._shutdown_event.is_set():
            # Read input on the thread-pool so we don't block the event loop
            try:
                raw = await loop.run_in_executor(
                    None, lambda: Console.user_prompt(self._active_agent)
                )
            except (EOFError, KeyboardInterrupt):
                raw = "/quit"

            text = raw.strip()
            if not text:
                # Empty enter: in voice mode, activate mic manually
                if not self._no_voice and self._voice_coord:
                    asyncio.create_task(self._activate_voice())
                continue

            if text.startswith("/"):
                should_continue = await self._handle_command(text)
                if not should_continue:
                    break
                continue

            # Normal text input → send to agent pipeline
            await self._ask_agent(text)

    async def _activate_voice(self) -> None:
        """Manually trigger the voice pipeline (same as wake word)."""
        if self._voice_coord and hasattr(self._voice_coord, "activate"):
            Console.info("Listening…")
            try:
                await self._voice_coord.activate()
            except Exception as exc:
                Console.warn(f"Voice activation error: {exc}")

    # ------------------------------------------------------------------
    # FIX 5 — Tool dispatch: intercept commands BEFORE sending to LLM
    # Returns a reply string if a tool was invoked, None otherwise.
    # ------------------------------------------------------------------

    _TOOL_PATTERNS: list[tuple] = []  # populated lazily after imports

    @staticmethod
    def _build_tool_patterns():
        """Build the command → tool routing table once."""
        import re
        return [
            # (compiled_pattern, tool_name, app_name_or_None)
            (re.compile(r"^open\s+(file\s*)?explorer\s*$", re.I), "apps.open", "explorer"),
            (re.compile(r"^open\s+notepad\s*$", re.I),             "apps.open", "notepad"),
            (re.compile(r"^open\s+chrome\s*$", re.I),              "apps.open", "chrome"),
            (re.compile(r"^open\s+firefox\s*$", re.I),             "apps.open", "firefox"),
            (re.compile(r"^open\s+calculator\s*$", re.I),          "apps.open", "calculator"),
            (re.compile(r"^open\s+terminal\s*$", re.I),            "apps.open", "terminal"),
            (re.compile(r"^open\s+vscode\b.*$", re.I),             "apps.open", "vscode"),
            (re.compile(r"^open\s+spotify\s*$", re.I),             "apps.open", "spotify"),
            (re.compile(r"^open\s+(.+)$", re.I),                   "apps.open", None),  # dynamic
            (re.compile(r"^close\s+(.+)$", re.I),                  "apps.close", None),
            (re.compile(r"^(launch|run|start)\s+(.+)$", re.I),     "apps.open", None),
        ]

    async def _try_tool_dispatch(self, text: str) -> str | None:
        """
        Check if text matches a tool command pattern.
        If yes: invoke the tool and return a formatted result string.
        If no match: return None so the caller falls through to LLM.
        """
        if not JarvisConsole._TOOL_PATTERNS:
            JarvisConsole._TOOL_PATTERNS = JarvisConsole._build_tool_patterns()

        import re
        for pattern, tool_name, fixed_arg in JarvisConsole._TOOL_PATTERNS:
            m = pattern.match(text.strip())
            if not m:
                continue

            # Resolve the app name: fixed arg or captured group
            if fixed_arg:
                app_name = fixed_arg
            else:
                # Last capturing group holds the dynamic app name
                app_name = m.group(m.lastindex).strip()

            if not app_name:
                continue

            # Try to invoke via tool registry
            try:
                if self._tool_registry:
                    result = await self._tool_registry.invoke(tool_name, name=app_name)
                else:
                    # Fallback: call apps_tool directly
                    from tools.system_tools.apps_tool import open_app, close_app
                    fn = open_app if tool_name == "apps.open" else close_app
                    import asyncio as _aio
                    result = await _aio.get_event_loop().run_in_executor(
                        None, fn, app_name
                    )

                if isinstance(result, dict):
                    if result.get("success"):
                        return f"✓ {result.get('message') or f'Launched {app_name}.'}"
                    else:
                        return f"✗ {result.get('message') or f'Could not open {app_name}.'}"
                return str(result)
            except Exception as exc:
                return f"Tool error: {exc}"

        return None  # no pattern matched → fall through to LLM

    async def _ask_agent(self, text: str) -> None:
        """
        Submit text to the ModelRouter (fast direct reply) AND to the
        Orchestrator (planning + memory). Displays the reply in the console.

        Tool commands (open X, close X) are intercepted FIRST and routed to
        the tool executor. The LLM only runs if no tool pattern matches.
        """
        session_id = f"console-{self._active_agent}"

        # ------------------------------------------------------------------
        # FIX 5: Tool intent detection — execute before LLM
        # Commands like "open explorer" must launch explorer.exe, not explain it.
        # ------------------------------------------------------------------
        tool_result = await self._try_tool_dispatch(text)
        if tool_result is not None:
            Console.jarvis_reply(tool_result, agent=self._active_agent)
            # Still submit to orchestrator for memory storage (no planning needed)
            if self._orchestrator:
                asyncio.create_task(
                    self._orchestrator.submit_intent(text, session_id)
                )
            return

        # Fast path: direct model call for immediate reply
        try:
            from models.router.model_router import TaskType
            # Map agent name to task type for best model selection
            _AGENT_TASK = {
                "oracle":  TaskType.CHAT,
                "athena":  TaskType.REASONING,
                "coder":   TaskType.CODE,
                "friday":  TaskType.FAST_TOOL,
                "herald":  TaskType.CHAT,
                "memory":  TaskType.CHAT,
                "vision":  TaskType.CHAT,
            }
            task_type = _AGENT_TASK.get(self._active_agent, TaskType.CHAT)

            # Build agent system context as system_override
            _AGENT_PROMPTS = {
                "oracle":  "You are ORACLE, the core conversational AI of J.A.R.V.I.S. Be precise, intelligent, and helpful. Keep responses concise but thorough.",
                "athena":  "You are ATHENA, the deep reasoning and research agent. Provide depth, nuance, and structured analysis. Use markdown where helpful.",
                "coder":   "You are CODER, the code generation agent. Write clean, production-grade code. Always use proper code blocks with language tags.",
                "friday":  "You are FRIDAY, the workflow execution agent. Be efficient, action-oriented, and systematic.",
                "herald":  "You are HERALD, the communications coordinator. Draft clearly and professionally.",
                "memory":  "You are the MEMORY AGENT. Manage episodic and semantic memory. Confirm what you store; retrieve clearly when asked.",
                "vision":  "You are VISION, the visual analysis agent. Handle visual understanding, OCR, and screen intelligence.",
            }
            system = _AGENT_PROMPTS.get(self._active_agent, _AGENT_PROMPTS["oracle"])

            resp = await self._model_router.complete(
                text,
                task_type=task_type,
                system_override=system,
                timeout_s=45,
            )
            reply_text = resp.content or "[empty response]"
            Console.jarvis_reply(reply_text, agent=self._active_agent)

            # Speak the reply via TTS if available
            if self._tts_router and reply_text and reply_text != "[empty response]":
                try:
                    asyncio.create_task(
                        self._tts_router.speak(text=reply_text, session_id=f"console-{self._active_agent}")
                    )
                except Exception:
                    pass

        except Exception as exc:
            Console.err(f"Model error: {exc}")

        # Background: submit to orchestrator for planning + memory storage
        # (fire-and-forget — don't block the REPL waiting for agent execution)
        if self._orchestrator:
            asyncio.create_task(
                self._orchestrator.submit_intent(text, session_id)
            )

    # ──────────────────────────────────────────────────────────────────────
    # REPL commands
    # ──────────────────────────────────────────────────────────────────────

    async def _handle_command(self, cmd: str) -> bool:
        """Handle /commands. Returns False to exit REPL."""
        parts = cmd.split(maxsplit=1)
        verb  = parts[0].lower()
        arg   = parts[1].strip() if len(parts) > 1 else ""

        if verb in ("/quit", "/exit", "/q"):
            Console.info("Shutting down…")
            return False

        elif verb in ("/help", "/?"):
            self._print_help()

        elif verb == "/status":
            await self._cmd_status()

        elif verb == "/memory":
            await self._cmd_memory()

        elif verb == "/agents":
            self._cmd_agents()

        elif verb == "/agent":
            if arg:
                self._active_agent = arg.lower()
                Console.ok(f"Active agent: {self._active_agent}")
            else:
                Console.info(f"Current agent: {self._active_agent}")

        elif verb == "/voice":
            await self._cmd_voice(arg)

        elif verb == "/clear":
            os.system("cls" if os.name == "nt" else "clear")
            Console.banner()

        elif verb == "/reset":
            coord = getattr(self._orchestrator, "_coordinator", None) if self._orchestrator else None
            if coord and hasattr(coord, "_active_plans"):
                n = len(coord._active_plans)
                coord._active_plans.clear()
                coord._replan_counts.clear()
                Console.ok(f"Cleared {n} active plan(s) — ready for commands")
            else:
                Console.warn("Coordinator not available")

        elif verb == "/stats":
            self._cmd_stats()

        else:
            Console.warn(f"Unknown command: {verb}  (type /help)")

        return True

    def _print_help(self) -> None:
        Console.section("JARVIS Console Commands")
        rows = [
            ("/help",          "Show this help"),
            ("/status",        "System health and agent status"),
            ("/memory",        "Show recent working memory entries"),
            ("/agents",        "List all registered agents and their status"),
            ("/agent <name>",  "Switch active agent (oracle|athena|coder|friday|herald|memory|vision)"),
            ("/voice on|off",  "Enable or disable voice pipeline at runtime"),
            ("/stats",         "ModelRouter provider statistics"),
            ("/clear",         "Clear terminal"),
            ("/reset",         "Clear stuck plans (fixes 'plan limit' drops)"),
            ("/quit",          "Graceful shutdown"),
        ]
        for cmd, desc in rows:
            Console.info(f"{cyan(cmd):<28} {desc}")
        Console.newline()

    async def _cmd_status(self) -> None:
        Console.section("System Status")
        if self._orchestrator:
            try:
                h = await self._orchestrator.health()
                mem  = h.get("memory", {})
                goal = h.get("goals",  {})
                Console.info(f"Memory   working={mem.get('working',{}).get('count','?')}  "
                             f"episodic={mem.get('episodic',{}).get('count','?')}")
                Console.info(f"Goals    total={goal.get('total','?')}  "
                             f"active={goal.get('by_status',{}).get('active',0)}")
                agents_h = h.get("agents", {})
                for name, status in agents_h.items():
                    icon = green("●") if status == "idle" else yellow("●")
                    Console.info(f"  {icon} {name:<20} {status}")
            except Exception as exc:
                Console.warn(f"Health unavailable: {exc}")
        if self._health:
            try:
                snap = await self._health.check_all()
                for name, result in snap.items():
                    ok = result.get("healthy", False)
                    Console.info(f"  {'✓' if ok else '✗'} {name}")
            except Exception as exc:
                Console.warn(f"Health check failed: {exc}")

    async def _cmd_memory(self) -> None:
        Console.section("Working Memory (last 10)")
        if self._memory_router:
            try:
                from memory.router.memory_router import MemoryQuery
                results = await self._memory_router.search(
                    MemoryQuery(text="", limit_each=10)
                )
                if not results:
                    Console.info("(empty)")
                for r in results[:10]:
                    content = getattr(r, "content", str(r))
                    Console.info(f"  {dim('·')} {content[:120]}")
            except Exception as exc:
                Console.warn(f"Memory query error: {exc}")
        else:
            Console.warn("MemoryRouter not available")

    def _cmd_agents(self) -> None:
        Console.section("Registered Agents")
        if self._orchestrator:
            for name, agent in self._orchestrator.agents.items():
                status = getattr(agent, "_status", "unknown")
                icon = green("●") if str(status) == "idle" else yellow("●")
                caps = [c.name for c in agent.capabilities()]
                Console.info(f"  {icon} {name:<20} {str(status):<12} {dim(', '.join(caps[:4]))}")
        else:
            Console.warn("Orchestrator not available")

    async def _cmd_voice(self, arg: str) -> None:
        if self._no_voice:
            Console.warn("Voice was disabled at startup (--no-voice). Restart without that flag.")
            return
        if arg == "off":
            # Publish a mute event — VoiceCoordinator honours it
            from kernel.event_bus.event_bus import Event
            await self._bus.publish(Event(
                event_type="voice.mode.changed",
                source="console",
                payload={"mode": "muted"},
            ))
            Console.ok("Voice muted")
        elif arg == "on":
            from kernel.event_bus.event_bus import Event
            await self._bus.publish(Event(
                event_type="voice.mode.changed",
                source="console",
                payload={"mode": "continuous"},
            ))
            Console.ok("Voice enabled")
        else:
            Console.info("Usage: /voice on | /voice off")

    def _cmd_stats(self) -> None:
        Console.section("ModelRouter Statistics")
        if self._model_router:
            stats = self._model_router.get_stats()
            for k, v in stats.items():
                Console.info(f"  {k:<28} {v}")
        else:
            Console.warn("ModelRouter not available")

    # ──────────────────────────────────────────────────────────────────────
    # EventBus subscribers (called from async event workers)
    # ──────────────────────────────────────────────────────────────────────

    def _on_voice_state(self, event) -> None:
        state = event.payload.get("to_state", event.payload.get("state", "IDLE"))
        Console.voice_state(state.upper())

    def _on_stt_final(self, event) -> None:
        text = event.payload.get("text", "")
        if text:
            Console.newline()
            Console.info(f"{dim('You said:')} {text}")

    def _on_agent_goal_completed(self, event) -> None:
        """Suppress per-goal completion noise; update metrics counter only."""
        payload = event.payload if hasattr(event, "payload") else {}
        goal_id = payload.get("goal_id", "?")
        try:
            from observability.metrics.metrics_collector import MetricsCollector
            MetricsCollector.get().increment("jarvis.goals.completed")
        except Exception:
            pass  # metrics are best-effort
        # orchestrator already logs at INFO; avoid duplicate console spam

    def _on_plan_completed(self, event) -> None:
        plan_id = event.payload.get("plan_id", "")
        Console.info(f"{dim('Plan complete:')} {plan_id}")

    # ──────────────────────────────────────────────────────────────────────
    # Shutdown
    # ──────────────────────────────────────────────────────────────────────

    async def _initiate_shutdown(self) -> None:
        self._running = False
        self._shutdown_event.set()

    def _install_signal_handlers(self) -> None:
        loop = asyncio.get_running_loop()
        for sig in (signal.SIGINT, signal.SIGTERM):
            try:
                loop.add_signal_handler(
                    sig,
                    lambda s=sig: asyncio.create_task(self._handle_signal(s)),
                )
            except (NotImplementedError, RuntimeError):
                pass  # Windows

    async def _handle_signal(self, sig) -> None:
        Console.newline()
        Console.warn(f"Signal {sig.name} — shutting down gracefully…")
        await self._initiate_shutdown()

    async def _shutdown(self) -> None:
        Console.section("SHUTDOWN")
        t0 = time.monotonic()

        # Run the actual cleanup as a separate task and shield it from
        # KeyboardInterrupt. If the user hits Ctrl+C again during shutdown,
        # asyncio.run() raises KeyboardInterrupt out of run_until_complete,
        # which would abandon this coroutine mid-cleanup — leaving
        # aiosqlite connections open and their worker threads alive when
        # the loop closes (causing "RuntimeError: Event loop is closed"
        # in aiosqlite's _connection_worker_thread). Shielding ensures the
        # cleanup task keeps running to completion even if this coroutine
        # itself gets cancelled/interrupted.
        cleanup_task = asyncio.ensure_future(self._shutdown_cleanup())
        try:
            await asyncio.shield(cleanup_task)
        except KeyboardInterrupt:
            Console.warn("Second interrupt — waiting for cleanup to finish…")
            try:
                await cleanup_task
            except Exception:
                pass
        except Exception:
            pass

        elapsed = (time.monotonic() - t0) * 1000
        Console.ok(f"Shutdown complete in {elapsed:.0f}ms")
        Console.newline()

    async def _shutdown_cleanup(self) -> None:
        if self._orchestrator:
            try:
                await self._orchestrator.stop()
                Console.ok("Agents stopped")
            except Exception as exc:
                Console.warn(f"Agent shutdown error: {exc}")

        if self._memory_router:
            try:
                await self._memory_router.stop()
                Console.ok("Memory flushed")
            except Exception as exc:
                Console.warn(f"Memory shutdown error: {exc}")

        # Stop perception services in reverse order
        if not self._no_voice:
            for svc_name in [
                "perception.voice_coordinator",
                "perception.tts",
                "perception.tts_engine",
                "perception.stt_router",
                "perception.live_stt",
                "perception.stt",
                "perception.wake_listener",
                "perception.hotword",
                "perception.microphone",
            ]:
                try:
                    if self._registry:
                        await self._registry.stop_service(svc_name)
                except Exception:
                    pass
            Console.ok("Perception stopped")

        if self._health:
            try:
                await self._health.stop()
                Console.ok("HealthMonitor stopped")
            except Exception:
                pass

        if self._bus:
            try:
                await self._bus.stop()
                Console.ok("EventBus stopped")
            except Exception:
                pass

        try:
            loop = asyncio.get_running_loop()
            await loop.shutdown_default_executor()
        except Exception:
            pass

    # ──────────────────────────────────────────────────────────────────────
    # Helpers
    # ──────────────────────────────────────────────────────────────────────

    async def _svc(
        self,
        name: str,
        start_fn,
        stop_fn,
        *,
        deps: list[str] = (),
        optional: bool = False,
    ) -> None:
        """Register and start a service descriptor."""
        from kernel.registry.service_registry import ServiceDescriptor

        async def _start():
            if asyncio.iscoroutinefunction(start_fn):
                await start_fn()
            else:
                start_fn()

        async def _stop():
            if asyncio.iscoroutinefunction(stop_fn):
                await stop_fn()
            else:
                stop_fn()

        self._registry.register(ServiceDescriptor(
            name=name,
            tags=["perception"],
            dependencies=list(deps),
            start_fn=_start,
            stop_fn=_stop,
            optional=optional,
        ))
        await self._registry.start_service(name)

    def _noop(self, name: str):
        """Return a no-op async coroutine for optional service lifecycle hooks."""
        async def _fn() -> None:
            pass  # intentional no-op — service has no start/stop implementation
        return _fn


# ═══════════════════════════════════════════════════════════════════════════
# Entry point
# ═══════════════════════════════════════════════════════════════════════════

def main() -> None:
    args = _parse_args()
    app  = JarvisConsole(args)

    try:
        exit_code = asyncio.run(app.run())
    except KeyboardInterrupt:
        print("\n[JARVIS] Interrupted.")
        exit_code = 0

    sys.exit(exit_code)


if __name__ == "__main__":
    main()