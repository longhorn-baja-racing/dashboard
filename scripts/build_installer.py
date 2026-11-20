"""Build the per-user Windows installer from the portable ZIP (issue #53).

    uv run --locked python scripts/build_portable.py
    uv run --locked python scripts/build_installer.py

The installer wraps the exact files of the smoke-tested portable ZIP: this
script extracts ``dist/portable/LBRDashboard-<version>-windows-x64.zip`` into
``build/installer/``, swaps the portable README for an installed one, and
runs Inno Setup's ``ISCC`` on ``packaging/installer.iss`` with the version,
source folder and output name passed as ``/D`` defines. Nothing in the
``.iss`` file is edited by hand per release.

Output: ``dist/installer/LBRDashboard-<version>-windows-x64-setup.exe`` and
``.sha256``. See ``docs/windows-installer.md``.
"""

from __future__ import annotations

import argparse
import os
import re
import shutil
import subprocess
import sys
import zipfile
from collections.abc import Callable, Mapping, Sequence
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))

import build_portable  # noqa: E402

APP_NAME = build_portable.APP_NAME
SCRIPT = ROOT / "packaging" / "installer.iss"
PORTABLE_DIR = build_portable.OUT_DIR
STAGE_DIR = ROOT / "build" / "installer"
OUT_DIR = ROOT / "dist" / "installer"
#: The only platform the installer targets (the .iss allows x64 Windows only).
PLATFORM = "windows-x64"
ZIP_NAME = re.compile(rf"^{APP_NAME}-(?P<version>[^-]+)-(?P<platform>.+)\.zip$")
#: Where Inno Setup 6 installs ISCC.exe when it is not on PATH.
ISCC_LOCATIONS = (
    ("ProgramFiles(x86)", "Inno Setup 6"),
    ("ProgramFiles", "Inno Setup 6"),
    ("LOCALAPPDATA", "Programs/Inno Setup 6"),
)

README = """\
LBR Dashboard {version}
=======================

Telemetry dashboard for Longhorn Baja Racing, installed by its setup program.
It does not need Python.

Start it from the Start menu (LBR Dashboard). Use File > Open to open a CSV;
example files are in the samples folder next to this file.

Where data is written
---------------------
Nothing is written inside this folder. Logs and preferences go under your
%LOCALAPPDATA% folder (LBR Dashboard\\logs and LonghornBaja\\dashboard).
Installing a newer version keeps them. Uninstalling (Settings > Apps >
Installed apps > LBR Dashboard > Uninstall) removes the program and its
shortcuts and asks before deleting logs and preferences; it never touches your
telemetry files.

Check this copy
---------------
{exe} --self-test --report report.txt writes a short check to report.txt and
exits with code 0 when everything passed.

Licenses
--------
The dashboard is MIT licensed (LICENSE). It bundles Python, Qt for Python and
other libraries under their own licenses; see notices\\THIRD-PARTY-NOTICES.txt.
"""


class BuildError(Exception):
    """The installer cannot be built; the message says why."""


def find_portable_zip(target: Path) -> Path:
    if target.is_file():
        return target
    found = sorted(target.glob(f"{APP_NAME}-*-{PLATFORM}.zip"))
    if len(found) != 1:
        raise BuildError(f"expected one {APP_NAME} {PLATFORM} ZIP in {target}, found {len(found)}")
    return found[0]


def parse_zip_name(zip_path: Path) -> tuple[str, str]:
    match = ZIP_NAME.match(zip_path.name)
    if match is None:
        raise BuildError(f"{zip_path.name} is not named {APP_NAME}-<version>-<platform>.zip")
    return match.group("version"), match.group("platform")


def file_version(version: str) -> str:
    """Numeric Windows version for the setup exe, e.g. ``0.2.0rc1`` -> ``0.2.0.0``."""

    return ".".join(str(part) for part in build_portable.version_tuple(version))


def installer_base_name(version: str, platform_tag: str = PLATFORM) -> str:
    return f"{APP_NAME}-{version}-{platform_tag}-setup"


def find_iscc(
    explicit: str | None = None,
    environ: Mapping[str, str] | None = None,
    which: Callable[[str], str | None] = shutil.which,
) -> Path | None:
    """``--iscc``, then ``ISCC`` in the environment, ``PATH``, and the default folders."""

    env = os.environ if environ is None else environ
    for candidate in (explicit, env.get("ISCC")):
        if candidate:
            return Path(candidate)
    for name in ("iscc", "ISCC.exe"):
        found = which(name)
        if found:
            return Path(found)
    for variable, folder in ISCC_LOCATIONS:
        base = env.get(variable)
        if base and (Path(base) / folder / "ISCC.exe").is_file():
            return Path(base) / folder / "ISCC.exe"
    return None


