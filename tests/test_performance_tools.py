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
generic = benchmark.bench_generic
system = sys.modules["bench_system"]


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
    window.load_csv(session_file)
    assert window.lbr_view.session is not None
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


LBR_METRICS = {
    "startup_s",
    "import_rows_per_s",
    "plot_one_s",
    "plot_six_s",
    "readout_ms",
    "session_mb_per_million_rows",
}


def test_measure_reports_every_lbr_budget(smoke: Path) -> None:
    result = benchmark.measure(smoke)
    assert result["rows"] > 75_000
    assert set(benchmark.BUDGETS) & set(result) == LBR_METRICS
    assert result["import_rows_per_s"] > 0 and result["peak_mb"] >= result["baseline_mb"] > 0


def _keys(events: list) -> list[tuple[str, int, object]]:
    return [(e.signal_id, e.sample.timestamp_ns, e.sample.value) for e in events]


def test_generic_fixture_is_deterministic_and_mixed_rate() -> None:
    first = list(generic.events(2))
    assert _keys(first) == _keys(list(generic.events(2)))
    assert _keys(first) != _keys(list(generic.events(2, seed=generic.SEED + 1)))
    assert generic.rate_per_second() == 3492 and len(generic.descriptors()) == 36
    assert len(first) == 2 * generic.rate_per_second()
    counts: dict[str, int] = {}
    for event in first:
        counts[event.signal_id] = counts.get(event.signal_id, 0) + 1
    assert counts["bench.strain"] == 2000 and counts["bench.imu.23"] == 200
    assert counts["bench.temp.08"] == 20 and counts["bench.battery.01"] == 2
    times = [event.sample.timestamp_ns for event in first]
    assert times == sorted(times), "a source delivers in measurement-time order"
    strain = [e.sample.timestamp_ns for e in first if e.signal_id == "bench.strain"]
    steps = np.diff(strain)
    assert steps.min() < 1_000_000 < steps.max(), "timestamps are irregular"
    description = generic.describe("quick")
    assert description["samples_per_s"] == 3492 and description["seed"] == generic.SEED
    assert description["replay_samples"] == 20 * 3492


def test_generic_fixture_has_one_spike_and_a_gap() -> None:
    seconds = 4
    records = list(generic.events(seconds))
    start, end = generic.gap_window_ns(seconds)
    for signal_id in generic.GAP_SIGNALS:
        missing = [e.sample for e in records if e.signal_id == signal_id and not e.sample.available]
        assert missing and all(s.quality == generic.GAP_QUALITY for s in missing)
        assert all(start <= s.timestamp_ns < end + 1_000_000_000 for s in missing)
    spikes = [e.sample for e in records if e.sample.value == generic.SPIKE_VALUE]
    assert len(spikes) == 1 and spikes[0].timestamp_ns == generic.spike_time_ns(seconds)


def test_generic_fixture_ingests_and_live_retention_stays_bounded() -> None:
    from lbr_dashboard.core import TelemetrySession

    replay = TelemetrySession(generic.schema())
    live = TelemetrySession(generic.schema(), max_samples_per_signal=generic.LIVE_CAP)
    total = 0
    for batch in generic.batches(generic.events(2)):
        assert len(batch) <= generic.BATCH
        total += replay.ingest(batch)
        live.ingest(batch)
    assert total == 2 * generic.rate_per_second()
    snapshot = live.snapshot()
    assert len(snapshot.series["bench.strain"]) == generic.LIVE_CAP
    assert snapshot.evicted["bench.strain"] == 1000
    assert len(replay.snapshot().series["bench.strain"]) == 2000


def test_time_plot_decimation_keeps_the_fixture_spike_and_gap() -> None:
    from lbr_dashboard.core import TelemetrySession
    from lbr_dashboard.ui.time_plot import NANOSECONDS, POINT_LIMIT, render_points

    seconds = 5
    session = TelemetrySession(generic.schema())
    for batch in generic.batches(generic.events(seconds)):
        session.ingest(batch)
    records = session.snapshot().series[generic.SPIKE_SIGNAL]
    assert len(records) > POINT_LIMIT
    xs, ys, connects = render_points(records, generic.START_NS)
    assert len(xs) < len(records), "decimation should be active"
    assert np.nanmax(ys) == generic.SPIKE_VALUE
    spike = generic.spike_time_ns(seconds) - generic.START_NS
    assert xs[int(np.nanargmax(ys))] == spike / NANOSECONDS
    assert np.isnan(ys).any() and not connects.all(), "the gap must break the line"


