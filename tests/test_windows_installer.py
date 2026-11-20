"""Windows installer script, build and smoke tooling, without Inno Setup or Windows."""

from __future__ import annotations

import importlib.util
import json
import os
import re
import shutil
import subprocess
import sys
import zipfile
from collections.abc import Sequence
from pathlib import Path
from types import ModuleType

import pytest

ROOT = Path(__file__).resolve().parents[1]
ISS = ROOT / "packaging" / "installer.iss"


def load_script(name: str) -> ModuleType:
    spec = importlib.util.spec_from_file_location(name, ROOT / "scripts" / f"{name}.py")
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


build = load_script("build_installer")
smoke = load_script("smoke_installer")


def iss_text() -> str:
    return ISS.read_text(encoding="utf-8")


def iss_sections() -> dict[str, list[str]]:
    """Each ``[Section]`` of the script with its non-comment lines."""

    sections: dict[str, list[str]] = {}
    current = ""
    for raw in iss_text().splitlines():
        line = raw.strip()
        if not line or line.startswith(";"):
            continue
        match = re.fullmatch(r"\[(\w+)\]", line)
        if match:
            current = match.group(1)
            sections[current] = []
        elif current:
            sections[current].append(line)
    return sections


def iss_define(name: str) -> str:
    match = re.search(rf'^#define {name} "([^"]*)"$', iss_text(), re.MULTILINE)
    assert match is not None, name
    return match.group(1)


def setup_values() -> dict[str, str]:
    return dict(line.split("=", 1) for line in iss_sections()["Setup"])


# Inno Setup script -------------------------------------------------------------------


def test_script_is_ascii_and_has_every_section() -> None:
    assert ISS.read_bytes().isascii()
    sections = iss_sections()
    for name in ("Setup", "Files", "Icons", "Run", "InstallDelete", "UninstallDelete", "Code"):
        assert name in sections, name


def test_installs_per_user_without_admin_by_default() -> None:
    setup = setup_values()
    assert setup["PrivilegesRequired"] == "lowest"
    # The user may still choose all users (elevated), in the dialog or with /ALLUSERS.
    assert set(setup["PrivilegesRequiredOverridesAllowed"].split()) == {"dialog", "commandline"}
    assert setup["DefaultDirName"] == "{autopf}\\{#AppName}"
    assert setup["ArchitecturesInstallIn64BitMode"] == "x64compatible"
    assert re.fullmatch(r"\{\{[0-9A-F]{8}(-[0-9A-F]{4}){3}-[0-9A-F]{12}\}", setup["AppId"])


def test_version_and_paths_come_only_from_the_build_script() -> None:
    setup = setup_values()
    assert setup["AppVersion"] == "{#AppVersion}"
    assert setup["VersionInfoVersion"] == "{#FileVersion}"
    assert setup["OutputBaseFilename"] == "{#OutputBaseName}"
    text = iss_text()
    assert not re.search(r"\d+\.\d+\.\d+", text.replace("EncodeVer(6, 3, 0)", ""))
    defines = build.installer_defines("0.1.0", Path("src"), Path("out"))
    for name in defines:
        assert f"#ifndef {name}\n  #error" in text, name
    assert "#ifndef Compression\n  #define" in text  # Optional, with a default.


def test_shortcut_uninstall_and_upgrade_rules() -> None:
    sections = iss_sections()
    assert any(line.startswith('Name: "{autoprograms}\\{#AppName}"') for line in sections["Icons"])
    desktop = [line for line in sections["Icons"] if "{autodesktop}" in line]
    assert desktop and all("Tasks: desktopicon" in line for line in desktop)
    assert "Flags: unchecked" in " ".join(sections["Tasks"])
    # Upgrades and downgrades replace the whole library folder.
    assert 'Type: filesandordirs; Name: "{app}\\_internal"' in sections["InstallDelete"]
    # Uninstall only ever deletes inside the program folder.
    for line in sections["UninstallDelete"] + sections["InstallDelete"]:
        assert 'Name: "{app}' in line, line
    assert 'Type: dirifempty; Name: "{app}"' in sections["UninstallDelete"]
    assert not any("postinstall" in line and "skipifsilent" not in line for line in sections["Run"])


def test_user_data_is_removed_only_when_the_user_says_yes() -> None:
    code = "\n".join(iss_sections()["Code"])
    assert "usPostUninstall" in code
    assert "UninstallSilent" in code
    # Default button is No, and /SUPPRESSMSGBOXES answers No.
    assert "MB_YESNO or MB_DEFBUTTON2, IDNO) = IDYES" in code
    assert code.count("DelTree(") == 2


