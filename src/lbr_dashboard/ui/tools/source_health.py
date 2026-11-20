"""Source health tool: connection, freshness and observable loss of the source."""

from __future__ import annotations

from PySide6.QtCore import QTimer
from PySide6.QtWidgets import QLabel, QVBoxLayout, QWidget

from ..analysis import AnalysisContext, AnalysisTool
from ..source_health_panel import SourceHealthPanel

EMPTY_HINT = "Connect a source (CSV replay or a simulator) to see its health."
HINT = (
    "Ages use the dashboard's arrival clock, not measurement time. "
    "'unknown' means the source does not provide that metadata; it is never shown as 0."
)


class SourceHealthTool(QWidget):
    """Follow the source controller's health reports; repaint at most every 200 ms."""

    def __init__(self, context: AnalysisContext, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.context = context
        self.hint = QLabel()
        self.hint.setWordWrap(True)
        self.panel = SourceHealthPanel()
        self.setMinimumSize(320, 240)
        layout = QVBoxLayout(self)
        layout.addWidget(self.hint)
        layout.addWidget(self.panel)
        self.timer = QTimer(self)
        self.timer.setSingleShot(True)
        self.timer.setInterval(200)
        self.timer.timeout.connect(self.refresh)
        # Reports arrive on every poll; do not restart the timer, or it would never fire.
        context.source_health_changed.connect(self._schedule)
        self.refresh()

    def _schedule(self) -> None:
        if not self.timer.isActive():
            self.timer.start()

    def refresh(self) -> None:
        self.timer.stop()
        report = self.context.source_health
        if report is None:
            self.hint.setText(EMPTY_HINT)
            self.panel.clear()
            return
        self.hint.setText(HINT)
        self.panel.show_report(report)


TOOL = AnalysisTool("source_health", "Source health", SourceHealthTool)
