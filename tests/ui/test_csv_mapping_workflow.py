"""Open arbitrary CSV files through the source picker's column mapping dialog (#31)."""

import csv
import hashlib
from pathlib import Path

import pytest
from PySide6.QtCore import Qt
from PySide6.QtWidgets import QFileDialog

from lbr_dashboard.app import create_main_window
from lbr_dashboard.core import SourceState
from lbr_dashboard.core.csv_mapping import SignalKind
from lbr_dashboard.source_controller import SourceController
from lbr_dashboard.ui.csv_mapping_dialog import CsvMappingDialog

ROOT = Path(__file__).resolve().parents[2]
LEGACY = ROOT / "sample_data" / "sample_baja_log.csv"
S = 1_000_000_000
MS = 1_000_000

# Unfamiliar time header in seconds, BOM, CRLF, quoted fields, missing values, an
# invalid time, an invalid number, a repeated time and a row out of time order.
ODD_CSV = (
    b"\xef\xbb\xbf"
    b'Elapsed Seconds,"Pressure, inlet",Temp [degC],Mode,Junk\r\n'
    b"0.5,1.5,20,idle,x\r\n"  # line 2
    b'1.0,,21,"run, fast",x\r\n'  # line 3: pressure missing
    b"oops,2.0,22,run,x\r\n"  # line 4: no valid time, skipped
    b'1.5,2.5,NA,"say ""hi""",x\r\n'  # line 5: temp missing
    b"1.5,2.6,23,run,x\r\n"  # line 6: repeated time
    b"1.25,2.4,bad,idle,x\r\n"  # line 7: out of order, temp invalid
)


@pytest.fixture
def app(qtbot):
    window = create_main_window()
    window.show()
    owner = window.source_owner
    assert isinstance(owner, SourceController)
    yield window, owner
    owner.disconnect()
    qtbot.waitUntil(lambda: owner.source is None and owner._preparing is None)
    window.close()
    window.deleteLater()


def open_dialog(qtbot, window, owner, monkeypatch, path: Path) -> CsvMappingDialog:
    monkeypatch.setattr(QFileDialog, "getOpenFileName", lambda *args: (str(path), ""))
    picker = window.source_picker
    picker.choices.setCurrentIndex(picker.choices.findData("csv_mapped"))
    qtbot.mouseClick(picker.connect_button, Qt.MouseButton.LeftButton)
    dialog = owner.mapping_dialog
    assert isinstance(dialog, CsvMappingDialog)
    qtbot.waitUntil(dialog.isVisible)
    return dialog


def wait_done(qtbot, owner):
    qtbot.waitUntil(
        lambda: (
            owner.source is not None
            and owner._preparing is None
            and not owner._releasing
            and owner.source.state is SourceState.ENDED
            and owner.source.events.stats.buffered == 0
        )
    )
    assert owner.session is not None
    return owner.session.snapshot()


def series(snapshot, identifier):
    return [(r.sample.timestamp_ns, r.sample.value) for r in snapshot.series[identifier]]


