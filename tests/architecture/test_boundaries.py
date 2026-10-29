"""Static checks for the dependency directions in docs/ARCHITECTURE.md."""

import ast
from importlib.util import resolve_name
from pathlib import Path

import pytest

PACKAGE_ROOT = Path(__file__).parents[2] / "src" / "lbr_dashboard"
APP_MODULES = {path.stem for path in PACKAGE_ROOT.glob("*.py") if path.stem != "__init__"}
FORBIDDEN_IMPORTS = {
    "core": {"app", "ui", "importers"},
    "importers": {"app", "ui"},
    "ui": {"app", "importers"},
}


def _imported_modules(path: Path, node: ast.Import | ast.ImportFrom) -> list[str]:
    if isinstance(node, ast.Import):
        return [alias.name for alias in node.names]
    if node.level:
        relative = path.relative_to(PACKAGE_ROOT).with_suffix("")
        package = ["lbr_dashboard", *relative.parts[:-1]]
        package = package[: len(package) - node.level + 1]
        base = ".".join((*package, *([node.module] if node.module else [])))
    else:
        base = node.module or ""
    return [base, *(f"{base}.{alias.name}" for alias in node.names)]


def _call_argument(node: ast.Call, index: int, keyword: str) -> ast.expr | None:
    if len(node.args) > index:
        return node.args[index]
    return next((item.value for item in node.keywords if item.arg == keyword), None)


def _dynamic_imports(tree: ast.AST) -> list[str]:
    """Resolve literal importlib/__import__ calls, including imported aliases."""

    modules = {"importlib"}
    functions = {"__import__"}
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            modules.update(
                alias.asname or alias.name for alias in node.names if alias.name == "importlib"
            )
        elif isinstance(node, ast.ImportFrom) and not node.level and node.module == "importlib":
            functions.update(
                alias.asname or alias.name for alias in node.names if alias.name == "import_module"
            )
    names = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        function = node.func
        is_import = (isinstance(function, ast.Name) and function.id in functions) or (
            isinstance(function, ast.Attribute)
            and isinstance(function.value, ast.Name)
            and function.value.id in modules
            and function.attr == "import_module"
        )
        argument = _call_argument(node, 0, "name")
        if is_import and isinstance(argument, ast.Constant) and isinstance(argument.value, str):
            name = argument.value
            if isinstance(function, ast.Name) and function.id == "__import__":
                fromlist = _call_argument(node, 3, "fromlist")
                if isinstance(fromlist, (ast.List, ast.Tuple)):
                    names.extend(
                        f"{name}.{item.value}"
                        for item in fromlist.elts
                        if isinstance(item, ast.Constant) and isinstance(item.value, str)
                    )
            elif name.startswith("."):
                package = _call_argument(node, 1, "package")
                if isinstance(package, ast.Constant) and isinstance(package.value, str):
                    try:
                        name = resolve_name(name, package.value)
                    except (ImportError, ValueError):
                        continue  # Invalid relative imports cannot load a dependency.
            names.append(name)
    return names


def _violations(path: Path, source: str) -> list[str]:
    relative = path.relative_to(PACKAGE_ROOT)
    layer = relative.parts[0] if len(relative.parts) > 1 else "app"
    tree = ast.parse(source)
    names = _dynamic_imports(tree)
    violations: list[str] = []
    for node in ast.walk(tree):
        if not isinstance(node, (ast.Import, ast.ImportFrom)):
            continue
        names.extend(_imported_modules(path, node))
        if layer == "ui" and isinstance(node, ast.ImportFrom):
            forbidden_models = {"LogImporter", "LogSession", "LogSessionStore"}
            if any(alias.name in forbidden_models for alias in node.names):
                violations.append(f"{path}: ui imports a model owner or importer contract")
    for name in names:
        parts = name.split(".")
        imported = parts[1] if parts[0] == "lbr_dashboard" and len(parts) > 1 else None
        if imported in APP_MODULES:
            imported = "app"
        if imported in FORBIDDEN_IMPORTS.get(layer, set()):
            violations.append(f"{path}: {layer} -> {imported}")
        if layer == "core" and parts[0] in {"PySide6", "PyQt6", "PyQt5", "pyqtgraph"}:
            violations.append(f"{path}: core imports Qt or PyQtGraph")
    return violations


def test_production_imports_respect_documented_boundaries() -> None:
    violations: list[str] = []
    for path in PACKAGE_ROOT.rglob("*.py"):
        violations.extend(_violations(path, path.read_text(encoding="utf-8")))

    assert violations == [], "Forbidden architecture imports:\n" + "\n".join(violations)


@pytest.mark.parametrize(
    ("path", "source"),
    [
        ("core/log.py", "from .. import ui"),
        ("core/log.py", "from lbr_dashboard import ui"),
        ("core/log.py", "from lbr_dashboard import core, ui as widgets"),
        ("core/log.py", "import lbr_dashboard.core, lbr_dashboard.ui"),
        ("core/log.py", "from lbr_dashboard.ui import main_window"),
        ("core/log.py", "import importlib; importlib.import_module('PySide6.QtCore')"),
        ("core/log.py", "import importlib as loader; loader.import_module('PyQt6.QtCore')"),
        ("core/log.py", "from importlib import import_module as load; load('pyqtgraph')"),
        ("core/log.py", "__import__('PyQt5.QtCore')"),
        ("core/log.py", "import importlib; importlib.import_module(name='PySide6')"),
        ("ui/constants.py", "from .. import importers"),
        ("ui/constants.py", "from lbr_dashboard import importers"),
        ("ui/constants.py", "import importlib; importlib.import_module('lbr_dashboard.importers')"),
        ("importers/csv_importer.py", "from .. import app"),
        ("ui/constants.py", "from ..core.log import LogSession"),
        ("core/nested/module.py", "from ... import ui"),
    ],
)
def test_forbidden_import_forms_are_detected(path: str, source: str) -> None:
    assert _violations(PACKAGE_ROOT / path, source)


