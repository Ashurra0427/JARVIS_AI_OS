"""
JARVIS AI OS — File Permissions
==================================
Path-level permission checks for the filesystem action layer.

Responsibilities:
  - Validate that a given path is within allowed sandboxes
  - Enforce read-only vs. read-write zones
  - Block access to sensitive system paths
  - Block access to secret/key files (.env, private keys, credential stores) — P-08
  - Return structured PermissionResult

Rules:
  - Pure validation — no I/O performed here
  - Called by FileManager before every operation
  - System paths are never writable regardless of policy
  - Secret files are never readable OR writable by agents — P-08
"""

from __future__ import annotations

import os
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Sequence

# P2-F: Read configurable file size limit from settings (default: 50 MB).
# Can be overridden in config YAML (max_file_size_mb) or env JARVIS_MAX_FILE_SIZE_MB.
try:
    from config.settings import ConfigManager as _ConfigManager
    _settings_cfg = _ConfigManager()._config
    _DEFAULT_MAX_FILE_SIZE_BYTES = getattr(_settings_cfg, "max_file_size_mb", 50) * 1024 * 1024
except Exception:
    _DEFAULT_MAX_FILE_SIZE_BYTES = 50 * 1024 * 1024  # 50 MB fallback


# ---------------------------------------------------------------------------
# System paths that are always off-limits
# ---------------------------------------------------------------------------

_SYSTEM_READ_ONLY: frozenset[str] = frozenset(
    {
        "/etc",
        "/bin",
        "/sbin",
        "/usr/bin",
        "/usr/sbin",
        "/lib",
        "/lib64",
        "/usr/lib",
        "/boot",
        "/proc",
        "/sys",
        "/dev",
        "C:\\Windows",
        "C:\\Program Files",
        "C:\\Program Files (x86)",
    }
)

_ALWAYS_BLOCKED: frozenset[str] = frozenset(
    {
        "/etc/passwd",
        "/etc/shadow",
        "/etc/sudoers",
        "/etc/ssh",
        "~/.ssh",
        "~/.gnupg",
        "/root",
    }
)

# Extensions that are never writable
_BLOCKED_EXTENSIONS: frozenset[str] = frozenset(
    {
        ".exe",
        ".dll",
        ".so",
        ".dylib",
        ".sys",
        ".ko",
    }
)


# ---------------------------------------------------------------------------
# P-08: Secret / credential file patterns (read AND write blocked for agents)
# ---------------------------------------------------------------------------

# Exact filenames (case-insensitive stem match)
_SECRET_FILENAMES: frozenset[str] = frozenset(
    {
        ".env",
        ".env.local",
        ".env.production",
        ".env.staging",
        ".env.development",
        ".env.test",
        ".env.example",       # may contain real-looking keys — block writes
        ".envrc",             # direnv
        ".netrc",             # FTP/HTTP credentials
        ".authinfo",          # Emacs auth-source
        ".pgpass",            # PostgreSQL password file
        ".my.cnf",            # MySQL credentials
        ".boto",              # AWS boto / gsutil credentials
        "credentials",        # AWS ~/.aws/credentials, GCP, etc.
        "secrets.yaml",
        "secrets.yml",
        "secrets.json",
        "secrets.toml",
        "vault.yaml",
        "vault.yml",
        "vault.json",
        "keystore.jks",
        "keystore.p12",
        "keystore.pfx",
    }
)

# Filename glob-style patterns (matched against the filename only, case-insensitive)
_SECRET_FILENAME_PATTERNS: tuple[re.Pattern, ...] = tuple(
    re.compile(p, re.IGNORECASE)
    for p in (
        r"^\.env(\..+)?$",          # .env, .env.local, .env.anything
        r"^.*\.pem$",               # PEM keys / certs
        r"^.*\.key$",               # generic private keys
        r"^.*\.p12$",               # PKCS#12 bundles
        r"^.*\.pfx$",               # same
        r"^.*\.jks$",               # Java KeyStore
        r"^.*_rsa$",                # SSH private keys
        r"^.*_dsa$",
        r"^.*_ecdsa$",
        r"^.*_ed25519$",
        r"^id_rsa$",
        r"^id_dsa$",
        r"^id_ecdsa$",
        r"^id_ed25519$",
        r"^.*\.ppk$",               # PuTTY private key
        r"^.*secret.*\.(json|yaml|yml|toml|env)$",  # *secret*.json etc.
        r"^.*credential.*\.(json|yaml|yml|toml)$",
        r"^service.account.*\.json$",               # GCP service account keys
        r"^.*api.?key.*\.(txt|json|env)$",
        r"^\.kubeconfig$",
        r"^kubeconfig(\.yaml)?$",
        r"^token(\.txt)?$",
        r"^access.?token.*$",
    )
)

