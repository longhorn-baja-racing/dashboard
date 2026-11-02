"""The project is MIT licensed and every shipped component brings its notices."""

from __future__ import annotations

import importlib.util
import sys
from importlib import metadata
from pathlib import Path
from types import ModuleType

import pytest

ROOT = Path(__file__).resolve().parents[1]


def load_collector() -> ModuleType:
    spec = importlib.util.spec_from_file_location(
        "collect_notices", ROOT / "scripts" / "collect_notices.py"
    )
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules["collect_notices"] = module
    spec.loader.exec_module(module)
    return module


notices = load_collector()


def test_repository_and_package_declare_mit() -> None:
    text = (ROOT / "LICENSE").read_text(encoding="ascii")
    assert text.startswith("MIT License\n\nCopyright (c) 2026 Longhorn Baja Racing\n")
    assert 'THE SOFTWARE IS PROVIDED "AS IS"' in text
    package = metadata.metadata("lbr-dashboard")
    assert package["License-Expression"] == "MIT"
    assert "LICENSE" in (package.get_all("License-File") or [])


def test_vendored_license_texts_are_the_canonical_ascii_files() -> None:
    lgpl = (ROOT / "licenses" / "LGPL-3.0.txt").read_text(encoding="ascii")
    gpl = (ROOT / "licenses" / "GPL-3.0.txt").read_text(encoding="ascii")
    assert lgpl.lstrip().startswith("GNU LESSER GENERAL PUBLIC LICENSE\n")
    assert "Version 3, 29 June 2007" in lgpl and "Version 3, 29 June 2007" in gpl
    assert gpl.lstrip().startswith("GNU GENERAL PUBLIC LICENSE\n")


def test_every_runtime_component_has_notices() -> None:
    components = notices.runtime_components()
    names = [component.name for component in components]
    assert names[0] == "lbr-dashboard" and names[-1] == "python"
    assert {"pyside6", "shiboken6", "pyqtgraph", "numpy"} <= set(names)
    assert "pytest" not in names and "ruff" not in names
    assert notices.missing_notices(components) == []
    qt = next(component for component in components if component.name == "pyside6")
    assert qt.supplemental == ("LGPL-3.0.txt", "GPL-3.0.txt")
    assert qt.source is not None and qt.version in qt.source


def test_collect_writes_files_and_summary(tmp_path: Path) -> None:
    out = tmp_path / "notices"
    summary = notices.collect(out, notices.runtime_components())
    text = summary.read_text(encoding="utf-8")
    assert text.startswith("Third-party notices for the LBR Dashboard")
    assert "lbr-dashboard 0.1.0\n  License: MIT\n  File: lbr-dashboard/LICENSE" in text
    assert "Corresponding source: https://code.qt.io/" in text
    assert (out / "pyside6" / "LGPL-3.0.txt").is_file()
    assert (out / "lbr-dashboard" / "LICENSE").read_text().startswith("MIT License")
    assert any((out / "python").iterdir())
    with pytest.raises(FileExistsError):
        notices.collect(out, notices.runtime_components())


def test_component_without_notices_blocks_collection(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    bare = notices.Component("mystery-lib", "1.0", "unknown", ())
    with pytest.raises(LookupError, match="mystery-lib"):
        notices.collect(tmp_path / "out", [bare])
    monkeypatch.setattr(notices, "runtime_components", lambda: [bare])
    assert notices.main(["--check"]) == 1
    assert "MISSING NOTICES" in capsys.readouterr().out


def test_command_line_check_and_out(tmp_path: Path) -> None:
    assert notices.main(["--check"]) == 0
    assert notices.main(["--out", str(tmp_path / "n")]) == 0
    assert (tmp_path / "n" / "THIRD-PARTY-NOTICES.txt").is_file()


def test_all_qt_for_python_wheels_get_lgpl_texts_and_source_links() -> None:
    for name in (
        "pyside6",
        "pyside6-essentials",
        "pyside6-addons",
        "pyside6-pdf",
        "pyside6-webengine",
        "shiboken6",
    ):
        assert notices.is_qt_for_python(name), name
    for name in ("pyqtgraph", "pyside2", "pyside6x", "numpy"):
        assert not notices.is_qt_for_python(name), name

    class Metadata(dict):
        def get_all(self, key: str) -> list[str] | None:
            return [self[key]] if key in self else None

    class Dist:
        metadata = Metadata(Name="PySide6_WebEngine", License="LGPL-3.0-only")
        version = "6.12.0.140"
        files: list[object] = []

    component = notices._component(Dist())  # type: ignore[arg-type]
    assert component.name == "pyside6-webengine" and component.has_notices
    assert component.supplemental == ("LGPL-3.0.txt", "GPL-3.0.txt")
    assert component.source is not None and component.source.endswith("?h=v6.12.0")


def test_license_name_fallbacks() -> None:
    class Fake:
        def __init__(self, data: dict[str, object]) -> None:
            self.metadata = FakeMetadata(data)

    class FakeMetadata:
        def __init__(self, data: dict[str, object]) -> None:
            self._data = data

        def get_all(self, key: str) -> object:
            value = self._data.get(key)
            return [value] if isinstance(value, str) else value

    assert notices.license_of(Fake({"License": "BSD-3-Clause\nlong text"})) == "BSD-3-Clause"
    classifier = {"Classifier": ["License :: OSI Approved :: BSD License"], "License": "x" * 200}
    assert notices.license_of(Fake(classifier)) == "BSD License"
    assert notices.license_of(Fake({})) == "unknown"
