"""Export selected raw telemetry samples to CSV with a JSON metadata sidecar.

The export is a faithful copy of raw samples: every signal keeps its own
timestamps, nothing is aligned, resampled, interpolated, or derived. See
docs/export.md for the file formats.

Intervals are CLOSED: a sample at exactly ``start_ns`` or ``end_ns`` is
exported. ``None`` for either bound means "unbounded on that side".
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
from collections.abc import Callable, Sequence
from contextlib import suppress
from dataclasses import dataclass
from itertools import pairwise
from pathlib import Path
from typing import Any, BinaryIO

from .lifecycle import CancellationToken

FORMAT_NAME = "lbr-dashboard-raw-csv"
FORMAT_VERSION = 1
CSV_COLUMNS = ("signal_id", "time_ns", "time_s", "value", "unit")
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
    This is only an export input, not a signal model.
    """

    signal_id: str
    unit: str
    times_ns: Sequence[int]
    values: Sequence[float | None]

    def __post_init__(self) -> None:
        if not self.signal_id:
            raise ValueError("Export signal id must be a non-empty string")
        if len(self.times_ns) != len(self.values):
            raise ValueError(f"Signal {self.signal_id!r} has different time and value counts")
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
            "interval": {"start_ns": start_ns, "end_ns": end_ns, "boundaries": "closed"},
            "missing_value": "empty value cell",
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
        for index in range(begin, stop):
            time_ns = signal.times_ns[index]
            value = _value_text(signal.values[index])
            if not value:
                missing += 1
            writer.writerow((signal.signal_id, time_ns, _seconds_text(time_ns), value, signal.unit))
            done += 1
            if done % CHUNK_ROWS == 0:
                flush()
        summaries.append(
            {
                "signal_id": signal.signal_id,
                "unit": signal.unit,
                "sample_count": stop - begin,
                "missing_count": missing,
                "first_time_ns": signal.times_ns[begin] if stop > begin else None,
                "last_time_ns": signal.times_ns[stop - 1] if stop > begin else None,
            }
        )
    flush()
    return digest.hexdigest(), summaries


def _value_text(value: float | None) -> str:
    """Return the shortest exact text for a value; missing becomes empty.

    Infinite values are raw data, not missing, and are written as ``inf`` or
    ``-inf``.
    """

    if value is None:
        return ""
    number = float(value)
    return "" if math.isnan(number) else repr(number)


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
