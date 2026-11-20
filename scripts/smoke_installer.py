"""Install, run, upgrade and uninstall the Windows installer silently (issue #53).

    uv run --locked python scripts/smoke_installer.py dist/installer --older build/installer-old

Windows only. It refuses to start if LBR Dashboard is already installed for
this user, then runs one or two scenarios, each ending in a silent uninstall:

* **Upgrade** (with ``--older``): install the older installer per-user into
  the default folder (``%LOCALAPPDATA%\\Programs\\LBR Dashboard``), plant a
  stale library in its ``_internal`` folder, then install the new one without
  ``/DIR`` and check it reused the folder, replaced the version and removed
  the stale file.
* **Fresh install** into a folder whose path has spaces and non-ASCII
  characters (``/DIR``).

Each scenario checks the uninstall entry (name, version, publisher, location),
the Start menu shortcut and the layout, runs ``--self-test`` and a normal
launch without Python on ``PATH``, checks nothing was written in the program
folder, puts a marker file in each per-user data folder, uninstalls with
``/VERYSILENT``, and checks the program folder, shortcut and uninstall entry
are gone while the user data is still there. Exit code 0 means every step
passed.
"""

from __future__ import annotations

import argparse
import os
import re
import shutil
import subprocess
import sys
import tempfile
import time
from collections.abc import Callable, Mapping, Sequence
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))

import smoke_portable  # noqa: E402

APP_NAME = smoke_portable.APP_NAME
PRODUCT_NAME = "LBR Dashboard"
PUBLISHER = "Longhorn Baja Racing"
SCRIPT = ROOT / "packaging" / "installer.iss"
INSTALLER_NAME = re.compile(rf"^{APP_NAME}-(?P<version>[^-]+)-(?P<platform>.+)-setup\.exe$")
INSTALL_FLAGS = ("/VERYSILENT", "/SUPPRESSMSGBOXES", "/NORESTART", "/CURRENTUSER")
UNINSTALL_FLAGS = ("/VERYSILENT", "/SUPPRESSMSGBOXES", "/NORESTART")
STALE_FILE = "_internal/stale-from-older-version.dll"
MARKER = "installer-smoke-keep.txt"
UNINSTALL_SECONDS = 120.0


class SmokeError(Exception):
    """A smoke step failed; the message says which and why."""


def app_id(script: Path = SCRIPT) -> str:
    """The ``AppId`` from the ``.iss`` file, e.g. ``{7CB5...}``."""

    match = re.search(r"^AppId=\{(\{[0-9A-F-]{36}\})\s*$", script.read_text("utf-8"), re.M)
    if match is None:
        raise SmokeError(f"no AppId={{{{GUID}} line in {script.name}")
    return match.group(1)


def uninstall_key(identifier: str) -> str:
    return rf"Software\Microsoft\Windows\CurrentVersion\Uninstall\{identifier}_is1"


def find_installer(target: Path) -> Path:
    if target.is_file():
        return target
    found = sorted(target.glob(f"{APP_NAME}-*-setup.exe"))
    if len(found) != 1:
        raise SmokeError(f"expected one {APP_NAME} installer in {target}, found {len(found)}")
    return found[0]


def parse_installer_name(installer: Path) -> tuple[str, str]:
    match = INSTALLER_NAME.match(installer.name)
    if match is None:
        raise SmokeError(f"{installer.name} is not named {APP_NAME}-<version>-<platform>-setup.exe")
    return match.group("version"), match.group("platform")


def install_arguments(installer: Path, log: Path, target: Path | None = None) -> list[str]:
    arguments = [str(installer), *INSTALL_FLAGS, f"/LOG={log}"]
    if target is not None:
        arguments.append(f"/DIR={target}")
    return arguments


def uninstall_arguments(uninstaller: Path, log: Path) -> list[str]:
    return [str(uninstaller), *UNINSTALL_FLAGS, f"/LOG={log}"]


def default_install_dir(environ: Mapping[str, str]) -> Path:
    """Where a per-user install goes when no folder is chosen (``{userpf}``)."""

    return Path(environ["LOCALAPPDATA"]) / "Programs" / PRODUCT_NAME


def start_menu_shortcut(environ: Mapping[str, str]) -> Path:
    programs = Path(environ["APPDATA"]) / "Microsoft" / "Windows" / "Start Menu" / "Programs"
    return programs / f"{PRODUCT_NAME}.lnk"


def user_data_dirs(environ: Mapping[str, str]) -> list[Path]:
    """Per-user folders the dashboard writes (see diagnostics/logs.py, core/preferences.py)."""

    base = Path(environ["LOCALAPPDATA"])
    return [base / "LBR Dashboard", base / "LonghornBaja" / "dashboard"]


def same_path(first: str | Path, second: str | Path) -> bool:
    def normal(path: str | Path) -> str:
        return os.path.normcase(os.path.realpath(str(path).rstrip("\\/")))

    return normal(first) == normal(second)