def installer_defines(
    version: str, source: Path, out_dir: Path, compression: str | None = None
) -> dict[str, str]:
    """The ``/D`` values ``packaging/installer.iss`` requires."""

    defines = {
        "AppVersion": version,
        "FileVersion": file_version(version),
        "SourceDir": str(source),
        "OutputDir": str(out_dir),
        "OutputBaseName": installer_base_name(version),
    }
    if compression:
        defines["Compression"] = compression
    return defines


def iscc_arguments(iscc: Path, defines: Mapping[str, str], script: Path = SCRIPT) -> list[str]:
    for name, value in defines.items():
        if '"' in value or "\n" in value:
            raise BuildError(f"{name} cannot contain quotes or line breaks: {value!r}")
    return [
        str(iscc),
        "/Qp",
        *(f"/D{name}={value}" for name, value in defines.items()),
        str(script),
    ]


def stage(zip_path: Path, version: str, stage_dir: Path) -> Path:
    """Extract the ZIP's single app folder and write the installed README."""

    if stage_dir.exists():
        shutil.rmtree(stage_dir)
    stage_dir.mkdir(parents=True)
    top = f"{APP_NAME}-{version}"
    with zipfile.ZipFile(zip_path) as archive:
        tops = {name.split("/", 1)[0] for name in archive.namelist()}
        if tops != {top}:
            raise BuildError(f"ZIP should hold one {top} folder, has {sorted(tops)}")
        archive.extractall(stage_dir)
    app_dir = stage_dir / top
    if not (app_dir / f"{APP_NAME}.exe").is_file():
        raise BuildError(f"{zip_path.name} has no {APP_NAME}.exe")
    readme = README.format(version=version, exe=f"{APP_NAME}.exe")
    (app_dir / "README.txt").write_bytes(readme.replace("\n", "\r\n").encode("ascii"))
    return app_dir


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Build the Windows installer.")
    parser.add_argument(
        "zip", type=Path, nargs="?", default=PORTABLE_DIR, help="portable ZIP or its folder"
    )
    parser.add_argument("--out", type=Path, default=OUT_DIR, help="folder for the installer")
    parser.add_argument("--stage", type=Path, default=STAGE_DIR, help="scratch folder")
    parser.add_argument("--iscc", help="path to ISCC.exe (default: PATH, then Inno Setup 6)")
    parser.add_argument(
        "--version",
        help="installer version instead of the ZIP's; only for upgrade-test installers",
    )
    parser.add_argument("--compression", help="Inno Setup compression, e.g. none for tests")
    parser.add_argument(
        "--find-iscc", action="store_true", help="print the ISCC path and exit (1 if missing)"
    )
    options = parser.parse_args(argv)

    iscc = find_iscc(options.iscc)
    if options.find_iscc:
        if iscc is None:
            print("ISCC not found; install Inno Setup 6", file=sys.stderr)
            return 1
        print(iscc)
        return 0
    try:
        if iscc is None:
            raise BuildError("ISCC not found; install Inno Setup 6.3 or newer, or pass --iscc")
        zip_path = find_portable_zip(options.zip)
        zip_version, platform_tag = parse_zip_name(zip_path)
        if platform_tag != PLATFORM:
            raise BuildError(f"the installer is {PLATFORM} only, not {platform_tag}")
        version = options.version or zip_version
        source = stage(zip_path, zip_version, options.stage.resolve())
        out_dir = options.out.resolve()
        out_dir.mkdir(parents=True, exist_ok=True)
        defines = installer_defines(version, source, out_dir, options.compression)
        installer = out_dir / f"{defines['OutputBaseName']}.exe"
        checksum = installer.with_name(installer.name + ".sha256")
        # A file left by an earlier build must never pass for this one.
        installer.unlink(missing_ok=True)
        checksum.unlink(missing_ok=True)
        completed = subprocess.run(iscc_arguments(iscc, defines), check=False)
        if completed.returncode != 0:
            raise BuildError(f"ISCC exited with {completed.returncode}")
        if not installer.is_file():
            raise BuildError(f"ISCC did not write {installer}")
    except (BuildError, OSError, zipfile.BadZipFile) as error:
        print(f"error: {error}", file=sys.stderr)
        return 1
    digest = build_portable.sha256_of(installer)
    checksum.write_text(f"{digest}  {installer.name}\n", encoding="ascii")
    size_mb = installer.stat().st_size / 1e6
    print(f"wrote {installer} ({size_mb:.1f} MB, sha256 {digest})")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
