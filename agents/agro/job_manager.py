"""
AGRO_AGENT — Job lifecycle manager.

Sits between agro_agent.py (action dispatcher) and database.py (raw SQL).
Encapsulates business rules that go beyond a single INSERT/UPDATE:
  - customer auto-create-or-find before job insert
  - rate × area / quantity → total_amount auto-fill
  - status transition validation (you can't go from completed → pending)
  - balance update when partial payment is recorded after job creation

All functions are async. Raise ValueError for rule violations so
agro_agent.py can catch them and return {success: False, message: ...}.
"""
from __future__ import annotations

import time
from typing import Any

from agents.agro.constants import (
    JOB_STATUSES,
    JOB_TYPE_AGRICULTURE,
    JOB_TYPE_TRANSPORT,
    JOB_TYPES,
    STATUS_CANCELLED,
    STATUS_COMPLETED,
    STATUS_CONFIRMED,
    STATUS_IN_PROGRESS,
    STATUS_PENDING,
)

# ── Status transition table ──────────────────────────────────────────────────
# Defines which transitions are legal. Anything not in the map is rejected.
_VALID_TRANSITIONS: dict[str, set[str]] = {
    STATUS_PENDING:     {STATUS_CONFIRMED, STATUS_CANCELLED},
    STATUS_CONFIRMED:   {STATUS_IN_PROGRESS, STATUS_CANCELLED},
    STATUS_IN_PROGRESS: {STATUS_COMPLETED, STATUS_CANCELLED},
    STATUS_COMPLETED:   set(),          # terminal — no further transitions
    STATUS_CANCELLED:   set(),          # terminal
}


# ─────────────────────────────────────────────────────────────────────────────
# Validation helpers
# ─────────────────────────────────────────────────────────────────────────────

def _require(data: dict, *keys: str) -> None:
    """Raise ValueError listing any missing required keys."""
    missing = [k for k in keys if not data.get(k)]
    if missing:
        raise ValueError(f"Missing required fields: {', '.join(missing)}")


def _validate_job_type(job_type: str) -> None:
    if job_type not in JOB_TYPES:
        raise ValueError(
            f"Invalid job_type '{job_type}'. Must be one of: {JOB_TYPES}"
        )


def _compute_total(data: dict) -> float | None:
    """
    Auto-compute total_amount = rate × quantity if both are provided and
    total_amount is not explicitly set by the caller.
    Returns None if not enough data to compute.
    """
    if data.get("total_amount") is not None:
        return float(data["total_amount"])

    rate = data.get("rate")
    if rate is None:
        return None

    job_type = data.get("job_type", JOB_TYPE_AGRICULTURE)
    if job_type == JOB_TYPE_AGRICULTURE:
        qty = data.get("area_value")
    else:
        qty = data.get("quantity_value")

    if rate is not None and qty is not None:
        return float(rate) * float(qty)
    return None


# ─────────────────────────────────────────────────────────────────────────────
# Public API — called by AgroAgent.handle_goal()
# ─────────────────────────────────────────────────────────────────────────────

async def create_job(raw: dict) -> dict[str, Any]:
    """
    Validate, enrich, and persist a new job.

    Business rules applied here:
      1. job_type must be 'agriculture' or 'transport'
      2. service field required
      3. customer name required (id resolved or created automatically)
      4. total_amount auto-filled from rate × area/quantity if not supplied
      5. balance_due computed server-side (never trusted from client)

    Returns dict with job_id and enriched data.
    """
    from agents.agro import database as db

    job_type = raw.get("job_type", JOB_TYPE_AGRICULTURE)
    _validate_job_type(job_type)
    _require(raw, "service")

    # ── Resolve/create customer ──────────────────────────────────────
    customer_id = raw.get("customer_id")
    customer_name = raw.get("customer_name", "").strip()
    if not customer_id and customer_name:
        customer_id = await db.get_or_create_customer(
            name=customer_name,
            phone=raw.get("customer_phone", ""),
            address=raw.get("customer_address", ""),
        )

    # ── Auto-compute total ────────────────────────────────────────────
    total_amount = _compute_total(raw)

    enriched = {**raw, "job_type": job_type, "customer_id": customer_id}
    if total_amount is not None:
        enriched["total_amount"] = total_amount

    job_id = await db.create_job(enriched)
    return {
        "job_id":       job_id,
        "customer_id":  customer_id,
        "total_amount": total_amount,
        "job_type":     job_type,
    }


