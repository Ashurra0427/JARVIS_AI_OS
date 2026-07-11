"""
AGRO_AGENT — aiosqlite database layer.
Auto-creates datastore/agro/agro.db on first run.
All functions are async. Never use sqlite3 — always aiosqlite.
"""
from __future__ import annotations

import json
import os
import time
from typing import Any

import aiosqlite

DB_PATH = os.path.join(
    os.path.dirname(__file__), "..", "..", "datastore", "agro", "agro.db"
)

# ─────────────────────────────────────────────────────────────────────────────
# Schema
# ─────────────────────────────────────────────────────────────────────────────

SCHEMA = """
PRAGMA journal_mode=WAL;
PRAGMA foreign_keys=ON;

-- Operators (tractor drivers / workers)
CREATE TABLE IF NOT EXISTS operators (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    name        TEXT NOT NULL,
    phone       TEXT,
    is_active   INTEGER DEFAULT 1,
    created_at  REAL DEFAULT (unixepoch())
);

-- Customers
CREATE TABLE IF NOT EXISTS customers (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    name        TEXT NOT NULL,
    phone       TEXT,
    address     TEXT,
    created_at  REAL DEFAULT (unixepoch())
);

-- Jobs (core table)
CREATE TABLE IF NOT EXISTS jobs (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    job_type        TEXT NOT NULL,          -- 'agriculture' | 'transport'
    service         TEXT NOT NULL,          -- e.g. 'Ploughing' | 'Gitti'
    customer_id     INTEGER REFERENCES customers(id),
    operator_id     INTEGER REFERENCES operators(id),
    status          TEXT DEFAULT 'pending', -- see constants.JOB_STATUSES
    -- Agriculture fields
    area_value      REAL,                   -- numeric area (e.g. 3.5)
    area_unit       TEXT,                   -- 'Katha' | 'Bigha'
    -- Transport fields
    material        TEXT,                   -- e.g. 'Gitti'
    quantity_value  REAL,                   -- numeric quantity
    quantity_unit   TEXT,                   -- 'Tali' | 'Trip' | 'Ton'
    -- Location
    location        TEXT,                   -- free text (ward/tole)
    -- Financials
    rate            REAL,                   -- price per unit
    total_amount    REAL,                   -- computed or manual
    advance_paid    REAL DEFAULT 0,
    balance_due     REAL,
    -- Notes
    notes           TEXT,
    -- Time-based billing (Water Pumping, etc.)
    time_taken      REAL,                   -- numeric duration (e.g. 2.5)
    time_unit       TEXT,                   -- 'Minute' | 'Hour'
    rate_per_unit   REAL,                   -- rate per time unit (when time-based)
    -- Phase 12: live per-minute timer billing (agriculture only)
    rate_per_min    REAL,                   -- Rs per minute (live timer jobs)
    time_value      REAL,                   -- elapsed minutes recorded at stop
    -- Signature / acknowledgement
    signature_name  TEXT,                   -- name of person who signed / received
    -- Timestamps
    scheduled_date  TEXT,                   -- 'YYYY-MM-DD'
    started_at      REAL,
    completed_at    REAL,
    created_at      REAL DEFAULT (unixepoch()),
    updated_at      REAL DEFAULT (unixepoch())
);

-- Fuel logs
CREATE TABLE IF NOT EXISTS fuel_logs (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    operator_id     INTEGER REFERENCES operators(id),
    job_id          INTEGER REFERENCES jobs(id),
    fuel_type       TEXT DEFAULT 'Diesel',
    liters          REAL NOT NULL,
    price_per_liter REAL,
    total_cost      REAL,
    petrol_pump     TEXT,
    logged_at       REAL DEFAULT (unixepoch()),
    notes           TEXT
);

-- Expense logs
CREATE TABLE IF NOT EXISTS expenses (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    category        TEXT NOT NULL,          -- see constants.EXPENSE_CATEGORIES
    amount          REAL NOT NULL,
    job_id          INTEGER REFERENCES jobs(id),
    operator_id     INTEGER REFERENCES operators(id),
    description     TEXT,
    receipt_ref     TEXT,
    logged_at       REAL DEFAULT (unixepoch())
);

-- Daily summaries (cached aggregates)
CREATE TABLE IF NOT EXISTS daily_summaries (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    summary_date    TEXT UNIQUE NOT NULL,   -- 'YYYY-MM-DD'
    total_jobs      INTEGER DEFAULT 0,
    completed_jobs  INTEGER DEFAULT 0,
    pending_jobs    INTEGER DEFAULT 0,
    revenue         REAL DEFAULT 0,
    fuel_cost       REAL DEFAULT 0,
    other_expenses  REAL DEFAULT 0,
    profit          REAL DEFAULT 0,
    generated_at    REAL DEFAULT (unixepoch())
);

-- Audit log (every write operation is recorded)
CREATE TABLE IF NOT EXISTS audit_log (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    action      TEXT NOT NULL,
    table_name  TEXT,
    record_id   INTEGER,
    user        TEXT DEFAULT 'system',
    payload     TEXT,                       -- JSON snapshot
    ts          REAL DEFAULT (unixepoch())
);

-- Indexes
CREATE INDEX IF NOT EXISTS idx_jobs_date ON jobs(scheduled_date);
CREATE INDEX IF NOT EXISTS idx_jobs_status ON jobs(status);
CREATE INDEX IF NOT EXISTS idx_jobs_type ON jobs(job_type);
CREATE INDEX IF NOT EXISTS idx_fuel_date ON fuel_logs(logged_at);
CREATE INDEX IF NOT EXISTS idx_expenses_date ON expenses(logged_at);
"""


