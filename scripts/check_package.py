"""Install and smoke-test both distributions outside the source checkout."""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path
from tempfile import TemporaryDirectory

ROOT = Path(__file__).resolve().parents[1]
SMOKE = """
import sys
from importlib.metadata import distribution
from pathlib import Path
from PySide6.QtCore import QTimer, QEventLoop
import lbr_dashboard
from lbr_dashboard.app import create_application, create_main_window, main
from lbr_dashboard.resources import resource_text
from lbr_dashboard.version import __version__

assert Path(lbr_dashboard.__file__).resolve().is_relative_to(Path(sys.prefix).resolve())
metadata = distribution("lbr-dashboard")
assert metadata.version == __version__
entry = next(e for e in metadata.entry_points if e.name == "lbr-dashboard")
assert entry.load() is main
assert "__version__" in resource_text("version.py")
application = create_application([])
window = create_main_window()
window.show()
assert window.import_state == "empty"
window.demo_button.click()
assert window.right_panel.table.rowCount() == 4
assert window._controller is not None
window._controller.start_import(sys.argv[1])
loop = QEventLoop()
poll = QTimer()
poll.timeout.connect(lambda: loop.quit() if window.import_state != "loading" else None)
poll.start(10)
QTimer.singleShot(10000, loop.quit)
loop.exec()
poll.stop()
assert window.import_state == "ready", window.import_message.text()
assert window.left_panel.list_widget.count() == 16
assert window.right_panel.table.rowCount() == 10
window.left_panel.list_widget.setCurrentRow(1)
plot = window.right_panel.plot_widget.getPlotItem()
assert plot is not None and len(plot.listDataItems()) == 1
window.close()
QTimer.singleShot(0, application.quit)
assert main([]) == 0
print("Installed package imports, resources, CSV, plotting, and entry point passed.")
"""


def run(*args: str, cwd: Path = ROOT) -> None:
    subprocess.run(args, cwd=cwd, check=True, timeout=180)


def main() -> None:
    wheels = list((ROOT / "dist").glob("*.whl"))
    archives = list((ROOT / "dist").glob("*.tar.gz"))
    assert len(wheels) == len(archives) == 1, "Build one wheel and one sdist in a clean dist/"
    with TemporaryDirectory(prefix="lbr-package-") as temporary:
        directory = Path(temporary)
        environment = directory / "environment"
        python = environment / ("Scripts/python.exe" if os.name == "nt" else "bin/python")
        requirements = directory / "requirements.txt"
        run("uv", "venv", "--python", sys.executable, str(environment))
        run(
            "uv",
            "--quiet",
            "export",
            "--locked",
            "--no-dev",
            "--no-emit-project",
            "--format",
            "requirements-txt",
            "--output-file",
            str(requirements),
        )
        run(
            "uv",
            "pip",
            "install",
            "--python",
            str(python),
            "--require-hashes",
            "-r",
            str(requirements),
        )
        for artifact in [wheels[0], archives[0]]:
            run(
                "uv",
                "pip",
                "install",
                "--python",
                str(python),
                "--no-deps",
                "--force-reinstall",
                str(artifact),
            )
            # -I prevents PYTHONPATH and the current directory hiding missing package files.
            run(
                str(python),
                "-I",
                "-c",
                SMOKE,
                str(ROOT / "sample_data" / "sample_baja_log.csv"),
                cwd=directory,
            )
            run("uv", "pip", "check", "--python", str(python))


if __name__ == "__main__":
    main()
