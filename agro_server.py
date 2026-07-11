#!/usr/bin/env python3
"""
AGRO STANDALONE SERVER  — Phase 12 (Customer TTS Push + Job Request Actions)
=====================================================
Lightweight FastAPI + WebSocket backend for the Flutter agro_operator app.
Zero dependency on JARVIS AI OS — no STT, no LLM, no orchestrator.

All it does:
  • SQLite database (aiosqlite)       — jobs, fuel, expenses, customers, operators
  • WebSocket API (/ws)               — Flutter app talks over this
  • HTTP REST fallback (/api/*)       — useful for debugging, ngrok tunnels, etc.
  • Excel export (openpyxl)           — daily / monthly reports to datastore/exports/
  • Bilingual TTS (edge_tts / kokoro) — speaks job confirmations in Nepali OR English

New in Phase 11.1:
  • set_language WS handler  — Flutter app pushes "ne"/"en" on startup + toggle.
  • _agro_speak()            — synthesises a clip and sends {type:"tts_audio"} to app.
  • Nepali voice: ne-NP-HemkalaNeural (edge_tts)
  • English voice: en-US-AndrewMultilingualNeural (edge_tts)
  • Kokoro (onnx) used as fallback when edge_tts is unavailable.

WebSocket message format (unchanged from original):
  App → Server:  {"type": "agro",         "action": "<action>", "data": {...}}
  Server → App:  {"type": "agro_result",  "action": "<action>", "data": {...}}
  Server → App:  {"type": "agro_ack",     "action": "<action>", "status": "queued|error"}
  App → Server:  {"type": "ping"}
  Server → App:  {"type": "pong",         "ts": <unix_ts>}
  App → Server:  {"type": "set_language", "language": "ne"|"en"}   ← NEW
  Server → App:  {"type": "tts_audio",    "b64": "...", "mime": "audio/mpeg",
                  "text": "...", "source": "agro"}                  ← NEW

Setup:
  pip install fastapi "uvicorn[standard]" aiosqlite openpyxl python-dotenv edge-tts
  # Optional kokoro fallback:
  pip install kokoro-onnx soundfile numpy
  python agro_server.py

.env (optional overrides):
  AGRO_PORT=7788
  AGRO_HOST=0.0.0.0
  AGRO_SECRET=your-token-here     # if set, Flutter must pass ?token=<secret>
  AGRO_TTS=edge_tts               # edge_tts | kokoro | none
  AGRO_VOICE_NE=ne-NP-HemkalaNeural
  AGRO_VOICE_EN=en-US-AndrewMultilingualNeural
"""
from __future__ import annotations

import asyncio
import base64
import calendar
import io
import json
import logging
import os
import tempfile
import time
from contextlib import asynccontextmanager
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Any

from dotenv import load_dotenv
from fastapi import FastAPI, Request, WebSocket, WebSocketDisconnect, HTTPException, Query
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles

# ── Load .env ─────────────────────────────────────────────────────────────────
load_dotenv(Path(__file__).parent / ".env")

PORT   = int(os.getenv("AGRO_PORT", os.getenv("JARVIS_PORT", "7788")))
HOST   = os.getenv("AGRO_HOST", "0.0.0.0")
# BUGFIX: the rest of the JARVIS .env uses the JARVIS_* naming convention
# (JARVIS_SECRET, JARVIS_PORT), but this file originally only looked for
# AGRO_SECRET/AGRO_PORT. If your .env has JARVIS_SECRET set (as the shipped
# .env does) but no AGRO_SECRET, SECRET silently ended up as "" and the
# server accepted ANY token (or no token at all) over the WS endpoint —
# the Flutter apps' hardcoded token was therefore never actually checked.
# Accept either name so both conventions work, preferring AGRO_SECRET.
SECRET = os.getenv("AGRO_SECRET") or os.getenv("JARVIS_SECRET", "")  # empty = no auth

# TTS config — read from env, fall back to sensible defaults.
TTS_BACKEND     = os.getenv("AGRO_TTS", "edge_tts").lower()   # edge_tts | kokoro | none
VOICE_NE        = os.getenv("AGRO_VOICE_NE", "ne-NP-HemkalaNeural")
VOICE_EN        = os.getenv("AGRO_VOICE_EN", "en-US-AndrewMultilingualNeural")

# STT config — mirrors server.py's STTEngine setup. The engine works fine
# with bus=None (transcribe_blob() is a plain request/response call, no
# EventBus round-trip needed) — see perception/speech/stt.py.
GROQ_API_KEY        = os.getenv("GROQ_API_KEY", "")
FASTER_WHISPER_MODEL = os.getenv("AGRO_STT_LOCAL_MODEL", "small")

# ── Logging ───────────────────────────────────────────────────────────────────
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s — %(message)s",
    datefmt="%H:%M:%S",
)
log = logging.getLogger("agro.server")

# ── Import agro business logic ────────────────────────────────────────────────
from agents.agro import database as db
from agents.agro import analytics
from agents.agro import expense_manager as em
from agents.agro import job_manager as jm
from agents.agro import customer_portal as cp
from agents.agro import backup_manager as bkp
from agents.agro.excel_exporter import AgroExcelExporter as xl
from agents.agro.constants import (
    AGRI_SERVICES, TRANSPORT_MATERIALS, LAND_UNITS, TRANSPORT_UNITS,
    JOB_STATUSES, FUEL_TYPES, EXPENSE_CATEGORIES,
)


# ─────────────────────────────────────────────────────────────────────────────
# Runtime language state
# ─────────────────────────────────────────────────────────────────────────────

# Defaults to Nepali because the app targets Nawal Parasi operators.
# The Flutter TtsPlaybackService will push the correct language on startup,
# so this is only the boot-time default until the first WS connection.
_tts_language: str = "ne"   # "ne" | "en"


def _get_language() -> str:
    return _tts_language


def _set_language(lang: str) -> None:
    global _tts_language
    if lang in ("ne", "en"):
        _tts_language = lang
        log.info("TTS language set to %r", lang)


# ─────────────────────────────────────────────────────────────────────────────
# Customer WebSocket registry — maps customer_id → active WebSocket
# so the operator-side TTS push can reach the right customer connection.
# Cleared automatically on disconnect.  Multiple connections from the
# same customer_id are allowed; all receive the push.
CUSTOMER_WS: dict[int, list] = {}   # customer_id → [WebSocket, ...]

# Customer session tokens — agro_client (customer-facing app)
# ─────────────────────────────────────────────────────────────────────────────
# In-memory only (cleared on server restart, which is fine — the customer's
# Flutter app just re-sends phone+pin to log in again). Maps an opaque token
# (returned by customer_portal.verify_customer_login) to the customer_id, so
# subsequent "customer" WS messages don't need to resend the PIN.
CUSTOMER_SESSIONS: dict[str, int] = {}


# ─────────────────────────────────────────────────────────────────────────────
# TTS synthesis — edge_tts primary, kokoro secondary
# ─────────────────────────────────────────────────────────────────────────────

