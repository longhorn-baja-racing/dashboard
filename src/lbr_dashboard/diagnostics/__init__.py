"""Local logging, uncaught-error capture and diagnostics reports (Qt-free)."""

from .hooks import ErrorReport, describe, install_exception_hooks
from .logs import (
    APP_LOGGER,
    LOG_BACKUP_COUNT,
    LOG_DIR_ENV,
    LOG_LEVEL_ENV,
    MAX_LOG_BYTES,
    DiagnosticsSession,
    configure_logging,
    default_log_directory,
    redact,
)
from .report import build_report, package_versions

__all__ = [
    "APP_LOGGER",
    "LOG_BACKUP_COUNT",
    "LOG_DIR_ENV",
    "LOG_LEVEL_ENV",
    "MAX_LOG_BYTES",
    "DiagnosticsSession",
    "ErrorReport",
    "build_report",
    "configure_logging",
    "default_log_directory",
    "describe",
    "install_exception_hooks",
    "package_versions",
    "redact",
]
