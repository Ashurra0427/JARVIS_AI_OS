"""
tests/test_file_manager_enforcement.py
──────────────────────────────────────
File manager and security sandbox enforcement tests.

Acceptance criteria:
  ✓ FilePermissions.check() rejects write/delete outside allowed paths.
  ✓ FilePermissions.check() allows write/delete inside allowed paths.
  ✓ FileManager.write() / .delete() enforce permissions and return
    a failed FileActionResult (not raise) when sandbox is violated.
  ✓ ActionGuard._detect_dangerous_patterns() blocks write/delete to
    hardcoded protected paths (/etc/passwd, /boot, /sys, etc.).
  ✓ SecurityIntegration.check() blocks file.write to /etc/ via
    PolicyEngine deny-write-etc rule.
  ✓ SecurityIntegration.check() allows file.write to /tmp.
  ✓ SecurityIntegration.check() blocks file.delete to /etc/ via
    PolicyEngine deny-delete-outside-tmp rule.
  ✓ SecurityIntegration.health_snapshot() reports file_manager status.
  ✓ ActionGuard without file_manager wiring does not crash.

NOTE: ActionGuard does NOT accept a file_manager constructor argument —
      path-sandbox enforcement lives in FilePermissions / FileManager.
      ActionGuard._detect_dangerous_patterns() enforces a hardcoded set
      of critically protected OS paths (/etc/passwd, /boot, /sys, /dev …)
      independent of FilePermissions.

Usage:
  cd <project_root>
  python -m pytest tests/test_file_manager_enforcement.py -v
"""

from __future__ import annotations

import asyncio
import os
import pathlib
import sys
import uuid

_ROOT = str(pathlib.Path(__file__).resolve().parent.parent)
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)


# ──────────────────────────────────────────────────────────────────────────────
# Helpers
# ──────────────────────────────────────────────────────────────────────────────

def _run(coro):
    return asyncio.run(coro)


def _make_request(action_type, action, params=None, requester="test.agent"):
    from actions.action_events import ActionRequest
    return ActionRequest(
        request_id=str(uuid.uuid4())[:8],
        action_type=action_type,
        action=action,
        params=params or {},
        requester=requester,
    )


# ──────────────────────────────────────────────────────────────────────────────
# Section 1: FilePermissions unit tests — the real sandbox layer
# ──────────────────────────────────────────────────────────────────────────────

class TestFilePermissionsSandbox:
    """
    Unit tests for FilePermissions.check() — the actual path-sandbox.
    ActionGuard does not hold a FilePermissions instance; FileManager does.
    """

    def test_write_outside_allowed_paths_denied(self):
        from actions.filesystem.file_permissions import FilePermissions
        perms = FilePermissions(
            allowed_read_paths=["/tmp"],
            allowed_write_paths=["/tmp"],
            allowed_delete_paths=["/tmp"],
        )
        result = perms.check("write", "/etc/malicious.conf")
        assert not result.allowed, (
            "Write to /etc/malicious.conf must be denied by FilePermissions sandbox"
        )
        print(f"✓ FilePermissions: write /etc/malicious.conf → denied: {result.reasons}")

    def test_delete_outside_allowed_paths_denied(self):
        from actions.filesystem.file_permissions import FilePermissions
        home = str(pathlib.Path.home())
        target = os.path.join(home, "important.txt")
        perms = FilePermissions(
            allowed_read_paths=[home, "/tmp"],
            allowed_write_paths=["/tmp"],
            allowed_delete_paths=["/tmp"],
        )
        result = perms.check("delete", target)
        assert not result.allowed, (
            f"Delete of {target} must be denied — outside allowed_delete_paths"
        )
        print(f"✓ FilePermissions: delete {target} → denied")

    def test_write_to_tmp_allowed(self):
        from actions.filesystem.file_permissions import FilePermissions
        perms = FilePermissions(
            allowed_read_paths=["/tmp"],
            allowed_write_paths=["/tmp"],
            allowed_delete_paths=["/tmp"],
        )
        result = perms.check("write", "/tmp/jarvis_test_output.txt")
        assert result.allowed, (
            f"Write to /tmp must be allowed; reasons: {result.reasons}"
        )
        print("✓ FilePermissions: write /tmp/jarvis_test_output.txt → allowed")

    def test_delete_from_tmp_allowed(self):
        from actions.filesystem.file_permissions import FilePermissions
        perms = FilePermissions(
            allowed_read_paths=["/tmp"],
            allowed_write_paths=["/tmp"],
            allowed_delete_paths=["/tmp"],
        )
        result = perms.check("delete", "/tmp/scratch.txt")
        assert result.allowed, (
            f"Delete from /tmp must be allowed; reasons: {result.reasons}"
        )
        print("✓ FilePermissions: delete /tmp/scratch.txt → allowed")

    def test_read_allowed_when_write_blocked(self):
        """Read must be checked against allowed_read_paths, independent of write paths."""
        from actions.filesystem.file_permissions import FilePermissions
        perms = FilePermissions(
            allowed_read_paths=["/tmp"],
            allowed_write_paths=[],   # no writes allowed
            allowed_delete_paths=[],
        )
        result = perms.check("read", "/tmp/notes.txt")
        assert result.allowed, (
            f"Read of /tmp/notes.txt must be allowed even with empty write paths: {result.reasons}"
        )
        print("✓ FilePermissions: read /tmp/notes.txt → allowed (independent of write paths)")

    def test_write_outside_all_paths_denied(self):
        from actions.filesystem.file_permissions import FilePermissions
        cwd = str(pathlib.Path.cwd())
        perms = FilePermissions(
            allowed_read_paths=["/tmp"],
            allowed_write_paths=["/tmp"],  # cwd not included
            allowed_delete_paths=["/tmp"],
        )
        target = os.path.join(cwd, "should_be_blocked.txt")
        result = perms.check("write", target)
        assert not result.allowed, (
            f"Write to cwd {target} must be denied when cwd not in allowed_write_paths"
        )
        print(f"✓ FilePermissions: write {target} (cwd not in allowed) → denied")