async def _synthesize_edge_tts(text: str, voice: str) -> bytes | None:
    """
    Synthesise `text` using edge_tts and return raw MP3 bytes.
    Returns None if edge_tts is not installed or synthesis fails.
    """
    try:
        import edge_tts  # type: ignore
        communicate = edge_tts.Communicate(text=text, voice=voice)
        buf = io.BytesIO()
        async for chunk in communicate.stream():
            if chunk["type"] == "audio":
                buf.write(chunk["data"])
        audio_bytes = buf.getvalue()
        if not audio_bytes:
            return None
        return audio_bytes
    except ImportError:
        log.warning("edge_tts not installed — install with: pip install edge-tts")
        return None
    except Exception as exc:
        log.warning("edge_tts synthesis failed: %s", exc)
        return None


async def _synthesize_kokoro(text: str, lang: str) -> bytes | None:
    """
    Synthesise `text` using the Kokoro ONNX runtime.
    Returns WAV bytes encoded as MP3 (via soundfile→mp3 conversion), or
    falls back to raw WAV bytes if mp3 conversion is unavailable.
    Returns None if kokoro-onnx is not installed or synthesis fails.
    """
    try:
        from kokoro_onnx import Kokoro  # type: ignore
        import numpy as np
        import soundfile as sf

        # Kokoro uses short voice codes; pick gender-neutral voices.
        voice_code = "ne" if lang == "ne" else "en-us"
        kokoro = Kokoro("kokoro-v0_19.onnx", "voices.bin")
        samples, sample_rate = kokoro.create(text, voice=voice_code, speed=1.0, lang=voice_code)

        # Write to in-memory WAV.
        wav_buf = io.BytesIO()
        sf.write(wav_buf, samples, sample_rate, format="WAV")
        wav_bytes = wav_buf.getvalue()
        return wav_bytes
    except ImportError:
        log.warning("kokoro-onnx not installed — install with: pip install kokoro-onnx soundfile")
        return None
    except Exception as exc:
        log.warning("Kokoro synthesis failed: %s", exc)
        return None


async def _synthesize(text: str) -> tuple[bytes, str] | None:
    """
    Synthesise `text` in the current TTS language.
    Returns (audio_bytes, mime_type) or None if all engines fail.
    """
    if TTS_BACKEND == "none":
        return None

    lang  = _get_language()
    voice = VOICE_NE if lang == "ne" else VOICE_EN

    # Primary: edge_tts
    if TTS_BACKEND in ("edge_tts", "auto"):
        audio = await _synthesize_edge_tts(text, voice)
        if audio:
            return audio, "audio/mpeg"

    # Secondary: kokoro ONNX
    audio = await _synthesize_kokoro(text, lang)
    if audio:
        # soundfile writes WAV; report correct mime so audioplayers decodes it.
        mime = "audio/wav"
        return audio, mime

    log.warning("All TTS engines failed for text=%r", text[:60])
    return None


# ─────────────────────────────────────────────────────────────────────────────
# Job voice text builder — bilingual
# ─────────────────────────────────────────────────────────────────────────────

def _agro_job_summary_text(job: dict) -> str:
    """
    Build a natural-language spoken summary of one agro job.
    Language is picked from the current _tts_language setting.
    """
    lang     = _get_language()
    job_type = str(job.get("job_type") or "agriculture").strip()
    service  = str(job.get("service") or job.get("material") or "job").strip()
    customer = str(job.get("customer_name") or job.get("customer") or "").strip()

    # ── Opening line ──────────────────────────────────────────────────────
    if lang == "ne":
        opener = f"नयाँ {job_type} काम"
        if customer:
            opener += f" {customer} को लागि"
        opener += f", सेवा {service}।"
    else:
        opener = f"New {job_type} job"
        if customer:
            opener += f" for {customer}"
        opener += f", service {service}."

    parts = [opener]

    # ── Quantity / area / time ────────────────────────────────────────────
    area_val = job.get("area_value")
    qty_val  = job.get("quantity_value")
    time_val = job.get("time_taken") or job.get("time_value")

    if area_val is not None:
        unit = job.get("area_unit") or ""
        parts.append(
            f"क्षेत्रफल: {area_val} {unit}।".strip() if lang == "ne"
            else f"Area: {area_val} {unit}.".strip()
        )
    elif qty_val is not None:
        unit = job.get("quantity_unit") or ""
        parts.append(
            f"परिमाण: {qty_val} {unit}।".strip() if lang == "ne"
            else f"Quantity: {qty_val} {unit}.".strip()
        )
    elif time_val is not None:
        unit = job.get("time_unit") or ""
        parts.append(
            f"समय: {time_val} {unit}।".strip() if lang == "ne"
            else f"Duration: {time_val} {unit}.".strip()
        )

    # ── Location ──────────────────────────────────────────────────────────
    location = str(job.get("location") or "").strip()
    if location:
        parts.append(
            f"स्थान: {location}।" if lang == "ne"
            else f"Location: {location}."
        )

    # ── Financials ────────────────────────────────────────────────────────
    rate = job.get("rate") or job.get("rate_per_unit")
    if rate is not None:
        parts.append(
            f"दर: {rate} रुपैयाँ।" if lang == "ne"
            else f"Rate: {rate} rupees."
        )

    total = job.get("total_amount")
    if total is not None:
        parts.append(
            f"कुल रकम: {total} रुपैयाँ।" if lang == "ne"
            else f"Total amount: {total} rupees."
        )

    advance = job.get("advance_paid")
    if advance:
        parts.append(
            f"अग्रिम भुक्तानी: {advance} रुपैयाँ।" if lang == "ne"
            else f"Advance paid: {advance} rupees."
        )

    balance = job.get("balance_due")
    if balance is not None and float(balance) > 0:
        parts.append(
            f"बाँकी रकम: {balance} रुपैयाँ।" if lang == "ne"
            else f"Balance due: {balance} rupees."
        )

    # ── Scheduled date ────────────────────────────────────────────────────
    sched = job.get("scheduled_date")
    if sched:
        parts.append(
            f"{sched} को लागि तोकिएको।" if lang == "ne"
            else f"Scheduled for {sched}."
        )

    return " ".join(p for p in parts if p)


def _build_agro_voice_text(action: str, data: dict, result: dict) -> str | None:
    """
    Decide what (if anything) should be spoken for one agro WS result.
    Returns None to stay silent.
    """
    if action not in ("log_job", "update_job"):
        return None
    if not isinstance(result, dict):
        return None
    if not result.get("success", True):
        return None   # never announce failures

    lang = _get_language()

    # Merge incoming data with what the server wrote (server version wins).
    job = result.get("job") if isinstance(result.get("job"), dict) else None
    if job is None and isinstance(result.get("data"), dict):
        job = result["data"]
    overlay  = job or {k: v for k, v in result.items() if k not in ("success", "message")}
    merged   = {**data, **overlay}

    summary = _agro_job_summary_text(merged)

    if action == "log_job":
        confirm = "काम दर्ता भयो।" if lang == "ne" else "Job registered."
        return f"{summary} {confirm}"

    # update_job
    new_status = str(merged.get("status") or "").strip().lower()
    if new_status == "completed":
        confirm = "काम सम्पन्न भयो।" if lang == "ne" else "Job completed."
        return f"{summary} {confirm}"
    if new_status:
        label = "काम अपडेट भयो। स्थिति:" if lang == "ne" else "Job updated. Status:"
        return f"{summary} {label} {new_status}."
    label = "काम अपडेट भयो।" if lang == "ne" else "Job updated."
    return f"{summary} {label}"


