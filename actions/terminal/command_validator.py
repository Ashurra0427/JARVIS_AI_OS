"""
JARVIS AI OS — Command Validator
==================================
Stateless command safety analysis.

Responsibilities:
  - Whitelist / blacklist evaluation
  - Dangerous pattern detection (rm -rf, dd, fork-bombs, etc.)
  - Risk scoring (0.0 = safe, 1.0 = definitely dangerous)
  - Produce structured ValidationResult

Rules:
  - Pure functions only — no I/O, no side effects
  - Called by CommandExecutor before any subprocess is started
"""

from __future__ import annotations

import re
import shlex
from dataclasses import dataclass, field
from typing import Sequence


# ---------------------------------------------------------------------------
# Risk levels
# ---------------------------------------------------------------------------

RISK_SAFE = 0.0
RISK_LOW = 0.2
RISK_MEDIUM = 0.5
RISK_HIGH = 0.8
RISK_CRITICAL = 1.0


# ---------------------------------------------------------------------------
# Pattern tables
# ---------------------------------------------------------------------------

# Commands that are always allowed regardless of arguments
_WHITELIST_COMMANDS: frozenset[str] = frozenset(
    {
        "echo",
        "pwd",
        "whoami",
        "date",
        "uptime",
        "uname",
        "ls",
        "dir",
        "cat",
        "head",
        "tail",
        "grep",
        "find",
        "wc",
        "sort",
        "uniq",
        "cut",
        "awk",
        "sed",
        "ps",
        "top",
        "df",
        "du",
        "free",
        "curl",
        "wget",
        "ping",
        "nslookup",
        "dig",
        "python",
        "python3",
        "pip",
        "pip3",
        "node",
        "npm",
        "npx",
        "git",
        "gh",
        "make",
        "cmake",
        "docker",
        "docker-compose",
        "kubectl",
        "helm",
        "ssh",
        "scp",
        "rsync",
        "tar",
        "gzip",
        "gunzip",
        "zip",
        "unzip",
        "mkdir",
        "touch",
        "cp",
        "mv",
        "which",
        "whereis",
        "type",
        "env",
        "printenv",
        "systemctl",
        "service",
        "journalctl",
        "ffmpeg",
        "convert",  # ImageMagick
    }
)

# Commands that are always blocked
_BLACKLIST_COMMANDS: frozenset[str] = frozenset(
    {
        "su",
        "sudo",
        "doas",
        "passwd",
        "chpasswd",
        "visudo",
        "mkfs",
        "fdisk",
        "parted",
        "dd",
        "shred",
        "nc",
        "ncat",
        "netcat",  # unless requester has explicit privilege
        "nmap",
        "tcpdump",
        "wireshark",
        "strace",
        "ptrace",
        "insmod",
        "modprobe",
        "rmmod",
        "iptables",
        "ip6tables",
        "nftables",
        "crontab",
    }
)

# Regex patterns that indicate high risk regardless of command
_DANGEROUS_PATTERNS: list[tuple[re.Pattern, float, str]] = [
    (re.compile(r"rm\s+(-\w*f\w*\s+)?-\w*r", re.I), RISK_CRITICAL, "recursive rm"),
    (
        re.compile(r">\s*/dev/(s?d[a-z]|nvme)", re.I),
        RISK_CRITICAL,
        "write to block device",
    ),
    (re.compile(r":\s*\(\s*\)\s*\{.*:\s*\|\s*:.*&.*\}\s*;?\s*:", re.I | re.DOTALL), RISK_CRITICAL, "fork bomb"),
    (re.compile(r"mkfs\b", re.I), RISK_CRITICAL, "format filesystem"),
    (re.compile(r"\bdd\b.*of=/"), RISK_CRITICAL, "dd to device"),
    (re.compile(r"chmod\s+-R\s+777", re.I), RISK_HIGH, "chmod -R 777"),
    (re.compile(r">\s*/etc/"), RISK_HIGH, "overwrite /etc"),
    (re.compile(r"curl.*\|\s*(ba)?sh", re.I), RISK_HIGH, "curl pipe to shell"),
    (re.compile(r"wget.*-O-.*\|\s*(ba)?sh", re.I), RISK_HIGH, "wget pipe to shell"),
    (
        re.compile(r"base64\s+--decode.*\|\s*(ba)?sh"),
        RISK_HIGH,
        "base64 decode pipe to shell",
    ),
    (re.compile(r"\beval\b"), RISK_MEDIUM, "eval usage"),
    (re.compile(r"history\s+-[cwd]", re.I), RISK_MEDIUM, "history wipe"),
    (
        re.compile(r">\s*~/?\.(bash|zsh|profile|bashrc)"),
        RISK_MEDIUM,
        "overwrite shell config",
    ),
    (re.compile(r"--force", re.I), RISK_LOW, "force flag"),
    (re.compile(r"--no-verify", re.I), RISK_LOW, "no-verify flag"),
]

