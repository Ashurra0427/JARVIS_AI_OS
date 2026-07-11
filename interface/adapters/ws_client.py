"""
interface/adapters/ws_client.py
────────────────────────────────
JARVIS AI OS — Backend Transport Adapter

P4-E FIX: ws_client now supports TWO backend modes, selectable at construction:

  Mode A — "kernel" (NEW, recommended):
    Talks directly to the in-process Kernel EventBus.
    No server.py required. The HUD and the kernel share the same process.
    ServerAdapter wraps the Orchestrator and relays signals via Qt.

  Mode B — "websocket" (legacy, unchanged):
    Connects to server.py at ws://localhost:7788/ws.
    All original WebSocket behaviour is preserved for anyone still using
    server.py as a standalone backend.

Usage (kernel mode):
    from kernel.orchestrator.orchestrator import Orchestrator
    adapter = ServerAdapter.from_kernel(orchestrator)
    window = JarvisWindow(server_adapter=adapter)

Usage (websocket mode — unchanged):
    adapter = ServerAdapter(url="ws://localhost:7788/ws")
    window = JarvisWindow(server_adapter=adapter)

Supported server → client message types (both modes):
  boot, metrics, agent_metrics, heartbeat, status, thinking,
  chat_reply, stt_result, tool_result, memory_stats, memory_results,
  tts_audio, settings_ack, task_created, task_list_result, task_error,
  pong, memory_ack
"""
from __future__ import annotations

import asyncio

import json

import logging

import os

import threading

from typing import Any

from PySide6.QtCore import QObject, QThread, Signal, Slot

log = logging.getLogger(__name__)

try:
    import websocket  # websocket-client
    _HAS_WS = True
except ImportError:
    _HAS_WS = False


# ─────────────────────────────────────────────────────────────────────────────
# Mode B: WebSocket recv thread (unchanged legacy path)
# ─────────────────────────────────────────────────────────────────────────────

class _RecvThread(QThread):
    """Runs blocking websocket recv in a background thread."""

    message_received = Signal(dict)
    connected        = Signal()
    disconnected     = Signal()

    def __init__(self, url: str, parent: QObject | None = None) -> None:
        super().__init__(parent)
        self._url = url
        self._ws: Any = None
        self._running = True

    def run(self) -> None:
        """
        FIX: Connection loop with backoff retry.

        The original implementation attempted the WebSocket connect exactly
        once — if server.py was still starting up (common on slower machines),
        _RecvThread died immediately and main_window's 5-second QTimer had to
        re-create the whole thread just to try again.  This caused the UI to
        flash the reconnect overlay repeatedly during the startup window.

        Now _RecvThread retries the connect internally with exponential backoff
        (1s → 2s → 4s → 8s, capped at 15s) before giving up and emitting
        disconnected so the outer QTimer retry can still kick in.
        """
        if not _HAS_WS:
            log.error("websocket-client not installed. Run: pip install websocket-client")
            return

        import time as _time
        _DELAYS = [1, 2, 4, 8, 15]

        for attempt, delay in enumerate(_DELAYS):
            if not self._running:
                break
            try:
                self._ws = websocket.WebSocket()
                self._ws.settimeout(10)
                self._ws.connect(self._url)
                self.connected.emit()
                log.info(f"WS connected to {self._url} (attempt {attempt + 1})")
                while self._running:
                    try:
                        raw = self._ws.recv()
                        if raw:
                            data = json.loads(raw)
                            self.message_received.emit(data)
                    except websocket.WebSocketConnectionClosedException:
                        log.info("WS connection closed by server")
                        break
                    except Exception as e:
                        log.debug(f"WS recv error: {e}")
                        break
                break  # Clean exit from recv loop — don't retry
            except Exception as e:
                log.warning(
                    f"WS connect failed ({self._url}) attempt {attempt + 1}/{len(_DELAYS)}: {e}"
                )
                if attempt < len(_DELAYS) - 1 and self._running:
                    log.info(f"Retrying in {delay}s…")
                    _time.sleep(delay)

        self.disconnected.emit()

    def send(self, payload: dict) -> None:
        if self._ws and self._running:
            try:
                self._ws.send(json.dumps(payload))
            except Exception as e:
                log.debug(f"WS send error: {e}")

    def stop(self) -> None:
        self._running = False
        if self._ws:
            try:
                self._ws.close()
            except Exception:
                pass