# Directory paths that contain credentials (block all access when traversed)
_SECRET_DIRECTORIES: tuple[str, ...] = (
    "~/.aws",
    "~/.gcloud",
    "~/.config/gcloud",
    "~/.azure",
    "~/.kube",
    "~/.ssh",
    "~/.gnupg",
    "~/.password-store",
    "~/.local/share/keyrings",
)


def _is_secret_file(path: Path) -> bool:
    """
    Return True if the path looks like a secret / credential file.

    Checks (in order):
      1. File is inside a known secret directory
      2. Filename matches an exact known-secret name
      3. Filename matches a regex pattern
    """
    path_str = str(path)

    # 1. Secret directory prefix
    for secret_dir in _SECRET_DIRECTORIES:
        expanded = str(Path(secret_dir).expanduser())
        if path_str == expanded or path_str.startswith(expanded + os.sep):
            return True

    filename = path.name.lower()

    # 2. Exact filename match
    if filename in _SECRET_FILENAMES:
        return True

    # 3. Regex patterns (matched against original-case filename)
    for pattern in _SECRET_FILENAME_PATTERNS:
        if pattern.match(path.name):
            return True

    return False


# ---------------------------------------------------------------------------
# Result type
# ---------------------------------------------------------------------------


@dataclass
class PermissionResult:
    allowed: bool
    reasons: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    resolved_path: str = ""

    def deny(self, reason: str) -> "PermissionResult":
        self.allowed = False
        self.reasons.append(reason)
        return self

    def warn(self, warning: str) -> "PermissionResult":
        self.warnings.append(warning)
        return self


# ---------------------------------------------------------------------------
# FilePermissions
# ---------------------------------------------------------------------------


