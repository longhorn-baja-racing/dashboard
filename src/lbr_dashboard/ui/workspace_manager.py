"""The Workspace menu: create, save, open, import and export portable workspaces.

A workspace captures the plot and value docks with their signal IDs and trace
styles, the open analysis tools with their own configuration, and how all of
those docks are arranged. It never holds telemetry, file paths, source
locations or the theme. Opening a file validates it completely before
anything in the window changes, so a bad file leaves the current workspace
as it was.
"""

from __future__ import annotations

import json
import logging
from collections.abc import Callable, Iterable, Mapping
from dataclasses import replace
from pathlib import Path
from typing import Any

from PySide6.QtCore import QObject, Qt, Signal
from PySide6.QtWidgets import (
    QDialog,
    QDialogButtonBox,
    QDockWidget,
    QFileDialog,
    QFormLayout,
    QInputDialog,
    QLabel,
    QLineEdit,
    QMainWindow,
    QMessageBox,
    QPlainTextEdit,
    QVBoxLayout,
    QWidget,
)

from ..workspace import (
    AxisPreference,
    SignalBinding,
    WidgetConfig,
    WidgetRef,
    Workspace,
    WorkspaceError,
    WorkspaceIssue,
    demo_workspace,
    dumps_workspace,
    load_workspace,
    read_workspace,
    resolve_bindings,
    save_workspace,
)
from ..workspace.model import thaw_json
from .analysis import AnalysisDocks
from .docks import PendingTrace, SignalDock, WorkspaceDocks
from .workspace_layout import AREAS, DockPlacement, capture_layout, place_layout

logger = logging.getLogger(__name__)

UNTITLED = "Untitled workspace"
FILE_FILTER = "Dashboard workspaces (*.json);;All files (*)"
MAX_NAME_LENGTH = 200
TYPE_NAMES = {
    "time_plot": "Plot",
    "value_cards": "Value",
    "event_timeline": "Event timeline",
    "raw_table": "Raw table",
    "analysis_tool": "Analysis tool",
}


def describe_error(error: WorkspaceError, limit: int = 6) -> str:
    """Readable list of a workspace's problems for a message box."""

    lines = []
    for issue in error.issues[:limit]:
        where = "" if issue.location in ("$", "") else f" ({issue.location})"
        lines.append(f"- {issue.message}{where}")
    if len(error.issues) > limit:
        lines.append(f"- and {len(error.issues) - limit} more problems")
    return "\n".join(lines)


def _valid_name(name: str) -> str | None:
    text = name.strip()
    if not text or len(text) > MAX_NAME_LENGTH or not text.isprintable():
        return None
    return text


class PlaceholderDock(QDockWidget):
    """Stands in for a saved widget this build cannot show, and keeps it for saving."""

    def __init__(self, config: WidgetConfig, reason: str, parent: QMainWindow) -> None:
        super().__init__(config.title or config.id, parent)
        self.setObjectName(f"placeholder.{config.id}")
        self.config = config
        body = QWidget()
        layout = QVBoxLayout(body)
        self.message = QLabel(reason + " It is kept in the workspace when you save.")
        self.message.setWordWrap(True)
        self.message.setTextFormat(Qt.TextFormat.PlainText)
        layout.addWidget(self.message)
        layout.addStretch()
        self.setWidget(body)


class ImportPreviewDialog(QDialog):
    """Shows what an import will restore before anything changes."""

    def __init__(self, workspace: Workspace, summary: str, parent: QWidget | None) -> None:
        super().__init__(parent)
        self.setWindowTitle("Import workspace")
        self.workspace = workspace
        self.name = QLineEdit(workspace.name)
        self.name.setAccessibleName("Workspace name after import")
        self.summary = QPlainTextEdit(summary)
        self.summary.setReadOnly(True)
        self.summary.setAccessibleName("What the import restores")
        self.buttons = QDialogButtonBox()
        self.replace_button = self.buttons.addButton(
            "Replace current workspace", QDialogButtonBox.ButtonRole.AcceptRole
        )
        self.buttons.addButton(QDialogButtonBox.StandardButton.Cancel)
        self.buttons.accepted.connect(self.accept)
        self.buttons.rejected.connect(self.reject)
        form = QFormLayout()
        form.addRow("Name", self.name)
        layout = QVBoxLayout(self)
        layout.addLayout(form)
        layout.addWidget(self.summary)
        layout.addWidget(self.buttons)
        self.resize(560, 420)


