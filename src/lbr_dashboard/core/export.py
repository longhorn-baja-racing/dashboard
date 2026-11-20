"""Export selected raw telemetry samples to CSV with a JSON metadata sidecar.

The export is a faithful copy of raw samples: every signal keeps its own
timestamps, nothing is aligned, resampled, interpolated, or derived. See
docs/export.md for the file formats.

Intervals are CLOSED: a sample at exactly ``start_ns`` or ``end_ns`` is
exported. ``None`` for either bound means "unbounded on that side".

``snapshot_signals()`` builds the export input from a session snapshot, so the
app exports the session's raw records and not arrays owned by a widget.
"""

from __future__ import annotations

import csv
import hashlib
import io
import json
import math
import os
import secrets
from bisect import bisect_left, bisect_right
from collections.abc import Callable, Mapping, Sequence
from contextlib import suppress
from dataclasses import dataclass
from itertools import pairwise
from numbers import Real
from pathlib import Path
from typing import Any, BinaryIO

from .lifecycle import CancellationToken
from .session import SessionRecord, SessionSnapshot
from .signals import SignalDescriptor

FORMAT_NAME = "lbr-dashboard-raw-csv"
FORMAT_VERSION = 2
CSV_COLUMNS = ("signal_id", "time_ns", "time_s", "value", "unit", "available", "quality")
UNSPECIFIED_FRAME = "unspecified"
# Descriptor keys that name a signal's coordinate or reference frame, in lookup order.
FRAME_KEYS = ("frame", "coordinate_frame", "reference_frame")
METADATA_SUFFIX = ".meta.json"

# Rows written between cancellation checks and progress reports.
CHUNK_ROWS = 10_000

ProgressCallback = Callable[[int, int], None]


class ExportError(Exception):
    """Raised when an export cannot be written; no new output is left behind."""


@dataclass(frozen=True, slots=True)
class ExportSignal:
    """Raw samples of one signal to export, as parallel sequences.

    ``times_ns`` are integer nanoseconds in non-decreasing order. ``values``
    holds one value per timestamp; ``None`` and NaN both mean "missing".
    Values are usually numbers; text, booleans and JSON values (vectors,
    events) are written as text. ``available`` optionally marks readings the
    source reported as unavailable (their value is never written) and
    ``quality`` optionally holds the source's quality label per sample.
    ``frame`` names the coordinate or reference frame of the values.
    This is only an export input, not a signal model.
    """

    signal_id: str
    unit: str
    times_ns: Sequence[int]
    values: Sequence[object]
    available: Sequence[bool] | None = None
    quality: Sequence[str | None] | None = None
    frame: str = UNSPECIFIED_FRAME
    display_name: str | None = None
    data_type: str | None = None
    timestamp_domain: str | None = None

    def __post_init__(self) -> None:
        if not self.signal_id:
            raise ValueError("Export signal id must be a non-empty string")
        if len(self.times_ns) != len(self.values):
            raise ValueError(f"Signal {self.signal_id!r} has different time and value counts")
        for name in ("available", "quality"):
            column = getattr(self, name)
            if column is not None and len(column) != len(self.times_ns):
                raise ValueError(f"Signal {self.signal_id!r} has different time and {name} counts")
        if not self.frame:
            raise ValueError(f"Signal {self.signal_id!r} frame must be a non-empty string")
        if any(later < earlier for earlier, later in pairwise(self.times_ns)):
            raise ValueError(f"Signal {self.signal_id!r} timestamps are not non-decreasing")


@dataclass(frozen=True, slots=True)
class ExportResult:
    """Paths and row count of a completed export."""

    csv_path: Path
    metadata_path: Path
    row_count: int


def metadata_path_for(path: str | os.PathLike[str]) -> Path:
    """Return the metadata sidecar path for a CSV export path."""

    csv_path = Path(path)
    return csv_path.with_name(csv_path.name + METADATA_SUFFIX)