# ──────────────────────────────────────────────────────────────────────────────
# Section 2: FileManager integration — sandbox enforced via FilePermissions
# ──────────────────────────────────────────────────────────────────────────────

class TestFileManagerSandboxEnforcement:
    """
    FileManager.write() / .delete() must return a failed FileActionResult
    (not raise) when the path is outside the configured sandbox.
    """

    def test_write_outside_sandbox_returns_failed_result(self):
        from actions.filesystem.file_manager import FileManager
        fm = FileManager(
            event_bus=None,
            allowed_read_paths=["/tmp"],
            allowed_write_paths=["/tmp"],
            allowed_delete_paths=["/tmp"],
        )

        async def _t():
            await fm.start()
            return await fm.write("/etc/evil.conf", "bad content", requester="test.agent")

        result = _run(_t())
        assert not result.success, (
            "FileManager.write to /etc/ must fail — outside allowed_write_paths"
        )
        assert "denied" in result.error.lower() or "permission" in result.error.lower(), (
            f"Error must mention denial/permission: {result.error}"
        )
        print(f"✓ FileManager.write /etc/evil.conf → failed: {result.error}")

    def test_delete_outside_sandbox_returns_failed_result(self):
        from actions.filesystem.file_manager import FileManager
        home = str(pathlib.Path.home())
        target = os.path.join(home, "important.txt")
        fm = FileManager(
            event_bus=None,
            allowed_read_paths=[home, "/tmp"],
            allowed_write_paths=["/tmp"],
            allowed_delete_paths=["/tmp"],
        )

        async def _t():
            await fm.start()
            return await fm.delete(target, requester="test.agent")

        result = _run(_t())
        assert not result.success, (
            f"FileManager.delete {target} must fail — outside allowed_delete_paths"
        )
        print(f"✓ FileManager.delete {target} → failed: {result.error}")

    def test_no_file_manager_guard_does_not_crash(self):
        """
        ActionGuard without a file_manager (its normal state) must not crash.
        It uses _detect_dangerous_patterns() for hardcoded protected paths only.
        """
        from actions.security.action_guard import ActionGuard
        from actions.security.policy_engine import PolicyEngine
        from actions.security.permission_manager import PermissionManager

        pe = PolicyEngine(default_allow=True)
        pm = PermissionManager(event_bus=None, audit_log_enabled=False)
        # ActionGuard has no file_manager param — this is the normal construction
        guard = ActionGuard(
            event_bus=None,
            permission_manager=pm,
            policy_engine=pe,
            auto_block_threshold=0.85,
            confirm_threshold=0.70,
        )

        async def _t():
            await guard.start()
            req = _make_request("filesystem", "write", {"path": "/tmp/test.txt"})
            return await guard.evaluate(req)

        result = _run(_t())
        assert hasattr(result, "approved"), "Guard must return a GuardResult"
        print(f"✓ ActionGuard (no file_manager) → no crash; approved={result.approved}")


