"""
AGRO_AGENT — Analytics engine.

Provides revenue, profit, job utilization, and trend data for:
  - Flutter dashboard home screen charts
  - Monthly performance summaries
  - Best customer / best service analysis
  - Operator performance tracking

All outputs are plain dicts — ready to JSON-encode for WS responses.
All functions are async.
"""
from __future__ import annotations

import calendar
from datetime import date as _date, timedelta
from typing import Any

from agents.agro.constants import (
    JOB_TYPE_AGRICULTURE,
    JOB_TYPE_TRANSPORT,
    STATUS_COMPLETED,
)


# ─────────────────────────────────────────────────────────────────────────────
# Daily & range aggregations
# ─────────────────────────────────────────────────────────────────────────────

async def daily_summary(date_str: str) -> dict[str, Any]:
    """
    Full daily snapshot for the Flutter home screen.
    Combines job stats + expense breakdown + net profit.
    """
    from agents.agro import database as db
    from agents.agro.expense_manager import get_expense_breakdown

    stats = await db.get_daily_stats(date_str)
    expenses = await get_expense_breakdown(date=date_str)

    return {
        **stats,
        "expense_breakdown": expenses["by_category"],
        "fuel_liters":       expenses["fuel_liters"],
        "total_expenses":    expenses["grand_total"],
        "net_profit":        stats["revenue"] - expenses["grand_total"],
    }


async def weekly_summary(anchor_date: str | None = None) -> dict[str, Any]:
    """
    7-day rolling summary ending on anchor_date (default: today).
    Returns per-day array + totals.
    """
    from agents.agro import database as db

    end = _date.fromisoformat(anchor_date) if anchor_date else _date.today()
    start = end - timedelta(days=6)

    days = []
    total_revenue = 0.0
    total_jobs = 0
    total_completed = 0

    for i in range(7):
        d = (start + timedelta(days=i)).isoformat()
        s = await db.get_daily_stats(d)
        days.append(s)
        total_revenue   += s["revenue"]
        total_jobs      += s["total_jobs"]
        total_completed += s["completed_jobs"]

    return {
        "period":          f"{start.isoformat()} to {end.isoformat()}",
        "days":            days,
        "total_revenue":   total_revenue,
        "total_jobs":      total_jobs,
        "total_completed": total_completed,
        "avg_revenue_per_day": round(total_revenue / 7, 2),
    }


async def monthly_summary(month: str | None = None) -> dict[str, Any]:
    """
    Full month breakdown ('YYYY-MM').
    Returns daily array + monthly totals + job-type split.
    """
    from agents.agro import database as db

    if not month:
        month = _date.today().strftime("%Y-%m")

    daily_list = await db.get_monthly_stats(month)

    # Job-type split for the month
    year, mon = int(month[:4]), int(month[5:7])
    days_in_month = calendar.monthrange(year, mon)[1]
    first = f"{month}-01"
    last  = f"{month}-{days_in_month:02d}"

    all_jobs = await db.get_jobs(limit=1000)
    month_jobs = [
        j for j in all_jobs
        if (j.get("scheduled_date") or "") >= first
        and (j.get("scheduled_date") or "") <= last
    ]

    agri_count     = sum(1 for j in month_jobs if j.get("job_type") == JOB_TYPE_AGRICULTURE)
    transport_count = sum(1 for j in month_jobs if j.get("job_type") == JOB_TYPE_TRANSPORT)
    agri_revenue   = sum(
        float(j.get("total_amount") or 0)
        for j in month_jobs
        if j.get("job_type") == JOB_TYPE_AGRICULTURE and j.get("status") == STATUS_COMPLETED
    )
    transport_revenue = sum(
        float(j.get("total_amount") or 0)
        for j in month_jobs
        if j.get("job_type") == JOB_TYPE_TRANSPORT and j.get("status") == STATUS_COMPLETED
    )

    total_revenue = sum(d["revenue"] for d in daily_list)
    total_jobs    = sum(d["total_jobs"] for d in daily_list)
    total_profit  = sum(d["profit"] for d in daily_list)
    total_fuel    = sum(d["fuel_cost"] for d in daily_list)

    return {
        "month":              month,
        "daily":              daily_list,
        "total_revenue":      total_revenue,
        "total_jobs":         total_jobs,
        "total_profit":       total_profit,
        "total_fuel_cost":    total_fuel,
        "agriculture_jobs":   agri_count,
        "transport_jobs":     transport_count,
        "agriculture_revenue": agri_revenue,
        "transport_revenue":  transport_revenue,
        "avg_revenue_per_working_day": (
            round(total_revenue / len(daily_list), 2) if daily_list else 0
        ),
    }


