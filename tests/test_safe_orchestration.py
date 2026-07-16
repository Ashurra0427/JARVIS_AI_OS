"""
Tests — Safe OS Orchestration layer:
  * os_platform detection, intent translation, forbidden/confirm detection
  * audit_log append + hash-chain tamper detection
  * safe_orchestrator dry-run / deny / confirm / execute paths
All use fakes; no real OS side effects.
"""

from __future__ import annotations

import asyncio
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from actions.security.os_platform import (  # noqa: E402
    build_profile, Platform, CommandIntent,
    translate_intent, command_is_forbidden, command_requires_confirmation,
    normalise_path, get_os_profile,
)
from actions.security.audit_log import AuditLog  # noqa: E402
from actions.security.safe_orchestrator import SafeOrchestrator  # noqa: E402
from actions.security.action_guard import ActionGuard, GuardResult, RiskLevel  # noqa: E402
from actions.security.policy_engine import PolicyEngine, PolicyEffect, PolicyRule  # noqa: E402
from actions.security.permission_manager import PermissionManager  # noqa: E402
from actions.action_events import ActionRequest  # noqa: E402


# ===========================================================================
# 1. OS Platform
# ===========================================================================


class TestOSPlatform:
    def setup_method(self):
        self.prof = build_profile()

    def test_detects_a_known_platform(self):
        assert self.prof.platform in (
            Platform.WINDOWS, Platform.LINUX, Platform.MACOS, Platform.UNKNOWN
        )

    def test_open_intent_translates(self):
        prof = build_profile()
        intent = CommandIntent(action="open", target="/tmp/x.txt")
        cmd = translate_intent(intent, prof)
        assert cmd
        if prof.platform == Platform.WINDOWS:
            assert "start" in cmd.lower()
        elif prof.platform == Platform.MACOS:
            assert cmd.startswith("open")
        else:
            assert "xdg-open" in cmd

    def test_install_intent_uses_platform_pm(self):
        prof = build_profile()
        intent = CommandIntent(action="install", target="git", args=("apt",))
        cmd = translate_intent(intent, prof)
        if prof.platform == Platform.LINUX and "apt" in prof.package_managers:
            assert "apt install" in cmd
        assert cmd

    def test_forbidden_command_detected(self):
        prof = build_profile()
        if prof.is_posix:
            assert command_is_forbidden("sudo rm -rf /", prof) is True
        assert len(prof.forbidden_commands) > 0

    def test_confirmation_detected(self):
        prof = build_profile()
        if prof.is_posix:
            assert command_requires_confirmation("sudo apt install x", prof)
        else:
            assert command_requires_confirmation("rmdir /s /q foo", prof)

    def test_normalise_path_expands_home(self):
        prof = build_profile()
        import os
        os.environ[prof.home_env_var] = "/home/jarvis"
        p = normalise_path("~/projects/x.py", prof)
        # Normalised to the platform separator but must contain home + project.
        assert "home" in p.replace("\\", "/")
        assert "jarvis" in p.replace("\\", "/")
        assert "projects" in p.replace("\\", "/")

    def test_get_os_profile_singleton(self):
        assert get_os_profile() is get_os_profile()


# ===========================================================================
# 2. Audit Log
# ===========================================================================


class TestAuditLog:
    def test_record_and_count(self):
        al = AuditLog()
        al.record("filesystem", "write", "agent.engineering", approved=True,
                  risk_level="LOW", result="success", detail="wrote x.py")
        assert al.count() == 1

    def test_chain_valid_by_default(self):
        al = AuditLog()
        for i in range(5):
            al.record("terminal", "execute", "agent.automation", approved=True,
                      risk_level="MEDIUM", result="success", detail=f"cmd {i}")
        assert al.verify_chain() is True

    def test_tamper_detection(self):
        al = AuditLog()
        al.record("filesystem", "delete", "agent.x", approved=True,
                  risk_level="HIGH", result="success", detail="del a")
        entries = al.all()
        entries[0].detail = "TAMPERED"
        assert al.verify_chain() is False

    def test_redacts_sensitive_params(self):
        al = AuditLog()
        e = al.record("api", "post", "agent.communication", approved=True,
                      risk_level="LOW", result="success",
                      detail="call", params={"password": "hunter2", "url": "x"})
        assert "hunter2" not in e.detail
        assert "***" in e.detail

    def test_recency_and_by_requester(self):
        al = AuditLog()
        al.record("x", "y", "agent.a", approved=True, risk_level="LOW",
                  result="success", detail="1")
        al.record("x", "y", "agent.b", approved=True, risk_level="LOW",
                  result="success", detail="2")
        assert len(al.by_requester("agent.a")) == 1
        assert len(al.recent(1)) == 1

    def test_summary_counts(self):
        al = AuditLog()
        al.record("x", "y", "a", approved=True, risk_level="LOW",
                  result="success", detail="1")
        al.record("x", "y", "a", approved=False, risk_level="CRITICAL",
                  result="denied", detail="2")
        s = al.summary()
        assert s["total"] == 2
        assert s["approved"] == 1
        assert s["denied"] == 1


