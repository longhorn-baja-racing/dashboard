"""Statistics tool: current value at the replay cursor and statistics for the interval."""

from __future__ import annotations

from PySide6.QtCore import QTimer
from PySide6.QtWidgets import QLabel, QVBoxLayout, QWidget

from ...core.statistics import current_value, interval_statistics
from ..analysis import AnalysisContext, AnalysisTool, is_numeric, refresh_soon
from ..statistics_panel import StatisticsPanel


class StatisticsTool(QWidget):
    """Follow the selected signal; recompute at most every 100 ms while data streams."""

    def __init__(self, context: AnalysisContext, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.context = context
        self.hint = QLabel()
        self.hint.setWordWrap(True)
        self.panel = StatisticsPanel()
        layout = QVBoxLayout(self)
        layout.addWidget(self.hint)
        layout.addWidget(self.panel)
        self.timer = QTimer(self)
        self.timer.setSingleShot(True)
        self.timer.setInterval(100)
        self.timer.timeout.connect(self.refresh)
        for change in (
            context.snapshot_changed,
            context.signal_changed,
            context.cursor_changed,
            context.interval_changed,
        ):
            change.connect(refresh_soon(self.timer))
        self.refresh()

    def refresh(self) -> None:
        self.timer.stop()
        context = self.context
        descriptor = context.descriptor(context.selected_id)
        if descriptor is None:
            self.hint.setText("Select a signal in the browser to see its statistics.")
            self.panel.clear()
            return
        if not is_numeric(descriptor):
            self.hint.setText(
                f"{descriptor.display_name} is {descriptor.data_type}; "
                "statistics need a numeric signal."
            )
            self.panel.clear()
            return
        unit = descriptor.canonical_unit or ""
        interval = context.interval
        cursor = context.cursor_ns
        current = None
        if cursor is not None:
            times, values = context.reading_at(descriptor.identifier, cursor)
            current = current_value(times, values, cursor, unit=unit)
        if interval is None:
            self.hint.setText(
                f"{descriptor.display_name}: select a time interval "
                "(Analysis > Select visible time range, then drag its edges)."
            )
            self.panel.clear()
            if current is not None:
                self.panel.show_current(current)
            return
        times, values = context.numeric_series(descriptor.identifier, interval)
        stats = interval_statistics(times, values, interval.start_ns, interval.end_ns, unit=unit)
        self.hint.setText(f"{descriptor.display_name} ({descriptor.identifier})")
        self.panel.show_statistics(stats, current)


TOOL = AnalysisTool("statistics", "Statistics", StatisticsTool)
