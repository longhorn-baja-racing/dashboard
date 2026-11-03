"""Read arbitrary timestamped CSV files through an explicit column mapping (#31).

Two steps, both read-only:

1. :func:`suggest_mapping` looks at the header and the first rows and proposes
   a :class:`~lbr_dashboard.core.csv_mapping.CsvMapping`. Time columns are
   recognized by name. Their unit is taken only from the header (``time_ms``,
   ``t (s)``, ``elapsed [us]``) or from self-describing ISO 8601 values, and is
   otherwise left for the user to choose. Every guess the user should confirm
   is listed in ``Suggestion.ambiguities``.
2. :class:`MappedCsvImporter` applies a confirmed mapping. Rows whose time is
   missing or invalid are skipped and reported with their line number. Bad
   cells become missing (never zero) and are reported too, as are rows with
   too few or too many cells, times out of order and repeated times.
"""

from __future__ import annotations

import csv
import re
from array import array
from collections.abc import Iterator, Sequence
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from decimal import Decimal, InvalidOperation
from math import isfinite
from pathlib import Path
from typing import TextIO

from ..core.csv_mapping import (
    ISO8601,
    MAX_PROBLEMS,
    MISSING_TOKENS,
    TIME_UNITS,
    ColumnMapping,
    CsvMapping,
    MappedSession,
    MappedSignal,
    RowProblem,
    SignalKind,
)
from ..core.lifecycle import CancellationToken

SAMPLE_ROWS = 200
CANCEL_CHECK_ROWS = 1000
TRUE_TOKENS = frozenset({"true", "yes", "on", "1"})
FALSE_TOKENS = frozenset({"false", "no", "off", "0"})
_WORD_BOOLEANS = frozenset({"true", "false", "yes", "no", "on", "off"})

_BRACKETED = re.compile(r"^(?P<name>.*?)\s*[\(\[](?P<unit>[^\)\]]+)[\)\]]\s*$")
_TIME_NAME = re.compile(
    r"^(t|time|timestamp|datetime|date_time|clock|elapsed|elapsed_time"
    r"|[a-z0-9]+_time|time_[a-z0-9]+|[a-z0-9]+_timestamp)$"
)
_TIME_SUFFIX = re.compile(r"^(?P<base>.+?)[_ ](?P<unit>s|sec|ms|msec|us|usec|ns|nsec)$")
_UNIT_WORDS = {
    "s": "s",
    "sec": "s",
    "secs": "s",
    "second": "s",
    "seconds": "s",
    "ms": "ms",
    "msec": "ms",
    "millisecond": "ms",
    "milliseconds": "ms",
    "us": "us",
    "usec": "us",
    chr(0xB5) + "s": "us",
    "microsecond": "us",
    "microseconds": "us",
    "ns": "ns",
    "nsec": "ns",
    "nanosecond": "ns",
    "nanoseconds": "ns",
}
_ISO = re.compile(
    r"^(?P<date>\d{4}-\d{2}-\d{2})[T ](?P<h>\d{2}):(?P<m>\d{2}):(?P<s>\d{2})"
    r"(?:[.,](?P<frac>\d{1,9}))?\s*(?P<tz>Z|[+-]\d{2}:?\d{2})?$",
    re.IGNORECASE,
)
_EPOCH = datetime(1970, 1, 1, tzinfo=timezone.utc)


class CsvMappingError(ValueError):
    """The file cannot be read with this mapping; the message says what to change."""


# Header and value helpers --------------------------------------------------------------


def split_unit(header: str) -> tuple[str, str]:
    """``"speed (km/h)"`` -> ``("speed", "km/h")``. Units are only read from brackets."""

    match = _BRACKETED.match(header.strip())
    if match is None or not match.group("name"):
        return header.strip(), ""
    return match.group("name"), match.group("unit").strip()


def time_unit_from_header(header: str) -> str | None:
    """The time unit written in a header, or None if the header does not say."""

    name, unit = split_unit(header)
    if unit:
        return _UNIT_WORDS.get(unit.lower())
    match = _TIME_SUFFIX.match(name.lower())
    return _UNIT_WORDS[match.group("unit")] if match else None


def looks_like_time(header: str) -> bool:
    name, _ = split_unit(header)
    lowered = name.lower().strip()
    match = _TIME_SUFFIX.match(lowered)
    base = match.group("base") if match else lowered
    return bool(_TIME_NAME.match(base.replace(" ", "_")))


def is_missing(text: str) -> bool:
    return text.strip().lower() in MISSING_TOKENS


def parse_number(text: str) -> float:
    try:
        value = float(text)
    except ValueError:
        raise ValueError(f"{text!r} is not a number") from None
    if not isfinite(value):
        raise ValueError(f"{text!r} is not a finite number")
    return value


