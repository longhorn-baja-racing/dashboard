"""Raw CSV export: selected content, boundaries, metadata, and safe finalization."""

from __future__ import annotations

import csv
import errno
import hashlib
import json
import os
from array import array
from decimal import Decimal
from pathlib import Path
from typing import Any

import pytest

from lbr_dashboard.core import export
from lbr_dashboard.core.export import (
    CSV_COLUMNS,
    ExportError,
    ExportSignal,
    export_csv,
    metadata_path_for,
    write_file_atomically,
)
from lbr_dashboard.core.lifecycle import CancellationRequested, CancellationToken

MS = 1_000_000


def _rpm() -> ExportSignal:
    times = array("q", [0, 10 * MS, 20 * MS, 30 * MS, 40 * MS])
    return ExportSignal(
        "engine_rpm", "1/min", times, array("d", [900.0, 950.5, 1000.25, 0.0, -1.5])
    )


def _temp() -> ExportSignal:
    # A slower signal with a gap between 25 ms and 100 ms.
    return ExportSignal("cvt_temp", "deg C", [0, 25 * MS, 100 * MS], [21.0, 21.5, 30.0])


def _rows(path: Path) -> list[list[str]]:
    with path.open(newline="", encoding="utf-8") as file:
        return list(csv.reader(file))


def _metadata(path: Path) -> dict[str, Any]:
    return json.loads(metadata_path_for(path).read_text(encoding="utf-8"))


def _row_keys(path: Path) -> list[tuple[str, int]]:
    return [(row[0], int(row[1])) for row in _rows(path)[1:]]


def test_reference_values_timestamps_and_units(tmp_path: Path) -> None:
    path = tmp_path / "run.csv"
    result = export_csv(path, [_rpm(), _temp()])

    assert result.csv_path == path
    assert result.metadata_path == tmp_path / "run.csv.meta.json"
    assert result.row_count == 8
    assert _rows(path) == [
        list(CSV_COLUMNS),
        ["engine_rpm", "0", "0.000000000", "900.0", "1/min"],
        ["engine_rpm", "10000000", "0.010000000", "950.5", "1/min"],
        ["engine_rpm", "20000000", "0.020000000", "1000.25", "1/min"],
        ["engine_rpm", "30000000", "0.030000000", "0.0", "1/min"],
        ["engine_rpm", "40000000", "0.040000000", "-1.5", "1/min"],
        ["cvt_temp", "0", "0.000000000", "21.0", "deg C"],
        ["cvt_temp", "25000000", "0.025000000", "21.5", "deg C"],
        ["cvt_temp", "100000000", "0.100000000", "30.0", "deg C"],
    ]
    assert path.read_bytes().count(b"\r\n") == 9


def test_mixed_rates_and_gaps_keep_native_timestamps(tmp_path: Path) -> None:
    path = tmp_path / "mixed.csv"
    export_csv(path, [_rpm(), _temp()])

    # No alignment rows: each signal appears only at its own sample times.
    keys = _row_keys(path)
    assert [t for s, t in keys if s == "cvt_temp"] == [0, 25 * MS, 100 * MS]
    assert [t for s, t in keys if s == "engine_rpm"] == [0, 10 * MS, 20 * MS, 30 * MS, 40 * MS]
    metadata = _metadata(path)
    assert metadata["alignment"] == "none"
    assert metadata["resampling"] == "none"
    assert metadata["derived_operations"] == []


def test_closed_interval_includes_samples_exactly_at_both_edges(tmp_path: Path) -> None:
    path = tmp_path / "edges.csv"
    export_csv(path, [_rpm(), _temp()], start_ns=10 * MS, end_ns=25 * MS)

    assert _row_keys(path) == [
        ("engine_rpm", 10 * MS),
        ("engine_rpm", 20 * MS),
        ("cvt_temp", 25 * MS),
    ]
    metadata = _metadata(path)
    assert metadata["interval"] == {"start_ns": 10 * MS, "end_ns": 25 * MS, "boundaries": "closed"}
    summaries = {item["signal_id"]: item for item in metadata["signals"]}
    assert summaries["engine_rpm"]["first_time_ns"] == 10 * MS
    assert summaries["engine_rpm"]["last_time_ns"] == 20 * MS
    assert summaries["cvt_temp"]["sample_count"] == 1