def export_csv(
    path: str | os.PathLike[str],
    signals: Sequence[ExportSignal],
    *,
    start_ns: int | None = None,
    end_ns: int | None = None,
    source: str | None = None,
    overwrite: bool = False,
    cancel: CancellationToken | None = None,
    progress: ProgressCallback | None = None,
) -> ExportResult:
    """Write the selected raw samples to ``path`` and metadata next to it.

    Rows are ordered by signal (in the given order), then by time; samples
    with equal timestamps keep their original order. The closed interval
    ``[start_ns, end_ns]`` selects samples. ``source`` is optional free-text
    provenance (for example the source log name) recorded in the metadata.

    Both files are written to temporary files and moved into place only after
    everything succeeded. Raises ``ExportError`` if an output exists and
    ``overwrite`` is False, or if writing fails; raises
    ``CancellationRequested`` if ``cancel`` is triggered. In every failure
    case no new output is left and existing files are unchanged.
    """

    if not signals:
        raise ValueError("Select at least one signal to export")
    if len({signal.signal_id for signal in signals}) != len(signals):
        raise ValueError("Export signal ids must be unique")
    if start_ns is not None and end_ns is not None and start_ns > end_ns:
        raise ValueError("Export interval start must not be after its end")

    csv_path = Path(path)
    meta_path = metadata_path_for(csv_path)
    targets = (meta_path, csv_path)
    if not overwrite:
        _reject_existing(targets)

    ranges = [_selected_range(signal.times_ns, start_ns, end_ns) for signal in signals]
    total = sum(stop - begin for begin, stop in ranges)
    temps: list[Path] = []
    try:
        csv_temp, csv_file = _open_temp(csv_path)
        temps.append(csv_temp)
        with csv_file:
            digest, summaries = _write_rows(csv_file, signals, ranges, total, cancel, progress)
            _sync(csv_file)
        metadata = {
            "format": FORMAT_NAME,
            "format_version": FORMAT_VERSION,
            "csv_file": csv_path.name,
            "csv_sha256": digest,
            "encoding": "utf-8",
            "columns": list(CSV_COLUMNS),
            "row_count": total,
            "row_order": "signal order as listed, then time; equal times keep source order",
            "time": {
                "column": "time_ns",
                "unit": "ns",
                "reference": "source log clock, exported unchanged",
                "time_s": "exact decimal copy of time_ns / 1e9",
            },
            "interval": {
                "start_ns": start_ns,
                "end_ns": end_ns,
                "boundaries": "closed",
                "scope": "all samples"
                if start_ns is None and end_ns is None
                else "samples inside the interval",
            },
            "missing_value": "empty value cell with available false",
            "available": "true when the sample has a value; false when the source "
            "reported it unavailable or the value is NaN",
            "quality": "quality label reported by the source; empty when none",
            "frame": "per signal in signals[].frame; 'unspecified' when the source gives none",
            "alignment": "none",
            "resampling": "none",
            "derived_operations": [],
            "source": source,
            "signals": summaries,
        }
        text = json.dumps(metadata, indent=2, ensure_ascii=False) + "\n"
        meta_temp, meta_file = _open_temp(meta_path)
        temps.append(meta_temp)
        with meta_file:
            meta_file.write(text.encode("utf-8"))
            _sync(meta_file)
        if cancel is not None:
            cancel.raise_if_cancelled()
        _commit(list(zip((meta_temp, csv_temp), targets)), overwrite=overwrite)
    except OSError as exc:
        raise ExportError(f"Could not write export {csv_path}: {exc}") from exc
    finally:
        for temp in temps:
            _remove_quietly(temp)
    return ExportResult(csv_path, meta_path, total)


def write_file_atomically(
    path: str | os.PathLike[str], data: bytes, *, overwrite: bool = False
) -> None:
    """Write ``data`` to ``path`` through a synced temporary file and a rename.

    Raises ``ExportError`` if ``path`` exists and ``overwrite`` is False, or
    if writing fails; an existing file is then left unchanged.
    """

    target = Path(path)
    if not overwrite:
        _reject_existing((target,))
    temp: Path | None = None
    try:
        temp, file = _open_temp(target)
        with file:
            file.write(data)
            _sync(file)
        _commit([(temp, target)], overwrite=overwrite)
    except OSError as exc:
        raise ExportError(f"Could not write {target}: {exc}") from exc
    finally:
        if temp is not None:
            _remove_quietly(temp)