# ─────────────────────────────────────────────────────────────────────────────
# TTS send helper
# ─────────────────────────────────────────────────────────────────────────────

async def _notify_customer_tts(customer_id: int, text: str) -> None:
    """
    Synthesise `text` and push a tts_audio message to ALL open WebSocket
    connections belonging to `customer_id`.  Used when the operator accepts
    or declines a job request so the customer's agro_client app speaks the
    confirmation aloud.  Non-blocking — errors are logged, never raised.
    """
    if not text or TTS_BACKEND == "none":
        return
    sockets = CUSTOMER_WS.get(customer_id, [])
    if not sockets:
        log.info("_notify_customer_tts: customer %s not connected, skipping TTS", customer_id)
        return
    try:
        result = await _synthesize(text)
        if result is None:
            return
        audio_bytes, mime = result
        b64 = base64.b64encode(audio_bytes).decode("ascii")
        payload = json.dumps({
            "type":   "tts_audio",
            "b64":    b64,
            "mime":   mime,
            "text":   text,
            "source": "agro_notification",
        }, default=str)
        dead = []
        for ws in list(sockets):
            try:
                await ws.send_text(payload)
            except Exception:
                dead.append(ws)
        for ws in dead:
            if ws in CUSTOMER_WS.get(customer_id, []):
                CUSTOMER_WS[customer_id].remove(ws)
        log.info("TTS→customer %s sent: %.60s…", customer_id, text)
    except Exception as exc:
        log.warning("_notify_customer_tts failed: %s", exc)


async def _push_to_customer(customer_id: int, message: dict) -> None:
    """
    Send a raw JSON message to ALL open WebSocket connections belonging to
    `customer_id`. Used to push live data updates (refreshed job_requests
    or jobs lists) so the agro_client app reflects operator actions
    (accept/decline/status change) immediately, without the customer having
    to manually pull-to-refresh.
    """
    sockets = CUSTOMER_WS.get(customer_id, [])
    if not sockets:
        return
    payload = json.dumps(message, default=str)
    dead = []
    for ws in list(sockets):
        try:
            await ws.send_text(payload)
        except Exception:
            dead.append(ws)
    for ws in dead:
        if ws in CUSTOMER_WS.get(customer_id, []):
            CUSTOMER_WS[customer_id].remove(ws)


async def _push_customer_job_requests(customer_id: int) -> None:
    """Push a fresh job_requests list to the customer (live UI update)."""
    try:
        requests = await cp.get_job_requests_for_customer(customer_id)
        await _push_to_customer(customer_id, {
            "type": "customer_result", "action": "get_job_requests",
            "data": {"success": True, "requests": requests},
        })
    except Exception as exc:
        log.warning("_push_customer_job_requests failed: %s", exc)


async def _push_customer_jobs(customer_id: int) -> None:
    """Push a fresh jobs list to the customer (live UI update, e.g. on status change)."""
    try:
        jobs = await cp.get_jobs_for_customer(customer_id)
        await _push_to_customer(customer_id, {
            "type": "customer_result", "action": "get_jobs",
            "data": {"success": True, "jobs": jobs},
        })
    except Exception as exc:
        log.warning("_push_customer_jobs failed: %s", exc)


async def _agro_speak(ws: WebSocket, text: str) -> None:
    """
    Synthesise `text`, base64-encode it, and send a tts_audio message to the
    Flutter client.  Fires-and-forgets — a synthesis failure is logged but
    never surfaces to the caller.
    """
    if not text or TTS_BACKEND == "none":
        return
    try:
        result = await _synthesize(text)
        if result is None:
            return
        audio_bytes, mime = result
        b64 = base64.b64encode(audio_bytes).decode("ascii")
        await manager.send(ws, {
            "type":   "tts_audio",
            "b64":    b64,
            "mime":   mime,
            "text":   text,
            "source": "agro",
        })
        log.info("TTS sent: lang=%s bytes=%d text=%.60s…", _get_language(), len(audio_bytes), text)
    except Exception as exc:
        log.warning("_agro_speak failed: %s", exc)


# ─────────────────────────────────────────────────────────────────────────────
# Connection manager — keep track of connected Flutter clients
# ─────────────────────────────────────────────────────────────────────────────

class ConnectionManager:
    def __init__(self):
        self._clients: list[WebSocket] = []

    async def connect(self, ws: WebSocket) -> None:
        await ws.accept()
        self._clients.append(ws)
        log.info(f"Client connected  — total: {len(self._clients)}")

    def disconnect(self, ws: WebSocket) -> None:
        if ws in self._clients:
            self._clients.remove(ws)
        log.info(f"Client disconnected — total: {len(self._clients)}")

    async def send(self, ws: WebSocket, msg: dict) -> None:
        try:
            await ws.send_text(json.dumps(msg, default=str))
        except Exception:
            pass

    async def broadcast(self, msg: dict) -> None:
        dead = []
        for ws in self._clients:
            try:
                await ws.send_text(json.dumps(msg, default=str))
            except Exception:
                dead.append(ws)
        for ws in dead:
            self._clients.remove(ws)


# ─────────────────────────────────────────────────────────────────────────────
# Auto-generated reports — daily at 23:59 Nepal time, monthly on month-end
# ─────────────────────────────────────────────────────────────────────────────
#
# "Nepal time" specifically (not server local time) because this server may
# be hosted anywhere (a VPS, a laptop behind a tunnel, etc.) while the
# business itself runs on Nepal time. Using date.today()/datetime.now()
# directly would fire at the wrong wall-clock moment — and worse, would
# label the report with the WRONG DATE — whenever the server's local
# timezone differs from Asia/Kathmandu (UTC+5:45, no DST, so a fixed offset
# is correct and doesn't need a tzdata lookup to stay right).
NEPAL_UTC_OFFSET = timedelta(hours=5, minutes=45)


def _nepal_now() -> datetime:
    return datetime.utcnow() + NEPAL_UTC_OFFSET