class FilePermissions:
    """
    Validates filesystem operations against configured sandbox zones.

    Usage:
        perms = FilePermissions(
            allowed_read_paths=["/home/user/projects", "/tmp"],
            allowed_write_paths=["/home/user/projects", "/tmp"],
        )
        result = perms.check(operation="write", path="/tmp/output.txt")

    P-08 additions:
        Secret and credential files (.env, *.pem, *.key, AWS credentials, etc.)
        are blocked for ALL agent operations regardless of sandbox configuration.
        Only an explicit ``allow_secret_files=True`` flag (never set in production)
        bypasses this protection.
    """

    def __init__(
        self,
        allowed_read_paths: Sequence[str] | None = None,
        allowed_write_paths: Sequence[str] | None = None,
        allowed_delete_paths: Sequence[str] | None = None,
        extra_blocked_paths: Sequence[str] | None = None,
        allow_hidden_files: bool = False,
        max_file_size_bytes: int = _DEFAULT_MAX_FILE_SIZE_BYTES,  # P2-F: from settings
        # P-08: set True ONLY in controlled test scenarios — never in production
        allow_secret_files: bool = False,
    ) -> None:
        self._read_roots = [
            Path(p).expanduser().resolve() for p in (allowed_read_paths or [])
        ]
        self._write_roots = [
            Path(p).expanduser().resolve() for p in (allowed_write_paths or [])
        ]
        self._delete_roots = [
            Path(p).expanduser().resolve() for p in (allowed_delete_paths or [])
        ]
        self._extra_blocked = [
            Path(p).expanduser().resolve() for p in (extra_blocked_paths or [])
        ]
        self._allow_hidden = allow_hidden_files
        self._max_file_size = max_file_size_bytes
        self._allow_secret_files = allow_secret_files  # P-08

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def check(self, operation: str, path: str) -> PermissionResult:
        """
        Check whether ``operation`` is permitted on ``path``.

        Args:
            operation: One of "read", "write", "delete", "move", "search".
            path:      Target file or directory path.

        Returns:
            PermissionResult with allowed flag and reasons.
        """
        result = PermissionResult(allowed=True)

        try:
            resolved = Path(path).expanduser().resolve()
        except Exception as exc:
            return PermissionResult(allowed=False, reasons=[f"Invalid path: {exc}"])

        result.resolved_path = str(resolved)

        # Always-blocked paths — check both the resolved path AND the original
        # input string so POSIX-style paths like /etc/shadow are caught on
        # Windows, where resolve() converts them to CWD-relative paths.
        if self._is_always_blocked(resolved) or self._is_always_blocked(Path(path)):
            return result.deny(f"Path '{path}' is in the always-blocked list")

        # P-08: Secret file guard — blocks read AND write for all agent operations
        if not self._allow_secret_files and _is_secret_file(resolved):
            return result.deny(
                f"Access denied: '{resolved.name}' is a protected secret/credential file. "
                "Secrets must be accessed through the vault API (config.settings.SecretKey), "
                "not via direct filesystem reads."
            )

        # System path guard
        if self._is_system_path(resolved):
            if operation in ("write", "delete"):
                return result.deny(f"System path '{resolved}' is read-only")
            result.warn(f"Reading system path '{resolved}'")

        # Hidden file check
        if not self._allow_hidden and self._has_hidden_component(resolved):
            if operation in ("write", "delete"):
                return result.deny(
                    f"Writing to hidden files/dirs is not permitted: {resolved}"
                )
            result.warn(f"Accessing hidden path: {resolved}")

        # Blocked extension check (write operations)
        if operation in ("write",) and resolved.suffix.lower() in _BLOCKED_EXTENSIONS:
            return result.deny(f"Writing '{resolved.suffix}' files is not permitted")

        # Extra blocked paths
        if self._extra_blocked and self._under_any(resolved, self._extra_blocked):
            return result.deny(f"Path '{resolved}' is in the extra-blocked list")

        # Sandbox check
        if operation == "read" or operation == "search":
            if self._read_roots and not self._under_any(resolved, self._read_roots):
                return result.deny(f"Path '{resolved}' is outside allowed read zones")
        elif operation == "write":
            if self._write_roots and not self._under_any(resolved, self._write_roots):
                return result.deny(f"Path '{resolved}' is outside allowed write zones")
        elif operation == "delete":
            if self._delete_roots and not self._under_any(resolved, self._delete_roots):
                return result.deny(f"Path '{resolved}' is outside allowed delete zones")
        elif operation == "move":
            # Move checked at FileManager level with separate src/dest checks
            pass

        return result

    def check_write_size(self, size_bytes: int) -> PermissionResult:
        """Check whether a file of size_bytes is within write limits."""
        result = PermissionResult(allowed=True)
        if size_bytes > self._max_file_size:
            result.deny(
                f"File size {size_bytes:,} bytes exceeds limit of {self._max_file_size:,} bytes"
            )
        return result

    def is_secret_file(self, path: str) -> bool:
        """
        Public helper: return True if path matches a known secret file pattern.

        Useful for pre-flight checks in UI layers before constructing a full
        ActionRequest.
        """
        try:
            resolved = Path(path).expanduser().resolve()
        except Exception:
            return False
        return _is_secret_file(resolved)

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------

    @staticmethod
    def _under_any(path: Path, roots: list[Path]) -> bool:
        for root in roots:
            try:
                path.relative_to(root)
                return True
            except ValueError:
                continue
        return False

    @staticmethod
    def _is_system_path(path: Path) -> bool:
        path_str = str(path)
        for sys_path in _SYSTEM_READ_ONLY:
            if path_str.startswith(sys_path):
                return True
        return False

    @staticmethod
    def _is_always_blocked(path: Path) -> bool:
        path_str = str(path)
        expanded = [str(Path(p).expanduser()) for p in _ALWAYS_BLOCKED]
        # Also check raw POSIX-style strings so Unix paths like /etc/shadow
        # are blocked on Windows too, where Path("/etc/shadow").resolve()
        # produces a CWD-relative path that would never match the expanded form.
        for blocked in expanded + list(_ALWAYS_BLOCKED):
            b = blocked.replace("\\", "/").rstrip("/")
            p = path_str.replace("\\", "/")
            if p == b or p.startswith(b + "/"):
                return True
        return False

    @staticmethod
    def _has_hidden_component(path: Path) -> bool:
        return any(part.startswith(".") for part in path.parts)