def _selected_range(
    times_ns: Sequence[int], start_ns: int | None, end_ns: int | None
) -> tuple[int, int]:
    begin = 0 if start_ns is None else bisect_left(times_ns, start_ns)
    stop = len(times_ns) if end_ns is None else bisect_right(times_ns, end_ns)
    return begin, stop


def _write_rows(
    file: BinaryIO,
    signals: Sequence[ExportSignal],
    ranges: Sequence[tuple[int, int]],
    total: int,
    cancel: CancellationToken | None,
    progress: ProgressCallback | None,
) -> tuple[str, list[dict[str, Any]]]:
    digest = hashlib.sha256()
    buffer = io.StringIO()
    writer = csv.writer(buffer, lineterminator="\r\n")
    done = 0
    reported = -1

    def flush() -> None:
        nonlocal reported
        data = buffer.getvalue().encode("utf-8")
        buffer.seek(0)
        buffer.truncate()
        digest.update(data)
        file.write(data)
        if cancel is not None:
            cancel.raise_if_cancelled()
        if progress is not None and done != reported:
            reported = done
            progress(done, total)

    writer.writerow(CSV_COLUMNS)
    summaries: list[dict[str, Any]] = []
    for signal, (begin, stop) in zip(signals, ranges):
        missing = 0
        flags, labels = signal.available, signal.quality
        for index in range(begin, stop):
            time_ns = signal.times_ns[index]
            raw = signal.values[index]
            present = (flags is None or flags[index]) and not _is_missing(raw)
            if not present:
                missing += 1
            writer.writerow(
                (
                    signal.signal_id,
                    time_ns,
                    _seconds_text(time_ns),
                    _value_text(raw) if present else "",
                    signal.unit,
                    "true" if present else "false",
                    (labels[index] if labels is not None else None) or "",
                )
            )
            done += 1
            if done % CHUNK_ROWS == 0:
                flush()
        summary: dict[str, Any] = {"signal_id": signal.signal_id}
        if signal.display_name is not None:
            summary["display_name"] = signal.display_name
        if signal.data_type is not None:
            summary["data_type"] = signal.data_type
        summary["unit"] = signal.unit
        summary["frame"] = signal.frame
        if signal.timestamp_domain is not None:
            summary["timestamp_domain"] = signal.timestamp_domain
        summary.update(
            {
                "sample_count": stop - begin,
                "missing_count": missing,
                "first_time_ns": signal.times_ns[begin] if stop > begin else None,
                "last_time_ns": signal.times_ns[stop - 1] if stop > begin else None,
            }
        )
        summaries.append(summary)
    flush()
    return digest.hexdigest(), summaries


def _is_missing(value: object) -> bool:
    return value is None or (isinstance(value, float) and math.isnan(value))


def _value_text(value: object) -> str:
    """Return exact text for a present value.

    Numbers use the shortest text that reads back as the same 64-bit float.
    Infinite values are raw data, not missing, and are written as ``inf`` or
    ``-inf``. Booleans are ``true``/``false``, text is written unchanged and
    other JSON values (vectors, event payloads) as compact JSON.
    """

    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, Real):
        return repr(float(value))
    if isinstance(value, str):
        return value
    return json.dumps(_plain_json(value), ensure_ascii=False, separators=(",", ":"))


def _plain_json(value: object) -> object:
    if isinstance(value, Mapping):
        return {str(key): _plain_json(item) for key, item in value.items()}
    if isinstance(value, (tuple, list)):
        return [_plain_json(item) for item in value]
    return value


def signal_frame(descriptor: SignalDescriptor) -> str:
    """Return the frame named by a descriptor, or ``"unspecified"``.

    Looks for a non-empty text value under ``FRAME_KEYS`` in the descriptor's
    ``extra``, ``display`` and ``calibration`` metadata, in that order.
    """

    for mapping in (descriptor.extra, descriptor.display, descriptor.calibration):
        for key in FRAME_KEYS:
            value = mapping.get(key)
            if isinstance(value, str) and value.strip():
                return value.strip()
    return UNSPECIFIED_FRAME


