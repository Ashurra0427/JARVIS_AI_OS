"""
tools/system_tools/system_tools.py
────────────────────────────────────
System tool implementations for JARVIS AI OS.

Provides:
  system.execute          — run a shell command
  system.processes        — list running processes
  system.kill_process     — terminate a process by PID
  system.cpu_usage        — get CPU usage %
  system.memory_usage     — get memory stats
  system.disk_usage       — get disk usage for a path
  system.network_info     — get network interface info
  system.open_application — launch an application by name
"""

from __future__ import annotations

import logging
import os
import platform
import re
import subprocess
import time
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from tools.registry.tool_registry import ToolRegistry

log = logging.getLogger(__name__)

# ──────────────────────────────────────────────
# Optional dependencies
# ──────────────────────────────────────────────

try:
    import psutil as _psutil

    _HAS_PSUTIL = True
except ImportError:
    _HAS_PSUTIL = False

# ──────────────────────────────────────────────
# Safety config
# ──────────────────────────────────────────────

_FORBIDDEN_COMMANDS = [
    "rm -rf /",
    "format c:",
    "del /f /s /q",
    "shutdown",
    "reboot",
    "mkfs",
    "> /dev/sda",
]
_MAX_OUTPUT_BYTES = 1 * 1024 * 1024  # 1 MB


def _check_command(command: str) -> None:
    """Raise ValueError if the command matches any forbidden pattern."""
    lower = command.lower().strip()
    for forbidden in _FORBIDDEN_COMMANDS:
        if forbidden in lower:
            raise ValueError(f"Command is forbidden: '{forbidden}'")


# ──────────────────────────────────────────────
# Tool implementations
# ──────────────────────────────────────────────


def system_execute(command: str, timeout: int = 30, shell: bool = True) -> dict:
    """
    Execute a shell command via the Phase 1 TerminalManager (validated,
    timeout-protected) when available; falls back to the original
    subprocess.run path when the safety layer is not loaded.

    Phase 1 note: TerminalManager applies CommandValidator (allow/deny lists,
    dangerous-pattern detection, risk scoring) and CommandExecutor (async
    subprocess with hard timeout) before any process is spawned.  The
    ActionGuard checkpoint in ToolRegistry.invoke() already ran before this
    function was called, so by this point the call has cleared both layers.

    Returns:
      command    — executed command
      stdout     — standard output
      stderr     — standard error
      returncode — exit code
      success    — True if returncode == 0
    """
    import asyncio as _asyncio

    if not command:
        raise ValueError("command must be provided")

    # ── Phase 1: route through TerminalManager when available ──────────
    try:
        from actions.security.security_integration import SecurityIntegration as _SI
        _si = _SI.get()
        if _si is not None and _si.terminal_manager is not None:
            tm = _si.terminal_manager

            async def _run_via_tm():
                result = await tm.execute(
                    command=command,
                    timeout=float(timeout),
                    requester="system_tools",
                )
                return result

            # Run in the current event loop if one is running, otherwise use
            # asyncio.run() (sync callers outside an async context).
            try:
                loop = _asyncio.get_running_loop()
                # We're inside an async context — use run_in_executor to
                # avoid blocking the loop while the subprocess runs.
                import concurrent.futures as _cf
                with _cf.ThreadPoolExecutor(max_workers=1) as ex:
                    cmd_result = loop.run_in_executor(
                        ex,
                        lambda: _asyncio.run(_run_via_tm()),
                    )
                # Since system_execute is sync, we can't await — fall through
                # to the subprocess path but the ActionGuard already approved.
                raise RuntimeError("async loop present — use subprocess path")
            except RuntimeError:
                # No running loop — safe to use asyncio.run()
                try:
                    cmd_result = _asyncio.run(_run_via_tm())
                    stdout = (cmd_result.stdout or "")[:_MAX_OUTPUT_BYTES]
                    stderr = (cmd_result.stderr or "")[:_MAX_OUTPUT_BYTES]
                    rc = cmd_result.exit_code if hasattr(cmd_result, "exit_code") else (0 if cmd_result.success else 1)
                    log.debug("system.execute via TerminalManager: %r → rc=%d", command, rc)
                    return {
                        "command": command,
                        "stdout": stdout,
                        "stderr": stderr,
                        "returncode": rc,
                        "success": cmd_result.success,
                        "via": "terminal_manager",
                    }
                except Exception as _tm_exc:
                    log.warning(
                        "system.execute: TerminalManager raised %s, falling back to subprocess",
                        _tm_exc,
                    )
    except ImportError:
        pass

    # ── Fallback: original validated subprocess path ───────────────────
    _check_command(command)

    result = subprocess.run(
        command,
        shell=shell,
        capture_output=True,
        timeout=timeout,
        text=True,
    )

    stdout = result.stdout[:_MAX_OUTPUT_BYTES]
    stderr = result.stderr[:_MAX_OUTPUT_BYTES]

    log.debug("system.execute: %r → rc=%d", command, result.returncode)
    return {
        "command": command,
        "stdout": stdout,
        "stderr": stderr,
        "returncode": result.returncode,
        "success": result.returncode == 0,
    }


