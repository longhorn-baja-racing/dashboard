"""Save, reopen, import and restart-restore workspaces in the real window."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest
from PySide6.QtCore import Qt
from PySide6.QtWidgets import QFileDialog, QInputDialog, QLabel, QPushButton, QVBoxLayout, QWidget

from lbr_dashboard.app import create_main_window
from lbr_dashboard.core import PreferencesStore
from lbr_dashboard.preferences_controller import LAST_WORKSPACE
from lbr_dashboard.source_controller import SourceController
from lbr_dashboard.ui.analysis import AnalysisContext, AnalysisTool
from lbr_dashboard.ui.docks import SignalDock
from lbr_dashboard.ui.main_window import MainWindow
from lbr_dashboard.ui.tools import TOOLS
from lbr_dashboard.ui.workspace_layout import SizeApplier
from lbr_dashboard.ui.workspace_manager import ImportPreviewDialog, PlaceholderDock
from lbr_dashboard.workspace import (
    CURRENT_VERSION,
    SplitLayout,
    TabLayout,
    WidgetRef,
    Workspace,
    backup_path,
    read_workspace,
    workspace_to_dict,
)

SAMPLES = Path(__file__).resolve().parents[2] / "sample_data"
DEMO_CSV = SAMPLES / "timestamped_demo.csv"


class ProbeTool(QWidget):
    """A stand-in analysis tool with its own configuration."""

    def __init__(self, context: AnalysisContext, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.settings: dict[str, Any] = {"bins": 10, "signal": None}
        QVBoxLayout(self).addWidget(QLabel("probe"))

    def configuration(self) -> dict[str, Any]:
        return dict(self.settings)

    def restore_configuration(self, settings: dict[str, Any]) -> None:
        if not isinstance(settings.get("bins"), int):
            raise ValueError("bins must be an integer")
        self.settings = dict(settings)


PROBE = AnalysisTool("probe", "Probe", ProbeTool)


def make_window(qtbot, store: PreferencesStore | None = None) -> MainWindow:
    window = create_main_window(preferences_store=store)
    qtbot.addWidget(window)
    window.analysis_docks.tools["probe"] = PROBE
    window.show()
    return window


def owner_of(window: MainWindow) -> SourceController:
    owner = window.source_owner
    assert isinstance(owner, SourceController)
    return owner


def open_simulator(qtbot, window: MainWindow, family: str = "process") -> None:
    owner = owner_of(window)
    owner._finished_rendered = False
    owner.choose(family)
    qtbot.waitUntil(lambda: owner._finished_rendered, timeout=10_000)
    qtbot.waitUntil(
        lambda: any(d.identifier.startswith(family) for d in window.analysis.descriptors)
    )


def open_csv_demo(qtbot, window: MainWindow) -> None:
    owner = owner_of(window)
    owner.open_csv(DEMO_CSV)
    qtbot.waitUntil(
        lambda: any(d.identifier == "csv.column.1" for d in window.analysis.descriptors),
        timeout=10_000,
    )


def press(dock: SignalDock, text: str) -> None:
    next(b for b in dock.findChildren(QPushButton) if b.text() == text).click()


def add_trace(window: MainWindow, dock: SignalDock, signal_id: str, axis: int = 0) -> None:
    window.left_panel.signal_browser.select_id(signal_id, emit=True)
    dock.axis_choice.setCurrentIndex(axis)
    press(dock, "Add selected signal")
    assert dock.multi_plot is not None and signal_id in dock.multi_plot.bindings


def bind_value(window: MainWindow, dock: SignalDock, signal_id: str) -> None:
    window.left_panel.signal_browser.select_id(signal_id, emit=True)
    press(dock, "Bind selected signal")
    assert dock.binding is not None and dock.binding[0] == signal_id


def trigger(window: MainWindow, text: str) -> None:
    action = next(a for a in window.workspace_docks.menu.actions() if a.text() == text)
    action.trigger()


def wait_for_layout(qtbot, window: MainWindow) -> None:
    """Let the restored split sizes settle, as they do after the platform's layout passes."""

    applier = window.findChild(SizeApplier, "workspaceSizeApplier")
    assert applier is not None
    qtbot.waitUntil(lambda: not applier.pending(), timeout=5_000)
    qtbot.wait(20)