def parse_boolean(text: str) -> bool:
    lowered = text.strip().lower()
    if lowered in TRUE_TOKENS:
        return True
    if lowered in FALSE_TOKENS:
        return False
    raise ValueError(f"{text!r} is not true/false, yes/no, on/off or 1/0")


def parse_iso8601(text: str) -> tuple[int, bool]:
    """Nanoseconds since 1970-01-01 UTC, and whether the text named its time zone."""

    match = _ISO.match(text.strip())
    if match is None:
        raise ValueError(f"{text!r} is not an ISO 8601 date and time")
    tz_text = match.group("tz")
    zone = timezone.utc
    if tz_text and tz_text.upper() != "Z":
        sign = -1 if tz_text[0] == "-" else 1
        digits = tz_text[1:].replace(":", "")
        zone = timezone(sign * timedelta(hours=int(digits[:2]), minutes=int(digits[2:])))
    moment = datetime.fromisoformat(
        f"{match.group('date')}T{match.group('h')}:{match.group('m')}:{match.group('s')}"
    ).replace(tzinfo=zone)
    seconds = (moment - _EPOCH) // timedelta(seconds=1)
    fraction = (match.group("frac") or "").ljust(9, "0")
    return seconds * 1_000_000_000 + int(fraction or 0), tz_text is not None


def parse_time(text: str, unit: str) -> int:
    """Exact integer nanoseconds for a time cell."""

    if unit == ISO8601:
        return parse_iso8601(text)[0]
    stripped = text.strip()
    if stripped.lstrip("-").isdigit():
        return int(stripped) * TIME_UNITS[unit]
    try:
        value = Decimal(stripped)
    except InvalidOperation:
        raise ValueError(f"{text!r} is not a number") from None
    if not value.is_finite():
        raise ValueError(f"{text!r} is not a finite number")
    return int((value * TIME_UNITS[unit]).to_integral_value())


#: A column is suggested as numeric when at least this share of its cells are.
NUMERIC_SHARE = 0.8


def classify(cells: Sequence[str]) -> tuple[SignalKind, tuple[str, ...]]:
    """Suggested kind, and the present cells that do not fit it (for the user to see).

    A column is numeric if at least ``NUMERIC_SHARE`` of its present cells are
    numbers; the rest are listed and will import as missing. It is boolean if
    every cell is a true/false word (0 and 1 alone stay numeric).
    """

    present = [cell.strip() for cell in cells if not is_missing(cell)]
    if not present:
        return SignalKind.NUMBER, ()
    misfits = []
    for cell in present:
        try:
            parse_number(cell)
        except ValueError:
            misfits.append(cell)
    lowered = {cell.lower() for cell in present}
    if misfits and lowered <= TRUE_TOKENS | FALSE_TOKENS and lowered & _WORD_BOOLEANS:
        return SignalKind.BOOLEAN, ()
    if len(misfits) <= round((1 - NUMERIC_SHARE) * len(present), 9):
        return SignalKind.NUMBER, tuple(misfits)
    return SignalKind.TEXT, ()


def detect_kind(cells: Sequence[str]) -> SignalKind:
    return classify(cells)[0]


# Reading -------------------------------------------------------------------------------


def _open(path: Path, encoding: str) -> TextIO:
    name = "utf-8-sig" if encoding.lower().replace("_", "-") in {"utf-8", "utf8"} else encoding
    try:
        return path.open(newline="", encoding=name, errors="strict")
    except LookupError:
        raise CsvMappingError(f"{encoding!r} is not a known text encoding.") from None
    except OSError as error:
        raise CsvMappingError(f"Could not read {path.name}: {error.strerror or error}") from None


def _reader(stream: TextIO, delimiter: str) -> Iterator[list[str]]:
    if len(delimiter) != 1:
        raise CsvMappingError("The delimiter must be one character.")
    return csv.reader(stream, delimiter=delimiter)


def _rows(
    reader: Iterator[list[str]], path: Path, encoding: str
) -> Iterator[tuple[int, list[str]]]:
    """(first line, cells) per row, blank lines dropped.

    A quoted cell can span lines, so the first line is where the row starts.
    Decoding errors name the line and how to fix them.
    """

    end = 0
    while True:
        start = end + 1
        try:
            row = next(reader)
        except StopIteration:
            return
        except UnicodeDecodeError:
            raise CsvMappingError(
                f"{path.name} is not valid {encoding} text near line {start}. "
                "Choose the file's encoding (for example cp1252) or save it as UTF-8."
            ) from None
        except csv.Error as error:
            raise CsvMappingError(f"{path.name} line {start}: {error}") from None
        end = getattr(reader, "line_num", start)
        if row and any(cell.strip() for cell in row):
            yield start, row