def test_generic_csv_fixture_is_deterministic_and_loadable(tmp_path: Path) -> None:
    from lbr_dashboard.core import CancellationToken
    from lbr_dashboard.importers.csv_source import prepare_csv

    path = tmp_path / "a.csv"
    samples = generic.write_csv(path, 300, 5)
    generic.write_csv(tmp_path / "b.csv", 300, 5)
    assert path.read_bytes() == (tmp_path / "b.csv").read_bytes()
    prepared = prepare_csv(path, CancellationToken())
    assert len(prepared.schema.signals) == 5 and len(prepared.records) == samples == 300 * 5
    missing = [sample for _, sample in prepared.records if not sample.available]
    assert len(missing) == pytest.approx(samples / 7, abs=5)


def test_budgets_are_checked_with_margins_and_headroom() -> None:
    budget = benchmark.Budget
    budgets = {
        "latency_ms": budget("max", 10.0, "ms", "x"),
        "rate_per_s": budget("min", 1000.0, "1/s", "x"),
        "absent_ms": budget("max", 1.0, "ms", "x"),
    }
    assert budgets["latency_ms"].allowed(4) == 40 and budgets["rate_per_s"].allowed(4) == 250
    summary = {"latency_ms": {"median": 20.0}, "rate_per_s": {"median": 500.0}}
    strict = {c.metric: c for c in benchmark.check_budgets(summary, 1.0, budgets)}
    assert not strict["latency_ms"].passed and strict["latency_ms"].headroom_pct == -100
    assert not strict["rate_per_s"].passed and strict["rate_per_s"].headroom_pct == -50
    assert not strict["absent_ms"].passed and strict["absent_ms"].value is None
    assert strict["absent_ms"].describe() == "absent_ms: not measured"
    loose = {c.metric: c for c in benchmark.check_budgets(summary, 4.0, budgets)}
    assert loose["latency_ms"].passed and loose["latency_ms"].headroom_pct == 50
    assert loose["rate_per_s"].passed and loose["rate_per_s"].headroom_pct == 100
    assert loose["latency_ms"].describe() == "latency_ms: median 20 vs max 40"
    with pytest.raises(ValueError):
        benchmark.check_budgets(summary, 0.5, budgets)


def test_every_budget_is_well_formed() -> None:
    for name, item in benchmark.BUDGETS.items():
        assert item.kind in {"max", "min"} and item.limit > 0, name
        assert item.unit and item.why, name
    assert benchmark.CI_MARGIN > 1


def test_summary_variance_and_report() -> None:
    runs = [
        {"rows": 10.0, "startup_s": 1.0, "readout_ms": 20.0},
        {"rows": 10.0, "startup_s": 3.0, "readout_ms": 30.0},
        {"rows": 10.0, "startup_s": 2.0, "readout_ms": 10.0},
    ]
    summary = benchmark.summarize(runs)
    assert summary["startup_s"]["median"] == 2.0
    assert (summary["startup_s"]["min"], summary["startup_s"]["max"]) == (1.0, 3.0)
    assert summary["startup_s"]["stdev"] == pytest.approx(1.0)
    assert summary["startup_s"]["spread_pct"] == pytest.approx(100.0)
    assert benchmark.summarize(runs[:1])["startup_s"]["stdev"] == 0.0
    budgets = {
        "startup_s": benchmark.Budget("max", 3.0, "s", "x"),
        "readout_ms": benchmark.Budget("max", 16.0, "ms", "x"),
        "missing_ms": benchmark.Budget("max", 1.0, "ms", "x"),
    }
    checks = benchmark.check_budgets(summary, 1.0, budgets)
    text = benchmark.report("smoke", summary, checks)
    assert text.startswith("smoke: 3 metrics")
    assert "PASS max 3 (+33%)" in text and "FAIL max 16 (-25%)" in text
    assert "missing_ms" in text and "not measured" in text