async def _broadcast_new_job_request(request_id: int, customer_id: int) -> None:
    """Push a `new_job_request` event to every connected OPERATOR app the
    moment a customer submits a request, instead of leaving it to sit until
    someone opens the Customer Requests screen. Mirrors _broadcast_report_ready's
    shape/pattern below. Only reaches operators whose app is currently open
    and connected to this WebSocket — there's no phone-off/app-closed push
    here, that would need FCM/APNs wiring on top of this."""
    try:
        pending = await cp.get_pending_job_requests()
        match = next((r for r in pending if r.get("id") == request_id), None)
        service = (match or {}).get("service", "")
        customer_name = (match or {}).get("customer_name", "")
    except Exception:
        service, customer_name = "", ""

    msg = {
        "type": "new_job_request",
        "request_id": request_id,
        "customer_id": customer_id,
        "customer_name": customer_name,
        "service": service,
    }
    await manager.broadcast(msg)

    lang = _get_language()
    if lang == "ne":
        text = f"{customer_name} बाट नयाँ काम अनुरोध आयो।" if customer_name else "नयाँ काम अनुरोध आयो।"
    else:
        text = f"New job request from {customer_name}." if customer_name else "New job request received."
    for ws in list(manager._clients):
        asyncio.create_task(_agro_speak(ws, text))


async def _broadcast_report_ready(report_type: str, period: str) -> None:
    """Push a report_ready event + a short TTS confirmation to every
    connected operator app. Flutter builds the actual download URL itself
    from AppConfig.serverUrl + this period, so this payload only needs to
    say WHAT is ready, not WHERE — the server doesn't reliably know its own
    public tunnel URL outside of a live request (see root() for why)."""
    msg = {"type": "report_ready", "report_type": report_type, "period": period}
    await manager.broadcast(msg)

    lang = _get_language()
    if report_type == "daily":
        text = "आजको रिपोर्ट तयार भयो।" if lang == "ne" else "Today's report is ready."
    else:
        text = "यस महिनाको रिपोर्ट तयार भयो।" if lang == "ne" else "This month's report is ready."
    for ws in list(manager._clients):
        asyncio.create_task(_agro_speak(ws, text))


async def _generate_and_announce_daily(d: str) -> None:
    try:
        stats = await db.get_daily_stats(d)
        jobs  = await db.get_jobs(date=d)
        await xl.generate_daily_report(stats, jobs, d)
        log.info("Auto-generated daily report for %s", d)
        await _broadcast_report_ready("daily", d)
    except Exception:
        log.exception("Auto daily report generation failed for %s", d)


async def _generate_and_announce_monthly(m: str) -> None:
    try:
        summary    = await analytics.monthly_summary(m)
        daily_raw  = summary.get("daily", [])
        all_jobs   = await db.get_jobs(limit=1000)
        month_jobs = [j for j in all_jobs if (j.get("scheduled_date") or "").startswith(m)]
        await xl.generate_monthly_report(
            month=m, daily_stats=daily_raw, all_jobs=month_jobs, all_expenses=[]
        )
        log.info("Auto-generated monthly report for %s", m)
        await _broadcast_report_ready("monthly", m)
    except Exception:
        log.exception("Auto monthly report generation failed for %s", m)


async def _report_scheduler() -> None:
    """
    Background loop: sleeps until 23:59:00 Nepal time, generates the daily
    report for the day that's ending, and — if that day is the last day of
    the month — also generates the monthly report. Runs forever; any single
    generation failure is logged and swallowed so one bad day never kills
    the scheduler for the rest of the month.
    """
    while True:
        now = _nepal_now()
        target = now.replace(hour=23, minute=59, second=0, microsecond=0)
        if now >= target:
            target += timedelta(days=1)
        await asyncio.sleep((target - now).total_seconds())

        report_date = target.strftime("%Y-%m-%d")
        await _generate_and_announce_daily(report_date)
        await bkp.run_nightly_backup(report_date)

        last_day_of_month = calendar.monthrange(target.year, target.month)[1]
        if target.day == last_day_of_month:
            await _generate_and_announce_monthly(target.strftime("%Y-%m"))

        # Push past the minute we just fired in so a slightly-early wakeup
        # (clock drift, sleep() rounding) can't cause the loop to fire twice.
        await asyncio.sleep(61)


manager = ConnectionManager()

# ── STT engine — same STTEngine class server.py uses, instantiated here
# because agro_server.py is its own standalone process (not merged into
# server.py's WS loop), so it needs its own instance. bus=None is fine —
# transcribe_blob() never touches the EventBus.
STT_ENGINE = None


# ─────────────────────────────────────────────────────────────────────────────
# App lifecycle
# ─────────────────────────────────────────────────────────────────────────────

@asynccontextmanager
async def lifespan(app: FastAPI):
    global STT_ENGINE
    log.info("AGRO Standalone Server starting … (TTS backend: %s)", TTS_BACKEND)
    await db.init_db()
    await cp.init_customer_portal()  # additive: job_requests table + customers.pin_hash
    log.info("Database ready: %s", Path(db.DB_PATH).resolve())
    try:
        from perception.speech.stt import STTEngine, STTConfig
        STT_ENGINE = STTEngine(
            bus=None,
            config=STTConfig(
                groq_api_key=GROQ_API_KEY,
                language="",  # auto-detect en/hi/ne
                local_model=FASTER_WHISPER_MODEL,
            ),
        )
        STT_ENGINE.start()
        log.info("STTEngine ready (mic button, groq_available=%s)", bool(GROQ_API_KEY))
    except Exception as e:
        log.error("STTEngine init failed — mic button will not work: %s", e)
        STT_ENGINE = None
    scheduler_task = asyncio.create_task(_report_scheduler())
    log.info("Auto-report scheduler started (daily 23:59 NPT, monthly on month-end)")
    log.info("Server live at ws://%s:%s/ws", HOST, PORT)
    yield
    scheduler_task.cancel()
    log.info("AGRO Server shut down.")


app = FastAPI(title="AGRO Standalone Backend", version="1.1.0", lifespan=lifespan)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)


# ─────────────────────────────────────────────────────────────────────────────
# Action dispatcher — the heart of the server
# ─────────────────────────────────────────────────────────────────────────────

