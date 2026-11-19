"""Cell edits update the owner model and plotted values, never the source CSV."""

from typing import Any, cast

import pytest
from PySide6.QtCore import Qt
from PySide6.QtWidgets import QLineEdit, QTableWidgetItem

from lbr_dashboard.app import create_main_window
from lbr_dashboard.core.log import LogSession, LogSessionStore
from lbr_dashboard.importers.csv_importer import CsvImporter


def opened(qtbot, tmp_path):
    path = tmp_path / "duplicate.csv"
    path.write_text("timestamp_ms,speed,speed\n0,0,100\n10,8,200\n20,5,300\n")
    window = create_main_window()
    qtbot.addWidget(window)
    window.show()
    window.load_csv(path)
    window.left_panel.list_widget.setCurrentRow(1)
    return window, path


def cell(window, row, column):
    item = window.right_panel.table.item(row, column)
    assert item is not None
    return item


def curve(window):
    plot = window.right_panel.plot_widget.getPlotItem()
    assert plot is not None
    items = plot.listDataItems()
    return items[0].getData() if items else ([], [])


def test_actual_cell_editor_changes_graph_and_preserves_selection_and_source(
    qtbot, tmp_path
) -> None:
    window, path = opened(qtbot, tmp_path)
    original = path.read_bytes()
    item = cell(window, 0, 1)
    window.right_panel.table.editItem(item)
    editor = window.right_panel.table.findChild(QLineEdit)
    assert editor is not None
    qtbot.keyClick(editor, Qt.Key.Key_A, Qt.KeyboardModifier.ControlModifier)
    qtbot.keyClicks(editor, "20")
    qtbot.keyClick(editor, Qt.Key.Key_Return)
    qtbot.waitUntil(lambda: item.text() == "20")
    assert list(curve(window)[1]) == [20, 8, 5]
    assert window.left_panel.list_widget.currentRow() == 1
    window.left_panel.list_widget.setCurrentRow(2)
    assert list(curve(window)[1]) == [100, 200, 300]
    window.left_panel.list_widget.setCurrentRow(1)
    assert list(curve(window)[1]) == [20, 8, 5]
    assert path.read_bytes() == original
    assert "Edited in memory" in window.statusBar().currentMessage()


def test_editing_timestamp_recomputes_the_selected_signal_x_values(qtbot, tmp_path) -> None:
    window, _ = opened(qtbot, tmp_path)
    cell(window, 0, 0).setText("-10")
    xs, ys = curve(window)
    assert list(xs) == [0, 20, 30] and list(ys) == [0, 8, 5]
    cell(window, 1, 0).setText("nan")
    xs, ys = curve(window)
    assert list(xs) == [0, 30] and list(ys) == [0, 5]


@pytest.mark.parametrize("text", ["", "nan", "inf", "status"])
def test_non_numeric_edit_is_unavailable_and_recoverable(qtbot, tmp_path, text) -> None:
    window, _ = opened(qtbot, tmp_path)
    cell(window, 0, 1).setText(text)
    assert list(curve(window)[1]) == [8, 5]
    cell(window, 0, 1).setText("0")
    assert list(curve(window)[1]) == [0, 8, 5]


def test_editing_second_duplicate_column_and_ragged_cell_uses_physical_identity(
    qtbot, tmp_path
) -> None:
    window, _ = opened(qtbot, tmp_path)
    cell(window, 0, 2).setText("999")
    assert list(curve(window)[1]) == [0, 8, 5]
    window.left_panel.list_widget.setCurrentRow(2)
    assert list(curve(window)[1]) == [999, 200, 300]
    ragged = tmp_path / "ragged.csv"
    ragged.write_text("timestamp_ms,x\n0\n10,2\n")
    window.load_csv(ragged)
    window.left_panel.list_widget.setCurrentRow(1)
    window.right_panel.table.setItem(0, 1, QTableWidgetItem("7"))
    assert list(curve(window)[1]) == [7, 2]


def test_loading_or_reloading_populates_without_edit_events(qtbot, tmp_path) -> None:
    window, path = opened(qtbot, tmp_path)
    edits = []
    window.cell_edited.connect(lambda *args: edits.append(args))
    window.load_csv(path)
    assert edits == []
    window.left_panel.list_widget.setCurrentRow(1)
    cell(window, 0, 1).setText("20")
    assert edits == [(0, 1, "20")]
    window.load_csv(path)
    window.left_panel.list_widget.setCurrentRow(1)
    assert list(curve(window)[1]) == [0, 8, 5] and len(edits) == 1


def test_model_edit_is_immutable_atomic_and_validates_location() -> None:
    original = LogSession.from_rows(("x", "x"), (("1", "2"), ()))
    changed = original.with_cell(0, 1, "9")
    assert changed.numeric_column(0) == (1, None)
    assert changed.numeric_column(1) == (9, None)
    assert changed.numeric_column("x") == original.numeric_column("x")
    assert original.rows == (("1", "2"), ())
    assert changed.with_cell(1, 1, "3").rows[1] == ("", "3")
    for row, column in ((-1, 0), (2, 0), (0, 2), (True, 0), (0, 1.5)):
        with pytest.raises(IndexError):
            original.with_cell(row, cast(Any, column), "8")
    with pytest.raises(TypeError):
        original.with_cell(0, 0, cast(Any, None))
    with pytest.raises(RuntimeError):
        LogSessionStore(CsvImporter()).edit_cell(0, 0, "8")
