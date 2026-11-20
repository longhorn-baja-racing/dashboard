"""Check a release tag and assemble the tested Windows artifacts (issue #56).

    uv run --locked python scripts/prepare_release.py check --tag v0.1.0
    uv run --locked python scripts/prepare_release.py assemble --tag v0.1.0

``check`` runs before anything is built: the tag must be ``v`` plus the
version in ``src/lbr_dashboard/version.py``, and ``CHANGELOG.md`` must have a
dated section for that version. ``assemble`` runs after the ZIP and installer
smoke tests passed: it verifies each artifact's name, version and ``.sha256``,
copies them with the license notices into ``dist/release/assets``, writes
``SHA256SUMS.txt`` and the release notes. The Release workflow publishes that
folder as is. Without ``--tag`` (or ``RELEASE_TAG``) both commands do a dry
run for ``v<version>`` and accept an ``Unreleased`` changelog section.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import shutil
import sys
import zipfile
from collections.abc import Sequence
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))

import build_portable  # noqa: E402

APP_NAME = build_portable.APP_NAME
PLATFORM = "windows-x64"
CHANGELOG = ROOT / "CHANGELOG.md"
OUT_DIR = ROOT / "dist" / "release"
ARTIFACT_DIRS = (ROOT / "dist" / "portable", ROOT / "dist" / "installer")
HEADING = re.compile(r"^## \[(?P<version>[^\]]+)\](?P<rest>.*)$", re.MULTILINE)
DATE = re.compile(r"^\s+-\s+\d{4}-\d{2}-\d{2}\s*$")
PRERELEASE = re.compile(r"(a|b|rc|dev)\d*", re.IGNORECASE)

NOTES = """\
{changes}

## Downloads

Windows 10 and 11, 64-bit (x64). No Python or administrator rights needed.

| File | What it is |
| --- | --- |
| `{installer}` | Installer: per-user, Start menu shortcut, uninstall entry. |
| `{zip}` | Portable ZIP. Extract and run `{exe}`; nothing is installed. |
| `{notices}` | License notices for bundled Python, Qt and libraries (also inside both). |
| `SHA256SUMS.txt` | SHA-256 checksums of the files above. |

```
{checksums}```

These builds are **not code signed**. Windows SmartScreen may say it protected
your PC: choose "More info", then "Run anyway". Check the download against
`SHA256SUMS.txt` first (`Get-FileHash <file> -Algorithm SHA256` in PowerShell).

Installing over an older version upgrades it in place and keeps your logs and
preferences. To go back to an earlier version, install that release's
installer over this one. See
[docs/releasing.md]({repo}/blob/{tag}/docs/releasing.md) for upgrade and
rollback steps. macOS and Linux builds are not published yet.

Built from {commit} with Python {python} and PySide6 {pyside6}.
"""


class ReleaseError(Exception):
    """The release is not ready; the message says why."""


def expected_tag(version: str) -> str:
    return f"v{version}"


def check_tag(tag: str | None, version: str) -> str:
    """Return the tag to release, or raise when it does not match the package."""

    wanted = expected_tag(version)
    if tag is None:
        return wanted
    if tag != wanted:
        raise ReleaseError(
            f"tag {tag} does not match the package version {version} "
            f"(src/lbr_dashboard/version.py); expected {wanted}"
        )
    return tag


def is_prerelease(version: str) -> bool:
    return PRERELEASE.search(version) is not None


def changelog_section(text: str, version: str, require_date: bool = True) -> str:
    """The body of ``## [<version>] - YYYY-MM-DD`` in the changelog."""

    headings = list(HEADING.finditer(text))
    for index, heading in enumerate(headings):
        if heading.group("version") != version:
            continue
        if require_date and not DATE.match(heading.group("rest")):
            raise ReleaseError(
                f"CHANGELOG.md section [{version}] is not dated; "
                f"write it as '## [{version}] - YYYY-MM-DD' before tagging"
            )
        end = headings[index + 1].start() if index + 1 < len(headings) else len(text)
        body = text[heading.end() : end].strip()
        if not body:
            raise ReleaseError(f"CHANGELOG.md section [{version}] is empty")
        return body
    raise ReleaseError(f"CHANGELOG.md has no '## [{version}]' section")


def artifact_names(version: str) -> dict[str, str]:
    base = f"{APP_NAME}-{version}-{PLATFORM}"
    return {
        "zip": f"{base}.zip",
        "installer": f"{base}-setup.exe",
        "notices": f"{APP_NAME}-{version}-THIRD-PARTY-NOTICES.txt",
    }


def find_artifact(folders: Sequence[Path], name: str, pattern: str) -> Path:
    """Exactly one file matching ``pattern``, and it must be named ``name``."""

    found = sorted({path for folder in folders for path in folder.glob(pattern)})
    if len(found) != 1:
        listed = ", ".join(path.name for path in found) or "none"
        raise ReleaseError(f"expected one {pattern} artifact, found {listed}")
    if found[0].name != name:
        raise ReleaseError(f"{found[0].name} should be named {name}")
    return found[0]


