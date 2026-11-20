"""Analysis tools listed in the Analysis menu, in menu order."""

from __future__ import annotations

from ..analysis import AnalysisTool
from .filters import TOOL as FILTERS
from .statistics import TOOL as STATISTICS

TOOLS: tuple[AnalysisTool, ...] = (STATISTICS, FILTERS)