def test_half_open_and_empty_intervals(tmp_path: Path) -> None:
    export_csv(tmp_path / "from.csv", [_rpm()], start_ns=30 * MS)
    export_csv(tmp_path / "until.csv", [_rpm()], end_ns=0)
    export_csv(tmp_path / "empty.csv", [_rpm()], start_ns=11 * MS, end_ns=19 * MS)

    assert [t for _, t in _row_keys(tmp_path / "from.csv")] == [30 * MS, 40 * MS]
    assert [t for _, t in _row_keys(tmp_path / "until.csv")] == [0]
    assert _rows(tmp_path / "empty.csv") == [list(CSV_COLUMNS)]
    summary = _metadata(tmp_path / "empty.csv")["signals"][0]
    assert summary == {
        "signal_id": "engine_rpm",
        "unit": "1/min",
        "sample_count": 0,
        "missing_count": 0,
        "first_time_ns": None,
        "last_time_ns": None,
    }


def test_only_selected_signals_are_exported_in_selection_order(tmp_path: Path) -> None:
    path = tmp_path / "selected.csv"
    export_csv(path, [_temp()])
    assert {row[0] for row in _rows(path)[1:]} == {"cvt_temp"}

    export_csv(path, [_temp(), _rpm()], overwrite=True)
    ids = [row[0] for row in _rows(path)[1:]]
    assert ids == ["cvt_temp"] * 3 + ["engine_rpm"] * 5
    assert [item["signal_id"] for item in _metadata(path)["signals"]] == ["cvt_temp", "engine_rpm"]


def test_duplicate_timestamps_keep_source_order(tmp_path: Path) -> None:
    path = tmp_path / "dup.csv"
    signal = ExportSignal("switch", "", [5, 5, 5, 6], [3.0, 1.0, 2.0, 4.0])
    export_csv(path, [signal], start_ns=5, end_ns=5)
    assert [(row[1], row[3]) for row in _rows(path)[1:]] == [
        ("5", "3.0"),
        ("5", "1.0"),
        ("5", "2.0"),
    ]


def test_missing_values_are_empty_cells_and_never_zero(tmp_path: Path) -> None:
    path = tmp_path / "missing.csv"
    signal = ExportSignal(
        "speed", "km/h", [1, 2, 3, 4, 5], [None, float("nan"), 0.0, float("inf"), -float("inf")]
    )
    export_csv(path, [signal])

    assert [row[3] for row in _rows(path)[1:]] == ["", "", "0.0", "inf", "-inf"]
    summary = _metadata(path)["signals"][0]
    assert summary["sample_count"] == 5
    assert summary["missing_count"] == 2
    assert _metadata(path)["missing_value"] == "empty value cell"


def test_values_round_trip_exactly(tmp_path: Path) -> None:
    path = tmp_path / "exact.csv"
    values = [0.1, 1 / 3, 1e-300, 2.0**53 + 2, -0.0, 5]
    export_csv(path, [ExportSignal("x", "", list(range(len(values))), values)])
    assert [float(row[3]) for row in _rows(path)[1:]] == [float(value) for value in values]
    assert _rows(path)[-1][3] == "5.0"


def test_reading_back_gives_exact_nanosecond_times(tmp_path: Path) -> None:
    path = tmp_path / "times.csv"
    times = [-1_500_000_001, -1, 0, 1, 1_700_000_000_123_456_789]
    export_csv(path, [ExportSignal("t", "s", times, [1.0] * len(times))])

    rows = _rows(path)[1:]
    assert [int(row[1]) for row in rows] == times
    assert [row[2] for row in rows] == [
        "-1.500000001",
        "-0.000000001",
        "0.000000000",
        "0.000000001",
        "1700000000.123456789",
    ]
    assert [Decimal(row[2]) * 10**9 for row in rows] == [Decimal(t) for t in times]


def test_csv_quoting_of_ids_and_units(tmp_path: Path) -> None:
    path = tmp_path / "quoted.csv"
    signal_id = 'wheel, "front" left'
    export_csv(path, [ExportSignal(signal_id, "m/s, avg", [0], [1.0])])
    assert _rows(path)[1] == [signal_id, "0", "0.000000000", "1.0", "m/s, avg"]


def test_unicode_path_ids_and_units(tmp_path: Path) -> None:
    folder = tmp_path / ("r" + chr(0xE9) + "sultats " + chr(0x6E2C) + chr(0x5B9A))
    folder.mkdir()
    path = folder / ("essai " + chr(0xE9) + ".csv")
    unit = chr(0xB0) + "C"
    signal_id = "temp" + chr(0x2082)
    export_csv(path, [ExportSignal(signal_id, unit, [0], [20.0])])

    assert not path.read_bytes().startswith(b"\xef\xbb\xbf")  # no byte order mark
    assert _rows(path)[1] == [signal_id, "0", "0.000000000", "20.0", unit]
    assert _metadata(path)["signals"][0]["unit"] == unit
    assert _metadata(path)["csv_file"] == path.name