async def init_db() -> None:
    """Create database directory and run schema. Safe to call multiple times."""
    os.makedirs(os.path.dirname(os.path.abspath(DB_PATH)), exist_ok=True)
    async with aiosqlite.connect(DB_PATH) as db:
        await db.executescript(SCHEMA)
        # ── Migrations: add new columns to existing databases safely ─────────
        # ALTER TABLE ADD COLUMN is idempotent in practice but SQLite raises if
        # the column already exists — catch that specific error and continue.
        _migrations = [
            "ALTER TABLE jobs ADD COLUMN time_taken     REAL",
            "ALTER TABLE jobs ADD COLUMN time_unit      TEXT",
            "ALTER TABLE jobs ADD COLUMN rate_per_unit  REAL",
            "ALTER TABLE jobs ADD COLUMN signature_name TEXT",
            # Phase 12: live per-minute timer billing
            "ALTER TABLE jobs ADD COLUMN rate_per_min   REAL",
            "ALTER TABLE jobs ADD COLUMN time_value     REAL",
        ]
        for _sql in _migrations:
            try:
                await db.execute(_sql)
            except Exception:
                pass  # column already exists — safe to ignore
        # A case-insensitive lookup ("ram thapa" vs "Ram Thapa") needs an
        # index on the normalized form or every customer match degenerates
        # into a full table scan as the customer list grows. Cheap now,
        # so add it up front rather than waiting for it to matter.
        try:
            await db.execute(
                "CREATE INDEX IF NOT EXISTS idx_customers_name_norm "
                "ON customers (LOWER(TRIM(name)))"
            )
        except Exception:
            pass
        await db.commit()

        # ── One-time cleanup: merge customers that were split into
        # duplicate rows before name matching was made case/whitespace
        # insensitive (see _find_existing_customer_id). Safe to run on
        # every boot — it's a no-op once there's nothing left to merge.
        await _merge_duplicate_customers(db)
        await db.commit()


# ─────────────────────────────────────────────────────────────────────────────
# Customer identity resolution
# ─────────────────────────────────────────────────────────────────────────────
# Operators type customer names freely on the Flutter form (no picker),
# so "Ram Thapa", "ram thapa", and "Ram  Thapa" (double space) must all
# resolve to the *same* customer record, or the same person's jobs get
# split across separate customer rows and their dues never add up to one
# bill. Phone number is the stronger signal when present — two different
# spellings with the same phone are unambiguously the same person — so it
# is checked first; normalized name is the fallback for cash customers who
# were logged without a phone.

def _normalize_name(name: str) -> str:
    """Trim + collapse internal whitespace + casefold, for matching only."""
    return " ".join((name or "").split()).casefold()