def structure(node: Any) -> Any:
    """A layout tree without weights, which depend on pixel rounding."""

    if isinstance(node, WidgetRef):
        return node.widget
    if isinstance(node, SplitLayout):
        return (node.orientation, tuple(structure(child) for child in node.children))
    assert isinstance(node, TabLayout)
    return ("tabs", node.current, tuple(structure(child) for child in node.children))


def weights(node: Any) -> list[float]:
    if isinstance(node, WidgetRef):
        return []
    found = list(node.weights or ()) if isinstance(node, SplitLayout) else []
    for child in node.children:
        found.extend(weights(child))
    return found


def without_layout(workspace: Workspace) -> dict[str, Any]:
    data = workspace_to_dict(workspace)
    data.pop("layout")
    return data


def assert_same(actual: Workspace, expected: Workspace) -> None:
    """Equal workspaces, allowing split sizes to differ by pixel rounding."""

    assert without_layout(actual) == without_layout(expected)
    assert structure(actual.layout) == structure(expected.layout)
    assert weights(actual.layout) == pytest.approx(weights(expected.layout), abs=0.01)


def build_session_workspace(qtbot, window: MainWindow) -> None:
    """Plot, value and tool docks split, tabbed, hidden and styled by the user."""

    docks = window.workspace_docks
    plot = docks.add("plot")
    add_trace(window, plot, "process.pressure", axis=1)
    add_trace(window, plot, "process.temperature", axis=2)
    assert plot.multi_plot is not None
    plot.multi_plot.curves["process.temperature"].setVisible(False)
    status = docks.add("value")
    bind_value(window, status, "process.status")
    window.splitDockWidget(plot, status, Qt.Orientation.Horizontal)
    second = docks.add("plot")
    add_trace(window, second, "process.pressure")
    window.tabifyDockWidget(plot, second)
    plot.raise_()
    spare = docks.add("value")
    spare.close()
    probe = window.analysis_docks.open("probe").widget()
    assert isinstance(probe, ProbeTool)
    probe.settings = {"bins": 25, "signal": "process.pressure"}
    window.analysis_docks.open("statistics")
    qtbot.waitUntil(lambda: plot.visibleRegion().isEmpty() is False)


