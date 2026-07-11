"""
agents/agro/customer_portal.py
================================
ADDITIVE ONLY — nothing in this file modifies existing tables/functions in
agents/agro/database.py. It reuses the same agro.db (same DB_PATH) and the
existing `customers` / `jobs` tables, and adds:

  1. A `pin_hash` column on `customers` (migration, additive — existing
     rows just get NULL until you issue a PIN for them).
  2. A new `job_requests` table — customers can ask for a new job from the
     agro_client app; this is a REQUEST queue, not a real job. Nothing
     here ever inserts directly into `jobs` — only you (the operator),
     via the existing agro_operator flow, turn a request into a real job.
     This guarantees customer-side writes can never corrupt the books.
  3. Plain functions used by the new "customer" WS message handlers in
     server.py / agro_server.py (see the docstring there for the message
     shapes). Both servers can import and call these identically.

Why a phone+PIN model instead of OTP:
  Real SMS OTP costs money per message (SMS gateway fees) and needs a
  third-party account. For a small operation, issuing a 4-digit PIN to
  each customer (in person, once) when you register them — the same
  security model already used for the operator app's lock screen — is
  free and good enough.
"""
from __future__ import annotations

import hashlib
import secrets
import time
from typing import Any

import aiosqlite

from .database import DB_PATH

# ─────────────────────────────────────────────────────────────────────────────
# Migration — additive only
# ─────────────────────────────────────────────────────────────────────────────

_JOB_REQUESTS_SCHEMA = """
CREATE TABLE IF NOT EXISTS job_requests (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    customer_id     INTEGER REFERENCES customers(id) NOT NULL,
    job_type        TEXT NOT NULL,              -- 'agriculture' | 'transport'
    service         TEXT,                       -- e.g. 'Ploughing' | 'Gitti'
    notes           TEXT,
    preferred_date  TEXT,                       -- 'YYYY-MM-DD', optional
    status          TEXT DEFAULT 'pending',      -- 'pending' | 'accepted' | 'declined'
    linked_job_id   INTEGER REFERENCES jobs(id), -- set once the operator turns this into a real job
    created_at      REAL DEFAULT (unixepoch())
);
"""


async def init_customer_portal() -> None:
    """
    Idempotent. Safe to call on every server boot, alongside the existing
    agents.agro.database.init_db(). Never drops or rewrites anything.
    """
    async with aiosqlite.connect(DB_PATH) as db:
        await db.executescript(_JOB_REQUESTS_SCHEMA)
        try:
            await db.execute("ALTER TABLE customers ADD COLUMN pin_hash TEXT")
        except Exception:
            pass  # column already exists
        try:
            await db.execute("ALTER TABLE customers ADD COLUMN pin_fail_count INTEGER DEFAULT 0")
        except Exception:
            pass
        try:
            await db.execute("ALTER TABLE customers ADD COLUMN pin_lock_until REAL")
        except Exception:
            pass
        await db.commit()


# ─────────────────────────────────────────────────────────────────────────────
# PIN hashing — same scheme as the Flutter operator app's AuthService
# (sha256 of a salted string), kept server-side only; never sent back.
# ─────────────────────────────────────────────────────────────────────────────

def _hash_pin(phone: str, pin: str) -> str:
    return hashlib.sha256(f"agro_jarvis_client_{phone}_{pin}".encode()).hexdigest()


_MAX_FAILURES = 5
_LOCK_SECONDS = 5 * 60


# ─────────────────────────────────────────────────────────────────────────────
# Customer lookup / PIN issuance
# ─────────────────────────────────────────────────────────────────────────────

async def find_customer_by_phone(phone: str) -> dict | None:
    phone = (phone or "").strip()
    if not phone:
        return None
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        cur = await db.execute(
            "SELECT * FROM customers WHERE phone = ? LIMIT 1", (phone,)
        )
        row = await cur.fetchone()
        return dict(row) if row else None