# ──────────────────────────────────────────────────────────────────────────────
# Section 3: ActionGuard._detect_dangerous_patterns() — hardcoded protected paths
# ──────────────────────────────────────────────────────────────────────────────

class TestActionGuardProtectedPaths:
    """
    ActionGuard._detect_dangerous_patterns() blocks write/delete to a hardcoded
    set of critically protected OS paths (/etc/passwd, /boot, /sys, /dev, etc.)
    independent of any FilePermissions sandbox configuration.
    """

    def test_write_to_protected_path_blocked(self):
        """Write to /etc/passwd (hardcoded protected) must score 1.0 and be blocked."""
        from actions.security.action_guard import ActionGuard
        from actions.security.policy_engine import PolicyEngine

        pe = PolicyEngine(default_allow=True)
        guard = ActionGuard(
            event_bus=None,
            policy_engine=pe,
            auto_block_threshold=0.85,
            confirm_threshold=0.70,
        )

        async def _t():
            await guard.start()
            req = _make_request("filesystem", "write", {"path": "/etc/passwd"})
            return await guard.evaluate(req)

        result = _run(_t())
        assert not result.approved, (
            "Write to /etc/passwd must be blocked by ActionGuard protected-path detection"
        )
        print(f"✓ ActionGuard: write /etc/passwd → blocked, reasons={result.reasons}")

    def test_delete_to_protected_path_blocked(self):
        """Delete to /boot must score 1.0 and be blocked."""
        from actions.security.action_guard import ActionGuard
        from actions.security.policy_engine import PolicyEngine

        pe = PolicyEngine(default_allow=True)
        guard = ActionGuard(
            event_bus=None,
            policy_engine=pe,
            auto_block_threshold=0.85,
            confirm_threshold=0.70,
        )

        async def _t():
            await guard.start()
            req = _make_request("filesystem", "delete", {"path": "/boot/grub.cfg"})
            return await guard.evaluate(req)

        result = _run(_t())
        assert not result.approved, (
            "Delete of /boot/grub.cfg must be blocked by ActionGuard protected-path detection"
        )
        print(f"✓ ActionGuard: delete /boot/grub.cfg → blocked, reasons={result.reasons}")

    def test_read_to_protected_path_not_blocked_by_pattern_check(self):
        """
        _detect_dangerous_patterns only triggers for write/delete/move —
        not for read. A read of /etc/passwd must pass pattern detection
        (though it may be denied by other means).
        """
        from actions.security.action_guard import ActionGuard
        guard = ActionGuard(event_bus=None, auto_block_threshold=0.85, confirm_threshold=0.70)

        score, reasons = guard._detect_dangerous_patterns(
            _make_request("filesystem", "read", {"path": "/etc/passwd"})
        )
        assert not any("protected path" in r for r in reasons), (
            f"Read must not trigger protected-path pattern detection: {reasons}"
        )
        print("✓ ActionGuard: read /etc/passwd not caught by _detect_dangerous_patterns")


# ──────────────────────────────────────────────────────────────────────────────
# Section 4: SecurityIntegration end-to-end
# ──────────────────────────────────────────────────────────────────────────────

