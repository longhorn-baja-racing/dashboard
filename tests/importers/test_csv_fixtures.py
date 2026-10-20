"""Tests pinning the neutral sample fixtures to their documented values."""

from pathlib import Path
import pytest

from lbr_dashboard.importers.csv_importer import CsvImporter

# Adjust parents[...] if the tests folder sits somewhere else relative to the repo root.
FIXTURES = Path(__file__).resolve().parents[2] / "sample_data" / "fixtures"


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


def test_fixtures_do_not_assume_vehicle_columns() -> None:
    paths = sorted(FIXTURES.glob("*.csv"))
    assert paths, f"no fixtures found in {FIXTURES}"
    for path in paths:
        session = CsvImporter().import_session(path)
        assert session.numeric_column("gps_lat") is None
        assert session.numeric_column("engine_rpm") is None


def test_missing_timestamp_fixture_documents_current_behavior() -> None:
    session = CsvImporter().import_session(FIXTURES / "error_missing_timestamp.csv")

    # The importer does not reject this file: the blank timestamp becomes a gap.
    assert session.numeric_column("t_s") == (0.0, None, 0.2)
    assert session.numeric_column("sensor_a") == (1.0, 2.0, 3.0)



def test_invalid_encoding_is_rejected() -> None:
    with pytest.raises(UnicodeDecodeError):
        CsvImporter().import_session(FIXTURES / "error_bad_encoding.csv")