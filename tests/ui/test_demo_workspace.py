"""The bundled demo workspace works with unrelated demo sources and rates."""

from __future__ import annotations

from pathlib import Path

from PySide6.QtCore import Qt
from PySide6.QtWidgets import QPushButton

from lbr_dashboard.app import create_main_window
from lbr_dashboard.source_controller import SourceController
from lbr_dashboard.ui.docks import SignalDock
from lbr_dashboard.ui.main_window import MainWindow
from lbr_dashboard.workspace import demo_workspace, dumps_workspace, read_workspace

SAMPLES = Path(__file__).resolve().parents[2] / "sample_data"
PRESET = SAMPLES / "workspaces" / "generic_demo.json"


def test_sample_preset_file_is_the_bundled_demo_workspace() -> None:
    assert PRESET.read_text(encoding="ascii") == dumps_workspace(demo_workspace())
    workspace = read_workspace(PRESET)
    # Every bound ID comes from a bundled demo source, never from vehicle hardware.
    assert all(
        signal.startswith(("process.", "motion.", "csv.column."))
        for signal in workspace.signal_ids()
    )


def trigger(window: MainWindow, text: str) -> None:
    next(a for a in window.workspace_docks.menu.actions() if a.text() == text).trigger()


def load(qtbot, window: MainWindow, family: str) -> None:
    owner = window.source_owner
    assert isinstance(owner, SourceController)
    if family == "csv":
        owner.open_csv(SAMPLES / "timestamped_demo.csv")
        prefix = "csv.column."
    else:
        owner.choose(family)
        prefix = f"{family}."
    qtbot.waitUntil(
        lambda: any(d.identifier.startswith(prefix) for d in window.analysis.descriptors),
        timeout=10_000,
    )
    qtbot.waitUntil(lambda: owner._finished_rendered, timeout=10_000)


def resolved(dock: SignalDock) -> bool:
    return not dock.pending and dock.binding is not None


def traces(dock: SignalDock) -> list[str]:
    assert dock.multi_plot is not None
    return list(dock.multi_plot.bindings)


def test_demo_workspace_fills_in_for_each_unrelated_demo_source(qtbot, tmp_path):
    window = create_main_window()
    qtbot.addWidget(window)
    window.show()
    trigger(window, "Open demo workspace")
    manager = window.workspace_manager
    docks = window.workspace_docks.docks
    assert manager.name == "Demo overview" and manager.path is None
    assert set(docks) == {
        "process-plot",
        "motion-plot",
        "csv-plot",
        "process-status",
        "motion-vector",
        "csv-valve",
    }
    assert "statistics" in window.analysis_docks.docks
    # Without a source every widget is an editable placeholder; nothing fails.
    assert all(dock.pending for dock in docks.values())
    assert "9 signal binding(s) are missing" in manager.last_message
    assert docks["motion-plot"] in window.tabifiedDockWidgets(docks["process-plot"])
    assert window.dockWidgetArea(window.analysis_docks.docks["statistics"]) == (
        Qt.DockWidgetArea.BottomDockWidgetArea
    )

    # Process simulator: two numeric signals at different rates and a status signal.
    load(qtbot, window, "process")
    assert traces(docks["process-plot"]) == ["process.pressure", "process.temperature"]
    assert resolved(docks["process-status"])
    assert "Missing" not in docks["process-status"].feedback.text()
    assert "Missing signal motion.speed - bind an available signal." in (
        docks["motion-plot"].feedback.text()
    )
    assert not resolved(docks["csv-valve"])

    # Motion simulator: numeric and boolean traces on separate axes, a vector value.
    load(qtbot, window, "motion")
    plot = docks["motion-plot"].multi_plot
    assert plot is not None
    assert traces(docks["motion-plot"]) == ["motion.speed", "motion.enabled"]
    assert {b.axis for b in plot.bindings.values()} == {"left", "right"}
    assert resolved(docks["motion-vector"])

    # The CSV demo: unitless numeric columns and a string state.
    load(qtbot, window, "csv")
    assert traces(docks["csv-plot"]) == ["csv.column.1", "csv.column.2"]
    assert resolved(docks["csv-valve"])
    # The tabs that just found their signals come to the front.
    qtbot.waitUntil(lambda: not docks["csv-plot"].visibleRegion().isEmpty())
    assert docks["process-plot"].visibleRegion().isEmpty()
    assert not docks["csv-valve"].visibleRegion().isEmpty()
    assert docks["csv-valve"].value_widget is not None
    assert docks["csv-valve"].value_widget.value_label.text().startswith('"')

    # The preset is editable: rebind, remove and save, then reopen the edit.
    window.left_panel.signal_browser.select_id("csv.column.2", emit=True)
    csv_plot = docks["csv-plot"]
    next(
        b for b in csv_plot.findChildren(QPushButton) if b.text() == "Bind selected signal"
    ).click()
    assert traces(csv_plot) == ["csv.column.2"]
    window.workspace_docks.remove("motion-vector")
    path = tmp_path / "my-demo.json"
    assert manager.save_as(path)
    window.close()

    reopened = create_main_window()
    qtbot.addWidget(reopened)
    reopened.show()
    load(qtbot, reopened, "csv")
    assert reopened.workspace_manager.open_file(path)
    again = reopened.workspace_docks.docks
    assert "motion-vector" not in again
    assert traces(again["csv-plot"]) == ["csv.column.2"]
    assert resolved(again["csv-valve"])
    assert again["process-plot"].pending  # still a repairable placeholder
    reopened.close()