def read_sample(
    path: Path, encoding: str = "utf-8", delimiter: str = ",", rows: int = SAMPLE_ROWS
) -> tuple[tuple[str, ...], tuple[tuple[str, ...], ...]]:
    """The header and up to ``rows`` data rows, for suggesting and previewing."""

    with _open(path, encoding) as stream:
        reader = _reader(stream, delimiter)
        found = _rows(reader, path, encoding)
        first = next(found, None)
        if first is None:
            raise CsvMappingError(f"{path.name} is empty.")
        header = first[1]
        sample = []
        for _, row in found:
            sample.append(tuple(row))
            if len(sample) >= rows:
                break
    return tuple(cell.strip() for cell in header), tuple(sample)


# Suggesting ----------------------------------------------------------------------------


@dataclass(frozen=True)
class Suggestion:
    """A proposed mapping plus everything the user should confirm or choose."""

    mapping: CsvMapping
    ambiguities: tuple[str, ...]


def _column(rows: Sequence[Sequence[str]], index: int) -> list[str]:
    return [row[index] if index < len(row) else "" for row in rows]


def _is_iso_column(cells: Sequence[str]) -> bool:
    present = [cell for cell in cells if not is_missing(cell)]
    try:
        return bool(present) and all(parse_iso8601(cell) is not None for cell in present)
    except ValueError:
        return False


def suggest_mapping(
    headers: Sequence[str],
    rows: Sequence[Sequence[str]],
    encoding: str = "utf-8",
    delimiter: str = ",",
) -> Suggestion:
    """Propose a mapping. Nothing is decided silently; see ``ambiguities``."""

    ambiguities: list[str] = []
    candidates: list[tuple[int, str | None]] = []
    for index, header in enumerate(headers):
        cells = _column(rows, index)
        if _is_iso_column(cells):
            candidates.append((index, ISO8601))
        elif looks_like_time(header):
            candidates.append((index, time_unit_from_header(header)))
    # Prefer a column whose unit is known, then the leftmost.
    candidates.sort(key=lambda item: (item[1] is None, item[0]))

    time_column, time_unit = (candidates[0][0], candidates[0][1] or "") if candidates else (-1, "")
    if not candidates:
        ambiguities.append("No column looks like time. Choose the time column and its unit.")
    else:
        label = repr(headers[time_column])
        if len(candidates) > 1:
            others = ", ".join(repr(headers[index]) for index, _ in candidates[1:])
            ambiguities.append(f"Several columns look like time; using {label}, not {others}.")
        if not time_unit:
            ambiguities.append(f"The unit of {label} is not in its header. Choose s, ms, us or ns.")

    signals: list[ColumnMapping] = []
    names: set[str] = set()
    for index, header in enumerate(headers):
        if index == time_column:
            continue
        name, unit = split_unit(header)
        name = name or f"column_{index + 1}"
        if name in names:
            renamed = f"{name}_{index + 1}"
            ambiguities.append(f"Column {index + 1} repeats the name {name!r}; using {renamed!r}.")
            name = renamed
        names.add(name)
        cells = _column(rows, index)
        kind, misfits = classify(cells)
        if misfits:
            shown = ", ".join(repr(cell) for cell in misfits[:3])
            more = f" and {len(misfits) - 3} more" if len(misfits) > 3 else ""
            ambiguities.append(
                f"{name!r} looks numeric but has {len(misfits)} other values ({shown}{more}); "
                "they will import as missing."
            )
        if all(is_missing(cell) for cell in cells):
            ambiguities.append(f"{name!r} has no values in the first {len(rows)} rows.")
        signals.append(ColumnMapping(index, name, unit, kind))

    mapping = CsvMapping(time_column, time_unit, tuple(signals), encoding, delimiter)
    return Suggestion(mapping, tuple(ambiguities))


def suggest_for_file(path: Path, encoding: str = "utf-8", delimiter: str = ",") -> Suggestion:
    headers, rows = read_sample(path, encoding, delimiter)
    return suggest_mapping(headers, rows, encoding, delimiter)


# Applying ------------------------------------------------------------------------------


class _Signal:
    __slots__ = ("mapping", "header", "times", "values", "missing", "invalid")

    def __init__(self, mapping: ColumnMapping, header: str) -> None:
        self.mapping = mapping
        self.header = header
        self.times = array("q")
        self.values: array | list[str] = (
            array("d")
            if mapping.kind is SignalKind.NUMBER
            else array("b")
            if mapping.kind is SignalKind.BOOLEAN
            else []
        )
        self.missing = 0
        self.invalid = 0

    def add(self, time_ns: int, text: str) -> None:
        kind = self.mapping.kind
        if kind is SignalKind.NUMBER:
            value: float | int | str = parse_number(text)
        elif kind is SignalKind.BOOLEAN:
            value = int(parse_boolean(text))
        else:
            value = text
        self.values.append(value)  # type: ignore[arg-type]
        self.times.append(time_ns)

    def freeze(self) -> MappedSignal:
        times, values = self.times, self.values
        if any(times[i] < times[i - 1] for i in range(1, len(times))):
            order = sorted(range(len(times)), key=times.__getitem__)  # stable
            times = array("q", (times[i] for i in order))
            if isinstance(values, array):
                values = array(values.typecode, (values[i] for i in order))
            else:
                values = [values[i] for i in order]
        return MappedSignal(
            name=self.mapping.name,
            unit=self.mapping.unit,
            kind=self.mapping.kind,
            source_column=self.header,
            times_ns=times,
            values=values if isinstance(values, array) else tuple(values),
            missing=self.missing,
            invalid=self.invalid,
        )


