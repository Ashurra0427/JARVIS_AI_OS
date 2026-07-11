"""
AGRO_AGENT — Agent 07 | Agriculture & Transport Business Manager
Nawal Parasi, Nepal. Family business operations.

Inherits BaseAgent. Registered in Orchestrator._start_agents().
If this agent crashes, JARVIS continues normally (guarded by _import_agent).
"""
from __future__ import annotations

import json
from datetime import date
from typing import Any

from agents.base.base_agent import BaseAgent, AgentCapability
from agents.metrics_publisher import MetricsPublisherMixin


class AgroAgent(MetricsPublisherMixin, BaseAgent):

    AGENT_DISPLAY_NAME = "AGRO"
    AGENT_NUMBER = "07"

    def __init__(
        self,
        memory_router,
        event_bus,
        model_router=None,
        registry=None,
        tool_registry=None,
        embedding_service=None,
    ):
        super().__init__(
            "agro",
            memory_router,
            event_bus,
            model_router,
            registry,
            tool_registry=tool_registry,
            embedding_service=embedding_service,
        )
        self._jobs_today: int = 0
        self._revenue_today: float = 0.0
        self._db_ready: bool = False
        self._current_task_desc: str = ""

    # ── Abstract implementations ───────────────────────────────────────

    def capabilities(self) -> list[AgentCapability]:
        return [
            AgentCapability(
                "log_job",
                "Log a new agriculture or transport job",
                ["job", "tractor", "ploughing", "transport", "carriage", "काम"],
            ),
            AgentCapability(
                "update_job",
                "Update job status (pending → confirmed → in_progress → completed)",
                ["start", "complete", "cancel", "update", "status"],
            ),
            AgentCapability(
                "log_fuel",
                "Record fuel consumption for a tractor or vehicle",
                ["fuel", "diesel", "petrol", "इन्धन", "liters"],
            ),
            AgentCapability(
                "log_expense",
                "Record a business expense (maintenance, wages, spare parts, etc.)",
                ["expense", "खर्च", "cost", "maintenance", "repair"],
            ),
            AgentCapability(
                "daily_report",
                "Generate daily summary and Excel export",
                ["report", "daily", "summary", "excel", "export", "रिपोर्ट"],
            ),
            AgentCapability(
                "analytics",
                "Revenue, profit, and utilization analytics",
                ["analytics", "profit", "revenue", "नाफा", "trend"],
            ),
        ]

    def _metrics_payload(self) -> dict[str, Any]:
        return {
            "jobs_today":    self._jobs_today,
            "revenue_today": self._revenue_today,
            "db_ready":      self._db_ready,
        }

    # ── Lifecycle ─────────────────────────────────────────────────────

    async def _on_start(self) -> None:
        """Initialize database and subscribe to EventBus topics."""
        try:
            from agents.agro.database import init_db
            await init_db()
            self._db_ready = True
            self._log.info("AgroAgent: database initialized at datastore/agro/agro.db")
        except Exception as exc:
            self._log.error(f"AgroAgent: DB init failed — {exc}. Will retry on first request.")

        # Subscribe to structured EventBus topics (from Flutter app via Phase 3 WS handler)
        self._subscribe("agro.job.create",    self._on_job_create)
        self._subscribe("agro.job.update",    self._on_job_update)
        self._subscribe("agro.fuel.log",      self._on_fuel_log)
        self._subscribe("agro.expense.log",   self._on_expense_log)
        self._subscribe("agro.report.daily",  self._on_daily_report)
        # Generic request passthrough (used by internal agent-to-agent calls)
        self._subscribe("agent.request.agro", self._on_request)

        self._start_metrics_loop()

    # ── EventBus subscribers ──────────────────────────────────────────
    async def _on_request(self, event: Any) -> None:
        """Processes requests and bridges the gap to Flutter for two-way communication."""
        data = event.payload.get("data", {})
        reply_to = data.pop("_reply_to", None)
        result = await self.handle_goal(data)
        if reply_to:
            await self._emit(reply_to, result)

    async def _on_job_create(self, event) -> None:
        await self.handle_goal({"action": "log_job", **event.payload})

    async def _on_job_update(self, event) -> None:
        await self.handle_goal({"action": "update_job", **event.payload})

    async def _on_fuel_log(self, event) -> None:
        await self.handle_goal({"action": "log_fuel", **event.payload})

    async def _on_expense_log(self, event) -> None:
        await self.handle_goal({"action": "log_expense", **event.payload})

    async def _on_daily_report(self, event) -> None:
        await self.handle_goal({"action": "daily_report", **event.payload})

    # ── Core handler ──────────────────────────────────────────────────

    async def handle_goal(self, goal: Any) -> dict[str, Any]:
        """Dispatch structured action goals or fall back to LLM for natural language."""
        from agents.agro import database as db
        from agents.agro import excel_exporter as xl
        from agents.agro import analytics  # ← NEW: analytics engine for monthly_report

        if isinstance(goal, str):
            goal = {"action": "chat", "text": goal}

        action = goal.get("action", "")
        self._current_task_desc = action[:60]

        # ── log_job ───────────────────────────────────────────────────
        if action == "log_job":
            try:
                job_id = await db.create_job(goal)
                self._jobs_today += 1
                result = {
                    "success": True,
                    "job_id": job_id,
                    "message": f"Job #{job_id} logged successfully.",
                }
                await self._emit("agro.job.created", result)
                self._log.info(f"AgroAgent: job created #{job_id}")
                return result
            except Exception as exc:
                self._log.error(f"AgroAgent: log_job failed — {exc}")
                return {"success": False, "message": str(exc)}

        # ── update_job ────────────────────────────────────────────────
        elif action == "update_job":
            job_id = goal.get("job_id")
            status = goal.get("status", "")
            if not job_id or not status:
                return {"success": False, "message": "job_id and status are required."}
            try:
                await db.update_job_status(int(job_id), status)
                result = {"success": True, "job_id": job_id, "status": status, "message": f"Job #{job_id} status → {status}"}
                await self._emit("agro.job.updated", result)
                return result
            except Exception as exc:
                return {"success": False, "message": str(exc)}

        # ── log_fuel ──────────────────────────────────────────────────
        elif action == "log_fuel":
            try:
                fuel_id = await db.log_fuel(goal)
                result = {
                    "success": True,
                    "fuel_log_id": fuel_id,
                    "message": f"Fuel logged: {goal.get('liters', '')}L {goal.get('fuel_type', 'Diesel')}",
                }
                await self._emit("agro.fuel.logged", result)
                return result
            except Exception as exc:
                return {"success": False, "message": str(exc)}

        # ── log_expense ───────────────────────────────────────────────
        elif action == "log_expense":
            try:
                exp_id = await db.log_expense(goal)
                result = {
                    "success": True,
                    "expense_id": exp_id,
                    "message": f"Expense logged: Rs {goal.get('amount', '')} [{goal.get('category', '')}]",
                }
                await self._emit("agro.expense.logged", result)
                return result
            except Exception as exc:
                return {"success": False, "message": str(exc)}

        # ── daily_report ──────────────────────────────────────────────
        elif action == "daily_report":
            report_date = goal.get("date", date.today().isoformat())
            try:
                stats = await db.get_daily_stats(report_date)
                jobs  = await db.get_jobs(date=report_date)
                path  = await xl.generate_daily_report(stats, jobs, report_date)
                self._revenue_today = stats.get("revenue", 0)
                await self._emit("agro.report.generated", {
                    "type": "daily", "date": report_date, "path": path
                })
                return {
                    "success": True,
                    "path": path,
                    "stats": stats,
                    "message": (
                        f"Daily report for {report_date}: "
                        f"{stats['total_jobs']} jobs, "
                        f"Revenue Rs {stats['revenue']:,.0f}, "
                        f"Profit Rs {stats['profit']:,.0f}. "
                        f"Excel saved: {path}"
                    ),
                }
            except Exception as exc:
                self._log.error(f"AgroAgent: daily_report failed — {exc}")
                return {"success": False, "message": str(exc)}

        # ── monthly_report ────────────────────────────────────────────
        elif action == "monthly_report":
            raw_year  = goal.get("year")
            raw_month = goal.get("month")
            do_export = goal.get("export", False)

            if raw_year and raw_month:
                month_str = f"{int(raw_year):04d}-{int(raw_month):02d}"
            else:
                month_str = str(goal.get("month_str", date.today().strftime("%Y-%m")))

            try:
                summary = await analytics.monthly_summary(month_str)

                # Build Flutter-expected response shape:
                # stats  → flat monthly totals (matches MonthlyReportScreen expectations)
                # daily  → per-day array [{date, jobs, revenue, profit}]
                # breakdown → per-service [{service, count, revenue}]
                daily_raw = summary.get("daily", [])
                stats_payload = {
                    "year":            int(month_str[:4]),
                    "month":           int(month_str[5:7]),
                    "total_jobs":      summary.get("total_jobs", 0),
                    "completed_jobs":  sum(d.get("completed_jobs", 0) for d in daily_raw),
                    "pending_jobs":    sum(d.get("pending_jobs", 0)   for d in daily_raw),
                    "revenue":         summary.get("total_revenue", 0),
                    "fuel_cost":       summary.get("total_fuel_cost", 0),
                    "other_expenses":  0,
                    "total_expenses":  summary.get("total_fuel_cost", 0),
                    "profit":          summary.get("total_profit", 0),
                }
                daily_payload = [
                    {
                        "date":    d.get("date", ""),
                        "jobs":    d.get("total_jobs", 0),
                        "revenue": d.get("revenue", 0),
                        "profit":  d.get("profit", 0),
                    }
                    for d in daily_raw if d.get("total_jobs", 0) > 0
                ]
                # Build service breakdown from agri/transport split
                breakdown_payload = []
                if summary.get("agriculture_jobs", 0):
                    breakdown_payload.append({
                        "service": "Agriculture",
                        "count":   summary["agriculture_jobs"],
                        "revenue": summary.get("agriculture_revenue", 0),
                    })
                if summary.get("transport_jobs", 0):
                    breakdown_payload.append({
                        "service": "Transport",
                        "count":   summary["transport_jobs"],
                        "revenue": summary.get("transport_revenue", 0),
                    })

                file_path = None
                if do_export:
                    all_jobs   = await db.get_jobs(limit=1000)
                    month_jobs = [j for j in all_jobs
                                  if (j.get("scheduled_date") or "").startswith(month_str)]
                    file_path = await xl.generate_monthly_report(
                        month=month_str,
                        daily_stats=daily_raw,
                        all_jobs=month_jobs,
                        all_expenses=[],
                    )

                result = {
                    "success":   True,
                    "stats":     stats_payload,
                    "daily":     daily_payload,
                    "breakdown": breakdown_payload,
                    "file_path": file_path,
                    "message": (
                        f"Monthly report for {month_str}: "
                        f"{stats_payload['total_jobs']} jobs, "
                        f"Revenue Rs {stats_payload['revenue']:,.0f}, "
                        f"Profit Rs {stats_payload['profit']:,.0f}."
                    ),
                }
                await self._emit("agro.report.generated", result)
                self._log.info(f"AgroAgent: monthly_report ready for {month_str}")
                return result
            except Exception as exc:
                self._log.error(f"AgroAgent: monthly_report failed — {exc}")
                return {"success": False, "message": str(exc)}

        # ── get_jobs ──────────────────────────────────────────────────
        elif action == "get_jobs":
            try:
                jobs = await db.get_jobs(
                    date=goal.get("date"),
                    status=goal.get("status"),
                    job_type=goal.get("job_type"),
                    limit=goal.get("limit", 50),
                )
                return {"success": True, "jobs": jobs, "count": len(jobs)}
            except Exception as exc:
                return {"success": False, "message": str(exc)}

        # ── get_stats ─────────────────────────────────────────────────
        elif action == "get_stats":
            report_date = goal.get("date", date.today().isoformat())
            try:
                stats = await db.get_daily_stats(report_date)
                return {"success": True, "stats": stats}
            except Exception as exc:
                return {"success": False, "message": str(exc)}

        # ── issue_customer_pin (agro_client support) ───────────────────
        # Operator-only action: hands out a free 4-digit PIN to a customer
        # so they can log into the agro_client app with phone+PIN instead
        # of SMS OTP. Gated only by the operator app's own PIN lock —
        # uses agents.agro.customer_portal, never touches jobs/ directly.
        elif action == "issue_customer_pin":
            from agents.agro import customer_portal as cp
            phone = (goal.get("phone") or "").strip()
            pin   = (goal.get("pin") or "").strip()
            if not phone or not pin:
                return {"success": False, "message": "phone and pin are required."}
            return await cp.issue_pin(phone, pin)

        # ── get_pending_job_requests (agro_client support) ──────────────
        # Operator-only read: lists job requests submitted by customers
        # from agro_client that haven't been turned into a real job yet.
        elif action == "get_pending_job_requests":
            from agents.agro import customer_portal as cp
            requests = await cp.get_pending_job_requests()
            return {"success": True, "requests": requests, "count": len(requests)}

        # ── Natural language via LLM ──────────────────────────────────
        elif action in ("chat", "natural_language") or not action:
            text = goal.get("text") or goal.get("description") or goal.get("query") or ""
            if self._model and text:
                from agents.agro.constants import (
                    AGRI_SERVICES, TRANSPORT_MATERIALS, LAND_UNITS, TRANSPORT_UNITS, NEPALI_LABELS
                )
                system = (
                    "You are AGRO (Agent 07), the agriculture and transport business "
                    "manager of J.A.R.V.I.S for the Nawal Parasi family business, Nepal. "
                    f"Agriculture services: {AGRI_SERVICES}. "
                    f"Transport materials: {TRANSPORT_MATERIALS}. "
                    f"Land units: {LAND_UNITS} (1 Bigha = 20 Katha in Terai). "
                    f"Transport units: {TRANSPORT_UNITS} (Tali = one tractor trolley load). "
                    "You help operators log jobs, track fuel, record expenses, and understand "
                    "daily/monthly business performance. "
                    "Respond in the same language the user uses — Nepali or English. "
                    "Be concise and practical. For structured data requests, confirm what "
                    "action you will take and ask for any missing required fields."
                )
                try:
                    reply = await self.complete(text, system=system, max_tokens=800)
                    return {"success": True, "reply": reply}
                except Exception as exc:
                    return {"success": False, "message": str(exc)}
            return {"success": False, "message": "No action or text provided."}

        else:
            return {
                "success": False,
                "message": (
                    f"Unknown action: '{action}'. "
                    "Valid actions: log_job, update_job, log_fuel, log_expense, "
                    "daily_report, monthly_report, get_jobs, get_stats, chat."
                ),
            }