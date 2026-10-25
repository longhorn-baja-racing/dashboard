"""CSV regressions checked through the real selection and plotting path."""

import os
from collections.abc import Iterator
from pathlib import Path

import pytest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtWidgets import QFileDialog

from lbr_dashboard.app import create_application, create_main_window
from lbr_dashboard.ui.main_window import MainWindow


@pytest.fixture
def window() -> Iterator[MainWindow]:
    application = create_application([])
    view = create_main_window()
    try:
        yield view
    finally:
        view.close()
        application.processEvents()


def _load(window: MainWindow, path: Path, data: str) -> None:
    path.write_text(data, encoding="utf-8")
    window.load_csv(path)


def _series(window: MainWindow, column: int) -> tuple[list[float], list[float]]:
    window.left_panel.list_widget.setCurrentRow(column)
    plot = window.right_panel.plot_widget.getPlotItem()
    assert plot is not None
    curves = plot.listDataItems()
    assert len(curves) == 1
    xs, ys = curves[0].getData()
    return list(xs), list(ys)


def test_duplicate_headers_plot_each_physical_column(window: MainWindow, tmp_path: Path) -> None:
    _load(window, tmp_path / "duplicate.csv", "timestamp_ms,rpm,rpm\n10,900,1900\n20,950,1950\n")
    assert _series(window, 1) == ([0, 10], [900, 950])
    assert _series(window, 2) == ([0, 10], [1900, 1950])
    assert _series(window, 1) == ([0, 10], [900, 950])


@pytest.mark.parametrize("empty", ["", "\n\n", "\ufeff"])
def test_empty_load_clears_table_columns_and_plot_then_recovers(
    window: MainWindow,
    tmp_path: Path,
    empty: str,
) -> None:
    path = tmp_path / "log.csv"
    _load(window, path, "timestamp_ms,rpm\n0,900\n10,950\n")
    assert _series(window, 1) == ([0, 10], [900, 950])
    _load(window, path, empty)
    assert window.left_panel.list_widget.count() == 0
    assert window.right_panel.table.rowCount() == 0
    assert window.right_panel.table.columnCount() == 0
    plot = window.right_panel.plot_widget.getPlotItem()
    assert plot is not None and plot.listDataItems() == []
    _load(window, path, "timestamp_ms,rpm\n100,1200\n120,1300\n")
    assert _series(window, 1) == ([0, 20], [1200, 1300])


@pytest.mark.parametrize("invalid", ["nan", "NaN", "inf", "-inf", "1e999", "", "nonnumeric"])
@pytest.mark.parametrize("position", [0, 1, 2])
def test_bad_timestamp_does_not_blank_valid_samples(
    window: MainWindow,
    tmp_path: Path,
    invalid: str,
    position: int,
) -> None:
    rows = ["100,900", "110,950", "120,1000"]
    rows.insert(position, f"{invalid},9999")
    _load(window, tmp_path / "timestamps.csv", "timestamp_ms,rpm\n" + "\n".join(rows))
    assert _series(window, 1) == ([0, 10, 20], [900, 950, 1000])


def test_nan_values_missing_columns_and_no_timestamp_are_safe(
    window: MainWindow, tmp_path: Path
) -> None:
    path = tmp_path / "ragged.csv"
    _load(window, path, "rpm,temperature\n900,10\nnan\n950,inf\n1000,20\n")
    assert _series(window, 0) == ([0, 2, 3], [900, 950, 1000])
    assert _series(window, 1) == ([0, 3], [10, 20])


def test_headers_only_replace_old_rows_and_clear_plot(window: MainWindow, tmp_path: Path) -> None:
    path = tmp_path / "headers.csv"
    _load(window, path, "timestamp_ms,rpm\n0,900\n")
    _load(window, path, "timestamp_ms,rpm\n")
    assert window.left_panel.list_widget.count() == 2
    assert window.right_panel.table.rowCount() == 0
    plot = window.right_panel.plot_widget.getPlotItem()
    assert plot is not None and plot.listDataItems() == []


def test_missing_cells_do_not_reuse_values_from_previous_file(
    window: MainWindow, tmp_path: Path
) -> None:
    path = tmp_path / "replaced.csv"
    _load(window, path, "timestamp_ms,rpm\n0,900\n10,950\n")
    _load(window, path, "timestamp_ms,rpm\n0\n10,1200\n")
    item = window.right_panel.table.item(0, 1)
    assert item is None or item.text() == ""
    assert _series(window, 1) == ([10], [1200])


@pytest.mark.parametrize("failure", ["missing", "encoding"])
def test_file_dialog_failure_is_visible_and_recoverable(
    window: MainWindow,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    failure: str,
) -> None:
    good = tmp_path / "good.csv"
    _load(window, good, "timestamp_ms,rpm\n100,900\n110,950\n")
    bad = tmp_path / "bad.csv"
    if failure == "encoding":
        bad.write_bytes(b"timestamp_ms,rpm\n0,\xff\n")
    failures: list[tuple[str, str]] = []
    window.csv_load_failed.connect(lambda path, message: failures.append((path, message)))
    monkeypatch.setattr(
        QFileDialog, "getOpenFileName", lambda *args: (str(bad), "CSV Files (*.csv)")
    )
    window.open_csv()
    assert len(failures) == 1
    assert failures[0][0] == str(bad)
    assert "Could not load" in window.statusBar().currentMessage()
    assert _series(window, 1) == ([0, 10], [900, 950])
    monkeypatch.setattr(
        QFileDialog, "getOpenFileName", lambda *args: (str(good), "CSV Files (*.csv)")
    )
    window.open_csv()
    assert window.statusBar().currentMessage() == ""
    assert len(failures) == 1
