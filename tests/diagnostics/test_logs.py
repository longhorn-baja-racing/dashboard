"""Local logs stay bounded, redacted and harmless when the disk misbehaves."""

from __future__ import annotations

import io
import logging
import warnings
from collections.abc import Iterator
from pathlib import Path

import pytest

from lbr_dashboard.diagnostics import (
    LOG_DIR_ENV,
    LOG_LEVEL_ENV,
    DiagnosticsSession,
    configure_logging,
    default_log_directory,
    logs,
    redact,
)

log = logging.getLogger("lbr_dashboard.test")


@pytest.fixture
def session(tmp_path: Path) -> Iterator[DiagnosticsSession]:
    active = configure_logging(
        tmp_path / "logs", environ={}, stderr=io.StringIO(), home="/home/driver"
    )
    yield active
    active.close()


def read_log(active: DiagnosticsSession) -> str:
    assert active.log_file is not None
    if active.file_handler is not None:
        active.file_handler.flush()
    return active.log_file.read_text(encoding="utf-8")


# Location ----------------------------------------------------------------------------


def test_environment_override_wins(monkeypatch: pytest.MonkeyPatch) -> None:
    assert default_log_directory({LOG_DIR_ENV: "/srv/logs"}) == Path("/srv/logs")


def test_windows_uses_local_app_data(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.setattr(logs.sys, "platform", "win32")
    monkeypatch.setattr(logs.Path, "home", lambda: tmp_path)
    assert default_log_directory({"LOCALAPPDATA": "X"}) == Path("X") / "LBR Dashboard" / "logs"
    assert default_log_directory({}) == tmp_path / "AppData" / "Local" / "LBR Dashboard" / "logs"


def test_macos_and_linux_locations(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.setattr(logs.Path, "home", lambda: tmp_path)
    monkeypatch.setattr(logs.sys, "platform", "darwin")
    assert default_log_directory({}) == tmp_path / "Library" / "Logs" / "LBR Dashboard"
    monkeypatch.setattr(logs.sys, "platform", "linux")
    assert default_log_directory({"XDG_STATE_HOME": "S"}) == Path("S") / "lbr-dashboard" / "logs"
    assert default_log_directory({}) == tmp_path / ".local" / "state" / "lbr-dashboard" / "logs"


# Redaction --------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("password=hunter2 ok", "password=<redacted> ok"),
        ("token: abc.def", "token: <redacted>"),
        ('api_key="a b c" next', "api_key=<redacted> next"),
        ("Authorization: Bearer xyz", "Authorization: <redacted>"),
        ("API-KEY='k'", "API-KEY=<redacted>"),
        ("tokens are fine", "tokens are fine"),
        ("opened /home/driver/run.csv", "opened ~/run.csv"),
    ],
)
def test_redaction(text: str, expected: str) -> None:
    assert redact(text, home="/home/driver") == expected


