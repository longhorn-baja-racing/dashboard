"""Uncaught errors are logged once with context, and reports exclude private data."""

from __future__ import annotations

import io
import sys
import threading
from collections.abc import Iterator
from datetime import datetime, timezone
from pathlib import Path
from types import TracebackType

import pytest

from lbr_dashboard.diagnostics import (
    DiagnosticsSession,
    ErrorReport,
    build_report,
    configure_logging,
    describe,
    install_exception_hooks,
    package_versions,
)
from lbr_dashboard.diagnostics import report as report_module
from lbr_dashboard.version import __version__


@pytest.fixture
def session(tmp_path: Path) -> Iterator[DiagnosticsSession]:
    active = configure_logging(tmp_path, environ={}, stderr=io.StringIO(), home="/home/driver")
    yield active
    active.close()


def raised(error: BaseException) -> tuple[type[BaseException], BaseException, TracebackType]:
    try:
        raise error
    except BaseException as caught:
        tb = caught.__traceback__
        assert tb is not None
        return type(caught), caught, tb


# describe ---------------------------------------------------------------------------


def test_describe_uses_file_name_and_line_only() -> None:
    report = describe(*raised(ValueError("bad frame")))
    assert report.summary == "ValueError: bad frame"
    assert report.location.startswith("test_hooks_and_report.py:")
    assert report.location.endswith("in raised")
    assert "/" not in report.location and "\\" not in report.location
    assert report.signature == f"ValueError@{report.location}"


def test_describe_bounds_summary_and_handles_missing_parts() -> None:
    report = describe(RuntimeError, RuntimeError("x" * 1000), None)
    assert len(report.summary) == 300 and report.summary.endswith("...")
    assert report.location == "unknown location"
    assert describe(KeyError, None, None).summary == "KeyError"


# hooks ------------------------------------------------------------------------------


def test_main_thread_exception_is_logged_and_reported(session: DiagnosticsSession) -> None:
    reports: list[ErrorReport] = []
    restore = install_exception_hooks(reports.append)
    try:
        sys.excepthook(*raised(ZeroDivisionError("division by zero")))
    finally:
        restore()
    assert [r.summary for r in reports] == ["ZeroDivisionError: division by zero"]
    assert any("Uncaught exception" in line for line in session.recent_records())
    assert any("Traceback" in line for line in session.recent_records())


def test_worker_thread_exception_is_reported_with_thread_name(session: DiagnosticsSession) -> None:
    reports: list[ErrorReport] = []
    restore = install_exception_hooks(reports.append)
    try:

        def fail() -> None:
            raise RuntimeError("source disconnected")

        worker = threading.Thread(target=fail, name="csv-reader")
        worker.start()
        worker.join()
        quiet = threading.Thread(target=lambda: sys.exit(0), name="exiting")
        quiet.start()
        quiet.join()
    finally:
        restore()
    assert len(reports) == 1
    assert reports[0].thread == "csv-reader"
    assert reports[0].summary == "RuntimeError: source disconnected"
    assert any("in thread csv-reader" in line for line in session.recent_records())


def test_thread_hook_handles_missing_thread(session: DiagnosticsSession) -> None:
    reports: list[ErrorReport] = []
    restore = install_exception_hooks(reports.append)
    try:
        error_type, error, tb = raised(OSError("gone"))
        threading.excepthook(threading.ExceptHookArgs((error_type, error, tb, None)))  # type: ignore[call-arg]
        threading.excepthook(threading.ExceptHookArgs((OSError, None, None, None)))  # type: ignore[call-arg]
    finally:
        restore()
    assert reports[0].thread == "unknown"
    assert reports[1].summary == "OSError" and reports[1].location == "unknown location"
    assert any("Uncaught OSError in thread unknown" in line for line in session.recent_records())


def test_keyboard_interrupt_keeps_previous_behavior(monkeypatch: pytest.MonkeyPatch) -> None:
    seen: list[type[BaseException]] = []
    monkeypatch.setattr(sys, "excepthook", lambda kind, value, tb: seen.append(kind))
    reports: list[ErrorReport] = []
    restore = install_exception_hooks(reports.append)
    try:
        sys.excepthook(*raised(KeyboardInterrupt()))
    finally:
        restore()
    assert seen == [KeyboardInterrupt] and reports == []


def test_failing_notifier_is_logged_not_raised(session: DiagnosticsSession) -> None:
    def broken(report: ErrorReport) -> None:
        raise RuntimeError("notifier broke")

    restore = install_exception_hooks(broken)
    try:
        sys.excepthook(*raised(ValueError("first")))
    finally:
        restore()
    assert any("Error notifier failed" in line for line in session.recent_records())


def test_hooks_without_notifier_only_log_and_restore(session: DiagnosticsSession) -> None:
    previous = (sys.excepthook, threading.excepthook)
    restore = install_exception_hooks()
    assert sys.excepthook is not previous[0]
    sys.excepthook(*raised(ValueError("logged only")))
    restore()
    assert (sys.excepthook, threading.excepthook) == previous
    assert any("logged only" in line for line in session.recent_records())


# report -----------------------------------------------------------------------------


def test_report_lists_versions_log_health_and_recent_problems(
    session: DiagnosticsSession,
) -> None:
    import logging

    logging.getLogger("lbr_dashboard.test").error("plot failed for /home/driver/run.csv")
    text = build_report(
        session,
        extra={"Qt runtime": "6.9.9"},
        now=datetime(2026, 1, 2, 3, 4, 5, tzinfo=timezone.utc),
        home="/home/driver",
    )
    assert text.startswith("LBR Dashboard diagnostics\nGenerated: 2026-01-02T03:04:05Z\n")
    assert f"Dashboard: {__version__}" in text
    assert "Python: " in text and "Platform: " in text and "Frozen build: no" in text
    assert "PySide6: " in text and "Qt runtime: 6.9.9" in text
    assert "Log status: ok" in text
    assert "plot failed for ~/run.csv" in text and "/home/driver" not in text
    assert text.rstrip().endswith("Telemetry values and file contents are not included.")


def test_report_without_logging_or_problems() -> None:
    text = build_report(None)
    assert "Log file: logging not configured" in text
    assert "Recent warnings and errors:\n  none" in text


def test_report_notes_when_older_problems_were_dropped(session: DiagnosticsSession) -> None:
    import logging

    for index in range(60):
        logging.getLogger("lbr_dashboard.test").warning("problem %d", index)
    assert "(last 50 of 60)" in build_report(session)


def test_missing_packages_are_reported() -> None:
    assert package_versions(("definitely-not-installed-package",)) == {
        "definitely-not-installed-package": "not installed"
    }


def test_frozen_builds_are_identified(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(report_module.sys, "frozen", True, raising=False)
    assert "Frozen build: yes" in build_report(None)


def test_failed_file_menu_load_is_logged_with_traceback(
    session: DiagnosticsSession, tmp_path: Path
) -> None:
    from lbr_dashboard.app import create_application, create_main_window

    create_application([])
    window = create_main_window()
    missing = tmp_path / "missing.csv"
    window.csv_requested.emit(str(missing))
    window.close()

    assert any("Could not load" in record for record in session.recent_records())
    assert session.log_file is not None
    text = session.log_file.read_text(encoding="utf-8")
    assert "Could not load" in text and "FileNotFoundError" in text