async def update_job_status(
    job_id: int,
    new_status: str,
    user: str = "operator",
    signature_name: str | None = None,
) -> dict[str, Any]:
    """
    Transition a job to a new status.

    Validates:
      - new_status is a known status value
      - transition is legal from the job's current status

    signature_name is optional — passed through to db.update_job_status when
    an operator captures a "received by" name while marking a job complete.

    Raises ValueError on rule violation.
    """
    from agents.agro import database as db

    if new_status not in JOB_STATUSES:
        raise ValueError(
            f"Unknown status '{new_status}'. Valid: {JOB_STATUSES}"
        )

    current_job = await db.get_job_by_id(job_id)
    if current_job is None:
        raise ValueError(f"Job #{job_id} not found.")

    current_status = current_job.get("status", STATUS_PENDING)
    allowed = _VALID_TRANSITIONS.get(current_status, set())
    if new_status not in allowed:
        if not allowed:
            raise ValueError(
                f"Job #{job_id} is '{current_status}' — no further transitions allowed."
            )
        raise ValueError(
            f"Cannot transition job #{job_id} from '{current_status}' to '{new_status}'. "
            f"Allowed next states: {sorted(allowed)}"
        )

    await db.update_job_status(job_id, new_status, user=user, signature_name=signature_name)
    return {
        "job_id":   job_id,
        "previous": current_status,
        "status":   new_status,
    }


async def record_payment(
    job_id: int,
    amount_paid: float,
    user: str = "operator",
) -> dict[str, Any]:
    """
    Record an additional payment on an existing job and recompute balance_due.
    This is for partial payments after job creation, e.g. customer pays
    remaining balance on completion.

    Returns updated financial snapshot.
    """
    from agents.agro import database as db
    import aiosqlite

    DB_PATH = db.DB_PATH
    import json

    if amount_paid is None or amount_paid <= 0:
        raise ValueError("Payment amount must be greater than zero.")

    async with aiosqlite.connect(DB_PATH) as conn:
        conn.row_factory = aiosqlite.Row
        cur = await conn.execute(
            "SELECT total_amount, advance_paid, balance_due, status FROM jobs WHERE id=?",
            (job_id,),
        )
        row = await cur.fetchone()
        if row is None:
            raise ValueError(f"Job #{job_id} not found.")

        total = float(row["total_amount"] or 0)
        old_balance = float(row["balance_due"] or 0)
        new_advance = float(row["advance_paid"] or 0) + float(amount_paid)
        new_balance = max(0.0, total - new_advance)
        # A payment larger than what's actually owed isn't an error — it can
        # be a legitimate advance toward the next job, a tip, or just the
        # customer rounding up — but it's worth surfacing rather than
        # silently absorbing, so the operator/bookkeeping notices.
        overpaid_by = round(amount_paid - old_balance, 2) if amount_paid > old_balance else 0.0
        now = time.time()

        await conn.execute(
            "UPDATE jobs SET advance_paid=?, balance_due=?, updated_at=? WHERE id=?",
            (new_advance, new_balance, now, job_id),
        )
        await conn.execute(
            "INSERT INTO audit_log (action, table_name, record_id, user, payload) VALUES (?,?,?,?,?)",
            (
                "PAYMENT",
                "jobs",
                job_id,
                user,
                json.dumps({"amount_paid": amount_paid, "new_balance": new_balance}),
            ),
        )
        await conn.commit()

    return {
        "job_id":      job_id,
        "total":       total,
        "advance_paid": new_advance,
        "balance_due": new_balance,
        "overpaid_by": overpaid_by,
    }


