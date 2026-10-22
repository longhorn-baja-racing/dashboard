"""Static checks for the dependency directions in docs/ARCHITECTURE.md."""

import ast
from pathlib import Path

PACKAGE_ROOT = Path(__file__).parents[2] / "src" / "lbr_dashboard"
FORBIDDEN_IMPORTS = {
    "core": {"app", "ui", "importers"},
    "importers": {"app", "ui"},
    "ui": {"importers"},
}


def _imported_module(path: Path, node: ast.Import | ast.ImportFrom) -> str | None:
    if isinstance(node, ast.Import):
        names = [alias.name for alias in node.names]
    elif node.level:
        relative = path.relative_to(PACKAGE_ROOT).with_suffix("")
        package = ["lbr_dashboard", *relative.parts[:-1]]
        package = package[: len(package) - node.level + 1]
        names = [".".join((*package, node.module or ""))]
    else:
        names = [node.module or ""]

    for name in names:
        parts = name.split(".")
        if parts[0] == "lbr_dashboard" and len(parts) > 1:
            return parts[1]
    return None


def test_production_imports_respect_documented_boundaries() -> None:
    violations: list[str] = []
    for path in PACKAGE_ROOT.rglob("*.py"):
        relative = path.relative_to(PACKAGE_ROOT)
        layer = relative.parts[0] if len(relative.parts) > 1 else "app"
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if not isinstance(node, (ast.Import, ast.ImportFrom)):
                continue
            imported = _imported_module(path, node)
            if imported in FORBIDDEN_IMPORTS.get(layer, set()):
                violations.append(f"{path}: {layer} -> {imported}")
            if layer == "ui" and isinstance(node, ast.ImportFrom):
                forbidden_models = {"LogImporter", "LogSession", "LogSessionStore"}
                if any(alias.name in forbidden_models for alias in node.names):
                    violations.append(f"{path}: ui imports a model owner or importer contract")
            if layer == "core":
                names = (
                    [alias.name for alias in node.names]
                    if isinstance(node, ast.Import)
                    else [node.module or ""]
                )
                qt_modules = ("PySide6", "PyQt6", "PyQt5", "pyqtgraph")
                if any(name.startswith(qt_modules) for name in names):
                    violations.append(f"{path}: core imports Qt or PyQtGraph")

    assert violations == [], "Forbidden architecture imports:\n" + "\n".join(violations)
