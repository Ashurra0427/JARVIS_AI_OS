"""
JARVIS AI OS — Bootstrap
=========================
Master startup and shutdown orchestrator.

Startup sequence (strictly ordered):
  Phase 0 — Config & Logging
  Phase 1 — Kernel Primitives  (EventBus, EventRouter, ServiceRegistry, DI Container)
  Phase 2 — Observability      (HealthMonitor, Metrics)
  Phase 3 — Model Layer        (ModelRouter, Providers)
  Phase 4 — Perception Layer   (STT, TTS, Microphone, Wake Word)
  Phase 5 — Memory Layer       (MemoryRouter, WorkingContext, VectorStore)
  Phase 6 — Cognition Layer    (DecisionEngine, Orchestrator)
  Phase 7 — Action Layer       (Desktop, Browser, Filesystem)
  Phase 8 — Agent Layer        (Coordinator, Agents)
  Phase 9 — Interface          (PySide6 UI — started last, after all services up)

Shutdown sequence (reverse phases, graceful drain):
  Phase 9 → Phase 0
"""

from __future__ import annotations

import asyncio
import os
import signal
import time
from dataclasses import dataclass
from enum import IntEnum
from typing import Any

# -- Core imports (available at boot time) -----------------------------------
from config.settings import ConfigManager
from observability.logging.logger import LoggerFactory, get_logger
from kernel.event_bus.event_bus import Event, EventBus, Priority
from kernel.event_bus.event_router import EventRouter
from kernel.registry.service_registry import ServiceDescriptor, ServiceRegistry
from boot.dependency_container import DependencyContainer
from observability.health.health_monitor import HealthCheck, HealthMonitor


def _env_flag(name: str, default: bool = True) -> bool:
    """Parse a boolean environment variable (true/false/1/0/yes/no)."""
    raw = os.getenv(name)
    if raw is None:
        return default
    return raw.strip().lower() in ("1", "true", "yes", "on", "y")




# ---------------------------------------------------------------------------
# Phase enum
# ---------------------------------------------------------------------------


class Phase(IntEnum):
    CONFIG = 0
    KERNEL = 1
    OBSERVABILITY = 2
    MODELS = 3
    PERCEPTION = 4
    MEMORY = 5
    COGNITION = 6
    ACTIONS = 7
    AGENTS = 8
    INTERFACE = 9


# ---------------------------------------------------------------------------
# Bootstrap result
# ---------------------------------------------------------------------------


@dataclass
class BootResult:
    success: bool
    phase: Phase
    elapsed_ms: int
    error: str | None = None


# ---------------------------------------------------------------------------
# Bootstrap
# ---------------------------------------------------------------------------