def run_command(arguments: Sequence[str], timeout: float = 600.0) -> int:
    return subprocess.run(list(arguments), timeout=timeout, check=False).returncode


def uninstall_entry(identifier: str) -> dict[str, str] | None:
    """The per-user uninstall entry's values, or None when not installed."""

    if sys.platform != "win32":
        raise SmokeError("the installer smoke test needs Windows")
    import winreg

    names = ("DisplayName", "DisplayVersion", "Publisher", "InstallLocation", "UninstallString")
    try:
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, uninstall_key(identifier)) as key:
            entry = {}
            for name in names:
                try:
                    entry[name] = str(winreg.QueryValueEx(key, name)[0])
                except FileNotFoundError:
                    entry[name] = ""
            return entry
    except FileNotFoundError:
        return None


def wait_until(check: Callable[[], bool], seconds: float, interval: float = 1.0) -> bool:
    deadline = time.monotonic() + seconds
    while True:
        if check():
            return True
        if time.monotonic() >= deadline:
            return False
        time.sleep(interval)


def check_entry(
    entry: Mapping[str, str] | None, version: str, location: Path | None = None
) -> list[str]:
    if entry is None:
        return ["no per-user uninstall entry"]
    problems = []
    expected = {"DisplayName": PRODUCT_NAME, "DisplayVersion": version, "Publisher": PUBLISHER}
    for name, value in expected.items():
        if entry.get(name) != value:
            problems.append(f"uninstall entry {name} is {entry.get(name)!r}, not {value!r}")
    if location is not None and not same_path(entry.get("InstallLocation", ""), location):
        problems.append(f"installed to {entry.get('InstallLocation')!r}, not {str(location)!r}")
    return problems


def install(installer: Path, logs: Path, target: Path | None) -> None:
    log = logs / f"install-{installer.stem}.log"
    code = run_command(install_arguments(installer, log, target))
    if code != 0:
        raise SmokeError(f"{installer.name} exited with {code}; see {log}")


def run_installed(app_dir: Path, version: str, platform_tag: str, work: Path) -> list[str]:
    problems = smoke_portable.check_layout(app_dir, version, platform_tag)
    if not (app_dir / "unins000.exe").is_file():
        problems.append("missing unins000.exe")
    if problems:
        raise SmokeError("layout: " + "; ".join(problems))
    env = smoke_portable.clean_environment(os.environ)
    exe = app_dir / smoke_portable.executable_name(platform_tag)
    work.mkdir(parents=True, exist_ok=True)
    before = smoke_portable.snapshot(app_dir)
    lines = ["layout ok", smoke_portable.run_self_test(exe, env, work).rstrip()]
    smoke_portable.run_normally(exe, env, work, smoke_portable.LAUNCH_SECONDS)
    lines.append(f"normal launch still running after {smoke_portable.LAUNCH_SECONDS:g} s")
    written = smoke_portable.changes(before, smoke_portable.snapshot(app_dir))
    if written:
        raise SmokeError("the app wrote inside its program folder: " + "; ".join(written))
    lines.append("nothing written inside the program folder")
    return lines


def plant_markers(folders: Sequence[Path]) -> list[Path]:
    """Write a marker in each user data folder; return the folders this created."""

    created = []
    for folder in folders:
        missing = [parent for parent in (folder, *folder.parents) if not parent.exists()]
        folder.mkdir(parents=True, exist_ok=True)
        created.extend(reversed(missing))
        (folder / MARKER).write_text("user data must survive uninstall\n", encoding="ascii")
    return created


def remove_markers(folders: Sequence[Path], created: Sequence[Path]) -> None:
    for folder in folders:
        (folder / MARKER).unlink(missing_ok=True)
    for folder in reversed(created):
        try:
            folder.rmdir()
        except OSError:
            pass


def uninstall(app_dir: Path, identifier: str, environ: Mapping[str, str], logs: Path) -> list[str]:
    data = user_data_dirs(environ)
    created = plant_markers(data)
    try:
        log = logs / f"uninstall-{len(list(logs.glob('uninstall-*')))}.log"
        code = run_command(uninstall_arguments(app_dir / "unins000.exe", log))
        if code != 0:
            raise SmokeError(f"uninstaller exited with {code}; see {log}")
        # The uninstaller finishes from a copy of itself, so wait for its work.
        gone = wait_until(
            lambda: not app_dir.exists() and uninstall_entry(identifier) is None,
            UNINSTALL_SECONDS,
        )
        problems = []
        if not gone:
            left = sorted(p.relative_to(app_dir).as_posix() for p in app_dir.rglob("*"))
            problems.append(f"program folder or uninstall entry still there: {left[:20]}")
        if start_menu_shortcut(environ).exists():
            problems.append("Start menu shortcut still there")
        if app_dir.parent.is_dir() and any(app_dir.parent.glob(f"{PRODUCT_NAME}*")):
            problems.append(f"stray files next to the program folder in {app_dir.parent}")
        for folder in data:
            if not (folder / MARKER).is_file():
                problems.append(f"uninstall deleted user data in {folder}")
        if problems:
            raise SmokeError("uninstall: " + "; ".join(problems))
    finally:
        remove_markers(data, created)
    return ["uninstalled: program folder, shortcut and entry removed, user data kept"]


