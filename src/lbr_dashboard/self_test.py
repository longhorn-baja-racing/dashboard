"""Built-in smoke check for packaged builds.

A portable build runs ``LBRDashboard.exe --self-test --report <file>`` to prove
that it starts without an installed Python, finds its Qt plugins, opens the
bundled sample CSV, plots it and ships its license notices. The packaged app
is a windowed program with no console, so results go to the report file and
the exit code, never to stdout.
"""

from __future__ import annotations

import argparse
import sys
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
from pathlib import Path

from .version import __version__

SELF_TEST_FLAG = "--self-test"
SAMPLE_NAME = "sample_baja_log.csv"
#: Files every portable bundle must carry next to the executable.
REQUIRED_BUNDLE_FILES = ("LICENSE", "notices/THIRD-PARTY-NOTICES.txt", "build-info.json")
PASSED = "self-test passed"
FAILED = "self-test FAILED"


@dataclass
class SelfTestResult:
    """Outcome of each named check, in order."""

    checks: list[tuple[str, bool, str]] = field(default_factory=list)

    @property
    def passed(self) -> bool:
        return bool(self.checks) and all(ok for _, ok, _ in self.checks)

    def record(self, name: str, ok: bool, detail: str = "") -> bool:
        self.checks.append((name, ok, detail))
        return ok

    def report(self) -> str:
        lines = [f"LBR Dashboard {__version__} self-test"]
        for name, ok, detail in self.checks:
            lines.append(f"{'ok  ' if ok else 'FAIL'} {name}" + (f": {detail}" if detail else ""))
        lines.append(PASSED if self.passed else FAILED)
        return "\n".join(lines) + "\n"


def bundle_root() -> Path | None:
    """Folder holding the packaged executable, or None when running from source."""

    if getattr(sys, "frozen", False):
        return Path(sys.executable).resolve().parent
    return None


def _check(result: SelfTestResult, name: str, action: Callable[[], str]) -> bool:
    try:
        detail = action()
    except Exception as error:  # Any failure is a finding, never a crash.
        return result.record(name, False, f"{type(error).__name__}: {error}")
    return result.record(name, True, detail)


def run(sample: Path, root: Path | None = None) -> SelfTestResult:
    """Start the dashboard, open ``sample`` and plot one of its columns."""

    result = SelfTestResult()
    if root is not None:
        for relative in REQUIRED_BUNDLE_FILES:
            present = (root / relative).is_file()
            result.record(f"bundle has {relative}", present, "" if present else "missing")

    from .app import create_application, create_main_window
    from .importers.csv_importer import CsvImporter

    application = create_application([sys.argv[0] if sys.argv else "lbr-dashboard"])
    window = create_main_window()

    def start() -> str:
        window.show()
        application.processEvents()
        return f"Qt platform {application.platformName()}"

    def open_sample() -> str:
        expected = CsvImporter().import_session(sample)
        if not expected.headers or not expected.rows:
            raise ValueError(f"{sample.name} has no data")
        window.load_csv(sample)
        shown = window.left_panel.list_widget.count()
        if shown != len(expected.headers):
            raise ValueError(f"listed {shown} columns, expected {len(expected.headers)}")
        return f"{sample.name}: {len(expected.headers)} columns, {len(expected.rows)} rows"

    def plot() -> str:
        window.left_panel.list_widget.setCurrentRow(1)
        application.processEvents()
        item = window.right_panel.plot_widget.getPlotItem()
        curves = len(item.listDataItems()) if item is not None else 0
        if curves != 1:
            raise ValueError(f"{curves} curves drawn, expected 1")
        image = window.grab()
        if image.isNull():
            raise ValueError("window could not be rendered")
        return f"rendered {image.width()}x{image.height()}"

    try:
        if _check(result, "start the main window", start) and _check(
            result, "open the sample CSV", open_sample
        ):
            _check(result, "plot a column", plot)
    finally:
        window.close()
    return result


def main(argv: Sequence[str] | None = None) -> int:
    """Run the self-test; 0 when every check passed."""

    parser = argparse.ArgumentParser(prog="LBRDashboard", add_help=False)
    parser.add_argument(SELF_TEST_FLAG, action="store_true", required=True)
    parser.add_argument("--report", type=Path, required=True)
    parser.add_argument("--sample", type=Path)
    options, _ = parser.parse_known_args(argv)

    root = bundle_root()
    sample = options.sample or ((root or Path.cwd()) / "samples" / SAMPLE_NAME)
    result = run(sample, root)
    options.report.write_text(result.report(), encoding="utf-8", newline="\n")
    return 0 if result.passed else 1