def test_user_data_folders_match_the_application(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    from lbr_dashboard.core import preferences
    from lbr_dashboard.diagnostics import logs

    monkeypatch.setenv("LOCALAPPDATA", str(tmp_path))
    monkeypatch.setattr(sys, "platform", "win32")
    log_dir = logs.default_log_directory({"LOCALAPPDATA": str(tmp_path)})
    prefs_dir = preferences.default_preferences_path().parent
    monkeypatch.undo()

    assert log_dir.parent.relative_to(tmp_path).as_posix() == iss_define("LogsFolder")
    prefs = prefs_dir.relative_to(tmp_path).as_posix()
    assert prefs == iss_define("PrefsFolder").replace("\\", "/")
    assert prefs.split("/")[0] == iss_define("PrefsParent")
    expected = [tmp_path / "LBR Dashboard", tmp_path / "LonghornBaja" / "dashboard"]
    assert smoke.user_data_dirs({"LOCALAPPDATA": str(tmp_path)}) == expected
    assert expected == [log_dir.parent, prefs_dir]


def test_metadata_matches_the_portable_build() -> None:
    portable = build.build_portable
    assert iss_define("AppName") == portable.PRODUCT_NAME == smoke.PRODUCT_NAME
    assert iss_define("AppPublisher") == portable.COMPANY == smoke.PUBLISHER
    assert iss_define("AppExe") == f"{portable.APP_NAME}.exe"


# Build script ------------------------------------------------------------------------


def test_defines_and_names_follow_the_package_version() -> None:
    from lbr_dashboard.version import __version__

    assert build.file_version("0.2.0rc1") == "0.2.0.0"
    assert build.file_version("1.4") == "1.4.0.0"
    defines = build.installer_defines("0.2.0rc1", Path("stage"), Path("out"), "none")
    assert defines == {
        "AppVersion": "0.2.0rc1",
        "FileVersion": "0.2.0.0",
        "SourceDir": "stage",
        "OutputDir": "out",
        "OutputBaseName": "LBRDashboard-0.2.0rc1-windows-x64-setup",
        "Compression": "none",
    }
    assert "Compression" not in build.installer_defines(__version__, Path("s"), Path("o"))
    assert build.installer_base_name(__version__) == f"LBRDashboard-{__version__}-windows-x64-setup"


def test_iscc_arguments(tmp_path: Path) -> None:
    folder = tmp_path / "with space"
    arguments = build.iscc_arguments(
        Path("ISCC.exe"), {"AppVersion": "0.1.0", "SourceDir": str(folder)}
    )
    assert arguments == [
        "ISCC.exe",
        "/Qp",
        "/DAppVersion=0.1.0",
        f"/DSourceDir={folder}",
        str(ISS),
    ]
    with pytest.raises(build.BuildError, match="quotes"):
        build.iscc_arguments(Path("ISCC.exe"), {"SourceDir": 'a"b'})


def test_find_iscc_order(tmp_path: Path) -> None:
    def nowhere(name: str) -> str | None:
        return None

    assert build.find_iscc("C:/x/ISCC.exe", {"ISCC": "y"}, nowhere) == Path("C:/x/ISCC.exe")
    assert build.find_iscc(None, {"ISCC": "y"}, nowhere) == Path("y")
    on_path = build.find_iscc(None, {}, lambda name: f"/bin/{name}" if name == "iscc" else None)
    assert on_path == Path("/bin/iscc")
    assert build.find_iscc(None, {"ProgramFiles(x86)": str(tmp_path)}, nowhere) is None
    installed = tmp_path / "Inno Setup 6" / "ISCC.exe"
    installed.parent.mkdir()
    installed.write_bytes(b"MZ")
    assert build.find_iscc(None, {"ProgramFiles(x86)": str(tmp_path)}, nowhere) == installed


def portable_zip(folder: Path, version: str = "0.1.0", platform_tag: str = "windows-x64") -> Path:
    bundle = folder / "bundle"
    for relative in (
        "LBRDashboard.exe",
        "_internal/python313.dll",
        "LICENSE",
        "README.txt",
        "notices/THIRD-PARTY-NOTICES.txt",
        "samples/sample_baja_log.csv",
    ):
        (bundle / relative).parent.mkdir(parents=True, exist_ok=True)
        (bundle / relative).write_bytes(b"x")
    info = {"version": version, "platform": platform_tag, "commit": "abc", "python": "3.13"}
    (bundle / "build-info.json").write_text(json.dumps(info), encoding="utf-8")
    out = folder / f"LBRDashboard-{version}-{platform_tag}.zip"
    return build.build_portable.write_zip(
        bundle, out, f"LBRDashboard-{version}", build.build_portable.ZIP_EPOCH
    )


def test_stage_extracts_the_zip_and_writes_an_installed_readme(tmp_path: Path) -> None:
    archive = portable_zip(tmp_path)
    stage = tmp_path / "stage"
    (stage / "old").mkdir(parents=True)
    app = build.stage(archive, "0.1.0", stage)
    assert app == stage / "LBRDashboard-0.1.0"
    assert not (stage / "old").exists()
    assert (app / "_internal" / "python313.dll").is_file()
    readme = (app / "README.txt").read_bytes()
    assert readme.isascii() and b"\r\n" in readme
    assert b"Start menu" in readme and b"Uninstall" in readme and b"--self-test" in readme

    with pytest.raises(build.BuildError, match="one LBRDashboard-0.2.0 folder"):
        build.stage(archive, "0.2.0", tmp_path / "s2")
    bare = tmp_path / "bare" / "LBRDashboard-0.1.0-windows-x64.zip"
    bare.parent.mkdir()
    with zipfile.ZipFile(bare, "w") as handle:
        handle.writestr("LBRDashboard-0.1.0/LICENSE", b"x")
    with pytest.raises(build.BuildError, match="no LBRDashboard.exe"):
        build.stage(bare, "0.1.0", tmp_path / "s3")


def test_find_and_name_the_portable_zip(tmp_path: Path) -> None:
    with pytest.raises(build.BuildError, match="found 0"):
        build.find_portable_zip(tmp_path)
    archive = portable_zip(tmp_path)
    assert build.find_portable_zip(tmp_path) == archive
    assert build.find_portable_zip(archive) == archive
    assert build.parse_zip_name(archive) == ("0.1.0", "windows-x64")
    with pytest.raises(build.BuildError, match="is not named"):
        build.parse_zip_name(tmp_path / "dashboard.zip")


class FakeIscc:
    """Stands in for ISCC.exe: records the call and writes the setup exe."""

    def __init__(self, code: int = 0, writes: bool = True) -> None:
        self.code, self.writes = code, writes
        self.calls: list[list[str]] = []

    def __call__(self, arguments: list[str], check: bool = False) -> subprocess.CompletedProcess:
        self.calls.append(arguments)
        defines = dict(a[2:].split("=", 1) for a in arguments if a.startswith("/D"))
        assert (Path(defines["SourceDir"]) / "LBRDashboard.exe").is_file()
        if self.writes:
            out = Path(defines["OutputDir"]) / f"{defines['OutputBaseName']}.exe"
            out.write_bytes(b"MZ setup " + defines["AppVersion"].encode())
        return subprocess.CompletedProcess(arguments, self.code)


def test_main_builds_and_checksums_the_installer(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    portable_zip(tmp_path / "portable")
    fake = FakeIscc()
    monkeypatch.setattr(build.subprocess, "run", fake)
    out = tmp_path / "out"
    common = ["--iscc", "ISCC.exe", "--stage", str(tmp_path / "stage"), "--out", str(out)]
    assert build.main([str(tmp_path / "portable"), *common]) == 0
    installer = out / "LBRDashboard-0.1.0-windows-x64-setup.exe"
    checksum = installer.with_name(installer.name + ".sha256").read_text(encoding="ascii")
    assert checksum == f"{build.build_portable.sha256_of(installer)}  {installer.name}\n"
    assert fake.calls[0][0] == "ISCC.exe" and fake.calls[0][-1] == str(ISS)

    # An upgrade-test installer can carry an older version without rebuilding the ZIP.
    older = tmp_path / "older"
    args = ["--version", "0.0.0", "--compression", "none", "--iscc", "ISCC.exe"]
    args += ["--stage", str(tmp_path / "stage2"), "--out", str(older)]
    assert build.main([str(tmp_path / "portable"), *args]) == 0
    assert (older / "LBRDashboard-0.0.0-windows-x64-setup.exe").is_file()
    assert "/DCompression=none" in fake.calls[1]


def test_main_reports_build_failures(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    portable_zip(tmp_path / "portable")
    stage = ["--stage", str(tmp_path / "stage"), "--out", str(tmp_path / "out")]
    monkeypatch.setattr(build, "find_iscc", lambda explicit=None: None)
    assert build.main([str(tmp_path / "portable"), *stage]) == 1
    assert "ISCC not found" in capsys.readouterr().err
    assert build.main(["--find-iscc"]) == 1

    monkeypatch.setattr(build, "find_iscc", lambda explicit=None: Path("ISCC.exe"))
    assert build.main(["--find-iscc"]) == 0
    assert capsys.readouterr().out.strip() == "ISCC.exe"
    monkeypatch.setattr(build.subprocess, "run", FakeIscc(code=2))
    assert build.main([str(tmp_path / "portable"), *stage]) == 1
    assert "ISCC exited with 2" in capsys.readouterr().err
    monkeypatch.setattr(build.subprocess, "run", FakeIscc(writes=False))
    assert build.main([str(tmp_path / "portable"), *stage]) == 1
    assert "did not write" in capsys.readouterr().err

    linux = portable_zip(tmp_path / "linux", platform_tag="linux-x64")
    assert build.main([str(linux), *stage]) == 1
    assert "windows-x64 only" in capsys.readouterr().err


# Smoke script helpers ----------------------------------------------------------------


def test_app_id_and_uninstall_key_come_from_the_script(tmp_path: Path) -> None:
    identifier = smoke.app_id()
    assert setup_values()["AppId"] == "{" + identifier
    assert smoke.uninstall_key(identifier).endswith(f"\\Uninstall\\{identifier}_is1")
    broken = tmp_path / "x.iss"
    broken.write_text("[Setup]\nAppName=x\n", encoding="ascii")
    with pytest.raises(smoke.SmokeError, match="no AppId"):
        smoke.app_id(broken)


def test_silent_install_and_uninstall_arguments(tmp_path: Path) -> None:
    exe = tmp_path / "LBRDashboard-0.1.0-windows-x64-setup.exe"
    log = tmp_path / "i.log"
    assert smoke.install_arguments(exe, log) == [
        str(exe),
        "/VERYSILENT",
        "/SUPPRESSMSGBOXES",
        "/NORESTART",
        "/CURRENTUSER",
        f"/LOG={log}",
    ]
    assert smoke.install_arguments(exe, log, tmp_path / "a b")[-1] == f"/DIR={tmp_path / 'a b'}"
    uninstaller = tmp_path / "unins000.exe"
    assert smoke.uninstall_arguments(uninstaller, log) == [
        str(uninstaller),
        "/VERYSILENT",
        "/SUPPRESSMSGBOXES",
        "/NORESTART",
        f"/LOG={log}",
    ]


def test_installer_names_and_windows_folders(tmp_path: Path) -> None:
    with pytest.raises(smoke.SmokeError, match="found 0"):
        smoke.find_installer(tmp_path)
    exe = tmp_path / "LBRDashboard-0.1.0-windows-x64-setup.exe"
    exe.write_bytes(b"MZ")
    assert smoke.find_installer(tmp_path) == exe
    assert smoke.parse_installer_name(exe) == ("0.1.0", "windows-x64")
    with pytest.raises(smoke.SmokeError, match="is not named"):
        smoke.parse_installer_name(tmp_path / "setup.exe")
    env = {"LOCALAPPDATA": str(tmp_path / "local"), "APPDATA": str(tmp_path / "roaming")}
    assert smoke.default_install_dir(env) == tmp_path / "local" / "Programs" / "LBR Dashboard"
    shortcut = smoke.start_menu_shortcut(env)
    assert shortcut.parent == tmp_path / "roaming/Microsoft/Windows/Start Menu/Programs"
    assert shortcut.name == "LBR Dashboard.lnk"


def test_uninstall_entry_checks() -> None:
    entry = {
        "DisplayName": "LBR Dashboard",
        "DisplayVersion": "0.1.0",
        "Publisher": "Longhorn Baja Racing",
        "InstallLocation": "C:/Apps/LBR Dashboard/",
    }
    assert smoke.check_entry(entry, "0.1.0") == []
    assert smoke.check_entry(None, "0.1.0") == ["no per-user uninstall entry"]
    assert smoke.check_entry(entry, "0.2.0") == [
        "uninstall entry DisplayVersion is '0.1.0', not '0.2.0'"
    ]
    assert smoke.same_path("C:/Apps/LBR Dashboard/", "C:/Apps/LBR Dashboard")
    assert len(smoke.check_entry(entry, "0.1.0", Path("C:/Elsewhere"))) == 1


def test_markers_are_planted_and_cleaned_up(tmp_path: Path) -> None:
    existing = tmp_path / "LBR Dashboard"
    existing.mkdir()
    (existing / "logs.txt").write_text("keep", encoding="ascii")
    new = tmp_path / "LonghornBaja" / "dashboard"
    created = smoke.plant_markers([existing, new])
    assert created == [tmp_path / "LonghornBaja", new]
    assert (existing / smoke.MARKER).is_file() and (new / smoke.MARKER).is_file()
    smoke.remove_markers([existing, new], created)
    assert sorted(p.name for p in tmp_path.iterdir()) == ["LBR Dashboard"]
    assert [p.name for p in existing.iterdir()] == ["logs.txt"]


def test_wait_until_polls_then_gives_up() -> None:
    answers = iter([False, True])
    assert smoke.wait_until(lambda: next(answers), 5.0, interval=0.0)
    assert not smoke.wait_until(lambda: False, 0.0, interval=0.0)


class FakeWindows:
    """Simulates Inno Setup installs and uninstalls on a fake per-user registry."""

    def __init__(self, env: dict[str, str]) -> None:
        self.env = env
        self.entry: dict[str, str] | None = None
        self.calls: list[str] = []
        self.keep_stale = False
        self.delete_user_data = False

    def run(self, arguments: Sequence[str], timeout: float = 600.0) -> int:
        program = Path(arguments[0])
        flags = dict(a[1:].split("=", 1) for a in arguments[1:] if "=" in a)
        if program.name == "unins000.exe":
            self.calls.append("uninstall")
            shutil.rmtree(program.parent)
            smoke.start_menu_shortcut(self.env).unlink()
            if self.delete_user_data:
                shutil.rmtree(Path(self.env["LOCALAPPDATA"]) / "LBR Dashboard")
            self.entry = None
            return 0
        version, platform_tag = smoke.parse_installer_name(program)
        if "DIR" in flags:
            target = Path(flags["DIR"])
        elif self.entry is not None:
            target = Path(self.entry["InstallLocation"])
        else:
            target = smoke.default_install_dir(self.env)
        self.calls.append(f"install {version} to {target.name}")
        if (target / "_internal").exists() and not self.keep_stale:
            shutil.rmtree(target / "_internal")
        files = ["LBRDashboard.exe", "unins000.exe", "_internal/python313.dll", "samples/a.csv"]
        files += ["LICENSE", "README.txt", "notices/THIRD-PARTY-NOTICES.txt"]
        for relative in files:
            (target / relative).parent.mkdir(parents=True, exist_ok=True)
            (target / relative).write_bytes(b"x")
        info = {"version": "0.1.0", "platform": platform_tag}
        (target / "build-info.json").write_text(json.dumps(info), encoding="utf-8")
        shortcut = smoke.start_menu_shortcut(self.env)
        shortcut.parent.mkdir(parents=True, exist_ok=True)
        shortcut.write_bytes(b"lnk")
        self.entry = {
            "DisplayName": "LBR Dashboard",
            "DisplayVersion": version,
            "Publisher": "Longhorn Baja Racing",
            "InstallLocation": str(target) + os.sep,  # Inno Setup ends it with a separator.
        }
        return 0


@pytest.fixture
def windows(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> FakeWindows:
    env = {"LOCALAPPDATA": str(tmp_path / "local"), "APPDATA": str(tmp_path / "roaming")}
    for key, value in env.items():
        monkeypatch.setenv(key, value)
    fake = FakeWindows(env)
    monkeypatch.setattr(smoke, "run_command", fake.run)
    monkeypatch.setattr(smoke, "uninstall_entry", lambda identifier: fake.entry)
    monkeypatch.setattr(smoke, "UNINSTALL_SECONDS", 0.0)
    portable = smoke.smoke_portable

    def self_test(exe: Path, env: dict[str, str], workdir: Path) -> str:
        assert "PYTHONPATH" not in env and exe.name == "LBRDashboard.exe"
        fake.calls.append("self-test")
        return "self-test passed\n"

    def launch(exe: Path, env: dict[str, str], workdir: Path, seconds: float) -> None:
        log = Path(fake.env["LOCALAPPDATA"]) / "LBR Dashboard" / "logs" / "dashboard.log"
        log.parent.mkdir(parents=True, exist_ok=True)
        log.write_text("started\n", encoding="ascii")
        fake.calls.append("launch")

    monkeypatch.setattr(portable, "run_self_test", self_test)
    monkeypatch.setattr(portable, "run_normally", launch)
    return fake


def installers(tmp_path: Path) -> tuple[Path, Path]:
    new = tmp_path / "dist" / "LBRDashboard-0.1.0-windows-x64-setup.exe"
    old = tmp_path / "old" / "LBRDashboard-0.0.0-windows-x64-setup.exe"
    for path in (new, old):
        path.parent.mkdir(parents=True)
        path.write_bytes(b"MZ")
    return new, old


def test_smoke_upgrades_runs_and_uninstalls(windows: FakeWindows, tmp_path: Path) -> None:
    new, old = installers(tmp_path)
    lines = smoke.smoke(new, tmp_path / "scratch", older=old)
    assert windows.calls == [
        "install 0.0.0 to LBR Dashboard",
        "install 0.1.0 to LBR Dashboard",
        "self-test",
        "launch",
        "uninstall",
        "install 0.1.0 to LBR Dashboard",
        "self-test",
        "launch",
        "uninstall",
    ]
    assert any(line.startswith("upgraded in place") for line in lines)
    assert sum("user data kept" in line for line in lines) == 2
    local = tmp_path / "local"
    assert not (local / "Programs" / "LBR Dashboard").exists()
    # The app's own log survives, and the smoke test's markers are cleaned up.
    assert (local / "LBR Dashboard" / "logs" / "dashboard.log").is_file()
    assert not list(local.rglob(smoke.MARKER))
    assert not (local / "LonghornBaja").exists()


def test_smoke_refuses_an_existing_install(windows: FakeWindows, tmp_path: Path) -> None:
    new, _ = installers(tmp_path)
    windows.entry = {"InstallLocation": str(tmp_path / "nowhere")}
    with pytest.raises(smoke.SmokeError, match="already installed"):
        smoke.smoke(new, tmp_path / "scratch")
    assert windows.calls == []


def test_smoke_catches_a_stale_upgrade(windows: FakeWindows, tmp_path: Path) -> None:
    new, old = installers(tmp_path)
    windows.keep_stale = True
    with pytest.raises(smoke.SmokeError, match="left a file from the older version"):
        smoke.smoke(new, tmp_path / "scratch", older=old)
    # The failed run uninstalls what it installed, so the next run can start.
    assert windows.calls[-1] == "uninstall" and windows.entry is None


def test_smoke_catches_deleted_user_data(windows: FakeWindows, tmp_path: Path) -> None:
    new, _ = installers(tmp_path)
    windows.delete_user_data = True
    with pytest.raises(smoke.SmokeError, match="deleted user data"):
        smoke.smoke(new, tmp_path / "scratch")


def test_smoke_catches_a_failed_install_and_writes(
    windows: FakeWindows, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    new, _ = installers(tmp_path)
    monkeypatch.setattr(smoke, "run_command", lambda arguments, timeout=600.0: 5)
    with pytest.raises(smoke.SmokeError, match="exited with 5"):
        smoke.smoke(new, tmp_path / "s1")

    monkeypatch.setattr(smoke, "run_command", windows.run)

    def writes(exe: Path, env: dict[str, str], workdir: Path, seconds: float) -> None:
        (exe.parent / "settings.ini").write_text("x", encoding="ascii")

    monkeypatch.setattr(smoke.smoke_portable, "run_normally", writes)
    with pytest.raises(smoke.SmokeError, match="added settings.ini"):
        smoke.smoke(new, tmp_path / "s2")


def test_main_reports_and_keeps_logs(
    windows: FakeWindows, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    assert smoke.main([str(tmp_path / "missing")]) == 1
    assert "found 0" in capsys.readouterr().err
    new, old = installers(tmp_path)
    logs = tmp_path / "kept"
    assert smoke.main([str(new.parent), "--older", str(old), "--keep-logs", str(logs)]) == 0
    assert capsys.readouterr().out.endswith("installer smoke test passed\n")
    assert logs.is_dir()


@pytest.mark.skipif(sys.platform == "win32", reason="checks the non-Windows guard")
def test_registry_lookup_needs_windows() -> None:
    with pytest.raises(smoke.SmokeError, match="needs Windows"):
        smoke.uninstall_entry(smoke.app_id())


def test_a_folder_the_uninstaller_removed_counts_as_clean(tmp_path):
    gone = tmp_path / "created by the installer"
    assert not smoke.has_stray_files(gone)
    gone.mkdir()
    assert not smoke.has_stray_files(gone)
    (gone / "left.txt").write_text("x", encoding="utf-8")
    assert smoke.has_stray_files(gone)