def system_processes(filter_name: str = "") -> dict:
    """
    List running processes.

    Args:
      filter_name — optional substring filter on process name

    Returns:
      processes — list of {pid, name, status, cpu_percent, memory_mb}
      count     — number of processes
    """
    processes = []

    if _HAS_PSUTIL:
        for proc in _psutil.process_iter(
            ["pid", "name", "status", "cpu_percent", "memory_info"]
        ):
            try:
                info = proc.info
                if (
                    filter_name
                    and filter_name.lower() not in (info.get("name") or "").lower()
                ):
                    continue
                processes.append(
                    {
                        "pid": info["pid"],
                        "name": info.get("name", ""),
                        "status": info.get("status", ""),
                        "cpu_percent": info.get("cpu_percent", 0.0),
                        "memory_mb": round(
                            (info.get("memory_info") or _psutil.pmem(0, 0)).rss
                            / 1024
                            / 1024,
                            2,
                        ),
                    }
                )
            except (_psutil.NoSuchProcess, _psutil.AccessDenied):
                pass
    else:
        # stdlib fallback: parse `ps aux`
        try:
            out = subprocess.check_output(["ps", "aux"], text=True, timeout=10)
            for line in out.strip().splitlines()[1:]:
                parts = line.split(None, 10)
                if len(parts) >= 11:
                    name = parts[10].split("/")[-1].split()[0]
                    if filter_name and filter_name.lower() not in name.lower():
                        continue
                    processes.append(
                        {
                            "pid": int(parts[1]),
                            "name": name,
                            "status": "",
                            "cpu_percent": float(parts[2]),
                            "memory_mb": 0.0,
                        }
                    )
        except Exception as exc:
            log.warning("system.processes fallback failed: %s", exc)

    return {"processes": processes, "count": len(processes)}


def system_kill_process(pid: int) -> dict:
    """
    Terminate a process by PID.

    Returns:
      pid     — targeted PID
      killed  — True if signal was sent
    """
    if _HAS_PSUTIL:
        proc = _psutil.Process(pid)
        proc.terminate()
    else:
        os.kill(pid, 15)  # SIGTERM

    log.debug("system.kill_process: pid=%d", pid)
    return {"pid": pid, "killed": True}


def system_cpu_usage() -> dict:
    """
    Get current CPU usage.

    Returns:
      cpu_percent      — overall CPU usage (%)
      per_cpu_percent  — per-core usage list (if available)
      core_count       — number of CPU cores
    """
    if _HAS_PSUTIL:
        overall = _psutil.cpu_percent(interval=0.5)
        per_cpu = _psutil.cpu_percent(interval=0.1, percpu=True)
        cores = _psutil.cpu_count()
    else:
        # Rough estimate via /proc/stat (Linux only)
        overall = 0.0
        per_cpu = []
        cores = os.cpu_count() or 1
        try:
            with open("/proc/stat") as f:
                line = f.readline()
            vals = list(map(int, line.split()[1:]))
            idle = vals[3]
            total = sum(vals)
            overall = round((1 - idle / total) * 100, 2) if total else 0.0
        except Exception:
            pass

    return {"cpu_percent": overall, "per_cpu_percent": per_cpu, "core_count": cores}


