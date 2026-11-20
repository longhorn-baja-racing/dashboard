"""Repeatable dashboard performance measurement (#27).

    uv run --locked python scripts/benchmark.py            # full mode, 3 runs
    uv run --locked python scripts/benchmark.py --quick    # CI-sized, 2 runs, under a minute
    uv run --locked python scripts/benchmark.py --ci       # --quick, fail only on gross regressions
    uv run --locked python scripts/benchmark.py --preset long --repeat 5 --onscreen

Each run measures the lbr-csv session view and the generic session stack
(``bench_generic``) in two fresh processes, so startup and peak memory are
measured cleanly. Results (median, min, max and spread per metric, plus
machine details) are printed, checked against ``BUDGETS`` and written as JSON
to ``build/bench/``. ``--enforce`` exits 1 when a median misses its budget
after applying ``--margin``. See ``docs/performance.md``.
"""

from __future__ import annotations

import argparse
import json
import os
import statistics
import subprocess
import sys
import time
from collections.abc import Mapping, Sequence
from dataclasses import asdict, dataclass
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))

import bench_generic  # noqa: E402
import make_bench_session  # noqa: E402
from bench_system import machine, peak_memory_mb  # noqa: E402

OUT = ROOT / "build" / "bench"
PLOT_ONE = "corner_fl.imu_fl_accel_z"
PLOT_SIX = [f"corner_fl.imu_fl_{kind}_{axis}" for kind in ("accel", "gyro") for axis in "xyz"]


@dataclass(frozen=True)
class Budget:
    """``max``: the median must not exceed ``limit``; ``min``: it must reach it."""

    kind: str
    limit: float
    unit: str
    why: str

    def allowed(self, margin: float = 1.0) -> float:
        """The limit loosened by ``margin`` (1 = the budget itself, 4 = CI)."""

        return self.limit * margin if self.kind == "max" else self.limit / margin


#: Every budget, in one place. They are provisional until measured on the
#: reference Windows laptop; docs/performance.md explains each one.
BUDGETS: dict[str, Budget] = {
    # lbr-csv session view
    "startup_s": Budget("max", 3.0, "s", "usable within a few seconds at the track"),
    "import_rows_per_s": Budget("min", 150_000, "rows/s", "20 min full-rate export in < 1 min"),
    "plot_one_s": Budget("max", 0.5, "s", "checking a signal feels immediate"),
    "plot_six_s": Budget("max", 1.0, "s", "comparing a few signals stays fluid"),
    "readout_ms": Budget("max", 16.0, "ms", "one 60 Hz frame while moving the cursor"),
    "session_mb_per_million_rows": Budget("max", 400.0, "MB", "a 2 h export fits in 8 GB"),
    # generic session stack: replay
    "ingest_samples_per_s": Budget("min", 100_000, "samples/s", "20 min at 3492/s in < 45 s"),
    "snapshot_ms": Budget("max", 50.0, "ms", "a snapshot per poll never stalls several frames"),
    "range_ms": Budget("max", 1.0, "ms", "a 10 s window query is a fraction of a frame"),
    "session_mb_per_million_samples": Budget("max", 600.0, "MB", "10 min fits in ~1.3 GB"),
    "generic_peak_mb": Budget("max", 2000.0, "MB", "full workload fits an 8 GB laptop"),
    # generic session stack: time plot and replay seek
    "plot_full_ms": Budget("max", 250.0, "ms", "fitting the whole 1 kHz trace feels immediate"),
    "plot_zoom_ms": Budget("max", 50.0, "ms", "pan and zoom on a 10 s window stay smooth"),
    "seek_ms": Budget("max", 16.0, "ms", "a typical seek lands within one frame"),
    "seek_p95_ms": Budget("max", 50.0, "ms", "dragging the replay slider never stutters"),
    # generic session stack: sustained live ingest with bounded retention
    "live_cpu_pct": Budget("max", 25.0, "% of a core", "live ingest leaves room to render"),
    "live_poll_p95_ms": Budget("max", 10.0, "ms", "each 30 ms poll leaves time to draw"),
    "live_rss_mb_per_min": Budget("max", 16.0, "MB/min", "memory stops growing once full"),
}

#: CI runners are shared and noisy, so CI only fails when a median misses its
#: budget by this factor (4x slower, 4x lower throughput, 4x more memory).
CI_MARGIN = 4.0


@dataclass(frozen=True)
class Mode:
    preset: str
    workload: str
    repeat: int


MODES = {
    "quick": Mode("smoke", "quick", 2),
    "full": Mode("standard", "full", 3),
}


