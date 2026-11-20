"""Record a live serial source to an ``.lbrlog`` file from the source picker.

The picker's Record button asks for a destination, creates a
:class:`LiveRecorder` and attaches it to the running serial source; the source
worker hands it every received record and the recorder writes them on its own
thread. The owner thread only polls status for the size/duration line. A
recording stops (and is finalized with an ``END`` record) when the user stops
it, the source disconnects or ends, another source is chosen, or the window
closes. A write failure stops the recording only; live reception continues.
"""

from __future__ import annotations

from collections.abc import Callable
from datetime import datetime, timezone
from pathlib import Path
from time import monotonic
from typing import TYPE_CHECKING

from PySide6.QtCore import QObject
from PySide6.QtWidgets import QFileDialog

from .core import SourceState
from .importers.lbrlog_writer import LiveRecorder, RecordingError, RecordingStatus
from .importers.serial_source import SerialLiveSource
from .version import __version__

if TYPE_CHECKING:
    from .source_controller import SourceController

_ACTIVE = {SourceState.OPEN, SourceState.OPENING, SourceState.RUNNING}
LBRLOG_FILTER = "Firmware logs (*.lbrlog)"


def format_size(size: int) -> str:
    if size < 1024:
        return f"{size} B"
    if size < 1024 * 1024:
        return f"{size / 1024:.1f} KB"
    return f"{size / (1024 * 1024):.1f} MB"


def format_span(seconds: float) -> str:
    whole = int(seconds)
    hours, rest = divmod(whole, 3600)
    minutes, secs = divmod(rest, 60)
    return f"{hours}:{minutes:02d}:{secs:02d}" if hours else f"{minutes}:{secs:02d}"


def _loss_text(status: RecordingStatus) -> str:
    parts = []
    if status.lost_packets:
        parts.append(f"{status.lost_packets} packets lost on the link")
    if status.rejected_bytes:
        parts.append(f"{status.rejected_bytes} bytes of bad frames")
    if status.dropped_records:
        parts.append(f"{status.dropped_records} records dropped (disk too slow)")
    return "loss marked in file: " + ", ".join(parts) if parts else "no loss"


def describe(status: RecordingStatus) -> str:
    """One status line: file, size, duration, records and loss."""

    name = Path(status.path).name
    if status.state == "waiting":
        return f"Recording to {name}: waiting for the first packet."
    return (
        f"Recording to {name}: {format_size(status.bytes_written)}, "
        f"{format_span(status.elapsed_s)} elapsed, "
        f"{format_span(status.device_span_s)} of device time, "
        f"{status.records_written:,} records; {_loss_text(status)}."
    )


class RecordingController(QObject):
    """Owns at most one recording of the active serial source."""

    def __init__(self, owner: SourceController) -> None:
        super().__init__(owner)
        self.owner = owner
        self.picker = owner.window.source_picker
        self.recorder: LiveRecorder | None = None
        self.source: SerialLiveSource | None = None
        #: The recording line when no recording runs: the last result, or empty.
        self.message = ""
        #: Returns the destination, or None if the user cancelled; tests replace it.
        self.choose_path: Callable[[Path], Path | None] = self._ask_path
        self._shown = 0.0
        self.picker.record_toggled.connect(self.toggle)

    def _ask_path(self, suggested: Path) -> Path | None:
        name, _ = QFileDialog.getSaveFileName(
            self.owner.window, "Record live data to", str(suggested), LBRLOG_FILTER
        )
        return Path(name) if name else None

    def toggle(self, checked: bool) -> None:
        if checked:
            self.start()
        else:
            self.stop("stopped")

    def start(self) -> None:
        source = self.owner.source
        if self.recorder is not None:
            return
        if not isinstance(source, SerialLiveSource) or source.state not in _ACTIVE:
            self.message = "Connect a USB serial source before recording."
            self.present(force=True)
            return
        stamp = datetime.now()
        suggested = Path.home() / f"live-{stamp:%Y%m%d-%H%M%S}.lbrlog"
        path = self.choose_path(suggested)
        if path is None:
            self.present(force=True)
            return
        if path.suffix.lower() != ".lbrlog":
            path = path.with_name(path.name + ".lbrlog")
        started = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
        recorder = LiveRecorder(
            path,
            catalog_hash=source.catalog.hash,
            config={
                "host_capture": "usb_serial_live_stream",
                "host_port": source.port_name.replace("=", "_"),
                "host_started_utc": started,
                "host_app": f"lbr-dashboard {__version__}",
                "host_note": "received over the live link; not the SD card log",
            },
        )
        try:
            recorder.start()
        except RecordingError as error:
            self.message = f"Recording not started: {error}"
            self.present(force=True)
            return
        source.attach_recorder(recorder)
        self.recorder, self.source = recorder, source
        self.present(force=True)

    def stop(self, reason: str) -> None:
        """Detach and finalize; ``reason`` completes "Saved ... - <reason>"."""

        recorder, source = self.recorder, self.source
        if recorder is None:
            self.present(force=True)
            return
        self.recorder = self.source = None
        if source is not None:
            source.attach_recorder(None)
        closed = recorder.stop(timeout=2.0)
        status = recorder.status()
        name = Path(status.path).name
        if status.state == "failed":
            self.message = (
                f"Recording failed: {status.error}. Live view continues; "
                f"{format_size(status.bytes_written)} kept in {name}."
            )
        elif not closed:
            self.message = f"Recording of {name} is still being finalized; the file stays readable."
        elif status.bytes_written == 0:
            Path(status.path).unlink(missing_ok=True)
            self.message = f"Recording stopped ({reason}) before any packet arrived; nothing saved."
        else:
            self.message = (
                f"Saved {name} ({format_size(status.bytes_written)}, "
                f"{format_span(status.device_span_s)} of device time, "
                f"{status.records_written:,} records; {_loss_text(status)}) - {reason}. "
                "Open it with Firmware log (.lbrlog). It holds what the link delivered, "
                "not the SD card log."
            )
        self.present(force=True)

    def poll(self) -> None:
        recorder, source = self.recorder, self.source
        if recorder is not None:
            status = recorder.status()
            if status.state == "failed":
                self.stop("write failed")
                return
            if source is not self.owner.source or source is None or source.state not in _ACTIVE:
                self.stop("the source disconnected")
                return
        self.present()

    def present(self, *, force: bool = False) -> None:
        now = monotonic()
        if not force and now - self._shown < 0.25:
            return
        self._shown = now
        source = self.owner.source
        available = (
            isinstance(source, SerialLiveSource) and source.state in _ACTIVE and not self.owner.busy
        )
        recorder = self.recorder
        text = describe(recorder.status()) if recorder is not None else self.message
        self.picker.set_recording(available=available, active=recorder is not None, text=text)