async def override_balance(
    job_id: int,
    new_balance: float,
    reason: str,
    user: str = "operator",
) -> dict[str, Any]:
    """
    Manually override a job's balance_due to an explicit value, bypassing
    the normal advance_paid/total_amount arithmetic that record_payment()
    uses.

    For cases record_payment() genuinely can't express — a due that's being
    waived/written off, a correction because the original total_amount was
    entered wrong, an amount settled by agreement for less than what's
    technically owed, etc.

    Deliberately kept separate from record_payment() rather than folding
    this into it as "just another payment path": it does NOT touch
    advance_paid (so it can't be mistaken for cash actually received), and
    it's logged as a distinct BALANCE_OVERRIDE audit entry rather than
    PAYMENT — so anyone reviewing the books later can tell "money changed
    hands" apart from "an operator manually adjusted a number" at a glance,
    instead of the two being indistinguishable in the log. A reason is
    mandatory for the same reason: an unexplained override is exactly the
    kind of change that's impossible to trust when reconciling later.
    """
    from agents.agro import database as db
    import aiosqlite
    import json

    if reason is None or not reason.strip():
        raise ValueError("A reason is required for a manual balance override.")
    if new_balance is None or new_balance < 0:
        raise ValueError("Balance cannot be overridden to a negative value.")

    DB_PATH = db.DB_PATH
    async with aiosqlite.connect(DB_PATH) as conn:
        conn.row_factory = aiosqlite.Row
        cur = await conn.execute(
            "SELECT total_amount, advance_paid, balance_due FROM jobs WHERE id=?",
            (job_id,),
        )
        row = await cur.fetchone()
        if row is None:
            raise ValueError(f"Job #{job_id} not found.")

        old_balance = float(row["balance_due"] or 0)
        now = time.time()

        await conn.execute(
            "UPDATE jobs SET balance_due=?, updated_at=? WHERE id=?",
            (new_balance, now, job_id),
        )
        await conn.execute(
            "INSERT INTO audit_log (action, table_name, record_id, user, payload) VALUES (?,?,?,?,?)",
            (
                "BALANCE_OVERRIDE",
                "jobs",
                job_id,
                user,
                json.dumps({
                    "old_balance": old_balance,
                    "new_balance": new_balance,
                    "reason": reason.strip(),
                }),
            ),
        )
        await conn.commit()

    return {
        "job_id":      job_id,
        "total":       float(row["total_amount"] or 0),
        "advance_paid": float(row["advance_paid"] or 0),
        "balance_due": new_balance,
        "old_balance": old_balance,
        "reason":      reason.strip(),
    }


async def get_jobs_summary(
    date: str | None = None,
    status: str | None = None,
    job_type: str | None = None,
    limit: int = 50,
) -> dict[str, Any]:
    """
    Fetch filtered jobs with a quick counts summary header.
    Used by Flutter job list screen and daily overview.
    """
    from agents.agro import database as db

    jobs = await db.get_jobs(date=date, status=status, job_type=job_type, limit=limit)

    pending_count   = sum(1 for j in jobs if j.get("status") == STATUS_PENDING)
    active_count    = sum(1 for j in jobs if j.get("status") == STATUS_IN_PROGRESS)
    completed_count = sum(1 for j in jobs if j.get("status") == STATUS_COMPLETED)
    total_revenue   = sum(
        float(j.get("total_amount") or 0)
        for j in jobs
        if j.get("status") == STATUS_COMPLETED
    )

    return {
        "jobs":            jobs,
        "count":           len(jobs),
        "pending_count":   pending_count,
        "active_count":    active_count,
        "completed_count": completed_count,
        "total_revenue":   total_revenue,
    }


async def get_customers_list() -> list[dict]:
    """Return all customers for Flutter dropdown population."""
    from agents.agro import database as db
    return await db.get_customers()


async def get_operators_list() -> list[dict]:
    """Return all operators for Flutter dropdown population."""
    from agents.agro import database as db
    return await db.get_operators()
