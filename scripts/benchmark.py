"""Repeatable dashboard performance measurement (#27).

    uv run --locked python scripts/benchmark.py                    # standard preset, 3 runs
    uv run --locked python scripts/benchmark.py --preset long --repeat 5 --onscreen

Each run happens in a fresh process so startup and peak memory are measured
cleanly. Results (median, min, max per metric, plus machine details) are
printed, compared with the provisional budgets in ``BUDGETS`` and written as
JSON to ``build/bench/``. ``--enforce`` exits 1 when a median misses a budget.
See ``docs/performance.md``.
"""

from __future__ import annotations

import argparse
import ctypes
import json
import os
import platform
import statistics
import subprocess
import sys
import time
from collections.abc import Sequence
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))

import make_bench_session  # noqa: E402

OUT = ROOT / "build" / "bench"
PLOT_ONE = "corner_fl.imu_fl_accel_z"
PLOT_SIX = [f"corner_fl.imu_fl_{kind}_{axis}" for kind in ("accel", "gyro") for axis in "xyz"]

#: Provisional budgets, to be confirmed on the team's reference Windows laptop.
#: (metric, comparison, limit). "min" means the value must be at least the limit.
BUDGETS: dict[str, tuple[str, float]] = {
    "startup_s": ("max", 3.0),
    "import_rows_per_s": ("min", 150_000),
    "plot_one_s": ("max", 0.5),
    "plot_six_s": ("max", 1.0),
    "readout_ms": ("max", 16.0),
    "session_mb_per_million_rows": ("max", 400.0),
}


def peak_memory_mb() -> float:
    """Peak resident memory of this process in MB."""

    if sys.platform == "win32":
        from ctypes import wintypes

        class Counters(ctypes.Structure):
            _fields_ = [
                ("cb", wintypes.DWORD),
                ("PageFaultCount", wintypes.DWORD),
                ("PeakWorkingSetSize", ctypes.c_size_t),
                ("WorkingSetSize", ctypes.c_size_t),
                ("QuotaPeakPagedPoolUsage", ctypes.c_size_t),
                ("QuotaPagedPoolUsage", ctypes.c_size_t),
                ("QuotaPeakNonPagedPoolUsage", ctypes.c_size_t),
                ("QuotaNonPagedPoolUsage", ctypes.c_size_t),
                ("PagefileUsage", ctypes.c_size_t),
                ("PeakPagefileUsage", ctypes.c_size_t),
            ]

        counters = Counters()
        counters.cb = ctypes.sizeof(Counters)
        windll: Any = getattr(ctypes, "windll")
        # Declare signatures: without them the GetCurrentProcess pseudo-handle
        # is truncated to 32 bits and the call fails, reporting 0 bytes.
        current = windll.kernel32.GetCurrentProcess
        current.restype = wintypes.HANDLE
        query = windll.psapi.GetProcessMemoryInfo
        query.argtypes = [wintypes.HANDLE, ctypes.POINTER(Counters), wintypes.DWORD]
        query.restype = wintypes.BOOL
        if not query(current(), ctypes.byref(counters), counters.cb):
            raise OSError("GetProcessMemoryInfo failed")
        return counters.PeakWorkingSetSize / 1e6
    import resource

    peak = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss
    return peak / 1e6 if sys.platform == "darwin" else peak / 1e3


def total_memory_gb() -> float | None:
    try:
        if sys.platform == "win32":

            class Status(ctypes.Structure):
                _fields_ = [
                    ("dwLength", ctypes.c_ulong),
                    ("dwMemoryLoad", ctypes.c_ulong),
                    ("ullTotalPhys", ctypes.c_ulonglong),
                    ("rest", ctypes.c_ulonglong * 6),
                ]

            status = Status()
            status.dwLength = ctypes.sizeof(Status)
            windll: Any = getattr(ctypes, "windll")
            query = windll.kernel32.GlobalMemoryStatusEx
            query.argtypes = [ctypes.POINTER(Status)]
            query.restype = ctypes.c_int
            if not query(ctypes.byref(status)):
                return None
            return status.ullTotalPhys / 1e9
        if sys.platform == "darwin":
            out = subprocess.run(["sysctl", "-n", "hw.memsize"], capture_output=True, text=True)
            return int(out.stdout) / 1e9
        with open("/proc/meminfo", encoding="ascii") as handle:
            return int(handle.readline().split()[1]) / 1e6
    except (OSError, ValueError, AttributeError):
        return None