# ─────────────────────────────────────────────────────────────────────────────
# Mode A: In-process kernel bridge
# ─────────────────────────────────────────────────────────────────────────────

class _KernelBridge(QObject):
    """
    Connects the Qt HUD directly to the in-process Kernel EventBus.
    No WebSocket, no server.py. The HUD and kernel share the same process.

    Subscribes to EventBus events and translates them into the same dict
    schema that _RecvThread emits, so ServerAdapter._dispatch() handles
    both modes identically.
    """

    message_received = Signal(dict)
    connected        = Signal()
    disconnected     = Signal()

    def __init__(self, orchestrator: Any, parent: QObject | None = None) -> None:
        super().__init__(parent)
        self._orch = orchestrator
        self._bus = getattr(orchestrator, "_bus", None)
        self._loop: asyncio.AbstractEventLoop | None = None
        self._thread: threading.Thread | None = None
        self._running = False

    def start(self) -> None:
        """Start a dedicated asyncio event loop thread for kernel comms."""
        self._running = True
        self._thread = threading.Thread(
            target=self._run_loop, daemon=True, name="kernel-bridge-loop"
        )
        self._thread.start()

    def _run_loop(self) -> None:
        self._loop = asyncio.new_event_loop()
        asyncio.set_event_loop(self._loop)
        self._loop.run_until_complete(self._subscribe_and_run())

    async def _subscribe_and_run(self) -> None:
        if self._bus is None:
            log.error("KernelBridge: no EventBus found on orchestrator")
            self.disconnected.emit()
            return

        # Subscribe to all events the HUD cares about
        _handlers = {
            "user.reply":                self._on_user_reply,          # BUGFIX: was "agent.chat_reply", which nothing emits
            "agent.thinking":           self._on_thinking,
            "agent.goal_completed":     self._on_goal_completed,
            "agent.goal_started":       self._on_goal_started,       # Phase 9.4
            "agent.tool_call.started":  self._on_tool_call_started,  # Phase 9.4
            "agent.tool_call.completed":self._on_tool_call_completed,# Phase 9.4
            "system.health.report":     self._on_system_health,      # Phase 9.4
            "stt.transcription.final":  self._on_stt_result,
            "tts.audio_ready":          self._on_tts_audio,
            "kernel.metrics":           self._on_metrics,
            "agent.metrics.updated":    self._on_agent_metrics,
            "memory.stats":             self._on_memory_stats,
            "plan.completed":           self._on_task_created,
            "reasoning.diagnostic":     self._on_reasoning_diagnostic,
        }

        for event_type, handler in _handlers.items():
            try:
                self._bus.subscribe(event_type, handler)
            except Exception as exc:
                log.warning("KernelBridge: subscribe failed for %s: %s", event_type, exc)

        # Emit synthetic "boot" message with initial state
        boot_payload = await self._build_boot_payload()
        self.message_received.emit({"type": "boot", **boot_payload})
        self.connected.emit()
        log.info("KernelBridge: connected to in-process EventBus")

        # Keep the loop alive
        while self._running:
            await asyncio.sleep(1.0)

    async def _build_boot_payload(self) -> dict:
        try:
            health = await self._orch.health()
            return {
                "providers": health.get("model_router", {}),
                "agents": {
                    name: {"agent_name": name, "status": "idle", "metrics": {}}
                    for name in health.get("agents", {}).keys()
                },
                "memory": health.get("memory", {}),
            }
        except Exception:
            return {"providers": {}, "agents": {}, "memory": {}}

    # ── EventBus → Qt signal translators ─────────────────────────────────

    async def _on_user_reply(self, event: Any) -> None:
        """Handles CoordinatorAgent's "user.reply" — the real event for both
        its fast-path shortcut and its final plan-complete reply. This is
        the primary way a direct chat answer reaches the HUD in kernel mode."""
        p = event.payload
        text = p.get("text", "")
        fallback_details = p.get("fallback_details") or []
        if fallback_details:
            disclosures = "; ".join(str(d) for d in fallback_details)
            text = f"{text}\n\n_[Model fallback: {disclosures}]_" if text else text
        self.message_received.emit({
            "type": "chat_reply",
            "agent": p.get("agent", "jarvis"),
            "text": str(text),
            "provider": p.get("provider", "kernel"),
        })

    async def _on_chat_reply(self, event: Any) -> None:
        p = event.payload
        self.message_received.emit({
            "type": "chat_reply",
            "agent": p.get("agent", "jarvis"),
            "text": p.get("text", p.get("response", p.get("result", ""))),
            "provider": p.get("provider", "kernel"),
        })

    async def _on_thinking(self, event: Any) -> None:
        self.message_received.emit({
            "type": "thinking",
            "agent": event.payload.get("agent", "jarvis"),
        })

    async def _on_goal_completed(self, event: Any) -> None:
        """Per-sub-agent goal completion. BUGFIX: this used to also emit a
        "chat_reply" here — but CoordinatorAgent already sends the single,
        combined final answer via "user.reply" (see _on_user_reply above),
        exactly like server.py's WS mode does. Doing both meant one bubble
        per contributing sub-agent PLUS the real combined reply. This
        handler now only relays the extra structured fields (Phase B/D
        plumbing) that individual agents return, matching WS-mode parity —
        server.py never turns agent.goal_completed into chat text either."""
        p = event.payload
        result = p.get("result", {})
        agent_name = p.get("agent_name", "jarvis")

        # Additive (Phase B/D plumbing): relay whatever structured fields
        # this particular agent's handle_goal() actually returned beyond
        # plain text — e.g. FRIDAY's executed/succeeded/tool, or HERALD's
        # browsed/browse_tool — as a separate, optional event. This does
        # NOT change the chat_reply shape or any existing consumer; agents
        # that don't return extra structured fields simply produce an
        # (agent-only) message that carries no extra keys.
        _KNOWN_TEXT_KEYS = {"findings", "output", "message", "analysis",
                             "response", "description", "_fallback"}
        extra_fields = {
            k: v for k, v in result.items()
            if k not in _KNOWN_TEXT_KEYS and isinstance(v, (bool, int, float, str))
        }
        if extra_fields:
            self.message_received.emit({
                "type": "agent_goal_result",
                "agent": agent_name,
                **extra_fields,
            })

    async def _on_stt_result(self, event: Any) -> None:
        self.message_received.emit({
            "type": "stt_result",
            "transcript": event.payload.get("text", ""),
        })

    async def _on_tts_audio(self, event: Any) -> None:
        import base64
        data = event.payload.get("audio_bytes") or event.payload.get("b64", b"")
        if isinstance(data, bytes):
            data = base64.b64encode(data).decode()
        msg: dict = {
            "type": "tts_audio",
            "b64": data,
            "mime": event.payload.get("mime", "audio/mp3"),
        }
        # Forward server-measured duration so the desktop HUD can pace
        # text reveal even when soundfile can't decode the container (e.g.
        # MP3 from edge-tts without libsndfile MP3 support).
        if "duration_s" in event.payload:
            msg["duration_s"] = event.payload["duration_s"]
        self.message_received.emit(msg)

    async def _on_metrics(self, event: Any) -> None:
        self.message_received.emit({"type": "metrics", **event.payload})

    async def _on_agent_metrics(self, event: Any) -> None:
        self.message_received.emit({"type": "agent_metrics", **event.payload})

    async def _on_memory_stats(self, event: Any) -> None:
        self.message_received.emit({"type": "memory_stats", **event.payload})

    async def _on_task_created(self, event: Any) -> None:
        self.message_received.emit({
            "type": "task_created",
            "plan_id": event.payload.get("plan_id", ""),
            **event.payload,
        })

    async def _on_reasoning_diagnostic(self, event: Any) -> None:
        # Surface reasoning results as a subtle status message
        confidence = event.payload.get("confidence", 0)
        domain = event.payload.get("domain", "")
        conclusion = event.payload.get("conclusion", "")[:80]
        self.message_received.emit({
            "type": "status",
            "text": f"[reasoning] {domain} | conf={confidence:.2f} | {conclusion}",
        })

    # Phase 9.4: live tool-call stream (Phase 8.4 events in kernel mode)
    async def _on_goal_started(self, event: Any) -> None:
        p = event.payload
        self.message_received.emit({
            "type":        "agent_goal_started",
            "agent":       p.get("agent_name", ""),
            "goal_id":     p.get("goal_id", ""),
            "description": p.get("description", ""),
        })

    async def _on_tool_call_started(self, event: Any) -> None:
        p = event.payload
        self.message_received.emit({
            "type":  "agent_tool_call",
            "agent": p.get("agent_name", ""),
            "tool":  p.get("tool", ""),
            "state": "started",
            "args":  p.get("args", {}),
        })

    async def _on_tool_call_completed(self, event: Any) -> None:
        p = event.payload
        self.message_received.emit({
            "type":       "agent_tool_call",
            "agent":      p.get("agent_name", ""),
            "tool":       p.get("tool", ""),
            "state":      "completed",
            "success":    p.get("success", True),
            "elapsed_ms": p.get("elapsed_ms", 0),
        })

    async def _on_system_health(self, event: Any) -> None:
        p = event.payload
        self.message_received.emit({
            "type":         "system_health",
            "signal":       p.get("signal", "continue"),
            "health_score": p.get("health_score"),
            "gap_count":    p.get("gap_count", 0),
            "gaps":         p.get("gaps", []),
        })

    # ── Outbound: Qt HUD → Kernel ─────────────────────────────────────────

    def send(self, payload: dict) -> None:
        """Route HUD actions into the kernel EventBus."""
        if self._loop is None or not self._running:
            log.warning("KernelBridge.send: loop not ready")
            return
        asyncio.run_coroutine_threadsafe(
            self._dispatch_to_kernel(payload), self._loop
        )

    async def _dispatch_to_kernel(self, payload: dict) -> None:
        from kernel.event_bus.event_bus import Event
        mtype = payload.get("type", "")

        if mtype == "chat":
            await self._bus.publish(Event(
                event_type="user.intent",
                source="hud.kernel_bridge",
                payload={
                    "text": payload.get("text", ""),
                    "session_id": "hud",
                    "tts": payload.get("tts", False),
                },
            ))

        elif mtype == "tool":
            await self._bus.publish(Event(
                event_type="tool.invoke_request",
                source="hud.kernel_bridge",
                payload={"tool": payload.get("tool"), "args": payload.get("args", {})},
            ))

        elif mtype == "memory_store":
            await self._bus.publish(Event(
                event_type="memory.store_request",
                source="hud.kernel_bridge",
                payload={"key": payload.get("key"), "value": payload.get("value")},
            ))

        elif mtype == "memory_recall":
            await self._bus.publish(Event(
                event_type="memory.recall_request",
                source="hud.kernel_bridge",
                payload={"query": payload.get("query")},
            ))

        elif mtype == "task_create":
            await self._bus.publish(Event(
                event_type="user.intent",
                source="hud.kernel_bridge",
                payload={"text": payload.get("text", ""), "session_id": "hud_tasks"},
            ))

        elif mtype == "task_list":
            # Emit current plan state via memory
            try:
                from cognition.planning.goal_manager import GoalStatus
                gm = self._orch.goal_manager
                active = await gm.by_status(GoalStatus.ACTIVE)
                plans = [{"plan_id": g.goal_id, "intent": g.title} for g in active]
                self.message_received.emit({"type": "task_list_result", "plans": plans})
            except Exception as exc:
                log.warning("task_list via kernel failed: %s", exc)

        elif mtype == "stt_audio":
            await self._bus.publish(Event(
                event_type="stt.audio_chunk",
                source="hud.kernel_bridge",
                payload={"data": payload.get("data"), "mime": payload.get("mime", "audio/wav")},
            ))

        elif mtype == "ping":
            import time
            self.message_received.emit({"type": "pong", "ts": time.time()})

        elif mtype == "model_switch":
            # Route model switch through ModelSwitcher directly (kernel mode)
            try:
                from models.switcher.model_switcher import ModelSwitcher
                switcher = ModelSwitcher.get_instance()
                provider = payload.get("provider", "groq")
                model = payload.get("model")

                async def _do_switch(_sw=switcher, _p=provider, _m=model) -> None:
                    ok = await _sw.switch(_p, _m)
                    state = _sw.get_state()
                    self.message_received.emit({"type": "model_switched", "success": ok, "state": state})

                asyncio.ensure_future(_do_switch())
            except Exception as exc:
                log.warning("KernelBridge: model_switch failed: %s", exc)

        elif mtype == "model_state":
            try:
                from models.switcher.model_switcher import ModelSwitcher
                state = ModelSwitcher.get_instance().get_state()
                self.message_received.emit({"type": "model_state", "state": state})
            except Exception as exc:
                log.warning("KernelBridge: model_state failed: %s", exc)

        elif mtype == "model_cycle":
            try:
                from models.switcher.model_switcher import ModelSwitcher
                switcher = ModelSwitcher.get_instance()

                async def _do_cycle(_sw=switcher) -> None:
                    ok = await _sw.cycle_to_next()
                    state = _sw.get_state()
                    self.message_received.emit({"type": "model_cycled", "success": ok, "state": state})

                asyncio.ensure_future(_do_cycle())
            except Exception as exc:
                log.warning("KernelBridge: model_cycle failed: %s", exc)

        elif mtype == "settings_update":
            # Settings updates in kernel mode are logged; no bus event defined yet
            log.debug("KernelBridge: settings_update received (kernel mode) — stored locally")

        else:
            log.debug("KernelBridge: unhandled outbound type '%s'", mtype)

    def stop(self) -> None:
        self._running = False
        if self._loop and not self._loop.is_closed():
            self._loop.call_soon_threadsafe(self._loop.stop)