def _normalize_phone(phone: str) -> str:
    """Digits only, so '+977 981-234-5678' and '9812345678' still match."""
    return "".join(ch for ch in (phone or "") if ch.isdigit())


async def _find_existing_customer_id(
    db: aiosqlite.Connection, name: str, phone: str = ""
) -> aiosqlite.Row | None:
    """
    Look up a customer by phone first (if given), then by case/whitespace-
    insensitive name. Shared by create_job()'s inline resolver and
    get_or_create_customer() so there is exactly one place that defines
    "same customer" — fixing it here fixes it everywhere.

    A name match is only trusted when it doesn't conflict with a phone:
    if the incoming phone and the matching name-record's phone are both
    present but different, that's two different people who happen to
    share a name (common enough with Nepali names) — not the same
    customer — so it's treated as no match rather than merged.
    """
    phone_norm = _normalize_phone(phone)
    if phone_norm:
        cur = await db.execute(
            "SELECT id, phone FROM customers "
            "WHERE phone IS NOT NULL AND REPLACE(REPLACE(REPLACE(REPLACE("
            "TRIM(phone),' ',''),'-',''),'(',''),')','') = ? LIMIT 1",
            (phone_norm,),
        )
        row = await cur.fetchone()
        if row:
            return row

    name_norm = _normalize_name(name)
    if name_norm:
        cur = await db.execute(
            "SELECT id, phone FROM customers WHERE LOWER(TRIM(name)) = ?",
            (name_norm,),
        )
        for row in await cur.fetchall():
            existing_phone_norm = _normalize_phone(row["phone"])
            if phone_norm and existing_phone_norm and phone_norm != existing_phone_norm:
                continue  # same name, conflicting phones — different people
            return row

    return None


async def _merge_duplicate_customers(db: aiosqlite.Connection) -> None:
    """
    Fold duplicate customer rows into a single canonical row, moving their
    jobs and job_requests across and deleting the losers.

    Two rows are considered the same customer if they share a normalized
    phone number OR a normalized (trimmed, casefolded) name — using OR
    rather than a single combined key matters: a record with a phone and
    a record with no phone but the same name must still end up in the
    same cluster, even though the phone-record's "key" would otherwise be
    phone-based and the phone-less record's would be name-based. Plain
    single-key grouping misses that case entirely, so this uses a small
    union-find over both signals instead.

    Runs on every startup; if there is nothing to merge it does one cheap
    SELECT and returns. Logged to audit_log so a merge is traceable.
    """
    db.row_factory = aiosqlite.Row
    cur = await db.execute("SELECT id, name, phone, created_at FROM customers")
    all_customers = await cur.fetchall()
    if len(all_customers) < 2:
        return

    parent: dict[int, int] = {c["id"]: c["id"] for c in all_customers}

    def find(x: int) -> int:
        while parent[x] != x:
            parent[x] = parent[parent[x]]
            x = parent[x]
        return x

    def union(a: int, b: int) -> None:
        ra, rb = find(a), find(b)
        if ra != rb:
            parent[rb] = ra

    by_phone: dict[str, list[int]] = {}
    by_name: dict[str, list[Any]] = {}
    for c in all_customers:
        phone_norm = _normalize_phone(c["phone"])
        if phone_norm:
            by_phone.setdefault(phone_norm, []).append(c["id"])
        name_norm = _normalize_name(c["name"])
        if name_norm:
            by_name.setdefault(name_norm, []).append(c)

    for ids in by_phone.values():
        for other in ids[1:]:
            union(ids[0], other)

    # Name matches only union when phones don't conflict — two different
    # people can share a name (common with Nepali names); only merge them
    # if at least one side has no phone on file to contradict the other.
    for rows in by_name.values():
        if len(rows) < 2:
            continue
        anchor = rows[0]
        anchor_phone = _normalize_phone(anchor["phone"])
        for other in rows[1:]:
            other_phone = _normalize_phone(other["phone"])
            if anchor_phone and other_phone and anchor_phone != other_phone:
                continue
            union(anchor["id"], other["id"])

    clusters: dict[int, list[Any]] = {}
    for c in all_customers:
        clusters.setdefault(find(c["id"]), []).append(c)

    for root, rows in clusters.items():
        if len(rows) < 2:
            continue
        # Canonical = oldest record (lowest id / earliest created_at) so
        # existing references skew toward the id already most in use.
        rows.sort(key=lambda r: (r["created_at"] or 0, r["id"]))
        canonical = rows[0]
        losers = rows[1:]
        for loser in losers:
            await db.execute(
                "UPDATE jobs SET customer_id=? WHERE customer_id=?",
                (canonical["id"], loser["id"]),
            )
            try:
                await db.execute(
                    "UPDATE job_requests SET customer_id=? WHERE customer_id=?",
                    (canonical["id"], loser["id"]),
                )
            except Exception:
                pass  # job_requests table may not exist yet on older DBs
            # Backfill phone onto the canonical row if it was missing one.
            if loser["phone"] and not (canonical["phone"] or "").strip():
                await db.execute(
                    "UPDATE customers SET phone=? WHERE id=?",
                    (loser["phone"], canonical["id"]),
                )
                canonical = dict(canonical) | {"phone": loser["phone"]}
            await db.execute("DELETE FROM customers WHERE id=?", (loser["id"],))
            await db.execute(
                "INSERT INTO audit_log (action, table_name, record_id, user, payload) "
                "VALUES (?,?,?,?,?)",
                (
                    "CUSTOMER_MERGE",
                    "customers",
                    canonical["id"],
                    "system",
                    json.dumps({
                        "merged_from_id": loser["id"],
                        "merged_from_name": loser["name"],
                        "canonical_name": canonical["name"],
                        "reason": "duplicate customer (case/whitespace/phone match)",
                    }),
                ),
            )


