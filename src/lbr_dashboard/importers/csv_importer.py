"""CSV log importer."""

from __future__ import annotations

import csv
from pathlib import Path

from ..core.log import LogSession


class CsvImporter:
    """Read a CSV file into a presentation-independent log model."""

    def import_session(self, path: Path) -> LogSession:
        with path.open(newline="", encoding="utf-8-sig") as csv_file:
            rows = tuple(tuple(row) for row in csv.reader(csv_file) if row)
        if not rows:
            return LogSession((), (), {})
        return LogSession.from_rows(rows[0], rows[1:])
