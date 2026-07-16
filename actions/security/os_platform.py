"""
JARVIS AI OS — OS Platform Abstraction (Safe Cross-Platform Orchestration)
==========================================================================

Provides a single, dependency-free abstraction over the host operating system
so the agent can *orchestrate the device* (files, terminal, apps) **without
harming it**, on Windows, Linux, and macOS.

Responsibilities
----------------
* Detect the platform once and expose a normalised view.
* Provide per-OS *safe command policies* — e.g. ``rm -rf`` is catastrophic on
  POSIX but ``del /s`` / ``rmdir`` differs on Windows; shutdown/reboot commands
  differ; package managers differ.
* Provide platform-correct command translation (e.g. ``open`` vs ``xdg-open``
  vs ``start``) so agents issue intent, not raw OS-specific spells.
* Centralise the list of *forbidden / destructive* operations per OS so the
  ActionGuard can refuse them uniformly.

This module is PURE and SIDE-EFFECT FREE (no file/process I/O). It only
describes policy and translates intent. Execution is delegated to the existing
terminal/file managers behind the ActionGuard.
"""

from __future__ import annotations

import os
import platform
import sys
from dataclasses import dataclass, field
from enum import Enum
from typing import Iterable


class Platform(str, Enum):
    WINDOWS = "windows"
    LINUX = "linux"
    MACOS = "macos"
    UNKNOWN = "unknown"


class ShellFamily(str, Enum):
    POWERSHELL = "powershell"
    CMD = "cmd"
    BASH = "bash"
    ZSH = "zsh"
    SH = "sh"


@dataclass(frozen=True)
class OSProfile:
    """Normalised description of the host OS."""

    platform: Platform
    system: str
    release: str
    machine: str
    shell: ShellFamily
    is_posix: bool
    path_sep: str
    home_env_var: str
    package_managers: tuple[str, ...]
    # Canonical destructive commands that must NEVER auto-run.
    forbidden_commands: tuple[str, ...]
    # Commands that require explicit human confirmation (HIGH risk).
    confirmation_commands: tuple[str, ...]


# ---------------------------------------------------------------------------
# Detection
# ---------------------------------------------------------------------------

def detect_platform() -> Platform:
    sys_name = sys.platform.lower()
    if sys_name.startswith("win"):
        return Platform.WINDOWS
    if sys_name.startswith("linux"):
        return Platform.LINUX
    if sys_name.startswith("darwin"):
        return Platform.MACOS
    return Platform.UNKNOWN


def _default_shell(plat: Platform) -> ShellFamily:
    if plat == Platform.WINDOWS:
        # Prefer PowerShell; fall back to cmd semantics handled at runtime.
        return ShellFamily.POWERSHELL
    if plat == Platform.MACOS:
        return ShellFamily.ZSH
    return ShellFamily.BASH


def _package_managers(plat: Platform) -> tuple[str, ...]:
    if plat == Platform.WINDOWS:
        return ("winget", "choco", "scoop")
    if plat == Platform.MACOS:
        return ("brew", "port")
    return ("apt", "dnf", "yum", "pacman", "apk", "zypper", "snap", "flatpak")


# Per-OS destructive command signatures (substring or token match).
_FORBIDDEN_POSIX = (
    "rm -rf /", "rm -rf /*", "mkfs", ":(){:|:&};:", "dd if=/dev/",
    "chmod -R 777 /", "chown -R /", "> /dev/sda", "shutdown -h now",
    "shutdown now", "reboot", "halt", "poweroff", "init 0", "init 6",
    "mv / /", "truncate -s 0 /", "wipefs",
)
_FORBIDDEN_WINDOWS = (
    "format ", "del /s /q c:", "rmdir /s /q c:\\", "diskpart",
    "cipher /w", "shutdown /s", "shutdown /r", "bcdedit",
)
_CONFIRM_POSIX = (
    "rm -rf", "rm -r", "chmod -R", "chown", "mv /", ">",
    "sudo", "su ", "apt remove", "apt purge", "dnf remove", "pip uninstall",
    "npm uninstall", "systemctl", "crontab",
)
_CONFIRM_WINDOWS = (
    "rmdir", "del ", "takeown", "icacls", "net user", "sc ", "taskkill",
    "winget uninstall", "choco uninstall",
)


def build_profile() -> OSProfile:
    plat = detect_platform()
    is_posix = plat in (Platform.LINUX, Platform.MACOS)
    if plat == Platform.WINDOWS:
        forbidden = _FORBIDDEN_WINDOWS
        confirm = _CONFIRM_WINDOWS
        sep = "\\"
        home_var = "USERPROFILE"
    else:
        forbidden = _FORBIDDEN_POSIX
        confirm = _CONFIRM_POSIX
        sep = "/"
        home_var = "HOME"
    return OSProfile(
        platform=plat,
        system=platform.system(),
        release=platform.release(),
        machine=platform.machine(),
        shell=_default_shell(plat),
        is_posix=is_posix,
        path_sep=sep,
        home_env_var=home_var,
        package_managers=_package_managers(plat),
        forbidden_commands=forbidden,
        confirmation_commands=confirm,
    )


