"""Bounded preview, cooperative cancellation and reporting use the real parser."""

import csv
from threading import Event

import pytest

from lbr_dashboard.importers.csv_importer import CsvImporter


def test_cancel_mid_read_never_returns_partial_session(tmp_path):
    path = tmp_path / "cancel.csv"
    path.write_text("timestamp_ms,value\n" + "0,2\n" * 400)
    cancelled = Event()

    def progress(count):
        if count >= 250:
            cancelled.set()

    with pytest.raises(InterruptedError):
        CsvImporter().import_session(path, cancelled=cancelled.is_set, progress=progress)
    assert path.read_text().endswith("0,2\n")


def test_parser_field_limit_retains_csv_line_diagnostic(tmp_path):
    path = tmp_path / "large.csv"
    path.write_text("a\n" + "x" * (csv.field_size_limit() + 1))
    with pytest.raises(csv.Error, match="CSV line 2"):
        CsvImporter().import_session(path)


def test_cancel_after_final_row_before_numeric_conversion(tmp_path):
    path = tmp_path / "one.csv"
    path.write_text("a\n")
    cancelled = Event()
    with pytest.raises(InterruptedError):
        CsvImporter().import_session(
            path, cancelled=cancelled.is_set, progress=lambda count: cancelled.set()
        )