async def dispatch(action: str, data: dict) -> dict[str, Any]:
    """
    Route an agro action to the correct business logic and return a result dict.
    All exceptions are caught by the caller and returned as {success: False}.
    """
    today = date.today().isoformat()

    # ── Jobs ──────────────────────────────────────────────────────────────
    if action == "log_job":
        job = await jm.create_job(data)
        return {
            "success": True,
            "job_id":  job["job_id"],
            "message": f"Job #{job['job_id']} logged successfully.",
            **job,
        }

    elif action == "update_job":
        job_id = data.get("job_id")
        status = data.get("status", "")
        signature_name = data.get("signature_name")
        if not job_id or not status:
            return {"success": False, "message": "job_id and status are required."}
        result = await jm.update_job_status(int(job_id), status, signature_name=signature_name)

        # Let the customer know their job's status changed (e.g. confirmed →
        # in_progress → completed), instead of leaving them to guess by
        # manually refreshing the agro_client app.
        job_row = await db.get_job_by_id(int(job_id))
        cid = job_row.get("customer_id") if job_row else None
        if cid:
            lang = _get_language()
            service = (job_row.get("service") or job_row.get("job_type") or "").strip()
            status_labels_ne = {
                "confirmed": "स्वीकृत", "in_progress": "सुरु भयो",
                "completed": "सम्पन्न भयो", "cancelled": "रद्द भयो",
            }
            status_labels_en = {
                "confirmed": "confirmed", "in_progress": "now in progress",
                "completed": "completed", "cancelled": "cancelled",
            }
            if lang == "ne":
                label = status_labels_ne.get(status, status)
                tts_text = f"तपाईंको {service} काम {label}।"
            else:
                label = status_labels_en.get(status, status)
                tts_text = f"Your {service} job is {label}."
            asyncio.create_task(_notify_customer_tts(int(cid), tts_text))
            asyncio.create_task(_push_customer_jobs(int(cid)))

        return {"success": True, "message": f"Job #{job_id} → {status}", **result}

    elif action == "update_job_time":
        # Called by the Flutter operator app when a live timer is stopped.
        # Patches time_value + total_amount on the job row.
        job_id     = data.get("job_id")
        time_value = data.get("time_value")
        time_unit  = data.get("time_unit", "Minute")
        total_amount = data.get("total_amount")
        if not job_id or time_value is None:
            return {"success": False, "message": "job_id and time_value are required."}
        result = await db.update_job_time(
            int(job_id),
            float(time_value),
            time_unit,
            float(total_amount) if total_amount is not None else None,
        )
        return result

    elif action == "get_jobs":
        result = await jm.get_jobs_summary(
            date=data.get("date"),
            status=data.get("status"),
            job_type=data.get("job_type"),
            limit=int(data.get("limit", 50)),
        )
        return {"success": True, **result}

    elif action == "get_job":
        job = await db.get_job_by_id(int(data.get("job_id", 0)))
        if job is None:
            return {"success": False, "message": "Job not found."}
        return {"success": True, "job": job}

    elif action == "record_payment":
        job_id = data.get("job_id")
        amount = data.get("amount")
        if not job_id or not amount:
            return {"success": False, "message": "job_id and amount are required."}
        try:
            result = await jm.record_payment(int(job_id), float(amount))
        except ValueError as e:
            return {"success": False, "message": str(e)}
        return {"success": True, "message": f"Payment of Rs {amount} recorded.", **result}

    elif action == "override_balance":
        job_id      = data.get("job_id")
        new_balance = data.get("new_balance")
        reason      = data.get("reason", "")
        if not job_id or new_balance is None:
            return {"success": False, "message": "job_id and new_balance are required."}
        try:
            result = await jm.override_balance(int(job_id), float(new_balance), str(reason))
        except ValueError as e:
            return {"success": False, "message": str(e)}
        return {"success": True, "message": "Balance overridden.", **result}

    # ── Fuel ──────────────────────────────────────────────────────────────
    elif action == "log_fuel":
        result = await em.log_fuel(data)
        return {
            "success": True,
            "message": f"Fuel logged: {result['liters']}L {result['fuel_type']}",
            **result,
        }

    elif action == "get_fuel":
        logs = await em.get_fuel_logs(
            date=data.get("date"),
            operator_id=data.get("operator_id"),
            limit=int(data.get("limit", 50)),
        )
        return {"success": True, "fuel_logs": logs, "count": len(logs)}

    # ── Expenses ──────────────────────────────────────────────────────────
    elif action == "log_expense":
        result = await em.log_expense(data)
        return {
            "success": True,
            "message": f"Expense Rs {result['amount']} [{result['category']}] logged.",
            **result,
        }

    elif action == "get_expenses":
        logs = await em.get_expenses(
            date=data.get("date"),
            category=data.get("category"),
            job_id=data.get("job_id"),
            limit=int(data.get("limit", 100)),
        )
        return {"success": True, "expenses": logs, "count": len(logs)}

    # ── Stats & Reports ───────────────────────────────────────────────────
    elif action == "get_stats":
        report_date = data.get("date", today)
        stats = await db.get_daily_stats(report_date)
        return {"success": True, "stats": stats}

    elif action == "daily_report":
        report_date = data.get("date", today)
        stats = await db.get_daily_stats(report_date)
        jobs  = await db.get_jobs(date=report_date)
        path  = await xl.generate_daily_report(stats, jobs, report_date)
        return {
            "success": True,
            "path":    path,
            "file_path": path,
            "stats":   stats,
            "message": (
                f"Daily report {report_date}: "
                f"{stats['total_jobs']} jobs, "
                f"Revenue Rs {stats['revenue']:,.0f}, "
                f"Profit Rs {stats['profit']:,.0f}. "
                f"Saved: {path}"
            ),
        }

    elif action == "monthly_report":
        raw_year  = data.get("year")
        raw_month = data.get("month")
        if raw_year and raw_month:
            month_str = f"{int(raw_year):04d}-{int(raw_month):02d}"
        else:
            month_str = str(data.get("month_str", today[:7]))
        do_export = data.get("export", False)

        summary    = await analytics.monthly_summary(month_str)
        daily_raw  = summary.get("daily", [])

        stats_payload = {
            "year":           int(month_str[:4]),
            "month":          int(month_str[5:7]),
            "total_jobs":     summary.get("total_jobs", 0),
            "completed_jobs": sum(d.get("completed_jobs", 0) for d in daily_raw),
            "pending_jobs":   sum(d.get("pending_jobs", 0)   for d in daily_raw),
            "revenue":        summary.get("total_revenue", 0),
            "fuel_cost":      summary.get("total_fuel_cost", 0),
            "other_expenses": 0,
            "total_expenses": summary.get("total_fuel_cost", 0),
            "profit":         summary.get("total_profit", 0),
        }
        daily_payload = [
            {"date": d["date"], "jobs": d["total_jobs"], "revenue": d["revenue"], "profit": d["profit"]}
            for d in daily_raw if d.get("total_jobs", 0) > 0
        ]
        breakdown = []
        if summary.get("agriculture_jobs"):
            breakdown.append({"service": "Agriculture", "count": summary["agriculture_jobs"], "revenue": summary.get("agriculture_revenue", 0)})
        if summary.get("transport_jobs"):
            breakdown.append({"service": "Transport", "count": summary["transport_jobs"], "revenue": summary.get("transport_revenue", 0)})

        file_path = None
        if do_export:
            all_jobs   = await db.get_jobs(limit=1000)
            month_jobs = [j for j in all_jobs if (j.get("scheduled_date") or "").startswith(month_str)]
            file_path  = await xl.generate_monthly_report(
                month=month_str, daily_stats=daily_raw,
                all_jobs=month_jobs, all_expenses=[],
            )

        return {
            "success":   True,
            "stats":     stats_payload,
            "daily":     daily_payload,
            "breakdown": breakdown,
            "file_path": file_path,
            "message":   f"Monthly {month_str}: {stats_payload['total_jobs']} jobs, Revenue Rs {stats_payload['revenue']:,.0f}",
        }

    # ── Customers & Operators ─────────────────────────────────────────────
    elif action == "get_customers":
        customers = await jm.get_customers_list()
        return {"success": True, "customers": customers, "count": len(customers)}

    elif action == "get_operators":
        operators = await jm.get_operators_list()
        return {"success": True, "operators": operators, "count": len(operators)}

    elif action == "add_operator":
        name  = (data.get("name") or "").strip()
        phone = data.get("phone", "")
        if not name:
            return {"success": False, "message": "name is required."}
        import aiosqlite
        async with aiosqlite.connect(db.DB_PATH) as conn:
            cur = await conn.execute(
                "INSERT INTO operators (name, phone) VALUES (?,?)", (name, phone)
            )
            await conn.commit()
            op_id = cur.lastrowid
        return {"success": True, "operator_id": op_id, "message": f"Operator '{name}' added."}

    # ── Customer PIN issuance / job requests (agro_client support) ─────────
    # Operator-only actions, gated by the existing operator-app PIN lock —
    # not the customer auth_token system. Lets the operator hand out a
    # 4-digit PIN to a customer (free, no SMS) and review pending job
    # requests submitted from the agro_client app.
    elif action == "issue_customer_pin":
        phone = (data.get("phone") or "").strip()
        pin   = (data.get("pin") or "").strip()
        if not phone or not pin:
            return {"success": False, "message": "phone and pin are required."}
        result = await cp.issue_pin(phone, pin)
        return result

    elif action == "get_pending_job_requests":
        requests = await cp.get_pending_job_requests()
        return {"success": True, "requests": requests, "count": len(requests)}

    elif action == "get_all_job_requests":
        requests = await cp.get_all_job_requests()
        return {"success": True, "requests": requests, "count": len(requests)}

    elif action == "accept_job_request":
        req_id     = int(data.get("request_id", 0))
        job_id     = data.get("linked_job_id")  # optional
        if not req_id:
            return {"success": False, "message": "request_id is required."}
        result = await cp.accept_job_request(req_id, int(job_id) if job_id else None)
        if result.get("success"):
            req = result["request"]
            cid = req.get("customer_id")
            customer_name = (req.get("customer_name") or "ग्राहक").strip()
            service = (req.get("service") or req.get("job_type") or "").strip()
            lang = _get_language()
            if lang == "ne":
                tts_text = f"{customer_name} जीको {service} अनुरोध स्वीकार गरियो।"
            else:
                tts_text = f"Job request for {service} from {customer_name} has been accepted."
            if cid:
                asyncio.create_task(_notify_customer_tts(int(cid), tts_text))
                asyncio.create_task(_push_customer_job_requests(int(cid)))
        return result

    elif action == "decline_job_request":
        req_id = int(data.get("request_id", 0))
        if not req_id:
            return {"success": False, "message": "request_id is required."}
        result = await cp.decline_job_request(req_id)
        if result.get("success"):
            req = result["request"]
            cid = req.get("customer_id")
            customer_name = (req.get("customer_name") or "ग्राहक").strip()
            service = (req.get("service") or req.get("job_type") or "").strip()
            lang = _get_language()
            if lang == "ne":
                tts_text = f"{customer_name} जीको {service} अनुरोध अस्वीकार गरियो।"
            else:
                tts_text = f"Job request for {service} from {customer_name} has been declined."
            if cid:
                asyncio.create_task(_notify_customer_tts(int(cid), tts_text))
                asyncio.create_task(_push_customer_job_requests(int(cid)))
        return result

    # ── Analytics ─────────────────────────────────────────────────────────
    elif action == "top_customers":
        result = await analytics.top_customers(
            limit=int(data.get("limit", 5)),
            month=data.get("month"),
        )
        return {"success": True, "customers": result}

    elif action == "service_performance":
        result = await analytics.service_performance(month=data.get("month"))
        return {"success": True, "services": result}

    elif action == "operator_performance":
        result = await analytics.operator_performance(month=data.get("month"))
        return {"success": True, "operators": result}

    elif action == "outstanding_balances":
        result = await analytics.outstanding_balances(
            min_balance=float(data.get("min_balance", 1.0))
        )
        total = await analytics.total_outstanding()
        return {"success": True, "balances": result, "total_outstanding": total}

    elif action == "dashboard_snapshot":
        result = await analytics.dashboard_snapshot(
            today=data.get("date"),
            month=data.get("month"),
        )
        return {"success": True, **result}

    # ── Meta / config ─────────────────────────────────────────────────────
    elif action == "get_constants":
        return {
            "success": True,
            "agri_services":       AGRI_SERVICES,
            "transport_materials": TRANSPORT_MATERIALS,
            "land_units":          LAND_UNITS,
            "transport_units":     TRANSPORT_UNITS,
            "job_statuses":        JOB_STATUSES,
            "fuel_types":          FUEL_TYPES,
            "expense_categories":  EXPENSE_CATEGORIES,
        }

    else:
        valid = [
            "log_job", "update_job", "update_job_time", "get_jobs", "get_job", "record_payment",
            "override_balance",
            "log_fuel", "get_fuel", "log_expense", "get_expenses",
            "get_stats", "daily_report", "monthly_report",
            "get_customers", "get_operators", "add_operator",
            "issue_customer_pin", "get_pending_job_requests",
            "accept_job_request", "decline_job_request",
            "top_customers", "service_performance", "operator_performance",
            "outstanding_balances", "dashboard_snapshot", "get_constants",
        ]
        return {"success": False, "message": f"Unknown action '{action}'. Valid: {valid}"}


