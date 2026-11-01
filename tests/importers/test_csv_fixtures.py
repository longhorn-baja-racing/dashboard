"""Tests pinning the neutral sample fixtures to their documented values."""

from itertools import pairwise
from pathlib import Path

import pytest

from lbr_dashboard.core.log import LogSessionStore
from lbr_dashboard.importers.csv_importer import CsvImporter

# Adjust parents[...] if the tests folder sits somewhere else relative to the repo root.
FIXTURES = Path(__file__).resolve().parents[2] / "sample_data" / "fixtures"
LEGACY = FIXTURES.parent / "legacy" / "sample_baja_log.csv"

EXPECTED_HEADERS = {
    "env_temp_pressure.csv": ("t_s", "coolant_temp_C", "line_pressure_kPa"),
    "motion_accel_gyro.csv": ("ts_ms", "ax", "gz"),
    "blank_timestamp_cell.csv": ("t_s", "sensor_a"),
}


@pytest.mark.parametrize(
    ("name", "time_column"),
    [("env_temp_pressure.csv", "t_s"), ("motion_accel_gyro.csv", "ts_ms")],
)
def test_fixture_timestamps_increase_and_are_nonuniform(name: str, time_column: str) -> None:
    times = CsvImporter().import_session(FIXTURES / name).numeric_column(time_column)

    assert times is not None
    values = [t for t in times if t is not None]
    assert len(values) == len(times)  # no blank or non-numeric timestamps
    steps = [b - a for a, b in pairwise(values)]
    assert all(step > 0 for step in steps)
    assert len({round(step, 9) for step in steps}) > 1  # spacing really is uneven


def test_fixture_set_matches_documented_headers() -> None:
    paths = sorted(FIXTURES.glob("*.csv"))
    assert {p.name for p in paths} == set(EXPECTED_HEADERS)
    for path in paths:
        assert CsvImporter().import_session(path).headers == EXPECTED_HEADERS[path.name]


def test_fixtures_share_no_headers_with_legacy_sample() -> None:
    legacy = {h.casefold() for h in CsvImporter().import_session(LEGACY).headers}
    assert legacy, "legacy sample has no headers"
    for path in sorted(FIXTURES.glob("*.csv")):
        headers = {h.casefold() for h in CsvImporter().import_session(path).headers}
        assert not headers & legacy, f"{path.name} reuses legacy columns: {headers & legacy}"


def test_env_fixture_keeps_blanks_as_gaps() -> None:
    session = CsvImporter().import_session(FIXTURES / "env_temp_pressure.csv")

    assert session.headers == ("t_s", "coolant_temp_C", "line_pressure_kPa")
    assert session.numeric_column("t_s") == (0.0, 0.1, 0.35, 0.6, 1.2, 2.0)
    assert session.numeric_column("coolant_temp_C") == (25.0, 25.5, None, 26.5, 27.0, 28.0)
    assert session.numeric_column("line_pressure_kPa") == (101.3, 101.4, 101.9, 102.2, None, 103.0)


def test_motion_fixture_uses_different_time_column() -> None:
    session = CsvImporter().import_session(FIXTURES / "motion_accel_gyro.csv")

    assert session.headers == ("ts_ms", "ax", "gz")
    assert session.numeric_column("ts_ms") == (0.0, 5.0, 12.0, 30.0, 41.0)
    assert session.numeric_column("ax") == (0.0, 0.1, 0.25, -0.05, 0.0)
    assert session.numeric_column("gz") == (0.0, 1.5, 3.0, None, 4.5)


def test_blank_timestamp_cell_loads_as_gap() -> None:
    # Current behavior: the importer has no notion of a time column, so a blank
    # timestamp is just a None. If importer-side validation is added later, replace
    # this test (and consider restoring an error_ fixture name) rather than
    # patching around it.
    session = CsvImporter().import_session(FIXTURES / "blank_timestamp_cell.csv")

    assert session.numeric_column("t_s") == (0.0, None, 0.2)
    assert session.numeric_column("sensor_a") == (1.0, 2.0, 3.0)


def test_empty_fixture_loads_as_empty_session() -> None:
    path = FIXTURES / "malformed" / "error_empty.csv"

    session = CsvImporter().import_session(path)

    assert session.headers == ()
    assert session.rows == ()
    assert not session.numeric_columns
    assert LogSessionStore(CsvImporter()).load(path) is False


def test_invalid_encoding_is_rejected() -> None:
    # The importer currently lets the raw exception escape and the app shows the
    # user nothing; user-facing handling is tracked in #103. Update this test when
    # the importer wraps decode errors.
    with pytest.raises(UnicodeDecodeError):
        CsvImporter().import_session(FIXTURES / "malformed" / "error_bad_encoding.csv")