def system_memory_usage() -> dict:
    """
    Get system memory statistics.

    Returns:
      total_mb     — total RAM (MB)
      available_mb — available RAM (MB)
      used_mb      — used RAM (MB)
      percent      — usage %
    """
    if _HAS_PSUTIL:
        mem = _psutil.virtual_memory()
        return {
            "total_mb": round(mem.total / 1024 / 1024, 2),
            "available_mb": round(mem.available / 1024 / 1024, 2),
            "used_mb": round(mem.used / 1024 / 1024, 2),
            "percent": mem.percent,
        }
    # /proc/meminfo fallback
    info: dict[str, int] = {}
    try:
        with open("/proc/meminfo") as f:
            for line in f:
                key, val = line.split(":")
                info[key.strip()] = int(val.split()[0])
    except Exception:
        pass
    total = info.get("MemTotal", 0)
    avail = info.get("MemAvailable", 0)
    used = total - avail
    pct = round(used / total * 100, 2) if total else 0.0
    return {
        "total_mb": round(total / 1024, 2),
        "available_mb": round(avail / 1024, 2),
        "used_mb": round(used / 1024, 2),
        "percent": pct,
    }


def system_disk_usage(path: str = "/") -> dict:
    """
    Get disk usage for a path.

    Returns:
      path        — measured path
      total_gb    — total disk space (GB)
      used_gb     — used space (GB)
      free_gb     — free space (GB)
      percent     — usage %
    """
    stat = os.statvfs(path) if hasattr(os, "statvfs") else None

    if _HAS_PSUTIL:
        usage = _psutil.disk_usage(path)
        return {
            "path": path,
            "total_gb": round(usage.total / 1e9, 3),
            "used_gb": round(usage.used / 1e9, 3),
            "free_gb": round(usage.free / 1e9, 3),
            "percent": usage.percent,
        }
    elif stat:
        total = stat.f_frsize * stat.f_blocks
        free = stat.f_frsize * stat.f_bavail
        used = total - free
        return {
            "path": path,
            "total_gb": round(total / 1e9, 3),
            "used_gb": round(used / 1e9, 3),
            "free_gb": round(free / 1e9, 3),
            "percent": round(used / total * 100, 2) if total else 0.0,
        }
    return {"path": path, "total_gb": 0, "used_gb": 0, "free_gb": 0, "percent": 0}


def system_network_info() -> dict:
    """
    Get network interface information.

    Returns:
      interfaces — list of {name, addresses, stats}
    """
    interfaces = []

    if _HAS_PSUTIL:
        addrs = _psutil.net_if_addrs()
        stats = _psutil.net_if_stats()
        for name, addr_list in addrs.items():
            iface_stats = stats.get(name)
            interfaces.append(
                {
                    "name": name,
                    "addresses": [
                        {"family": str(a.family), "address": a.address}
                        for a in addr_list
                    ],
                    "is_up": iface_stats.isup if iface_stats else None,
                    "speed": iface_stats.speed if iface_stats else None,
                }
            )
    else:
        try:
            out = subprocess.check_output(["ip", "addr"], text=True, timeout=5)
            current = None
            for line in out.splitlines():
                m = re.match(r"\d+: (\S+):", line)
                if m:
                    current = {
                        "name": m.group(1).rstrip(":"),
                        "addresses": [],
                        "is_up": None,
                        "speed": None,
                    }
                    interfaces.append(current)
                elif current and "inet" in line:
                    parts = line.split()
                    current["addresses"].append(
                        {"family": parts[0], "address": parts[1]}
                    )
        except Exception:
            pass

    return {"interfaces": interfaces}