class WorkspaceManager(QObject):
    """Own the current workspace's name and file, and the Workspace menu actions."""

    #: The file the current workspace is saved in, as a string, or None when
    #: it has not been saved (new, imported, duplicated or a preset).
    path_changed = Signal(object)

    def __init__(self, window: QMainWindow, docks: WorkspaceDocks, analysis: AnalysisDocks) -> None:
        super().__init__(window)
        self.window = window
        self.docks = docks
        self.analysis = analysis
        self.name = UNTITLED
        self.path: Path | None = None
        self.description: str | None = None
        self.extensions: Mapping[str, Any] = {}
        self.placeholders: dict[str, PlaceholderDock] = {}
        self.analysis_ids: dict[str, str] = {}
        self.message_box: QMessageBox | None = None
        self.last_message = ""
        #: Asks the user to confirm an import; replaced in tests.
        self.confirm_import: Callable[[ImportPreviewDialog], bool] = lambda dialog: (
            dialog.exec() == QDialog.DialogCode.Accepted
        )
        docks.resolved_callbacks.append(self._show_resolved)
        menu = docks.menu
        self.title_action = menu.addAction("")
        self.title_action.setEnabled(False)
        menu.addAction("New workspace", lambda: self.new())
        menu.addAction("Open workspace...", lambda: self.open_file())
        menu.addAction("Open demo workspace", lambda: self.open_demo())
        menu.addAction("Save workspace", lambda: self.save())
        menu.addAction("Save workspace as...", lambda: self.save_as())
        menu.addAction("Rename workspace...", lambda: self.rename())
        menu.addAction("Duplicate workspace", lambda: self.duplicate())
        menu.addAction("Import workspace...", lambda: self.import_file())
        menu.addAction("Export workspace...", lambda: self.export())
        menu.addSeparator()
        self._update_title()

    # Messages ------------------------------------------------------------------

    def notify(self, text: str, *, problem: bool = False, details: str = "") -> None:
        """Explain an outcome without blocking: status bar, plus a box for problems."""

        self.last_message = text + ("\n" + details if details else "")
        self.window.statusBar().showMessage(text)
        if not problem:
            return
        logger.warning("%s %s", text, details)
        if self.message_box is not None:
            self.message_box.close()
            self.message_box.deleteLater()
        box = QMessageBox(QMessageBox.Icon.Warning, "Workspace", text, parent=self.window)
        if details:
            box.setInformativeText(details)
        box.setModal(False)
        box.show()
        self.message_box = box

    def _update_title(self) -> None:
        where = self.path.name if self.path is not None else "not saved"
        self.title_action.setText(f"Workspace: {self.name} ({where})")

    def _set_path(self, path: Path | None) -> None:
        self.path = path
        self._update_title()
        self.path_changed.emit(str(path) if path is not None else None)

    # Capture -------------------------------------------------------------------

    def _managed(self) -> dict[str, QDockWidget]:
        managed: dict[str, QDockWidget] = dict(self.docks.docks)
        for key, dock in self.analysis.docks.items():
            if not dock.isHidden() or dock.isFloating():
                managed[self.analysis_ids.get(key, f"analysis.{key}")] = dock
        managed.update(self.placeholders)
        return managed

    def capture(self) -> Workspace:
        """Describe the window as a workspace; raises WorkspaceError if it cannot."""

        managed = self._managed()
        if not managed:
            raise WorkspaceError(
                [WorkspaceIssue("$", "empty", "Add at least one widget before saving.")]
            )
        defaults = {
            key: "bottom" if not isinstance(dock, SignalDock | PlaceholderDock) else "right"
            for key, dock in managed.items()
        }
        captured = capture_layout(self.window, managed, defaults)
        dropped: list[str] = []
        widgets = []
        for key, dock in managed.items():
            placement = captured.placements[key]
            if isinstance(dock, SignalDock):
                config = self._signal_widget(key, dock)
            elif isinstance(dock, PlaceholderDock):
                config = dock.config
            else:
                config = self._tool_widget(key, dock, dropped)
            widgets.append(
                replace(
                    config,
                    area=placement.area,
                    hidden=placement.hidden,
                    floating=placement.floating,
                )
            )
        workspace = Workspace(
            name=self.name,
            description=self.description,
            widgets=tuple(widgets),
            layout=captured.layout,
            extensions=self.extensions,
        )
        dumps_workspace(workspace)  # Validate now so callers report problems early.
        if dropped:
            self.notify("Some analysis tool settings could not be saved: " + ", ".join(dropped))
        return workspace

    def _signal_widget(self, key: str, dock: SignalDock) -> WidgetConfig:
        title = dock.windowTitle() or None
        labels: dict[str, str] = dict(dock.preserved.get("labels", {}))
        if dock.multi_plot is None:
            bindings: list[SignalBinding] = []
            if dock.binding is not None:
                bindings.append(SignalBinding(dock.binding[0], labels.get(dock.binding[0])))
            elif dock.pending:
                signal = dock.pending[0].signal
                bindings.append(SignalBinding(signal, labels.get(signal)))
            extra: tuple[SignalBinding, ...] = dock.preserved.get("extra", ())
            bindings.extend(b for b in extra if all(b.signal != o.signal for o in bindings))
            return WidgetConfig(key, "value_cards", title, tuple(bindings))

        prefs: dict[str, AxisPreference] = dict(dock.preserved.get("axes", {}))
        traces: list[tuple[str, str | None, str | None, bool, str | None]] = []
        for binding in dock.multi_plot.bindings.values():
            curve = dock.multi_plot.curves.get(binding.signal_id)
            visible = curve.isVisible() if curve is not None else True
            traces.append((binding.signal_id, binding.axis, binding.color, visible, binding.unit))
        for trace in dock.pending:
            traces.append((trace.signal, trace.axis, trace.color, trace.visible, None))
        order: list[str] = list(dock.preserved.get("order", ()))
        traces.sort(key=lambda t: order.index(t[0]) if t[0] in order else len(order))

        axes: list[AxisPreference] = []
        axis_ids: dict[str, str] = {}
        for side in ("left", "right"):
            on_side = [t for t in traces if t[1] == side]
            if not on_side:
                continue
            pref = prefs.get(side)
            axis_id = pref.id if pref is not None else side
            if axis_id in axis_ids.values():
                axis_id = f"{side}-axis"
            unit = next((t[4] for t in on_side if t[4]), None)
            if unit is None and pref is not None:
                unit = pref.unit
            axis_ids[side] = axis_id
            axes.append(
                AxisPreference(
                    axis_id,
                    side,
                    pref.label if pref is not None else None,
                    unit,
                    pref.range if pref is not None else None,
                )
            )
        bindings = [
            SignalBinding(
                signal,
                labels.get(signal),
                color,
                axis_ids.get(side) if side is not None else None,
                hidden=not visible,
            )
            for signal, side, color, visible, _unit in traces
        ]
        return WidgetConfig(key, "time_plot", title, tuple(bindings), tuple(axes))

    def _tool_widget(self, key: str, dock: QDockWidget, dropped: list[str]) -> WidgetConfig:
        tool = next(name for name, item in self.analysis.docks.items() if item is dock)
        settings = None
        widget = dock.widget()
        configuration = getattr(widget, "configuration", None)
        if callable(configuration):
            try:
                value = json.loads(json.dumps(configuration(), allow_nan=False))
            except Exception:  # Tool code is outside this module's control.
                logger.exception("Could not read the %s tool configuration", tool)
                value = None
            settings = value if isinstance(value, dict) else None
            if settings is not None:
                # Tool settings come from tool code; keep only what the format accepts.
                check = WidgetConfig(key, "analysis_tool", tool=tool, settings=settings)
                try:
                    dumps_workspace(Workspace("check", (check,), WidgetRef(key)))
                except WorkspaceError:
                    settings = None
            if settings is None:
                dropped.append(tool)
        return WidgetConfig(
            key, "analysis_tool", dock.windowTitle() or None, tool=tool, settings=settings
        )

    # Restore -------------------------------------------------------------------

    def _clear(self) -> None:
        self.docks.clear()
        for dock in self.analysis.docks.values():
            dock.setFloating(False)
            dock.hide()
        for dock in self.placeholders.values():
            self.window.removeDockWidget(dock)
            dock.hide()
            dock.deleteLater()
        self.placeholders.clear()
        self.analysis_ids.clear()

    def apply(self, workspace: Workspace) -> list[str]:
        """Replace the window's docks with ``workspace``; returns notes for the user."""

        notes: list[str] = []
        self._clear()
        created: dict[str, QDockWidget] = {}
        placements: dict[str, DockPlacement] = {}
        for config in workspace.widgets:
            default = "bottom" if config.type == "analysis_tool" else "right"
            area = config.area or default
            dock = self._create(config, AREAS[area], notes)
            created[config.id] = dock
            placements[config.id] = DockPlacement(area, config.hidden, config.floating)
        place_layout(self.window, workspace.layout, created, placements)
        self.name = workspace.name
        self.description = workspace.description
        self.extensions = dict(workspace.extensions)
        self.docks.update_snapshot(self.docks.snapshot)
        self._update_title()
        return notes

    def _create(
        self, config: WidgetConfig, area: Qt.DockWidgetArea, notes: list[str]
    ) -> QDockWidget:
        if config.type == "time_plot":
            dock = self.docks.add("plot", config.id, config.title, area=area)
            sides = {axis.id: axis.side for axis in config.axes}
            dock.preserved["axes"] = {
                axis.side: axis for axis in reversed(config.axes)
            }  # first axis per side wins
            dock.preserved["labels"] = {b.signal: b.label for b in config.bindings if b.label}
            dock.preserved["order"] = tuple(b.signal for b in config.bindings)
            seen: set[str] = set()
            for binding in config.bindings:
                if binding.signal in seen:
                    continue
                seen.add(binding.signal)
                side = sides.get(binding.axis) if binding.axis is not None else None
                dock.pending.append(
                    PendingTrace(
                        binding.signal,
                        "left" if side == "left" else "right" if side == "right" else None,
                        binding.color,
                        not binding.hidden,
                    )
                )
            return dock
        if config.type == "value_cards":
            dock = self.docks.add("value", config.id, config.title, area=area)
            if config.bindings:
                dock.pending.append(PendingTrace(config.bindings[0].signal))
                dock.preserved["extra"] = config.bindings[1:]
                dock.preserved["labels"] = {b.signal: b.label for b in config.bindings if b.label}
                if len(config.bindings) > 1:
                    notes.append(
                        f"{config.title or config.id} shows {config.bindings[0].signal}; "
                        f"{len(config.bindings) - 1} more binding(s) are kept for saving."
                    )
            return dock
        if config.type == "analysis_tool" and config.tool in self.analysis.tools:
            tool = config.tool
            if tool not in self.analysis_ids:
                tool_dock = self.analysis.open(tool)
                self.analysis_ids[tool] = config.id
                self._restore_tool(tool_dock, config, notes)
                return tool_dock
            reason = f"The {tool} tool is already open in this workspace."
        elif config.type == "analysis_tool":
            reason = f"The analysis tool '{config.tool}' is not available in this version."
        else:
            name = TYPE_NAMES.get(config.type, config.type)
            reason = f"{name} widgets are not available in this version yet."
        notes.append(f"{config.title or config.id}: {reason}")
        placeholder = PlaceholderDock(config, reason, self.window)
        self.window.addDockWidget(area, placeholder)
        self.placeholders[config.id] = placeholder
        return placeholder

    def _restore_tool(self, dock: QDockWidget, config: WidgetConfig, notes: list[str]) -> None:
        restore = getattr(dock.widget(), "restore_configuration", None)
        if config.settings is None or not callable(restore):
            return
        try:
            restore(thaw_json(config.settings))
        except Exception as error:  # Tool code is outside this module's control.
            logger.exception("Could not restore the %s tool configuration", config.tool)
            notes.append(f"{dock.windowTitle()}: saved settings were not restored ({error}).")

    def _show_resolved(self, dock: SignalDock) -> None:
        """Bring a dock that just found its signals to the front of its tab group."""

        def shows_data(mate: QDockWidget) -> bool:
            if not isinstance(mate, SignalDock) or mate.binding is None:
                return False
            descriptor = self.docks.descriptor(mate.binding[0])
            return descriptor is not None and descriptor.source_id == mate.binding[1]

        mates = self.window.tabifiedDockWidgets(dock)
        if mates and not any(shows_data(mate) for mate in mates):
            dock.raise_()

    # Menu actions --------------------------------------------------------------

    def new(self) -> None:
        self._clear()
        self.name, self.description, self.extensions = UNTITLED, None, {}
        self._set_path(None)
        self.notify("Started a new empty workspace. Add widgets from the Workspace menu.")

    def _choose_open(self, title: str) -> Path | None:
        start = str(self.path.parent) if self.path is not None else ""
        file_path, _ = QFileDialog.getOpenFileName(self.window, title, start, FILE_FILTER)
        return Path(file_path) if file_path else None

    def _choose_save(self, title: str) -> Path | None:
        start = str(self.path) if self.path is not None else f"{self.name}.json"
        file_path, _ = QFileDialog.getSaveFileName(self.window, title, start, FILE_FILTER)
        if not file_path:
            return None
        path = Path(file_path)
        return path if path.suffix else path.with_suffix(".json")

    def open_file(self, path: Path | None = None) -> bool:
        """Open a workspace file and make it the current one."""

        path = path or self._choose_open("Open workspace")
        if path is None:
            return False
        try:
            result = load_workspace(path)
        except WorkspaceError as error:
            self.notify(
                f"Could not open {path.name}. The current workspace is unchanged.",
                problem=True,
                details=describe_error(error),
            )
            return False
        notes = self.apply(result.workspace)
        self._set_path(path)
        if result.recovered and result.primary_error is not None:
            self.notify(
                f"{path.name} was damaged, so its backup was opened. "
                "Saving replaces the damaged file.",
                problem=True,
                details=describe_error(result.primary_error),
            )
        else:
            self._report_open(f"Opened workspace {result.workspace.name}.", notes)
        return True

    def _report_open(self, text: str, notes: Iterable[str]) -> None:
        missing = sum(len(dock.pending) for dock in self.docks.docks.values() if dock.pending)
        if missing:
            text += f" {missing} signal binding(s) are missing; bind them in their docks."
        notes = list(notes)
        if notes:
            self.notify(text, problem=True, details="\n".join(notes))
        else:
            self.notify(text)

    def open_demo(self) -> None:
        """Open the bundled demo overview as an unsaved workspace."""

        notes = self.apply(demo_workspace())
        self._set_path(None)
        self._report_open("Opened the demo workspace.", notes)

    def restore_last(self, path_text: str) -> bool:
        """Reopen the workspace from the previous run; never raises."""

        path = Path(path_text)
        try:
            if not path.exists():
                self.notify(
                    f"The last workspace {path.name} was not found, so the dashboard "
                    "started with an empty workspace.",
                    problem=True,
                    details=str(path),
                )
                return False
            return self.open_file(path)
        except Exception as error:  # Startup must continue whatever went wrong.
            logger.exception("Could not restore the last workspace")
            self.notify(
                "The last workspace could not be reopened, so the dashboard started with "
                "an empty workspace.",
                problem=True,
                details=str(error),
            )
            return False

    def _write(self, path: Path, workspace: Workspace) -> bool:
        try:
            save_workspace(path, workspace)
        except WorkspaceError as error:
            self.notify(
                f"Could not save to {path.name}. The file was not changed.",
                problem=True,
                details=describe_error(error),
            )
            return False
        return True

    def _captured(self) -> Workspace | None:
        try:
            return self.capture()
        except WorkspaceError as error:
            self.notify(
                "The workspace could not be saved.", problem=True, details=describe_error(error)
            )
            return None

    def save(self) -> bool:
        if self.path is None:
            return self.save_as()
        workspace = self._captured()
        if workspace is None or not self._write(self.path, workspace):
            return False
        self._set_path(self.path)
        self.notify(f"Saved workspace {self.name} to {self.path.name}.")
        return True

    def save_as(self, path: Path | None = None) -> bool:
        workspace = self._captured()
        if workspace is None:
            return False
        path = path or self._choose_save("Save workspace as")
        if path is None or not self._write(path, workspace):
            return False
        self._set_path(path)
        self.notify(f"Saved workspace {self.name} to {path.name}.")
        return True

    def export(self, path: Path | None = None) -> bool:
        """Write a portable copy without changing which file is current."""

        workspace = self._captured()
        if workspace is None:
            return False
        path = path or self._choose_save("Export workspace")
        if path is None or not self._write(path, workspace):
            return False
        self.notify(f"Exported workspace {self.name} to {path.name}.")
        return True

    def rename(self, name: str | None = None) -> bool:
        if name is None:
            text, accepted = QInputDialog.getText(
                self.window, "Rename workspace", "Workspace name:", text=self.name
            )
            if not accepted:
                return False
            name = text
        valid = _valid_name(name)
        if valid is None:
            self.notify(
                f"A workspace name needs 1 to {MAX_NAME_LENGTH} printable characters.",
                problem=True,
            )
            return False
        self.name = valid
        self._update_title()
        self.notify(f"Renamed the workspace to {valid}. Save to keep the new name.")
        return True

    def duplicate(self) -> None:
        """Continue with an unsaved copy, so saving never touches the original."""

        self.name = _valid_name(f"{self.name} copy"[:MAX_NAME_LENGTH]) or UNTITLED
        self._set_path(None)
        self.notify(f"Duplicated as {self.name}. Save it to keep the copy.")

    def preview(self, workspace: Workspace) -> str:
        """Describe what ``workspace`` restores and which bindings resolve now."""

        snapshot = self.docks.snapshot
        available = [d.identifier for d in snapshot.schema.signals] if snapshot else []
        resolution = resolve_bindings(workspace, available)
        missing = {(item.widget_id, item.binding_index) for item in resolution.unresolved}
        lines = [f"Workspace: {workspace.name}"]
        if workspace.description:
            lines.append(workspace.description)
        lines.append("")
        lines.append(f"Widgets ({len(workspace.widgets)}):")
        for widget in workspace.widgets:
            name = TYPE_NAMES.get(widget.type, widget.type)
            title = f' "{widget.title}"' if widget.title else ""
            if widget.type == "analysis_tool":
                known = widget.tool in self.analysis.tools
                state = "" if known else " - not available in this version, kept as placeholder"
                lines.append(f"- {name} {widget.tool}{title}{state}")
                continue
            if widget.type in ("event_timeline", "raw_table"):
                lines.append(f"- {name}{title}: not available yet, kept as a placeholder")
                continue
            lines.append(f"- {name}{title} [{widget.id}]")
            for index, binding in enumerate(widget.bindings):
                state = (
                    "missing - shown as a placeholder you can rebind"
                    if (widget.id, index) in missing
                    else "available"
                )
                lines.append(f"    {binding.signal}: {state}")
        total = len(resolution.resolved) + len(resolution.unresolved)
        lines.append("")
        if snapshot is None:
            lines.append(
                f"No session is open, so all {total} signal binding(s) stay unresolved "
                "until a session with those signal IDs is loaded."
            )
        else:
            lines.append(
                f"{len(resolution.resolved)} of {total} signal binding(s) resolve against "
                "the current session."
            )
        lines.append("Importing replaces the current docks. Theme and sources are not changed.")
        return "\n".join(lines)

    def import_file(self, path: Path | None = None) -> bool:
        """Preview a shared workspace, then optionally replace the current one."""

        path = path or self._choose_open("Import workspace")
        if path is None:
            return False
        try:
            workspace = read_workspace(path)
        except WorkspaceError as error:
            self.notify(
                f"Could not import {path.name}. The current workspace is unchanged.",
                problem=True,
                details=describe_error(error),
            )
            return False
        dialog = ImportPreviewDialog(workspace, self.preview(workspace), self.window)
        try:
            if not self.confirm_import(dialog):
                self.notify("Import cancelled. The current workspace is unchanged.")
                return False
            name = _valid_name(dialog.name.text()) or workspace.name
        finally:
            dialog.deleteLater()
        notes = self.apply(replace(workspace, name=name))
        self._set_path(None)
        self._report_open(f"Imported workspace {name}. Save it to keep it.", notes)
        return True