# ─────────────────────────────────────────────────────────────────────────────
# WebSocket endpoint — Flutter app connects here
# ─────────────────────────────────────────────────────────────────────────────

@app.websocket("/ws")
async def websocket_endpoint(ws: WebSocket, token: str = Query(default="")):
    # Token auth (optional — only checked when AGRO_SECRET is set in .env)
    if SECRET and token != SECRET:
        await ws.close(code=4001, reason="Unauthorized: invalid token")
        return

    await manager.connect(ws)
    try:
        while True:
            raw = await ws.receive_text()
            try:
                msg = json.loads(raw)
            except json.JSONDecodeError:
                await manager.send(ws, {"type": "error", "message": "Invalid JSON"})
                continue

            mtype  = msg.get("type", "")
            action = msg.get("action", "")
            data   = msg.get("data", {})

            # ── Ping / keepalive ──────────────────────────────────────
            if mtype == "ping":
                await manager.send(ws, {"type": "pong", "ts": time.time()})
                continue

            # ── set_language (bilingual TTS) ──────────────────────────
            # Flutter TtsPlaybackService sends this on startup and on every
            # language toggle so the server synthesises in the right voice.
            if mtype == "set_language":
                lang = str(msg.get("language", "")).strip().lower()
                _set_language(lang)
                # Fire-and-forget — no response needed.
                continue

            # ── STT audio (mic button, VoiceService.stopAndSend) ───────
            # Flutter sends {"type": "stt_audio", "audio": <base64>,
            # "mime": ..., "stt_pipeline": ...} and listens for a reply
            # shaped {"type": "stt_result", "text": ...} (see MicButton /
            # voice_service.dart). Key names below MUST match those exactly
            # — this previously fell through to "Unknown message type"
            # because there was no handler here at all, and even a naive
            # port of server.py's handler would have used the wrong key
            # names ("data"/"transcript") since server.py's own Flutter
            # client is a different app with a different wire format.
            if mtype == "stt_audio":
                b64  = msg.get("audio", "")
                mime = msg.get("mime", "audio/webm")
                if not b64:
                    await manager.send(ws, {"type": "stt_result", "text": ""})
                    continue
                if STT_ENGINE is None:
                    log.error("stt_audio received but STT_ENGINE is not initialised")
                    await manager.send(ws, {
                        "type": "error",
                        "message": "Speech-to-text is not available on this server.",
                    })
                    continue
                try:
                    audio_bytes = base64.b64decode(b64)
                    text, lang, _confidence = await asyncio.to_thread(
                        STT_ENGINE.transcribe_blob, audio_bytes, mime
                    )
                except Exception as exc:
                    log.exception("STT transcription failed")
                    await manager.send(ws, {
                        "type": "error",
                        "message": f"Transcription failed: {exc}",
                    })
                    continue
                await manager.send(ws, {"type": "stt_result", "text": text, "language": lang})
                continue

            # ── Agro actions ──────────────────────────────────────────
            if mtype == "agro":
                if not action:
                    await manager.send(ws, {
                        "type": "agro_ack", "action": action,
                        "status": "error", "message": "action field is required",
                    })
                    continue

                log.info("agro action=%s data_keys=%s", action, list(data.keys()))
                # Ack immediately so Flutter doesn't time out on slow ops.
                await manager.send(ws, {
                    "type": "agro_ack", "action": action, "status": "processing",
                })

                try:
                    result = await dispatch(action, data)
                except Exception as exc:
                    log.exception("dispatch error action=%s", action)
                    result = {"success": False, "message": str(exc)}

                await manager.send(ws, {
                    "type":   "agro_result",
                    "action": action,
                    "data":   result,
                })

                # ── TTS: speak confirmation for write operations ───────
                # Only log_job and update_job trigger voice. Reads/reports
                # stay silent. Synthesis is non-blocking — the agro_result
                # is already delivered above before we start encoding audio.
                voice_text = _build_agro_voice_text(action, data, result)
                if voice_text:
                    asyncio.create_task(_agro_speak(ws, voice_text))

                continue

            # ── Customer actions (agro_client app) ─────────────────────
            # Message shape: {"type": "customer", "action": "...", "data": {...}}
            # Separate namespace from "agro" (operator actions) — a customer
            # session token can NEVER trigger an operator write; the handlers
            # below only ever call agents.agro.customer_portal, which only
            # reads jobs / writes to job_requests, never to jobs directly.
            if mtype == "customer":
                if not action:
                    await manager.send(ws, {
                        "type": "customer_ack", "action": action,
                        "status": "error", "message": "action field is required",
                    })
                    continue

                if action == "login":
                    phone = str(data.get("phone", "")).strip()
                    pin   = str(data.get("pin", "")).strip()
                    result = await cp.verify_customer_login(phone, pin)
                    if result.get("success"):
                        cid = result["customer_id"]
                        CUSTOMER_SESSIONS[result["token"]] = cid
                        # Register this WS so operator-side TTS can reach the customer.
                        CUSTOMER_WS.setdefault(cid, [])
                        if ws not in CUSTOMER_WS[cid]:
                            CUSTOMER_WS[cid].append(ws)
                    await manager.send(ws, {
                        "type": "customer_result", "action": "login", "data": result,
                    })
                    continue

                # Every other customer action requires a valid token.
                token = str(data.get("auth_token", "")).strip()
                customer_id = CUSTOMER_SESSIONS.get(token)
                if customer_id is None:
                    await manager.send(ws, {
                        "type": "customer_result", "action": action,
                        "data": {"success": False, "error": "Not logged in or session expired."},
                    })
                    continue

                # Re-register WS on every authenticated request so the operator's
                # TTS push can reach this customer even after a WebSocket reconnect
                # (the customer may reconnect with auth_token directly, skipping login).
                CUSTOMER_WS.setdefault(customer_id, [])
                if ws not in CUSTOMER_WS[customer_id]:
                    CUSTOMER_WS[customer_id].append(ws)

                if action == "get_jobs":
                    jobs = await cp.get_jobs_for_customer(customer_id)
                    await manager.send(ws, {
                        "type": "customer_result", "action": "get_jobs",
                        "data": {"success": True, "jobs": jobs},
                    })

                elif action == "get_outstanding":
                    outstanding = await cp.get_outstanding_for_customer(customer_id)
                    await manager.send(ws, {
                        "type": "customer_result", "action": "get_outstanding",
                        "data": {"success": True, **outstanding},
                    })

                elif action == "request_job":
                    req_id = await cp.create_job_request(customer_id, data)
                    await manager.send(ws, {
                        "type": "customer_result", "action": "request_job",
                        "data": {"success": True, "request_id": req_id},
                    })
                    # Alert every connected OPERATOR app immediately instead of
                    # letting the request sit until someone opens that screen.
                    asyncio.create_task(_broadcast_new_job_request(req_id, customer_id))

                elif action == "get_job_requests":
                    requests = await cp.get_job_requests_for_customer(customer_id)
                    await manager.send(ws, {
                        "type": "customer_result", "action": "get_job_requests",
                        "data": {"success": True, "requests": requests},
                    })

                else:
                    await manager.send(ws, {
                        "type": "customer_result", "action": action,
                        "data": {"success": False, "error": f"Unknown customer action '{action}'."},
                    })

                continue

            # ── Unknown message type ───────────────────────────────────
            await manager.send(ws, {
                "type":    "error",
                "message": f"Unknown message type '{mtype}'. Use type='agro', 'customer', or 'set_language'.",
            })

    except WebSocketDisconnect:
        manager.disconnect(ws)
        # Remove this socket from CUSTOMER_WS if it was a logged-in customer.
        for cid, sockets in list(CUSTOMER_WS.items()):
            if ws in sockets:
                sockets.remove(ws)
                if not sockets:
                    del CUSTOMER_WS[cid]
                break
    except Exception as exc:
        log.exception("WebSocket error")
        manager.disconnect(ws)
        for cid, sockets in list(CUSTOMER_WS.items()):
            if ws in sockets:
                sockets.remove(ws)
                if not sockets:
                    del CUSTOMER_WS[cid]
                break



