"""Smoke-test a portable ZIP the way a user would run it (issue #52).

    uv run --locked python scripts/smoke_portable.py dist/portable

Extracts the ZIP into a folder whose path has spaces and non-ASCII
characters, removes Python from ``PATH`` and the ``PYTHON*`` variables, then:

1. checks the layout (executable, license, notices, samples, build info that
   matches the ZIP name) and that no unused Qt modules were bundled,
2. runs ``--self-test`` (start, open the sample CSV, plot, notices present),
3. launches the dashboard normally and checks it is still running after a
   few seconds,
4. checks that neither run wrote anything inside the extracted folder.

Exit code 0 means every step passed.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import subprocess
import sys
import tempfile
import time
import zipfile
from collections.abc import Mapping, Sequence
from pathlib import Path

APP_NAME = "LBRDashboard"
ZIP_NAME = re.compile(rf"^{APP_NAME}-(?P<version>[^-]+)-(?P<platform>.+)\.zip$")
#: Folder name with a space, an accented letter and a CJK character, written
#: with escapes so this file stays ASCII.
AWKWARD_FOLDER = "LBR test " + chr(0xE9) + chr(0x6E2C)
UNUSED_QT = re.compile(r"Qt6?(WebEngine|Pdf|Quick|Qml|3D|Multimedia)", re.IGNORECASE)
REQUIRED = ("LICENSE", "README.txt", "build-info.json", "notices/THIRD-PARTY-NOTICES.txt")
LAUNCH_SECONDS = 5.0


class SmokeError(Exception):
    """A smoke step failed; the message says which and why."""


def find_zip(target: Path) -> Path:
    if target.is_file():
        return target
    found = sorted(target.glob(f"{APP_NAME}-*.zip"))
    if len(found) != 1:
        raise SmokeError(f"expected one {APP_NAME} ZIP in {target}, found {len(found)}")
    return found[0]


def executable_name(platform_tag: str) -> str:
    return f"{APP_NAME}.exe" if platform_tag.startswith("windows") else APP_NAME


def clean_environment(environ: Mapping[str, str], offscreen: bool = True) -> dict[str, str]:
    """The user's environment without any Python on ``PATH`` or ``PYTHON*`` settings."""

    env = {
        key: value
        for key, value in environ.items()
        if not key.upper().startswith(("PYTHON", "VIRTUAL_ENV", "UV_", "CONDA"))
    }
    path_key = next((key for key in env if key.upper() == "PATH"), "PATH")
    parts = env.get(path_key, "").split(os.pathsep)
    keep = [part for part in parts if part and not _has_python(Path(part))]
    env[path_key] = os.pathsep.join(keep)
    if offscreen:
        env["QT_QPA_PLATFORM"] = "offscreen"
    return env


def _has_python(folder: Path) -> bool:
    names = ("python.exe", "python3.exe", "python", "python3", "py.exe")
    lowered = str(folder).lower()
    return "python" in lowered or any((folder / name).is_file() for name in names)


def snapshot(folder: Path) -> dict[str, tuple[int, int]]:
    return {
        path.relative_to(folder).as_posix(): (path.stat().st_size, path.stat().st_mtime_ns)
        for path in folder.rglob("*")
        if path.is_file()
    }


def changes(
    before: Mapping[str, tuple[int, int]], after: Mapping[str, tuple[int, int]]
) -> list[str]:
    added = [f"added {name}" for name in sorted(after.keys() - before.keys())]
    removed = [f"removed {name}" for name in sorted(before.keys() - after.keys())]
    changed = [
        f"changed {name}"
        for name in sorted(before.keys() & after.keys())
        if before[name] != after[name]
    ]
    return added + removed + changed


def check_layout(app_dir: Path, version: str, platform_tag: str) -> list[str]:
    problems = []
    exe = app_dir / executable_name(platform_tag)
    if not exe.is_file():
        problems.append(f"missing {exe.name}")
    for relative in REQUIRED:
        if not (app_dir / relative).is_file():
            problems.append(f"missing {relative}")
    if not any((app_dir / "samples").glob("*.csv")):
        problems.append("no sample CSV files")
    info_path = app_dir / "build-info.json"
    if info_path.is_file():
        info = json.loads(info_path.read_text(encoding="utf-8"))
        if info.get("version") != version:
            problems.append(f"build-info version {info.get('version')} is not {version}")
        if info.get("platform") != platform_tag:
            problems.append(f"build-info platform {info.get('platform')} is not {platform_tag}")
    unused = sorted(p.name for p in app_dir.rglob("*") if p.is_file() and UNUSED_QT.search(p.name))
    if unused:
        problems.append("unused Qt modules bundled: " + ", ".join(unused))
    return problems