class TestSecurityIntegrationFileManagerEnforcement:
    """
    End-to-end: SecurityIntegration.check() must block file.write to /etc/
    via the PolicyEngine deny-write-etc rule, and allow file.write to /tmp.
    """

    def _make_si(self):
        from actions.security.security_integration import SecurityIntegration
        return SecurityIntegration(event_bus=None)

    def test_write_to_etc_blocked_via_policy(self):
        """
        SecurityIntegration has a deny-write-etc PolicyRule covering /etc*.
        file.write to /etc/evil.conf must be blocked.
        """
        si = self._make_si()

        async def _run():
            await si.initialize()
            approved, reason = await si.check(
                tool_name="file.write",
                kwargs={"path": "/etc/evil.conf"},
                requester="test.agent",
            )
            return approved, reason

        approved, reason = asyncio.run(_run())
        assert not approved, (
            f"file.write to /etc/ must be BLOCKED by PolicyEngine deny-write-etc rule; "
            f"got approved=True"
        )
        assert "[SECURITY]" in reason or "denied" in reason.lower() or "blocked" in reason.lower(), (
            f"Denial reason must reference security/policy: {reason}"
        )
        print(f"✓ SI.check file.write /etc/evil.conf → BLOCKED: {reason}")

    def test_write_to_tmp_approved(self):
        """
        file.write to /tmp must pass — no deny rule covers /tmp writes.
        """
        si = self._make_si()

        async def _run():
            await si.initialize()
            approved, reason = await si.check(
                tool_name="file.write",
                kwargs={"path": "/tmp/jarvis_output.txt"},
                requester="test.agent",
            )
            return approved, reason

        approved, reason = asyncio.run(_run())
        assert approved, (
            f"file.write to /tmp must be APPROVED via SecurityIntegration, got reason: {reason}"
        )
        print("✓ SI.check file.write /tmp/jarvis_output.txt → APPROVED")

    def test_delete_to_etc_blocked_via_policy(self):
        """
        SecurityIntegration has a deny-delete-outside-tmp PolicyRule for /etc*.
        file.delete to /etc/something must be blocked.
        """
        si = self._make_si()

        async def _run():
            await si.initialize()
            approved, reason = await si.check(
                tool_name="file.delete",
                kwargs={"path": "/etc/cron.d/malicious"},
                requester="test.agent",
            )
            return approved, reason

        approved, reason = asyncio.run(_run())
        assert not approved, (
            f"file.delete to /etc/ must be BLOCKED by PolicyEngine; "
            f"got approved=True; reason={reason}"
        )
        print(f"✓ SI.check file.delete /etc/cron.d/malicious → BLOCKED: {reason}")

    def test_health_snapshot_reports_file_manager_status(self):
        """
        health_snapshot() must report initialized=True and include file_manager
        in components with a known status value.
        """
        si = self._make_si()

        async def _run():
            await si.initialize()
            return si.health_snapshot()

        snap = asyncio.run(_run())
        assert snap["initialized"] is True
        assert "file_manager" in snap["components"]
        assert snap["components"]["file_manager"] in ("ok", "not loaded"), (
            f"Unexpected file_manager status: {snap['components']['file_manager']}"
        )
        print(f"✓ health_snapshot file_manager status: {snap['components']['file_manager']}")


# ──────────────────────────────────────────────────────────────────────────────
# Runner (for python tests/test_file_manager_enforcement.py)
# ──────────────────────────────────────────────────────────────────────────────

def _run_section(cls, label: str) -> int:
    instance = cls()
    failures = 0
    print(f"\n{'='*60}")
    print(f"  {label}")
    print(f"{'='*60}")
    for name in sorted(dir(instance)):
        if not name.startswith("test_"):
            continue
        method = getattr(instance, name)
        try:
            method()
        except AssertionError as exc:
            print(f"  ✗ FAIL  {name}: {exc}")
            failures += 1
        except Exception as exc:
            print(f"  ✗ ERROR {name}: {type(exc).__name__}: {exc}")
            import traceback
            traceback.print_exc()
            failures += 1
    return failures


if __name__ == "__main__":
    total = 0
    total += _run_section(
        TestFilePermissionsSandbox,
        "FilePermissions sandbox — unit tests",
    )
    total += _run_section(
        TestFileManagerSandboxEnforcement,
        "FileManager sandbox enforcement — integration tests",
    )
    total += _run_section(
        TestActionGuardProtectedPaths,
        "ActionGuard protected-path detection — unit tests",
    )
    total += _run_section(
        TestSecurityIntegrationFileManagerEnforcement,
        "SecurityIntegration end-to-end — policy enforcement",
    )
    print(f"\n{'='*60}")
    if total == 0:
        print("  ALL FILE MANAGER ENFORCEMENT TESTS PASSED ✓")
    else:
        print(f"  {total} TEST(S) FAILED ✗")
    print(f"{'='*60}")
    sys.exit(0 if total == 0 else 1)