# ─────────────────────────────────────────────────────────────────────────────
# Public API — ServerAdapter (unified, mode-agnostic)
# ─────────────────────────────────────────────────────────────────────────────

class ServerAdapter(QObject):
    """
    High-level Qt wrapper — unified interface for both kernel and WebSocket modes.

    All Qt signals are identical regardless of which backend is active.
    The HUD (main_window.py) does not need to know which mode is in use.
    """

    # ── Connection ────────────────────────────────────────────────────
    connected       = Signal()
    disconnected    = Signal()

    # ── Data signals ─────────────────────────────────────────────────
    boot_received         = Signal(dict)
    metrics_updated       = Signal(dict)
    agent_metrics_updated = Signal(dict)
    status_changed        = Signal(str)
    thinking              = Signal(str)
    chat_reply            = Signal(str, str, str)   # agent, text, provider
    # FIX: streaming signals — server sends chat_stream deltas then chat_stream_end
    chat_stream_delta     = Signal(str, str)        # agent, delta_text
    chat_stream_end       = Signal(str)             # agent
    stt_result            = Signal(str)
    stt_partial           = Signal(str)             # FIX: live STT partial transcript
    live_stt_ack          = Signal(bool)            # FIX: mic toggle acknowledgement
    tool_result           = Signal(str, str)
    memory_stats          = Signal(dict)
    memory_results        = Signal(dict)
    tts_audio             = Signal(bytes, str)      # audio_bytes, mime
    heartbeat             = Signal(int)
    settings_ack          = Signal(dict)
    knowledge_feed_status = Signal(dict)   # Phase 12: {"enabled","topics":[...],"stats":{...}}
    conversation_history  = Signal(list)   # Phase 12: [{"title","timestamp"}, ...] for sidebar
    task_created          = Signal(dict)
    task_list_result      = Signal(list)
    task_error            = Signal(str)
    pong                  = Signal(float)
    memory_ack            = Signal(dict)
    model_switched        = Signal(dict)
    # Phase 9.4 / Phase 8.4: live tool-call stream and goal-started notifications
    # from server.py's orchestrator bridge (agent.tool_call.started/completed,
    # agent.goal_started → broadcast as agent_tool_call / agent_goal_started).
    agent_tool_call       = Signal(dict)   # {"agent", "tool", "state", "elapsed_ms"?, ...}
    agent_goal_started    = Signal(dict)   # {"agent", "goal_id", "description"}
    # Phase B/D plumbing (UI upgrade doc): additive, optional structured
    # goal-result fields beyond plain chat text — e.g. FRIDAY's
    # executed/succeeded/tool, HERALD's browsed/browse_tool. Only emitted
    # when the agent's handle_goal() actually returned extra fields; does
    # not replace or alter chat_reply.
    agent_goal_result     = Signal(dict)   # {"agent", ...extra fields...}
    # Agent workflow phase steps (ATHENA research pipeline + VISION eng loop)
    # Payload: {"agent", "step_id", "label", "status": active|complete|error, "detail"}
    agent_workflow_step   = Signal(dict)
    # Phase 9.4: system health degraded notifications from ProjectIntelligence
    system_health         = Signal(dict)   # {"signal", "health_score", "gap_count", "gaps"}

    # ── Construction ──────────────────────────────────────────────────

    def __init__(
        self,
        url: str = "ws://localhost:7788/ws",
        parent: QObject | None = None,
    ) -> None:
        """Mode B (WebSocket) constructor — legacy path, unchanged."""
        super().__init__(parent)
        # If the server enforces ?token=<JARVIS_SECRET> auth (set in .env), the
        # HUD must append it to the WS URL or the connection is rejected (403).
        secret = os.getenv("JARVIS_SECRET", "")
        if secret and "/ws" in url and "token=" not in url:
            sep = "&" if "?" in url else "?"
            url = f"{url}{sep}token={secret}"
        self._url = url
        self._thread: _RecvThread | None = None
        self._bridge: _KernelBridge | None = None
        self._online = False
        self._mode = "websocket"

    @classmethod
    def from_kernel(
        cls, orchestrator: Any, parent: QObject | None = None
    ) -> "ServerAdapter":
        """
        Mode A (kernel) factory constructor.
        Pass the live Orchestrator instance; no WebSocket needed.

        Example:
            adapter = ServerAdapter.from_kernel(orchestrator)
            window = JarvisWindow(server_adapter=adapter)
        """
        inst = cls.__new__(cls)
        QObject.__init__(inst, parent)
        inst._url = ""
        inst._thread = None
        inst._online = False
        inst._mode = "kernel"
        inst._bridge = _KernelBridge(orchestrator, inst)
        inst._bridge.message_received.connect(inst._dispatch)
        inst._bridge.connected.connect(inst._on_connected)
        inst._bridge.disconnected.connect(inst._on_disconnected)
        return inst

    # ── Connection management ─────────────────────────────────────────

    def connect_to_server(self) -> None:
        if self._mode == "kernel":
            if self._bridge and not (self._bridge._thread and self._bridge._thread.is_alive()):
                self._bridge.start()
        else:
            if self._thread and self._thread.isRunning():
                return
            self._thread = _RecvThread(self._url, self)
            self._thread.message_received.connect(self._dispatch)
            self._thread.connected.connect(self._on_connected)
            self._thread.disconnected.connect(self._on_disconnected)
            self._thread.start()

    def disconnect_from_server(self) -> None:
        if self._mode == "kernel":
            if self._bridge:
                self._bridge.stop()
        else:
            if self._thread:
                self._thread.stop()
                self._thread.wait(2000)

    @property
    def is_online(self) -> bool:
        return self._online

    @property
    def mode(self) -> str:
        """Returns 'kernel' or 'websocket'."""
        return self._mode

    # ── Send helpers (identical API regardless of mode) ───────────────

    def send_chat(self, text: str, agent: str = "oracle", tts: bool = False,
                  files: list[str] | None = None, stream: bool = False) -> None:
        """
        Phase 10.1: `stream=True` tells server.py to use ModelRouter.stream()
        and send chat_stream / chat_stream_end WS messages instead of a single
        chat_reply.  Only effective when Groq is the active provider; server.py
        falls back to non-streaming for other providers automatically.
        """
        payload: dict = {"type": "chat", "text": text, "agent": agent, "tts": tts}
        if stream:
            payload["stream"] = True
        if files:
            payload["files"] = files
        self._send(payload)

    def send_files(self, files: list[str], text: str = "", agent: str = "oracle") -> None:
        self.send_chat(text, agent=agent, files=files)

    def send_ping(self) -> None:
        self._send({"type": "ping"})

    def send_tool(self, tool: str, args: dict) -> None:
        self._send({"type": "tool", "tool": tool, "args": args})

    def send_stt_audio(self, b64_data: str, mime: str = "audio/webm") -> None:
        self._send({"type": "stt_audio", "data": b64_data, "mime": mime})

    def recall_memory(self, query: str) -> None:
        self._send({"type": "memory_recall", "query": query})

    def store_memory(self, key: str, value: str) -> None:
        self._send({"type": "memory_store", "key": key, "value": value})

    def create_task(self, intent: str) -> None:
        self._send({"type": "task_create", "text": intent})

    def list_tasks(self) -> None:
        self._send({"type": "task_list"})

    def send_settings_update(self, settings: dict) -> None:
        self._send({"type": "settings_update", "settings": settings})

    def send_knowledge_feed_action(self, action: dict) -> None:
        """Phase 12: add/remove a watch topic, toggle enabled, or trigger an
        immediate refresh. Sent as its own message type — deliberately not
        piggy-backed on settings_update/settings_changed, whose payload
        always gets wrapped as {"type": "settings_update", "settings": ...}
        regardless of any "type" key inside the dict passed to it, which
        made similar in-band signalling (e.g. the Test TTS button) silently
        unroutable server-side."""
        self._send({"type": "knowledge_feed_action", **action})

    def request_knowledge_feed_status(self) -> None:
        self._send({"type": "knowledge_feed_get"})

    def request_conversation_history(self, limit: int = 10) -> None:
        self._send({"type": "conversation_history_get", "limit": limit})

    def send_model_switch(self, provider: str, model: str | None = None) -> None:
        """Send a model switch request to the backend.

        Server-side parsing rules (server.py model_switch handler):
          kind="ollama" -> provider="ollama",  model=msg["model"] (Ollama tag)
          kind="local"  -> provider=msg["provider"],              model=None  (OpenVINO)
          kind="cloud"  -> provider=msg["model"] (provider name), model=None
        """
        if provider == "ollama":
            payload: dict = {"type": "model_switch", "kind": "ollama", "model": model or ""}
        elif provider in ("openvino", "qwen_openvino"):
            # Server reads provider from msg["provider"] for local non-Ollama
            payload = {"type": "model_switch", "kind": "local", "provider": provider}
        else:
            # Cloud (groq, gemini …): server reads provider name from msg["model"]
            payload = {"type": "model_switch", "kind": "cloud", "model": provider}
        self._send(payload)

    def send_model_state(self) -> None:
        """Request the current model state from the backend."""
        self._send({"type": "model_state"})

    def send_model_cycle(self) -> None:
        """Request the backend to cycle to the next provider."""
        self._send({"type": "model_cycle"})

    def _send(self, payload: dict) -> None:
        if self._mode == "kernel":
            if self._bridge:
                self._bridge.send(payload)
        else:
            if self._thread:
                self._thread.send(payload)

    # ── Internal ──────────────────────────────────────────────────────

    @Slot()
    def _on_connected(self) -> None:
        self._online = True
        self.connected.emit()
        log.info("ServerAdapter connected (mode=%s)", self._mode)

    @Slot()
    def _on_disconnected(self) -> None:
        self._online = False
        self.disconnected.emit()
        log.info("ServerAdapter disconnected (mode=%s)", self._mode)

    @Slot(dict)
    def _dispatch(self, msg: dict) -> None:
        mtype = msg.get("type", "")

        if mtype == "boot":
            self.boot_received.emit(msg)
        elif mtype == "metrics":
            self.metrics_updated.emit(msg)
        elif mtype == "agent_metrics":
            self.agent_metrics_updated.emit(msg)
        elif mtype == "settings_ack":
            self.settings_ack.emit(msg.get("settings", {}))
        elif mtype == "knowledge_feed_status":
            self.knowledge_feed_status.emit(msg.get("data", {}))
        elif mtype == "conversation_history":
            self.conversation_history.emit(msg.get("items", []))
        elif mtype == "status":
            self.status_changed.emit(msg.get("text", ""))
        elif mtype == "thinking":
            self.thinking.emit(msg.get("agent", ""))
        elif mtype == "chat_reply":
            self.chat_reply.emit(
                msg.get("agent", ""),
                msg.get("text", ""),
                msg.get("provider", ""),
            )
        elif mtype == "stt_result":
            self.stt_result.emit(msg.get("transcript", ""))
        elif mtype == "tool_result":
            self.tool_result.emit(msg.get("tool", ""), msg.get("result", ""))
        elif mtype == "memory_stats":
            self.memory_stats.emit(msg)
        elif mtype == "memory_results":
            self.memory_results.emit(msg)
        elif mtype == "task_created":
            self.task_created.emit(msg)
        elif mtype == "task_list_result":
            self.task_list_result.emit(msg.get("plans", []))
        elif mtype == "task_error":
            self.task_error.emit(msg.get("error", "Unknown error"))
        elif mtype == "tts_audio":
            import base64
            try:
                raw = base64.b64decode(msg.get("b64", ""))
                self.tts_audio.emit(raw, msg.get("mime", "audio/mp3"))
            except Exception as e:
                log.debug("ws_client: Failed to decode/emit TTS audio: %s", e)
        elif mtype == "heartbeat":
            self.heartbeat.emit(int(msg.get("uptime", 0)))
        elif mtype == "pong":
            self.pong.emit(float(msg.get("ts", 0.0)))
        elif mtype == "memory_ack":
            self.memory_ack.emit(msg)
        # FIX: streaming message types sent by server.py when stream=True
        elif mtype == "chat_stream":
            self.chat_stream_delta.emit(
                msg.get("agent", ""),
                msg.get("delta", ""),
            )
        elif mtype == "chat_stream_end":
            self.chat_stream_end.emit(msg.get("agent", ""))
        # FIX: Live STT partial transcript from mic_chunk path
        elif mtype == "stt_partial":
            self.stt_partial.emit(msg.get("text", ""))
        # FIX: Live STT toggle acknowledgement
        elif mtype == "live_stt_ack":
            self.live_stt_ack.emit(bool(msg.get("active", False)))
        elif mtype == "model_switched":
            self.model_switched.emit(msg.get("state", {}))
        elif mtype == "model_state":
            self.model_switched.emit(msg.get("state", {}))
        elif mtype == "model_cycled":
            # Cycle response — treat identically to model_switched (state updated)
            self.model_switched.emit(msg.get("state", {}))
        # Phase 9.4 / Phase 8.4: live tool-call stream from orchestrator bridge
        elif mtype == "agent_tool_call":
            self.agent_tool_call.emit(msg)
        elif mtype == "agent_goal_started":
            self.agent_goal_started.emit(msg)
        elif mtype == "agent_goal_result":
            self.agent_goal_result.emit(msg)
        elif mtype == "agent_workflow_step":
            self.agent_workflow_step.emit(msg)
        # Phase 9.4: system health signal from ProjectIntelligence
        elif mtype == "system_health":
            self.system_health.emit(msg)
        # Silently discard known informational types to avoid log noise
        elif mtype in ("reflection_trigger", "chat_stream_error"):
            log.debug("ws_client: received informational type '%s' — no handler", mtype)
        elif mtype == "error":
            # Server-side error (e.g. rate limit exceeded) — surface as status message
            log.warning("ws_client: server error: %s", msg.get("text", ""))
            self.status_changed.emit(f"⚠ {msg.get('text', 'Server error')}")