def verify_checksum(artifact: Path) -> str:
    """Compare the artifact with the ``.sha256`` file its build wrote."""

    sidecar = artifact.with_name(artifact.name + ".sha256")
    if not sidecar.is_file():
        raise ReleaseError(f"missing {sidecar.name}")
    recorded = sidecar.read_text(encoding="ascii").split()
    digest = build_portable.sha256_of(artifact)
    if recorded != [digest, artifact.name]:
        raise ReleaseError(f"{sidecar.name} does not match {artifact.name} (actual {digest})")
    return digest


def zip_build_info(zip_path: Path, version: str) -> tuple[dict[str, str], bytes]:
    """``build-info.json`` and the notices from inside the portable ZIP."""

    top = f"{APP_NAME}-{version}"
    with zipfile.ZipFile(zip_path) as archive:
        try:
            info = json.loads(archive.read(f"{top}/build-info.json"))
            notices = archive.read(f"{top}/notices/THIRD-PARTY-NOTICES.txt")
        except KeyError as error:
            raise ReleaseError(f"{zip_path.name} is incomplete: {error}") from error
    for key, wanted in (("version", version), ("platform", PLATFORM)):
        if info.get(key) != wanted:
            raise ReleaseError(f"{zip_path.name} was built as {key} {info.get(key)}, not {wanted}")
    return info, notices


def assemble(
    tag: str, version: str, folders: Sequence[Path], out: Path, changes: str, repo: str
) -> list[Path]:
    """Copy the verified artifacts to ``out/assets`` and write sums and notes."""

    names = artifact_names(version)
    zip_path = find_artifact(folders, names["zip"], f"{APP_NAME}-*.zip")
    installer = find_artifact(folders, names["installer"], f"{APP_NAME}-*-setup.exe")
    digests = {zip_path.name: verify_checksum(zip_path), installer.name: verify_checksum(installer)}
    info, notices = zip_build_info(zip_path, version)

    assets = out / "assets"
    if out.exists():
        shutil.rmtree(out)
    assets.mkdir(parents=True)
    copied = [shutil.copy2(path, assets / path.name) for path in (zip_path, installer)]
    notices_path = assets / names["notices"]
    notices_path.write_bytes(notices)
    digests[notices_path.name] = build_portable.sha256_of(notices_path)
    for path in map(Path, copied):
        if build_portable.sha256_of(path) != digests[path.name]:
            raise ReleaseError(f"{path.name} changed while copying")
    checksums = "".join(f"{digests[name]}  {name}\n" for name in sorted(digests))
    sums = assets / "SHA256SUMS.txt"
    sums.write_text(checksums, encoding="ascii", newline="\n")
    notes = NOTES.format(
        changes=changes,
        installer=installer.name,
        zip=zip_path.name,
        notices=notices_path.name,
        exe=f"{APP_NAME}.exe",
        checksums=checksums,
        repo=repo,
        tag=tag,
        commit=info.get("commit", "unknown"),
        python=info.get("python", "unknown"),
        pyside6=info.get("pyside6", "unknown"),
    )
    (out / "release-notes.md").write_text(notes, encoding="utf-8", newline="\n")
    return sorted(assets.iterdir())


def write_outputs(path: str | None, values: dict[str, str]) -> None:
    if not path:
        return
    with open(path, "a", encoding="utf-8", newline="\n") as stream:
        for key, value in values.items():
            stream.write(f"{key}={value}\n")


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Check and assemble a Windows release.")
    parser.add_argument("command", choices=("check", "assemble"))
    parser.add_argument(
        "--tag",
        default=os.environ.get("RELEASE_TAG") or None,
        help="the pushed tag (default: RELEASE_TAG; none means a dry run)",
    )
    parser.add_argument("--changelog", type=Path, default=CHANGELOG)
    parser.add_argument("--out", type=Path, default=OUT_DIR)
    parser.add_argument(
        "--artifacts", type=Path, nargs="+", default=list(ARTIFACT_DIRS), help="build folders"
    )
    parser.add_argument(
        "--repo",
        default="https://github.com/"
        + os.environ.get("GITHUB_REPOSITORY", "longhorn-baja-racing/dashboard"),
    )
    parser.add_argument("--github-output", default=os.environ.get("GITHUB_OUTPUT"))
    options = parser.parse_args(argv)

    version = build_portable.project_version()
    dry_run = options.tag is None
    try:
        tag = check_tag(options.tag, version)
        text = options.changelog.read_text(encoding="utf-8")
        changes = changelog_section(text, version, require_date=not dry_run)
        if options.command == "assemble":
            assets = assemble(tag, version, options.artifacts, options.out, changes, options.repo)
            for path in assets:
                print(f"release asset {path.name}")
    except (ReleaseError, OSError, ValueError, zipfile.BadZipFile) as error:
        print(f"error: {error}", file=sys.stderr)
        return 1
    prerelease = is_prerelease(version)
    write_outputs(
        options.github_output,
        {"tag": tag, "version": version, "prerelease": str(prerelease).lower()},
    )
    kind = "dry run for" if dry_run else "release"
    print(f"{kind} {tag} (version {version}{', pre-release' if prerelease else ''}) is ready")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
