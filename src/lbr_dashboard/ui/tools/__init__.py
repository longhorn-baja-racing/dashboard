"""Analysis tools listed in the Analysis menu.

Every module in this package that defines ``TOOL = AnalysisTool(...)`` is a
tool. Tools are listed alphabetically by menu label, so adding a tool never
requires editing this file.
"""

from __future__ import annotations

from importlib import import_module
from pkgutil import iter_modules

from ..analysis import AnalysisTool


def discover_tools() -> tuple[AnalysisTool, ...]:
    """Import each tool module in this package and return its tools sorted by label."""

    found: list[AnalysisTool] = []
    for module in iter_modules(__path__):
        tool = getattr(import_module(f"{__name__}.{module.name}"), "TOOL", None)
        if isinstance(tool, AnalysisTool):
            found.append(tool)
    keys = [tool.key for tool in found]
    duplicates = sorted({key for key in keys if keys.count(key) > 1})
    if duplicates:
        raise RuntimeError(f"Analysis tool keys must be unique: {', '.join(duplicates)}")
    return tuple(sorted(found, key=lambda tool: tool.label.casefold()))


TOOLS: tuple[AnalysisTool, ...] = discover_tools()