async def get_db() -> aiosqlite.Connection:
    """Return an open connection. Caller must use as async context manager."""
    db = await aiosqlite.connect(DB_PATH)
    db.row_factory = aiosqlite.Row
    await db.execute("PRAGMA foreign_keys=ON")
    return db


# ─────────────────────────────────────────────────────────────────────────────
# Job CRUD
# ─────────────────────────────────────────────────────────────────────────────

async def create_job(data: dict) -> int:
    """Insert a new job. Returns the new job id."""
    # ── Server-side balance computation ─────────────────────────────────
    # balance_due is ALWAYS derived here, never taken from the client as-is.
    # A Flutter bug or bad manual entry must not silently corrupt the books.
    total_amount = data.get("total_amount")
    advance_paid = data.get("advance_paid", 0) or 0
    balance_due  = (total_amount - advance_paid) if total_amount is not None else None

    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row

        # ── Auto-resolve customer_name → customer_id ─────────────────
        # Flutter operators type customer names freely (no ID lookup UI yet).
        # Find-or-create so the jobs table always has a proper FK.
        #
        # NOTE: this mirrors get_or_create_customer() above — kept as a
        # separate inline block because this is the path server.py's
        # AgroAgent.handle_goal() actually calls (db.create_job(goal)
        # directly, not via job_manager.create_job()). Previously this
        # block only ever inserted `name`, silently dropping any phone
        # number the operator typed — which meant customers logged
        # through server.py could never be issued an agro_client PIN.
        # Fixed to capture + backfill phone, same as get_or_create_customer.
        #
        # Matching now goes through _find_existing_customer_id() (phone
        # first, then case/whitespace-insensitive name) instead of an
        # exact `name = ?` match — that exact match was why "Ram Thapa"
        # and "ram thapa" used to become two separate customers with
        # their jobs split across two dues instead of one bill.
        customer_id = data.get("customer_id")
        customer_name = (data.get("customer_name") or "").strip()
        customer_phone = (data.get("customer_phone") or "").strip()
        if not customer_id and customer_name:
            row = await _find_existing_customer_id(db, customer_name, customer_phone)
            if row:
                customer_id = row["id"]
                if customer_phone and not (row["phone"] or "").strip():
                    await db.execute(
                        "UPDATE customers SET phone = ? WHERE id = ?",
                        (customer_phone, customer_id),
                    )
            else:
                cur_c2 = await db.execute(
                    "INSERT INTO customers (name, phone) VALUES (?, ?)",
                    (customer_name, customer_phone),
                )
                customer_id = cur_c2.lastrowid

        cur = await db.execute("""
            INSERT INTO jobs (
                job_type, service, customer_id, operator_id,
                area_value, area_unit, material, quantity_value, quantity_unit,
                location, rate, total_amount, advance_paid, balance_due,
                notes, scheduled_date,
                time_taken, time_unit, rate_per_unit, signature_name,
                rate_per_min, time_value
            ) VALUES (
                :job_type, :service, :customer_id, :operator_id,
                :area_value, :area_unit, :material, :quantity_value, :quantity_unit,
                :location, :rate, :total_amount, :advance_paid, :balance_due,
                :notes, :scheduled_date,
                :time_taken, :time_unit, :rate_per_unit, :signature_name,
                :rate_per_min, :time_value
            )
        """, {
            "job_type":       data.get("job_type", "agriculture"),
            "service":        data.get("service", ""),
            "customer_id":    customer_id,
            "operator_id":    data.get("operator_id"),
            # area_value/area_unit store land area (Katha/Bigha) for area-based jobs
            "area_value":     data.get("area_value"),
            "area_unit":      data.get("area_unit", "Katha"),
            "material":       data.get("material"),
            "quantity_value": data.get("quantity_value"),
            "quantity_unit":  data.get("quantity_unit", "Tali"),
            "location":       data.get("location", ""),
            "rate":           data.get("rate"),
            "total_amount":   total_amount,
            "advance_paid":   advance_paid,
            "balance_due":    balance_due,
            "notes":          data.get("notes", ""),
            "scheduled_date": data.get("scheduled_date"),
            # Time-based billing fields (Water Pumping, etc.)
            "time_taken":     data.get("time_taken") or data.get("time_value"),
            "time_unit":      data.get("time_unit", "Hour"),
            "rate_per_unit":  data.get("rate_per_unit") or data.get("rate"),
            "signature_name": data.get("signature_name") or data.get("received_by", ""),
            # Phase 12 per-minute billing
            "rate_per_min":   data.get("rate_per_min"),
            "time_value":     data.get("time_value") or data.get("time_taken"),
        })
        job_id = cur.lastrowid
        await db.execute(
            "INSERT INTO audit_log (action, table_name, record_id, payload) VALUES (?,?,?,?)",
            ("CREATE", "jobs", job_id, json.dumps(data, default=str))
        )
        await db.commit()
        return job_id


