"""Collect license and notice files for everything a dashboard build ships.

Walks the runtime dependencies of the installed ``lbr-dashboard`` distribution
(no development extras), copies each one's license files, adds texts for
wheels that ship none (Qt for Python), includes the Python runtime license,
and writes a ``THIRD-PARTY-NOTICES.txt`` summary.

    python scripts/collect_notices.py --check          # fail if anything lacks notices
    python scripts/collect_notices.py --out build/notices

Packaging (#52/#53) copies the output folder into the ZIP and installer.
"""

from __future__ import annotations

import argparse
import re
import shutil
import sys
import sysconfig
from collections.abc import Sequence
from dataclasses import dataclass
from importlib import metadata
from pathlib import Path

from packaging.requirements import Requirement

ROOT = Path(__file__).resolve().parents[1]
LICENSES = ROOT / "licenses"
PROJECT = "lbr-dashboard"

#: Wheels that contain no license text. Qt for Python is used under the
#: LGPL-3.0, which incorporates the GPL-3.0, so both texts must ship.
SUPPLEMENTAL = {
    "pyside6": ("LGPL-3.0.txt", "GPL-3.0.txt"),
    "pyside6-essentials": ("LGPL-3.0.txt", "GPL-3.0.txt"),
    "pyside6-addons": ("LGPL-3.0.txt", "GPL-3.0.txt"),
    "shiboken6": ("LGPL-3.0.txt", "GPL-3.0.txt"),
}
SOURCE_OFFERS = {
    "pyside6": "https://code.qt.io/cgit/pyside/pyside-setup.git/tag/?h=v{version}",
    "pyside6-essentials": "https://code.qt.io/cgit/pyside/pyside-setup.git/tag/?h=v{version}",
    "pyside6-addons": "https://code.qt.io/cgit/pyside/pyside-setup.git/tag/?h=v{version}",
    "shiboken6": "https://code.qt.io/cgit/pyside/pyside-setup.git/tag/?h=v{version}",
}
QT_SOURCE = "https://download.qt.io/official_releases/qt/"
_LICENSE_NAME = re.compile(r"(LICEN[CS]E|COPYING|NOTICE)", re.IGNORECASE)


@dataclass(frozen=True)
class Component:
    """One shipped component and the notice files that must travel with it."""

    name: str
    version: str
    license: str
    files: tuple[Path, ...]
    supplemental: tuple[str, ...] = ()
    source: str | None = None

    @property
    def has_notices(self) -> bool:
        return bool(self.files or self.supplemental)


def normalize(name: str) -> str:
    return re.sub(r"[-_.]+", "-", name).lower()


def _first(data: metadata.PackageMetadata, key: str) -> str:
    values = data.get_all(key) or []
    return str(values[0]) if values else ""


def license_of(distribution: metadata.Distribution) -> str:
    data = distribution.metadata
    expression = _first(data, "License-Expression")
    if expression:
        return expression
    text = _first(data, "License").strip().splitlines()
    if text and len(text[0]) <= 100:
        return text[0]
    classifiers = [c for c in data.get_all("Classifier") or [] if c.startswith("License ::")]
    return classifiers[-1].split(" :: ")[-1] if classifiers else "unknown"


def license_files(distribution: metadata.Distribution) -> tuple[Path, ...]:
    found = []
    for entry in distribution.files or []:
        text = str(entry).replace("\\", "/")
        if ".dist-info/licenses/" in text or (
            ".dist-info/" in text and _LICENSE_NAME.search(entry.name)
        ):
            found.append(Path(str(distribution.locate_file(entry))))
    return tuple(sorted(found))


def _component(distribution: metadata.Distribution) -> Component:
    name = normalize(distribution.metadata["Name"])
    source = SOURCE_OFFERS.get(name)
    return Component(
        name=name,
        version=distribution.version,
        license=license_of(distribution),
        files=license_files(distribution),
        supplemental=SUPPLEMENTAL.get(name, ()),
        source=source.format(version=distribution.version) if source else None,
    )


def python_runtime() -> Component:
    """The interpreter a frozen build bundles, and its license file."""

    candidates = [
        Path(sysconfig.get_paths()["stdlib"]) / "LICENSE.txt",
        Path(sys.base_prefix) / "LICENSE.txt",
    ]
    found = next((path for path in candidates if path.is_file()), None)
    version = ".".join(str(part) for part in sys.version_info[:3])
    return Component("python", version, "PSF-2.0", (found,) if found else ())


def runtime_components(root: str = PROJECT) -> list[Component]:
    """The project, every non-optional runtime dependency, and Python itself."""

    seen: dict[str, Component] = {}
    pending = [root]
    while pending:
        distribution = metadata.distribution(pending.pop())
        component = _component(distribution)
        if component.name in seen:
            continue
        seen[component.name] = component
        for requirement in distribution.requires or []:
            parsed = Requirement(requirement)
            if parsed.marker is None or parsed.marker.evaluate({"extra": ""}):
                pending.append(parsed.name)
    components = sorted(seen.values(), key=lambda c: (c.name != normalize(root), c.name))
    return components + [python_runtime()]


def missing_notices(components: Sequence[Component]) -> list[str]:
    return [component.name for component in components if not component.has_notices]


def summary(components: Sequence[Component]) -> str:
    lines = [
        "Third-party notices for the LBR Dashboard",
        "",
        "This build contains the components below. Each one's license and notice",
        "files are in the folder named after it.",
        "",
    ]
    for component in components:
        lines.append(f"{component.name} {component.version}")
        lines.append(f"  License: {component.license}")
        for path in component.files:
            lines.append(f"  File: {component.name}/{path.name}")
        for name in component.supplemental:
            lines.append(f"  File: {component.name}/{name}")
        if component.source:
            lines.append(f"  Corresponding source: {component.source}")
            lines.append(f"  Qt source: {QT_SOURCE}")
        lines.append("")
    return "\n".join(lines)


def collect(destination: Path, components: Sequence[Component]) -> Path:
    """Copy every notice file into ``destination`` and write the summary."""

    if destination.exists() and any(destination.iterdir()):
        raise FileExistsError(f"{destination} is not empty")
    missing = missing_notices(components)
    if missing:
        raise LookupError(f"no license files for: {', '.join(missing)}")
    for component in components:
        folder = destination / component.name
        folder.mkdir(parents=True, exist_ok=True)
        for path in component.files:
            shutil.copyfile(path, folder / path.name)
        for name in component.supplemental:
            shutil.copyfile(LICENSES / name, folder / name)
    notices = destination / "THIRD-PARTY-NOTICES.txt"
    notices.write_text(summary(components), encoding="utf-8", newline="\n")
    return notices


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Collect license and notice files.")
    group = parser.add_mutually_exclusive_group(required=True)
    group.add_argument("--check", action="store_true", help="only verify notices exist")
    group.add_argument("--out", type=Path, help="empty folder to write notices into")
    options = parser.parse_args(argv)

    components = runtime_components()
    missing = missing_notices(components)
    for component in components:
        status = "ok" if component.has_notices else "MISSING NOTICES"
        print(f"{component.name} {component.version} [{component.license}] {status}")
    if missing:
        print(f"error: no license files for {', '.join(missing)}", file=sys.stderr)
        return 1
    if options.out is not None:
        print(f"wrote {collect(options.out, components)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