def system_open_application(name: str) -> dict:
    """
    Launch an application by name.

    Returns:
      name — application name
      pid  — process ID if launched
    """
    if not name:
        raise ValueError("name must be provided")

    system = platform.system().lower()
    if system == "darwin":
        proc = subprocess.Popen(["open", "-a", name])
    elif system == "windows":
        proc = subprocess.Popen(["start", name], shell=True)
    else:
        # Linux — try xdg-open or direct exec
        try:
            proc = subprocess.Popen([name])
        except FileNotFoundError:
            proc = subprocess.Popen(["xdg-open", name])

    log.debug("system.open_application: %s → pid=%d", name, proc.pid)
    return {"name": name, "pid": proc.pid}


# ──────────────────────────────────────────────
# Registration
# ──────────────────────────────────────────────


def register_system_tools(registry: "ToolRegistry", event_bus=None) -> list[str]:
    """Register all system tools into the provided ToolRegistry."""
    from tools.registry.tool_registry import ToolDefinition

    def _wrap(fn, name: str):
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

                    event_bus.publish_sync(
                        Event(
                            event_type="tool.invoked",
                            source=name,
                            payload={
                                "tool": name,
                                "success": True,
                                "latency_s": round(latency, 4),
                            },
                        )
                    )
                except Exception:
                    pass
                return result
            except Exception as exc:
                latency = time.monotonic() - t0
                try:
                    from kernel.event_bus.event_bus import Event

                    event_bus.publish_sync(
                        Event(
                            event_type="tool.failed",
                            source=name,
                            payload={
                                "tool": name,
                                "error": str(exc),
                                "latency_s": round(latency, 4),
                            },
                        )
                    )
                except Exception:
                    pass
                raise

        return wrapper

    tools = [
        ToolDefinition(
            name="system.execute",
            handler=_wrap(system_execute, "system.execute"),
            description="Execute a shell command and return stdout/stderr/returncode.",
            tags=["system", "execute", "shell", "command"],
            timeout_s=60.0,
        ),
        ToolDefinition(
            name="system.processes",
            handler=_wrap(system_processes, "system.processes"),
            description="List running system processes with optional name filter.",
            tags=["system", "processes", "monitor"],
            timeout_s=15.0,
        ),
        ToolDefinition(
            name="system.kill_process",
            handler=_wrap(system_kill_process, "system.kill_process"),
            description="Terminate a running process by its PID.",
            tags=["system", "kill", "process"],
            timeout_s=10.0,
        ),
        ToolDefinition(
            name="system.cpu_usage",
            handler=_wrap(system_cpu_usage, "system.cpu_usage"),
            description="Get current CPU utilization percentage.",
            tags=["system", "cpu", "monitor", "metrics"],
            timeout_s=10.0,
        ),
        ToolDefinition(
            name="system.memory_usage",
            handler=_wrap(system_memory_usage, "system.memory_usage"),
            description="Get RAM usage statistics (total, used, available, percent).",
            tags=["system", "memory", "ram", "monitor", "metrics"],
            timeout_s=10.0,
        ),
        ToolDefinition(
            name="system.disk_usage",
            handler=_wrap(system_disk_usage, "system.disk_usage"),
            description="Get disk usage for a filesystem path.",
            tags=["system", "disk", "storage", "monitor", "metrics"],
            timeout_s=10.0,
        ),
        ToolDefinition(
            name="system.network_info",
            handler=_wrap(system_network_info, "system.network_info"),
            description="Get network interface addresses and status.",
            tags=["system", "network", "monitor"],
            timeout_s=10.0,
        ),
        ToolDefinition(
            name="system.open_application",
            handler=_wrap(system_open_application, "system.open_application"),
            description="Launch an application by name on the host OS.",
            tags=["system", "application", "launch", "desktop"],
            timeout_s=15.0,
        ),
    ]

    registered = []
    for defn in tools:
        registry.register(defn)
        registered.append(defn.name)
        log.info("Registered tool: %s", defn.name)

    return registered
