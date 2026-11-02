"""Firmware lbr-csv sessions open in the session view and plot correctly."""

from __future__ import annotations

from collections.abc import Iterator
from pathlib import Path

import numpy as np
import pytest
from PySide6.QtCore import QPointF
from pytestqt.qtbot import QtBot

from lbr_dashboard.app import create_main_window
from lbr_dashboard.ui import lbr_session_view, main_window
from lbr_dashboard.ui.lbr_session_view import LbrSessionView, clock_of
from lbr_dashboard.ui.main_window import MainWindow

ROOT = Path(__file__).resolve().parents[2]
FIXTURE = ROOT / "tests" / "fixtures" / "lbr" / "session.csv"
DEMO = ROOT / "sample_data" / "lbr_demo_session.csv"
LEGACY = ROOT / "sample_data" / "sample_baja_log.csv"


@pytest.fixture
def window(qtbot: QtBot) -> Iterator[MainWindow]:
    widget = create_main_window()
    qtbot.addWidget(widget)
    widget.show()
    yield widget
    if widget.import_error is not None:
        widget.import_error.close()


def view_of(window: MainWindow) -> LbrSessionView:
    return window.lbr_view


def check(view: LbrSessionView, *ids: str) -> None:
    for channel_id in ids:
        view.browser.set_checked(channel_id, True)


def table_text(table, row: int, column: int) -> str:
    item = table.item(row, column)
    assert item is not None
    return item.text()


# Opening ---------------------------------------------------------------------------------


def test_firmware_fixture_opens_in_the_session_view(window: MainWindow) -> None:
    window.load_csv(FIXTURE)
    view = view_of(window)
    assert window.pages.currentWidget() is view
    assert window.windowTitle() == "LBR Dashboard - session.csv"
    assert view.browser.tree.topLevelItemCount() == 7
    assert view.browser.summary.text() == "47 channels from 7 nodes (1 on node clocks)"
    assert view.tabs.tabText(2) == "Session (4 warnings)" and view.tabs.currentIndex() == 2
    assert "BROWNOUT" in view.warnings.text()
    fields = {
        table_text(view.session_table, row, 0): table_text(view.session_table, row, 1)
        for row in range(view.session_table.rowCount())
    }
    assert fields["session.reset_reason"] == "5 (BROWNOUT)"
    assert fields["config.can_bitrate"] == "500000"
    assert fields["integrity.truncated_tail"] == "9"
    assert view.tabs.tabText(1) == "Events (3)"
    assert table_text(view.events_table, 2, 3) == "NODE_LOST"


def test_clean_session_shows_no_warnings(window: MainWindow, tmp_path: Path) -> None:
    path = tmp_path / "clean.csv"
    path.write_text(
        "t_s,kind,node,name,value,unit,status,synced,seq\n"
        "0.000000,format,,lbr_csv_version,1,,,,\n"
        "1.000000,session,central,protocol_version,4,,,,\n"
        "1.000000,config,central,,orphan line,,,,\n"
        "1.000000,sample,imu,X,1,,ok,1,1\n"
        "2.000000,end,central,records_written,1,,,,\n"
        "2.000000,integrity,central,records,1,,,,\n"
        "2.500000,event,stress,TIME_SYNCED,0,,ok,0,1\n"
    )
    window.load_csv(path)
    view = view_of(window)
    assert "No integrity problems" in view.warnings.text()
    assert view.tabs.tabText(2) == "Session" and view.tabs.currentIndex() == 0
    assert table_text(view.events_table, 0, 0) == "2.500000 (node clock)"
    view._focus_event(0, 0)  # node-clock events cannot be placed on the shared axis


def test_legacy_csv_still_uses_the_table_view(window: MainWindow) -> None:
    window.load_csv(DEMO)
    window.load_csv(LEGACY)
    assert window.pages.currentIndex() == 0
    assert window.left_panel.list_widget.count() == 16
    assert window.windowTitle() == "LBR Dashboard - sample_baja_log.csv"


