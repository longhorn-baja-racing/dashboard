"""CSV log importer."""

from __future__ import annotations

import csv
from collections.abc import Callable
from pathlib import Path

from ..core.log import LogSession


class CsvImporter:
    """Read a CSV file into a presentation-independent log model."""

    def import_session(
        self,
        path: Path,
        *,
        cancelled: Callable[[], bool] = lambda: False,
        progress: Callable[[int], None] = lambda count: None,
    ) -> LogSession:
        with path.open(newline="", encoding="utf-8-sig") as csv_file:
            rows = []
            reader = csv.reader(csv_file, strict=True)
            try:
                for index, row in enumerate(reader):
                    if cancelled():
                        raise InterruptedError("CSV import cancelled")
                    if row:
                        rows.append(tuple(row))
                    if index % 250 == 0:
                        progress(index)
            except csv.Error as error:
                raise csv.Error(f"CSV line {reader.line_num}: {error}") from error
        if not rows:
            return LogSession((), (), {})
        if cancelled():
            raise InterruptedError("CSV import cancelled")
        headers, *data = rows
        width = len(headers)
        while (
            width > 1
            and not headers[width - 1].strip()
            and all(
                len(row) <= len(headers) and (len(row) < width or not row[width - 1].strip())
                for row in data
            )
        ):
            width -= 1
        return LogSession.from_rows(
            headers[:width], tuple(row[:width] if width < len(headers) else row for row in data)
        )
