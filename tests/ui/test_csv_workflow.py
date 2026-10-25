"""Exercise the menu action, Qt error boundary, retained session, and recovery."""

import os
import sys
from collections.abc import Iterator
from pathlib import Path

import pytest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtGui import QAction
from PySide6.QtWidgets import QFileDialog

from lbr_dashboard.app import create_application, create_main_window
from lbr_dashboard.core import LogImporter, LogSession, Registry
from lbr_dashboard.dashboard_controller import DashboardController
from lbr_dashboard.importers.csv_importer import CsvImporter
from lbr_dashboard.ui.main_window import MainWindow


@pytest.fixture
def window() -> Iterator[MainWindow]:
    application = create_application([])
    view = create_main_window()
    view.show()
    application.processEvents()
    try:
        yield view
    finally:
        view.close()
        application.processEvents()


def _trigger_open(window: MainWindow, monkeypatch: pytest.MonkeyPatch, path: str) -> None:
    monkeypatch.setattr(QFileDialog, "getOpenFileName", lambda *args: (path, "CSV Files (*.csv)"))
    action = next(
        action for action in window.menuBar().findChildren(QAction) if action.text() == "&Open"
    )
    action.trigger()
    create_application([]).processEvents()


def _assert_series(window: MainWindow, xs: list[float], ys: list[float]) -> None:
    window.left_panel.list_widget.setCurrentRow(1)
    plot = window.right_panel.plot_widget.getPlotItem()
    assert plot is not None
    curves = plot.listDataItems()
    assert len(curves) == 1
    actual_x, actual_y = curves[0].getData()
    assert list(actual_x) == xs
    assert list(actual_y) == ys


@pytest.mark.parametrize(
    "failure", ["missing", "encoding", "directory", "permission", "unexpected"]
)
def test_menu_failure_preserves_data_and_recovers(
    window: MainWindow,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    failure: str,
) -> None:
    initial = tmp_path / "initial log \u521d\u56de.csv"
    initial.write_text("timestamp_ms,rpm\n100,900\n110,950\n", encoding="utf-8")
    replacement = tmp_path / "replacement.csv"
    replacement.write_text(
        "timestamp_ms,rpm,note\n200,1200,start\n220,1300,end\n", encoding="utf-8"
    )
    bad = tmp_path / "bad.csv"
    if failure == "encoding":
        bad.write_bytes(b"timestamp_ms,rpm\n0,\xff\n")
    elif failure == "directory":
        bad.mkdir()
    failures: list[tuple[str, str]] = []
    unhandled: list[object] = []
    window.csv_load_failed.connect(lambda path, message: failures.append((path, message)))
    monkeypatch.setattr(sys, "excepthook", lambda *error: unhandled.append(error))

    _trigger_open(window, monkeypatch, str(initial))
    assert window.left_panel.list_widget.count() == 2
    assert window.right_panel.table.rowCount() == 2
    _assert_series(window, [0, 10], [900, 950])
    capsys.readouterr()
    with monkeypatch.context() as importer_patch:
        if failure in {"permission", "unexpected"}:

            def fail_import(self: CsvImporter, path: Path):
                if failure == "permission":
                    raise PermissionError("access denied")
                raise ValueError("importer rejected input")

            importer_patch.setattr(CsvImporter, "import_session", fail_import)
        # Repeat failures to check that one request produces one failure signal.
        for attempt in range(2):
            _trigger_open(window, monkeypatch, str(bad))
            assert len(failures) == attempt + 1
            assert failures[-1][0] == str(bad)
            assert failures[-1][1]
            assert "Could not load" in window.statusBar().currentMessage()
            assert window.statusBar().isVisible()
            item = window.right_panel.table.item(0, 1)
            assert item is not None and item.text() == "900"
            _assert_series(window, [0, 10], [900, 950])
            assert unhandled == []
            assert capsys.readouterr().err == ""

    # Cancelling the dialog must not load anything or emit a failure.
    _trigger_open(window, monkeypatch, "")
    assert len(failures) == 2
    _assert_series(window, [0, 10], [900, 950])
    _trigger_open(window, monkeypatch, str(replacement))
    assert window.statusBar().currentMessage() == ""
    assert len(failures) == 2
    assert window.left_panel.list_widget.count() == 3
    assert window.right_panel.table.columnCount() == 3
    item = window.right_panel.table.item(0, 1)
    assert item is not None and item.text() == "1200"
    _assert_series(window, [0, 20], [1200, 1300])
    assert unhandled == []
    assert capsys.readouterr().err == ""


def test_direct_loader_preserves_original_exception(
    window: MainWindow, monkeypatch: pytest.MonkeyPatch
) -> None:
    original = PermissionError("access denied")

    def fail_import(self: CsvImporter, path: Path):
        raise original

    monkeypatch.setattr(CsvImporter, "import_session", fail_import)
    with pytest.raises(PermissionError) as raised:
        window.load_csv(Path("unreadable.csv"))
    assert raised.value is original


def test_uncomposed_window_reports_missing_loader() -> None:
    create_application([])
    window = MainWindow()
    try:
        with pytest.raises(RuntimeError, match="No CSV loader"):
            window.load_csv(Path("anything.csv"))
    finally:
        window.close()


def test_first_load_failure_and_cancel_leave_empty_window(
    window: MainWindow,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    failures: list[tuple[str, str]] = []
    window.csv_load_failed.connect(lambda path, message: failures.append((path, message)))
    _trigger_open(window, monkeypatch, "")
    assert failures == []
    _trigger_open(window, monkeypatch, str(tmp_path / "missing.csv"))
    assert len(failures) == 1
    assert window.left_panel.list_widget.count() == 0
    assert window.right_panel.table.rowCount() == 0
    sample = Path(__file__).parents[2] / "sample_data" / "sample_baja_log.csv"
    _trigger_open(window, monkeypatch, str(sample))
    assert window.right_panel.table.rowCount() == 10
    assert window.statusBar().currentMessage() == ""
    assert len(failures) == 1


def test_application_can_select_another_importer_without_widget_changes() -> None:
    create_application([])
    calls: list[Path] = []

    class CustomImporter:
        def import_session(self, path: Path) -> LogSession:
            calls.append(path)
            return LogSession.from_rows(
                ("timestamp_ms", "custom.signal/999"), (("-20", "17.5"), ("-5", "19.25"))
            )

    providers = Registry[LogImporter]("importers")
    providers.register("custom", CustomImporter)
    window = MainWindow()
    window.attach_controller(DashboardController(window, providers.create("custom")))
    try:
        path = Path("does-not-need-to-exist.custom")
        window.load_csv(path)
        assert calls == [path]
        item = window.left_panel.list_widget.item(1)
        assert item is not None and item.text() == "custom.signal/999"
        assert not hasattr(window, "_log_store")
        _assert_series(window, [0, 15], [17.5, 19.25])
    finally:
        window.close()