# Max command length (characters)
_MAX_COMMAND_LENGTH = 4096

# Max number of chained sub-commands (via ; && || |)
_MAX_CHAIN_DEPTH = 10


# ---------------------------------------------------------------------------
# Result type
# ---------------------------------------------------------------------------


@dataclass
class ValidationResult:
    allowed: bool
    risk_score: float = 0.0
    reasons: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)

    @property
    def is_safe(self) -> bool:
        return self.allowed and self.risk_score < RISK_MEDIUM

    def add_reason(self, reason: str) -> None:
        self.reasons.append(reason)

    def add_warning(self, warning: str) -> None:
        self.warnings.append(warning)


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


def validate_command(
    command: str,
    *,
    allowed_commands: Sequence[str] | None = None,
    blocked_commands: Sequence[str] | None = None,
    max_risk: float = RISK_HIGH,
    allow_chaining: bool = True,
) -> ValidationResult:
    """
    Validate a shell command string before execution.

    Args:
        command:          Raw command string.
        allowed_commands: Extra whitelist additions (merged with built-in).
        blocked_commands: Extra blacklist additions (merged with built-in).
        max_risk:         Reject if risk_score >= max_risk.
        allow_chaining:   If False, reject commands with shell operators.

    Returns:
        ValidationResult with allowed, risk_score, reasons, warnings.
    """
    result = ValidationResult(allowed=True)
    command = command.strip()

    # Length guard
    if len(command) > _MAX_COMMAND_LENGTH:
        result.allowed = False
        result.risk_score = RISK_HIGH
        result.add_reason(
            f"Command too long: {len(command)} chars (max {_MAX_COMMAND_LENGTH})"
        )
        return result

    if not command:
        result.allowed = False
        result.add_reason("Empty command")
        return result

    # Resolve effective whitelist / blacklist
    effective_whitelist = _WHITELIST_COMMANDS | frozenset(allowed_commands or [])
    effective_blacklist = _BLACKLIST_COMMANDS | frozenset(blocked_commands or [])

    # Extract base command
    base_cmd = _extract_base_command(command)

    # Blacklist check
    if base_cmd in effective_blacklist:
        result.allowed = False
        result.risk_score = RISK_CRITICAL
        result.add_reason(f"Command '{base_cmd}' is explicitly blocked")
        return result

    # Chaining check
    chain_count = _count_chain_operators(command)
    if not allow_chaining and chain_count > 0:
        result.allowed = False
        result.add_reason("Command chaining not permitted in this context")
        return result
    if chain_count > _MAX_CHAIN_DEPTH:
        result.allowed = False
        result.add_reason(
            f"Too many chained commands: {chain_count} (max {_MAX_CHAIN_DEPTH})"
        )
        return result
    if chain_count > 3:
        result.add_warning(f"Command chain depth {chain_count} — review carefully")

    # Dangerous pattern scan
    for pattern, risk, label in _DANGEROUS_PATTERNS:
        if pattern.search(command):
            result.risk_score = max(result.risk_score, risk)
            if risk >= RISK_HIGH:
                result.add_reason(f"Dangerous pattern detected: {label}")
            else:
                result.add_warning(f"Potentially risky pattern: {label}")

    # Whitelist bonus — lower risk for known-safe commands
    if base_cmd in effective_whitelist and result.risk_score < RISK_MEDIUM:
        result.risk_score = max(0.0, result.risk_score - 0.1)

    # Final decision
    if result.risk_score >= max_risk:
        result.allowed = False
        if not result.reasons:
            result.add_reason(
                f"Risk score {result.risk_score:.2f} exceeds threshold {max_risk:.2f}"
            )

    return result


def score_risk(command: str) -> float:
    """Quick risk score without full validation; useful for logging."""
    return validate_command(command, max_risk=RISK_CRITICAL + 1).risk_score


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _extract_base_command(command: str) -> str:
    """Return the leading command token (handles env var prefixes)."""
    try:
        tokens = shlex.split(command)
    except ValueError:
        tokens = command.split()
    if not tokens:
        return ""
    # Skip VAR=value prefixes
    for token in tokens:
        if "=" not in token or token.startswith("-"):
            return token.split("/")[-1]  # strip path
    return tokens[0].split("/")[-1]


def _count_chain_operators(command: str) -> int:
    """Count shell chaining points: ; && || | (rough heuristic, not AST)."""
    # Strip quoted sections to avoid false positives
    cleaned = re.sub(r'"[^"]*"', "", command)
    cleaned = re.sub(r"'[^']*'", "", cleaned)
    return len(re.findall(r"(;|&&|\|\||(?<!\|)\|(?!\|))", cleaned))