def machine() -> dict[str, object]:
    return {
        "platform": platform.platform(),
        "processor": platform.processor() or platform.machine(),
        "cpu_count": os.cpu_count(),
        "memory_gb": total_memory_gb(),
        "python": platform.python_version(),
    }


def measure(path: Path) -> dict[str, float]:
    """One run in this process. Imports Qt only here so startup is included."""

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


def summarize(runs: list[dict[str, float]]) -> dict[str, dict[str, float]]:
    return {
        key: {
            "median": statistics.median(run[key] for run in runs),
            "min": min(run[key] for run in runs),
            "max": max(run[key] for run in runs),
        }
        for key in runs[0]
    }


def check_budgets(summary: dict[str, dict[str, float]]) -> list[str]:
    misses = []
    for key, (kind, limit) in BUDGETS.items():
        value = summary[key]["median"]
        if (kind == "max" and value > limit) or (kind == "min" and value < limit):
            misses.append(f"{key}: median {value:.3g} vs {kind} {limit:g}")
    return misses


def report(preset: str, summary: dict[str, dict[str, float]], runs: int) -> str:
    lines = [f"preset {preset}, {int(summary['rows']['median'])} rows, {runs} run(s)"]
    for key, values in summary.items():
        if key == "rows":
            continue
        budget = BUDGETS.get(key)
        note = f"  (budget {budget[0]} {budget[1]:g})" if budget else ""
        lines.append(
            f"  {key:26} median {values['median']:12.4g}   "
            f"min {values['min']:10.4g}   max {values['max']:10.4g}{note}"
        )
    return "\n".join(lines)


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Measure dashboard performance.")
    parser.add_argument("--preset", choices=sorted(make_bench_session.PRESETS), default="standard")
    parser.add_argument("--repeat", type=int, default=3)
    parser.add_argument("--onscreen", action="store_true", help="render on the real display")
    parser.add_argument("--enforce", action="store_true", help="exit 1 if a budget is missed")
    parser.add_argument("--out", type=Path, default=OUT, help="folder for fixtures and results")
    parser.add_argument("--child", type=Path, help=argparse.SUPPRESS)
    options = parser.parse_args(argv)

    if options.child is not None:
        print(json.dumps(measure(options.child)))
        return 0

    path = fixture(options.preset, options.out)
    env = dict(os.environ)
    if not options.onscreen:
        env["QT_QPA_PLATFORM"] = "offscreen"
    runs = []
    for index in range(options.repeat):
        result = subprocess.run(
            [sys.executable, str(Path(__file__).resolve()), "--child", str(path)],
            capture_output=True,
            text=True,
            env=env,
            check=False,
        )
        if result.returncode:
            print(result.stdout + result.stderr, file=sys.stderr)
            return result.returncode
        runs.append(json.loads(result.stdout.strip().splitlines()[-1]))
        print(f"run {index + 1}/{options.repeat} done", flush=True)

    summary = summarize(runs)
    misses = check_budgets(summary)
    print(report(options.preset, summary, len(runs)))
    print("budgets: all met" if not misses else "budgets missed:\n  " + "\n  ".join(misses))
    options.out.mkdir(parents=True, exist_ok=True)
    result_path = options.out / f"results-{options.preset}.json"
    result_path.write_text(
        json.dumps(
            {
                "preset": options.preset,
                "onscreen": options.onscreen,
                "machine": machine(),
                "budgets": BUDGETS,
                "summary": summary,
                "runs": runs,
                "missed": misses,
            },
            indent=2,
        )
        + "\n",
        encoding="utf-8",
    )
    print(f"wrote {result_path}")
    return 1 if options.enforce and misses else 0


if __name__ == "__main__":
    raise SystemExit(main())
