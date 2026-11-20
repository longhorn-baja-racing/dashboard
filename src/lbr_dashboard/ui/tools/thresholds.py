"""Thresholds tool: user-defined alarm rules on numeric signals, evaluated read-only.

Rules are evaluated with `core.thresholds` over the shared session snapshot. In
replay the state is looked up at the replay cursor; while the clock follows a
live source it is looked up at the newest data. Nothing here sends anything to
a source: the tool only reads the snapshot and draws on the main plot.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import pyqtgraph as pg
from PySide6.QtCore import QSignalBlocker, Qt, QTimer
from PySide6.QtGui import QBrush, QColor
from PySide6.QtWidgets import (
    QAbstractItemView,
    QComboBox,
    QDoubleSpinBox,
    QFileDialog,
    QFormLayout,
    QFrame,
    QGroupBox,
    QHBoxLayout,
    QHeaderView,
    QLabel,
    QLineEdit,
    QPushButton,
    QScrollArea,
    QStyle,
    QTableWidget,
    QTableWidgetItem,
    QVBoxLayout,
    QWidget,
)

from ...core import SessionSnapshot
from ...core.statistics import format_number, format_seconds
from ...core.thresholds import (
    MAX_THRESHOLD_FILE_CHARS,
    REASON_STALE,
    SEVERITIES,
    AlarmState,
    AlarmTransition,
    ThresholdFileError,
    ThresholdRule,
    ThresholdRuleError,
    acknowledge,
    evaluate,
    rules_from_json,
    rules_to_json,
    transition_at,
)
from ..analysis import AnalysisContext, AnalysisTool, is_numeric

NORMAL = "normal"
ACTIVE = "active"
UNKNOWN = "unknown"
STALE = "stale"

NS_PER_S = 1_000_000_000
FILE_FILTER = "Threshold rules (*.json);;All files (*)"
COLUMNS = ("State", "Since", "Rule", "Signal", "Severity", "Details")

# Text marker, standard icon and cell colors per state. The marker and the
# words carry the meaning; color only repeats it.
_MARKERS = {ACTIVE: "[!]", NORMAL: "[ok]", UNKNOWN: "[?]", STALE: "[~]"}
_ICONS = {
    ACTIVE: QStyle.StandardPixmap.SP_MessageBoxCritical,
    NORMAL: QStyle.StandardPixmap.SP_DialogApplyButton,
    UNKNOWN: QStyle.StandardPixmap.SP_MessageBoxQuestion,
    STALE: QStyle.StandardPixmap.SP_MessageBoxWarning,
}
_COLORS = {ACTIVE: "#f4c7c3", NORMAL: "#cfe8d3", UNKNOWN: "#dddddd", STALE: "#f6e2b3"}
_SEVERITY_PENS = {"info": "#3b7dd8", "warning": "#d08a00", "critical": "#d62728"}


@dataclass(frozen=True)
class RuleStatus:
    """What one rule shows at the evaluation time."""

    rule: ThresholdRule
    state: str
    since_ns: int | None
    detail: str
    activation_ns: int | None = None
    acknowledged: bool = False

    @property
    def text(self) -> str:
        """State in words with a shape marker, e.g. ``[!] ACTIVE - not acknowledged``."""

        marker = _MARKERS[self.state]
        if self.state == ACTIVE:
            ack = "acknowledged" if self.acknowledged else "not acknowledged"
            return f"{marker} ACTIVE - {ack}"
        return f"{marker} {self.state.capitalize()}"


def rule_name(rule: ThresholdRule) -> str:
    return rule.label or rule.rule_id


def rule_status(
    rule: ThresholdRule,
    transitions: tuple[AlarmTransition, ...],
    time_ns: int,
    acknowledged: frozenset[tuple[str, int]],
) -> RuleStatus:
    """Classify the transition in effect at ``time_ns``; no data is UNKNOWN, never NORMAL."""

    unit = rule.unit
    current = transition_at(transitions, time_ns)
    if current is None:
        return RuleStatus(rule, UNKNOWN, None, "no valid data at or before this time")
    if current.state is AlarmState.ACTIVE:
        value = format_number(current.value, unit) if current.value is not None else "--"
        return RuleStatus(
            rule,
            ACTIVE,
            current.time_ns,
            f"{rule.comparison} {format_number(rule.level, unit)}; value {value}",
            current.time_ns,
            (rule.rule_id, current.time_ns) in acknowledged,
        )
    if current.state is AlarmState.NORMAL:
        value = format_number(current.value, unit) if current.value is not None else "--"
        return RuleStatus(rule, NORMAL, current.time_ns, f"value {value}")
    if current.reason == REASON_STALE and rule.stale_after_ns is not None:
        last = current.time_ns - rule.stale_after_ns - 1
        return RuleStatus(
            rule,
            STALE,
            current.time_ns,
            f"no sample after {format_seconds(last)} "
            f"(validity window {format_seconds(rule.stale_after_ns)})",
        )
    return RuleStatus(rule, UNKNOWN, current.time_ns, "missing value")


def _spin(minimum: float, maximum: float, decimals: int, suffix: str = "") -> QDoubleSpinBox:
    spin = QDoubleSpinBox()
    spin.setRange(minimum, maximum)
    spin.setDecimals(decimals)
    spin.setSuffix(suffix)
    spin.setKeyboardTracking(False)
    return spin


class ThresholdsTool(QWidget):
    """Edit threshold rules and show each rule's state at the cursor or the newest data."""

    def __init__(self, context: AnalysisContext, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setMinimumSize(360, 200)
        self.context = context
        self.rules: list[ThresholdRule] = []
        self.acknowledged: frozenset[tuple[str, int]] = frozenset()
        self.statuses: dict[str, RuleStatus] = {}
        self.evaluation_ns: int | None = None
        self.lines: list[pg.InfiniteLine] = []
        self._line_plot: pg.PlotWidget | None = None
        self._line_spec: tuple[tuple[object, ...], ...] = ()
        self._session: object | None = None
        self._cache_key: tuple[SessionSnapshot | None, int | None] = (None, None)
        self._transitions: dict[str, tuple[AlarmTransition, ...]] = {}
        self._signal_choices: tuple[tuple[str, str], ...] = ()

        self.hint = QLabel()
        self.hint.setWordWrap(True)
        self.table = QTableWidget(0, len(COLUMNS))
        self.table.setHorizontalHeaderLabels(list(COLUMNS))
        self.table.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        self.table.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
        self.table.setSelectionMode(QAbstractItemView.SelectionMode.SingleSelection)
        self.table.verticalHeader().setVisible(False)
        self.table.setAccessibleName("Threshold rules and their states")
        header = self.table.horizontalHeader()
        header.setSectionResizeMode(QHeaderView.ResizeMode.ResizeToContents)
        header.setStretchLastSection(True)
        self.table.itemSelectionChanged.connect(self._selection_changed)

        self.signal = QComboBox()
        self.signal.setAccessibleName("Signal")
        self.comparison = QComboBox()
        self.comparison.addItem("Above (alarm when value > level)", "above")
        self.comparison.addItem("Below (alarm when value < level)", "below")
        self.level = _spin(-1e12, 1e12, 6)
        self.hysteresis = _spin(0.0, 1e12, 6)
        self.clear_level = QLabel()
        self.debounce = _spin(0.0, 86_400.0, 3, " s")
        self.validity = _spin(0.0, 86_400.0, 3, " s")
        self.validity.setSpecialValueText("off")
        self.severity = QComboBox()
        for severity in SEVERITIES:
            self.severity.addItem(severity.capitalize(), severity)
        self.severity.setCurrentIndex(SEVERITIES.index("warning"))
        self.label = QLineEdit()
        self.label.setPlaceholderText("optional name shown in the list and on the plot")
        for widget in (self.comparison, self.signal):
            widget.currentIndexChanged.connect(self._update_clear_level)
        for spin in (self.level, self.hysteresis):
            spin.valueChanged.connect(self._update_clear_level)

        form = QFormLayout()
        form.addRow("Signal", self.signal)
        form.addRow("Comparison", self.comparison)
        form.addRow("Level", self.level)
        form.addRow("Hysteresis", self.hysteresis)
        form.addRow("Clear level", self.clear_level)
        form.addRow("Debounce", self.debounce)
        form.addRow("Validity window", self.validity)
        form.addRow("Severity", self.severity)
        form.addRow("Name", self.label)
        editor = QGroupBox("Rule")
        editor.setLayout(form)

        self.add_button = QPushButton("Add rule")
        self.update_button = QPushButton("Update rule")
        self.remove_button = QPushButton("Remove rule")
        self.acknowledge_button = QPushButton("Acknowledge")
        self.import_button = QPushButton("Import...")
        self.export_button = QPushButton("Export...")
        self.add_button.clicked.connect(self.add_rule)
        self.update_button.clicked.connect(self.update_rule)
        self.remove_button.clicked.connect(self.remove_rule)
        self.acknowledge_button.clicked.connect(self.acknowledge_selected)
        self.import_button.clicked.connect(self.import_rules)
        self.export_button.clicked.connect(self.export_rules)
        edit_row = QHBoxLayout()
        for button in (
            self.add_button,
            self.update_button,
            self.remove_button,
            self.acknowledge_button,
        ):
            edit_row.addWidget(button)
        file_row = QHBoxLayout()
        file_row.addWidget(self.import_button)
        file_row.addWidget(self.export_button)
        file_row.addStretch(1)
        self.message = QLabel()
        self.message.setWordWrap(True)

        # The list sits beside the editor so the dock stays short and the main
        # plot keeps its height; a scroll area takes over when space is tight.
        rules_column = QVBoxLayout()
        rules_column.addWidget(self.hint)
        rules_column.addWidget(self.table, 1)
        rules_column.addWidget(self.message)
        editor_column = QVBoxLayout()
        editor_column.addWidget(editor)
        editor_column.addLayout(edit_row)
        editor_column.addLayout(file_row)
        editor_column.addStretch(1)
        content = QWidget()
        columns = QHBoxLayout(content)
        columns.addLayout(rules_column, 3)
        columns.addLayout(editor_column, 2)
        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setFrameShape(QFrame.Shape.NoFrame)
        scroll.setWidget(content)
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.addWidget(scroll)

        self.timer = QTimer(self)
        self.timer.setSingleShot(True)
        self.timer.setInterval(100)
        self.timer.timeout.connect(self.refresh)
        for change in (context.snapshot_changed, context.signal_changed, context.cursor_changed):
            change.connect(self.timer.start)
        context.signal_changed.connect(self._follow_selected_signal)
        context.main_plot_changed.connect(self._sync_lines)
        self._update_signal_choices()
        self._follow_selected_signal()
        self.refresh()

    # Evaluation -----------------------------------------------------------

    def refresh(self) -> None:
        """Evaluate every rule and show its state at the cursor or the newest data."""

        self.timer.stop()
        context = self.context
        if context.session is not self._session:
            self._session = context.session
            self.acknowledged = frozenset()
            self._cache_key = (None, None)
        self._update_signal_choices()
        snapshot = context.snapshot
        bounds = context.bounds
        statuses: dict[str, RuleStatus] = {}
        if snapshot is None or bounds is None:
            self.evaluation_ns = None
            self.hint.setText(
                "Load a session to evaluate rules."
                if snapshot is None
                else "The session has no readings yet; every rule is unknown."
            )
            for rule in self.rules:
                statuses[rule.rule_id] = RuleStatus(rule, UNKNOWN, None, "no session data")
        else:
            live = context.following_live or context.cursor_ns is None
            at = bounds.end_ns if live else context.cursor_ns
            assert at is not None
            self.evaluation_ns = at
            end_ns = max(bounds.end_ns, at)
            cached_snapshot, cached_end = self._cache_key
            # Identity, not equality: comparing snapshots would compare every record.
            if cached_snapshot is not snapshot or cached_end != end_ns:
                self._cache_key = (snapshot, end_ns)
                self._transitions.clear()
            where = (
                "Following live data: evaluated at the newest sample"
                if context.following_live
                else "Evaluated at the newest sample"
                if live
                else "Evaluated at the replay cursor"
            )
            self.hint.setText(
                f"{where}, {format_seconds(at)}. Rules only read session data; "
                "nothing is sent to the source."
            )
            for rule in self.rules:
                statuses[rule.rule_id] = self._status(rule, at)
        self.statuses = statuses
        self._show_rows()
        self._sync_lines()

    def transitions(self, rule: ThresholdRule) -> tuple[AlarmTransition, ...]:
        """Return the rule's state changes over the whole loaded snapshot."""

        cached = self._transitions.get(rule.rule_id)
        if cached is not None:
            return cached
        snapshot, end_ns = self._cache_key
        if snapshot is None or end_ns is None:
            return ()
        times, values = self.context.numeric_series(rule.signal_id)
        result = evaluate(rule, times, values, end_ns=max([end_ns, *times[-1:]]))
        self._transitions[rule.rule_id] = result
        return result

    def _status(self, rule: ThresholdRule, at: int) -> RuleStatus:
        descriptor = self.context.descriptor(rule.signal_id)
        if descriptor is None:
            return RuleStatus(rule, UNKNOWN, None, "signal is not in this session")
        if not is_numeric(descriptor):
            return RuleStatus(
                rule,
                UNKNOWN,
                None,
                f"signal is {descriptor.data_type}; thresholds need a numeric signal",
            )
        unit = descriptor.canonical_unit or ""
        if rule.unit and unit and rule.unit != unit:
            return RuleStatus(
                rule,
                UNKNOWN,
                None,
                f"unit mismatch: rule uses {rule.unit!r}, signal reports {unit!r}",
            )
        return rule_status(rule, self.transitions(rule), at, self.acknowledged)

    def status(self, rule_id: str) -> RuleStatus:
        return self.statuses[rule_id]

    def rows(self) -> list[tuple[str, ...]]:
        """Return the displayed cell texts, one tuple per rule, in list order."""

        return [
            tuple(
                (item.text() if (item := self.table.item(row, column)) else "")
                for column in range(self.table.columnCount())
            )
            for row in range(self.table.rowCount())
        ]

    def _show_rows(self) -> None:
        selected = self.selected_rule()
        style = self.style()
        with QSignalBlocker(self.table):
            self.table.setRowCount(len(self.rules))
            for row, rule in enumerate(self.rules):
                status = self.statuses[rule.rule_id]
                since = format_seconds(status.since_ns) if status.since_ns is not None else "--"
                cells = (
                    status.text,
                    since,
                    f"{rule_name(rule)}: {rule.describe()}",
                    rule.signal_id,
                    rule.severity,
                    status.detail,
                )
                spoken = (
                    f"{rule_name(rule)} is {status.text.split(' ', 1)[1]}, since {since}. "
                    f"{status.detail}"
                )
                for column, text in enumerate(cells):
                    item = QTableWidgetItem(text)
                    item.setData(Qt.ItemDataRole.UserRole, rule.rule_id)
                    item.setToolTip(spoken)
                    if column == 0:
                        item.setIcon(style.standardIcon(_ICONS[status.state]))
                        item.setData(Qt.ItemDataRole.AccessibleTextRole, spoken)
                        item.setBackground(QBrush(QColor(_COLORS[status.state])))
                        item.setForeground(QBrush(QColor("#000000")))
                    self.table.setItem(row, column, item)
            if selected is not None and selected in self.rules:
                self.table.selectRow(self.rules.index(selected))
        self._update_buttons()

    # Editing --------------------------------------------------------------

    def selected_rule(self) -> ThresholdRule | None:
        rows = self.table.selectionModel().selectedRows()
        if not rows or not 0 <= rows[0].row() < len(self.rules):
            return None
        return self.rules[rows[0].row()]

    def select_rule(self, rule_id: str) -> None:
        for row, rule in enumerate(self.rules):
            if rule.rule_id == rule_id:
                self.table.selectRow(row)
                return
        raise KeyError(rule_id)

    def rule_from_form(self, rule_id: str | None = None) -> ThresholdRule:
        """Build a rule from the editor; raises ThresholdRuleError naming a bad field."""

        signal_id = self.signal.currentData()
        if not signal_id:
            raise ThresholdRuleError("signal_id", "choose a numeric signal")
        descriptor = self.context.descriptor(signal_id)
        previous = next((rule for rule in self.rules if rule.rule_id == rule_id), None)
        if descriptor is not None:
            unit = descriptor.canonical_unit or ""
        elif previous is not None and previous.signal_id == signal_id:
            unit = previous.unit
        else:
            unit = ""
        comparison = self.comparison.currentData()
        validity = round(self.validity.value() * NS_PER_S)
        return ThresholdRule(
            rule_id or self._new_rule_id(signal_id, comparison),
            signal_id,
            comparison,
            self.level.value(),
            hysteresis=self.hysteresis.value(),
            debounce_ns=round(self.debounce.value() * NS_PER_S),
            severity=self.severity.currentData(),
            stale_after_ns=validity or None,
            label=self.label.text().strip(),
            unit=unit,
        )

    def add_rule(self) -> ThresholdRule | None:
        try:
            rule = self.rule_from_form()
        except ThresholdRuleError as error:
            self.message.setText(f"Cannot add the rule: {error}")
            return None
        self.rules.append(rule)
        self._rules_changed(f"Added {rule_name(rule)}.")
        self.select_rule(rule.rule_id)
        return rule

    def update_rule(self) -> ThresholdRule | None:
        current = self.selected_rule()
        if current is None:
            self.message.setText("Select a rule in the list to update it.")
            return None
        try:
            rule = self.rule_from_form(current.rule_id)
        except ThresholdRuleError as error:
            self.message.setText(f"Cannot update the rule: {error}")
            return None
        self.rules[self.rules.index(current)] = rule
        self._forget(rule.rule_id)
        self._rules_changed(f"Updated {rule_name(rule)}.")
        return rule

    def remove_rule(self) -> None:
        current = self.selected_rule()
        if current is None:
            self.message.setText("Select a rule in the list to remove it.")
            return
        self.table.clearSelection()
        self.rules.remove(current)
        self._forget(current.rule_id)
        self._rules_changed(f"Removed {rule_name(current)}.")

    def set_rules(self, rules: list[ThresholdRule] | tuple[ThresholdRule, ...]) -> None:
        """Replace every rule (IDs must be unique) and forget all acknowledgements."""

        rules_to_json(rules)  # Validates types and unique IDs.
        self.rules = list(rules)
        self.acknowledged = frozenset()
        self._transitions.clear()
        self._rules_changed("")

    def acknowledge_selected(self) -> bool:
        """Acknowledge the activation in effect for the selected rule."""

        rule = self.selected_rule()
        status = self.statuses.get(rule.rule_id) if rule is not None else None
        if rule is None or status is None or status.activation_ns is None:
            self.message.setText("Only an active rule can be acknowledged.")
            return False
        self.acknowledged = acknowledge(
            self.acknowledged, rule.rule_id, self.transitions(rule), status.activation_ns
        )
        self.message.setText(
            f"Acknowledged {rule_name(rule)} active since {format_seconds(status.activation_ns)}."
        )
        self.refresh()
        return True

    def _new_rule_id(self, signal_id: str, comparison: str) -> str:
        used = {rule.rule_id for rule in self.rules}
        number = 1
        while f"{signal_id}-{comparison}-{number}" in used:
            number += 1
        return f"{signal_id}-{comparison}-{number}"

    def _forget(self, rule_id: str) -> None:
        self._transitions.pop(rule_id, None)
        self.acknowledged = frozenset(key for key in self.acknowledged if key[0] != rule_id)

    def _rules_changed(self, message: str) -> None:
        self.message.setText(message)
        self.refresh()

    def _selection_changed(self) -> None:
        rule = self.selected_rule()
        if rule is not None:
            self._load_form(rule)
        self._update_buttons()

    def _load_form(self, rule: ThresholdRule) -> None:
        self._update_signal_choices(extra=rule.signal_id)
        self.signal.setCurrentIndex(self.signal.findData(rule.signal_id))
        self.comparison.setCurrentIndex(self.comparison.findData(rule.comparison))
        self.level.setValue(rule.level)
        self.hysteresis.setValue(rule.hysteresis)
        self.debounce.setValue(rule.debounce_ns / NS_PER_S)
        self.validity.setValue((rule.stale_after_ns or 0) / NS_PER_S)
        self.severity.setCurrentIndex(self.severity.findData(rule.severity))
        self.label.setText(rule.label)
        self._update_clear_level()

    def _update_buttons(self) -> None:
        rule = self.selected_rule()
        status = self.statuses.get(rule.rule_id) if rule is not None else None
        self.update_button.setEnabled(rule is not None)
        self.remove_button.setEnabled(rule is not None)
        self.acknowledge_button.setEnabled(
            status is not None and status.state == ACTIVE and not status.acknowledged
        )
        self.export_button.setEnabled(bool(self.rules))

    def _update_clear_level(self) -> None:
        above = self.comparison.currentData() == "above"
        hysteresis = self.hysteresis.value()
        level = self.level.value()
        clear = level - hysteresis if above else level + hysteresis
        descriptor = self.context.descriptor(self.signal.currentData())
        unit = descriptor.canonical_unit or "" if descriptor is not None else ""
        self.clear_level.setText(
            f"clears {'below' if above else 'above'} {format_number(clear, unit)}"
        )

    def _update_signal_choices(self, extra: str | None = None) -> None:
        choices = [
            (item.identifier, f"{item.display_name} ({item.identifier})")
            for item in self.context.descriptors
            if is_numeric(item)
        ]
        known = {identifier for identifier, _ in choices}
        for signal_id in [*(rule.signal_id for rule in self.rules), extra]:
            if signal_id and signal_id not in known:
                known.add(signal_id)
                choices.append((signal_id, f"{signal_id} (not in this session)"))
        if tuple(choices) == self._signal_choices:
            return
        self._signal_choices = tuple(choices)
        current = self.signal.currentData()
        with QSignalBlocker(self.signal):
            self.signal.clear()
            for identifier, text in choices:
                self.signal.addItem(text, identifier)
            index = self.signal.findData(current)
            self.signal.setCurrentIndex(index if index >= 0 else 0)
        self._update_clear_level()

    def _follow_selected_signal(self) -> None:
        """Preset the editor to the signal selected in the browser while no rule is selected."""

        if self.selected_rule() is not None:
            return
        self._update_signal_choices()
        index = self.signal.findData(self.context.selected_id)
        if index >= 0:
            self.signal.setCurrentIndex(index)

    # Main plot lines ------------------------------------------------------

    def _sync_lines(self) -> None:
        """Draw rule levels on the main plot; re-add them after the plot was cleared."""

        context = self.context
        plot = context.main_plot
        signal_id = context.main_plot_signal_id
        spec: tuple[tuple[object, ...], ...] = ()
        if plot is not None and signal_id is not None and signal_id == context.selected_id:
            specs: list[tuple[object, ...]] = []
            for rule in self.rules:
                if rule.signal_id != signal_id:
                    continue
                name = rule_name(rule)
                unit = f" {rule.unit}" if rule.unit else ""
                specs.append(
                    (
                        rule.level,
                        rule.severity,
                        False,
                        f"{name}: {rule.comparison} {rule.level:g}{unit}",
                    )
                )
                if rule.hysteresis:
                    clear = "below" if rule.comparison == "above" else "above"
                    specs.append(
                        (
                            rule.clear_level,
                            rule.severity,
                            True,
                            f"{name}: clears {clear} {rule.clear_level:g}{unit}",
                        )
                    )
            spec = tuple(specs)
        attached = all(line.scene() is not None for line in self.lines)
        if plot is self._line_plot and spec == self._line_spec and attached:
            return
        self._remove_lines()
        self._line_plot, self._line_spec = plot, spec
        if plot is None:
            return
        for level, severity, is_clear, text in spec:
            pen = pg.mkPen(
                _SEVERITY_PENS[str(severity)],
                width=1 if is_clear else 2,
                style=Qt.PenStyle.DotLine if is_clear else Qt.PenStyle.DashLine,
            )
            line = pg.InfiniteLine(pos=level, angle=0, movable=False, pen=pen)
            line.setToolTip(str(text))
            # No text label on the line: pyqtgraph line labels crash in a short
            # plot. The tooltip and the Rule column name each level instead.
            plot.addItem(line)
            self.lines.append(line)

    def _remove_lines(self) -> None:
        plot = self._line_plot
        for line in self.lines:
            if plot is not None and line.scene() is not None:
                try:
                    plot.removeItem(line)
                except RuntimeError:
                    pass  # The plot was already deleted with the window.
        self.lines = []

    # Files and configuration ---------------------------------------------

    def configuration(self) -> dict[str, Any]:
        """Return the rules as the versioned threshold document (a JSON object)."""

        return json.loads(rules_to_json(self.rules))

    def restore_configuration(self, config: object) -> None:
        """Replace the rules from ``configuration()`` output; raises ValueError if invalid."""

        if not isinstance(config, dict):
            raise ValueError("Threshold configuration must be a JSON object.")
        try:
            text = json.dumps(config)
        except (TypeError, ValueError) as error:
            raise ValueError(f"Threshold configuration is not JSON data: {error}") from None
        self.set_rules(rules_from_json(text))

    def import_rules(self) -> bool:
        path, _ = QFileDialog.getOpenFileName(self, "Import threshold rules", "", FILE_FILTER)
        if not path:
            return False
        name = Path(path).name
        try:
            with open(path, encoding="utf-8") as handle:
                text = handle.read(MAX_THRESHOLD_FILE_CHARS + 1)
            rules = rules_from_json(text)
        except (OSError, UnicodeDecodeError, ThresholdFileError) as error:
            self.message.setText(f"Could not import {name}: {error}")
            return False
        self.set_rules(rules)
        self.message.setText(f"Imported {len(rules)} rule(s) from {name}.")
        return True

    def export_rules(self) -> bool:
        path, _ = QFileDialog.getSaveFileName(
            self, "Export threshold rules", "thresholds.json", FILE_FILTER
        )
        if not path:
            return False
        target = Path(path)
        if not target.suffix:
            target = target.with_suffix(".json")
        try:
            target.write_text(rules_to_json(self.rules), encoding="ascii", newline="\n")
        except OSError as error:
            self.message.setText(f"Could not export to {target.name}: {error}")
            return False
        self.message.setText(f"Exported {len(self.rules)} rule(s) to {target.name}.")
        return True


TOOL = AnalysisTool("thresholds", "Thresholds", ThresholdsTool)
