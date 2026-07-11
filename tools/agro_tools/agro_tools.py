"""
tools/agro_tools/agro_tools.py
────────────────────────────────
AGRO_AGENT tool wrappers registered into JARVIS ToolRegistry.

These are thin sync/async bridges so other agents can invoke agro
capabilities using the standard Agent.invoke("agro.*", ...) pattern
without importing AgroAgent directly.

Tools registered:
  agro.log_job         — Create a new agriculture or transport job
  agro.update_job      — Update job status
  agro.log_fuel        — Record fuel consumption
  agro.log_expense     — Record a business expense
  agro.daily_report    — Generate daily summary + Excel export
  agro.get_jobs        — Fetch jobs with optional filters
  agro.get_stats       — Get daily statistics
  agro.analytics       — Full dashboard snapshot (today + month)
  agro.top_customers   — Top customers by revenue
  agro.outstanding     — Outstanding payment balances
"""
from __future__ import annotations

import asyncio
import logging
import time
from typing import Any, TYPE_CHECKING

if TYPE_CHECKING:
    from tools.registry.tool_registry import ToolRegistry

log = logging.getLogger("tools.agro")


# ─────────────────────────────────────────────────────────────────────────────
# Helpers
# ─────────────────────────────────────────────────────────────────────────────

def _run(coro) -> Any:
    """Run an async coroutine from a sync context (tool handlers are sync)."""
    try:
        loop = asyncio.get_event_loop()
        if loop.is_running():
            # Already inside an async context (e.g. pytest-asyncio, server)
            # Use asyncio.run_coroutine_threadsafe or a new loop in a thread
            import concurrent.futures
            with concurrent.futures.ThreadPoolExecutor(max_workers=1) as ex:
                future = ex.submit(asyncio.run, coro)
                return future.result(timeout=30)
        else:
            return loop.run_until_complete(coro)
    except RuntimeError:
        return asyncio.run(coro)


def _ok(data: Any, message: str = "") -> dict:
    return {"success": True, "data": data, "message": message}


def _err(message: str) -> dict:
    return {"success": False, "data": None, "message": message}


# ─────────────────────────────────────────────────────────────────────────────
# Tool implementations
# ─────────────────────────────────────────────────────────────────────────────

def agro_log_job(
    job_type: str,
    service: str,
    customer_name: str = "",
    operator_id: int | None = None,
    area_value: float | None = None,
    area_unit: str = "Katha",
    quantity_value: float | None = None,
    quantity_unit: str = "Tali",
    material: str = "",
    rate: float | None = None,
    total_amount: float | None = None,
    advance_paid: float = 0,
    location: str = "",
    scheduled_date: str = "",
    notes: str = "",
) -> dict:
    """Create a new agriculture or transport job."""
    try:
        from agents.agro.job_manager import create_job
        result = _run(create_job({
            "job_type":      job_type,
            "service":       service,
            "customer_name": customer_name,
            "operator_id":   operator_id,
            "area_value":    area_value,
            "area_unit":     area_unit,
            "quantity_value": quantity_value,
            "quantity_unit": quantity_unit,
            "material":      material,
            "rate":          rate,
            "total_amount":  total_amount,
            "advance_paid":  advance_paid,
            "location":      location,
            "scheduled_date": scheduled_date,
            "notes":         notes,
        }))
        return _ok(result, f"Job #{result.get('job_id')} created.")
    except Exception as exc:
        log.error("agro.log_job failed: %s", exc)
        return _err(str(exc))


def agro_update_job(job_id: int, status: str, user: str = "system") -> dict:
    """Update job status (pending → confirmed → in_progress → completed)."""
    try:
        from agents.agro.job_manager import update_job_status
        result = _run(update_job_status(job_id, status, user=user))
        return _ok(result, f"Job #{job_id} → {status}")
    except Exception as exc:
        return _err(str(exc))


def agro_log_fuel(
    liters: float,
    fuel_type: str = "Diesel",
    price_per_liter: float | None = None,
    total_cost: float | None = None,
    operator_id: int | None = None,
    job_id: int | None = None,
    petrol_pump: str = "",
    notes: str = "",
) -> dict:
    """Record fuel consumption for a tractor or vehicle."""
    try:
        from agents.agro.expense_manager import log_fuel
        result = _run(log_fuel({
            "liters":          liters,
            "fuel_type":       fuel_type,
            "price_per_liter": price_per_liter,
            "total_cost":      total_cost,
            "operator_id":     operator_id,
            "job_id":          job_id,
            "petrol_pump":     petrol_pump,
            "notes":           notes,
        }))
        return _ok(result, f"Fuel logged: {liters}L {fuel_type}")
    except Exception as exc:
        return _err(str(exc))