def test_metadata_describes_the_csv_and_is_reproducible(tmp_path: Path) -> None:
    first = tmp_path / "a" / "run.csv"
    second = tmp_path / "b" / "run.csv"
    first.parent.mkdir()
    second.parent.mkdir()
    export_csv(first, [_rpm(), _temp()], start_ns=0, source="session-07.csv")
    export_csv(second, [_rpm(), _temp()], start_ns=0, source="session-07.csv")

    assert first.read_bytes() == second.read_bytes()
    assert metadata_path_for(first).read_bytes() == metadata_path_for(second).read_bytes()
    metadata = _metadata(first)
    assert metadata["format"] == "lbr-dashboard-raw-csv"
    assert metadata["format_version"] == 1
    assert metadata["csv_sha256"] == hashlib.sha256(first.read_bytes()).hexdigest()
    assert metadata["columns"] == list(CSV_COLUMNS)
    assert metadata["row_count"] == 8
    assert metadata["time"]["unit"] == "ns"
    assert metadata["source"] == "session-07.csv"
    assert metadata["interval"]["end_ns"] is None


def test_existing_files_are_kept_without_overwrite(tmp_path: Path) -> None:
    path = tmp_path / "run.csv"
    path.write_bytes(b"old csv")
    with pytest.raises(ExportError, match="already exists"):
        export_csv(path, [_rpm()])
    assert path.read_bytes() == b"old csv"
    assert sorted(item.name for item in tmp_path.iterdir()) == ["run.csv"]

    path.unlink()
    metadata_path_for(path).write_bytes(b"old meta")
    with pytest.raises(ExportError, match="already exists"):
        export_csv(path, [_rpm()])
    assert not path.exists()
    assert metadata_path_for(path).read_bytes() == b"old meta"


def test_overwrite_replaces_both_files_and_leaves_no_temporaries(tmp_path: Path) -> None:
    path = tmp_path / "run.csv"
    path.write_bytes(b"old csv")
    metadata_path_for(path).write_bytes(b"old meta")
    export_csv(path, [_temp()], overwrite=True)

    assert len(_rows(path)) == 4
    assert _metadata(path)["row_count"] == 3
    assert sorted(item.name for item in tmp_path.iterdir()) == ["run.csv", "run.csv.meta.json"]


def _existing_pair(tmp_path: Path) -> Path:
    path = tmp_path / "run.csv"
    path.write_bytes(b"old csv")
    metadata_path_for(path).write_bytes(b"old meta")
    return path


def _assert_old_pair_intact(path: Path) -> None:
    assert path.read_bytes() == b"old csv"
    assert metadata_path_for(path).read_bytes() == b"old meta"
    assert sorted(item.name for item in path.parent.iterdir()) == ["run.csv", "run.csv.meta.json"]


def _disk_full(*_: object) -> None:
    raise OSError(errno.ENOSPC, "No space left on device")