@dataclass(frozen=True)
class Check:
    metric: str
    value: float | None
    kind: str
    limit: float
    allowed: float
    passed: bool
    #: How far inside the allowed limit the median is, in percent of that
    #: limit; negative means missed. ``None`` when the metric was not measured.
    headroom_pct: float | None

    def describe(self) -> str:
        if self.value is None:
            return f"{self.metric}: not measured"
        return f"{self.metric}: median {self.value:.4g} vs {self.kind} {self.allowed:g}"


def measure(path: Path) -> dict[str, float]:
    """One lbr-csv run in this process. Imports Qt only here so startup is included."""

    started = time.perf_counter()
    from lbr_dashboard.app import create_application, create_main_window
    from lbr_dashboard.importers.lbr_csv import LbrCsvImporter

    application = create_application([])
    window = create_main_window()
    window.resize(1400, 850)
    window.show()
    application.processEvents()
    startup = time.perf_counter() - started
    baseline = peak_memory_mb()

    began = time.perf_counter()
    session = LbrCsvImporter().import_session(path)
    import_s = time.perf_counter() - began
    del session

    began = time.perf_counter()
    window.load_csv(path)
    application.processEvents()
    open_s = time.perf_counter() - began
    view = window.lbr_view
    if view.session is None:
        raise RuntimeError(f"could not open {path}")
    rows = view.session.row_count if view.session is not None else 0

    def plot(ids: list[str]) -> float:
        view.browser.clear_checks()
        application.processEvents()
        began = time.perf_counter()
        for channel_id in ids:
            view.browser.set_checked(channel_id, True)
        application.processEvents()
        window.grab()
        return time.perf_counter() - began

    plot_one = plot([PLOT_ONE])
    plot_six = plot(PLOT_SIX)
    end_us = int(view.plot.channels[0].times_us[-1])
    samples = []
    for step in range(200):
        t_us = 1_000_000 + (end_us - 1_000_000) * step // 199
        began = time.perf_counter()
        view.plot.show_readout(t_us)
        samples.append((time.perf_counter() - began) * 1000)
    peak = peak_memory_mb()
    window.close()
    return {
        "rows": float(rows),
        "startup_s": startup,
        "import_s": import_s,
        "import_rows_per_s": rows / import_s,
        "open_s": open_s,
        "plot_one_s": plot_one,
        "plot_six_s": plot_six,
        "readout_ms": statistics.median(samples),
        "peak_mb": peak,
        "baseline_mb": baseline,
        "session_mb_per_million_rows": (peak - baseline) / (rows / 1e6),
    }


def fixture(preset: str, out: Path) -> Path:
    path = out / f"{preset}.csv"
    if not path.exists():
        print(f"generating {path} ...", flush=True)
        make_bench_session.generate(path, make_bench_session.PRESETS[preset])
    return path


def summarize(runs: Sequence[Mapping[str, float]]) -> dict[str, dict[str, float]]:
    """Median, min, max, sample standard deviation and spread (max - min) / median."""

    summary = {}
    for key in runs[0]:
        values = [run[key] for run in runs]
        median = statistics.median(values)
        summary[key] = {
            "median": median,
            "min": min(values),
            "max": max(values),
            "stdev": statistics.stdev(values) if len(values) > 1 else 0.0,
            "spread_pct": 100 * (max(values) - min(values)) / median if median else 0.0,
        }
    return summary


def check_budgets(
    summary: Mapping[str, Mapping[str, float]],
    margin: float = 1.0,
    budgets: Mapping[str, Budget] | None = None,
) -> list[Check]:
    """Compare each budgeted median with its limit loosened by ``margin``."""

    if margin < 1:
        raise ValueError("margin must be at least 1")
    checks = []
    for key, budget in (BUDGETS if budgets is None else budgets).items():
        allowed = budget.allowed(margin)
        if key not in summary:
            checks.append(Check(key, None, budget.kind, budget.limit, allowed, False, None))
            continue
        value = summary[key]["median"]
        if budget.kind == "max":
            passed = value <= allowed
            headroom = 100 * (allowed - value) / allowed
        else:
            passed = value >= allowed
            headroom = 100 * (value - allowed) / allowed
        checks.append(Check(key, value, budget.kind, budget.limit, allowed, passed, headroom))
    return checks


def report(label: str, summary: Mapping[str, Mapping[str, float]], checks: list[Check]) -> str:
    by_metric = {check.metric: check for check in checks}
    lines = [f"{label}: {len(summary)} metrics"]
    for key, values in summary.items():
        check = by_metric.get(key)
        note = ""
        if check is not None:
            verdict = "PASS" if check.passed else "FAIL"
            note = f"  {verdict} {check.kind} {check.allowed:g} ({check.headroom_pct:+.0f}%)"
        lines.append(
            f"  {key:32} median {values['median']:11.4g}  "
            f"min {values['min']:10.4g}  max {values['max']:10.4g}  "
            f"spread {values['spread_pct']:5.1f}%{note}"
        )
    for check in checks:
        if check.value is None:
            lines.append(f"  {check.metric:32} not measured  FAIL")
    return "\n".join(lines)


