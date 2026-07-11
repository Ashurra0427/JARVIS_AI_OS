"""
agents/agro/backup_manager.py
=====================================================
Nightly backup of the single SQLite datastore that agro_server.py runs on
(datastore/agro/agro.db). Piggybacks on the existing 23:59 NPT scheduler
loop in agro_server.py — see _report_scheduler().

Two layers, both best-effort and independently optional:

1. LOCAL ROTATION (always on, no setup required)
   Copies agro.db → datastore/agro/backups/agro_YYYY-MM-DD.db each night
   and deletes any local backup older than AGRO_BACKUP_KEEP_DAYS (default 7).
   This protects against corruption / accidental deletion / "oops I ran a
   bad migration" — it does NOT protect against the machine itself dying,
   since the backup lives on the same disk as the original.

2. OFF-DEVICE PUSH (opt-in — only runs if AGRO_BACKUP_WEBHOOK_URL is set)
   POSTs the freshly-copied backup file as multipart/form-data to a webhook
   URL of your choosing. This is deliberately the lowest-setup off-device
   option: no OAuth flow, no API credentials to provision — point it at
   anything that accepts a file upload (a small self-hosted endpoint, a
   Zapier/Make webhook that forwards to Drive/Dropbox, an n8n workflow,
   etc). If you'd rather push straight to Google Drive/Dropbox with their
   native APIs, that's a reasonable upgrade later, but it needs you to
   register an app and hand this code a token — deliberately NOT done here
   since it can't be guessed or defaulted.

   AGRO_BACKUP_WEBHOOK_URL unset  → off-device push is skipped entirely,
   local rotation still runs. Nothing breaks if you never set this.
"""
from __future__ import annotations

import logging
import os
import shutil
from datetime import datetime
from pathlib import Path

from .database import DB_PATH

log = logging.getLogger("agro.backup")

BACKUP_DIR = Path(DB_PATH).resolve().parent / "backups"
KEEP_DAYS = int(os.getenv("AGRO_BACKUP_KEEP_DAYS", "7"))
WEBHOOK_URL = os.getenv("AGRO_BACKUP_WEBHOOK_URL", "").strip()


def _rotate_local(today_backup: Path) -> int:
    """Delete local backups older than KEEP_DAYS. Returns count removed."""
    removed = 0
    for f in BACKUP_DIR.glob("agro_*.db"):
        if f == today_backup:
            continue
        try:
            date_part = f.stem.removeprefix("agro_")
            file_date = datetime.strptime(date_part, "%Y-%m-%d")
        except ValueError:
            continue
        age_days = (datetime.now() - file_date).days
        if age_days > KEEP_DAYS:
            try:
                f.unlink()
                removed += 1
            except OSError as exc:
                log.warning("Could not remove old backup %s: %s", f, exc)
    return removed


async def _push_off_device(backup_path: Path) -> None:
    """Best-effort webhook upload. Any failure is logged, never raised —
    a backup destination being down for a night should not be treated as
    worse than not backing up off-device at all, and it must never take
    down the report scheduler it's piggybacking on."""
    if not WEBHOOK_URL:
        return
    try:
        import httpx
    except ImportError:
        log.warning(
            "AGRO_BACKUP_WEBHOOK_URL is set but httpx isn't installed — "
            "run: pip install httpx. Skipping off-device push tonight."
        )
        return
    try:
        async with httpx.AsyncClient(timeout=60) as client:
            with open(backup_path, "rb") as fh:
                resp = await client.post(
                    WEBHOOK_URL,
                    files={"file": (backup_path.name, fh, "application/octet-stream")},
                    data={"filename": backup_path.name, "source": "agro_db_backup"},
                )
        if resp.status_code >= 400:
            log.warning(
                "Off-device backup push got HTTP %s from webhook — check the URL/endpoint.",
                resp.status_code,
            )
        else:
            log.info("Off-device backup push succeeded (%s).", backup_path.name)
    except Exception as exc:
        log.warning("Off-device backup push failed (local backup still saved): %s", exc)


async def run_nightly_backup(date_str: str) -> dict:
    """
    Copies agro.db to datastore/agro/backups/agro_<date_str>.db, prunes old
    local copies past AGRO_BACKUP_KEEP_DAYS, and (if configured) pushes the
    fresh copy off-device. Called once per night from _report_scheduler(),
    right alongside the existing daily-report generation. Never raises —
    a backup failure is logged and swallowed so it can't take down the
    report scheduler.
    """
    result = {"ok": False, "backup_path": None, "removed_old": 0}
    try:
        src = Path(DB_PATH).resolve()
        if not src.exists():
            log.warning("agro.db not found at %s — skipping tonight's backup.", src)
            return result

        BACKUP_DIR.mkdir(parents=True, exist_ok=True)
        dest = BACKUP_DIR / f"agro_{date_str}.db"

        # sqlite3's backup API (not a raw file copy) so an in-progress write
        # can't be caught mid-transaction and leave a corrupt copy behind.
        import sqlite3
        src_conn = sqlite3.connect(str(src))
        dest_conn = sqlite3.connect(str(dest))
        with dest_conn:
            src_conn.backup(dest_conn)
        src_conn.close()
        dest_conn.close()

        result["backup_path"] = str(dest)
        result["removed_old"] = _rotate_local(dest)
        log.info(
            "Nightly DB backup saved: %s (pruned %d old local backup(s), keeping last %d days)",
            dest.name, result["removed_old"], KEEP_DAYS,
        )

        await _push_off_device(dest)
        result["ok"] = True
    except Exception:
        log.exception("Nightly DB backup failed for %s", date_str)
    return result