def test_windows_home_is_redacted_case_insensitively(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(logs.sys, "platform", "win32")
    assert redact(r"C:\USERS\Driver\log.csv", home=r"C:\Users\driver") == r"~\log.csv"


def test_root_home_is_not_redacted() -> None:
    assert redact("/etc/hosts", home="/") == "/etc/hosts"


def test_default_home_is_the_real_home() -> None:
    assert redact(str(Path.home() / "x")).startswith("~")


# Files ------------------------------------------------------------------------------


def test_records_reach_file_memory_and_stderr(session: DiagnosticsSession) -> None:
    log.info("source connected")
    log.warning("frame dropped from /home/driver/run.csv")
    text = read_log(session)
    assert "INFO" in text and "source connected" in text
    assert "~/run.csv" in text and "/home/driver" not in text
    assert session.recent_records()[-1].endswith("frame dropped from ~/run.csv")
    assert all("source connected" not in line for line in session.recent_records())
    assert session.log_health() == "ok"


def test_tracebacks_are_written_and_redacted(session: DiagnosticsSession) -> None:
    try:
        raise ValueError("password=secret123 at /home/driver/x")
    except ValueError:
        log.exception("worker failed")
    text = read_log(session)
    assert "Traceback" in text and "ValueError" in text
    assert "secret123" not in text and "/home/driver" not in text


def test_long_messages_are_truncated(session: DiagnosticsSession) -> None:
    log.warning("%s", "x" * (logs.MAX_MESSAGE_CHARS + 500))
    text = read_log(session)
    assert "[500 characters omitted]" in text
    assert len(text) < logs.MAX_MESSAGE_CHARS + 200


def test_rotation_keeps_disk_usage_bounded(tmp_path: Path) -> None:
    directory = tmp_path / "logs"
    active = configure_logging(
        directory, max_bytes=2000, backup_count=2, environ={}, stderr=io.StringIO()
    )
    try:
        for index in range(500):
            log.info("sample line %04d %s", index, "y" * 40)
    finally:
        active.close()
    files = sorted(p.name for p in directory.iterdir())
    assert files == ["dashboard.log", "dashboard.log.1", "dashboard.log.2"]
    assert all((directory / name).stat().st_size <= 2000 for name in files)
    assert "sample line 0499" in (directory / "dashboard.log").read_text()


def test_level_comes_from_environment(tmp_path: Path) -> None:
    active = configure_logging(tmp_path, environ={LOG_LEVEL_ENV: "debug"}, stderr=io.StringIO())
    try:
        log.debug("very detailed")
        assert "very detailed" in read_log(active)
    finally:
        active.close()
    active = configure_logging(tmp_path, environ={LOG_LEVEL_ENV: "chatty"}, stderr=io.StringIO())
    try:
        assert logging.getLogger().level == logging.INFO
    finally:
        active.close()


def test_unwritable_folder_falls_back_without_stopping(tmp_path: Path) -> None:
    blocker = tmp_path / "not-a-folder"
    blocker.write_text("")
    stderr = io.StringIO()
    active = configure_logging(blocker / "logs", environ={}, stderr=stderr)
    try:
        assert active.log_file is None and active.file_error is not None
        assert active.log_health().startswith("not writing a log file")
        assert "Log file unavailable" in active.recent_records()[0]
        log.error("still reported")
        assert active.recent_records()[-1].endswith("still reported")
        assert "still reported" in stderr.getvalue()
    finally:
        active.close()


class BrokenStream(io.StringIO):
    """A stream that fails every write and flush, like a full disk."""

    def write(self, text: str) -> int:
        raise OSError(28, "No space left on device")

    def flush(self) -> None:
        raise OSError(28, "No space left on device")


def test_write_failures_are_counted_not_raised(session: DiagnosticsSession) -> None:
    handler = session.file_handler
    assert handler is not None
    log.info("open the file first")
    assert handler.stream is not None
    handler.stream.close()
    handler.stream = BrokenStream()  # type: ignore[assignment]
    log.info("lost line one")
    log.info("lost line two")
    assert handler.failures >= 2
    assert "No space left on device" in session.log_health()
    stderr = handler._stderr.getvalue()  # type: ignore[attr-defined]
    assert stderr.count("log file write failed") == 1
    log.warning("memory still works")
    assert session.recent_records()[-1].endswith("memory still works")


def test_failure_without_exception_info_is_described() -> None:
    handler = logs.SafeRotatingFileHandler(
        Path("unused.log"), max_bytes=10, backup_count=1, stderr=io.StringIO()
    )
    handler.handleError(logging.makeLogRecord({"msg": "x"}))
    assert handler.last_failure == "unknown error"
    handler.close()


def test_recent_buffer_is_bounded_and_counts_everything(session: DiagnosticsSession) -> None:
    for index in range(logs.RECENT_LIMIT + 10):
        log.warning("problem %d", index)
    assert len(session.recent_records()) == logs.RECENT_LIMIT
    assert session.recent.total == logs.RECENT_LIMIT + 10
    assert session.recent_records()[-1].endswith(f"problem {logs.RECENT_LIMIT + 9}")


def test_recent_handler_survives_formatting_errors(session: DiagnosticsSession) -> None:
    before = session.recent.total
    logging.raiseExceptions, previous = False, logging.raiseExceptions
    try:
        log.warning("bad %d format", "not a number")
    finally:
        logging.raiseExceptions = previous
    assert session.recent.total == before


def test_python_warnings_are_captured(session: DiagnosticsSession) -> None:
    with warnings.catch_warnings():
        warnings.simplefilter("always")
        warnings.warn("deprecated option", UserWarning, stacklevel=1)
    assert any("deprecated option" in line for line in session.recent_records())


def test_close_restores_logging_state(tmp_path: Path) -> None:
    root = logging.getLogger()
    before = list(root.handlers)
    level = root.level
    active = configure_logging(tmp_path, environ={}, stderr=io.StringIO())
    assert len(root.handlers) == len(before) + 3
    active.close()
    active.close()
    assert root.handlers == before and root.level == level
