"""Machine details and process memory for the benchmark (#27).

Works on Windows, Linux and macOS with the standard library only. On macOS the
current resident size is not available without extra packages, so
``current_memory_mb`` falls back to the peak there.
"""

from __future__ import annotations

import ctypes
import os
import platform
import subprocess
import sys
from importlib import metadata
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]


def _windows_counters() -> Any:
    from ctypes import wintypes

    class Counters(ctypes.Structure):
        _fields_ = [
            ("cb", wintypes.DWORD),
            ("PageFaultCount", wintypes.DWORD),
            ("PeakWorkingSetSize", ctypes.c_size_t),
            ("WorkingSetSize", ctypes.c_size_t),
            ("QuotaPeakPagedPoolUsage", ctypes.c_size_t),
            ("QuotaPagedPoolUsage", ctypes.c_size_t),
            ("QuotaPeakNonPagedPoolUsage", ctypes.c_size_t),
            ("QuotaNonPagedPoolUsage", ctypes.c_size_t),
            ("PagefileUsage", ctypes.c_size_t),
            ("PeakPagefileUsage", ctypes.c_size_t),
        ]

    counters = Counters()
    counters.cb = ctypes.sizeof(Counters)
    windll: Any = getattr(ctypes, "windll")
    # Declare signatures: without them the GetCurrentProcess pseudo-handle
    # is truncated to 32 bits and the call fails, reporting 0 bytes.
    current = windll.kernel32.GetCurrentProcess
    current.restype = wintypes.HANDLE
    query = windll.psapi.GetProcessMemoryInfo
    query.argtypes = [wintypes.HANDLE, ctypes.POINTER(Counters), wintypes.DWORD]
    query.restype = wintypes.BOOL
    if not query(current(), ctypes.byref(counters), counters.cb):
        raise OSError("GetProcessMemoryInfo failed")
    return counters


def peak_memory_mb() -> float:
    """Peak resident memory (working set on Windows) of this process in MB."""

    if sys.platform == "win32":
        return _windows_counters().PeakWorkingSetSize / 1e6
    import resource

    # ru_maxrss is in bytes on macOS and in KiB elsewhere.
    peak = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss
    return peak / 1e6 if sys.platform == "darwin" else peak * 1024 / 1e6


def current_memory_mb() -> float:
    """Current resident memory of this process in MB (the peak on macOS)."""

    if sys.platform == "win32":
        return _windows_counters().WorkingSetSize / 1e6
    if sys.platform.startswith("linux"):
        with open("/proc/self/statm", encoding="ascii") as handle:
            pages = int(handle.read().split()[1])
        return pages * os.sysconf("SC_PAGE_SIZE") / 1e6
    return peak_memory_mb()


def total_memory_gb() -> float | None:
    try:
        if sys.platform == "win32":

            class Status(ctypes.Structure):
                _fields_ = [
                    ("dwLength", ctypes.c_ulong),
                    ("dwMemoryLoad", ctypes.c_ulong),
                    ("ullTotalPhys", ctypes.c_ulonglong),
                    ("rest", ctypes.c_ulonglong * 6),
                ]

            status = Status()
            status.dwLength = ctypes.sizeof(Status)
            windll: Any = getattr(ctypes, "windll")
            query = windll.kernel32.GlobalMemoryStatusEx
            query.argtypes = [ctypes.POINTER(Status)]
            query.restype = ctypes.c_int
            if not query(ctypes.byref(status)):
                return None
            return status.ullTotalPhys / 1e9
        if sys.platform == "darwin":
            out = subprocess.run(["sysctl", "-n", "hw.memsize"], capture_output=True, text=True)
            return int(out.stdout) / 1e9
        with open("/proc/meminfo", encoding="ascii") as handle:
            return int(handle.readline().split()[1]) / 1e6
    except (OSError, ValueError, AttributeError):
        return None


def cpu_model() -> str:
    """Marketing name of the CPU where the OS exposes it, else the architecture."""

    try:
        if sys.platform == "win32":
            import winreg

            key = winreg.OpenKey(
                winreg.HKEY_LOCAL_MACHINE, r"HARDWARE\DESCRIPTION\System\CentralProcessor\0"
            )
            with key:
                return str(winreg.QueryValueEx(key, "ProcessorNameString")[0]).strip()
        if sys.platform == "darwin":
            out = subprocess.run(
                ["sysctl", "-n", "machdep.cpu.brand_string"], capture_output=True, text=True
            )
            if out.stdout.strip():
                return out.stdout.strip()
        else:
            with open("/proc/cpuinfo", encoding="utf-8", errors="replace") as handle:
                for line in handle:
                    if line.startswith("model name"):
                        return line.split(":", 1)[1].strip()
    except (OSError, ValueError, IndexError):
        pass
    return platform.processor() or platform.machine()


def _version(package: str) -> str | None:
    try:
        return metadata.version(package)
    except metadata.PackageNotFoundError:
        return None


def _commit() -> str | None:
    try:
        out = subprocess.run(
            ["git", "rev-parse", "--short", "HEAD"],
            cwd=ROOT,
            capture_output=True,
            text=True,
            timeout=10,
            check=False,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    return out.stdout.strip() or None


def machine() -> dict[str, object]:
    """Everything needed to compare a result with another machine's."""

    return {
        "platform": platform.platform(),
        "system": platform.system(),
        "machine": platform.machine(),
        "processor": cpu_model(),
        "cpu_count": os.cpu_count(),
        "memory_gb": total_memory_gb(),
        "python": platform.python_version(),
        "python_implementation": platform.python_implementation(),
        "packages": {
            name: _version(name) for name in ("PySide6", "pyqtgraph", "numpy", "lbr-dashboard")
        },
        "qt_platform": os.environ.get("QT_QPA_PLATFORM", "default"),
        "ci": bool(os.environ.get("CI")),
        "commit": _commit(),
    }