def scenario(
    installer: Path,
    version: str,
    platform_tag: str,
    identifier: str,
    scratch: Path,
    target: Path | None = None,
    older: Path | None = None,
) -> list[str]:
    environ = os.environ
    logs = scratch / "logs"
    logs.mkdir(parents=True, exist_ok=True)
    expected_dir = target or default_install_dir(environ)
    lines = []
    if older is not None:
        older_version, _ = parse_installer_name(older)
        install(older, logs, target)
        problems = check_entry(uninstall_entry(identifier), older_version, expected_dir)
        if problems:
            raise SmokeError(f"{older.name}: " + "; ".join(problems))
        stale = expected_dir / STALE_FILE
        stale.parent.mkdir(parents=True, exist_ok=True)
        stale.write_bytes(b"left by an older version")
        lines.append(f"installed {older.name} to {expected_dir}")
    install(installer, logs, target if older is None else None)
    problems = check_entry(uninstall_entry(identifier), version, expected_dir)
    if not start_menu_shortcut(environ).is_file():
        problems.append("no Start menu shortcut")
    if older is not None and (expected_dir / STALE_FILE).exists():
        problems.append("upgrade left a file from the older version in _internal")
    if problems:
        raise SmokeError(f"{installer.name}: " + "; ".join(problems))
    action = "upgraded in place" if older is not None else "installed"
    lines.append(f"{action} {installer.name} to {expected_dir}")
    lines += run_installed(expected_dir, version, platform_tag, scratch / "work")
    lines += uninstall(expected_dir, identifier, environ, logs)
    return lines


def smoke(installer: Path, scratch: Path, older: Path | None = None) -> list[str]:
    """Run every scenario; return progress lines, raise SmokeError on the first failure."""

    version, platform_tag = parse_installer_name(installer)
    identifier = app_id()
    if uninstall_entry(identifier) is not None:
        raise SmokeError(f"{PRODUCT_NAME} is already installed for this user; uninstall it first")
    lines = [f"testing {installer.name}"]
    target = scratch / smoke_portable.AWKWARD_FOLDER / PRODUCT_NAME
    try:
        if older is not None:
            lines += scenario(
                installer, version, platform_tag, identifier, scratch / "u", older=older
            )
        lines += scenario(
            installer, version, platform_tag, identifier, scratch / "f", target=target
        )
    except SmokeError:
        remove_leftover_install(identifier, scratch)
        raise
    if any(target.parent.iterdir()):
        raise SmokeError(f"stray files left in {target.parent}")
    return lines


def remove_leftover_install(identifier: str, scratch: Path) -> None:
    """After a failure, uninstall whatever is still installed so a rerun can start."""

    entry = uninstall_entry(identifier)
    if entry is None or not entry.get("InstallLocation"):
        return
    uninstaller = Path(entry["InstallLocation"]) / "unins000.exe"
    if uninstaller.is_file():
        run_command(uninstall_arguments(uninstaller, scratch / "cleanup-uninstall.log"))


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Smoke-test the Windows installer.")
    parser.add_argument("target", type=Path, help="the installer, or the folder holding it")
    parser.add_argument("--older", type=Path, help="an older installer to upgrade from")
    parser.add_argument("--keep-logs", type=Path, help="copy installer logs to this folder")
    options = parser.parse_args(argv)
    for stream in (sys.stdout, sys.stderr):
        if hasattr(stream, "reconfigure"):
            stream.reconfigure(errors="backslashreplace")  # type: ignore[union-attr]
    code = 0
    with tempfile.TemporaryDirectory(prefix="lbr-installer-") as scratch:
        try:
            installer = find_installer(options.target)
            older = find_installer(options.older) if options.older else None
            for line in smoke(installer, Path(scratch), older):
                print(line)
        except (SmokeError, OSError, subprocess.SubprocessError) as error:
            print(f"error: {error}", file=sys.stderr)
            code = 1
        if options.keep_logs is not None:
            options.keep_logs.mkdir(parents=True, exist_ok=True)
            for log in Path(scratch).rglob("*.log"):
                shutil.copyfile(log, options.keep_logs / f"{log.parent.parent.name}-{log.name}")
    if code == 0:
        print("installer smoke test passed")
    return code


if __name__ == "__main__":
    raise SystemExit(main())
