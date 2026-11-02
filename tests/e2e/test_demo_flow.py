"""End-to-end demo flow without hardware: open a file, find a signal, plot it.

Runs the real application window offscreen and drives it the way a user does
(File > Open, the signal filter, checking a signal, the events list), then
checks the plotted series against an independent read of the same file.

Steps still to add as their features merge (#107):
- play / pause / seek through the shared session clock (#22, #41)
- switching the source to the deterministic simulator (#23, #35)
"""

from __future__ import annotations

import csv
import threading
from collections.abc import Iterator
from dataclasses import dataclass
from pathlib import Path

import numpy as np
import pytest
from PySide6.QtCore import Qt
from PySide6.QtGui import QAction
from PySide6.QtWidgets import QApplication, QMenu, QMessageBox
from pytestqt.qtbot import QtBot

from lbr_dashboard.app import create_main_window
from lbr_dashboard.ui import main_window
from lbr_dashboard.ui.main_window import MainWindow

ROOT = Path(__file__).resolve().parents[2]
DEMO = ROOT / "sample_data" / "lbr_demo_session.csv"
FIXTURE = ROOT / "tests" / "fixtures" / "lbr" / "session.csv"
SIGNAL_ID = "powertrain.engine_rpm"


@dataclass(frozen=True)
class FlowResult:
    """What a user would see at the end of the flow, for comparing runs."""

    title: str
    checked: tuple[str, ...]
    xs: tuple[float, ...]
    ys: tuple[float, ...]
    readout: str
    focus_center: float
    events: int


def expected_series(path: Path, node: str, name: str) -> tuple[list[float], list[float]]:
    """Read one signal straight from the CSV, independently of the importer."""

    times: list[float] = []
    values: list[float] = []
    with path.open(newline="", encoding="ascii") as handle:
        for row in csv.DictReader(handle):
            if row["kind"] == "sample" and row["node"] == node and row["name"] == name:
                seconds, micros = row["t_s"].split(".")
                times.append((int(seconds) * 1_000_000 + int(micros)) / 1e6)
                values.append(float(row["value"]))
    return times, values


def open_action(window: MainWindow) -> QAction:
    for menu_action in window.menuBar().actions():
        menu = menu_action.menu()
        if not isinstance(menu, QMenu):
            continue
        for action in menu.actions():
            if action.text().replace("&", "") == "Open":
                return action
    raise AssertionError("File > Open is missing")


def open_through_menu(window: MainWindow, path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        main_window.QFileDialog, "getOpenFileName", lambda *args, **kwargs: (str(path), "")
    )
    open_action(window).trigger()


def run_flow(window: MainWindow, qtbot: QtBot, monkeypatch: pytest.MonkeyPatch) -> FlowResult:
    view = window.lbr_view
    assert view.session is None

    # 1. Open the demo session the way a user would.
    open_through_menu(window, DEMO, monkeypatch)
    qtbot.waitUntil(lambda: window.pages.currentWidget() is view)
    session = view.session
    assert session is not None and session.source_name == DEMO.name

    # 2. Find the signal by its ID with the filter, then check it.
    view.browser.filter.setText(SIGNAL_ID)
    visible = [i for i, item in view.browser._items.items() if not item.isHidden()]
    assert visible == [SIGNAL_ID], visible
    view.browser._items[SIGNAL_ID].setCheckState(0, Qt.CheckState.Checked)

    # 3. The plot shows exactly the samples in the file, on the Central clock.
    qtbot.waitUntil(lambda: SIGNAL_ID in view.plot.curves)
    xs, ys = view.plot.curves[SIGNAL_ID].getData()
    assert xs is not None and ys is not None
    want_x, want_y = expected_series(DEMO, "powertrain", "ENGINE_RPM")
    assert len(want_x) == 998
    np.testing.assert_allclose(xs, want_x, rtol=0, atol=1e-9)
    np.testing.assert_array_equal(ys, want_y)
    assert view.plot.plot_item.getAxis("bottom").labelText == "Time since Central boot (s)"

    # 4. The readout reports the file's value at a chosen time.
    index = int(np.searchsorted(want_x, 4.0, side="right")) - 1
    view.plot.show_readout(4_000_000)
    readout = view.plot.readout.text()
    assert f"{want_y[index]:.6g} rpm" in readout

    # 5. A fault in the events list centers the plot on it.
    row = next(
        r
        for r in range(view.events_table.rowCount())
        if (item := view.events_table.item(r, 1)) is not None and item.text() == "fault"
    )
    view.events_table.cellDoubleClicked.emit(row, 0)
    low, high = view.plot._view().viewRange()[0]

    return FlowResult(
        title=window.windowTitle(),
        checked=tuple(view.browser.checked_ids()),
        xs=tuple(float(x) for x in xs),
        ys=tuple(float(y) for y in ys),
        readout=readout,
        focus_center=round((low + high) / 2, 6),
        events=len(session.events),
    )