def run_self_test(exe: Path, env: Mapping[str, str], workdir: Path) -> str:
    report = workdir / "self-test.txt"
    completed = subprocess.run(
        [str(exe), "--self-test", "--report", str(report)],
        cwd=workdir,
        env=dict(env),
        capture_output=True,
        text=True,
        timeout=180,
    )
    text = report.read_text(encoding="utf-8") if report.is_file() else ""
    if completed.returncode != 0 or "self-test passed" not in text:
        raise SmokeError(
            f"self-test exited with {completed.returncode}\n{text}"
            f"{completed.stdout}{completed.stderr}"
        )
    return text


def run_normally(exe: Path, env: Mapping[str, str], workdir: Path, seconds: float) -> None:
    process = subprocess.Popen(
        [str(exe)],
        cwd=workdir,
        env=dict(env),
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
    )
    try:
        time.sleep(seconds)
        code = process.poll()
        if code is not None:
            output = process.stdout.read().decode(errors="replace") if process.stdout else ""
            raise SmokeError(f"dashboard exited early with code {code}\n{output}")
    finally:
        if process.poll() is None:
            process.kill()
        process.communicate(timeout=30)


def smoke(zip_path: Path, scratch: Path, launch_seconds: float = LAUNCH_SECONDS) -> list[str]:
    """Run every step; return progress lines, raise SmokeError on the first failure."""

    match = ZIP_NAME.match(zip_path.name)
    if match is None:
        raise SmokeError(f"{zip_path.name} is not named {APP_NAME}-<version>-<platform>.zip")
    version, platform_tag = match.group("version"), match.group("platform")
    lines = [f"testing {zip_path.name}"]

    target = scratch / AWKWARD_FOLDER
    with zipfile.ZipFile(zip_path) as archive:
        tops = {name.split("/", 1)[0] for name in archive.namelist()}
        if tops != {f"{APP_NAME}-{version}"}:
            raise SmokeError(f"ZIP should hold one {APP_NAME}-{version} folder, has {sorted(tops)}")
        archive.extractall(target)
    app_dir = target / f"{APP_NAME}-{version}"
    exe = app_dir / executable_name(platform_tag)
    if exe.is_file() and not platform_tag.startswith("windows"):
        exe.chmod(0o755)
    lines.append(f"extracted to {app_dir}")

    problems = check_layout(app_dir, version, platform_tag)
    if problems:
        raise SmokeError("layout: " + "; ".join(problems))
    lines.append("layout ok")

    env = clean_environment(os.environ)
    workdir = scratch / "work"
    workdir.mkdir(exist_ok=True)
    before = snapshot(app_dir)
    lines.append(run_self_test(exe, env, workdir).rstrip())
    run_normally(exe, env, workdir, launch_seconds)
    lines.append(f"normal launch still running after {launch_seconds:g} s")
    written = changes(before, snapshot(app_dir))
    if written:
        raise SmokeError("the app wrote inside its folder: " + "; ".join(written))
    lines.append("nothing written inside the app folder")
    return lines


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Smoke-test a portable dashboard ZIP.")
    parser.add_argument("target", type=Path, help="the ZIP, or the folder holding it")
    parser.add_argument("--launch-seconds", type=float, default=LAUNCH_SECONDS)
    options = parser.parse_args(argv)
    # The extract path is non-ASCII; never fail just because a console cannot show it.
    for stream in (sys.stdout, sys.stderr):
        if hasattr(stream, "reconfigure"):
            stream.reconfigure(errors="backslashreplace")  # type: ignore[union-attr]
    try:
        zip_path = find_zip(options.target)
        with tempfile.TemporaryDirectory(prefix="lbr-portable-") as scratch:
            for line in smoke(zip_path, Path(scratch), options.launch_seconds):
                print(line)
    except (SmokeError, OSError, subprocess.SubprocessError, zipfile.BadZipFile) as error:
        print(f"error: {error}", file=sys.stderr)
        return 1
    print("portable smoke test passed")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