# ─────────────────────────────────────────────────────────────────────────────
# Customer & service analytics
# ─────────────────────────────────────────────────────────────────────────────

async def top_customers(limit: int = 5, month: str | None = None) -> list[dict]:
    """
    Return top customers by revenue (completed jobs only).
    Useful for Flutter analytics screen.
    """
    import aiosqlite
    from agents.agro import database as db

    date_filter = ""
    params: list = []
    if month:
        date_filter = "AND strftime('%Y-%m', j.scheduled_date) = ?"
        params.append(month)

    async with aiosqlite.connect(db.DB_PATH) as conn:
        conn.row_factory = aiosqlite.Row
        cur = await conn.execute(
            f"""SELECT c.name, c.phone,
                       COUNT(j.id) as job_count,
                       COALESCE(SUM(j.total_amount), 0) as total_revenue,
                       COALESCE(SUM(j.balance_due), 0) as total_outstanding
                FROM jobs j
                LEFT JOIN customers c ON c.id = j.customer_id
                WHERE j.status = 'completed'
                {date_filter}
                GROUP BY j.customer_id
                ORDER BY total_revenue DESC
                LIMIT ?""",
            params + [limit],
        )
        rows = await cur.fetchall()
        return [dict(r) for r in rows]


async def service_performance(month: str | None = None) -> list[dict]:
    """
    Revenue and job count per service/material type.
    Used for Flutter pie chart on analytics screen.
    """
    import aiosqlite
    from agents.agro import database as db

    date_filter = ""
    params: list = []
    if month:
        date_filter = "AND strftime('%Y-%m', scheduled_date) = ?"
        params.append(month)

    async with aiosqlite.connect(db.DB_PATH) as conn:
        conn.row_factory = aiosqlite.Row
        cur = await conn.execute(
            f"""SELECT
                    COALESCE(service, material, 'Unknown') as service_name,
                    job_type,
                    COUNT(*) as job_count,
                    COALESCE(SUM(total_amount), 0) as total_revenue
                FROM jobs
                WHERE status = 'completed'
                {date_filter}
                GROUP BY service_name, job_type
                ORDER BY total_revenue DESC""",
            params,
        )
        rows = await cur.fetchall()
        return [dict(r) for r in rows]


async def operator_performance(month: str | None = None) -> list[dict]:
    """
    Jobs done and revenue generated per operator.
    Useful for wage calculation reference.
    """
    import aiosqlite
    from agents.agro import database as db

    date_filter = ""
    params: list = []
    if month:
        date_filter = "AND strftime('%Y-%m', j.scheduled_date) = ?"
        params.append(month)

    async with aiosqlite.connect(db.DB_PATH) as conn:
        conn.row_factory = aiosqlite.Row
        cur = await conn.execute(
            f"""SELECT
                    o.name as operator_name,
                    COUNT(j.id) as jobs_done,
                    COALESCE(SUM(j.total_amount), 0) as revenue_generated,
                    COALESCE(SUM(f.total_cost), 0) as fuel_consumed_cost
                FROM operators o
                LEFT JOIN jobs j ON j.operator_id = o.id
                    AND j.status = 'completed' {date_filter}
                LEFT JOIN fuel_logs f ON f.operator_id = o.id
                WHERE o.is_active = 1
                GROUP BY o.id
                ORDER BY jobs_done DESC""",
            params,
        )
        rows = await cur.fetchall()
        return [dict(r) for r in rows]


# ─────────────────────────────────────────────────────────────────────────────
# Outstanding balances
# ─────────────────────────────────────────────────────────────────────────────