def test_save_close_and_reopen_restores_layout_bindings_and_tool_configuration(
    qtbot, tmp_path, monkeypatch
):
    path = tmp_path / "bench.json"
    window = make_window(qtbot)
    open_simulator(qtbot, window)
    build_session_workspace(qtbot, window)
    manager = window.workspace_manager
    assert manager.rename("Bench layout")
    monkeypatch.setattr(QFileDialog, "getSaveFileName", lambda *args: (str(path), ""))
    trigger(window, "Save workspace as...")
    assert manager.path == path and "Saved workspace Bench layout" in manager.last_message
    saved = read_workspace(path)
    before = manager.capture()
    assert before == saved
    text = path.read_text(encoding="ascii")
    assert str(tmp_path) not in text and "simulator" not in text  # no paths or source IDs
    data = json.loads(text)
    assert data["version"] == CURRENT_VERSION
    plot = next(w for w in data["widgets"] if w["id"] == "workspace.plot.1")
    assert [b["signal"] for b in plot["bindings"]] == ["process.pressure", "process.temperature"]
    assert plot["bindings"][1]["hidden"] is True and "hidden" not in plot["bindings"][0]
    assert {a["side"] for a in plot["axes"]} == {"left", "right"}
    tools = {w["tool"]: w for w in data["widgets"] if w["type"] == "analysis_tool"}
    assert tools["probe"]["settings"] == {"bins": 25, "signal": "process.pressure"}
    assert "settings" not in tools["statistics"]  # no configuration() on this tool
    spare = next(w for w in data["widgets"] if w["id"] == "workspace.value.4")
    assert spare["hidden"] is True and spare["bindings"] == []
    styles = {
        key: (b.axis, b.color)
        for key, b in window.workspace_docks.docks["workspace.plot.1"].multi_plot.bindings.items()  # type: ignore[union-attr]
    }
    window.close()

    restarted = make_window(qtbot)
    open_simulator(qtbot, restarted)
    monkeypatch.setattr(QFileDialog, "getOpenFileName", lambda *args: (str(path), ""))
    trigger(restarted, "Open workspace...")
    again = restarted.workspace_manager
    assert again.path == path and again.name == "Bench layout"
    wait_for_layout(qtbot, restarted)
    after = again.capture()
    assert without_layout(after) == without_layout(before)
    assert structure(after.layout) == structure(before.layout)
    assert weights(after.layout) == pytest.approx(weights(before.layout), abs=0.05)
    docks = restarted.workspace_docks.docks
    plot_dock = docks["workspace.plot.1"]
    assert plot_dock.multi_plot is not None and not plot_dock.pending
    assert {key: (b.axis, b.color) for key, b in plot_dock.multi_plot.bindings.items()} == styles
    assert not plot_dock.multi_plot.curves["process.temperature"].isVisible()
    assert docks["workspace.value.2"].binding is not None
    assert docks["workspace.value.2"].binding[0] == "process.status"
    assert docks["workspace.value.4"].isHidden()
    assert (
        restarted.dockWidgetArea(docks["workspace.plot.1"]) == Qt.DockWidgetArea.RightDockWidgetArea
    )
    assert docks["workspace.plot.3"] in restarted.tabifiedDockWidgets(plot_dock)
    probe = restarted.analysis_docks.docks["probe"]
    assert isinstance(probe.widget(), ProbeTool)
    assert probe.widget().settings == {"bins": 25, "signal": "process.pressure"}  # type: ignore[union-attr]
    assert restarted.dockWidgetArea(probe) == Qt.DockWidgetArea.BottomDockWidgetArea
    restarted.close()


