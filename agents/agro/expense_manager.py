"""
AGRO_AGENT — Expense & fuel tracking manager.

Business logic layer above database.py for:
  - Fuel logging with auto total_cost computation
  - Expense categorization and validation
  - Linking expenses to jobs (optional)
  - Monthly/date-range expense summaries by category

All functions are async. Raise ValueError for validation failures.
"""
from __future__ import annotations

from typing import Any

from agents.agro.constants import (
    DEFAULT_FUEL_TYPE,
    EXPENSE_CATEGORIES,
    FUEL_TYPES,
)


# ─────────────────────────────────────────────────────────────────────────────
# Fuel
# ─────────────────────────────────────────────────────────────────────────────

async def log_fuel(data: dict) -> dict[str, Any]:
    """
    Validate and record fuel consumption.

    Auto-computes total_cost = liters × price_per_liter if both provided
    and total_cost not explicitly supplied.

    Returns dict with fuel_log_id and computed total_cost.
    """
    from agents.agro import database as db

    fuel_type = data.get("fuel_type", DEFAULT_FUEL_TYPE)
    if fuel_type not in FUEL_TYPES:
        raise ValueError(f"Invalid fuel_type '{fuel_type}'. Must be one of: {FUEL_TYPES}")

    liters = data.get("liters")
    if liters is None or float(liters) <= 0:
        raise ValueError("liters must be a positive number.")

    price = data.get("price_per_liter")
    total_cost = data.get("total_cost")
    if total_cost is None and price is not None:
        total_cost = float(liters) * float(price)

    enriched = {
        **data,
        "fuel_type":  fuel_type,
        "liters":     float(liters),
        "total_cost": total_cost,
    }

    fuel_id = await db.log_fuel(enriched)
    return {
        "fuel_log_id": fuel_id,
        "liters":      float(liters),
        "fuel_type":   fuel_type,
        "total_cost":  total_cost,
    }


async def get_fuel_logs(
    date: str | None = None,
    operator_id: int | None = None,
    limit: int = 50,
) -> list[dict]:
    """Fetch fuel logs, optionally filtered by date and/or operator."""
    import aiosqlite
    from agents.agro import database as db

    clauses, params = [], []
    if date:
        clauses.append("date(f.logged_at, 'unixepoch') = ?")
        params.append(date)
    if operator_id:
        clauses.append("f.operator_id = ?")
        params.append(operator_id)

    where = ("WHERE " + " AND ".join(clauses)) if clauses else ""

    async with aiosqlite.connect(db.DB_PATH) as conn:
        conn.row_factory = aiosqlite.Row
        cur = await conn.execute(
            f"""SELECT f.*, o.name as operator_name
                FROM fuel_logs f
                LEFT JOIN operators o ON o.id = f.operator_id
                {where}
                ORDER BY f.logged_at DESC LIMIT ?""",
            params + [limit],
        )
        rows = await cur.fetchall()
        return [dict(r) for r in rows]


# ─────────────────────────────────────────────────────────────────────────────
# General expenses
# ─────────────────────────────────────────────────────────────────────────────

async def log_expense(data: dict) -> dict[str, Any]:
    """
    Validate and record a business expense.

    If category is 'Fuel' the caller should use log_fuel() instead for
    proper liter tracking. We still allow it here so existing callers
    aren't broken — it just won't populate the fuel_logs table.
    """
    from agents.agro import database as db

    category = data.get("category", "Other")
    if category not in EXPENSE_CATEGORIES:
        # Accept unknown categories rather than hard-reject — business may
        # have one-off categories. Log a warning but continue.
        import logging
        logging.getLogger("agent.agro").warning(
            "Unknown expense category '%s' — accepted as-is.", category
        )

    amount = data.get("amount")
    if amount is None or float(amount) <= 0:
        raise ValueError("amount must be a positive number.")

    exp_id = await db.log_expense({**data, "amount": float(amount)})
    return {
        "expense_id": exp_id,
        "category":   category,
        "amount":     float(amount),
    }


async def get_expenses(
    date: str | None = None,
    category: str | None = None,
    job_id: int | None = None,
    limit: int = 100,
) -> list[dict]:
    """Fetch expenses with optional filters."""
    import aiosqlite
    from agents.agro import database as db

    clauses, params = [], []
    if date:
        clauses.append("date(e.logged_at, 'unixepoch') = ?")
        params.append(date)
    if category:
        clauses.append("e.category = ?")
        params.append(category)
    if job_id:
        clauses.append("e.job_id = ?")
        params.append(job_id)

    where = ("WHERE " + " AND ".join(clauses)) if clauses else ""

    async with aiosqlite.connect(db.DB_PATH) as conn:
        conn.row_factory = aiosqlite.Row
        cur = await conn.execute(
            f"""SELECT e.*, o.name as operator_name
                FROM expenses e
                LEFT JOIN operators o ON o.id = e.operator_id
                {where}
                ORDER BY e.logged_at DESC LIMIT ?""",
            params + [limit],
        )
        rows = await cur.fetchall()
        return [dict(r) for r in rows]


async def get_expense_breakdown(
    date: str | None = None,
    month: str | None = None,
) -> dict[str, Any]:
    """
    Return category-level expense breakdown for a date or month.

    Provide either `date` ('YYYY-MM-DD') OR `month` ('YYYY-MM'), not both.
    Returns a dict with:
      - by_category: {category: total_amount}
      - fuel_liters: total fuel consumed in the period
      - grand_total: sum of all expenses
    """
    import aiosqlite
    from agents.agro import database as db

    if date:
        date_filter = "date(e.logged_at, 'unixepoch') = ?"
        fuel_filter = "date(f.logged_at, 'unixepoch') = ?"
        param = date
    elif month:
        date_filter = "strftime('%Y-%m', e.logged_at, 'unixepoch') = ?"
        fuel_filter = "strftime('%Y-%m', f.logged_at, 'unixepoch') = ?"
        param = month
    else:
        date_filter = "1=1"
        fuel_filter = "1=1"
        param = None

    by_category: dict[str, float] = {}
    grand_total = 0.0
    fuel_liters = 0.0

    async with aiosqlite.connect(db.DB_PATH) as conn:
        conn.row_factory = aiosqlite.Row

        # Category breakdown from expenses table
        q_params = [param] if param else []
        cur = await conn.execute(
            f"""SELECT category, COALESCE(SUM(amount), 0) as total
                FROM expenses
                WHERE {date_filter}
                GROUP BY category""",
            q_params,
        )
        for row in await cur.fetchall():
            by_category[row["category"]] = float(row["total"])
            grand_total += float(row["total"])

        # Fuel from fuel_logs
        cur2 = await conn.execute(
            f"""SELECT COALESCE(SUM(liters), 0) as liters,
                       COALESCE(SUM(total_cost), 0) as fuel_cost
                FROM fuel_logs
                WHERE {fuel_filter}""",
            q_params,
        )
        frow = dict(await cur2.fetchone() or {})
        fuel_liters = float(frow.get("liters") or 0)
        fuel_cost_from_logs = float(frow.get("fuel_cost") or 0)

        # Merge fuel from fuel_logs into by_category['Fuel'] if not already there
        if fuel_cost_from_logs > 0:
            by_category["Fuel (pump logs)"] = fuel_cost_from_logs
            grand_total += fuel_cost_from_logs

    return {
        "period":      date or month or "all",
        "by_category": by_category,
        "fuel_liters": fuel_liters,
        "grand_total": grand_total,
    }