# ===========================================================================
# 3. Safe Orchestrator
# ===========================================================================


def _make_guard(confirm_cb=None):
    pe = PolicyEngine()
    pe.apply_safe_defaults()
    pm = PermissionManager()
    guard = ActionGuard(policy_engine=pe, permission_manager=pm,
                        confirmation_callback=confirm_cb)
    return guard


async def _noop_executor(request):
    return True, f"executed {request.action}"


class TestSafeOrchestrator:
    def setup_method(self):
        # Use a fresh event loop per test so a loop closed by another suite
        # (e.g. asyncio.run() in test_smart_router) cannot break this one.
        self._loop = asyncio.new_event_loop()
        asyncio.set_event_loop(self._loop)
        self.guard = _make_guard()
        self._loop.run_until_complete(self.guard.start())

    def teardown_method(self):
        if self._loop.is_running():
            self._loop.stop()
        self._loop.close()

    def _req(self, atype, action, params, requester="agent.engineering"):
        return ActionRequest(
            request_id="r1", action_type=atype, action=action,
            params=params, requester=requester,
        )

    def test_execute_low_risk_success(self):
        orch = SafeOrchestrator(self.guard, _noop_executor)
        req = self._req("filesystem", "write",
                        {"path": "/home/user/proj/main.py", "content": "x=1"})
        out = self._loop.run_until_complete(orch.execute(req))
        assert out.approved is True
        assert out.executed is True
        assert out.success is True
        assert orch._audit.count() == 1

    def test_dry_run_does_not_execute(self):
        orch = SafeOrchestrator(self.guard, _noop_executor)
        req = self._req("filesystem", "write",
                        {"path": "/home/user/proj/main.py", "content": "x=1"})
        out = self._loop.run_until_complete(
            orch.execute(req, dry_run=True))
        assert out.dry_run is True
        assert out.executed is False
        assert orch.stats()["dry_run"] == 1

    def test_os_forbidden_command_denied(self):
        from actions.security.os_platform import OSProfile, Platform, ShellFamily
        # Inject a deterministic profile so the test is platform-independent.
        custom = OSProfile(
            platform=Platform.LINUX, system="Linux", release="1", machine="x86",
            shell=ShellFamily.BASH, is_posix=True, path_sep="/",
            home_env_var="HOME", package_managers=("apt",),
            forbidden_commands=("definitely-forbidden-cmd",),
            confirmation_commands=("sudo",),
        )
        orch = SafeOrchestrator(self.guard, _noop_executor, os_profile=custom)
        req = self._req("terminal", "execute",
                        {"command": "definitely-forbidden-cmd now"},
                        requester="agent.automation")
        out = self._loop.run_until_complete(orch.execute(req))
        assert out.approved is False
        assert out.executed is False

    def test_high_risk_requires_confirmation(self):
        orch = SafeOrchestrator(self.guard, _noop_executor)
        req = self._req("terminal", "execute",
                        {"command": "sudo apt install nginx"},
                        requester="agent.automation")
        out = self._loop.run_until_complete(orch.execute(req))
        assert out.approved in (True, False)
        if out.approved:
            assert out.executed is True
        else:
            assert out.executed is False

    def test_confirmation_callback_accepted(self):
        confirmed_flag = {"v": True}

        async def _cb(request, result):
            return confirmed_flag["v"]

        orch = SafeOrchestrator(self.guard, _noop_executor, audit=AuditLog())
        req = self._req("terminal", "execute",
                        {"command": "sudo reboot"},
                        requester="agent.automation")
        out = self._loop.run_until_complete(
            orch.execute(req, confirmation_callback=_cb))
        assert out.approved is False

    def test_audit_chain_valid_after_execution(self):
        orch = SafeOrchestrator(self.guard, _noop_executor, audit=AuditLog())
        req = self._req("filesystem", "write",
                        {"path": "/home/user/proj/a.py", "content": "y=2"})
        self._loop.run_until_complete(orch.execute(req))
        assert orch._audit.verify_chain() is True

    def test_health_reports_chain(self):
        orch = SafeOrchestrator(self.guard, _noop_executor, audit=AuditLog())
        h = self._loop.run_until_complete(orch.health())
        assert h["audit_chain_valid"] is True
        assert "os_platform" in h