def test_missing_signals_are_placeholders_that_resolve_later_and_can_be_rebound(qtbot, tmp_path):
    path = tmp_path / "bench.json"
    window = make_window(qtbot)
    open_simulator(qtbot, window)
    plot = window.workspace_docks.add("plot")
    add_trace(window, plot, "process.pressure")
    add_trace(window, plot, "process.temperature")
    value = window.workspace_docks.add("value")
    bind_value(window, value, "process.temperature")
    assert window.workspace_manager.save_as(path)
    assert plot.multi_plot is not None
    colors = {key: b.color for key, b in plot.multi_plot.bindings.items()}
    window.close()

    empty = make_window(qtbot)
    assert empty.workspace_manager.open_file(path)
    assert "2 signal binding(s) are missing" not in empty.workspace_manager.last_message
    assert "3 signal binding(s) are missing" in empty.workspace_manager.last_message
    plot = empty.workspace_docks.docks["workspace.plot.1"]
    value = empty.workspace_docks.docks["workspace.value.2"]
    assert "Missing signal process.pressure - bind an available signal." in plot.feedback.text()
    assert "Missing signal process.temperature" in plot.feedback.text()
    assert [plot.trace_list.itemText(i) for i in range(plot.trace_list.count())] == [
        "Missing: process.pressure",
        "Missing: process.temperature",
    ]
    assert value.feedback.text() == (
        "Missing signal process.temperature - bind an available signal."
    )
    assert value.value_widget is not None
    assert value.value_widget.state_label.text() == "[NO DATA]"
    # Placeholders are saved back unchanged.
    assert_same(empty.workspace_manager.capture(), read_workspace(path))

    # A different, unrelated source leaves them unresolved; the user can rebind one.
    open_csv_demo(qtbot, empty)
    assert len(plot.pending) == 2 and value.binding is None
    plot.trace_list.setCurrentIndex(plot.trace_list.findData("process.temperature"))
    empty.left_panel.signal_browser.select_id("csv.column.2", emit=True)
    press(plot, "Replace trace with selected signal")
    assert plot.multi_plot is not None and list(plot.multi_plot.bindings) == ["csv.column.2"]
    assert plot.multi_plot.bindings["csv.column.2"].color == colors["process.temperature"]
    assert [t.signal for t in plot.pending] == ["process.pressure"]
    bind_value(empty, value, "csv.column.3")
    assert value.pending == [] and "Missing" not in value.feedback.text()
    rebound = empty.workspace_manager.capture()
    by_id = {w.id: w for w in rebound.widgets}
    assert [b.signal for b in by_id["workspace.plot.1"].bindings] == [
        "process.pressure",
        "csv.column.2",
    ]
    assert [b.signal for b in by_id["workspace.value.2"].bindings] == ["csv.column.3"]
    empty.close()

    # Opened before any session, the bindings resolve once matching IDs arrive.
    later = make_window(qtbot)
    assert later.workspace_manager.open_file(path)
    plot = later.workspace_docks.docks["workspace.plot.1"]
    assert len(plot.pending) == 2
    open_simulator(qtbot, later)
    assert plot.pending == [] and "Missing" not in plot.feedback.text()
    assert plot.multi_plot is not None
    assert {key: b.color for key, b in plot.multi_plot.bindings.items()} == colors
    value = later.workspace_docks.docks["workspace.value.2"]
    assert value.binding is not None and value.binding[0] == "process.temperature"
    assert "Missing" not in value.feedback.text()
    later.close()


@pytest.mark.parametrize(
    ("content", "expected"),
    [
        (b'{"format": "lbr-dashboard-workspace", "version": 2, "name": ', "invalid JSON"),
        (b"\x00\xff garbage", "not UTF-8"),
        (
            json.dumps(
                {
                    "format": "lbr-dashboard-workspace",
                    "version": CURRENT_VERSION + 1,
                    "name": "From the future",
                    "widgets": [],
                    "layout": {},
                }
            ).encode(),
            "newer than this dashboard supports",
        ),
        (
            json.dumps(
                {
                    "format": "lbr-dashboard-workspace",
                    "version": 2,
                    "name": "Leaky",
                    "widgets": [{"id": "p", "type": "time_plot", "title": "C:\\Users\\me"}],
                    "layout": {"kind": "widget", "widget": "p"},
                }
            ).encode(),
            "workspaces must be portable",
        ),
    ],
)
def test_corrupt_newer_or_unportable_files_are_rejected_and_never_overwritten(
    qtbot, tmp_path, content, expected
):
    path = tmp_path / "bad.json"
    path.write_bytes(content)
    current = tmp_path / "current.json"
    window = make_window(qtbot)
    manager = window.workspace_manager
    manager.open_demo()
    assert manager.save_as(current)
    before = manager.capture()
    for action in (manager.open_file, manager.import_file):
        assert not action(path)
        assert manager.message_box is not None and manager.message_box.isVisible()
        assert "The current workspace is unchanged." in manager.last_message
        assert expected in manager.last_message
        assert_same(manager.capture(), before)
        assert manager.path == current
    assert manager.save()  # saves to the current file, never to the rejected one
    assert path.read_bytes() == content
    if expected.startswith("newer") or "newer" in expected:
        # Even chosen explicitly, a newer dashboard's file is not replaced.
        assert not manager.save_as(path)
        assert "newer dashboard version" in manager.last_message
        assert path.read_bytes() == content
    window.close()