def test_disk_full_while_writing_leaves_no_partial_output(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    real_open = open

    class FullFile:
        def __init__(self, path: Path, mode: str) -> None:
            self._file = real_open(path, mode)

        def __enter__(self) -> FullFile:
            return self

        def __exit__(self, *_: object) -> None:
            self._file.close()

        def write(self, data: bytes) -> int:
            _disk_full()
            return 0  # pragma: no cover

        def close(self) -> None:
            self._file.close()

    monkeypatch.setattr(export, "open", FullFile, raising=False)
    path = _existing_pair(tmp_path)
    with pytest.raises(ExportError, match="No space left") as caught:
        export_csv(path, [_rpm()], overwrite=True)
    assert isinstance(caught.value.__cause__, OSError)
    _assert_old_pair_intact(path)

    fresh = tmp_path / "fresh" / "new.csv"
    fresh.parent.mkdir()
    with pytest.raises(ExportError):
        export_csv(fresh, [_rpm()])
    assert list(fresh.parent.iterdir()) == []


def test_failed_sync_leaves_no_partial_output(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = _existing_pair(tmp_path)
    monkeypatch.setattr(export.os, "fsync", _disk_full)
    with pytest.raises(ExportError, match="No space left"):
        export_csv(path, [_rpm()], overwrite=True)
    _assert_old_pair_intact(path)


@pytest.mark.parametrize("failing_target", [".meta.json", ".csv"])
def test_failed_final_rename_restores_the_previous_files(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, failing_target: str
) -> None:
    """A target locked by another program must not leave a mismatched pair."""

    real_replace = os.replace
    failures: list[str] = []

    def replace(source: str | Path, target: str | Path) -> None:
        # Fail only the first move of new data; the restore of a backup succeeds.
        if not failures and str(target).endswith(failing_target) and str(source).endswith(".tmp"):
            failures.append(str(target))
            raise PermissionError(errno.EACCES, "File is in use")
        real_replace(source, target)

    path = _existing_pair(tmp_path)
    monkeypatch.setattr(export.os, "replace", replace)
    with pytest.raises(ExportError, match="in use"):
        export_csv(path, [_rpm()], overwrite=True)
    assert len(failures) == 1
    _assert_old_pair_intact(path)


def test_locked_existing_file_fails_before_anything_changes(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    real_replace = os.replace

    def replace(source: str | Path, target: str | Path) -> None:
        if str(source).endswith("run.csv"):
            raise PermissionError(errno.EACCES, "File is in use")
        real_replace(source, target)

    path = _existing_pair(tmp_path)
    monkeypatch.setattr(export.os, "replace", replace)
    with pytest.raises(ExportError, match="in use"):
        export_csv(path, [_rpm()], overwrite=True)
    _assert_old_pair_intact(path)


def test_missing_folder_is_an_export_error(tmp_path: Path) -> None:
    with pytest.raises(ExportError):
        export_csv(tmp_path / "missing" / "run.csv", [_rpm()])


def test_cancellation_midway_leaves_nothing(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(export, "CHUNK_ROWS", 2)
    token = CancellationToken()
    calls: list[tuple[int, int]] = []

    def progress(done: int, total: int) -> None:
        calls.append((done, total))
        token.cancel()

    with pytest.raises(CancellationRequested):
        export_csv(tmp_path / "run.csv", [_rpm(), _temp()], cancel=token, progress=progress)
    assert calls == [(2, 8)]
    assert list(tmp_path.iterdir()) == []


def test_cancellation_after_last_row_keeps_existing_files(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = _existing_pair(tmp_path)
    token = CancellationToken()

    def progress(done: int, total: int) -> None:
        if done == total:
            token.cancel()

    with pytest.raises(CancellationRequested):
        export_csv(path, [_rpm()], overwrite=True, cancel=token, progress=progress)
    _assert_old_pair_intact(path)


@pytest.mark.parametrize(
    ("rows", "expected"),
    [(5, [(2, 5), (4, 5), (5, 5)]), (4, [(2, 4), (4, 4)]), (0, [(0, 0)])],
)
def test_progress_reports_rows_written(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, rows: int, expected: list[tuple[int, int]]
) -> None:
    monkeypatch.setattr(export, "CHUNK_ROWS", 2)
    calls: list[tuple[int, int]] = []
    signal = ExportSignal("x", "", list(range(rows)), [1.0] * rows)
    result = export_csv(tmp_path / "run.csv", [signal], progress=lambda *args: calls.append(args))
    assert calls == expected
    assert result.row_count == rows


@pytest.mark.parametrize(
    ("signals", "start_ns", "message"),
    [
        pytest.param([], None, "at least one", id="none"),
        pytest.param([_temp(), _temp()], None, "unique", id="duplicate"),
        pytest.param([_temp()], 2, "start", id="reversed"),
    ],
)
def test_invalid_requests_are_rejected(
    tmp_path: Path, signals: list[ExportSignal], start_ns: int | None, message: str
) -> None:
    with pytest.raises(ValueError, match=message):
        export_csv(tmp_path / "run.csv", signals, start_ns=start_ns, end_ns=1)
    assert list(tmp_path.iterdir()) == []


def test_invalid_signals_are_rejected() -> None:
    with pytest.raises(ValueError, match="non-empty"):
        ExportSignal("", "", [], [])
    with pytest.raises(ValueError, match="counts"):
        ExportSignal("x", "", [1, 2], [1.0])
    with pytest.raises(ValueError, match="non-decreasing"):
        ExportSignal("x", "", [2, 1], [1.0, 2.0])


def test_write_file_atomically(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    path = tmp_path / "plot.png"
    write_file_atomically(path, b"first")
    with pytest.raises(ExportError, match="already exists"):
        write_file_atomically(path, b"second")
    assert path.read_bytes() == b"first"

    monkeypatch.setattr(export.os, "fsync", _disk_full)
    with pytest.raises(ExportError, match="No space left"):
        write_file_atomically(path, b"second", overwrite=True)
    assert path.read_bytes() == b"first"
    assert [item.name for item in tmp_path.iterdir()] == ["plot.png"]

    monkeypatch.undo()
    write_file_atomically(path, b"second", overwrite=True)
    assert path.read_bytes() == b"second"
    with pytest.raises(ExportError):
        write_file_atomically(tmp_path / "missing" / "plot.png", b"data")