# ── Tunnel helper — Cloudflare Tunnel sends CF-Visitor JSON header ────────────
def _cf_scheme(request) -> str | None:
    """Extract scheme from Cloudflare's CF-Visitor header, e.g. {"scheme":"https"}."""
    cf = request.headers.get("cf-visitor")
    if cf:
        try:
            return json.loads(cf).get("scheme")
        except Exception:
            pass
    return None


# ─────────────────────────────────────────────────────────────────────────────
# HTTP REST endpoints — handy for testing / ngrok
# ─────────────────────────────────────────────────────────────────────────────

@app.get("/")
async def root(request: Request):
    # ── Tunnel-aware URL derivation ────────────────────────────────────────
    # Works with: ngrok free, Cloudflare Tunnel (cloudflared), local LAN.
    #
    # Both ngrok and cloudflared inject X-Forwarded-Proto and forward the
    # real public host via the Host header (cloudflared) or
    # X-Forwarded-Host (ngrok).  We check both, preferring the explicit
    # forwarded-host header, then the Host header, then the raw netloc.
    #
    # Cloudflare Tunnel also sets CF-Visitor: {"scheme":"https"} which we
    # honour as a last resort for the scheme.
    scheme = (
        request.headers.get("x-forwarded-proto")
        or _cf_scheme(request)
        or request.url.scheme
    )
    host = (
        request.headers.get("x-forwarded-host")
        or request.headers.get("host")
        or request.url.netloc
    )
    ws_scheme = "wss" if scheme == "https" else "ws"
    return {
        "service":      "AGRO Standalone Backend",
        "version":      "1.1.0",
        "ws":           f"{ws_scheme}://{host}/ws",
        "docs":         f"{scheme}://{host}/docs",
        "db":           str(Path(db.DB_PATH).resolve()),
        "tts_backend":  TTS_BACKEND,
        "tts_language": _get_language(),
        "voice_ne":     VOICE_NE,
        "voice_en":     VOICE_EN,
    }


