"""
JARVIS AI OS — Action Audit Log
=================================

An append-only, tamper-evident audit trail of every *mutating* action the
system attempts (write, delete, move, terminal command, app launch, API
mutation). This is the accountability layer that makes "orchestrate the OS
without harming it" verifiable: if something goes wrong, the audit log shows
exactly what ran, by which agent, with what result.

Design
------
* Thread/async safe (uses a lock around the write path).
* Pluggable sinks: in-memory ring buffer (default) + optional file sink.
* Each entry is a structured dict with a monotonic sequence id and timestamp.
* ``verify_chain`` recomputes the running hash chain so tampering is detectable.
* Privacy: secret values are NOT stored; only command shapes / paths.

No network or external dependencies. Safe to construct anywhere.
"""

from __future__ import annotations

import hashlib
import json
import threading
import time
from dataclasses import dataclass, field
from typing import Any

from observability.logging.logger import get_logger

log = get_logger(__name__)


# Keys whose values must never be persisted to the audit log.
_SENSITIVE_KEYS = (
    "password", "secret", "token", "api_key", "apikey", "authorization",
    "auth", "credential", "private_key", "cookie", "session",
)


@dataclass
class AuditEntry:
    """One audited action record."""

    seq: int
    timestamp: float
    action_type: str
    action: str
    requester: str
    approved: bool
    risk_level: str
    result: str            # "success" | "denied" | "error" | "pending"
    detail: str
    guard_id: str = ""
    correlation_id: str = ""
    prev_hash: str = ""
    entry_hash: str = ""

    def redacted_params(self, params: dict) -> dict:
        return {
            k: ("***redacted***" if k.lower() in _SENSITIVE_KEYS else v)
            for k, v in params.items()
        }

    def as_dict(self) -> dict[str, Any]:
        return {
            "seq": self.seq,
            "timestamp": self.timestamp,
            "action_type": self.action_type,
            "action": self.action,
            "requester": self.requester,
            "approved": self.approved,
            "risk_level": self.risk_level,
            "result": self.result,
            "detail": self.detail,
            "guard_id": self.guard_id,
            "correlation_id": self.correlation_id,
            "prev_hash": self.prev_hash,
            "entry_hash": self.entry_hash,
        }


def _hash_entry(prev_hash: str, entry: dict) -> str:
    payload = json.dumps(
        {k: entry[k] for k in (
            "seq", "timestamp", "action_type", "action", "requester",
            "approved", "risk_level", "result", "detail",
        )},
        sort_keys=True, default=str,
    )
    return hashlib.sha256((prev_hash + "|" + payload).encode("utf-8")).hexdigest()


class AuditLog:
    """
    Append-only audit trail with a hash chain.

    Example
    -------
        al = AuditLog()
        al.record("filesystem", "write", "agent.engineering",
                  approved=True, risk_level="LOW", result="success",
                  detail="wrote main.py (1200 bytes)")
        assert al.verify_chain() is True
    """

    def __init__(self, max_entries: int = 10_000, file_path: str | None = None) -> None:
        self._entries: list[AuditEntry] = []
        self._max = max(1, max_entries)
        self._file_path = file_path
        self._lock = threading.RLock()
        self._seq = 0
        self._last_hash = "GENESIS"

    # -- recording ---------------------------------------------------------

    def record(
        self,
        action_type: str,
        action: str,
        requester: str,
        *,
        approved: bool,
        risk_level: str,
        result: str,
        detail: str,
        guard_id: str = "",
        correlation_id: str = "",
        params: dict | None = None,
    ) -> AuditEntry:
        with self._lock:
            self._seq += 1
            seq = self._seq
            prev_hash = self._last_hash
            # Redact sensitive params into detail if provided.
            if params:
                redacted = {
                    k: ("***" if k.lower() in _SENSITIVE_KEYS else v)
                    for k, v in params.items()
                }
                detail = f"{detail} | params={redacted}"
            entry = AuditEntry(
                seq=seq,
                timestamp=time.time(),
                action_type=action_type,
                action=action,
                requester=requester,
                approved=approved,
                risk_level=risk_level,
                result=result,
                detail=detail,
                guard_id=guard_id,
                correlation_id=correlation_id,
                prev_hash=prev_hash,
            )
            entry.entry_hash = _hash_entry(prev_hash, entry.as_dict())
            self._last_hash = entry.entry_hash
            self._entries.append(entry)
            if len(self._entries) > self._max:
                self._entries = self._entries[-self._max:]
            if self._file_path:
                self._append_to_file(entry)
            return entry

    def _append_to_file(self, entry: AuditEntry) -> None:
        try:
            with open(self._file_path, "a", encoding="utf-8") as fh:
                fh.write(json.dumps(entry.as_dict(), default=str) + "\n")
        except Exception as exc:  # pragma: no cover - best effort sink
            log.warning("AuditLog file sink failed", error=str(exc))

    # -- queries -----------------------------------------------------------

    def all(self) -> list[AuditEntry]:
        with self._lock:
            return list(self._entries)

    def recent(self, n: int = 50) -> list[AuditEntry]:
        with self._lock:
            return list(self._entries[-n:])

    def by_requester(self, requester: str) -> list[AuditEntry]:
        with self._lock:
            return [e for e in self._entries if e.requester == requester]

    def by_result(self, result: str) -> list[AuditEntry]:
        with self._lock:
            return [e for e in self._entries if e.result == result]

    def count(self) -> int:
        with self._lock:
            return len(self._entries)

    def summary(self) -> dict[str, Any]:
        with self._lock:
            approved = sum(1 for e in self._entries if e.approved)
            denied = sum(1 for e in self._entries if e.result == "denied")
            errors = sum(1 for e in self._entries if e.result == "error")
            return {
                "total": len(self._entries),
                "approved": approved,
                "denied": denied,
                "errors": errors,
                "last_hash": self._last_hash,
            }

    # -- integrity ---------------------------------------------------------

    def verify_chain(self) -> bool:
        """
        Recompute the hash chain from scratch. Returns False if any entry was
        altered (tamper detection).
        """
        with self._lock:
            prev = "GENESIS"
            for e in self._entries:
                expected = _hash_entry(prev, e.as_dict())
                if expected != e.entry_hash:
                    return False
                if e.prev_hash != prev:
                    return False
                prev = e.entry_hash
            return True

    def clear(self) -> None:
        """Clear the in-memory log (does NOT delete an external file sink)."""
        with self._lock:
            self._entries.clear()
            self._seq = 0
            self._last_hash = "GENESIS"


# ---------------------------------------------------------------------------
# Singleton
# ---------------------------------------------------------------------------

_audit_instance: AuditLog | None = None
_audit_lock = threading.Lock()


def get_audit_log(file_path: str | None = None) -> AuditLog:
    global _audit_instance
    with _audit_lock:
        if _audit_instance is None:
            _audit_instance = AuditLog(file_path=file_path)
    return _audit_instance