# ---------------------------------------------------------------------------
# Intent translation
# ---------------------------------------------------------------------------

@dataclass
class CommandIntent:
    """A platform-neutral *intent* the agent wants to perform."""

    action: str                       # e.g. "open", "list_dir", "install"
    target: str = ""                  # file/app/url
    args: tuple[str, ...] = field(default_factory=tuple)
    as_admin: bool = False            # request elevation (still guarded)
    interactive: bool = False


def translate_intent(intent: CommandIntent, profile: OSProfile) -> str:
    """
    Translate a platform-neutral intent into a concrete command string for
    the host shell. Returns an empty string if the action is unsupported on
    the platform (caller must handle gracefully).
    """
    a = intent.action.lower()
    t = intent.target

    if a == "open":
        if profile.platform == Platform.WINDOWS:
            return f'start "" "{t}"' if t else ""
        if profile.platform == Platform.MACOS:
            return f'open "{t}"'
        return f'xdg-open "{t}"'

    if a == "list_dir":
        return f'dir "{t}"' if profile.platform == Platform.WINDOWS else f'ls -la "{t}"'

    if a == "kill_process":
        return f'taskkill /PID {t} /F' if profile.platform == Platform.WINDOWS \
            else f'kill -9 {t}'

    if a == "install":
        pm = intent.args[0] if intent.args else (
            profile.package_managers[0] if profile.package_managers else ""
        )
        if not pm:
            return ""
        if pm in ("apt",):
            return f"sudo apt install -y {t}"
        if pm in ("dnf", "yum"):
            return f"sudo {pm} install -y {t}"
        if pm in ("pacman",):
            return f"sudo pacman -S --noconfirm {t}"
        if pm in ("brew",):
            return f"brew install {t}"
        if pm in ("winget",):
            return f"winget install --accept-package-agreements {t}"
        if pm in ("choco",):
            return f"choco install -y {t}"
        return f"{pm} install {t}"

    if a == "uninstall":
        pm = intent.args[0] if intent.args else (
            profile.package_managers[0] if profile.package_managers else ""
        )
        if not pm:
            return ""
        if pm in ("apt",):
            return f"sudo apt remove -y {t}"
        if pm in ("brew",):
            return f"brew uninstall {t}"
        if pm in ("winget",):
            return f"winget uninstall {t}"
        if pm in ("choco",):
            return f"choco uninstall -y {t}"
        return f"{pm} remove {t}"

    if a == "restart_service":
        if profile.platform == Platform.WINDOWS:
            return f"sc stop {t} && sc start {t}"
        return f"sudo systemctl restart {t}"

    if a == "echo":
        return f'echo "{t}"'

    return ""


# ---------------------------------------------------------------------------
# Dangerous-operation detection (used by ActionGuard)
# ---------------------------------------------------------------------------

def command_is_forbidden(command: str, profile: OSProfile) -> bool:
    """
    Return True if the command matches a hard-forbidden signature.

    Signatures may carry a trailing space (e.g. ``"format "``) to require a
    following argument and avoid matching substrings like ``"formatting"``.
    We therefore match both the raw signature and its right-stripped form so
    trailing-space signatures still fire without stripping the command (which
    would erase the space the signature relies on).
    """
    lowered = command.lower()
    for sig in profile.forbidden_commands:
        if sig in lowered or sig.rstrip() in lowered:
            return True
    return False


def command_requires_confirmation(command: str, profile: OSProfile) -> bool:
    """Return True if the command matches a HIGH-risk confirmation signature."""
    lowered = command.lower()
    for sig in profile.confirmation_commands:
        if sig in lowered or sig.rstrip() in lowered:
            return True
    return False


def normalise_path(path: str, profile: OSProfile) -> str:
    """
    Best-effort path normalisation. Expands ``~`` and the home env var, and
    converts separators to the platform default. Does NOT resolve symlinks
    (that would require I/O and is done by FilePermissions at execution time).
    """
    if not path:
        return path
    p = path.replace("\\", "/")
    if p.startswith("~") or p.startswith("$HOME") or p.startswith(f"${profile.home_env_var}"):
        home = os.getenv(profile.home_env_var, "")
        if p == "~" or p == f"${profile.home_env_var}":
            return home
        rest = p[1:] if p.startswith("~") else p[p.find("/"):]
        p = home + "/" + rest.lstrip("/")
    sepped = p.replace("/", profile.path_sep)
    return sepped


# ---------------------------------------------------------------------------
# Module-level singleton profile
# ---------------------------------------------------------------------------

_profile_instance: OSProfile | None = None


def get_os_profile() -> OSProfile:
    global _profile_instance
    if _profile_instance is None:
        _profile_instance = build_profile()
    return _profile_instance


def list_supported_platforms() -> Iterable[str]:
    return [p.value for p in Platform if p != Platform.UNKNOWN]
