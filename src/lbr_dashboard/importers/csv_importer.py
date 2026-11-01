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
        if path.stat().st_size > 16 * 1024 * 1024:
            raise ValueError("This preview supports CSV files up to 16 MiB. Choose a smaller CSV.")
        with path.open(newline="", encoding="utf-8-sig") as csv_file:
            rows = []
            cells = 0
            for index, row in enumerate(csv.reader(csv_file, strict=True)):
                if cancelled():
                    raise InterruptedError("CSV import cancelled")
                if index > 50000:
                    raise ValueError(
                        "This preview supports at most 50,000 rows. Choose a smaller CSV."
                    )
                if len(row) > 128:
                    raise ValueError(
                        "This preview supports at most 128 columns. Choose a narrower CSV."
                    )
                cells += len(row)
                if cells > 50000:
                    raise ValueError(
                        "This table preview supports at most 50,000 cells. Choose a smaller CSV."
                    )
                rows.append(tuple(row))
                if index % 250 == 0:
                    progress(index)
        if not rows:
            return LogSession((), (), {})
        if cancelled():
            raise InterruptedError("CSV import cancelled")
        return LogSession.from_rows(rows[0], tuple(rows[1:]))