async def update_job_status(
    job_id: int, status: str, user: str = "system", signature_name: str | None = None
) -> None:
    """Update job status and set timestamp if completing.

    signature_name is optional — captured when an operator marks a job
    'completed' with a "received by" name. Only written when provided so a
    plain status change (confirm/start/cancel) doesn't clobber it with None.
    """
    now = time.time()
    extra = ""
    params: list = [status, now, job_id]
    if status == "in_progress":
        extra = ", started_at=?"
        params = [status, now, now, job_id]
    elif status == "completed":
        extra = ", completed_at=?"
        params = [status, now, now, job_id]
    if signature_name:
        extra += ", signature_name=?"
        params.insert(-1, signature_name)
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute(
            f"UPDATE jobs SET status=?, updated_at=?{extra} WHERE id=?", params
        )
        await db.execute(
            "INSERT INTO audit_log (action, table_name, record_id, user, payload) VALUES (?,?,?,?,?)",
            ("STATUS_UPDATE", "jobs", job_id, user, json.dumps({"status": status}))
        )
        await db.commit()


async def update_job_time(
    job_id: int,
    time_value: float,
    time_unit: str = "Minute",
    total_amount: float | None = None,
    user: str = "operator",
) -> dict:
    """
    Phase 12 — Live timer billing:
    Update the elapsed time and recomputed total on an agriculture job.
    Called when the operator stops the per-minute timer.
    """
    import time as _time
    now = _time.time()
    async with aiosqlite.connect(DB_PATH) as db:
        row = await (await db.execute(
            "SELECT rate_per_min, advance_paid FROM jobs WHERE id=?",
            (job_id,)
        )).fetchone()
        if row is None:
            raise ValueError(f"Job #{job_id} not found.")
        rate_per_min, advance_paid = row[0], (row[1] or 0.0)
        # Compute total from rate if not supplied
        if total_amount is None and rate_per_min:
            total_amount = round(rate_per_min * time_value, 2)
        balance_due = (total_amount or 0.0) - advance_paid
        await db.execute(
            """UPDATE jobs
               SET time_value=?, time_unit=?, total_amount=?,
                   balance_due=?, updated_at=?
               WHERE id=?""",
            (time_value, time_unit, total_amount, balance_due, now, job_id),
        )
        await db.execute(
            "INSERT INTO audit_log (action, table_name, record_id, user, payload) VALUES (?,?,?,?,?)",
            ("TIMER_STOP", "jobs", job_id, user,
             json.dumps({"time_value": time_value, "total_amount": total_amount})),
        )
        await db.commit()
    return {
        "job_id":       job_id,
        "time_value":   time_value,
        "time_unit":    time_unit,
        "total_amount": total_amount,
        "balance_due":  balance_due,
    }


