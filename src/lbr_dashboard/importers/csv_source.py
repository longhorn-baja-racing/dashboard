"""Explicit legacy timestamp_ms CSV bridge; arbitrary mappings belong to #31."""

from __future__ import annotations

import csv
from dataclasses import dataclass
from decimal import Decimal, DecimalException
from math import isfinite
from pathlib import Path

from ..core import (
    CancellationToken,
    IteratorSource,
    SignalDescriptor,
    SignalSample,
    SignalSchema,
    SourceEvent,
    SourceEventKind,
)

MAX_BYTES = 16 * 1024 * 1024
MAX_ROWS = 20000
MAX_SIGNALS = 64
MAX_SAMPLES = 200000


def timestamp_ns(text: str) -> int:
    """Convert exactly, without Decimal context rounding or unbounded exponent expansion."""
    if len(text) > 128:
        raise ValueError("timestamp is too long")
    value = Decimal(text)
    exponent = value.as_tuple().exponent
    if not value.is_finite() or not isinstance(exponent, int) or abs(exponent) > 128:
        raise ValueError("timestamp must be finite and bounded")
    numerator, denominator = value.as_integer_ratio()
    result, remainder = divmod(numerator * 1000000, denominator)
    if remainder or not -(2**63) <= result <= 2**63 - 1:
        raise ValueError("timestamp is outside exact nanosecond range")
    return result


@dataclass(frozen=True)
class PreparedCsv:
    schema: SignalSchema
    records: tuple[tuple[str, SignalSample], ...]

    def source(self) -> IteratorSource:
        source_id = self.schema.signals[0].source_id

        def generate(token):
            for identifier, sample in self.records:
                token.raise_if_cancelled()
                yield SourceEvent(
                    SourceEventKind.RECORD, source_id, signal_id=identifier, sample=sample
                )

        return IteratorSource(source_id, self.schema, generate)


def prepare_csv(path: Path, token: CancellationToken) -> PreparedCsv:
    """Read and validate off the GUI thread, preserving physical column identity."""
    source_id = str(path.resolve())
    token.raise_if_cancelled()
    if path.stat().st_size > MAX_BYTES:
        raise ValueError("CSV exceeds 16 MiB. Choose a smaller file.")
    rows = []
    size = 0
    with path.open(encoding="utf-8-sig", newline="") as stream:
        for line, row in enumerate(csv.reader(stream, strict=True)):
            token.raise_if_cancelled()
            size += sum(len(cell.encode("utf-8")) for cell in row) + len(row) + 1
            if size > MAX_BYTES or line > MAX_ROWS:
                raise ValueError("CSV exceeds the import limit of 16 MiB / 20,000 rows.")
            if row:
                rows.append(tuple(row))
    if len(rows) < 2:
        raise ValueError("CSV has no readings. Choose a populated timestamp_ms CSV.")
    headers, *data = rows
    if headers.count("timestamp_ms") != 1:
        raise ValueError(
            "This CSV preset needs exactly one timestamp_ms column. "
            "Other timestamp formats need the explicit mapping adapter (#31)."
        )
    time_column = headers.index("timestamp_ms")
    columns = [i for i in range(len(headers)) if i != time_column]
    if not columns or len(columns) > MAX_SIGNALS or len(data) * len(columns) > MAX_SAMPLES:
        raise ValueError("Choose 1–64 signal columns and at most 200,000 readings.")
    times = []
    for number, row in enumerate(data, 2):
        token.raise_if_cancelled()
        if len(row) != len(headers):
            raise ValueError(f"Row {number}: expected {len(headers)} fields, got {len(row)}.")
        try:
            times.append(timestamp_ns(row[time_column]))
        except (DecimalException, ValueError):
            raise ValueError(
                f"Row {number}: timestamp_ms must be finite, within signed "
                "64-bit nanoseconds and have at most nanosecond precision."
            ) from None
    descriptors = []
    records = []
    for column in columns:
        token.raise_if_cancelled()
        cells = [row[column] for row in data]
        populated = [cell for cell in cells if cell.strip()]
        numeric = True
        for cell in populated:
            try:
                value = float(cell)
            except OverflowError:
                raise ValueError(f"Column {column + 1}: reading is too large.") from None
            except ValueError:
                numeric = False
            else:
                if not isfinite(value):
                    raise ValueError(
                        f"Column {column + 1}: non-finite reading {cell!r}. "
                        "Use an empty cell for unavailable data."
                    )
        identifier = f"csv.column.{column}"
        descriptors.append(
            SignalDescriptor(
                identifier,
                headers[column] or f"Column {column + 1}",
                ("csv",),
                "numeric" if numeric else "string",
                source_id,
                timestamp_domain="relative",
                display={"csv_column": column, "csv_header": headers[column]},
            )
        )
        for timestamp, cell in zip(times, cells):
            token.raise_if_cancelled()
            available = bool(cell.strip())
            sample = SignalSample(
                timestamp,
                (float(cell) if numeric else cell) if available else None,
                available=available,
                quality="measured" if available else "missing",
                source_id=source_id,
                timestamp_domain="relative",
            )
            records.append((identifier, sample))
    token.raise_if_cancelled()
    return PreparedCsv(SignalSchema(1, tuple(descriptors)), tuple(records))
