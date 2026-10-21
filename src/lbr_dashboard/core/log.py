"""Qt-independent legacy CSV model; mixed-rate telemetry is separate roadmap work."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from types import MappingProxyType
from typing import Protocol, TypeAlias

NumericColumn: TypeAlias = tuple[float | None, ...]


@dataclass(frozen=True, slots=True)
class LogSession:
    """Immutable rows and pre-parsed numeric columns shared with presenters."""

    headers: tuple[str, ...]
    rows: tuple[tuple[str, ...], ...]
    numeric_columns: Mapping[str, NumericColumn]

    def __post_init__(self) -> None:
        object.__setattr__(self, "headers", tuple(self.headers))
        object.__setattr__(self, "rows", tuple(tuple(row) for row in self.rows))
        object.__setattr__(
            self,
            "numeric_columns",
            MappingProxyType(
                {name: tuple(values) for name, values in self.numeric_columns.items()}
            ),
        )

    def numeric_column(self, name: str) -> NumericColumn | None:
        """Return the parsed values for one column, when that column exists."""

        return self.numeric_columns.get(name)

    @classmethod
    def from_rows(cls, headers: tuple[str, ...], rows: tuple[tuple[str, ...], ...]) -> LogSession:
        """Create normalized rows and numeric columns from decoded values."""

        def as_float(value: str) -> float | None:
            try:
                return float(value)
            except (TypeError, ValueError):
                return None

        columns = {
            header: tuple(as_float(row[index]) if index < len(row) else None for row in rows)
            for index, header in enumerate(headers)
        }
        return cls(headers, rows, columns)


class LogImporter(Protocol):
    """Decode a legacy file into the presentation-independent tabular model."""

    def import_session(self, path: Path) -> LogSession: ...


class LogSessionStore:
    """Application-owned holder for the current imported log session."""

    def __init__(self, importer: LogImporter) -> None:
        self._importer = importer
        self._session: LogSession | None = None

    def load(self, path: Path) -> bool:
        """Replace the current session only when the import contains a header row."""

        session = self._importer.import_session(path)
        if not session.headers:
            return False
        self._session = session
        return True

    @property
    def headers(self) -> tuple[str, ...]:
        return self._session.headers if self._session is not None else ()

    @property
    def rows(self) -> tuple[tuple[str, ...], ...]:
        return self._session.rows if self._session is not None else ()

    def numeric_column(self, name: str) -> NumericColumn | None:
        """Return one numeric column from the current session."""

        if self._session is None:
            return None
        return self._session.numeric_column(name)