async def issue_pin(phone: str, pin: str) -> dict:
    """
    Called from the OPERATOR side (agro_operator) — NOT from agro_client.
    Sets/overwrites a customer's PIN. If no customer exists yet for this
    phone, returns an error: a customer record must exist (i.e. they've
    had at least one job logged) before a PIN can be issued.
    """
    if not (pin or "").isdigit() or len(pin) != 4:
        return {"success": False, "error": "PIN must be exactly 4 digits."}

    customer = await find_customer_by_phone(phone)
    if customer is None:
        return {"success": False, "error": f"No customer found with phone '{phone}'."}

    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute(
            "UPDATE customers SET pin_hash=?, pin_fail_count=0, pin_lock_until=NULL WHERE id=?",
            (_hash_pin(phone, pin), customer["id"]),
        )
        await db.commit()
    return {"success": True, "customer_id": customer["id"], "customer_name": customer["name"]}


async def verify_customer_login(phone: str, pin: str) -> dict:
    """
    Returns:
      {"success": True, "customer_id": ..., "customer_name": ..., "token": ...}
    or
      {"success": False, "error": "..."}
    `token` is an opaque per-session string the client should send back on
    every subsequent "customer" WS message as `auth_token` (server keeps a
    short-lived in-memory map — see CUSTOMER_SESSIONS in the WS handler).
    """
    customer = await find_customer_by_phone(phone)
    if customer is None:
        return {"success": False, "error": "No account found for this phone number."}

    if not customer.get("pin_hash"):
        return {"success": False, "error": "No PIN has been set up yet. Contact the operator."}

    lock_until = customer.get("pin_lock_until")
    if lock_until and time.time() < lock_until:
        remaining = int(lock_until - time.time())
        return {"success": False, "error": f"Too many wrong PINs. Try again in {remaining}s."}

    if _hash_pin(phone, pin) != customer["pin_hash"]:
        fail_count = (customer.get("pin_fail_count") or 0) + 1
        new_lock = time.time() + _LOCK_SECONDS if fail_count >= _MAX_FAILURES else None
        async with aiosqlite.connect(DB_PATH) as db:
            await db.execute(
                "UPDATE customers SET pin_fail_count=?, pin_lock_until=? WHERE id=?",
                (0 if new_lock else fail_count, new_lock, customer["id"]),
            )
            await db.commit()
        if new_lock:
            return {"success": False, "error": f"Too many wrong PINs. Locked for {_LOCK_SECONDS // 60} minutes."}
        remaining = _MAX_FAILURES - fail_count
        return {"success": False, "error": f"Wrong PIN. {remaining} attempt(s) remaining."}

    # Correct PIN.
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute(
            "UPDATE customers SET pin_fail_count=0, pin_lock_until=NULL WHERE id=?",
            (customer["id"],),
        )
        await db.commit()

    token = secrets.token_hex(16)
    return {
        "success": True,
        "customer_id": customer["id"],
        "customer_name": customer["name"],
        "token": token,
    }


# ─────────────────────────────────────────────────────────────────────────────
# Customer-scoped reads (own jobs / own outstanding balance only)
# ─────────────────────────────────────────────────────────────────────────────

async def get_jobs_for_customer(customer_id: int, limit: int = 100) -> list[dict]:
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        cur = await db.execute(
            """
            SELECT id, job_type, service, material, status,
                   area_value, area_unit, quantity_value, quantity_unit,
                   location, rate, total_amount, advance_paid, balance_due,
                   notes, scheduled_date, started_at, completed_at, created_at
            FROM jobs
            WHERE customer_id = ?
            ORDER BY created_at DESC
            LIMIT ?
            """,
            (customer_id, limit),
        )
        rows = await cur.fetchall()
        return [dict(r) for r in rows]


async def get_outstanding_for_customer(customer_id: int) -> dict:
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        cur = await db.execute(
            """
            SELECT COALESCE(SUM(balance_due), 0) AS total_due,
                   COUNT(*) AS unpaid_jobs
            FROM jobs
            WHERE customer_id = ? AND balance_due IS NOT NULL AND balance_due > 0
            """,
            (customer_id,),
        )
        row = await cur.fetchone()
        return dict(row) if row else {"total_due": 0, "unpaid_jobs": 0}