async def outstanding_balances(min_balance: float = 1.0) -> list[dict]:
    """
    Return ONE bill per customer, combining every completed job of theirs
    that still has an unpaid balance — not one row per job.

    Previously this returned a flat row per job, so a customer with two
    unpaid jobs showed up as two separate dues in the Biller screen. Jobs
    are grouped by customer_id here (each bill's `jobs` list carries the
    date and service of every job that makes it up), so as long as those
    jobs share a customer_id the bill is automatically correct — including
    for customers who used to get split across duplicate records due to
    the name-matching bug fixed in database.py (see
    _find_existing_customer_id / _merge_duplicate_customers). Nothing here
    needs to change if that matching logic improves further; grouping by
    customer_id just inherits whatever "same customer" means upstream.
    """
    import aiosqlite
    from agents.agro import database as db

    async with aiosqlite.connect(db.DB_PATH) as conn:
        conn.row_factory = aiosqlite.Row
        cur = await conn.execute(
            """SELECT j.id, j.customer_id, j.scheduled_date, j.service,
                      j.total_amount, j.advance_paid, j.balance_due,
                      c.name as customer_name, c.phone as customer_phone,
                      o.name as operator_name
               FROM jobs j
               LEFT JOIN customers c ON c.id = j.customer_id
               LEFT JOIN operators o ON o.id = j.operator_id
               WHERE j.status = 'completed'
                 AND j.balance_due > 0
               ORDER BY j.customer_id, j.scheduled_date ASC, j.id ASC"""
        )
        rows = [dict(r) for r in await cur.fetchall()]

    bills: dict[Any, dict] = {}
    order: list[Any] = []
    for r in rows:
        key = r["customer_id"] if r["customer_id"] is not None else f"noid:{r['customer_name']}"
        if key not in bills:
            bills[key] = {
                "customer_id":     r["customer_id"],
                "customer_name":   r["customer_name"] or "Unknown",
                "customer_phone":  r["customer_phone"],
                "total_amount":    0.0,
                "advance_paid":    0.0,
                "balance_due":     0.0,
                "jobs_count":      0,
                "jobs":            [],
            }
            order.append(key)
        bill = bills[key]
        bill["total_amount"] += float(r["total_amount"] or 0)
        bill["advance_paid"] += float(r["advance_paid"] or 0)
        bill["balance_due"]  += float(r["balance_due"] or 0)
        bill["jobs_count"]   += 1
        bill["jobs"].append({
            "id":             r["id"],
            "scheduled_date": r["scheduled_date"],
            "service":        r["service"],
            "total_amount":   float(r["total_amount"] or 0),
            "advance_paid":   float(r["advance_paid"] or 0),
            "balance_due":    float(r["balance_due"] or 0),
            "operator_name":  r["operator_name"],
        })

    result = [bills[k] for k in order if bills[k]["balance_due"] >= min_balance]
    result.sort(key=lambda b: b["balance_due"], reverse=True)
    return result


async def total_outstanding() -> float:
    """Quick scalar: sum of all unpaid balances on completed jobs."""
    import aiosqlite
    from agents.agro import database as db

    async with aiosqlite.connect(db.DB_PATH) as conn:
        cur = await conn.execute(
            "SELECT COALESCE(SUM(balance_due), 0) FROM jobs WHERE status='completed' AND balance_due > 0"
        )
        row = await cur.fetchone()
        return float(row[0] if row else 0)


# ─────────────────────────────────────────────────────────────────────────────
# Full analytics snapshot for Flutter dashboard
# ─────────────────────────────────────────────────────────────────────────────

async def dashboard_snapshot(
    today: str | None = None,
    month: str | None = None,
) -> dict[str, Any]:
    """
    One-call aggregate for the Flutter home/dashboard screen.
    Returns today summary + month totals + outstanding dues.
    """
    if not today:
        today = _date.today().isoformat()
    if not month:
        month = today[:7]

    today_stats     = await daily_summary(today)
    month_stats     = await monthly_summary(month)
    top_custs       = await top_customers(limit=3, month=month)
    outstanding_amt = await total_outstanding()

    return {
        "today":            today_stats,
        "month":            {
            "revenue":       month_stats["total_revenue"],
            "jobs":          month_stats["total_jobs"],
            "profit":        month_stats["total_profit"],
        },
        "top_customers":    top_custs,
        "outstanding_dues": outstanding_amt,
        "generated_at":     today,
    }
