"""Analysis tools listed in the Analysis menu, in menu order."""

from __future__ import annotations

from ..analysis import AnalysisTool
from .data_quality import TOOL as DATA_QUALITY
from .statistics import TOOL as STATISTICS

TOOLS: tuple[AnalysisTool, ...] = (STATISTICS, DATA_QUALITY)
