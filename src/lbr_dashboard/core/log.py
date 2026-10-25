"""Qt-independent legacy CSV model; mixed-rate telemetry is separate roadmap work."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from math import isfinite
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
    columns_by_index: tuple[NumericColumn, ...] = ()

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
        indexed = self.columns_by_index or tuple(
            self.numeric_columns.get(header, ()) for header in self.headers
        )
        object.__setattr__(self, "columns_by_index", tuple(tuple(values) for values in indexed))

    def numeric_column(self, name: str | int) -> NumericColumn | None:
        """Look up a physical column by index, or the first column with a name."""

        if isinstance(name, int):
            return self.columns_by_index[name] if 0 <= name < len(self.columns_by_index) else None
        return self.numeric_columns.get(name)

    @classmethod
    def from_rows(cls, headers: tuple[str, ...], rows: tuple[tuple[str, ...], ...]) -> LogSession:
        """Create normalized rows and numeric columns from decoded values."""

        def as_float(value: str) -> float | None:
            try:
                number = float(value)
                return number if isfinite(number) else None
            except (TypeError, ValueError):
                return None

        indexed = tuple(
            tuple(as_float(row[index]) if index < len(row) else None for row in rows)
            for index in range(len(headers))
        )
        columns: dict[str, NumericColumn] = {}
        for header, values in zip(headers, indexed):
            columns.setdefault(header, values)
        return cls(headers, rows, columns, indexed)


class LogImporter(Protocol):
    """Decode a legacy file into the presentation-independent tabular model."""

    def import_session(self, path: Path) -> LogSession: ...


class LogSessionStore:
    """Application-owned holder for the current imported log session."""

    def __init__(self, importer: LogImporter) -> None:
        self._importer = importer
        self._session: LogSession | None = None

    def load(self, path: Path) -> bool:
        """Replace successfully imported data, including an empty session."""

        session = self._importer.import_session(path)
        self._session = session
        return bool(session.headers)

    @property
    def headers(self) -> tuple[str, ...]:
        return self._session.headers if self._session is not None else ()

    @property
    def rows(self) -> tuple[tuple[str, ...], ...]:
        return self._session.rows if self._session is not None else ()

    def numeric_column(self, name: str | int) -> NumericColumn | None:
        """Return one numeric column from the current session."""

        if self._session is None:
            return None
        return self._session.numeric_column(name)
