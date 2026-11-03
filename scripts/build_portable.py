"""Build the portable dashboard ZIP (issue #52).

    uv sync --locked --extra package
    uv run --locked python scripts/build_portable.py

PyInstaller freezes ``packaging/entry.py`` into a one-folder app (the Qt
libraries stay separate, replaceable DLLs). This script is the only build
configuration: it adds the project license, every third-party notice, the
sample data, a README and ``build-info.json``, then writes a ZIP with sorted
entries and fixed timestamps plus its SHA-256. The same inputs (commit,
``uv.lock``, Python and PyInstaller versions) give the same ZIP layout.

Output: ``dist/portable/LBRDashboard-<version>-<platform>.zip`` and ``.sha256``.
See ``docs/portable-build.md``.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import platform
import re
import shutil
import subprocess
import sys
import sysconfig
import time
import zipfile
from collections.abc import Sequence
from importlib import metadata
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))

import collect_notices  # noqa: E402

APP_NAME = "LBRDashboard"
PRODUCT_NAME = "LBR Dashboard"
COMPANY = "Longhorn Baja Racing"
COPYRIGHT = "Copyright (c) 2026 Longhorn Baja Racing. MIT License."
ENTRY = ROOT / "packaging" / "entry.py"
BUILD_DIR = ROOT / "build" / "portable"
OUT_DIR = ROOT / "dist" / "portable"
SAMPLES = ROOT / "sample_data"
#: Earliest time a ZIP entry can carry (1980-01-01T00:00:00Z).
ZIP_EPOCH = 315532800

#: Qt modules the dashboard never uses. Excluding them keeps browser engines,
#: QML and 3D runtimes out of the ZIP even if a dependency probes for them.
EXCLUDED_MODULES = (
    "tkinter",
    "PySide6.QtWebEngineCore",
    "PySide6.QtWebEngineWidgets",
    "PySide6.QtWebEngineQuick",
    "PySide6.QtWebChannel",
    "PySide6.QtWebSockets",
    "PySide6.QtPdf",
    "PySide6.QtPdfWidgets",
    "PySide6.QtQml",
    "PySide6.QtQuick",
    "PySide6.QtQuickWidgets",
    "PySide6.QtQuick3D",
    "PySide6.Qt3DCore",
    "PySide6.Qt3DRender",
    "PySide6.QtMultimedia",
    "PySide6.QtMultimediaWidgets",
    "PySide6.QtCharts",
    "PySide6.QtDataVisualization",
    "PySide6.QtGraphs",
    "PySide6.QtDesigner",
)
#: Qt libraries the dashboard does not use. Two Qt plugins collected by the
#: PySide6 hook (the virtual keyboard input method and the PDF image format)
#: link to them, so those plugins and these libraries are removed after the
#: build. ``scripts/smoke_portable.py`` proves the app still runs without them.
UNUSED_QT = re.compile(
    r"virtualkeyboard[^/]*$|imageformats/[^/]*qpdf[^/]*$"
    r"|Qt6?(WebEngine|Pdf|Quick|Qml|3D|Multimedia|Charts|DataVisualization|Graphs)"
    r"[^/]*\.(dll|so[.0-9]*|dylib|pyd)$",
    re.IGNORECASE,
)

README = """\
LBR Dashboard {version} (portable)
=================================

Telemetry dashboard for Longhorn Baja Racing. This folder runs as is; it does
not need Python or administrator rights.

Run
---
1. Extract the whole ZIP to a folder you can write to, for example your
   Documents or Desktop. Do not run it from inside the ZIP.
2. Double-click {exe}.
3. Use File > Open to open a CSV. Example files are in the samples folder.

The build is not code signed, so Windows SmartScreen may say it protected
your PC. Choose "More info", then "Run anyway".

Where data is written
---------------------
Nothing is written inside this folder, so it can live on a read-only drive.
Per-user data such as logs and preferences goes under your %LOCALAPPDATA%
folder. Deleting this folder removes the program but never your telemetry
files.

Check a copy
------------
{exe} --self-test --report report.txt writes a short check of this copy to
report.txt and exits with code 0 when everything passed.

Licenses
--------
The dashboard is MIT licensed (LICENSE). It bundles Python, Qt for Python and
other libraries under their own licenses; see notices\\THIRD-PARTY-NOTICES.txt.

