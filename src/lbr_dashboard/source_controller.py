"""Application-owned source selection, bounded ingestion and nonblocking cleanup."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from time import monotonic

from PySide6.QtCore import QObject, QTimer
from PySide6.QtWidgets import QAbstractItemView, QFileDialog

from .core import (
    BackgroundService,
    CancellationToken,
    SignalSchema,
    SimulatorSource,
    SourceState,
    TelemetrySession,
    TelemetrySource,
    example_signals,
)
from .importers.csv_source import prepare_csv
from .ui.main_window import MainWindow


@dataclass(frozen=True)
class SourceOption:
    key: str
    label: str
    prepare: Callable[[CancellationToken], Callable[[], TelemetrySource]]


class SourceController(QObject):
    """Poll on the Qt owner; workers never call widgets or own presentation state."""

    def __init__(self, window: MainWindow) -> None:
        super().__init__(window)
        self.window = window
        self.source: TelemetrySource | None = None
        self.session: TelemetrySession | None = None
        self._options: dict[str, SourceOption] = {}
        self._preparing: BackgroundService | None = None
        self._prepared: list[Callable[[], TelemetrySource]] = []
        self._next: Callable[[], TelemetrySource] | None = None
        self._last: SourceOption | None = None
        self._cancel_preparation = False
        self._preparation_cancel_started: float | None = None
        self._releasing = False
        self._release_started = 0.0
        self._closing = False
        self._revision = -1
        self._selected = 0
        self._label = ""
        self._notice = ""
        self._displayed_schema: SignalSchema | None = None
        self._finished_rendered = False
        self._legacy_loaded = False
        for family in ("process", "motion", "live"):

            def prepare(token, family=family):
                token.raise_if_cancelled()
                return lambda: SimulatorSource(
                    example_signals("process" if family == "live" else family),
                    seed=7,
                    duration_ns=None if family == "live" else 3_000_000_000,
                    realtime=family == "live",
                )

            self.register(SourceOption(family, f"Fictional {family} simulator", prepare))
        window.source_picker.connect_requested.connect(self.choose)
        window.source_picker.disconnect_requested.connect(self.disconnect)
        window.source_picker.retry_requested.connect(self.retry)
        window.source_picker.seek_requested.connect(lambda: self.operate("seek"))
        window.source_picker.rate_requested.connect(lambda: self.operate("rate"))
        window.left_panel.list_widget.currentRowChanged.connect(self.select)
        window.left_panel.signal_browser.signal_selected.connect(self.select_signal)
        self.timer = QTimer(self)
        self.timer.setInterval(30)
        self.timer.timeout.connect(self.poll)
        self.timer.start()

    def register(self, option: SourceOption) -> None:
        if option.key in self._options:
            raise ValueError(f"Source option already registered: {option.key}")
        self._options[option.key] = option
        picker = self.window.source_picker
        if picker.choices.findData(option.key) < 0:
            picker.choices.addItem(option.label, option.key)

    def choose(self, key: str) -> None:
        if key == "csv":
            file_path, _ = QFileDialog.getOpenFileName(
                self.window, "Open timestamp_ms CSV", "", "CSV files (*.csv)"
            )
            if file_path:
                self.open_csv(Path(file_path))
        else:
            self.request(self._options[key])

    def open_csv(self, path: Path) -> None:
        def prepare(token):
            prepared = prepare_csv(path, token)
            return prepared.source

        self.request(SourceOption("csv", str(path), prepare))

    def request(self, option: SourceOption) -> None:
        if self._preparing is not None or self._releasing or self._closing:
            return
        self._last = option
        self._prepared = []
        self._cancel_preparation = False
        self._preparation_cancel_started = None
        self._notice = f"Preparing {option.label} - Disconnect cancels."

        def work(token, emit):
            factory = option.prepare(token)
            token.raise_if_cancelled()
            self._prepared.append(factory)

        self._preparing = BackgroundService(work)
        self._preparing.start()
        self._present()

    def retry(self) -> None:
        if self._last is not None:
            self.request(self._last)

    def operate(self, operation: str) -> None:
        if self.source is None or self._preparing is not None or self._releasing:
            return
        try:
            if operation == "seek":
                if not self.source.capabilities.supports_seek:
                    return
                snapshot = self.session.snapshot() if self.session is not None else None
                times = (
                    [
                        records[0].sample.timestamp_ns
                        for records in snapshot.series.values()
                        if records
                    ]
                    if snapshot
                    else []
                )
                self.source.seek(min(times) if times else 0)
                self._finished_rendered = False
            else:
                if not self.source.capabilities.supports_rate_control:
                    return
                self.source.set_rate(2)
            self._notice = f"Source {operation} requested."
        except Exception as error:
            self._notice = f"Source {operation} failed: {error}. Retry or disconnect."
        self._present()

    def disconnect(self) -> None:
        self._next = None
        self._cancel_preparation = True
        if self._preparing is not None:
            self._preparing.cancel()
            if self._preparation_cancel_started is None:
                self._preparation_cancel_started = monotonic()
            self._notice = "Cancelling source preparation - waiting for worker cleanup."
        self._begin_release()
        self._present()

    def has_session(self) -> bool:
        return self.session is not None

    def release_for_legacy(self) -> None:
        """Legacy table imports may replace presentation only after a successful decode."""
        if self.source is not None or self._preparing is not None or self.session is not None:
            self.disconnect()
            self.session = None
            self._legacy_loaded = True
            self._notice = (
                "Legacy CSV table loaded. Use the source picker for timestamped sessions."
            )

    def _begin_release(self) -> None:
        if self._releasing:
            return
        self._releasing = True
        self._release_started = monotonic()
        if self.source is not None:
            self.source.stop()

    def close(self) -> bool:
        """Keep window alive until every owned worker actually releases its resources."""
        if self.source is None and self._preparing is None and not self._releasing:
            self.timer.stop()
            return True
        self._closing = True
        self.disconnect()
        return False

    def _start(self, factory: Callable[[], TelemetrySource]) -> None:
        try:
            self.source = factory()
            self.session = TelemetrySession(
                self.source.schema,
                max_samples_per_signal=1000 if self.source.capabilities.is_live else None,
            )
            self.source.open()
            self.source.start()
            self._legacy_loaded = False
            self._label = self._last.label if self._last else self.source.source_id
            self._notice = ""
            self._revision = -1
            self._displayed_schema = None
            self._finished_rendered = False
        except Exception as error:
            self._notice = f"Could not start source: {error}. Retry or select another source."
            self._begin_release()

    def poll(self) -> None:
        try:
            self._poll()
        except Exception as error:
            self._notice = f"Source data could not be displayed: {error}. Disconnect or retry."
            self._next = None
            self._begin_release()
            self._present()

    def _poll(self) -> None:
        if (
            self._preparing is not None
            and self._preparation_cancel_started is not None
            and monotonic() - self._preparation_cancel_started >= 2
        ):
            self._notice = (
                "Preparation cleanup is still pending. "
                "Waiting for the adapter to release its worker."
            )
        if self._preparing is not None and self._preparing.join(0):
            failure = self._preparing.failure
            self._preparing = None
            self._preparation_cancel_started = None
            if self._cancel_preparation:
                if not self._legacy_loaded:
                    self._notice = "Cancelled - previous readings remain available."
            elif failure is not None:
                self._notice = (
                    f"Could not open source: {failure.message}. Correct the file or retry."
                )
            elif self._prepared:
                self._next = self._prepared[0]
                self._begin_release()
            self._prepared.clear()
        if self._releasing:
            if self.source is None or self.source.close(0):
                self.source = None
                self._releasing = False
                if self._next is not None and not self._closing:
                    factory, self._next = self._next, None
                    self._start(factory)
                elif self._preparing is None:
                    if self._legacy_loaded:
                        self._notice = (
                            "Legacy CSV table loaded. "
                            "Use the source picker for timestamped sessions."
                        )
                    elif not self._notice or self._cancel_preparation:
                        self._notice = "Disconnected - readings retained for inspection."
            elif monotonic() - self._release_started >= 2:
                self._notice = (
                    "Cleanup is still pending. The adapter has not released its worker; "
                    "switching and window close will wait."
                )
        if (
            self.source is not None
            and self.session is not None
            and not self._releasing
            and not self._finished_rendered
        ):
            self.session.ingest_source(self.source, limit=512)
            snapshot = self.session.snapshot()
            if snapshot.revision != self._revision:
                self._revision = snapshot.revision
                self._render()
            self._finished_rendered = self.source.join(0) and self.source.events.stats.buffered == 0
        self._present()
        if (
            self._closing
            and self.source is None
            and self._preparing is None
            and not self._releasing
        ):
            self.timer.stop()
            self.window.close()

    def _present(self) -> None:
        picker = self.window.source_picker
        busy = self._preparing is not None or self._releasing
        picker.set_actions(
            busy=busy or self._closing,
            connected=self.source is not None or self._preparing is not None,
            retry=self._last is not None,
        )
        source = self.source
        picker.show_capabilities(source.capabilities if source else None)
        message = self._notice
        if source is not None and not self._releasing:
            health = source.health()
            detail = f"{self._label} | {source.source_id} | {health.state.value}"
            if health.last_error is not None:
                detail += f" | {health.last_error.message}"
            if health.state is SourceState.FAILED:
                detail += " | Retry or choose another source."
            if health.dropped_records:
                detail += f" | lost records: {health.dropped_records}"
            if message:
                detail += f" | {message}"
            message = detail
        picker.status.setText(message or "Idle - choose CSV or a fictional simulator.")

    def select(self, index: int) -> None:
        self._selected = index
        if self.session is not None:
            self._render()

    def select_signal(self, identifier: str) -> None:
        if self.session is None or not identifier:
            return
        self._selected = next(
            (i for i, d in enumerate(self.session.schema.signals) if d.identifier == identifier), -1
        )
        self._render()

    def _render(self) -> None:
        if self.session is None:
            return
        snapshot = self.session.snapshot()
        descriptors = snapshot.schema.signals
        browser = self.window.left_panel.signal_browser
        previous_id = browser.selected_id
        if self._displayed_schema != snapshot.schema:
            self._displayed_schema = snapshot.schema
            labels = [
                f"{d.display_name} [{d.canonical_unit or 'unit unspecified'}] - {d.identifier}"
                for d in descriptors
            ]
            self.window.left_panel.list_widget.blockSignals(True)
            self.window.left_panel.set_columns(labels)
            self._selected = next(
                (i for i, d in enumerate(descriptors) if d.identifier == previous_id),
                next((i for i, d in enumerate(descriptors) if d.data_type == "numeric"), 0),
            )
            self.window.left_panel.list_widget.setCurrentRow(self._selected)
            self.window.left_panel.list_widget.blockSignals(False)
        availability = {
            identifier: (
                records[-1].sample.available,
                records[-1].sample.quality
                or (
                    "Latest reading available"
                    if records[-1].sample.available
                    else "Missing reading"
                ),
            )
            for identifier, records in snapshot.series.items()
            if records
        }
        browser.refresh(snapshot.schema, availability)
        self.window.left_panel.list_widget.hide()
        browser.show()
        if not 0 <= self._selected < len(descriptors):
            return
        descriptor = descriptors[self._selected]
        browser.select_id(descriptor.identifier)
        records = snapshot.series[descriptor.identifier]
        # One selected signal, at most 1,000 visible raw records; original session stays intact.
        rows = [
            (
                str(r.sample.timestamp_ns),
                str(r.sample.value) if r.sample.available else "",
                str(r.sample.available),
                r.sample.quality or "",
                r.sample.unit or descriptor.canonical_unit or "",
                r.source_id,
            )
            for r in records[-100:]
        ]
        self.window.right_panel.set_table(
            ("Original time (ns)", "Value", "Available", "Quality", "Unit", "Source"), rows
        )
        self.window.right_panel.table.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        panel = self.window.right_panel
        panel._reset_plot()
        if descriptor.data_type not in {"numeric", "boolean"} or descriptor.shape:
            panel.plot_widget.setTitle(
                f"{descriptor.display_name} - {descriptor.data_type} (inspect values below)"
            )
            return
        if not records:
            panel.plot_widget.setTitle(f"{descriptor.display_name} - waiting for readings")
            return
        origin = records[0].sample.timestamp_ns
        visible = records[-1000:]
        xs = [(r.sample.timestamp_ns - origin) / 1e9 for r in visible]
        ys = [float(r.sample.value) if r.sample.available else float("nan") for r in visible]
        panel.plot_widget.plot(xs, ys, pen="#00aaff", symbol="o", symbolSize=5, connect="finite")
        panel.plot_widget.setTitle(descriptor.display_name)
        panel.plot_widget.setLabel("bottom", "Measurement time since first reading", units="s")
        panel.plot_widget.setLabel(
            "left", descriptor.display_name, units=descriptor.canonical_unit or ""
        )
