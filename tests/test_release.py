"""Release tag, notes and artifact checks, and the shape of the Release workflow."""

from __future__ import annotations

import importlib.util
import json
import re
import sys
import zipfile
from pathlib import Path
from types import ModuleType

import pytest

ROOT = Path(__file__).resolve().parents[1]
WORKFLOW = ROOT / ".github" / "workflows" / "release.yml"


def load_script(name: str) -> ModuleType:
    spec = importlib.util.spec_from_file_location(name, ROOT / "scripts" / f"{name}.py")
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


release = load_script("prepare_release")
sha256_of = release.build_portable.sha256_of

CHANGELOG = """\
# Changelog

## [0.2.0] - Unreleased

- Not tagged yet.

## [0.1.0] - 2026-11-20

### Added

- First preview.

## [0.0.9] - 2026-01-01

- Older.
"""


# Tag and notes -----------------------------------------------------------------------


def test_tag_must_match_the_package_version() -> None:
    assert release.check_tag("v0.1.0", "0.1.0") == "v0.1.0"
    assert release.check_tag(None, "0.1.0") == "v0.1.0"  # Dry run.
    for wrong in ("v0.1.1", "0.1.0", "v0.1.0-rc1", "release-0.1.0"):
        with pytest.raises(release.ReleaseError, match="does not match the package version"):
            release.check_tag(wrong, "0.1.0")
    assert release.is_prerelease("0.2.0rc1") and release.is_prerelease("1.0.0b2")
    assert not release.is_prerelease("0.1.0")


def test_notes_come_from_the_dated_changelog_section() -> None:
    assert release.changelog_section(CHANGELOG, "0.1.0") == "### Added\n\n- First preview."
    assert release.changelog_section(CHANGELOG, "0.0.9") == "- Older."
    with pytest.raises(release.ReleaseError, match="not dated"):
        release.changelog_section(CHANGELOG, "0.2.0")
    assert release.changelog_section(CHANGELOG, "0.2.0", require_date=False) == "- Not tagged yet."
    with pytest.raises(release.ReleaseError, match=r"no '## \[0.3.0\]' section"):
        release.changelog_section(CHANGELOG, "0.3.0")
    with pytest.raises(release.ReleaseError, match="is empty"):
        release.changelog_section("## [0.1.0] - 2026-11-20\n\n", "0.1.0")


def test_the_current_version_has_release_notes() -> None:
    from lbr_dashboard.version import __version__

    text = (ROOT / "CHANGELOG.md").read_text(encoding="utf-8")
    assert (ROOT / "CHANGELOG.md").read_bytes().isascii()
    assert release.changelog_section(text, __version__, require_date=False)
    assert release.build_portable.project_version() == __version__


# Artifacts ---------------------------------------------------------------------------


def write_with_checksum(path: Path, data: bytes) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(data)
    path.with_name(path.name + ".sha256").write_text(
        f"{sha256_of(path)}  {path.name}\n", encoding="ascii"
    )
    return path


def build_outputs(
    tmp_path: Path, version: str = "0.1.0", info_version: str | None = None
) -> list[Path]:
    portable = tmp_path / "portable"
    zip_path = portable / f"LBRDashboard-{version}-windows-x64.zip"
    portable.mkdir(parents=True)
    top = f"LBRDashboard-{version}"
    info = {
        "version": info_version or version,
        "platform": "windows-x64",
        "commit": "0123abc",
        "python": "3.13.9",
        "pyside6": "6.11.2",
    }
    with zipfile.ZipFile(zip_path, "w") as archive:
        archive.writestr(f"{top}/LBRDashboard.exe", b"MZ")
        archive.writestr(f"{top}/build-info.json", json.dumps(info))
        archive.writestr(f"{top}/notices/THIRD-PARTY-NOTICES.txt", b"pyside6 6.11.2 LGPL-3.0\n")
    write_with_checksum(zip_path, zip_path.read_bytes())
    installer = tmp_path / "installer" / f"LBRDashboard-{version}-windows-x64-setup.exe"
    write_with_checksum(installer, b"MZ setup")
    return [portable, installer.parent]


def test_assemble_writes_assets_checksums_and_notes(tmp_path: Path) -> None:
    folders = build_outputs(tmp_path)
    out = tmp_path / "release"
    repo = "https://github.com/example/dashboard"
    assets = release.assemble("v0.1.0", "0.1.0", folders, out, "- First preview.", repo)
    names = [path.name for path in assets]
    assert names == [
        "LBRDashboard-0.1.0-THIRD-PARTY-NOTICES.txt",
        "LBRDashboard-0.1.0-windows-x64-setup.exe",
        "LBRDashboard-0.1.0-windows-x64.zip",
        "SHA256SUMS.txt",
    ]
    sums = (out / "assets" / "SHA256SUMS.txt").read_text(encoding="ascii").splitlines()
    assert len(sums) == 3
    for line in sums:
        digest, name = line.split("  ")
        assert sha256_of(out / "assets" / name) == digest

    notes = (out / "release-notes.md").read_text(encoding="utf-8")
    assert notes.isascii()
    assert notes.startswith("- First preview.\n")
    assert "not code signed" in notes and "SHA256SUMS.txt" in notes
    assert f"{repo}/blob/v0.1.0/docs/releasing.md" in notes
    assert "0123abc" in notes and "PySide6 6.11.2" in notes
    for line in sums:
        assert line in notes

    # Assembling again starts from an empty folder.
    (out / "assets" / "leftover.txt").write_text("x", encoding="ascii")
    release.assemble("v0.1.0", "0.1.0", folders, out, "- First preview.", repo)
    assert not (out / "assets" / "leftover.txt").exists()


