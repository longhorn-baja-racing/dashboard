"""Analysis tools listed in the Analysis menu, in menu order."""

from __future__ import annotations

from ..analysis import AnalysisTool
from .statistics import TOOL as STATISTICS
from .thresholds import TOOL as THRESHOLDS

TOOLS: tuple[AnalysisTool, ...] = (STATISTICS, THRESHOLDS)