def run_child(args: list[str], env: Mapping[str, str]) -> dict[str, float]:
    result = subprocess.run(
        [sys.executable, str(Path(__file__).resolve()), *args],
        capture_output=True,
        text=True,
        env=dict(env),
        check=False,
    )
    if result.returncode:
        raise RuntimeError(f"benchmark child {args} failed:\n{result.stdout}{result.stderr}")
    return json.loads(result.stdout.strip().splitlines()[-1])


def parse(argv: Sequence[str] | None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Measure dashboard performance.")
    parser.add_argument("--quick", action="store_true", help="CI-sized workloads, 2 runs")
    parser.add_argument(
        "--ci",
        action="store_true",
        help=f"--quick --enforce --margin {CI_MARGIN:g}: fail only on gross regressions",
    )
    parser.add_argument("--preset", choices=sorted(make_bench_session.PRESETS))
    parser.add_argument("--workload", choices=sorted(bench_generic.WORKLOADS))
    parser.add_argument("--repeat", type=int)
    parser.add_argument("--margin", type=float, help="loosen every budget by this factor")
    parser.add_argument("--onscreen", action="store_true", help="render on the real display")
    parser.add_argument("--enforce", action="store_true", help="exit 1 if a budget is missed")
    parser.add_argument("--out", type=Path, default=OUT, help="folder for fixtures and results")
    parser.add_argument("--child-lbr", type=Path, help=argparse.SUPPRESS)
    parser.add_argument("--child-generic", help=argparse.SUPPRESS)
    options = parser.parse_args(argv)
    if options.ci:
        options.quick = options.enforce = True
        options.margin = CI_MARGIN if options.margin is None else options.margin
    mode = MODES["quick" if options.quick else "full"]
    name = "quick" if options.quick else "full"
    if options.preset is not None or options.workload is not None:
        name = f"{options.preset or mode.preset}-{options.workload or mode.workload}"
    options.name = name
    options.preset = options.preset or mode.preset
    options.workload = options.workload or mode.workload
    options.repeat = mode.repeat if options.repeat is None else options.repeat
    options.margin = 1.0 if options.margin is None else options.margin
    if options.repeat < 1:
        parser.error("--repeat must be at least 1")
    if options.margin < 1:
        parser.error("--margin must be at least 1")
    return options


def main(argv: Sequence[str] | None = None) -> int:
    options = parse(argv)
    if options.child_lbr is not None:
        print(json.dumps(measure(options.child_lbr)))
        return 0
    if options.child_generic is not None:
        print(json.dumps(bench_generic.measure(options.child_generic, options.out)))
        return 0

    began = time.perf_counter()
    path = fixture(options.preset, options.out)
    env = dict(os.environ)
    if not options.onscreen:
        env["QT_QPA_PLATFORM"] = "offscreen"
    runs = []
    try:
        for index in range(options.repeat):
            run = run_child(["--child-lbr", str(path)], env)
            run.update(
                run_child(
                    ["--child-generic", options.workload, "--out", str(options.out)],
                    env,
                )
            )
            runs.append(run)
            print(f"run {index + 1}/{options.repeat} done", flush=True)
    except RuntimeError as error:
        print(error, file=sys.stderr)
        return 2

    summary = summarize(runs)
    checks = check_budgets(summary, options.margin)
    missed = [check.describe() for check in checks if not check.passed]
    label = (
        f"{options.name}: lbr-csv preset {options.preset}, generic workload "
        f"{options.workload}, {len(runs)} run(s), margin {options.margin:g}"
    )
    print(report(label, summary, checks))
    print("budgets: all met" if not missed else "budgets missed:\n  " + "\n  ".join(missed))
    options.out.mkdir(parents=True, exist_ok=True)
    result_path = options.out / f"results-{options.name}.json"
    result_path.write_text(
        json.dumps(
            {
                "mode": options.name,
                "preset": options.preset,
                "lbr_seconds": make_bench_session.PRESETS[options.preset],
                "workload": bench_generic.describe(options.workload),
                "onscreen": options.onscreen,
                "margin": options.margin,
                "machine": machine(),
                "budgets": {
                    key: {**asdict(budget), "allowed": budget.allowed(options.margin)}
                    for key, budget in BUDGETS.items()
                },
                "summary": summary,
                "checks": [asdict(check) for check in checks],
                "runs": runs,
                "missed": missed,
                "passed": not missed,
                "elapsed_s": time.perf_counter() - began,
            },
            indent=2,
        )
        + "\n",
        encoding="utf-8",
    )
    print(f"wrote {result_path} in {time.perf_counter() - began:.0f} s")
    return 1 if options.enforce and missed else 0


if __name__ == "__main__":
    raise SystemExit(main())