def test_restart_reopens_the_last_workspace_and_reports_failures_without_blocking(qtbot, tmp_path):
    preferences = tmp_path / "preferences.json"
    path = tmp_path / "layouts" / "mine.json"
    path.parent.mkdir()
    window = make_window(qtbot, PreferencesStore(preferences))
    window.workspace_manager.open_demo()
    window.workspace_docks.add("plot", title="Extra plot")
    assert window.workspace_manager.save_as(path)
    stored = PreferencesStore(preferences).reload().preferences
    assert stored.extra[LAST_WORKSPACE] == str(path.resolve())
    assert stored.theme == "system"  # workspaces never carry or change the theme
    wait_for_layout(qtbot, window)
    expected = window.workspace_manager.capture()
    assert weights(expected.layout)  # the demo's 3:1 plot/value split is kept
    window.close()

    restarted = make_window(qtbot, PreferencesStore(preferences))
    manager = restarted.workspace_manager
    assert manager.path == path.resolve() and manager.name == "Demo overview"
    # Restored before the window was shown; sizes are applied again after show.
    wait_for_layout(qtbot, restarted)
    assert structure(manager.capture().layout) == structure(expected.layout)
    assert weights(manager.capture().layout) == pytest.approx(weights(expected.layout), abs=0.05)
    assert without_layout(manager.capture()) == without_layout(expected)
    assert "Extra plot" in [d.windowTitle() for d in restarted.workspace_docks.docks.values()]
    restarted.close()

    # A damaged file with a good backup is recovered and the user is told.
    assert manager is not None
    window = make_window(qtbot, PreferencesStore(preferences))
    assert window.workspace_manager.save()
    assert backup_path(path).exists()
    window.close()
    path.write_bytes(b"{ truncated")
    recovered = make_window(qtbot, PreferencesStore(preferences))
    assert "was damaged, so its backup was opened" in recovered.workspace_manager.last_message
    assert recovered.workspace_manager.name == "Demo overview"
    assert path.read_bytes() == b"{ truncated"
    recovered.close()

    # Without the file the dashboard still starts, empty, and says why.
    path.unlink()
    backup_path(path).unlink()
    missing = make_window(qtbot, PreferencesStore(preferences))
    manager = missing.workspace_manager
    assert "was not found" in manager.last_message
    assert manager.message_box is not None and manager.message_box.isVisible()
    assert missing.workspace_docks.docks == {} and manager.path is None
    assert missing.isVisible()
    missing.close()

    # A new workspace forgets the last file.
    path.write_text("[]")
    broken = make_window(qtbot, PreferencesStore(preferences))
    assert "Could not open mine.json" in broken.workspace_manager.last_message
    trigger(broken, "New workspace")
    assert LAST_WORKSPACE not in PreferencesStore(preferences).reload().preferences.extra
    broken.close()


def test_import_previews_bindings_and_can_rename_or_cancel(qtbot, tmp_path):
    shared = tmp_path / "shared.json"
    window = make_window(qtbot)
    window.workspace_manager.open_demo()
    assert window.workspace_manager.export(shared)
    assert window.workspace_manager.path is None
    window.close()

    window = make_window(qtbot)
    open_simulator(qtbot, window)
    manager = window.workspace_manager
    seen: list[str] = []

    def cancel(dialog: ImportPreviewDialog) -> bool:
        seen.append(dialog.summary.toPlainText())
        return False

    manager.confirm_import = cancel
    assert not manager.import_file(shared)
    assert window.workspace_docks.docks == {} and "Import cancelled" in manager.last_message
    summary = seen[0]
    assert "Workspace: Demo overview" in summary
    assert "process.pressure: available" in summary
    assert "csv.column.1: missing - shown as a placeholder you can rebind" in summary
    assert "3 of 9 signal binding(s) resolve against the current session." in summary
    assert "Analysis tool statistics" in summary

    def accept(dialog: ImportPreviewDialog) -> bool:
        dialog.name.setText("Shared with me")
        return True

    manager.confirm_import = accept
    monkeypatch_dialog = QFileDialog.getOpenFileName
    try:
        QFileDialog.getOpenFileName = staticmethod(lambda *args: (str(shared), ""))  # type: ignore[method-assign]
        trigger(window, "Import workspace...")
    finally:
        QFileDialog.getOpenFileName = monkeypatch_dialog  # type: ignore[method-assign]
    assert manager.name == "Shared with me" and manager.path is None
    assert "Save it to keep it" in manager.last_message
    assert window.workspace_docks.docks["process-plot"].pending == []
    window.close()


