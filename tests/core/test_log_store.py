"""The session holder must preserve valid data across failed imports."""

from dataclasses import FrozenInstanceError
from pathlib import Path

import pytest

from lbr_dashboard.core.log import LogSession, LogSessionStore
from lbr_dashboard.importers.csv_importer import CsvImporter


def test_unloaded_store_is_empty() -> None:
    store = LogSessionStore(CsvImporter())
    assert store.headers == ()
    assert store.rows == ()
    assert store.numeric_column("missing") is None


def test_load_and_missing_file_preserve_current_session(tmp_path: Path) -> None:
    path = tmp_path / "log.csv"
    path.write_text("time,value\n0,12\n")
    store = LogSessionStore(CsvImporter())
    assert store.load(path)
    assert store.headers == ("time", "value")
    assert store.rows == (("0", "12"),)
    assert store.numeric_column("value") == (12.0,)
    assert store.numeric_column("missing") is None
    with pytest.raises(FileNotFoundError):
        store.load(tmp_path / "missing.csv")
    assert store.numeric_column("value") == (12.0,)


def test_empty_import_preserves_existing_session(tmp_path: Path) -> None:
    path = tmp_path / "valid.csv"
    empty = tmp_path / "empty.csv"
    path.write_text("rpm\n900\n")
    empty.write_text("")
    store = LogSessionStore(CsvImporter())
    assert not store.load(empty)
    assert store.load(path)
    assert not store.load(empty)
    assert store.headers == ("rpm",)
    assert store.numeric_column("rpm") == (900.0,)


def test_short_rows_and_quoted_csv(tmp_path: Path) -> None:
    path = tmp_path / "quoted.csv"
    path.write_text('time,value,note\n0,12,"hello, team"\n1\n')
    session = CsvImporter().import_session(path)
    assert session.numeric_column("value") == (12.0, None)
    assert session.rows[0][2] == "hello, team"
    assert session.numeric_column("note") == (None, None)


def test_model_copies_rows_and_rejects_mutation() -> None:
    headers = ["rpm"]
    rows = [["900"]]
    session = LogSession(headers, rows, {"rpm": [900.0]})  # type: ignore[arg-type]
    headers.append("other")
    rows[0][0] = "1000"
    assert session.headers == ("rpm",)
    assert session.rows == (("900",),)
    with pytest.raises(FrozenInstanceError):
        session.headers = ()  # type: ignore[misc]