def snapshot_signals(
    snapshot: SessionSnapshot,
    signal_ids: Sequence[str],
    *,
    start_ns: int | None = None,
    end_ns: int | None = None,
) -> list[ExportSignal]:
    """Return the raw records of ``signal_ids`` in ``snapshot`` as export input.

    Only records inside the closed interval are taken (``None`` = unbounded).
    Values, availability and quality are copied from each sample unchanged;
    unit, frame, name and type come from the signal descriptor. Raises
    ``ValueError`` for an id that the snapshot does not contain.
    """

    descriptors = {item.identifier: item for item in snapshot.schema.signals}
    signals: list[ExportSignal] = []
    for signal_id in signal_ids:
        descriptor = descriptors.get(signal_id)
        if descriptor is None or signal_id not in snapshot.series:
            raise ValueError(f"Signal {signal_id!r} is not in the session")
        samples = [record.sample for record in _records_in(snapshot, signal_id, start_ns, end_ns)]
        signals.append(
            ExportSignal(
                signal_id,
                descriptor.canonical_unit or "",
                [sample.timestamp_ns for sample in samples],
                [sample.value for sample in samples],
                available=[sample.available for sample in samples],
                quality=[sample.quality for sample in samples],
                frame=signal_frame(descriptor),
                display_name=descriptor.display_name,
                data_type=descriptor.data_type,
                timestamp_domain=descriptor.timestamp_domain,
            )
        )
    return signals


def _records_in(
    snapshot: SessionSnapshot, signal_id: str, start_ns: int | None, end_ns: int | None
) -> Sequence[SessionRecord]:
    records = snapshot.series[signal_id]
    if (start_ns is None and end_ns is None) or not records:
        return records
    first = records[0].sample.timestamp_ns if start_ns is None else start_ns
    last = records[-1].sample.timestamp_ns if end_ns is None else end_ns
    return snapshot.range(signal_id, first, last) if first <= last else ()


def _seconds_text(time_ns: int) -> str:
    """Return ``time_ns`` in seconds as exact decimal text with 9 fraction digits."""

    whole, fraction = divmod(abs(time_ns), 1_000_000_000)
    return f"{'-' if time_ns < 0 else ''}{whole}.{fraction:09d}"


def _reject_existing(paths: Sequence[Path]) -> None:
    for path in paths:
        if os.path.lexists(path):
            raise ExportError(f"{path} already exists; choose overwrite to replace it")


def _open_temp(target: Path) -> tuple[Path, BinaryIO]:
    """Create a new hidden temporary file next to ``target`` (normal permissions)."""

    temp = target.with_name(f".{target.name}.{secrets.token_hex(8)}.tmp")
    return temp, open(temp, "xb")


def _sync(file: BinaryIO) -> None:
    file.flush()
    os.fsync(file.fileno())


def _commit(moves: Sequence[tuple[Path, Path]], *, overwrite: bool) -> None:
    """Move every temporary file onto its target, or restore the old targets.

    Existing targets are first renamed to backups so that a failure part way
    (for example a target held open by another program on Windows) can put
    every original file back.
    """

    if not overwrite:
        _reject_existing([target for _, target in moves])
    backups: list[tuple[Path, Path]] = []
    placed: list[Path] = []
    try:
        for _, target in moves:
            if os.path.lexists(target):
                backup, file = _open_temp(target)
                file.close()
                try:
                    os.replace(target, backup)
                except OSError:
                    _remove_quietly(backup)
                    raise
                backups.append((backup, target))
        for temp, target in moves:
            os.replace(temp, target)
            placed.append(target)
    except BaseException:
        for target in placed:
            _remove_quietly(target)
        for backup, target in backups:
            with suppress(OSError):  # If this fails the original stays in the backup file.
                os.replace(backup, target)
        raise
    for backup, _ in backups:
        _remove_quietly(backup)


def _remove_quietly(path: Path) -> None:
    with suppress(OSError):
        os.remove(path)
