"""The packaged self-test opens the sample, plots it and checks bundle files."""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

from lbr_dashboard import self_test
from lbr_dashboard.self_test import REQUIRED_BUNDLE_FILES, SelfTestResult, bundle_root, run

ROOT = Path(__file__).resolve().parents[1]
SAMPLE = ROOT / "sample_data" / "sample_baja_log.csv"


def complete_bundle(folder: Path) -> Path:
    for relative in REQUIRED_BUNDLE_FILES:
        path = folder / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("x", encoding="ascii")
    return folder


def test_sample_opens_and_plots_from_source(qapp: object) -> None:
    result = run(SAMPLE)
    assert result.passed
    names = [name for name, _, _ in result.checks]
    assert names == ["start the main window", "open the sample CSV", "plot a column"]
    report = result.report()
    assert "16 columns, 10 rows" in report and report.endswith("self-test passed\n")
    assert report.isascii()


def test_bundle_files_are_checked(qapp: object, tmp_path: Path) -> None:
    assert run(SAMPLE, complete_bundle(tmp_path / "ok")).passed

    result = run(SAMPLE, tmp_path / "empty")
    assert not result.passed
    missing = [name for name, ok, _ in result.checks if not ok]
    assert missing == [f"bundle has {relative}" for relative in REQUIRED_BUNDLE_FILES]
    assert result.report().endswith("self-test FAILED\n")


def test_unreadable_sample_fails_without_plotting(qapp: object, tmp_path: Path) -> None:
    empty = tmp_path / "empty.csv"
    empty.write_text("", encoding="ascii")
    result = run(empty)
    assert not result.passed
    assert result.checks[-1][0] == "open the sample CSV"
    assert "has no data" in result.checks[-1][2]

    result = run(tmp_path / "missing.csv")
    assert not result.passed and "FileNotFoundError" in result.checks[-1][2]


def test_a_failing_start_is_reported(qapp: object, monkeypatch: pytest.MonkeyPatch) -> None:
    from lbr_dashboard.ui import main_window

    def broken(self: object) -> None:
        raise RuntimeError("no display")

    monkeypatch.setattr(main_window.MainWindow, "show", broken)
    result = run(SAMPLE)
    assert [(name, ok) for name, ok, _ in result.checks] == [("start the main window", False)]
    assert "RuntimeError: no display" in result.report()


def test_plot_failures_are_reported(qapp: object, monkeypatch: pytest.MonkeyPatch) -> None:
    from lbr_dashboard.ui.right_panel import right_panel

    monkeypatch.setattr(right_panel.RightPanel, "plot_column", lambda self, *args: None)
    result = run(SAMPLE)
    assert not result.passed
    assert result.checks[-1][0] == "plot a column"
    assert "0 curves drawn" in result.checks[-1][2]


def test_main_writes_the_report_and_exit_code(qapp: object, tmp_path: Path) -> None:
    report = tmp_path / "report.txt"
    assert self_test.main(["--self-test", "--report", str(report), "--sample", str(SAMPLE)]) == 0
    assert report.read_text(encoding="utf-8").endswith("self-test passed\n")

    missing = tmp_path / "nope.csv"
    assert self_test.main(["--self-test", "--report", str(report), "--sample", str(missing)]) == 1
    assert "FAILED" in report.read_text(encoding="utf-8")


def test_frozen_builds_use_the_executable_folder(
    qapp: object, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    assert bundle_root() is None
    exe = tmp_path / "LBRDashboard.exe"
    monkeypatch.setattr(sys, "frozen", True, raising=False)
    monkeypatch.setattr(sys, "executable", str(exe))
    assert bundle_root() == tmp_path.resolve()

    # Without --sample the frozen app opens samples/ beside the executable.
    complete_bundle(tmp_path)
    (tmp_path / "samples").mkdir()
    (tmp_path / "samples" / "sample_baja_log.csv").write_bytes(SAMPLE.read_bytes())
    report = tmp_path / "report.txt"
    assert self_test.main(["--self-test", "--report", str(report)]) == 0


def test_an_empty_result_has_not_passed() -> None:
    assert not SelfTestResult().passed