def test_failed_import_keeps_the_current_session(window: MainWindow, tmp_path: Path) -> None:
    window.load_csv(FIXTURE)
    session = view_of(window).session
    bad = tmp_path / "v2.csv"
    bad.write_text(
        "t_s,kind,node,name,value,unit,status,synced,seq\n0.000000,format,,lbr_csv_version,2,,,,\n"
    )
    window.load_csv(bad)
    assert view_of(window).session is session
    box = window.import_error
    assert box is not None and box.isVisible() and not box.isModal()
    assert "version 2" in box.text()
    assert "still shown" in box.informativeText()


def test_cancelled_import_changes_nothing(
    window: MainWindow, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(main_window.QProgressDialog, "wasCanceled", lambda self: True)
    assert window.load_lbr_csv(DEMO) is False
    assert view_of(window).session is None and window.pages.currentIndex() == 0
    assert window.import_error is None


# Plotting ---------------------------------------------------------------------------------


def test_checking_signals_plots_them_on_the_shared_clock(window: MainWindow) -> None:
    window.load_csv(DEMO)
    view = view_of(window)
    check(view, "driver_input.gas_pedal", "temperature.temp_brake_bl")
    plot = view.plot
    assert list(plot.curves) == ["driver_input.gas_pedal", "temperature.temp_brake_bl"]
    gas = plot.curves["driver_input.gas_pedal"]
    xs, ys = gas.getData()
    assert xs is not None and ys is not None and len(xs) == 998
    assert xs[0] == pytest.approx(1.00012)
    assert gas.opts["connect"] == "finite" and gas.opts["symbol"] is None
    _, brake = plot.curves["temperature.temp_brake_bl"].getData()
    assert brake is not None and int(np.isnan(brake).sum()) == 15
    assert plot.plot_item.getAxis("bottom").labelText == "Time since Central boot (s)"
    assert plot.plot_item.getAxis("left").labelText == "Value (mixed units)"
    assert [m.value() for m in plot.markers] == pytest.approx([1.0, 1.3, 5.0009, 6.5009, 9.23])
    rows = view.selected_table.rowCount()
    assert rows == 2
    assert table_text(view.selected_table, 1, 4) == "15 (SENSOR_FAULT x15)"


def test_unavailable_channel_is_never_drawn_as_zero(window: MainWindow) -> None:
    window.load_csv(DEMO)
    view = view_of(window)
    check(view, "temperature.temp_brake_br")
    curve = view.plot.curves["temperature.temp_brake_br"]
    _, ys = curve.getData()
    assert ys is not None and np.all(np.isnan(ys))
    assert "(no data: NOT_FITTED)" in str(curve.name())
    assert table_text(view.selected_table, 0, 5) == "-"
    assert table_text(view.selected_table, 0, 7) == "-"
    assert view.plot.plot_item.getAxis("left").labelText == "Value (degC)"


def test_discrete_scaled_and_sparse_channels(window: MainWindow) -> None:
    window.load_csv(FIXTURE)
    view = view_of(window)
    check(view, "central.gps_latitude", "central.gps_fix", "imu.imu_ch_accel_z")
    plot = view.plot
    _, latitude = plot.curves["central.gps_latitude"].getData()
    assert latitude is not None and latitude[0] == pytest.approx(30.284987)
    assert "[deg]" in str(plot.curves["central.gps_latitude"].name())
    assert plot.curves["central.gps_fix"].opts["stepMode"] == "right"
    assert plot.curves["imu.imu_ch_accel_z"].opts["symbol"] == "o"
    names = [table_text(view.selected_table, r, 0) for r in range(view.selected_table.rowCount())]
    latitude_row = names.index("central.GPS_LATITUDE")
    assert table_text(view.selected_table, latitude_row, 5) == "30.285"
    assert table_text(view.selected_table, latitude_row, 1) == "deg"


def test_node_clock_signals_never_share_the_central_axis(window: MainWindow) -> None:
    window.load_csv(FIXTURE)
    view = view_of(window)
    check(view, "driver_input.brake_pedal", "stress.stress_3.node-clock")
    assert view.browser.checked_ids() == ["driver_input.brake_pedal"]
    assert list(view.plot.curves) == ["driver_input.brake_pedal"]
    assert view.plot.notice.isVisibleTo(view) and "stress node's clock" in view.plot.notice.text()

    view.browser.clear_button.click()
    assert view.plot.curves == {} and not view.plot.notice.isVisibleTo(view)
    check(view, "stress.stress_3.node-clock")
    assert view.plot.plot_item.getAxis("bottom").labelText.startswith("stress node clock (s)")
    assert view.plot.markers == []
    assert table_text(view.selected_table, 0, 2) == "node"

    check(view, "driver_input.brake_pedal")
    assert "the Central's clock" in view.plot.notice.text()
    assert view.browser.checked_ids() == ["stress.stress_3.node-clock"]


def test_clock_of() -> None:
    assert clock_of([]) is None


def test_marker_count_is_bounded(window: MainWindow, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(lbr_session_view, "MAX_MARKERS", 2)
    window.load_csv(DEMO)
    assert len(view_of(window).plot.markers) == 2


# Browser and readout ----------------------------------------------------------------------


def test_filter_hides_unmatched_signals_and_empty_groups(window: MainWindow) -> None:
    window.load_csv(DEMO)
    browser = view_of(window).browser
    browser.filter.setText("rpm")
    visible = [i for i, item in browser._items.items() if not item.isHidden()]
    assert visible == ["powertrain.cvt_rpm", "powertrain.engine_rpm"]
    root = browser.tree.invisibleRootItem()
    groups = [root.child(i) for i in range(root.childCount())]
    shown = [g.text(0) for g in groups if g is not None and not g.isHidden()]
    assert shown == ["powertrain"]
    browser.filter.setText("heartbeat.err")
    assert sum(not item.isHidden() for item in browser._items.values()) == 5
    browser.filter.clear()
    assert all(not item.isHidden() for item in browser._items.values())
    browser.set_checked("does.not.exist", True)


def test_tooltips_describe_availability_and_clock(window: MainWindow) -> None:
    window.load_csv(FIXTURE)
    items = view_of(window).browser._items
    assert "unavailable: NOT_FITTED x1" in items["temperature.temp_cvt"].toolTip(0)
    assert "own clock" in items["stress.stress_3.node-clock"].toolTip(0)
    assert "unit: deg*1e-7" in items["central.gps_latitude"].toolTip(0)


def test_readout_shows_values_reasons_and_gaps(window: MainWindow) -> None:
    window.load_csv(DEMO)
    view = view_of(window)
    check(view, "driver_input.gas_pedal", "temperature.temp_brake_bl", "temperature.temp_brake_br")
    view.plot.show_readout(5_200_000)
    text = view.plot.readout.text()
    assert "t = 5.200000 s" in text and "SENSOR_FAULT" in text and "NOT_FITTED" in text
    assert view.plot.cursor_line.value() == pytest.approx(5.2)
    view.plot.show_readout(0)
    assert view.plot.readout.text().count(" -</span>") == 0 and " -" in view.plot.readout.text()


def test_mouse_movement_updates_the_readout(window: MainWindow, qtbot: QtBot) -> None:
    window.load_csv(DEMO)
    view = view_of(window)
    view.plot._mouse_moved(QPointF(0, 0))
    check(view, "powertrain.engine_rpm")
    qtbot.waitUntil(lambda: view.plot.plot_item.sceneBoundingRect().width() > 0)
    view.plot._mouse_moved(QPointF(-10_000, -10_000))
    assert view.plot.readout.text() == ""
    box = view.plot._view()
    box.autoRange()
    inside = box.mapViewToScene(box.viewRect().center())
    view.plot._mouse_moved(inside)
    assert "ENGINE_RPM" in view.plot.readout.text() and "rpm" in view.plot.readout.text()


def test_double_clicking_an_event_centers_the_plot(window: MainWindow) -> None:
    window.load_csv(DEMO)
    view = view_of(window)
    row = next(
        r
        for r in range(view.events_table.rowCount())
        if table_text(view.events_table, r, 1) == "fault"
    )
    view.events_table.cellDoubleClicked.emit(row, 0)
    low, high = view.plot._view().viewRange()[0]
    assert (low + high) / 2 == pytest.approx(5.0009)
