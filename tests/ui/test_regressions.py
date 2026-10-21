"""Regression checks for import, selection, plotting, and application lifecycle."""

from collections.abc import Iterator
from pathlib import Path

import pytest
from PySide6.QtCore import QTimer
from PySide6.QtWidgets import QApplication, QFileDialog, QMenu
from pytestqt.qtbot import QtBot

from lbr_dashboard import app
from lbr_dashboard.resources import resource_bytes, resource_path
from lbr_dashboard.ui.main_window import MainWindow


@pytest.fixture
def window(qtbot: QtBot) -> Iterator[MainWindow]:
    widget = app.create_main_window()
    qtbot.addWidget(widget)
    widget.show()
    yield widget
    widget.close()


def test_selecting_columns_updates_table_and_plot(window: MainWindow, tmp_path: Path) -> None:
    path = tmp_path / "gaps.csv"
    path.write_text("timestamp_ms,rpm,note\n-20,900,start\n-10,,middle\n0,1000,end\n")
    window.load_csv(path)
    window.left_panel.list_widget.setCurrentRow(1)
    plot = window.right_panel.plot_widget.getPlotItem()
    assert plot is not None
    x_values, y_values = plot.listDataItems()[0].getData()
    assert list(x_values) == [0.0, 20.0]
    assert list(y_values) == [900.0, 1000.0]
    assert {item.column() for item in window.right_panel.table.selectedItems()} == {1}

    window.left_panel.list_widget.setCurrentRow(2)
    assert not plot.listDataItems()
    window.left_panel.list_widget.setCurrentRow(-1)
    assert not plot.listDataItems()


def test_second_import_replaces_old_session(window: MainWindow, tmp_path: Path) -> None:
    first = tmp_path / "first.csv"
    second = tmp_path / "second.csv"
    first.write_text("timestamp_ms,rpm,note\n0,900,start\n1,950,next\n")
    second.write_text("speed\n42\n")
    window.load_csv(first)
    window.load_csv(second)
    assert window.left_panel.list_widget.count() == 1
    assert window.right_panel.table.rowCount() == 1
    assert window.right_panel.table.columnCount() == 1
    item = window.right_panel.table.item(0, 0)
    assert item is not None
    assert item.text() == "42"
    plot = window.right_panel.plot_widget.getPlotItem()
    assert plot is not None
    x_values, y_values = plot.listDataItems()[0].getData()
    assert list(x_values) == [0.0]
    assert list(y_values) == [42.0]


def test_empty_import_does_not_disconnect_displayed_data(
    window: MainWindow, tmp_path: Path
) -> None:
    path = tmp_path / "valid.csv"
    empty = tmp_path / "empty.csv"
    path.write_text("timestamp_ms,rpm\n0,900\n10,950\n")
    empty.write_text("")
    window.load_csv(path)
    window.load_csv(empty)
    window.left_panel.list_widget.setCurrentRow(1)
    plot = window.right_panel.plot_widget.getPlotItem()
    assert plot is not None
    _, values = plot.listDataItems()[0].getData()
    assert list(values) == [900.0, 950.0]


def test_file_menu_opens_csv_and_cancel_preserves_session(
    window: MainWindow, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = tmp_path / "selected.csv"
    path.write_text("speed\n23\n")
    monkeypatch.setattr(QFileDialog, "getOpenFileName", lambda *args: (str(path), "CSV"))
    menu = window.menuBar().actions()[0].menu()
    assert isinstance(menu, QMenu)
    menu.actions()[0].trigger()
    assert window.right_panel.table.rowCount() == 1
    monkeypatch.setattr(QFileDialog, "getOpenFileName", lambda *args: ("", ""))
    menu.actions()[0].trigger()
    assert window.right_panel.table.rowCount() == 1
    menu.actions()[-1].trigger()
    assert not window.isVisible()


@pytest.mark.parametrize(
    ("values", "timestamps", "expected_x", "expected_y"),
    [
        ((1.0, None, 3.0), None, [0.0, 2.0], [1.0, 3.0]),
        ((1.0, 2.0, 3.0), (None, -10.0, 0.0), [0.0, 10.0], [2.0, 3.0]),
        ((1.0, 2.0), (None, None), [], []),
        ((None, None), None, [], []),
        (None, None, [], []),
    ],
)
def test_plot_handles_missing_values(
    window: MainWindow,
    values: tuple[float | None, ...] | None,
    timestamps: tuple[float | None, ...] | None,
    expected_x: list[float],
    expected_y: list[float],
) -> None:
    window.right_panel.plot_column("rpm", values, timestamps)
    plot = window.right_panel.plot_widget.getPlotItem()
    assert plot is not None
    if expected_x:
        xs, ys = plot.listDataItems()[0].getData()
        assert list(xs) == expected_x
        assert list(ys) == expected_y
    else:
        assert not plot.listDataItems()


def test_real_entrypoint_runs_event_loop_and_returns_exit_code(
    qapp: QApplication, qtbot: QtBot, monkeypatch: pytest.MonkeyPatch
) -> None:
    window = app.create_main_window()
    qtbot.addWidget(window)
    monkeypatch.setattr(app, "create_main_window", lambda: window)
    monkeypatch.setattr(app.signal, "signal", lambda *args: None)
    QTimer.singleShot(0, lambda: qapp.exit(7))
    assert app.main([]) == 7


def test_binary_and_path_resources() -> None:
    expected = resource_bytes("version.py")
    assert b"__version__" in expected
    with resource_path("version.py") as path:
        assert path.read_bytes() == expected
    with pytest.raises(FileNotFoundError):
        resource_bytes("missing-resource.txt")
