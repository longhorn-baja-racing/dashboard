"""Portable ZIP build and smoke tooling, without running PyInstaller."""

from __future__ import annotations

import ast
import hashlib
import importlib.util
import json
import os
import subprocess
import sys
import zipfile
from pathlib import Path
from types import ModuleType

import pytest

ROOT = Path(__file__).resolve().parents[1]


def load_script(name: str) -> ModuleType:
    spec = importlib.util.spec_from_file_location(name, ROOT / "scripts" / f"{name}.py")
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


build = load_script("build_portable")
smoke = load_script("smoke_portable")


@pytest.fixture
def no_pyinstaller(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """Quality CI installs only the dev extra, so fake what PyInstaller provides."""

    copying = tmp_path / "pyinstaller-COPYING.txt"
    copying.write_text("GPL with bootloader exception", encoding="ascii")
    notice = build.collect_notices.Component(
        "pyinstaller-bootloader", "6.0", "GPL-2.0-or-later WITH Bootloader-exception", (copying,)
    )
    monkeypatch.setattr(build, "pyinstaller_notice", lambda: notice)
    real_version = build.metadata.version
    monkeypatch.setattr(
        build.metadata,
        "version",
        lambda name: "6.0" if name == "pyinstaller" else real_version(name),
    )


def fake_bundle(folder: Path, exe: str = "LBRDashboard.exe") -> Path:
    files = {
        exe: b"MZ",
        "_internal/PySide6/Qt6Widgets.dll": b"w",
        "_internal/PySide6/Qt6Pdf.dll": b"p",
        "_internal/PySide6/Qt6QuickWidgets.dll": b"q",
        "_internal/PySide6/Qt6VirtualKeyboard.dll": b"v",
        "_internal/PySide6/plugins/platforms/qwindows.dll": b"pw",
        "_internal/PySide6/plugins/imageformats/qjpeg.dll": b"j",
        "_internal/PySide6/plugins/imageformats/qpdf.dll": b"pp",
        "_internal/PySide6/plugins/platforminputcontexts/qtvirtualkeyboardplugin.dll": b"k",
    }
    for relative, data in files.items():
        path = folder / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(data)
    return folder


# Build configuration -----------------------------------------------------------------


def test_version_metadata_comes_from_the_package() -> None:
    from lbr_dashboard.version import __version__

    assert build.project_version() == __version__
    assert build.version_tuple("0.1.0") == (0, 1, 0, 0)
    assert build.version_tuple("1.2") == (1, 2, 0, 0)
    assert build.version_tuple("2.0.1rc3+local.7") == (2, 0, 1, 0)


def test_windows_version_resource_names_the_product() -> None:
    text = build.windows_version_file("0.3.1")
    ast.parse(text)  # PyInstaller evaluates it as a Python expression.
    assert "filevers=(0, 3, 1, 0)" in text
    for value in ("Longhorn Baja Racing", "LBR Dashboard", "LBRDashboard.exe", "MIT License"):
        assert value in text
    assert text.isascii()


def test_platform_tags() -> None:
    assert build.platform_tag("win-amd64") == "windows-x64"
    assert build.platform_tag("win-arm64") == "windows-arm64"
    assert build.platform_tag("linux-x86_64") == "linux-x64"
    assert build.platform_tag("macosx-11.0-arm64") == "macos-arm64"
    assert build.platform_tag("macosx-10.13-x86_64") == "macos-x64"
    assert build.platform_tag("Solaris_2.11 SPARC") == "solaris-2-11-sparc"


def test_pyinstaller_arguments_freeze_one_windowed_folder(tmp_path: Path) -> None:
    arguments = build.pyinstaller_arguments(tmp_path / "version.txt")
    assert arguments[0].endswith("entry.py")
    for flag in ("--onedir", "--windowed", "--clean", "--noconfirm"):
        assert flag in arguments
    assert "--onefile" not in arguments  # Qt libraries must stay separate files.
    excluded = {arguments[i + 1] for i, a in enumerate(arguments) if a == "--exclude-module"}
    assert {"PySide6.QtWebEngineCore", "PySide6.QtQuick", "tkinter"} <= excluded
    assert arguments[arguments.index("--version-file") + 1] == str(tmp_path / "version.txt")
    assert "--version-file" not in build.pyinstaller_arguments(None)


def test_source_date_epoch_is_explicit_or_the_commit_time() -> None:
    assert build.source_date_epoch({"SOURCE_DATE_EPOCH": "1700000000"}) == 1700000000
    assert build.source_date_epoch({"SOURCE_DATE_EPOCH": "5"}) == build.ZIP_EPOCH
    assert build.source_date_epoch({}) >= build.ZIP_EPOCH


# Bundle and ZIP ----------------------------------------------------------------------


def test_unused_qt_files_and_their_plugins_are_pruned(tmp_path: Path) -> None:
    bundle = fake_bundle(tmp_path / "bundle")
    removed = build.prune_unused_qt(bundle)
    assert removed == [
        "_internal/PySide6/Qt6Pdf.dll",
        "_internal/PySide6/Qt6QuickWidgets.dll",
        "_internal/PySide6/Qt6VirtualKeyboard.dll",
        "_internal/PySide6/plugins/imageformats/qpdf.dll",
        "_internal/PySide6/plugins/platforminputcontexts/qtvirtualkeyboardplugin.dll",
    ]
    left = sorted(p.name for p in bundle.rglob("*") if p.is_file())
    assert left == ["LBRDashboard.exe", "Qt6Widgets.dll", "qjpeg.dll", "qwindows.dll"]
    assert build.prune_unused_qt(bundle) == []


def test_bundle_gets_license_notices_samples_and_build_info(
    no_pyinstaller: None, tmp_path: Path
) -> None:
    bundle = fake_bundle(tmp_path / "bundle")
    info = build.build_info("0.1.0", build.ZIP_EPOCH)
    build.add_bundle_files(bundle, info)

    assert (bundle / "LICENSE").read_bytes() == (ROOT / "LICENSE").read_bytes()
    notices = (bundle / "notices" / "THIRD-PARTY-NOTICES.txt").read_text(encoding="utf-8")
    for name in ("lbr-dashboard", "pyside6", "pyqtgraph", "python", "pyinstaller-bootloader"):
        assert f"\n{name} " in notices or notices.startswith(f"{name} ")
    assert (bundle / "notices" / "pyinstaller-bootloader" / "pyinstaller-COPYING.txt").is_file()
    assert (bundle / "samples" / "sample_baja_log.csv").is_file()
    readme = (bundle / "README.txt").read_bytes()
    assert readme.isascii() and b"\r\n" in readme and b"--self-test" in readme
    data = json.loads((bundle / "build-info.json").read_text(encoding="utf-8"))
    assert data["version"] == "0.1.0" and data["pyinstaller"] == "6.0"
    lock = hashlib.sha256((ROOT / "uv.lock").read_bytes()).hexdigest()
    assert data["uv_lock_sha256"] == lock

    # Rebuilding into the same folder replaces the notices instead of failing.
    build.add_bundle_files(bundle, info)


def test_zip_is_sorted_rooted_and_reproducible(tmp_path: Path) -> None:
    source = fake_bundle(tmp_path / "bundle")
    (source / "README.txt").write_text("hi", encoding="ascii")
    first = build.write_zip(source, tmp_path / "a" / "x.zip", "LBRDashboard-0.1.0", 1700000000)
    os.utime(source / "README.txt", (1, 1))  # File times must not leak into the ZIP.
    second = build.write_zip(source, tmp_path / "b" / "x.zip", "LBRDashboard-0.1.0", 1700000000)
    assert first.read_bytes() == second.read_bytes()

    with zipfile.ZipFile(first) as archive:
        names = archive.namelist()
        assert names == sorted(names)
        assert all(name.startswith("LBRDashboard-0.1.0/") for name in names)
        assert {info.date_time for info in archive.infolist()} == {(2023, 11, 14, 22, 13, 20)}
        exe = archive.getinfo("LBRDashboard-0.1.0/LBRDashboard.exe")
        assert exe.external_attr >> 16 == 0o755

    early = build.write_zip(source, tmp_path / "c.zip", "top", 0)
    with zipfile.ZipFile(early) as archive:
        assert archive.infolist()[0].date_time == (1980, 1, 1, 0, 0, 0)


def test_main_packages_an_existing_bundle(no_pyinstaller: None, tmp_path: Path) -> None:
    bundle = fake_bundle(tmp_path / "bundle")
    out = tmp_path / "out"
    assert build.main(["--bundle", str(bundle), "--out", str(out)]) == 0
    archives = list(out.glob("LBRDashboard-*.zip"))
    assert len(archives) == 1
    checksum = archives[0].with_suffix(".zip.sha256").read_text(encoding="ascii")
    assert checksum == f"{build.sha256_of(archives[0])}  {archives[0].name}\n"
    assert not (bundle / "_internal" / "PySide6" / "Qt6Pdf.dll").exists()

    assert build.main(["--bundle", str(tmp_path / "missing")]) == 1


# Smoke test helpers ------------------------------------------------------------------


def test_clean_environment_hides_python(tmp_path: Path) -> None:
    python_dir = tmp_path / "tools"
    python_dir.mkdir()
    (python_dir / "python.exe").write_bytes(b"")
    plain = tmp_path / "plain"
    plain.mkdir()
    environ = {
        "Path": os.pathsep.join([str(python_dir), str(plain), str(tmp_path / "Python313"), ""]),
        "PYTHONPATH": "x",
        "PYTHONHOME": "x",
        "VIRTUAL_ENV": "x",
        "UV_CACHE_DIR": "x",
        "HOME": "h",
    }
    env = smoke.clean_environment(environ)
    assert env == {"Path": str(plain), "HOME": "h", "QT_QPA_PLATFORM": "offscreen"}
    assert "QT_QPA_PLATFORM" not in smoke.clean_environment({}, offscreen=False)


def test_snapshot_changes_report_writes(tmp_path: Path) -> None:
    (tmp_path / "a.txt").write_text("1", encoding="ascii")
    (tmp_path / "b.txt").write_text("1", encoding="ascii")
    before = smoke.snapshot(tmp_path)
    (tmp_path / "a.txt").write_text("22", encoding="ascii")
    (tmp_path / "b.txt").unlink()
    (tmp_path / "settings.ini").write_text("x", encoding="ascii")
    assert smoke.changes(before, smoke.snapshot(tmp_path)) == [
        "added settings.ini",
        "removed b.txt",
        "changed a.txt",
    ]


def test_layout_problems_are_listed(tmp_path: Path) -> None:
    app = tmp_path / "app"
    app.mkdir()
    problems = smoke.check_layout(app, "0.1.0", "windows-x64")
    assert "missing LBRDashboard.exe" in problems
    assert "missing notices/THIRD-PARTY-NOTICES.txt" in problems
    assert "no sample CSV files" in problems

    fake_bundle(app)
    for relative in smoke.REQUIRED:
        (app / relative).parent.mkdir(parents=True, exist_ok=True)
        (app / relative).write_text("x", encoding="ascii")
    (app / "samples").mkdir()
    (app / "samples" / "s.csv").write_text("a\n1\n", encoding="ascii")
    (app / "build-info.json").write_text(
        json.dumps({"version": "0.0.9", "platform": "linux-x64"}), encoding="utf-8"
    )
    assert smoke.check_layout(app, "0.1.0", "windows-x64") == [
        "build-info version 0.0.9 is not 0.1.0",
        "build-info platform linux-x64 is not windows-x64",
        "unused Qt modules bundled: Qt6Pdf.dll, Qt6QuickWidgets.dll",
    ]


def test_find_zip_needs_exactly_one(tmp_path: Path) -> None:
    with pytest.raises(smoke.SmokeError, match="found 0"):
        smoke.find_zip(tmp_path)
    archive = tmp_path / "LBRDashboard-0.1.0-windows-x64.zip"
    archive.write_bytes(b"")
    assert smoke.find_zip(tmp_path) == archive
    assert smoke.find_zip(archive) == archive


def make_portable_zip(tmp_path: Path, version: str = "0.1.0") -> Path:
    bundle = fake_bundle(tmp_path / "bundle", exe="LBRDashboard")
    build.prune_unused_qt(bundle)
    for relative in smoke.REQUIRED:
        (bundle / relative).parent.mkdir(parents=True, exist_ok=True)
        (bundle / relative).write_text("x", encoding="ascii")
    (bundle / "samples").mkdir()
    (bundle / "samples" / "s.csv").write_text("a\n1\n", encoding="ascii")
    info = {"version": version, "platform": "linux-x64"}
    (bundle / "build-info.json").write_text(json.dumps(info), encoding="utf-8")
    out = tmp_path / f"LBRDashboard-{version}-linux-x64.zip"
    return build.write_zip(bundle, out, f"LBRDashboard-{version}", build.ZIP_EPOCH)


def test_smoke_runs_every_step(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    archive = make_portable_zip(tmp_path)
    calls: list[str] = []

    def self_test(exe: Path, env: dict[str, str], workdir: Path) -> str:
        assert smoke.AWKWARD_FOLDER in str(exe) and "PYTHONPATH" not in env
        calls.append("self-test")
        return "self-test passed\n"

    def launch(exe: Path, env: dict[str, str], workdir: Path, seconds: float) -> None:
        calls.append(f"launch {seconds}")

    monkeypatch.setattr(smoke, "run_self_test", self_test)
    monkeypatch.setattr(smoke, "run_normally", launch)
    lines = smoke.smoke(archive, tmp_path / "scratch", launch_seconds=0.5)
    assert calls == ["self-test", "launch 0.5"]
    assert lines[-1] == "nothing written inside the app folder"

    def writes(exe: Path, env: dict[str, str], workdir: Path, seconds: float) -> None:
        (exe.parent / "settings.ini").write_text("x", encoding="ascii")

    monkeypatch.setattr(smoke, "run_normally", writes)
    with pytest.raises(smoke.SmokeError, match="added settings.ini"):
        smoke.smoke(archive, tmp_path / "scratch2")


def test_smoke_rejects_bad_names_and_layouts(tmp_path: Path) -> None:
    wrong = tmp_path / "dashboard.zip"
    wrong.write_bytes(b"")
    with pytest.raises(smoke.SmokeError, match="is not named"):
        smoke.smoke(wrong, tmp_path)

    loose = tmp_path / "LBRDashboard-0.1.0-linux-x64.zip"
    with zipfile.ZipFile(loose, "w") as archive:
        archive.writestr("LBRDashboard", b"")
    with pytest.raises(smoke.SmokeError, match="one LBRDashboard-0.1.0 folder"):
        smoke.smoke(loose, tmp_path / "s1")

    empty = tmp_path / "empty"
    empty.mkdir()
    bare = build.write_zip(
        fake_bundle(empty), tmp_path / "e" / loose.name, "LBRDashboard-0.1.0", build.ZIP_EPOCH
    )
    with pytest.raises(smoke.SmokeError, match="layout: missing LBRDashboard"):
        smoke.smoke(bare, tmp_path / "s2")


def test_processes_that_fail_are_reported(tmp_path: Path) -> None:
    exe = Path(sys.executable)
    env = dict(os.environ)
    with pytest.raises(smoke.SmokeError, match="self-test exited with 2"):
        smoke.run_self_test(exe, env, tmp_path)
    script = tmp_path / "quit.py"
    script.write_text("raise SystemExit(3)\n", encoding="ascii")

    def early(*args: object, **kwargs: object) -> subprocess.Popen[bytes]:
        return real_popen([str(exe), str(script)], **kwargs)  # type: ignore[arg-type]

    real_popen = subprocess.Popen
    smoke.subprocess.Popen = early  # type: ignore[assignment]
    try:
        with pytest.raises(smoke.SmokeError, match="exited early with code 3"):
            smoke.run_normally(exe, env, tmp_path, 3.0)
    finally:
        smoke.subprocess.Popen = real_popen  # type: ignore[assignment]


def test_a_running_process_is_stopped(tmp_path: Path) -> None:
    script = tmp_path / "wait.py"
    script.write_text("import time\ntime.sleep(60)\n", encoding="ascii")
    real_popen = subprocess.Popen

    def waits(*args: object, **kwargs: object) -> subprocess.Popen[bytes]:
        return real_popen([sys.executable, str(script)], **kwargs)  # type: ignore[arg-type]

    smoke.subprocess.Popen = waits  # type: ignore[assignment]
    try:
        smoke.run_normally(Path(sys.executable), dict(os.environ), tmp_path, 0.5)
    finally:
        smoke.subprocess.Popen = real_popen  # type: ignore[assignment]


def test_main_reports_errors_and_success(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    assert smoke.main([str(tmp_path)]) == 1
    assert "found 0" in capsys.readouterr().err

    archive = make_portable_zip(tmp_path)
    monkeypatch.setattr(smoke, "run_self_test", lambda *args: "self-test passed")
    monkeypatch.setattr(smoke, "run_normally", lambda *args: None)
    assert smoke.main([str(archive), "--launch-seconds", "0"]) == 0
    assert capsys.readouterr().out.endswith("portable smoke test passed\n")