# ─────────────────────────────────────────────────────────────────────────────
# Customer-initiated job requests (never writes directly to `jobs`)
# ─────────────────────────────────────────────────────────────────────────────

async def create_job_request(customer_id: int, data: dict) -> int:
    async with aiosqlite.connect(DB_PATH) as db:
        cur = await db.execute(
            """
            INSERT INTO job_requests (customer_id, job_type, service, notes, preferred_date)
            VALUES (?, ?, ?, ?, ?)
            """,
            (
                customer_id,
                data.get("job_type", "agriculture"),
                data.get("service", ""),
                data.get("notes", ""),
                data.get("preferred_date"),
            ),
        )
        await db.commit()
        return cur.lastrowid


async def get_job_requests_for_customer(customer_id: int) -> list[dict]:
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        cur = await db.execute(
            "SELECT * FROM job_requests WHERE customer_id = ? ORDER BY created_at DESC",
            (customer_id,),
        )
        rows = await cur.fetchall()
        return [dict(r) for r in rows]


async def get_pending_job_requests() -> list[dict]:
    """For the OPERATOR side — list all pending requests across all customers."""
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        cur = await db.execute(
            """
            SELECT jr.*, c.name AS customer_name, c.phone AS customer_phone
            FROM job_requests jr
            JOIN customers c ON c.id = jr.customer_id
            WHERE jr.status = 'pending'
            ORDER BY jr.created_at ASC
            """
        )
        rows = await cur.fetchall()
        return [dict(r) for r in rows]


async def get_all_job_requests() -> list[dict]:
    """For the OPERATOR side — list ALL requests (pending + accepted + declined)."""
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        cur = await db.execute(
            """
            SELECT jr.*, c.name AS customer_name, c.phone AS customer_phone
            FROM job_requests jr
            JOIN customers c ON c.id = jr.customer_id
            ORDER BY jr.created_at DESC
            """
        )
        rows = await cur.fetchall()
        return [dict(r) for r in rows]


async def accept_job_request(request_id: int, linked_job_id: int | None = None) -> dict:
    """
    Called from the OPERATOR side. Marks a job_request as 'accepted'.
    Optionally links it to a real job_id (if the operator has already
    logged the corresponding job). Returns the request row + customer info
    so the server can push a TTS notification to the customer's WS.
    """
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        cur = await db.execute(
            "SELECT jr.*, c.name AS customer_name, c.phone AS customer_phone "
            "FROM job_requests jr JOIN customers c ON c.id = jr.customer_id "
            "WHERE jr.id = ?",
            (request_id,),
        )
        row = await cur.fetchone()
        if row is None:
            return {"success": False, "error": f"Job request #{request_id} not found."}
        if row["status"] != "pending":
            return {"success": False, "error": f"Request #{request_id} is already '{row['status']}'."}

        await db.execute(
            "UPDATE job_requests SET status='accepted', linked_job_id=? WHERE id=?",
            (linked_job_id, request_id),
        )
        await db.commit()
        return {"success": True, "request": dict(row)}


async def decline_job_request(request_id: int) -> dict:
    """
    Called from the OPERATOR side. Marks a job_request as 'declined'.
    Returns the request row + customer info for TTS push.
    """
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        cur = await db.execute(
            "SELECT jr.*, c.name AS customer_name, c.phone AS customer_phone "
            "FROM job_requests jr JOIN customers c ON c.id = jr.customer_id "
            "WHERE jr.id = ?",
            (request_id,),
        )
        row = await cur.fetchone()
        if row is None:
            return {"success": False, "error": f"Job request #{request_id} not found."}
        if row["status"] != "pending":
            return {"success": False, "error": f"Request #{request_id} is already '{row['status']}'."}

        await db.execute(
            "UPDATE job_requests SET status='declined' WHERE id=?",
            (request_id,),
        )
        await db.commit()
        return {"success": True, "request": dict(row)}