@app.get("/health")
async def health():
    return {
        "status":       "ok",
        "ts":           time.time(),
        "tts_backend":  TTS_BACKEND,
        "tts_language": _get_language(),
    }


@app.get("/api/jobs")
async def api_get_jobs(
    date: str | None = None,
    status: str | None = None,
    job_type: str | None = None,
    limit: int = 50,
):
    result = await jm.get_jobs_summary(date=date, status=status, job_type=job_type, limit=limit)
    return result


@app.post("/api/jobs")
async def api_create_job(body: dict):
    try:
        job = await jm.create_job(body)
        return {"success": True, **job}
    except Exception as e:
        raise HTTPException(status_code=400, detail=str(e))


@app.patch("/api/jobs/{job_id}")
async def api_update_job(job_id: int, body: dict):
    status = body.get("status")
    if not status:
        raise HTTPException(status_code=400, detail="status is required")
    try:
        result = await jm.update_job_status(job_id, status)
        return {"success": True, **result}
    except Exception as e:
        raise HTTPException(status_code=400, detail=str(e))


@app.get("/api/stats")
async def api_stats(date: str | None = None):
    d = date or _today()
    return await db.get_daily_stats(d)


@app.get("/api/customers")
async def api_customers():
    return await jm.get_customers_list()


@app.get("/api/operators")
async def api_operators():
    return await jm.get_operators_list()


@app.get("/api/report/daily")
async def api_daily_report(date: str | None = None):
    d = date or _today()
    stats = await db.get_daily_stats(d)
    jobs  = await db.get_jobs(date=d)
    path  = await xl.generate_daily_report(stats, jobs, d)
    return FileResponse(
        path,
        media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        filename=Path(path).name,
    )


@app.get("/api/report/monthly")
async def api_monthly_report(month: str | None = None):
    m = month or _today()[:7]
    summary    = await analytics.monthly_summary(m)
    daily_raw  = summary.get("daily", [])
    all_jobs   = await db.get_jobs(limit=1000)
    month_jobs = [j for j in all_jobs if (j.get("scheduled_date") or "").startswith(m)]
    path = await xl.generate_monthly_report(
        month=m, daily_stats=daily_raw, all_jobs=month_jobs, all_expenses=[]
    )
    return FileResponse(
        path,
        media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        filename=Path(path).name,
    )


@app.get("/api/analytics/dashboard")
async def api_dashboard(date: str | None = None, month: str | None = None):
    return await analytics.dashboard_snapshot(today=date, month=month)


@app.get("/api/analytics/outstanding")
async def api_outstanding():
    balances = await analytics.outstanding_balances()
    total    = await analytics.total_outstanding()
    return {"balances": balances, "total_outstanding": total}


def _today() -> str:
    return date.today().isoformat()


# ─────────────────────────────────────────────────────────────────────────────
# Optional: serve the Flutter *web* build from this same process
# ─────────────────────────────────────────────────────────────────────────────
# `flutter build web` (run inside agro_flutter_app/agro_operator) produces a
# static build/web/ folder containing index.html + JS/assets — that's just
# files, no separate web server logic needed. Mounting it here means the one
# ngrok tunnel you already run (`ngrok http 7788`) serves the web app AND the
# API/WebSocket from the exact same origin/port. No nginx, no second tunnel,
# no CORS wrinkles (same-origin). If you'd rather use nginx/Caddy in front
# instead (e.g. once you're off ngrok and onto a real domain), just don't set
# AGRO_WEB_BUILD_DIR and serve the build/web folder there instead — this
# block is a no-op if the directory isn't present, so it's safe either way.
#
# Point this at wherever you copied build/web on the Debian box, e.g.:
#   AGRO_WEB_BUILD_DIR=/opt/jarvis/agro_flutter_app/agro_operator/build/web
_WEB_BUILD_DIR = os.getenv(
    "AGRO_WEB_BUILD_DIR",
    str(Path(__file__).parent / "agro_flutter_app" / "agro_operator" / "build" / "web"),
)
if Path(_WEB_BUILD_DIR).is_dir():
    # html=True → unmatched paths fall back to index.html, which Flutter's
    # web router needs for deep links (e.g. reloading on a sub-route).
    # Mounted at "/" but added LAST (after every @app.get/@app.websocket
    # above), so those explicit routes — /ws, /health, /api/*, /docs — are
    # matched first and never shadowed by the static mount.
    app.mount("/", StaticFiles(directory=_WEB_BUILD_DIR, html=True), name="agro_web")
    log.info("Serving Flutter web build from %s at /", _WEB_BUILD_DIR)
else:
    log.info(
        "No Flutter web build found at %s — web app not served "
        "(run `flutter build web` in agro_flutter_app/agro_operator and "
        "point AGRO_WEB_BUILD_DIR at the resulting build/web folder if it's "
        "elsewhere).",
        _WEB_BUILD_DIR,
    )


# ─────────────────────────────────────────────────────────────────────────────
# Entry point
# ─────────────────────────────────────────────────────────────────────────────

if __name__ == "__main__":
    import uvicorn
    print(f"\n  AGRO Standalone Backend v1.1.0  (Bilingual TTS)")
    print(f"  WebSocket  : ws://{HOST}:{PORT}/ws")
    print(f"  REST docs  : http://{HOST}:{PORT}/docs")
    print(f"  Health     : http://{HOST}:{PORT}/health")
    print(f"  TTS engine : {TTS_BACKEND}")
    print(f"  Voice NE   : {VOICE_NE}")
    print(f"  Voice EN   : {VOICE_EN}")
    print(f"  Token auth : {'ENABLED (token required)' if SECRET else 'DISABLED (no token set — set AGRO_SECRET in .env)'}\n")
    uvicorn.run("agro_server:app", host=HOST, port=PORT, reload=False, log_level="info")
