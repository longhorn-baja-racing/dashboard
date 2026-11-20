"""Application-owned source selection, bounded ingestion and nonblocking cleanup."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from time import monotonic, monotonic_ns

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
from .core.csv_mapping import CsvMapping
from .core.source_health import summary_text
from .core.source_monitor import SourceHealthMonitor
from .importers.csv_source import MAPPED_PREVIEW_ROWS, prepare_csv, prepare_mapped_csv
from .importers.lbrlog_source import prepare_lbrlog
from .importers.mapped_csv import CsvMappingError, MappedCsvImporter, read_sample, suggest_mapping
from .replay_controller import ReplayController
from .ui.csv_mapping_dialog import CsvMappingDialog
from .ui.main_window import MainWindow


@dataclass(frozen=True)
class SourceOption:
    key: str
    label: str
    prepare: Callable[[CancellationToken], Callable[[], TelemetrySource]]
    path: Path | None = None
    """The file the source reads, or None for sources without one."""


#: ``progress(done, total)`` from a preparation worker.
Progress = Callable[[int, int], None]


@dataclass(frozen=True)
class FileSourceOption:
    """A replay source read from a file chosen in an open dialog."""

    key: str
    label: str
    file_filter: str
    prepare: Callable[[Path, CancellationToken, Progress], Callable[[], TelemetrySource]]


def _prepare_lbrlog(path: Path, token: CancellationToken, progress: Progress):
    return prepare_lbrlog(path, token, progress).source


class SourceController(QObject):
    """Poll on the Qt owner; workers never call widgets or own presentation state."""

    def __init__(self, window: MainWindow) -> None:
        super().__init__(window)
        self.window = window
        self.source: TelemetrySource | None = None
        self.session: TelemetrySession | None = None
        self.log_path: Path | None = None
        self._options: dict[str, SourceOption] = {}
        self._file_options: dict[str, FileSourceOption] = {}
        self._progress: tuple[int, int] | None = None
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
        self.health_clock: Callable[[], int] = monotonic_ns
        self.health: SourceHealthMonitor | None = None
        self.mapping_dialog: CsvMappingDialog | None = None
        self.replay = ReplayController(window)
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
        self.register_file(
            FileSourceOption(
                "lbrlog",
                "Firmware log (.lbrlog)",
                "Firmware logs (*.lbrlog *.LBRLOG *.lbr *.LBR);;All files (*)",
                _prepare_lbrlog,
            )
        )
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

    def register_file(self, option: FileSourceOption) -> None:
        if option.key in self._options or option.key in self._file_options:
            raise ValueError(f"Source option already registered: {option.key}")
        self._file_options[option.key] = option
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
        elif key == "csv_mapped":
            file_path, _ = QFileDialog.getOpenFileName(
                self.window, "Open CSV to map", "", "CSV files (*.csv);;All files (*)"
            )
            if file_path:
                self.map_csv(Path(file_path))
        elif key in self._file_options:
            option = self._file_options[key]
            file_path, _ = QFileDialog.getOpenFileName(
                self.window, f"Open {option.label}", "", option.file_filter
            )
            if file_path:
                self.open_file(key, Path(file_path))
        else:
            self.request(self._options[key])

    def open_file(self, key: str, path: Path) -> None:
        option = self._file_options[key]

        def prepare(token):
            def progress(done: int, total: int) -> None:
                # Runs on the worker; a cancelled worker cannot report into a newer request.
                if not token.is_cancelled:
                    self._progress = (done, total)

            return option.prepare(path, token, progress)

        self.request(SourceOption(key, f"{option.label} {path}", prepare))

    def open_csv(self, path: Path) -> None:
        def prepare(token):
            prepared = prepare_csv(path, token)
            return prepared.source

        self.request(SourceOption("csv", str(path), prepare, path))

    def map_csv(self, path: Path) -> None:
        """Ask for an explicit column mapping; Cancel leaves the current session as it was."""
        try:
            headers, rows = read_sample(path)
        except CsvMappingError as error:
            self._notice = f"Could not open {path.name}: {error}"
            self._present()
            return
        suggestion = suggest_mapping(headers, rows)
        importer = MappedCsvImporter()
        dialog = CsvMappingDialog(
            path.name,
            headers,
            rows,
            suggestion.mapping,
            suggestion.ambiguities,
            lambda mapping: importer.import_session(path, mapping, max_rows=MAPPED_PREVIEW_ROWS),
            self.window,
        )
        dialog.accepted.connect(lambda: self.open_mapped_csv(path, dialog.mapping()))
        dialog.finished.connect(dialog.deleteLater)
        self.mapping_dialog = dialog
        dialog.open()

    def open_mapped_csv(self, path: Path, mapping: CsvMapping) -> None:
        def prepare(token):
            return prepare_mapped_csv(path, mapping, token).source

        self.request(SourceOption("csv_mapped", str(path), prepare))

    def request(self, option: SourceOption) -> None:
        if self._preparing is not None or self._releasing or self._closing:
            return
        self._last = option
        self._prepared = []
        self._progress = None
        self._cancel_preparation = False
        self._preparation_cancel_started = None
        self._notice = f"Preparing {option.label} - Disconnect cancels."

        def work(token, emit):
            factory = option.prepare(token)
            token.raise_if_cancelled()
            self._prepared.append(factory)

        self._preparing = BackgroundService(work)
        self._preparing.start()
        self.replay.set_busy()
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
            self.window.workspace_docks.update_snapshot(None)
            self.window.analysis.clear()
            self.window.analysis_docks.leave_plot()
            self.health = None
            self._legacy_loaded = True
            self._notice = (
                "Legacy CSV table loaded. Use the source picker for timestamped sessions."
            )

    def _begin_release(self) -> None:
        if self._releasing:
            return
        self.replay.set_busy()
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
            self.health = SourceHealthMonitor(self.source, clock=self.health_clock)
            self.session = TelemetrySession(
                self.source.schema,
                max_samples_per_signal=1000 if self.source.capabilities.is_live else None,
            )
            self.source.open()
            self.source.start()
            self._legacy_loaded = False
            self._label = self._last.label if self._last else self.source.source_id
            self.log_path = self._last.path if self._last else None
            self.window.display_telemetry_source()
            # A CSV replay may say which rows its import skipped.
            self._notice = getattr(self.source, "import_summary", "")
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
        replay_snapshot = None
        if (
            self._preparing is not None
            and self._preparation_cancel_started is not None
            and monotonic() - self._preparation_cancel_started >= 2
        ):
            self._notice = (
                "Preparation cleanup is still pending. "
                "Waiting for the adapter to release its worker."
            )
        progress = self._progress
        if self._preparing is not None and progress and not self._cancel_preparation:
            done, total = progress
            if total:
                self._notice = (
                    f"Preparing {self._last.label if self._last else 'source'} - "
                    f"{min(100, done * 100 // total)}% read - Disconnect cancels."
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
                if self.source is not None:
                    self._record_final_health(self.source)
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
            observer = self.health.observe_events if self.health is not None else None
            self.session.ingest_source(self.source, limit=512, observer=observer)
            snapshot = self.session.snapshot()
            replay_snapshot = snapshot
            finished = self.source.join(0) and self.source.events.stats.buffered == 0
            if snapshot.revision != self._revision or finished:
                self._revision = snapshot.revision
                self._finished_rendered = finished
                self._render()
                if finished and self.window.right_panel.time_plot.timer.isActive():
                    self.window.right_panel.time_plot.refresh()
        self.replay.update(
            self.session,
            snapshot=replay_snapshot,
            live=self.source.capabilities.is_live if self.source is not None else None,
            ready=(
                self.source is None or self._finished_rendered or self.source.capabilities.is_live
            ),
            connected=(
                self.source is not None
                and self.source.state
                in {
                    SourceState.OPEN,
                    SourceState.OPENING,
                    SourceState.RUNNING,
                }
            ),
            busy=self._preparing is not None or self._releasing or self._closing,
        )
        self.window.analysis.set_source_health(self.health.report() if self.health else None)
        self._present()
        if (
            self._closing
            and self.source is None
            and self._preparing is None
            and not self._releasing
        ):
            self.timer.stop()
            self.window.close()

    def _record_final_health(self, source: TelemetrySource) -> None:
        """Keep the released source's last health so analysis sees it disconnected."""
        session = self.session
        if session is None or self._closing:
            return
        try:
            session.ingest((), source_health={source.source_id: source.health()})
        except ValueError:
            return  # The session belongs to a different source (failed start).
        self.window.analysis.set_snapshot(session, session.snapshot())

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
        report = self.window.analysis.source_health
        picker.show_health(
            summary_text(report.health) if report else None,
            report.health.status.value if report else "unknown",
        )

    def select(self, index: int) -> None:
        self._selected = index
        if self.session is not None:
            self._render()
            self.replay.present()

    def select_signal(self, identifier: str) -> None:
        if self.session is None or not identifier:
            return
        self._selected = next(
            (i for i, d in enumerate(self.session.schema.signals) if d.identifier == identifier), -1
        )
        self._render()
        self.replay.present()

    def _render(self) -> None:
        if self.session is None:
            return
        snapshot = self.session.snapshot()
        self.window.workspace_docks.update_snapshot(snapshot)
        self.window.analysis.set_snapshot(self.session, snapshot, log_path=self.log_path)
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
            self.window.analysis.set_selected(None)
            return
        descriptor = descriptors[self._selected]
        self.window.analysis.set_selected(descriptor.identifier)
        browser.select_id(descriptor.identifier)
        records = snapshot.series[descriptor.identifier]
        # The table shows a fixed number of original records independently of the plot window.
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
        if descriptor.data_type not in {"numeric", "boolean"} or descriptor.shape:
            panel._reset_plot()
            panel.plot_widget.setTitle(
                f"{descriptor.display_name} - {descriptor.data_type} (inspect values below)"
            )
            return
        if not records:
            panel._reset_plot()
            panel.plot_widget.setTitle(f"{descriptor.display_name} - waiting for readings")
            return
        if self.source and not self.source.capabilities.is_live and not self._finished_rendered:
            panel._reset_plot()
            panel.plot_widget.setTitle("Loading measurements")
            return
        identity = (self.session, descriptor.identifier)
        if panel.time_plot.identity != identity:
            panel._reset_plot()
        panel.time_plot.set_trace(
            identity,
            descriptor.display_name,
            descriptor.canonical_unit,
            (records[0].sample.timestamp_ns, records[-1].sample.timestamp_ns),
            lambda start, end: snapshot.range(descriptor.identifier, start, end, neighbors=True),
        )
        self.window.analysis_docks.follow_plot(panel.plot_widget, panel.time_plot.origin_ns)
        panel.fit_plot.show()