class Bootstrap:
    """
    Drives the full system startup and shutdown lifecycle.

    Usage:
        bootstrap = Bootstrap()
        result = await bootstrap.start()
        if not result.success:
            sys.exit(1)
        ...
        await bootstrap.stop()
    """

    def __init__(self, config_dir: str = "config") -> None:
        self._config_dir = config_dir
        self._started = False
        self._loop: asyncio.AbstractEventLoop | None = None
        # Set by the calling entry point (interface/launch.py or main.py) after
        # construction; skip voice/UI phases when True
        self.no_voice: bool = False
        self.headless: bool = False

        # Core singletons — populated during startup
        self._config: ConfigManager | None = None
        self._bus: EventBus | None = None
        self._router: EventRouter | None = None
        self._registry: ServiceRegistry | None = None
        self._container: DependencyContainer | None = None
        self._health: HealthMonitor | None = None

    # ------------------------------------------------------------------
    # Public: Start
    # ------------------------------------------------------------------

    async def start(self) -> BootResult:
        t0 = time.monotonic()
        self._loop = asyncio.get_running_loop()
        self._install_signal_handlers()

        phases = [
            (Phase.CONFIG, self._phase_config),
            (Phase.KERNEL, self._phase_kernel),
            (Phase.OBSERVABILITY, self._phase_observability),
            (Phase.MODELS, self._phase_models),
            (Phase.PERCEPTION, self._phase_perception),
            (Phase.MEMORY, self._phase_memory),
            (Phase.COGNITION, self._phase_cognition),
            (Phase.ACTIONS, self._phase_actions),
            (Phase.AGENTS, self._phase_agents),
            (Phase.INTERFACE, self._phase_interface),
        ]

        for phase, fn in phases:
            log.info("Bootstrap phase starting", phase=phase.name)
            try:
                await fn()
                log.info("Bootstrap phase complete", phase=phase.name)
            except Exception as exc:
                elapsed_ms = int((time.monotonic() - t0) * 1000)
                import traceback as _tb
                log.critical(
                    f"Bootstrap failed [{phase.name}]: {type(exc).__name__}: {exc}\n"
                    + _tb.format_exc().strip()
                )
                await self._emergency_shutdown()
                return BootResult(
                    success=False,
                    phase=phase,
                    elapsed_ms=elapsed_ms,
                    error=str(exc),
                )

        self._started = True
        elapsed_ms = int((time.monotonic() - t0) * 1000)
        log.info("JARVIS AI OS started successfully", elapsed_ms=elapsed_ms)
        self._emit_system_ready(elapsed_ms)
        return BootResult(success=True, phase=Phase.INTERFACE, elapsed_ms=elapsed_ms)

    # ------------------------------------------------------------------
    # Public: Stop
    # ------------------------------------------------------------------

    async def stop(self) -> None:
        if not self._started:
            return

        log.info("JARVIS AI OS shutdown initiated")
        t0 = time.monotonic()

        self._emit("system.shutdown.started", {"reason": "graceful"})

        # Reverse-phase shutdown
        await self._stop_phase_interface()
        await self._stop_phase_agents()
        await self._stop_phase_actions()
        await self._stop_phase_cognition()
        await self._stop_phase_memory()
        await self._stop_phase_perception()
        await self._stop_phase_models()
        await self._stop_phase_observability()
        await self._stop_phase_kernel()

        elapsed_ms = int((time.monotonic() - t0) * 1000)
        log.info("JARVIS AI OS shutdown complete", elapsed_ms=elapsed_ms)
        self._started = False

    # ------------------------------------------------------------------
    # Phase 0 — Config & Logging
    # ------------------------------------------------------------------

    async def _phase_config(self) -> None:
        # Load config first (synchronous, fast)
        self._config = ConfigManager()
        self._config.load(self._config_dir)
        cfg = self._config.config

        # Configure logging with loaded settings — use reconfigure() to upgrade
        # the early auto-configured logger (triggered at import time) with the
        # full settings from config: correct format, log level, and file handler.
        LoggerFactory.reconfigure(
            level=cfg.logging.level,
            fmt=cfg.logging.format,
            file_enabled=cfg.logging.file_enabled,
            file_path=cfg.logging.file_path,
            max_bytes=cfg.logging.max_bytes,
            backup_count=cfg.logging.backup_count,
            console=cfg.logging.console,
        )
        log.info(
            "Config and logging initialised", environment=cfg.system.environment.value
        )

        # ── Registry validation (Phase 0, pre-kernel) ────────────────
        # Catches missing/malformed apps.yaml and web.yaml before any agent
        # or tool tries to use them. EventBus is not yet up, so pass bus=None.
        # raise_on_failure=False: treat registry problems as warnings at boot
        # (missing YAML files should not prevent headless / no-voice operation).
        try:
            from boot.startup_registry_validation import validate_registries
            result = validate_registries(event_bus=None, raise_on_failure=False)
            if result["success"]:
                log.info(
                    "Registry startup validation passed",
                    elapsed_s=result["elapsed_s"],
                )
            else:
                log.warning(
                    "Registry startup validation found issues — continuing boot",
                    errors=result["errors"],
                    elapsed_s=result["elapsed_s"],
                )
        except Exception as exc:
            # Validator itself failed to import/run — non-fatal, log and continue.
            log.warning("Registry validation raised unexpected error (non-fatal)", error=str(exc))

    # ------------------------------------------------------------------
    # Phase 1 — Kernel Primitives
    # ------------------------------------------------------------------

    async def _phase_kernel(self) -> None:
        cfg = self._config.config

        # 1a. EventBus
        self._bus = EventBus(
            max_queue_size=cfg.event_bus.max_queue_size,
            worker_count=cfg.event_bus.worker_threads,
            deadletter_enabled=cfg.event_bus.deadletter_enabled,
        )
        await self._bus.start()

        # 1b. EventRouter (wired to bus; handlers registered later per-phase)
        self._router = EventRouter(self._bus)

        # 1c. ServiceRegistry (needs bus for events)
        self._registry = ServiceRegistry()
        self._registry.set_bus(self._bus)

        # 1d. DI Container — register core singletons
        self._container = DependencyContainer()
        (
            self._container.register_instance("config", self._config)
            .register_instance("event_bus", self._bus)
            .register_instance("event_router", self._router)
            .register_instance("service_registry", self._registry)
            .register_instance("container", self._container)
        )

        log.info("Kernel primitives online")

    # ------------------------------------------------------------------
    # Phase 2 — Observability
    # ------------------------------------------------------------------

    async def _phase_observability(self) -> None:
        cfg = self._config.config

        self._health = HealthMonitor(
            bus=self._bus,
            check_interval_s=cfg.health.check_interval_s,
            degraded_threshold=cfg.health.degraded_threshold,
            unhealthy_threshold=cfg.health.unhealthy_threshold,
            window_size=cfg.health.history_window,
        )

        # Register self-checks
        self._health.register(
            HealthCheck(
                name="event_bus",
                check_fn=lambda: self._bus._running if self._bus else False,
                critical=True,
            )
        )

        await self._health.start()
        self._container.register_instance("health_monitor", self._health)

        # ── Fix 4: Wire MetricsCollector into the live health/event pipeline ───────
        # MetricsCollector is a thread-safe singleton (MetricsCollector.get()).
        # We wire it here so:
        #   • EventBus publishes increment event counts (event_type → counter)
        #   • health_monitor degraded/unhealthy events record timing metrics
        # This makes snapshot() useful for the UI and diagnostics.
        try:
            from observability.metrics.metrics_collector import MetricsCollector
            _mc = MetricsCollector.get()
            self._container.register_instance("metrics_collector", _mc)

            # Tap the EventBus: count every published event by type.
            # Uses a synchronous subscriber so it never blocks the bus.
            def _metrics_bus_tap(event) -> None:
                try:
                    _mc.increment(f"events.{event.event_type}")
                except Exception:
                    pass

            # Subscribe to the catch-all wildcard so every event is counted.
            # EventBus.subscribe() with "*" pattern matches all event types.
            self._bus.subscribe("*", _metrics_bus_tap)
            log.info("MetricsCollector wired to EventBus (all events)")
        except Exception as exc:
            log.warning("MetricsCollector wire-up failed (non-fatal)", error=str(exc))

        log.info("Observability layer online")

    # ------------------------------------------------------------------
    # Phase 3 — Model Layer (stub; full impl in models/)
    # ------------------------------------------------------------------

    async def _phase_models(self) -> None:
        from models.router.model_router import init_router

        cfg_llm = getattr(self._config.config, "llm_providers", {})
        _gemini_cfg = cfg_llm.get("gemini")
        _groq_cfg = cfg_llm.get("groq")
        _gemini_key = (
            os.getenv(_gemini_cfg.api_key_env, "")
            if _gemini_cfg
            else os.getenv("GEMINI_API_KEY", "")
        )
        _groq_key = (
            os.getenv(_groq_cfg.api_key_env, "")
            if _groq_cfg
            else os.getenv("GROQ_API_KEY", "")
        )
        # Qwen local fast-path (OpenVINO IR / ONNX) — raw config since
        # engine/device/model_dir aren't part of the typed LLMProviderConfig.
        _qwen_local_raw = self._config._raw.get("llm_providers", {}).get("qwen_local", {})
        _qwen_local_engine = _qwen_local_raw.get("engine", "openvino")
        # Device priority: OPENVINO_DEVICE env var > config file > "AUTO"
        _qwen_local_device = (
            os.getenv("OPENVINO_DEVICE")
            or _qwen_local_raw.get("device")
            or "AUTO"
        )

        model_router = init_router(
            gemini_api_key=_gemini_key or None,
            groq_api_key=_groq_key or None,
            qwen_local_engine=_qwen_local_engine,
            qwen_local_device=_qwen_local_device,
            emergency_model=os.getenv("OLLAMA_EMERGENCY_MODEL", "qwen3:4b"),
        )
        self._container.register_instance("model_router", model_router)

        # ── Smart (task-aware) routing wrapper ──
        # Wraps the base ModelRouter with capability/cost/privacy-aware
        # provider selection. Enabled by JARVIS_SMART_ROUTING (default true).
        # The wrapper is what agents and the server should prefer to call so
        # free cloud tiers and local models rotate by task automatically.
        try:
            from models.router.smart_router import wrap_router
            _smart_on = _env_flag("JARVIS_SMART_ROUTING", True)
            _prefer_local = _env_flag("JARVIS_PREFER_LOCAL", False)
            smart_router = wrap_router(
                model_router,
                smart_routing=_smart_on,
                prefer_local_default=_prefer_local,
            )
            self._container.register_instance("smart_model_router", smart_router)
        except Exception as exc:
            log.warning("SmartModelRouter wrap failed (non-fatal)", error=str(exc))

        # ── Register agent_defaults from agents.yaml in DI container ──
        try:
            self._container.register_instance("agent_defaults", self._config.config.agent_defaults)
        except Exception as exc:
            log.warning("agent_defaults registration failed (non-fatal)", error=str(exc))

        self._registry.register(
            ServiceDescriptor(
                name="models.router",
                tags=["llm", "routing"],
                dependencies=["kernel"],
                start_fn=self._noop_start("models.router"),
                stop_fn=self._noop_stop("models.router"),
                health_fn=lambda: model_router is not None,
            )
        )
        await self._registry.start_service("models.router")

        # Pre-register router handler so model.request.created events are not dropped
        async def _model_event_handler(event) -> None:
            pass  # ModelRouter is invoked directly via Orchestrator; events are informational

        self._router.register_handler("models.router", _model_event_handler)
        # ── Apply task_routing from models.yaml into _ROUTE_TABLE ──────
        try:
            from models.router.model_router import apply_task_routing
            task_routing_cfg = self._config._raw.get("model_router", {}).get("task_routing", {})
            if task_routing_cfg:
                apply_task_routing(task_routing_cfg)
        except Exception as exc:
            log.warning("apply_task_routing failed (non-fatal)", error=str(exc))

        log.info("Model layer registered", provider_count=len(model_router._providers))

    # ------------------------------------------------------------------
    # Phase 4 — Perception Layer
    # ------------------------------------------------------------------

    async def _phase_perception(self) -> None:
        if self.no_voice:
            log.info("Perception phase skipped (--no-voice)")
            return

        # --- Microphone ---------------------------------------------------
        from perception.speech.microphone import MicrophoneEngine

        mic = MicrophoneEngine(bus=self._bus)
        self._container.register_instance("microphone", mic)

        self._registry.register(
            ServiceDescriptor(
                name="perception.microphone",
                tags=["voice", "perception"],
                dependencies=["models.router"],
                start_fn=self._sync_start_fn(mic),
                stop_fn=self._sync_stop_fn(mic),
                optional=True,
            )
        )
        await self._registry.start_service("perception.microphone")

        # --- HotwordDetector -----------------------------------------------
        from perception.speech.hotword import HotwordDetector, HotwordConfig

        _ww_raw = self._config._raw.get("wake_word", {}) if self._config else {}
        _hotword_config = HotwordConfig(
            keywords=[_ww_raw.get("phrase", "hey jarvis"), "jarvis"],
            stage2_threshold=float(_ww_raw.get("sensitivity", 0.62)),
            use_porcupine=bool(_ww_raw.get("use_porcupine", True)),
            porcupine_access_key=str(_ww_raw.get("porcupine_access_key", "")),
            porcupine_keywords=_ww_raw.get("porcupine_keywords", ["hey_jarvis", "jarvis"]),
            porcupine_sensitivities=_ww_raw.get("porcupine_sensitivities") or None,
            porcupine_keyword_paths=_ww_raw.get("porcupine_keyword_paths") or None,
        )
        hotword = HotwordDetector(bus=self._bus, audio_queue=mic.audio_queue, config=_hotword_config)
        self._container.register_instance("hotword_detector", hotword)

        self._registry.register(
            ServiceDescriptor(
                name="perception.hotword",
                tags=["voice", "perception"],
                dependencies=["perception.microphone"],
                start_fn=self._sync_start_fn(hotword),
                stop_fn=self._sync_stop_fn(hotword),
                optional=True,
            )
        )
        await self._registry.start_service("perception.hotword")

        # --- WakeListener -------------------------------------------------
        from perception.speech.wake_listener import WakeListener

        wake_listener = WakeListener(bus=self._bus, audio_queue=mic.audio_queue)
        # Give WakeListener its own independent audio stream (fan-out from mic).
        # Without this, WakeListener and HotwordDetector compete on the same queue.
        mic.subscribe_audio_queue(wake_listener._mic_audio_queue)
        self._container.register_instance("wake_listener", wake_listener)

        self._registry.register(
            ServiceDescriptor(
                name="perception.wake_listener",
                tags=["voice", "perception"],
                dependencies=["perception.microphone"],
                start_fn=self._sync_start_fn(wake_listener),
                stop_fn=self._sync_stop_fn(wake_listener),
                optional=True,
            )
        )
        await self._registry.start_service("perception.wake_listener")

        # --- WakeWordManager (Task 12 fix) --------------------------------
        # Exposes enable/disable toggle to the settings panel so users can
        # turn wake-word detection on/off without restarting.
        try:
            from wakeword.manager import WakeWordManager
            wake_word_manager = WakeWordManager(
                bus=self._bus,
                mic=mic,
                hotword_detector=hotword,
                wake_listener=wake_listener,
                config=self._config,
            )
            self._container.register_instance("wakeword_manager", wake_word_manager)
            log.info("WakeWordManager registered")
        except Exception as exc:
            log.warning("WakeWordManager init failed (non-fatal): %s", exc)

        # --- STTEngine ----------------------------------------------------
        stt_engine = None
        try:
            from perception.speech.stt import STTEngine, STTConfig as SttEngineConfig

            _stt_groq_key = os.getenv("GROQ_API_KEY", "")
            _stt_raw = self._config._raw.get("stt", {}) if self._config else {}
            stt_engine = STTEngine(
                bus=self._bus,
                config=SttEngineConfig(
                    groq_api_key=_stt_groq_key,
                    language=_stt_raw.get("language", "en"),
                    sample_rate=int(_stt_raw.get("sample_rate", 16000)),
                ),
            )
            self._container.register_instance("stt_engine", stt_engine)
            # Feed STT into the hotword detector for reliable wake-phrase matching.
            try:
                hotword.attach_stt_engine(stt_engine)
            except Exception:
                pass

            self._registry.register(
                ServiceDescriptor(
                    name="perception.stt",
                    tags=["voice", "perception"],
                    dependencies=["perception.wake_listener"],
                    start_fn=self._sync_start_fn(stt_engine),
                    stop_fn=self._sync_stop_fn(stt_engine),
                    optional=True,
                )
            )
            await self._registry.start_service("perception.stt")
        except Exception as exc:
            log.warning(f"STTEngine init failed (non-fatal): {type(exc).__name__}: {exc}")

        # --- LiveSTT (streaming partials while user speaks) ---------------
        live_stt = None
        try:
            from perception.speech.live_stt import LiveSTT

            live_stt = LiveSTT(bus=self._bus)
            self._container.register_instance("live_stt", live_stt)

            self._registry.register(
                ServiceDescriptor(
                    name="perception.live_stt",
                    tags=["voice", "perception"],
                    dependencies=["perception.stt"],
                    start_fn=self._sync_start_fn(live_stt),
                    stop_fn=self._sync_stop_fn(live_stt),
                    optional=True,
                )
            )
            await self._registry.start_service("perception.live_stt")
        except Exception as exc:
            log.warning(f"LiveSTT init failed (non-fatal): {type(exc).__name__}: {exc}")

        # --- TTSEngine (synthesis + playback) ---------------------------------
        tts_engine = None
        try:
            from perception.voice.tts import TTSEngine, TTSConfig

            _tts_raw = self._config._raw.get("tts", {}) if self._config else {}
            tts_engine = TTSEngine(bus=self._bus, config=TTSConfig(
                primary_provider=_tts_raw.get("primary", "edge_tts"),
                fallback_provider=_tts_raw.get("fallback", "kokoro"),
                voice=_tts_raw.get("voice", "en-US-AndrewMultilingualNeural"),
                rate=_tts_raw.get("rate", "+0%"),
                pitch=_tts_raw.get("pitch", "+0Hz"),
            ))
            tts_engine.start()
            self._container.register_instance("tts_engine", tts_engine)

            self._registry.register(
                ServiceDescriptor(
                    name="perception.tts_engine",
                    tags=["voice", "perception"],
                    dependencies=["models.router"],
                    start_fn=self._noop_start("perception.tts_engine"),
                    stop_fn=self._sync_stop_fn(tts_engine),
                    optional=True,
                )
            )
            await self._registry.start_service("perception.tts_engine")
        except Exception as exc:
            log.warning(f"TTSEngine init failed (non-fatal): {type(exc).__name__}: {exc}")

        # --- TTSRouter (pure router: speak_request → TTSEngine) -----------
        tts_router = None
        try:
            from perception.speech.tts_router import TTSRouter

            tts_router = TTSRouter(
                event_bus=self._bus,
                default_engine=tts_engine,
                service_registry=self._registry,
            )
            self._container.register_instance("tts_router", tts_router)

            self._registry.register(
                ServiceDescriptor(
                    name="perception.tts",
                    tags=["voice", "perception"],
                    dependencies=["perception.tts_engine"],
                    start_fn=tts_router.start,
                    stop_fn=tts_router.stop,
                    optional=True,
                )
            )
            await self._registry.start_service("perception.tts")
        except Exception as exc:
            log.warning(f"TTSRouter init failed (non-fatal): {type(exc).__name__}: {exc}")

        # --- VoiceCoordinator (wired last — depends on STT + TTS) ---------
        try:
            from perception.speech.stt_router import STTRouter

            stt_router = STTRouter(
                event_bus=self._bus,
                engine=stt_engine,
                service_registry=self._registry,
            )
            self._container.register_instance("stt_router", stt_router)

            self._registry.register(
                ServiceDescriptor(
                    name="perception.stt_router",
                    tags=["voice", "perception"],
                    dependencies=["perception.live_stt"],
                    start_fn=stt_router.start,
                    stop_fn=stt_router.stop,
                    optional=True,
                )
            )
            await self._registry.start_service("perception.stt_router")
        except Exception as exc:
            log.warning(f"STTRouter init failed (non-fatal): {type(exc).__name__}: {exc}")

        try:
            from perception.speech.interrupt_detector import InterruptDetector

            interrupt_detector = InterruptDetector(
                event_bus=self._bus,
                service_registry=self._registry,
                system_health=self._health,
            )
            self._container.register_instance("interrupt_detector", interrupt_detector)

            self._registry.register(
                ServiceDescriptor(
                    name="perception.interrupt_detector",
                    tags=["voice", "perception"],
                    dependencies=["perception.stt_router", "perception.tts"],
                    start_fn=interrupt_detector.start,
                    stop_fn=interrupt_detector.stop,
                    optional=True,
                )
            )
            await self._registry.start_service("perception.interrupt_detector")
        except Exception as exc:
            log.warning(f"InterruptDetector init failed (non-fatal): {type(exc).__name__}: {exc}")
            interrupt_detector = None

        try:
            from perception.speech.voice_coordinator import VoiceCoordinator

            voice_coordinator = VoiceCoordinator(
                event_bus=self._bus,
                tts_router=tts_router,
                interrupt_detector=interrupt_detector,
                service_registry=self._registry,
                system_health=self._health,
                # stt_router NOT passed — STT now event-driven (FIX 2)
            )
            self._container.register_instance("voice_coordinator", voice_coordinator)

            self._registry.register(
                ServiceDescriptor(
                    name="perception.voice_coordinator",
                    tags=["voice", "perception"],
                    dependencies=["perception.stt_router", "perception.tts"],
                    start_fn=voice_coordinator.start,
                    stop_fn=voice_coordinator.stop,
                    optional=True,
                )
            )
            await self._registry.start_service("perception.voice_coordinator")
        except Exception as exc:
            log.warning(f"VoiceCoordinator init failed (non-fatal): {type(exc).__name__}: {exc}")

        # Observer Engine (passive context awareness)
        try:
            from perception.observation.observer import Observer
            observer = Observer(event_bus=self._bus)
            self._container.register_instance("observer", observer)
            await observer.start()  # async start — launches internal poll loop
            log.info("ObserverEngine started")
        except Exception as exc:
            log.warning("ObserverEngine start failed (non-fatal)", error=str(exc))

        # OCR Pipeline
        model_router = self._container.try_resolve("model_router")
        try:
            from perception.ocr.ocr_pipeline import OCRPipeline
            ocr = OCRPipeline(event_bus=self._bus, model_router=model_router)
            self._container.register_instance("ocr_pipeline", ocr)
            log.info("OCRPipeline registered")
        except Exception as exc:
            log.warning("OCRPipeline init failed (non-fatal)", error=str(exc))
        except Exception as exc:
            log.warning("OCRPipeline init failed (non-fatal)", error=str(exc))

        # Screenshot Service
        try:
            from perception.vision.screenshot_service import ScreenshotService
            screenshot_svc = ScreenshotService(event_bus=self._bus)
            self._container.register_instance("screenshot_service", screenshot_svc)
            await screenshot_svc.start()  # async start
            log.info("ScreenshotService registered")
        except Exception as exc:
            log.warning("ScreenshotService init failed (non-fatal)", error=str(exc))

        # AcknowledgementEngine — backchannel filler phrases while agent thinks
        try:
            from perception.speech.acknowledgement import AcknowledgementEngine, AcknowledgementConfig
            tts_router = self._container.try_resolve("tts_router")
            _ack_raw = self._config._raw.get("acknowledgement", {}) if self._config else {}
            ack_engine = AcknowledgementEngine(
                bus=self._bus,
                tts_router=tts_router,
                config=AcknowledgementConfig(
                    enabled=bool(_ack_raw.get("enabled", True)),
                    probability=float(_ack_raw.get("probability", 0.82)),
                ),
            )
            self._container.register_instance("acknowledgement_engine", ack_engine)
            log.info("AcknowledgementEngine registered")
        except Exception as exc:
            log.warning("AcknowledgementEngine init failed (non-fatal)", error=str(exc))

        log.info(
            "Perception layer online — Mic, Hotword, WakeListener, STT, TTS, VoiceCoordinator, Ack"
        )

    def _sync_start_fn(self, svc):
        """Wrap a synchronous .start() method as an async callable."""

        async def _start():
            svc.start()

        return _start

    def _sync_stop_fn(self, svc):
        """Wrap a synchronous .stop() method as an async callable."""

        async def _stop():
            svc.stop()

        return _stop

    # ------------------------------------------------------------------
    # Phase 5 — Memory Layer (stub)
    # ------------------------------------------------------------------

    async def _phase_memory(self) -> None:
        from memory.working.context import WorkingMemory
        from memory.episodic.episodic_memory import EpisodicMemory
        from memory.semantic.semantic_memory import SemanticMemory
        from memory.vector.vector_memory import VectorMemory
        from memory.router.memory_router import MemoryRouter
        from models.embeddings.embedding_service import EmbeddingService

        # Initialise EmbeddingService early so MemoryRouter and ReasoningEngine
        # can both resolve it from the container.
        embedding_service = EmbeddingService()
        self._container.register_instance("embedding_service", embedding_service)
        log.info("EmbeddingService initialised")

        model_router = self._container.try_resolve("model_router")
        memory_router = MemoryRouter(
            working=WorkingMemory(),
            episodic=EpisodicMemory(),
            semantic=SemanticMemory(),
            vector=VectorMemory(),
        )
        memory_router.inject(event_bus=self._bus, model_router=model_router)
        await memory_router.start()
        self._container.register_instance("memory_router", memory_router)

        for svc in [
            "memory.router",
            "memory.working_context",
            "memory.episodic",
            "memory.semantic",
            "memory.vector",
        ]:
            self._registry.register(
                ServiceDescriptor(
                    name=svc,
                    tags=["memory"],
                    dependencies=["models.router"],
                    start_fn=self._noop_start(svc),
                    stop_fn=self._noop_stop(svc),
                )
            )
            await self._registry.start_service(svc)

        # Register memory router handlers for EventRouter targets
        async def _memory_store_handler(event) -> None:
            try:
                content = event.payload.get("content", str(event.payload))
                await memory_router.remember(content=content)
            except Exception as exc:
                log.debug("Memory store event handling failed", error=str(exc))

        self._router.register_handler("memory.router", _memory_store_handler)
        self._router.register_handler("memory.working_context", _memory_store_handler)
        self._router.register_handler("memory.episodic", lambda e: None)
        log.info("Memory layer registered and started")

    # ------------------------------------------------------------------
    # Phase 6 — Cognition Layer (stub)
    # ------------------------------------------------------------------

    async def _phase_cognition(self) -> None:
        from boot.startup import start_kernel

        # Start kernel runtime (StateManager, Scheduler, Debugger)
        scheduler = None
        try:
            kh = start_kernel()
            self._container.register_instance("state_manager", kh["state_manager"])
            self._container.register_instance("scheduler", kh["scheduler"])
            self._container.register_instance("debugger", kh["debugger"])
            scheduler = kh["scheduler"]
            log.info("Kernel runtime started", boot_time=kh.get("boot_time"))
        except Exception as exc:
            log.warning("Kernel runtime start failed (non-fatal)", error=str(exc))

        # ── Phase 3 fix: start the async tick loop + register periodic tasks ─────
        # boot/startup.py calls scheduler.start() (sync, no loop).
        # start_async() adds the self-driving asyncio.Task on top.
        if scheduler is not None:
            try:
                await scheduler.start_async()
            except Exception as exc:
                log.warning("Scheduler start_async failed (non-fatal)", error=str(exc))

            # ── Real call site 1: MetricsCollector snapshot flush (every 60 s) ──
            # Writes the current counter/latency snapshot into StateManager so
            # it appears in Debugger.dump_state() and the state snapshot file.
            try:
                from kernel.scheduler.scheduler import PeriodicTaskSpec, TaskPriority
                from observability.metrics.metrics_collector import MetricsCollector
                _sm = self._container.try_resolve("state_manager")

                async def _flush_metrics() -> None:
                    try:
                        snap = MetricsCollector.get().snapshot()
                        if _sm:
                            _sm.set("observability.metrics_snapshot", snap)
                    except Exception as exc:
                        log.debug("MetricsCollector flush task error", error=str(exc))

                scheduler.add_periodic_task(PeriodicTaskSpec(
                    name="metrics.flush",
                    interval_s=60.0,
                    fn=_flush_metrics,
                    priority=TaskPriority.LOW,
                ))
                log.info("Periodic task registered: metrics.flush (60 s)")
            except Exception as exc:
                log.warning("metrics.flush periodic task setup failed (non-fatal)", error=str(exc))

            # ── Real call site 2: DailySummary trigger (every 6 h) ───────────
            # Generates and publishes a daily_summary.ready event so the
            # ReflectionEngine (already subscribed) runs a reflection cycle.
            # Interval is 6 h so a long-running session gets at least one
            # mid-session summary without waiting until midnight.
            try:
                from kernel.scheduler.scheduler import PeriodicTaskSpec, TaskPriority
                _bus_ref = self._bus

                async def _run_daily_summary() -> None:
                    try:
                        from memory.summaries.daily_summary import DailySummary
                        from kernel.event_bus.event_bus import Event, Priority
                        ds = DailySummary()
                        report = ds.generate_summary()
                        if _bus_ref:
                            import dataclasses as _dc
                            payload = {
                                "date_label": report.date_label,
                                "report": _dc.asdict(report),
                            }
                            await _bus_ref.publish(Event(
                                event_type="daily_summary.ready",
                                source="scheduler.daily_summary",
                                payload=payload,
                                priority=Priority.LOW,
                            ))
                            log.info("DailySummary published", date_label=report.date_label)
                    except Exception as exc:
                        log.warning("DailySummary task error (non-fatal)", error=str(exc))

                scheduler.add_periodic_task(PeriodicTaskSpec(
                    name="memory.daily_summary",
                    interval_s=6 * 3600.0,       # every 6 hours
                    fn=_run_daily_summary,
                    priority=TaskPriority.LOW,
                ))
                log.info("Periodic task registered: memory.daily_summary (6 h)")
            except Exception as exc:
                log.warning("daily_summary periodic task setup failed (non-fatal)", error=str(exc))

            # ── Real call site 3: ReflectionEngine periodic cycle (every 12 h) ─
            try:
                from kernel.scheduler.scheduler import PeriodicTaskSpec, TaskPriority

                async def _run_reflection_cycle() -> None:
                    try:
                        from cognition.reflection.reflection_engine import ReflectionEngine
                        re_inst = self._container.try_resolve("reflection_engine")
                        if re_inst is None:
                            re_inst = ReflectionEngine()
                            re_inst.inject(event_bus=self._bus)
                            await re_inst.start()
                        await re_inst.reflect()
                        log.info("ReflectionEngine periodic cycle completed")
                    except Exception as exc:
                        log.warning("ReflectionEngine cycle error (non-fatal)", error=str(exc))

                scheduler.add_periodic_task(PeriodicTaskSpec(
                    name="cognition.reflection_cycle",
                    interval_s=43200.0,  # 12 hours
                    fn=_run_reflection_cycle,
                    priority=TaskPriority.LOW,
                ))
                log.info("Periodic task registered: cognition.reflection_cycle (12 h)")
            except Exception as exc:
                log.warning("ReflectionEngine periodic task setup failed (non-fatal)", error=str(exc))

        for svc in [
            "cognition.decision_engine",
            "kernel.orchestrator",
            "kernel.scheduler",
            "kernel.state_manager",
        ]:
            self._registry.register(
                ServiceDescriptor(
                    name=svc,
                    tags=["cognition", "kernel"],
                    dependencies=["memory.router", "models.router"],
                    start_fn=self._noop_start(svc),
                    stop_fn=self._noop_stop(svc),
                )
            )
            await self._registry.start_service(svc)

        # Stub handlers — real dispatch happens through Orchestrator (Phase 8)
        self._router.register_handler("cognition.decision_engine", lambda e: None)
        self._router.register_handler("kernel.state_manager", lambda e: None)
        log.info("Cognition layer registered")

    # ------------------------------------------------------------------
    # Phase 7 — Action Layer (stub)
    # ------------------------------------------------------------------

    async def _phase_actions(self) -> None:
        # FIX 5-A: Initialise and populate the global ToolRegistry
        try:
            from tools.registry.tool_registry import get_registry
            from tools.registry.tool_registry_registration import register_all_tools
            tool_registry = get_registry()
            registered = register_all_tools(tool_registry, event_bus=self._bus)
            self._container.register_instance("tool_registry", tool_registry)
            log.info(
                "Tool registry populated",
                counts={k: len(v) for k, v in registered.items()},
            )
        except Exception as exc:
            log.warning("Tool registry population failed (non-fatal)", error=str(exc))

        for svc in [
            "actions.desktop",
            "actions.browser",
            "actions.filesystem",
            "actions.terminal",
        ]:
            self._registry.register(
                ServiceDescriptor(
                    name=svc,
                    tags=["action", "automation"],
                    dependencies=["kernel.orchestrator"],
                    start_fn=self._noop_start(svc),
                    stop_fn=self._noop_stop(svc),
                    optional=True,
                )
            )
            await self._registry.start_service(svc)

        # Register action routing targets — ActionCoordinator subscribes its own events
        # at start(); these stubs satisfy the EventRouter target resolution.
        self._router.register_handler("actions.desktop", lambda e: None)
        self._router.register_handler("actions.browser", lambda e: None)
        self._router.register_handler("actions.filesystem", lambda e: None)
        log.info("Action layer registered")

    # ------------------------------------------------------------------
    # Phase 8 — Agent Layer (stub)
    # ------------------------------------------------------------------

    async def _phase_agents(self) -> None:
        from kernel.orchestrator.orchestrator import Orchestrator

        model_router = self._container.try_resolve("model_router")
        memory_router = self._container.resolve(
            "memory_router"
        )  # singleton from Phase 5
        tool_registry = self._container.try_resolve("tool_registry")  # FIX 5-B
        orchestrator = Orchestrator(
            event_bus=self._bus,
            model_router=model_router,
            memory_router=memory_router,
            tool_registry=tool_registry,
        )
        # FIX 7: supply perception services to orchestrator → VisionAgent
        ocr_pipeline = self._container.try_resolve("ocr_pipeline")
        screenshot_svc = self._container.try_resolve("screenshot_service")
        if hasattr(orchestrator, "inject_perception"):
            orchestrator.inject_perception(
                ocr_pipeline=ocr_pipeline,
                screenshot_service=screenshot_svc,
            )

        await orchestrator.start()
        self._container.register_instance("orchestrator", orchestrator)
        self._container.register_instance("agent_registry", orchestrator.agent_registry)
        # memory_router is NOT re-registered here — it is already the Phase 5 singleton.

        # P2-D: Register goal_manager.overdue_sweep with the live Scheduler
        scheduler_inst = self._container.try_resolve("scheduler")
        if scheduler_inst is not None and hasattr(orchestrator, "register_periodic_tasks"):
            try:
                orchestrator.register_periodic_tasks(scheduler_inst)
            except Exception as exc:
                log.warning("register_periodic_tasks failed (non-fatal): %s", exc)

        # ── Phase 12: Knowledge Feed — continuous knowledge ingestion ──────
        # Roadmap item 9. Keeps local-LLM answers current by periodically
        # pulling watched topics through the existing web.search /
        # web.extract_text tools and embedding them into memory, instead of
        # requiring model retraining. See memory/knowledge_feed/knowledge_feed.py
        # for the full design and PHASE12_STATUS.md for scope/limitations.
        try:
            from memory.knowledge_feed.knowledge_feed import (
                KnowledgeFeedService, KnowledgeFeedConfig, KnowledgeFeedTopic,
            )
            kf_cfg = self._load_knowledge_feed_config()
            knowledge_feed = KnowledgeFeedService(
                memory_router=memory_router,
                tool_registry=tool_registry,
                event_bus=self._bus,
                config=kf_cfg,
            )
            self._container.register_instance("knowledge_feed", knowledge_feed)
            if kf_cfg.enabled and scheduler_inst is not None:
                knowledge_feed.register_periodic(scheduler_inst)
            log.info(
                "Knowledge Feed initialised",
                enabled=kf_cfg.enabled,
                topics=len(kf_cfg.topics),
                interval_h=round(kf_cfg.interval_s / 3600, 2),
            )
        except Exception as exc:
            log.warning("Knowledge Feed initialisation failed (non-fatal)", error=str(exc))

        for svc in [
            "agents.coordinator",
            "agents.analysis",
            "agents.planning",
            "agents.research",
            "agents.automation",
            "agents.engineering",
        ]:
            self._registry.register(
                ServiceDescriptor(
                    name=svc,
                    tags=["agent"],
                    dependencies=["kernel.orchestrator", "memory.router"],
                    start_fn=self._noop_start(svc),
                    stop_fn=self._noop_stop(svc),
                )
            )
            await self._registry.start_service(svc)

        # Wire EventRouter handlers to live Orchestrator/memory objects
        coord = orchestrator._coordinator
        mem = orchestrator.memory_router

        async def _intent_handler(event) -> None:
            # Only route explicit cognition.decision_engine events, not voice events
            # (voice is already handled by _voice_utterance_handler → submit_intent)
            source = getattr(event, "source", "") or ""
            if "voice" in source or "bootstrap.voice_bridge" in source:
                return
            text = event.payload.get("text", str(event.payload))
            session_id = event.payload.get("session_id", "")
            await orchestrator.submit_intent(text, session_id)

        async def _memory_handler(event) -> None:
            try:
                content = event.payload.get("content", str(event.payload))
                await mem.remember(content=content)
            except Exception as exc:
                log.debug("Memory handler error", error=str(exc))

        async def _coord_handler(event) -> None:
            if coord:
                await coord.on_event(event)

        async def _orch_handler(event) -> None:
            text = event.payload.get("text", "")
            if text:
                await orchestrator.submit_intent(text)

        _voice_model_router = model_router  # captured from Phase 3 singleton

        # ── Voice intent extractor ──────────────────────────────────────────
        # Replaces the old rigid regex list.  Handles natural spoken forms:
        #   "open brave", "could you open brave for me", "launch brave",
        #   "open the brave browser", "close notepad", "shut down spotify",
        #   "play spotify", "start chrome", "open youtube.com"
        import re as _re

        # Words that introduce the app name (action verbs / filler phrases)
        _OPEN_RE = _re.compile(
            r"""
            (?:                                     # optional polite preamble
              (?:could\s+you|can\s+you|please|hey|jarvis)\s+
            )*
            (?:                                     # action verb
              open|launch|start|run|boot|load|fire\s+up|bring\s+up|
              pull\s+up|show\s+me|go\s+to|navigate\s+to|take\s+me\s+to
            )
            \s+
            (?:the\s+|a\s+|my\s+)?                 # optional article
            (?P<app>.+?)                            # app name (lazy)
            (?:                                     # optional trailing noise
              \s+(?:app|browser|application|program|window|for\s+me|please|now)
            )*
            \s*$
            """,
            _re.IGNORECASE | _re.VERBOSE,
        )

        _CLOSE_RE = _re.compile(
            r"""
            (?:(?:could\s+you|can\s+you|please|hey|jarvis)\s+)*
            (?:close|quit|exit|kill|shut\s+down|terminate|stop)
            \s+
            (?:the\s+|a\s+|my\s+)?
            (?P<app>.+?)
            (?:\s+(?:app|browser|application|program|window|for\s+me|please|now))*
            \s*$
            """,
            _re.IGNORECASE | _re.VERBOSE,
        )

        # Known app aliases for cleaning up STT artefacts ("the brave app" → "brave")
        _APP_ALIASES = {
            "brave browser": "brave", "brave app": "brave",
            "chrome browser": "chrome", "google chrome": "chrome",
            "microsoft edge": "edge", "edge browser": "edge",
            "vs code": "vscode", "visual studio code": "vscode",
            "visual studio": "vscode",
            "file explorer": "explorer", "windows explorer": "explorer",
            "command prompt": "cmd",
            "task manager": "taskmgr",
            "media player": "vlc",
        }

        def _resolve_app_name(raw: str) -> str:
            """Clean up STT noise and resolve aliases."""
            name = raw.strip().lower()
            # Strip trailing filler
            name = _re.sub(r"\s+(app|browser|application|program|window|for me|please|now)\s*$", "", name)
            return _APP_ALIASES.get(name, name)

        def _extract_voice_intent(text: str):
            """
            Returns (tool_name, app_name) or (None, None).
            Handles full natural-language spoken commands.
            """
            t = text.strip()
            m = _OPEN_RE.match(t)
            if m:
                return "apps.open", _resolve_app_name(m.group("app"))
            m = _CLOSE_RE.match(t)
            if m:
                return "apps.close", _resolve_app_name(m.group("app"))
            return None, None

        async def _try_voice_tool(text: str, tool_reg) -> str | None:
            """Attempt tool dispatch; return spoken reply or None."""
            tool_name, app_name = _extract_voice_intent(text)
            if not tool_name or not app_name:
                return None

            log.info("Voice tool dispatch", tool=tool_name, app=app_name)
            try:
                if tool_reg:
                    result = await tool_reg.invoke(tool_name, name=app_name)
                else:
                    from tools.system_tools.apps_tool import open_app, close_app
                    import asyncio as _aio
                    fn = open_app if tool_name == "apps.open" else close_app
                    result = await _aio.get_event_loop().run_in_executor(None, fn, app_name)

                if isinstance(result, dict):
                    success = result.get("success", False)
                    msg = result.get("message", "")
                    target = result.get("target", app_name)
                    if success:
                        verb = "Opened" if tool_name == "apps.open" else "Closed"
                        return msg or f"{verb} {target}."
                    else:
                        return msg or f"Sorry, I couldn't find {app_name} on this system."
                return str(result)
            except Exception as exc:
                log.warning("Voice tool dispatch failed", tool=tool_name, app=app_name, error=str(exc))
                return f"Sorry, I encountered an error trying to open {app_name}."

        async def _voice_utterance_handler(event) -> None:
            run_id = event.payload.get("run_id", "")
            text = event.payload.get("text", "")
            session_id = event.payload.get("session_id", "")
            if not text:
                return

            t0 = time.monotonic()
            log.info("Voice utterance received — generating response", text_preview=text[:60], run_id=run_id[:8])

            # FIX 5: Try tool dispatch FIRST — execution before conversation
            _tool_reg = None
            try:
                from boot.dependency_container import get_container
                _tool_reg = get_container().try_resolve("tool_registry")
            except Exception:
                pass

            tool_response = await _try_voice_tool(text, _tool_reg)
            t_tool = time.monotonic()
            log.debug(
                "[VOICE_BRIDGE] Tool dispatch check done",
                run_id=run_id[:8],
                elapsed_s=round(t_tool - t0, 2),
                matched=tool_response is not None,
            )
            if tool_response:
                response_text = tool_response
            else:
                # Generate spoken response via model_router (direct chat — fast path)
                try:
                    response = await _voice_model_router.complete(
                        text,
                        system_override=(
                            "You are JARVIS, an AI assistant. "
                            "Give concise spoken answers — 1-3 sentences. "
                            "No markdown, no bullet points, no lists. "
                            "Natural speech only."
                        ),
                        timeout_s=8,
                    )
                    response_text = (response.content or "").strip()
                    log.debug(
                        "[VOICE_BRIDGE] Model call done",
                        run_id=run_id[:8],
                        elapsed_s=round(time.monotonic() - t_tool, 2),
                        provider=getattr(response, "provider", None),
                    )
                    if not response_text:
                        # Model returned empty — give a safe fallback so TTS has something to say
                        log.warning("Voice model returned empty content — using fallback response")
                        response_text = "I'm here, but I didn't have a response for that. Could you rephrase?"
                except Exception as exc:
                    log.warning(
                        "Voice model inference failed",
                        error=str(exc),
                        elapsed_s=round(time.monotonic() - t_tool, 2),
                    )
                    response_text = (
                        "I'm sorry, I encountered an error processing your request."
                    )

            log.info(
                "Voice response ready — publishing",
                text_preview=response_text[:60],
                run_id=run_id[:8],
                total_elapsed_s=round(time.monotonic() - t0, 2),
            )

            # Reply to VoiceCoordinator so it can speak the response
            if self._bus:
                await self._bus.publish(
                    Event(
                        event_type="voice.response.ready",
                        source="bootstrap.voice_bridge",
                        payload={
                            "run_id": run_id,
                            "text": response_text,
                            "session_id": session_id,
                        },
                        priority=Priority.HIGH,
                    )
                )

            # Also submit to orchestrator for planning/memory — done AFTER
            # the TTS reply is dispatched so the user hears something immediately.
            # submit_intent publishes user.intent → CoordinatorAgent handles it.
            await orchestrator.submit_intent(text, session_id)

        self._bus.subscribe("voice.utterance.received", _voice_utterance_handler)

        # Upgrade stub handlers with live callables
        self._router.register_handler("cognition.decision_engine", _intent_handler)
        self._router.register_handler("agents.coordinator", _coord_handler)
        self._router.register_handler("kernel.orchestrator", _orch_handler)
        self._router.register_handler("memory.working_context", _memory_handler)
        self._router.register_handler("memory.router", _memory_handler)
        self._router.register_handler("memory.episodic", lambda e: None)

        # ── Fix 3: real observability.health + kernel.state_manager handlers ──
        # Previously both were lambda e: None.  Now:
        #   observability.health  → logs the event and increments MetricsCollector counter
        #   kernel.state_manager  → records health event into StateManager key-value store
        # boot.shutdown keeps its existing create_task(self.stop()) — that IS real.
        _metrics_collector = None
        try:
            from observability.metrics.metrics_collector import MetricsCollector
            _metrics_collector = MetricsCollector.get()
        except Exception:
            pass

        _state_mgr_ref = self._container.try_resolve("state_manager") if self._container else None

        async def _health_event_handler(event) -> None:
            """Log health events and record metrics so they are no longer silently dropped."""
            severity = event.event_type  # e.g. "system.health.degraded"
            payload = event.payload or {}
            log.warning("Health event routed to observability", event_type=severity, payload=payload)
            if _metrics_collector:
                try:
                    _metrics_collector.increment(f"health.{severity}")
                except Exception:
                    pass

        async def _state_manager_health_handler(event) -> None:
            """Record health events into StateManager so they survive in the snapshot."""
            payload = event.payload or {}
            sm = _state_mgr_ref
            if sm is not None:
                try:
                    sm.set(f"last_health_event.{event.event_type}", {
                        "event_type": event.event_type,
                        "payload": payload,
                        "ts": __import__("time").time(),
                    })
                except Exception as exc:
                    log.debug("state_manager health record failed", error=str(exc))

        self._router.register_handler("observability.health", _health_event_handler)
        self._router.register_handler("kernel.state_manager", _state_manager_health_handler)
        self._router.register_handler(
            "boot.shutdown", lambda e: asyncio.create_task(self.stop())
        )
        self._router.register_handler("perception.speech", lambda e: None)
        self._router.register_handler("perception.voice.tts", lambda e: None)

        # Install all routes now that all handlers are registered
        self._router.install_routes()

        # FIX 9: Wire all EventBus voice/tool events → UIEventBridge signals
        await self._wire_ui_bridge()

        log.info(
            "Agent layer registered, routing installed",
            agents=list(orchestrator.agents.keys()),
        )

    # ------------------------------------------------------------------
    # _wire_ui_bridge — single place where EventBus voice/tool events
    # are forwarded to UIEventBridge Qt signals (FIX 9).
    # ------------------------------------------------------------------

    async def _wire_ui_bridge(self) -> None:
        """
        Connect the EventBus to UIEventBridge so ALL backend events are
        forwarded as thread-safe Qt signals to the UI.

        Strategy (two-tier):
          TIER 1 — UIEventBridge.connect(bus)
                   The full bridge subscribes to agent.*, model.*, memory.*,
                   cognition.*, vision.*, browser.*, terminal.*, workflow.*,
                   tool.*, action.*, system.*, and voice state events.
                   This is the production path when PySide6 is available.

          TIER 2 — Manual voice subscriptions (legacy fallback)
                   Only used when UIEventBridge is unavailable (headless/test).
                   Keeps voice state / STT / TTS feedback working in that mode.

        NOTE: The previous implementation manually re-subscribed a small
        subset of events (voice + tool.invoked + system.health) and never
        called bridge.connect(). This meant agent status, model routing
        events, memory ops, goals, and workflow events were never forwarded
        to the UI, causing the activity log and agent status panels to stay
        blank even though the backend was running correctly.
        """
        bus = self._bus
        if bus is None:
            return

        # ── TIER 1: Full UIEventBridge (preferred) ───────────────────────
        try:
            from interface.ui_event_bridge import UIEventBridge
            bridge = UIEventBridge.instance()
            bridge.connect(bus)          # subscribes ALL event types in one call
            log.info("UIEventBridge fully wired to EventBus (all event types)")
            return                        # done — no need for manual fallback
        except ImportError:
            log.debug("UIEventBridge not available — falling back to manual voice wiring")
        except Exception as exc:
            log.warning("UIEventBridge.connect() failed — falling back", error=str(exc))

        # ── TIER 2: Manual voice-only fallback (headless / dev mode) ────
        # Only reached when UIEventBridge import fails or errors.
        try:
            from perception.speech.voice_events import VoiceEvent
        except ImportError:
            log.debug("VoiceEvent not importable — skipping voice bridge wiring")
            return

        def _emit_str(signal_name: str, value: str):
            """Thread-safe str-signal emit via PySide6 QMetaObject."""
            try:
                from PySide6.QtCore import QMetaObject, Qt, Q_ARG
                from interface.ui_event_bridge import UIEventBridge
                bridge = UIEventBridge.instance()
                sig = getattr(bridge.signals, signal_name, None)
                if sig is None:
                    return
                QMetaObject.invokeMethod(
                    bridge.signals,
                    signal_name,
                    Qt.ConnectionType.QueuedConnection,
                    Q_ARG(str, value),
                )
            except Exception as exc:
                log.debug("Fallback voice signal emit failed", signal=signal_name, error=str(exc))

        _STATE_MAP = {
            "LISTENING":   "LISTENING",
            "PROCESSING":  "PROCESSING",
            "SPEAKING":    "SPEAKING",
            "IDLE":        "IDLE",
            "INTERRUPTED": "IDLE",
        }

        bus.subscribe(
            "voice.state.changed",
            lambda e: _emit_str("voice_state", _STATE_MAP.get(e.payload.get("to_state", ""), "IDLE")),
        )
        bus.subscribe(VoiceEvent.LISTENING_STARTED, lambda e: _emit_str("voice_state", "LISTENING"))
        bus.subscribe(VoiceEvent.LISTENING_ENDED,   lambda e: _emit_str("voice_state", "PROCESSING"))
        bus.subscribe(VoiceEvent.TTS_SPEAKING_STARTED,  lambda e: _emit_str("voice_state", "SPEAKING"))
        bus.subscribe(VoiceEvent.TTS_SPEAKING_FINISHED, lambda e: _emit_str("voice_state", "IDLE"))
        log.info("UIEventBridge fallback: voice-only events wired")

    # ------------------------------------------------------------------
    # Phase 9 — Interface (stub — full UI started in interface/app.py)
    # ------------------------------------------------------------------

    async def _phase_interface(self) -> None:
        # The Qt UI must run on the main thread; it is launched by
        # interface/launch.py (or main.py for the legacy UI) after this
        # coroutine returns. This phase only registers
        # the interface service descriptor.
        self._registry.register(
            ServiceDescriptor(
                name="interface.app",
                tags=["ui", "interface"],
                dependencies=["agents.coordinator"],
                start_fn=self._noop_start("interface.app"),
                stop_fn=self._noop_stop("interface.app"),
                optional=True,
            )
        )
        log.info("Interface layer registered — UI startup deferred to main thread")

    # ------------------------------------------------------------------
    # Shutdown helpers (reverse phases)
    # ------------------------------------------------------------------

    async def _stop_phase_interface(self) -> None:
        await self._safe_stop("interface.app")

    async def _stop_phase_agents(self) -> None:
        # Drive agent shutdown through the live Orchestrator, which stops agents
        # in reverse-start order.  Then deregister the stub service entries.
        orchestrator = (
            self._container.try_resolve("orchestrator") if self._container else None
        )
        if orchestrator is not None:
            try:
                await orchestrator.stop()
            except Exception as exc:
                log.error("Orchestrator stop error", error=str(exc))
        for svc in [
            "agents.coordinator",
            "agents.analysis",
            "agents.planning",
            "agents.research",
            "agents.automation",
            "agents.engineering",
        ]:
            await self._safe_stop(svc)

    async def _stop_phase_actions(self) -> None:
        for svc in [
            "actions.desktop",
            "actions.browser",
            "actions.filesystem",
            "actions.terminal",
        ]:
            await self._safe_stop(svc)

    async def _stop_phase_cognition(self) -> None:
        # ── Graceful kernel runtime shutdown via boot/shutdown.py ─────────────────────
        # shutdown_kernel() performs the ordered 4-step teardown:
        #   1. Stop Scheduler (drain in-flight tasks)
        #   2. State snapshot (persist to logs/kernel_state_snapshot.json)
        #   3. Debugger flush (finalise diagnostic log)
        #   4. StateManager mark_offline
        # All steps are individually guarded — a failure in one does not
        # prevent the rest from running.
        if self._container:
            # Cancel the async tick loop before shutdown_kernel drains/stops.
            _sched = self._container.try_resolve("scheduler")
            if _sched is not None and hasattr(_sched, "stop_async"):
                try:
                    await _sched.stop_async()
                    log.debug("Scheduler async tick loop stopped")
                except Exception as exc:
                    log.warning("Scheduler stop_async error (non-fatal)", error=str(exc))
            try:
                from boot.shutdown import shutdown_kernel
                result = shutdown_kernel(
                    state_manager=self._container.try_resolve("state_manager"),
                    scheduler=self._container.try_resolve("scheduler"),
                    debugger=self._container.try_resolve("debugger"),
                )
                log.info(
                    "Kernel shutdown_kernel complete",
                    elapsed_ms=result["elapsed_ms"],
                    snapshot_keys=result["snapshot_keys"],
                    flushed_events=result["flushed_events"],
                    errors=result["errors"],
                )
            except Exception as exc:
                log.error("shutdown_kernel raised unexpectedly (non-fatal)", error=str(exc))

        for svc in [
            "kernel.scheduler",
            "cognition.decision_engine",
            "kernel.orchestrator",
            "kernel.state_manager",
        ]:
            await self._safe_stop(svc)

    async def _stop_phase_memory(self) -> None:
        # Stop the live MemoryRouter singleton (owned by Bootstrap Phase 5).
        # This cascades into episodic.stop(), semantic.stop(), vector.stop()
        # which close the aiosqlite connections and join their worker threads.
        memory_router = (
            self._container.try_resolve("memory_router") if self._container else None
        )
        if memory_router is not None:
            try:
                await memory_router.stop()
            except Exception as exc:
                log.error("MemoryRouter stop error", error=str(exc))
        for svc in [
            "memory.vector",
            "memory.semantic",
            "memory.episodic",
            "memory.working_context",
            "memory.router",
        ]:
            await self._safe_stop(svc)

    async def _stop_phase_perception(self) -> None:
        for svc in [
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
            await self._safe_stop(svc)

    async def _stop_phase_models(self) -> None:
        await self._safe_stop("models.router")

    async def _stop_phase_observability(self) -> None:
        if self._health:
            await self._health.stop()

    async def _stop_phase_kernel(self) -> None:
        if self._bus:
            await self._bus.stop()
        await self._shutdown_executor()

    async def _emergency_shutdown(self) -> None:
        log.critical("Emergency shutdown triggered")
        # Best-effort kernel state snapshot so we don't lose runtime state
        # even during a crash-boot.  Failures here are non-fatal.
        if self._container:
            try:
                from boot.shutdown import shutdown_kernel
                shutdown_kernel(
                    state_manager=self._container.try_resolve("state_manager"),
                    scheduler=self._container.try_resolve("scheduler"),
                    debugger=self._container.try_resolve("debugger"),
                )
            except Exception as exc:
                log.error("Emergency shutdown_kernel failed (non-fatal)", error=str(exc))
        try:
            if self._health:
                await self._health.stop()
            if self._bus:
                await self._bus.stop()
        except Exception:
            pass
        await self._shutdown_executor()

    async def _shutdown_executor(self) -> None:
        """
        Drain the event loop's default ThreadPoolExecutor.

        asyncio.run() calls loop.shutdown_default_executor() in its finally
        block, so this is a no-op when the process goes through asyncio.run().
        It IS necessary when Bootstrap.stop() is called from run_until_complete()
        or any other path that does not go through asyncio.run()'s cleanup,
        because run_in_executor() calls throughout the codebase (EventBus,
        EventRouter, health checks, model providers, perception, actions, tools)
        leave up to N asyncio_* worker threads alive until the executor drains.

        Calling shutdown_default_executor() is safe in all cases:
        - Idempotent: a second call after asyncio.run()'s implicit call finds
          no threads remaining and returns instantly.
        - Does not close the loop or prevent further coroutines from running.
        - Available since Python 3.9 (this project targets 3.11+).
        """
        loop = self._loop
        if loop is None or loop.is_closed():
            return
        try:
            await loop.shutdown_default_executor()
            log.debug("Default executor shut down")
        except RuntimeError:
            # Loop closed between the is_closed() check and the await.
            # Executor will be collected with the loop — nothing to do.
            pass

    async def _safe_stop(self, name: str) -> None:
        if self._registry:
            try:
                await self._registry.stop_service(name)
            except Exception as exc:
                log.error("Error stopping service", name=name, error=str(exc))

    # ------------------------------------------------------------------
    # Utilities
    # ------------------------------------------------------------------

    def _noop_start(self, name: str) -> Any:
        async def _start() -> None:
            log.debug("Service start stub", name=name)

        return _start

    def _noop_stop(self, name: str) -> Any:
        async def _stop() -> None:
            log.debug("Service stop stub", name=name)

        return _stop

    def _load_knowledge_feed_config(self) -> Any:
        """Load config/knowledge_feed.yaml into a KnowledgeFeedConfig.

        Falls back to sane defaults (disabled, no topics) if the file is
        missing or malformed — Knowledge Feed is opt-in until the user adds
        topics via Settings, so a missing config file must never crash boot.
        """
        from memory.knowledge_feed.knowledge_feed import KnowledgeFeedConfig, KnowledgeFeedTopic
        from pathlib import Path as _Path

        path = _Path("config") / "knowledge_feed.yaml"
        cfg = KnowledgeFeedConfig()
        try:
            if path.exists():
                import yaml
                with open(path, "r", encoding="utf-8") as fh:
                    raw = (yaml.safe_load(fh) or {}).get("knowledge_feed", {})
                cfg.enabled = bool(raw.get("enabled", cfg.enabled))
                cfg.interval_s = float(raw.get("interval_s", cfg.interval_s))
                cfg.ttl_days = float(raw.get("ttl_days", cfg.ttl_days))
                cfg.max_concurrent_fetches = int(
                    raw.get("max_concurrent_fetches", cfg.max_concurrent_fetches)
                )
                cfg.chunk_chars = int(raw.get("chunk_chars", cfg.chunk_chars))
                cfg.cycle_budget_s = float(raw.get("cycle_budget_s", cfg.cycle_budget_s))
                cfg.topics = [
                    KnowledgeFeedTopic(query=t) if isinstance(t, str)
                    else KnowledgeFeedTopic(
                        query=t.get("query", ""),
                        max_results=int(t.get("max_results", 3)),
                        enabled=bool(t.get("enabled", True)),
                    )
                    for t in raw.get("topics", [])
                ]
        except Exception as exc:
            log.warning("knowledge_feed.yaml load failed, using defaults (non-fatal)", error=str(exc))
        return cfg

    def _emit(self, event_type: str, payload: dict) -> None:
        if self._bus:
            self._bus.publish_sync(
                Event(
                    event_type=event_type,
                    source="bootstrap",
                    payload=payload,
                    priority=Priority.HIGH,
                )
            )

    def _emit_system_ready(self, elapsed_ms: int) -> None:
        self._emit(
            "system.startup.complete",
            {
                "elapsed_ms": elapsed_ms,
                "services": self._registry.snapshot() if self._registry else {},
            },
        )

    def _install_signal_handlers(self) -> None:
        if self._loop is None:
            return
        for sig in (signal.SIGINT, signal.SIGTERM):
            try:
                self._loop.add_signal_handler(
                    sig, lambda s=sig: asyncio.create_task(self._handle_signal(s))
                )
            except (NotImplementedError, RuntimeError):
                # Windows or already-handled
                pass

    async def _handle_signal(self, sig: signal.Signals) -> None:
        log.warning("Signal received — initiating graceful shutdown", signal=sig.name)
        await self.stop()
        self._loop.stop()