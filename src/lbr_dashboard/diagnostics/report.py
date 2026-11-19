"""Plain-text diagnostics report for copying into an issue or chat."""

from __future__ import annotations

import platform
import sys
from collections.abc import Mapping
from datetime import datetime, timezone
from importlib import metadata

from ..version import __version__
from .logs import DiagnosticsSession, redact

PACKAGES = ("PySide6", "pyqtgraph", "numpy")


def package_versions(names: tuple[str, ...] = PACKAGES) -> dict[str, str]:
    """Installed versions without importing the packages themselves."""

    versions: dict[str, str] = {}
    for name in names:
        try:
            versions[name] = metadata.version(name)
        except metadata.PackageNotFoundError:
            versions[name] = "not installed"
    return versions


def build_report(
    session: DiagnosticsSession | None,
    *,
    extra: Mapping[str, str] | None = None,
    now: datetime | None = None,
    home: str | None = None,
) -> str:
    """Return versions, log health and recent problems as redacted text.

    It never includes telemetry values, file contents or environment variables.
    ``extra`` adds labeled lines, for example the Qt runtime version or, once
    sources exist (#20/#44), their health.
    """

    stamp = (now or datetime.now(timezone.utc)).strftime("%Y-%m-%dT%H:%M:%SZ")
    lines = [
        "LBR Dashboard diagnostics",
        f"Generated: {stamp}",
        f"Dashboard: {__version__}",
        f"Python: {platform.python_version()} ({platform.python_implementation()})",
        f"Platform: {platform.platform()} ({platform.machine() or 'unknown machine'})",
        f"Frozen build: {'yes' if getattr(sys, 'frozen', False) else 'no'}",
    ]
    lines += [f"{name}: {version}" for name, version in package_versions().items()]
    lines += [f"{label}: {value}" for label, value in (extra or {}).items()]

    if session is None:
        lines.append("Log file: logging not configured")
        recent: tuple[str, ...] = ()
        total = 0
    else:
        lines.append(f"Log file: {session.log_file or 'none'}")
        lines.append(f"Log status: {session.log_health()}")
        recent = session.recent_records()
        total = session.recent.total

    lines.append("")
    if total > len(recent):
        lines.append(f"Recent warnings and errors (last {len(recent)} of {total}):")
    else:
        lines.append("Recent warnings and errors:")
    lines += [f"  {line}" for record in recent for line in record.splitlines()] or ["  none"]
    lines.append("")
    lines.append("Telemetry values and file contents are not included.")
    return redact("\n".join(lines) + "\n", home)
