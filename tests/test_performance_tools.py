"""Benchmark fixtures are deterministic and plot decimation keeps what matters."""

from __future__ import annotations

import importlib.util
import json
import sys
from pathlib import Path
from types import ModuleType

import numpy as np
import pytest
from pytestqt.qtbot import QtBot

from lbr_dashboard.app import create_main_window
from lbr_dashboard.importers.lbr_csv import LbrCsvImporter

ROOT = Path(__file__).resolve().parents[1]


def load(name: str) -> ModuleType:
    sys.path.insert(0, str(ROOT / "scripts"))
    try:
        spec = importlib.util.spec_from_file_location(name, ROOT / "scripts" / f"{name}.py")
        assert spec is not None and spec.loader is not None
        module = importlib.util.module_from_spec(spec)
        sys.modules[name] = module
        spec.loader.exec_module(module)
        return module
    finally:
        sys.path.remove(str(ROOT / "scripts"))


generator = load("make_bench_session")
benchmark = load("benchmark")


@pytest.fixture(scope="module")
def smoke(tmp_path_factory: pytest.TempPathFactory) -> Path:
    path = tmp_path_factory.mktemp("bench") / "smoke.csv"
    generator.generate(path, generator.PRESETS["smoke"])
    return path


def test_generator_is_deterministic_and_valid(smoke: Path, tmp_path: Path) -> None:
    again = tmp_path / "again.csv"
    rows = generator.generate(again, generator.PRESETS["smoke"])
    assert again.read_bytes() == smoke.read_bytes()
    session = LbrCsvImporter().import_session(smoke)
    assert session.row_count == rows and session.malformed_count == 0
    assert session.warnings == ()
    rates = {c.id: c.count for c in session.channels}
    assert rates["corner_fl.imu_fl_accel_x"] == 3000
    assert rates["temperature.temp_cvt"] == 300
    assert rates["powertrain.fuel_level"] == 30
    faulted = session.channel("corner_br.imu_br_gyro_z")
    assert faulted is not None and faulted.unavailable_reasons() == {"SENSOR_FAULT": 200}


def test_timestamps_are_irregular_and_interleaved(smoke: Path) -> None:
    session = LbrCsvImporter().import_session(smoke)
    channel = session.channel("corner_fl.imu_fl_accel_y")
    assert channel is not None
    steps = np.diff(channel.times_us)
    assert steps.min() < 10_000 < steps.max()
    lines = smoke.read_text(encoding="ascii").splitlines()[5:200]
    times = [int(line.split(",")[0].replace(".", "")) for line in lines]
    assert times != sorted(times)


def test_decimated_plot_keeps_the_spike_and_the_gaps(tmp_path: Path, qtbot: QtBot) -> None:
    seconds = 120  # 12000 points per 100 Hz signal, well past the plot width
    session_file = tmp_path / "two-minutes.csv"
    generator.generate(session_file, seconds)
    window = create_main_window()
    qtbot.addWidget(window)
    window.resize(900, 600)
    window.show()
    assert window.load_lbr_csv(session_file)
    view = window.lbr_view
    for channel_id in ("corner_fl.imu_fl_accel_z", "corner_br.imu_br_accel_z"):
        view.browser.set_checked(channel_id, True)
    plot = view.plot
    plot._view().autoRange()
    window.grab()
    spike = plot.curves["corner_fl.imu_fl_accel_z"]._getDisplayDataset()
    assert spike is not None
    assert len(spike.x) < 12000, "decimation should be active"
    assert np.nanmax(spike.y) == generator.SPIKE_VALUE
    gaps = plot.curves["corner_br.imu_br_accel_z"]._getDisplayDataset()
    assert gaps is not None and len(gaps.x) < 12000
    assert np.isnan(gaps.y).any(), "the SENSOR_FAULT gap must survive decimation"
    expected = generator.spike_time_us(seconds) / 1e6
    peak = spike.x[int(np.nanargmax(spike.y))]
    assert abs(peak - expected) < 0.5


def test_measure_reports_every_budgeted_metric(smoke: Path) -> None:
    result = benchmark.measure(smoke)
    assert result["rows"] > 75_000
    assert set(benchmark.BUDGETS) <= set(result)
    assert result["import_rows_per_s"] > 0 and result["peak_mb"] >= result["baseline_mb"] > 0


def test_summary_budgets_and_report(monkeypatch: pytest.MonkeyPatch) -> None:
    runs = [
        {"rows": 10.0, "startup_s": 1.0, "readout_ms": 20.0, "import_rows_per_s": 1e6},
        {"rows": 10.0, "startup_s": 3.0, "readout_ms": 30.0, "import_rows_per_s": 2e5},
        {"rows": 10.0, "startup_s": 2.0, "readout_ms": 10.0, "import_rows_per_s": 1e5},
    ]
    summary = benchmark.summarize(runs)
    assert summary["startup_s"] == {"median": 2.0, "min": 1.0, "max": 3.0}
    budgets = {
        "startup_s": ("max", 3.0),
        "readout_ms": ("max", 16.0),
        "import_rows_per_s": ("min", 3e5),
    }
    monkeypatch.setattr(benchmark, "BUDGETS", budgets)
    misses = benchmark.check_budgets(summary)
    text = benchmark.report("smoke", summary, 3)
    assert [m.split(":")[0] for m in misses] == ["readout_ms", "import_rows_per_s"]
    assert text.startswith("preset smoke, 10 rows, 3 run(s)") and "(budget max 3)" in text


def test_machine_details() -> None:
    details = benchmark.machine()
    assert details["cpu_count"] and details["python"]
    memory = details["memory_gb"]
    assert memory is None or memory > 0
    assert benchmark.peak_memory_mb() > 0


def test_command_line_end_to_end(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    assert benchmark.main(["--preset", "smoke", "--repeat", "1", "--out", str(tmp_path)]) == 0
    result = json.loads((tmp_path / "results-smoke.json").read_text(encoding="utf-8"))
    assert result["preset"] == "smoke" and len(result["runs"]) == 1
    assert result["machine"]["cpu_count"]
    assert "budgets:" in capsys.readouterr().out
    assert generator.main(["--preset", "smoke", str(tmp_path / "x.csv")]) == 0