async def get_jobs(
    date: str | None = None,
    status: str | None = None,
    job_type: str | None = None,
    limit: int = 50,
) -> list[dict]:
    """Fetch jobs with optional filters."""
    clauses, params = [], []
    if date:
        clauses.append("scheduled_date = ?")
        params.append(date)
    if status:
        clauses.append("status = ?")
        params.append(status)
    if job_type:
        clauses.append("job_type = ?")
        params.append(job_type)
    where = ("WHERE " + " AND ".join(clauses)) if clauses else ""
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        cur = await db.execute(
            f"""SELECT j.*, c.name as customer_name, o.name as operator_name
                FROM jobs j
                LEFT JOIN customers c ON c.id = j.customer_id
                LEFT JOIN operators o ON o.id = j.operator_id
                {where}
                ORDER BY j.created_at DESC LIMIT ?""",
            params + [limit]
        )
        rows = await cur.fetchall()
        return [dict(r) for r in rows]


async def get_job_by_id(job_id: int) -> dict | None:
    """Fetch a single job by id."""
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        cur = await db.execute(
            """SELECT j.*, c.name as customer_name, o.name as operator_name
               FROM jobs j
               LEFT JOIN customers c ON c.id = j.customer_id
               LEFT JOIN operators o ON o.id = j.operator_id
               WHERE j.id = ?""",
            (job_id,)
        )
        row = await cur.fetchone()
        return dict(row) if row else None


# ─────────────────────────────────────────────────────────────────────────────
# Customer + Operator CRUD
# ─────────────────────────────────────────────────────────────────────────────

async def get_or_create_customer(name: str, phone: str = "", address: str = "") -> int:
    """
    Return existing customer id by name or create new. Returns customer id.

    Phone backfill: if the customer already exists but has no phone on
    file (e.g. logged before customer_phone was collected in the
    Flutter form) and a phone IS supplied on this job, we save it. This
    is what lets a customer who was already in the system before the
    agro_client feature existed still end up with a phone number once
    you log their next job with it filled in — otherwise they could
    never be issued a PIN.
    """
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        row = await _find_existing_customer_id(db, name, phone)
        if row:
            if phone and not (row["phone"] or "").strip():
                await db.execute(
                    "UPDATE customers SET phone = ? WHERE id = ?", (phone, row["id"])
                )
                await db.commit()
            return row["id"]
        cur = await db.execute(
            "INSERT INTO customers (name, phone, address) VALUES (?,?,?)",
            (name.strip(), phone, address)
        )
        await db.commit()
        return cur.lastrowid


async def get_customers() -> list[dict]:
    """Fetch all active customers."""
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        cur = await db.execute("SELECT * FROM customers ORDER BY name")
        return [dict(r) for r in await cur.fetchall()]


async def get_operators() -> list[dict]:
    """Fetch all active operators."""
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        cur = await db.execute(
            "SELECT * FROM operators WHERE is_active=1 ORDER BY name"
        )
        return [dict(r) for r in await cur.fetchall()]


# ─────────────────────────────────────────────────────────────────────────────
# Fuel + Expense CRUD
# ─────────────────────────────────────────────────────────────────────────────