@pytest.fixture
def window(qtbot: QtBot) -> Iterator[MainWindow]:
    widget = create_main_window()
    qtbot.addWidget(widget)
    widget.show()
    yield widget
    if widget.import_error is not None:
        widget.import_error.close()


def test_demo_flow_plots_real_values(
    window: MainWindow, qtbot: QtBot, monkeypatch: pytest.MonkeyPatch
) -> None:
    result = run_flow(window, qtbot, monkeypatch)
    assert result.title == f"LBR Dashboard - {DEMO.name}"
    assert result.checked == (SIGNAL_ID,)
    assert result.focus_center == pytest.approx(5.0009)


def test_demo_flow_is_identical_across_runs(qtbot: QtBot, monkeypatch: pytest.MonkeyPatch) -> None:
    results = []
    for _ in range(2):
        widget = create_main_window()
        qtbot.addWidget(widget)
        widget.show()
        results.append(run_flow(widget, qtbot, monkeypatch))
        widget.close()
    assert results[0] == results[1]


def test_bad_and_cancelled_sources_keep_the_plot(
    window: MainWindow, qtbot: QtBot, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    run_flow(window, qtbot, monkeypatch)
    view = window.lbr_view
    session = view.session
    before = view.plot.curves[SIGNAL_ID].getData()[1]

    bad = tmp_path / "future.csv"
    bad.write_text(
        "t_s,kind,node,name,value,unit,status,synced,seq\n0.000000,format,,lbr_csv_version,9,,,,\n"
    )
    open_through_menu(window, bad, monkeypatch)
    box = window.import_error
    assert isinstance(box, QMessageBox) and box.isVisible() and "version 9" in box.text()
    assert view.session is session and SIGNAL_ID in view.plot.curves

    monkeypatch.setattr(main_window.QProgressDialog, "wasCanceled", lambda self: True)
    open_through_menu(window, FIXTURE, monkeypatch)
    assert view.session is session
    after = view.plot.curves[SIGNAL_ID].getData()[1]
    assert after is not None and before is not None
    np.testing.assert_array_equal(after, before)


def test_closing_after_the_flow_leaves_nothing_running(
    qtbot: QtBot, monkeypatch: pytest.MonkeyPatch
) -> None:
    threads = threading.active_count()
    widget = create_main_window()
    qtbot.addWidget(widget)
    widget.show()
    run_flow(widget, qtbot, monkeypatch)
    widget.close()
    qtbot.waitUntil(lambda: not widget.isVisible())
    assert threading.active_count() == threads
    assert not [w for w in QApplication.topLevelWidgets() if w.isVisible() and w.isWindow()]


def test_a_broken_ui_binding_fails_the_flow(
    window: MainWindow, qtbot: QtBot, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Guard against a flow that would pass without the UI being wired up."""

    window.lbr_view.browser.checked_changed.disconnect()
    with pytest.raises(qtbot.TimeoutError):
        run_flow(window, qtbot, monkeypatch)