def test_incomplete_or_mismatched_builds_are_rejected(tmp_path: Path) -> None:
    def attempt(folders: list[Path]) -> None:
        release.assemble("v0.1.0", "0.1.0", folders, tmp_path / "out", "notes", "r")

    with pytest.raises(release.ReleaseError, match=r"expected one LBRDashboard-\*.zip"):
        attempt([tmp_path / "nothing"])

    folders = build_outputs(tmp_path / "a")
    (folders[1] / "LBRDashboard-0.1.0-windows-x64-setup.exe").unlink()
    with pytest.raises(release.ReleaseError, match="setup.exe artifact, found none"):
        attempt(folders)

    folders = build_outputs(tmp_path / "b", version="0.0.9")
    with pytest.raises(release.ReleaseError, match="should be named LBRDashboard-0.1.0"):
        attempt(folders)

    folders = build_outputs(tmp_path / "c")
    (folders[1] / "LBRDashboard-0.1.0-windows-x64-setup.exe").write_bytes(b"tampered")
    with pytest.raises(release.ReleaseError, match="does not match"):
        attempt(folders)

    folders = build_outputs(tmp_path / "d")
    (folders[0] / "LBRDashboard-0.1.0-windows-x64.zip.sha256").unlink()
    with pytest.raises(release.ReleaseError, match="missing LBRDashboard-0.1.0-windows-x64.zip"):
        attempt(folders)

    folders = build_outputs(tmp_path / "e", info_version="0.0.9")
    with pytest.raises(release.ReleaseError, match="built as version 0.0.9"):
        attempt(folders)
    assert not (tmp_path / "out").exists()


def test_main_checks_and_assembles(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    version = release.build_portable.project_version()
    changelog = tmp_path / "CHANGELOG.md"
    changelog.write_text(f"## [{version}] - Unreleased\n\n- Notes.\n", encoding="ascii")
    outputs = tmp_path / "outputs.txt"
    monkeypatch.delenv("RELEASE_TAG", raising=False)
    base = ["--changelog", str(changelog), "--github-output", str(outputs)]

    assert release.main(["check", *base]) == 0  # A dry run accepts Unreleased.
    assert f"dry run for v{version}" in capsys.readouterr().out
    assert release.main(["check", "--tag", f"v{version}", *base]) == 1
    assert "not dated" in capsys.readouterr().err
    assert release.main(["check", "--tag", "v99.0.0", *base]) == 1

    changelog.write_text(f"## [{version}] - 2026-11-20\n\n- Notes.\n", encoding="ascii")
    folders = build_outputs(tmp_path / "build", version=version)
    out = tmp_path / "release"
    monkeypatch.setenv("RELEASE_TAG", f"v{version}")
    args = ["assemble", *base, "--out", str(out), "--artifacts", *map(str, folders)]
    assert release.main(args) == 0
    assert f"release v{version}" in capsys.readouterr().out
    assert (out / "assets" / "SHA256SUMS.txt").is_file()
    lines = outputs.read_text(encoding="utf-8").splitlines()
    assert lines[-3:] == [f"tag=v{version}", f"version={version}", "prerelease=false"]


# Workflow ----------------------------------------------------------------------------


def workflow_jobs() -> dict[str, str]:
    """Each job's block of ``release.yml`` (the file is checked as text, no YAML parser)."""

    text = WORKFLOW.read_text(encoding="utf-8")
    body = text.split("\njobs:\n", 1)[1]
    parts = re.split(r"^  ([a-z][\w-]*):\n", body, flags=re.MULTILINE)
    return dict(zip(parts[1::2], parts[2::2]))


def test_release_workflow_publishes_only_tested_tags() -> None:
    text = WORKFLOW.read_text(encoding="utf-8")
    assert WORKFLOW.read_bytes().isascii()
    assert 'tags: ["v*.*.*"]' in text
    head = text.split("\njobs:\n", 1)[0]
    assert "permissions:\n  contents: read\n" in head
    jobs = workflow_jobs()
    assert set(jobs) == {"windows", "publish"}
    windows, publish = jobs["windows"], jobs["publish"]

    assert "runs-on: windows-latest" in windows and "contents: write" not in windows
    steps = [
        "prepare_release.py check",
        "check_quality.py --stage tests",
        "build_portable.py",
        "smoke_portable.py dist/portable",
        "build_installer.py dist/portable",
        "smoke_installer.py dist/installer",
        "prepare_release.py assemble",
        "name: release-windows-x64",
    ]
    positions = [windows.index(step) for step in steps]
    assert positions == sorted(positions)

    assert "needs: windows" in publish
    assert "if: github.event_name == 'push' && github.ref_type == 'tag'" in publish
    assert "permissions:\n      contents: write" in publish
    assert "sha256sum --check --strict SHA256SUMS.txt" in publish
    assert "GH_TOKEN: ${{ github.token }}" in publish
    # A draft is only made public after every file is uploaded.
    order = ["gh release create", "gh release view", "gh release edit"]
    assert [publish.index(item) for item in order] == sorted(publish.index(i) for i in order)
    assert "flags=(--draft --verify-tag" in publish and "--draft=false" in publish
    # No other workflow can write releases.
    for other in WORKFLOW.parent.glob("*.yml"):
        if other != WORKFLOW:
            assert "contents: write" not in other.read_text(encoding="utf-8"), other.name


def test_actions_are_pinned_to_commits() -> None:
    for workflow in WORKFLOW.parent.glob("*.yml"):
        for line in workflow.read_text(encoding="utf-8").splitlines():
            if "uses:" in line:
                assert re.search(r"uses: [\w./-]+@[0-9a-f]{40} # v\d", line), (workflow.name, line)