def agro_log_expense(
    category: str,
    amount: float,
    description: str = "",
    job_id: int | None = None,
    operator_id: int | None = None,
    receipt_ref: str = "",
) -> dict:
    """Record a business expense (maintenance, wages, repair, etc.)."""
    try:
        from agents.agro.expense_manager import log_expense
        result = _run(log_expense({
            "category":    category,
            "amount":      amount,
            "description": description,
            "job_id":      job_id,
            "operator_id": operator_id,
            "receipt_ref": receipt_ref,
        }))
        return _ok(result, f"Expense Rs {amount} [{category}] logged.")
    except Exception as exc:
        return _err(str(exc))


def agro_daily_report(date: str = "") -> dict:
    """Generate daily summary report and Excel export for a given date."""
    try:
        from datetime import date as _d
        from agents.agro import database as db, excel_exporter as xl
        report_date = date or _d.today().isoformat()

        async def _gen():
            stats = await db.get_daily_stats(report_date)
            jobs  = await db.get_jobs(date=report_date)
            path  = await xl.generate_daily_report(stats, jobs, report_date)
            return {"stats": stats, "path": path, "job_count": len(jobs)}

        result = _run(_gen())
        return _ok(result, f"Daily report for {report_date}: {result['path']}")
    except Exception as exc:
        return _err(str(exc))


def agro_get_jobs(
    date: str = "",
    status: str = "",
    job_type: str = "",
    limit: int = 50,
) -> dict:
    """Fetch jobs with optional filters."""
    try:
        from agents.agro import database as db
        jobs = _run(db.get_jobs(
            date=date or None,
            status=status or None,
            job_type=job_type or None,
            limit=limit,
        ))
        return _ok({"jobs": jobs, "count": len(jobs)}, f"Found {len(jobs)} jobs.")
    except Exception as exc:
        return _err(str(exc))


def agro_get_stats(date: str = "") -> dict:
    """Get daily job and financial statistics."""
    try:
        from datetime import date as _d
        from agents.agro import database as db
        report_date = date or _d.today().isoformat()
        stats = _run(db.get_daily_stats(report_date))
        return _ok(stats, f"Stats for {report_date}")
    except Exception as exc:
        return _err(str(exc))


def agro_analytics(today: str = "", month: str = "") -> dict:
    """Full dashboard analytics snapshot: today + month + top customers + outstanding dues."""
    try:
        from agents.agro.analytics import dashboard_snapshot
        result = _run(dashboard_snapshot(today=today or None, month=month or None))
        return _ok(result, "Analytics snapshot generated.")
    except Exception as exc:
        return _err(str(exc))


def agro_top_customers(limit: int = 5, month: str = "") -> dict:
    """Return top customers by revenue."""
    try:
        from agents.agro.analytics import top_customers
        result = _run(top_customers(limit=limit, month=month or None))
        return _ok(result, f"Top {len(result)} customers.")
    except Exception as exc:
        return _err(str(exc))


def agro_outstanding(min_balance: float = 1.0) -> dict:
    """Return one consolidated bill per customer with an outstanding balance."""
    try:
        from agents.agro.analytics import outstanding_balances
        result = _run(outstanding_balances(min_balance=min_balance))
        total = sum(float(r.get("balance_due", 0)) for r in result)
        job_count = sum(int(r.get("jobs_count", 0)) for r in result)
        return _ok(
            {"items": result, "count": len(result), "total_outstanding": total},
            f"Rs {total:,.0f} outstanding across {len(result)} customers "
            f"({job_count} unpaid jobs).",
        )
    except Exception as exc:
        return _err(str(exc))


# ─────────────────────────────────────────────────────────────────────────────
# Registration
# ─────────────────────────────────────────────────────────────────────────────