def test_unfamiliar_csv_maps_previews_problems_and_imports_file_values(
    app, qtbot, tmp_path, monkeypatch
):
    window, owner = app
    path = tmp_path / "bench log.csv"
    path.write_bytes(ODD_CSV)
    digest = hashlib.sha256(ODD_CSV).hexdigest()
    dialog = open_dialog(qtbot, window, owner, monkeypatch, path)

    # Nothing is guessed: the time column and unit start unset and Import is disabled.
    assert dialog.headers[0] == "Elapsed Seconds"  # BOM removed
    assert dialog.headers[1] == "Pressure, inlet"
    assert dialog.time_column.currentData() == -1
    assert dialog.time_unit.currentData() == ""
    assert not dialog.import_button.isEnabled()
    problems = dialog.problem_texts()
    assert "Fix: Choose the time column." in problems
    assert any("No column looks like time" in text for text in problems)
    cells = [dialog.sample.item(row, 3) for row in (1, 3)]
    assert [cell.text() if cell else None for cell in cells] == ["run, fast", 'say "hi"']

    dialog.set_time(0, "s")
    dialog.set_column(1, name="inlet_pressure", unit="kPa")
    dialog.set_column(2, unit="deg C")
    dialog.set_column(4, included=False)
    dialog.refresh()
    assert dialog.kind_box(3).currentData() == SignalKind.TEXT.value
    assert not dialog.import_button.isEnabled()
    assert any("'deg C'" in text and "spaces" in text for text in dialog.problem_texts())
    qtbot.mouseClick(dialog.import_button, Qt.MouseButton.LeftButton)
    assert dialog.isVisible() and owner.session is None

    dialog.set_column(2, unit="degC")
    dialog.refresh()
    assert dialog.import_button.isEnabled()
    problems = dialog.problem_texts()
    assert "Line 4, 'Elapsed Seconds': skipped: 'oops' is not a number" in problems
    assert "Line 7, 'Temp [degC]': kept as missing: 'bad' is not a number" in problems
    assert any("out of time order" in text for text in problems)
    assert "1 row skipped" in dialog.summary.text()
    assert "1 repeated time" in dialog.summary.text()

    qtbot.mouseClick(dialog.import_button, Qt.MouseButton.LeftButton)
    snapshot = wait_done(qtbot, owner)
    descriptors = {d.identifier: d for d in snapshot.schema.signals}
    assert sorted(descriptors) == ["csv.column.1", "csv.column.2", "csv.column.3"]
    pressure, temp, mode = (descriptors[f"csv.column.{i}"] for i in (1, 2, 3))
    assert (pressure.display_name, pressure.canonical_unit, pressure.data_type) == (
        "inlet_pressure",
        "kPa",
        "numeric",
    )
    assert (temp.display_name, temp.canonical_unit, temp.data_type) == ("Temp", "degC", "numeric")
    assert (mode.display_name, mode.canonical_unit, mode.data_type) == ("Mode", None, "string")
    # Missing and invalid cells produce no sample; repeated times are both kept.
    assert series(snapshot, "csv.column.1") == [
        (S // 2, 1.5),
        (5 * S // 4, 2.4),
        (3 * S // 2, 2.5),
        (3 * S // 2, 2.6),
    ]
    assert series(snapshot, "csv.column.2") == [(S // 2, 20.0), (S, 21.0), (3 * S // 2, 23.0)]
    assert series(snapshot, "csv.column.3") == [
        (S // 2, "idle"),
        (S, "run, fast"),
        (5 * S // 4, "idle"),
        (3 * S // 2, 'say "hi"'),
        (3 * S // 2, "run"),
    ]
    status = window.source_picker.status.text()
    assert "Imported 3 signals from 6 rows, 1 row skipped" in status
    assert "ended" in status
    assert window.right_panel.plot_widget.getPlotItem().listDataItems()
    assert hashlib.sha256(path.read_bytes()).hexdigest() == digest


def test_legacy_example_default_is_visible_and_editable(app, qtbot, monkeypatch):
    window, owner = app
    with LEGACY.open(newline="", encoding="utf-8") as stream:
        header, *rows = list(csv.reader(stream))
    dialog = open_dialog(qtbot, window, owner, monkeypatch, LEGACY)
    # The familiar timestamp_ms header is pre-selected, shown and still editable.
    assert dialog.time_column.currentData() == 0
    assert dialog.time_unit.currentData() == "ms"
    assert dialog.time_unit.isEnabled() and dialog.time_column.isEnabled()
    assert dialog.import_button.isEnabled()
    qtbot.mouseClick(dialog.import_button, Qt.MouseButton.LeftButton)
    snapshot = wait_done(qtbot, owner)
    names = {d.identifier: d.display_name for d in snapshot.schema.signals}
    assert names == {f"csv.column.{i}": name for i, name in enumerate(header) if i}
    for column in range(1, len(header)):
        expected = [(int(row[0]) * MS, float(row[column])) for row in rows]
        assert series(snapshot, f"csv.column.{column}") == expected
    assert "Imported 15 signals from 10 rows" in window.source_picker.status.text()

    dialog = open_dialog(qtbot, window, owner, monkeypatch, LEGACY)
    dialog.set_time(0, "s")
    qtbot.mouseClick(dialog.import_button, Qt.MouseButton.LeftButton)
    qtbot.waitUntil(lambda: owner.session is not None and owner.session.snapshot() != snapshot)
    snapshot = wait_done(qtbot, owner)
    assert [t for t, _ in series(snapshot, "csv.column.15")] == [int(r[0]) * S for r in rows]


def test_cancel_leaves_current_session_untouched(app, qtbot, tmp_path, monkeypatch):
    window, owner = app
    first = tmp_path / "first.csv"
    first.write_text("time (ms),a\n0,1\n10,2\n", encoding="utf-8")
    dialog = open_dialog(qtbot, window, owner, monkeypatch, first)
    qtbot.mouseClick(dialog.import_button, Qt.MouseButton.LeftButton)
    snapshot = wait_done(qtbot, owner)
    session, source, status = owner.session, owner.source, window.source_picker.status.text()
    assert series(snapshot, "csv.column.1") == [(0, 1.0), (10 * MS, 2.0)]

    other = tmp_path / "other.csv"
    other.write_bytes(ODD_CSV)
    dialog = open_dialog(qtbot, window, owner, monkeypatch, other)
    dialog.set_time(0, "s")
    qtbot.mouseClick(
        dialog.buttons.button(dialog.buttons.StandardButton.Cancel), Qt.MouseButton.LeftButton
    )
    qtbot.waitUntil(lambda: not dialog.isVisible())
    qtbot.wait(100)
    assert owner.session is session and owner.source is source
    assert owner._preparing is None
    assert owner.session.snapshot().schema == snapshot.schema
    assert series(owner.session.snapshot(), "csv.column.1") == [(0, 1.0), (10 * MS, 2.0)]
    assert window.source_picker.status.text() == status


def test_unreadable_file_reports_and_keeps_session(app, qtbot, tmp_path, monkeypatch):
    window, owner = app
    empty = tmp_path / "empty.csv"
    empty.write_text("", encoding="utf-8")
    monkeypatch.setattr(QFileDialog, "getOpenFileName", lambda *args: (str(empty), ""))
    picker = window.source_picker
    picker.choices.setCurrentIndex(picker.choices.findData("csv_mapped"))
    qtbot.mouseClick(picker.connect_button, Qt.MouseButton.LeftButton)
    assert owner.mapping_dialog is None
    assert "Could not open empty.csv" in picker.status.text()
    assert owner.session is None