@pytest.mark.parametrize(
    ("path", "source"),
    [
        ("core/log.py", "from .. import core"),
        ("core/log.py", "from pathlib import Path"),
        ("core/log.py", "import PySide6_helpers"),
        ("ui/constants.py", "from ..core.log import NumericColumn"),
        ("ui/constants.py", "from PySide6.QtCore import Qt"),
        ("app.py", "from . import ui, importers"),
    ],
)
def test_allowed_import_forms_are_accepted(path: str, source: str) -> None:
    assert not _violations(PACKAGE_ROOT / path, source)


@pytest.mark.parametrize(
    ("path", "source"),
    [
        ("core/log.py", "from .. import ui"),
        ("core/log.py", "from lbr_dashboard import ui"),
        ("core/log.py", "import importlib; importlib.import_module('PySide6.QtCore')"),
        ("ui/constants.py", "from .. import importers"),
        ("ui/constants.py", "from lbr_dashboard import importers"),
        ("core/log.py", "from lbr_dashboard.ui import main_window"),
    ],
)
def test_production_scan_rejects_reported_bypasses(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, path: str, source: str
) -> None:
    """Exercise the actual scanner against mutated production source, not just helpers."""

    target = tmp_path / path
    target.parent.mkdir(parents=True)
    target.write_text(
        (PACKAGE_ROOT / path).read_text(encoding="utf-8") + "\n" + source + "\n", encoding="utf-8"
    )
    monkeypatch.setitem(globals(), "PACKAGE_ROOT", tmp_path)
    with pytest.raises(AssertionError, match="Forbidden architecture imports"):
        test_production_imports_respect_documented_boundaries()


@pytest.mark.parametrize(
    ("layer", "forbidden"),
    [(layer, target) for layer, targets in FORBIDDEN_IMPORTS.items() for target in sorted(targets)],
)
@pytest.mark.parametrize(
    "source",
    [
        "import lbr_dashboard.{target}",
        "import lbr_dashboard.core, lbr_dashboard.{target} as forbidden",
        "from lbr_dashboard import {target} as forbidden",
        "from .. import {target}",
        "from lbr_dashboard.{target} import something",
        "import importlib; importlib.import_module('lbr_dashboard.{target}')",
        "from importlib import import_module as load; load('lbr_dashboard.{target}')",
        "__import__('lbr_dashboard.{target}')",
        "import importlib; importlib.import_module('.{target}', 'lbr_dashboard')",
        "__import__('lbr_dashboard', fromlist=['{target}'])",
    ],
)
def test_dependency_directions_across_import_styles(
    layer: str, forbidden: str, source: str
) -> None:
    assert _violations(PACKAGE_ROOT / layer / "module.py", source.format(target=forbidden))


@pytest.mark.parametrize("module", ["PySide6", "PyQt6", "PyQt5", "pyqtgraph"])
@pytest.mark.parametrize(
    "source",
    [
        "import {module}.QtCore as qt",
        "from {module} import QtCore",
        "import importlib as loader; loader.import_module('{module}.QtCore')",
        "from importlib import import_module; import_module(name='{module}.QtCore')",
        "__import__(name='{module}.QtCore')",
    ],
)
def test_core_framework_import_styles(module: str, source: str) -> None:
    assert _violations(PACKAGE_ROOT / "core" / "module.py", source.format(module=module))


@pytest.mark.parametrize("path", ["core/__init__.py", "core/nested/__init__.py"])
def test_relative_package_initializers(path: str) -> None:
    dots = ".." if path == "core/__init__.py" else "..."
    assert _violations(PACKAGE_ROOT / path, f"from {dots} import ui")
    assert not _violations(PACKAGE_ROOT / path, f"from {dots} import core")


@pytest.mark.parametrize(
    "source",
    [
        "import importlib; importlib.import_module('.core', package='lbr_dashboard')",
        "__import__('lbr_dashboard', None, None, ('core',))",
        "from .importlib import import_module; import_module('PySide6')",
    ],
)
def test_allowed_dynamic_imports(source: str) -> None:
    assert not _violations(PACKAGE_ROOT / "core" / "module.py", source)


@pytest.mark.parametrize("layer", ["core", "importers", "ui"])
@pytest.mark.parametrize("module", sorted(APP_MODULES))
@pytest.mark.parametrize(
    "source",
    [
        "from lbr_dashboard import {module}",
        "from .. import {module}",
        "import importlib; importlib.import_module('lbr_dashboard.{module}')",
    ],
)
def test_layers_cannot_import_top_level_application_modules(
    layer: str,
    module: str,
    source: str,
) -> None:
    assert _violations(PACKAGE_ROOT / layer / "module.py", source.format(module=module))