class MappedCsvImporter:
    """Apply a confirmed :class:`CsvMapping` to a CSV file."""

    def import_session(
        self,
        path: Path,
        mapping: CsvMapping,
        *,
        cancel: CancellationToken | None = None,
        max_rows: int | None = None,
    ) -> MappedSession:
        """Read the whole file (or the first ``max_rows`` data rows, for a preview)."""

        with _open(path, mapping.encoding) as stream:
            reader = _reader(stream, mapping.delimiter)
            rows = _rows(reader, path, mapping.encoding)
            first = next(rows, None)
            if first is None:
                raise CsvMappingError(f"{path.name} is empty.")
            headers = tuple(cell.strip() for cell in first[1])
            reasons = mapping.problems(headers)
            if reasons:
                raise CsvMappingError(" ".join(reasons))
            return self._read(path, mapping, headers, rows, cancel, max_rows)

    def _read(
        self,
        path: Path,
        mapping: CsvMapping,
        headers: tuple[str, ...],
        rows: Iterator[tuple[int, list[str]]],
        cancel: CancellationToken | None,
        max_rows: int | None,
    ) -> MappedSession:
        signals = [_Signal(signal, headers[signal.column]) for signal in mapping.signals]
        problems: list[RowProblem] = []
        unlisted = 0

        def problem(line: int, message: str, column: str | None = None) -> None:
            nonlocal unlisted
            if len(problems) < MAX_PROBLEMS:
                problems.append(RowProblem(line, message, column))
            else:
                unlisted += 1

        width = len(headers)
        time_header = headers[mapping.time_column]
        read = skipped = out_of_order = 0
        latest: int | None = None
        seen: set[int] = set()
        duplicates = 0
        zoneless = False
        lowest: int | None = None
        truncated = False
        for line, row in rows:
            if max_rows is not None and read >= max_rows:
                truncated = True
                break
            read += 1
            if cancel is not None and read % CANCEL_CHECK_ROWS == 0:
                cancel.raise_if_cancelled()
            if len(row) != width:
                problem(line, f"has {len(row)} cells but the header has {width}")
            time_text = row[mapping.time_column] if mapping.time_column < len(row) else ""
            if is_missing(time_text):
                skipped += 1
                problem(line, "skipped: no time", time_header)
                continue
            try:
                if mapping.time_unit == ISO8601:
                    time_ns, zoned = parse_iso8601(time_text)
                    zoneless = zoneless or not zoned
                else:
                    time_ns = parse_time(time_text, mapping.time_unit)
            except ValueError as error:
                skipped += 1
                problem(line, f"skipped: {error}", time_header)
                continue
            if latest is not None and time_ns < latest:
                out_of_order += 1
            latest = time_ns if latest is None else max(latest, time_ns)
            lowest = time_ns if lowest is None else min(lowest, time_ns)
            if time_ns in seen:
                duplicates += 1
            seen.add(time_ns)
            for signal in signals:
                column = signal.mapping.column
                text = row[column] if column < len(row) else ""
                if is_missing(text):
                    signal.missing += 1
                    continue
                try:
                    signal.add(time_ns, text)
                except ValueError as error:
                    signal.invalid += 1
                    problem(line, f"kept as missing: {error}", signal.header)

        notes = []
        if zoneless:
            notes.append(f"Some {time_header!r} values have no time zone; they are read as UTC.")
        if out_of_order:
            rows_text = "1 row was" if out_of_order == 1 else f"{out_of_order} rows were"
            notes.append(f"{rows_text} out of time order; samples are sorted by time.")
        if truncated:
            notes.append(f"Preview of the first {max_rows} rows.")
        return MappedSession(
            source_name=path.name,
            mapping=mapping,
            time_header=time_header,
            signals=tuple(signal.freeze() for signal in signals),
            rows_read=read,
            rows_skipped=skipped,
            problems=tuple(problems),
            problems_not_listed=unlisted,
            out_of_order_rows=out_of_order,
            duplicate_times=duplicates,
            notes=tuple(notes),
            truncated=truncated,
            time_range_ns=None if lowest is None or latest is None else (lowest, latest),
        )
