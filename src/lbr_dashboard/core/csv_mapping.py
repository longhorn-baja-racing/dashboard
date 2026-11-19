"""Explicit mapping from CSV columns to timestamped signals (issue #31).

A CSV has no schema, so the user (or a suggestion they confirm) says which
column holds time, what unit that time is in, and which other columns become
signals with which names, units and types. This module holds that mapping and
the mapped result. It uses only the standard library; the reader is
:mod:`lbr_dashboard.importers.mapped_csv`.

Times are integer nanoseconds. Each signal keeps only the rows where its cell
held a value, so slower signals in a mixed-rate file get their own timeline
and a missing cell is never turned into zero.
"""

from __future__ import annotations

from array import array
from collections.abc import Sequence
from dataclasses import dataclass, field
from enum import Enum

#: Nanoseconds per unit for numeric time columns.
TIME_UNITS = {"s": 1_000_000_000, "ms": 1_000_000, "us": 1_000, "ns": 1}
#: Time column holding ISO 8601 date-times such as ``2026-03-01T12:00:00.5Z``.
ISO8601 = "iso8601"
TIME_UNIT_CHOICES = (*TIME_UNITS, ISO8601)
#: Cell texts that mean "no value" (compared case-insensitively, trimmed).
MISSING_TOKENS = frozenset({"", "na", "n/a", "nan", "null", "none", "-"})
#: Problems kept with line numbers; later ones are only counted.
MAX_PROBLEMS = 100


class SignalKind(str, Enum):
    """How a column's cells are decoded."""

    NUMBER = "number"
    BOOLEAN = "boolean"
    TEXT = "text"


@dataclass(frozen=True, slots=True)
class ColumnMapping:
    """One CSV column that becomes a signal."""

    column: int
    name: str
    unit: str = ""
    kind: SignalKind = SignalKind.NUMBER


@dataclass(frozen=True, slots=True)
class CsvMapping:
    """Which column is time, in what unit, and which columns become signals."""

    time_column: int
    time_unit: str
    signals: tuple[ColumnMapping, ...]
    encoding: str = "utf-8"
    delimiter: str = ","

    def problems(self, headers: Sequence[str]) -> list[str]:
        """Reasons this mapping cannot be applied to a file with ``headers``."""

        found: list[str] = []
        if not 0 <= self.time_column < len(headers):
            found.append("Choose the time column.")
        if self.time_unit not in TIME_UNIT_CHOICES:
            choices = ", ".join(TIME_UNIT_CHOICES)
            found.append(f"Choose the time unit ({choices}); it is never guessed.")
        if len(self.delimiter) != 1:
            found.append("The delimiter must be one character.")
        if not self.signals:
            found.append("Map at least one column to a signal.")
        names: set[str] = set()
        for signal in self.signals:
            label = _label(headers, signal.column)
            if not 0 <= signal.column < len(headers):
                found.append(f"Column {signal.column + 1} does not exist.")
            elif signal.column == self.time_column:
                found.append(f"{label} is the time column and cannot also be a signal.")
            if not signal.name.strip():
                found.append(f"{label} needs a signal name.")
            elif signal.name in names:
                found.append(f"Two columns are named {signal.name!r}; names must be unique.")
            names.add(signal.name)
        return found


def _label(headers: Sequence[str], column: int) -> str:
    if 0 <= column < len(headers) and headers[column].strip():
        return repr(headers[column])
    return f"column {column + 1}"


@dataclass(frozen=True, slots=True)
class RowProblem:
    """Something wrong at one line of the file. ``column`` is the header text."""

    line: int
    message: str
    column: str | None = None


@dataclass(frozen=True)
class MappedSignal:
    """Samples of one mapped column, in time order.

    ``values`` is an ``array('d')`` for numbers, ``array('b')`` (0 or 1) for
    booleans and a tuple of strings for text. ``missing`` counts empty or
    missing-token cells; ``invalid`` counts cells that could not be decoded.
    Neither kind of cell produces a sample.
    """

    name: str
    unit: str
    kind: SignalKind
    source_column: str
    times_ns: array
    values: array | tuple[str, ...]
    missing: int = 0
    invalid: int = 0

    def __len__(self) -> int:
        return len(self.times_ns)


@dataclass(frozen=True)
class MappedSession:
    """The result of applying a mapping to a file. The file itself is never changed."""

    source_name: str
    mapping: CsvMapping
    time_header: str
    signals: tuple[MappedSignal, ...]
    rows_read: int
    rows_skipped: int
    problems: tuple[RowProblem, ...] = ()
    problems_not_listed: int = 0
    out_of_order_rows: int = 0
    duplicate_times: int = 0
    notes: tuple[str, ...] = ()
    truncated: bool = False
    time_range_ns: tuple[int, int] | None = field(default=None)

    def signal(self, name: str) -> MappedSignal:
        for signal in self.signals:
            if signal.name == name:
                return signal
        raise KeyError(name)

    def summary(self) -> str:
        """One-line, user-facing description of what was imported and skipped."""

        parts = [f"{_count(len(self.signals), 'signal')} from {_count(self.rows_read, 'row')}"]
        if self.rows_skipped:
            parts.append(f"{_count(self.rows_skipped, 'row')} skipped")
        problems = len(self.problems) + self.problems_not_listed
        if problems:
            parts.append(_count(problems, "problem"))
        if self.out_of_order_rows:
            parts.append(f"{_count(self.out_of_order_rows, 'row')} out of time order")
        if self.duplicate_times:
            parts.append(_count(self.duplicate_times, "repeated time"))
        return ", ".join(parts)


def _count(number: int, noun: str) -> str:
    return f"{number} {noun}" + ("" if number == 1 else "s")