def test_new_rename_duplicate_and_export_keep_the_original_file(qtbot, tmp_path, monkeypatch):
    path = tmp_path / "original.json"
    copy = tmp_path / "copy.json"
    window = make_window(qtbot)
    manager = window.workspace_manager
    assert not manager.save()  # nothing to save yet
    assert "Add at least one widget" in manager.last_message
    window.workspace_docks.add("value")
    assert manager.save_as(path)
    original = path.read_bytes()
    monkeypatch.setattr(QInputDialog, "getText", lambda *args, **kwargs: ("   ", True))
    trigger(window, "Rename workspace...")
    assert manager.name == "Untitled workspace" and "printable" in manager.last_message
    monkeypatch.setattr(QInputDialog, "getText", lambda *args, **kwargs: ("Pit wall", True))
    trigger(window, "Rename workspace...")
    assert manager.name == "Pit wall"
    trigger(window, "Duplicate workspace")
    assert manager.name == "Pit wall copy" and manager.path is None
    assert "Pit wall copy (not saved)" in manager.title_action.text()
    monkeypatch.setattr(QFileDialog, "getSaveFileName", lambda *args: (str(copy), ""))
    trigger(window, "Save workspace")  # unsaved, so it asks for a file
    assert manager.path == copy and read_workspace(copy).name == "Pit wall copy"
    assert path.read_bytes() == original
    exported = tmp_path / "exported"
    monkeypatch.setattr(QFileDialog, "getSaveFileName", lambda *args: (str(exported), ""))
    trigger(window, "Export workspace...")
    assert read_workspace(exported.with_suffix(".json")).name == "Pit wall copy"
    assert manager.path == copy
    trigger(window, "New workspace")
    assert window.workspace_docks.docks == {} and manager.name == "Untitled workspace"
    assert manager.path is None
    window.close()


def test_unknown_widgets_and_tools_are_kept_as_placeholders(qtbot, tmp_path):
    path = tmp_path / "mixed.json"
    document = json.loads((SAMPLES / "workspaces" / "mixed_status.json").read_text())
    document["widgets"].append(
        {"id": "future-tool", "type": "analysis_tool", "tool": "orbit", "settings": {"a": 1}}
    )
    document["widgets"].append(
        {"id": "probe", "type": "analysis_tool", "tool": "probe", "settings": {"bins": "x"}}
    )
    document["layout"]["children"].append({"kind": "widget", "widget": "future-tool"})
    document["layout"]["children"].append({"kind": "widget", "widget": "probe"})
    document["layout"]["weights"].extend([1.0, 1.0])
    path.write_text(json.dumps(document))
    window = make_window(qtbot)
    manager = window.workspace_manager
    assert manager.open_file(path)
    assert set(manager.placeholders) == {"status-timeline", "raw", "future-tool"}
    # A tool that rejects its saved settings still opens, with its defaults.
    assert "Probe: saved settings were not restored (bins must be an integer)" in (
        manager.last_message
    )
    probe = window.analysis_docks.docks["probe"].widget()
    assert isinstance(probe, ProbeTool) and probe.settings["bins"] == 10
    timeline = manager.placeholders["status-timeline"]
    assert isinstance(timeline, PlaceholderDock)
    assert "not available in this version yet" in timeline.message.text()
    assert "orbit" in manager.placeholders["future-tool"].message.text()
    assert "status-values" in window.workspace_docks.docks
    saved = manager.capture()
    original = read_workspace(path)
    by_id = {w.id: w for w in saved.widgets}
    for widget in original.widgets:
        if widget.type in ("event_timeline", "raw_table") or widget.id == "future-tool":
            assert by_id[widget.id].bindings == widget.bindings
            assert by_id[widget.id].settings == widget.settings
    assert [b.signal for b in by_id["status-values"].bindings] == ["engine_rpm", "button_mode"]
    assert saved.extensions == original.extensions
    window.close()


