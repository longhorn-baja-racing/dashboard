"""Bounded preview, cooperative cancellation and reporting use the real parser."""

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


@pytest.mark.parametrize(
    ("content", "message"),
    [
        ("a\n" + "\n" * 50001, "50,000 rows"),
        (",".join(["a"] * 129) + "\n", "128 columns"),
        ("a\n" + "x" * (16 * 1024 * 1024), "16 MiB"),
        ("a,b\n" + "1,2\n" * 25000, "50,000 cells"),
    ],
    ids=["rows", "columns", "bytes", "cells"],
)
def test_large_preview_rejected_with_explanation(tmp_path, content, message):
    path = tmp_path / "large.csv"
    path.write_text(content)
    with pytest.raises(ValueError, match=message):
        CsvImporter().import_session(path)


def test_cancel_after_final_row_before_numeric_conversion(tmp_path):
    path = tmp_path / "one.csv"
    path.write_text("a\n")
    cancelled = Event()
    with pytest.raises(InterruptedError):
        CsvImporter().import_session(
            path, cancelled=cancelled.is_set, progress=lambda count: cancelled.set()
        )