async def log_fuel(data: dict) -> int:
    """Log fuel consumption. Returns new record id."""
    liters = data.get("liters") or 0
    ppl    = data.get("price_per_liter")
    total  = (liters * ppl) if (liters and ppl) else data.get("total_cost")
    async with aiosqlite.connect(DB_PATH) as db:
        cur = await db.execute("""
            INSERT INTO fuel_logs (operator_id, job_id, fuel_type, liters,
                                   price_per_liter, total_cost, petrol_pump, notes)
            VALUES (:operator_id, :job_id, :fuel_type, :liters,
                    :price_per_liter, :total_cost, :petrol_pump, :notes)
        """, {
            "operator_id":    data.get("operator_id"),
            "job_id":         data.get("job_id"),
            "fuel_type":      data.get("fuel_type", "Diesel"),
            "liters":         liters,
            "price_per_liter": ppl,
            "total_cost":     total,
            "petrol_pump":    data.get("petrol_pump", ""),
            "notes":          data.get("notes", ""),
        })
        await db.commit()
        return cur.lastrowid


async def log_expense(data: dict) -> int:
    """Log a business expense. Returns new record id."""
    async with aiosqlite.connect(DB_PATH) as db:
        cur = await db.execute("""
            INSERT INTO expenses (category, amount, job_id, operator_id, description, receipt_ref)
            VALUES (:category, :amount, :job_id, :operator_id, :description, :receipt_ref)
        """, {
            "category":    data.get("category", "Other"),
            "amount":      data.get("amount", 0),
            "job_id":      data.get("job_id"),
            "operator_id": data.get("operator_id"),
            "description": data.get("description", ""),
            "receipt_ref": data.get("receipt_ref", ""),
        })
        await db.commit()
        return cur.lastrowid


async def get_daily_stats(date: str) -> dict:
    """Compute daily aggregates for a given 'YYYY-MM-DD' date."""
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row

        jobs_cur = await db.execute("""
            SELECT
                COUNT(*) as total,
                SUM(CASE WHEN status='completed' THEN 1 ELSE 0 END) as completed,
                SUM(CASE WHEN status='pending' OR status='confirmed' THEN 1 ELSE 0 END) as pending,
                COALESCE(SUM(CASE WHEN status='completed' THEN total_amount ELSE 0 END), 0) as revenue
            FROM jobs WHERE scheduled_date=?
        """, (date,))
        jobs = dict(await jobs_cur.fetchone() or {})

        fuel_cur = await db.execute("""
            SELECT COALESCE(SUM(total_cost), 0) as fuel_cost
            FROM fuel_logs
            WHERE date(logged_at, 'unixepoch') = ?
        """, (date,))
        fuel = dict(await fuel_cur.fetchone() or {})

        exp_cur = await db.execute("""
            SELECT COALESCE(SUM(amount), 0) as other_expenses
            FROM expenses
            WHERE category != 'Fuel' AND date(logged_at, 'unixepoch') = ?
        """, (date,))
        exp = dict(await exp_cur.fetchone() or {})

        revenue  = float(jobs.get("revenue") or 0)
        fuel_cost = float(fuel.get("fuel_cost") or 0)
        other_exp = float(exp.get("other_expenses") or 0)

        return {
            "date":           date,
            "total_jobs":     int(jobs.get("total") or 0),
            "completed_jobs": int(jobs.get("completed") or 0),
            "pending_jobs":   int(jobs.get("pending") or 0),
            "revenue":        revenue,
            "fuel_cost":      fuel_cost,
            "other_expenses": other_exp,
            "total_expenses": fuel_cost + other_exp,
            "profit":         revenue - fuel_cost - other_exp,
        }


async def get_monthly_stats(month: str) -> list[dict]:
    """Get daily stats for each day in a given month ('YYYY-MM'). Returns list of day stats."""
    from datetime import date as _date, timedelta
    import calendar

    year, mon = int(month[:4]), int(month[5:7])
    days_in_month = calendar.monthrange(year, mon)[1]
    result = []
    for day in range(1, days_in_month + 1):
        d = f"{year:04d}-{mon:02d}-{day:02d}"
        stats = await get_daily_stats(d)
        if stats["total_jobs"] > 0 or stats["fuel_cost"] > 0:
            result.append(stats)
    return result