Version {version}, commit {commit}, built with Python {python}.
"""


def project_version() -> str:
    text = (ROOT / "src" / "lbr_dashboard" / "version.py").read_text(encoding="utf-8")
    match = re.search(r'__version__\s*=\s*"([^"]+)"', text)
    if match is None:
        raise ValueError("no __version__ in src/lbr_dashboard/version.py")
    return match.group(1)


def version_tuple(version: str) -> tuple[int, int, int, int]:
    """Numeric Windows file version, e.g. ``0.2.0rc1`` -> ``(0, 2, 0, 0)``."""

    numbers = [int(part) for part in re.findall(r"\d+", version.split("+")[0])[:3]]
    major, minor, patch = (numbers + [0, 0, 0])[:3]
    return (major, minor, patch, 0)


def platform_tag(platform_name: str | None = None) -> str:
    name = platform_name or sysconfig.get_platform()
    table = {
        "win-amd64": "windows-x64",
        "win-arm64": "windows-arm64",
        "win32": "windows-x86",
        "linux-x86_64": "linux-x64",
        "linux-aarch64": "linux-arm64",
    }
    if name in table:
        return table[name]
    if name.startswith("macosx"):
        return "macos-" + ("arm64" if name.endswith("arm64") else "x64")
    return re.sub(r"[^a-z0-9]+", "-", name.lower())


def windows_version_file(version: str) -> str:
    """PyInstaller version resource: product name, company and version in Explorer."""

    numbers = version_tuple(version)
    strings = {
        "CompanyName": COMPANY,
        "FileDescription": PRODUCT_NAME,
        "FileVersion": version,
        "InternalName": APP_NAME,
        "LegalCopyright": COPYRIGHT,
        "OriginalFilename": f"{APP_NAME}.exe",
        "ProductName": PRODUCT_NAME,
        "ProductVersion": version,
    }
    table = ",\n".join(f"          StringStruct({k!r}, {v!r})" for k, v in strings.items())
    return f"""VSVersionInfo(
  ffi=FixedFileInfo(
    filevers={numbers}, prodvers={numbers}, mask=0x3F, flags=0x0,
    OS=0x40004, fileType=0x1, subtype=0x0, date=(0, 0)),
  kids=[
    StringFileInfo([
      StringTable('040904B0', [
{table}
      ])
    ]),
    VarFileInfo([VarStruct('Translation', [1033, 1200])])
  ]
)
"""


def _git(*args: str) -> str | None:
    try:
        completed = subprocess.run(
            ["git", *args], cwd=ROOT, capture_output=True, text=True, check=True
        )
    except (OSError, subprocess.CalledProcessError):
        return None
    return completed.stdout.strip() or None


def source_date_epoch(environ: dict[str, str] | None = None) -> int:
    """``SOURCE_DATE_EPOCH``, else the commit time, never before 1980."""

    env = os.environ if environ is None else environ
    value = env.get("SOURCE_DATE_EPOCH") or _git("log", "-1", "--format=%ct") or "0"
    return max(int(value), ZIP_EPOCH)


def sha256_of(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1 << 20), b""):
            digest.update(chunk)
    return digest.hexdigest()


def build_info(version: str, epoch: int) -> dict[str, str | int]:
    return {
        "name": PRODUCT_NAME,
        "version": version,
        "commit": _git("rev-parse", "HEAD") or "unknown",
        "platform": platform_tag(),
        "python": platform.python_version(),
        "pyinstaller": metadata.version("pyinstaller"),
        "pyside6": metadata.version("PySide6"),
        "uv_lock_sha256": sha256_of(ROOT / "uv.lock"),
        "source_date_epoch": epoch,
    }


def pyinstaller_arguments(version_file: Path | None) -> list[str]:
    arguments = [
        str(ENTRY),
        "--name",
        APP_NAME,
        "--onedir",
        "--windowed",
        "--noconfirm",
        "--clean",
        "--log-level",
        "WARN",
        "--distpath",
        str(BUILD_DIR / "dist"),
        "--workpath",
        str(BUILD_DIR / "work"),
        "--specpath",
        str(BUILD_DIR),
        "--paths",
        str(ROOT / "src"),
        "--collect-submodules",
        "lbr_dashboard",
    ]
    for module in EXCLUDED_MODULES:
        arguments += ["--exclude-module", module]
    if version_file is not None:
        arguments += ["--version-file", str(version_file)]
    return arguments


def run_pyinstaller(version: str) -> Path:
    """Freeze the app and return the one-folder output."""

    import PyInstaller.__main__

    BUILD_DIR.mkdir(parents=True, exist_ok=True)
    version_file = None
    if sys.platform == "win32":
        version_file = BUILD_DIR / "version-info.txt"
        version_file.write_text(windows_version_file(version), encoding="utf-8")
    PyInstaller.__main__.run(pyinstaller_arguments(version_file))
    return BUILD_DIR / "dist" / APP_NAME


def pyinstaller_notice() -> collect_notices.Component:
    """The bootloader inside the executable comes from PyInstaller."""

    distribution = metadata.distribution("pyinstaller")
    return collect_notices.Component(
        name="pyinstaller-bootloader",
        version=distribution.version,
        license="GPL-2.0-or-later WITH Bootloader-exception",
        files=collect_notices.license_files(distribution),
    )


def add_bundle_files(bundle: Path, info: dict[str, str | int]) -> None:
    """Put the license, notices, samples, README and build info beside the exe."""

    shutil.copyfile(ROOT / "LICENSE", bundle / "LICENSE")
    components = [*collect_notices.runtime_components(), pyinstaller_notice()]
    notices = bundle / "notices"
    if notices.exists():
        shutil.rmtree(notices)
    collect_notices.collect(notices, components)
    samples = bundle / "samples"
    samples.mkdir(exist_ok=True)
    for sample in sorted(SAMPLES.glob("*.csv")):
        shutil.copyfile(sample, samples / sample.name)
    exe = f"{APP_NAME}.exe" if sys.platform == "win32" else APP_NAME
    readme = README.format(
        version=info["version"], exe=exe, commit=info["commit"], python=info["python"]
    )
    (bundle / "README.txt").write_bytes(readme.replace("\n", "\r\n").encode("ascii"))
    (bundle / "build-info.json").write_text(
        json.dumps(info, indent=2, sort_keys=True) + "\n", encoding="utf-8", newline="\n"
    )


def files_in(folder: Path) -> list[Path]:
    """Every file below ``folder``, ordered by exact relative name on every OS.

    Windows paths compare case-insensitively, so sorting ``Path`` objects would
    give a different order (and a different ZIP) than on Linux.
    """

    return sorted(
        (path for path in folder.rglob("*") if path.is_file()),
        key=lambda path: path.relative_to(folder).as_posix(),
    )


def unused_qt_files(bundle: Path) -> list[Path]:
    return [
        path for path in files_in(bundle) if UNUSED_QT.search(path.relative_to(bundle).as_posix())
    ]


def prune_unused_qt(bundle: Path) -> list[str]:
    """Delete the unused Qt libraries and the plugins that pull them in."""

    removed = []
    for path in unused_qt_files(bundle):
        removed.append(path.relative_to(bundle).as_posix())
        path.unlink()
    return removed


def write_zip(source: Path, destination: Path, top: str, epoch: int) -> Path:
    """Zip ``source`` under ``top/`` with sorted names and one fixed timestamp."""

    stamp = time.gmtime(max(epoch, ZIP_EPOCH))[:6]
    destination.parent.mkdir(parents=True, exist_ok=True)
    files = files_in(source)
    with zipfile.ZipFile(destination, "w", zipfile.ZIP_DEFLATED, compresslevel=9) as archive:
        for path in files:
            relative = path.relative_to(source).as_posix()
            entry = zipfile.ZipInfo(f"{top}/{relative}", date_time=stamp)
            entry.compress_type = zipfile.ZIP_DEFLATED
            executable = path.suffix.lower() in {".exe", ".dll", ".pyd", ".so"} or (
                os.access(path, os.X_OK) and not path.suffix
            )
            entry.external_attr = (0o755 if executable else 0o644) << 16
            entry.create_system = 3
            archive.writestr(entry, path.read_bytes())
    return destination


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Build the portable dashboard ZIP.")
    parser.add_argument("--out", type=Path, default=OUT_DIR, help="folder for the ZIP")
    parser.add_argument(
        "--bundle",
        type=Path,
        help="use an existing PyInstaller output folder instead of building",
    )
    options = parser.parse_args(argv)

    version = project_version()
    epoch = source_date_epoch()
    bundle = options.bundle or run_pyinstaller(version)
    if not bundle.is_dir():
        print(f"error: {bundle} is not a folder", file=sys.stderr)
        return 1
    removed = prune_unused_qt(bundle)
    if removed:
        print(f"removed {len(removed)} unused Qt files: " + ", ".join(removed))
    info = build_info(version, epoch)
    add_bundle_files(bundle, info)

    name = f"{APP_NAME}-{version}-{info['platform']}"
    archive = write_zip(bundle, options.out / f"{name}.zip", f"{APP_NAME}-{version}", epoch)
    digest = sha256_of(archive)
    archive.with_suffix(".zip.sha256").write_text(f"{digest}  {archive.name}\n", encoding="ascii")
    size_mb = archive.stat().st_size / 1e6
    print(f"wrote {archive} ({size_mb:.1f} MB, sha256 {digest})")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