def register_agro_tools(registry: "ToolRegistry", event_bus=None) -> list[str]:
    """
    Register all AGRO tools into the provided ToolRegistry.
    Called from tools/registry/tool_registry_registration.py during bootstrap.
    Safe to call even if agents/agro/ is broken — failures are non-fatal.
    """
    try:
        from tools.registry.tool_registry import ToolDefinition
    except ImportError:
        log.warning("ToolDefinition not available — skipping agro tool registration.")
        return []

    def _wrap(fn, name: str):
        """Add EventBus telemetry around a tool function."""
        if event_bus is None:
            return fn
        import functools

        @functools.wraps(fn)
        def wrapper(*args, **kwargs):
            t0 = time.monotonic()
            try:
                result = fn(*args, **kwargs)
                latency = time.monotonic() - t0
                try:
                    from kernel.event_bus.event_bus import Event
                    event_bus.publish_sync(Event(
                        event_type="tool.invoked",
                        source=name,
                        payload={"tool": name, "success": True, "latency_s": round(latency, 4)},
                    ))
                except Exception:
                    pass
                return result
            except Exception as exc:
                latency = time.monotonic() - t0
                try:
                    from kernel.event_bus.event_bus import Event
                    event_bus.publish_sync(Event(
                        event_type="tool.failed",
                        source=name,
                        payload={"tool": name, "error": str(exc), "latency_s": round(latency, 4)},
                    ))
                except Exception:
                    pass
                raise

        return wrapper

    tools = [
        ToolDefinition(
            name="agro.log_job",
            handler=_wrap(agro_log_job, "agro.log_job"),
            description=(
                "Create a new agriculture (ploughing, rotavator, seeding, harvesting, pumping) "
                "or transport (Gitti, Baluwa, Dhunga, Cement, etc.) job for the Nawal Parasi "
                "family business. Returns job_id."
            ),
            tags=["agro", "job", "agriculture", "transport", "tractor", "nepal"],
            timeout_s=10.0,
        ),
        ToolDefinition(
            name="agro.update_job",
            handler=_wrap(agro_update_job, "agro.update_job"),
            description=(
                "Update the status of an existing agro job. "
                "Valid transitions: pending→confirmed→in_progress→completed or cancelled."
            ),
            tags=["agro", "job", "status", "update"],
            timeout_s=5.0,
        ),
        ToolDefinition(
            name="agro.log_fuel",
            handler=_wrap(agro_log_fuel, "agro.log_fuel"),
            description="Record fuel (Diesel/Petrol) consumption for a tractor or vehicle.",
            tags=["agro", "fuel", "diesel", "expense"],
            timeout_s=5.0,
        ),
        ToolDefinition(
            name="agro.log_expense",
            handler=_wrap(agro_log_expense, "agro.log_expense"),
            description=(
                "Record a business expense (Maintenance, Repair, Operator Wage, "
                "Spare Parts, Fuel, Other)."
            ),
            tags=["agro", "expense", "cost", "maintenance"],
            timeout_s=5.0,
        ),
        ToolDefinition(
            name="agro.daily_report",
            handler=_wrap(agro_daily_report, "agro.daily_report"),
            description=(
                "Generate a daily summary report and Excel export for a given date. "
                "Returns file path and statistics."
            ),
            tags=["agro", "report", "excel", "daily", "export"],
            timeout_s=30.0,
        ),
        ToolDefinition(
            name="agro.get_jobs",
            handler=_wrap(agro_get_jobs, "agro.get_jobs"),
            description="Fetch agro jobs with optional filters: date, status, job_type.",
            tags=["agro", "jobs", "list", "filter"],
            timeout_s=10.0,
        ),
        ToolDefinition(
            name="agro.get_stats",
            handler=_wrap(agro_get_stats, "agro.get_stats"),
            description="Get daily job and financial statistics for a given date.",
            tags=["agro", "stats", "revenue", "profit"],
            timeout_s=5.0,
        ),
        ToolDefinition(
            name="agro.analytics",
            handler=_wrap(agro_analytics, "agro.analytics"),
            description=(
                "Full dashboard analytics: today summary, month totals, "
                "top customers, outstanding balances."
            ),
            tags=["agro", "analytics", "dashboard", "revenue", "profit"],
            timeout_s=15.0,
        ),
        ToolDefinition(
            name="agro.top_customers",
            handler=_wrap(agro_top_customers, "agro.top_customers"),
            description="Return top customers ranked by revenue for a given month.",
            tags=["agro", "customers", "revenue", "ranking"],
            timeout_s=10.0,
        ),
        ToolDefinition(
            name="agro.outstanding",
            handler=_wrap(agro_outstanding, "agro.outstanding"),
            description="Return jobs with outstanding payment balances (उधारो / बाँकी रकम).",
            tags=["agro", "outstanding", "payment", "balance", "dues"],
            timeout_s=10.0,
        ),
    ]

    registered = []
    for defn in tools:
        try:
            registry.register(defn)
            registered.append(defn.name)
            log.info("Registered tool: %s", defn.name)
        except Exception as exc:
            log.error("Failed to register tool %s: %s", defn.name, exc)

    return registered