def test_modes_and_ci_options() -> None:
    full = benchmark.parse([])
    assert (full.name, full.preset, full.workload, full.repeat) == ("full", "standard", "full", 3)
    assert full.margin == 1 and not full.enforce
    quick = benchmark.parse(["--quick"])
    quick_choices = (quick.name, quick.preset, quick.workload, quick.repeat)
    assert quick_choices == ("quick", "smoke", "quick", 2)
    ci = benchmark.parse(["--ci"])
    assert ci.quick and ci.enforce and ci.margin == benchmark.CI_MARGIN and ci.name == "quick"
    custom = benchmark.parse(["--quick", "--workload", "smoke", "--repeat", "1"])
    assert custom.name == "smoke-smoke" and custom.repeat == 1
    for bad in (["--repeat", "0"], ["--margin", "0.5"]):
        with pytest.raises(SystemExit):
            benchmark.parse(bad)


def test_main_writes_json_and_enforces(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    values = {name: item.limit for name, item in benchmark.BUDGETS.items()}
    calls: list[list[str]] = []

    def child(args: list[str], env: dict[str, str]) -> dict[str, float]:
        calls.append(args)
        assert env["QT_QPA_PLATFORM"] == "offscreen"
        return dict(values) if args[0] == "--child-lbr" else {"extra": 1.0}

    monkeypatch.setattr(benchmark, "fixture", lambda preset, out: out / f"{preset}.csv")
    monkeypatch.setattr(benchmark, "run_child", child)
    assert benchmark.main(["--ci", "--repeat", "2", "--out", str(tmp_path)]) == 0
    assert len(calls) == 4 and calls[1][:2] == ["--child-generic", "quick"]
    result = json.loads((tmp_path / "results-quick.json").read_text(encoding="utf-8"))
    assert result["passed"] and result["margin"] == benchmark.CI_MARGIN
    assert result["workload"]["name"] == "quick" and result["machine"]["cpu_count"]
    assert result["budgets"]["startup_s"]["allowed"] == 3.0 * benchmark.CI_MARGIN
    assert len(result["runs"]) == 2 and len(result["checks"]) == len(benchmark.BUDGETS)
    values["startup_s"] = 3.0 * benchmark.CI_MARGIN * 2
    assert benchmark.main(["--ci", "--repeat", "1", "--out", str(tmp_path)]) == 1
    assert "budgets missed:\n  startup_s" in capsys.readouterr().out
    assert benchmark.main(["--quick", "--repeat", "1", "--out", str(tmp_path)]) == 0

    def broken(args: list[str], env: dict[str, str]) -> dict[str, float]:
        raise RuntimeError("child crashed")

    monkeypatch.setattr(benchmark, "run_child", broken)
    assert benchmark.main(["--quick", "--out", str(tmp_path)]) == 2


def test_machine_details() -> None:
    details = benchmark.machine()
    assert details["cpu_count"] and details["python"] and details["processor"]
    packages = details["packages"]
    assert isinstance(packages, dict) and packages["numpy"]
    memory = details["memory_gb"]
    assert memory is None or memory > 0
    assert benchmark.peak_memory_mb() > 0
    assert 0 < system.current_memory_mb() <= benchmark.peak_memory_mb() * 1.01


def test_budgets_and_ci_are_documented() -> None:
    text = (ROOT / "docs" / "performance.md").read_text(encoding="utf-8")
    for name in benchmark.BUDGETS:
        assert f"`{name}`" in text, f"{name} is not documented"
    assert f"{benchmark.CI_MARGIN:g}x" in text and "--ci" in text
    workflow = (ROOT / ".github" / "workflows" / "performance.yml").read_text(encoding="utf-8")
    assert "windows-latest" in workflow and "ubuntu-latest" in workflow
    assert "scripts/benchmark.py --ci" in workflow and "build/bench/results-*.json" in workflow


def test_command_line_end_to_end(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    argv = ["--preset", "smoke", "--workload", "smoke", "--repeat", "1", "--out", str(tmp_path)]
    assert benchmark.main(argv) == 0
    result = json.loads((tmp_path / "results-smoke-smoke.json").read_text(encoding="utf-8"))
    assert result["preset"] == "smoke" and len(result["runs"]) == 1
    assert set(benchmark.BUDGETS) <= set(result["runs"][0])
    assert result["runs"][0]["live_retained_max"] == generic.LIVE_CAP
    assert result["machine"]["cpu_count"] and len(result["checks"]) == len(benchmark.BUDGETS)
    # Smoke sizes are too small to judge budgets; only the report must be there.
    out = capsys.readouterr().out
    assert "budgets: all met" in out or "budgets missed:" in out
    assert generator.main(["--preset", "smoke", str(tmp_path / "x.csv")]) == 0