def test_placeholder_traces_can_be_moved_removed_and_bound_traces_replaced(qtbot, tmp_path):
    window = make_window(qtbot)
    manager = window.workspace_manager
    manager.open_demo()
    plot = window.workspace_docks.docks["csv-plot"]
    plot.trace_list.setCurrentIndex(plot.trace_list.findData("csv.column.1"))
    plot.axis_choice.setCurrentIndex(2)
    press(plot, "Assign trace axis")
    assert plot.pending[0].axis == "right"
    plot.trace_list.setCurrentIndex(plot.trace_list.findData("csv.column.2"))
    press(plot, "Remove selected trace")
    assert [t.signal for t in plot.pending] == ["csv.column.1"]
    widget = next(w for w in manager.capture().widgets if w.id == "csv-plot")
    assert [(b.signal, b.axis) for b in widget.bindings] == [("csv.column.1", "second")]

    open_simulator(qtbot, window)
    process = window.workspace_docks.docks["process-plot"]
    assert process.multi_plot is not None
    color = process.multi_plot.bindings["process.temperature"].color
    process.trace_list.setCurrentIndex(process.trace_list.findData("process.temperature"))
    window.left_panel.signal_browser.select_id("process.status", emit=True)
    press(process, "Replace trace with selected signal")  # a status is not plottable
    assert list(process.multi_plot.bindings) == ["process.pressure", "process.temperature"]
    assert "scalar numeric or boolean" in process.feedback.text()
    open_simulator(qtbot, window, "motion")
    # The old traces are now missing; replacing one keeps its style.
    process.trace_list.setCurrentIndex(process.trace_list.findData("process.temperature"))
    window.left_panel.signal_browser.select_id("motion.speed", emit=True)
    press(process, "Replace trace with selected signal")
    assert list(process.multi_plot.bindings) == ["process.pressure", "motion.speed"]
    assert process.multi_plot.bindings["motion.speed"].color == color
    assert process.multi_plot.bindings["motion.speed"].axis == "right"
    assert "Missing signal: process.pressure" in process.feedback.text()
    process.trace_list.setCurrentIndex(process.trace_list.findData("process.pressure"))
    press(process, "Remove selected trace")
    assert list(process.multi_plot.bindings) == ["motion.speed"]
    assert process.binding is not None and process.binding[0] == "motion.speed"
    assert "Missing" not in process.feedback.text()
    window.close()


def test_every_analysis_tool_reopens_with_its_configuration(qtbot, tmp_path):
    path = tmp_path / "tools.json"
    window = make_window(qtbot)
    open_simulator(qtbot, window)
    analysis = window.analysis_docks
    configured: dict[str, Any] = {}
    for tool in TOOLS:
        widget = analysis.open(tool.key).widget()
        if tool.key == "histogram":
            widget.restore_configuration({"bins": 17})  # type: ignore[union-attr]
        if hasattr(widget, "configuration"):
            configured[tool.key] = widget.configuration()  # type: ignore[union-attr]
    assert configured["histogram"] == {"bins": 17}
    assert window.workspace_manager.save_as(path), window.workspace_manager.last_message
    window.close()

    restarted = make_window(qtbot)
    open_simulator(qtbot, restarted)
    assert restarted.workspace_manager.open_file(path)
    docks = restarted.analysis_docks.docks
    assert set(docks) == {tool.key for tool in TOOLS}
    for key, settings in configured.items():
        assert not docks[key].isHidden()
        assert docks[key].widget().configuration() == settings  # type: ignore[union-attr]
    restarted.close()
