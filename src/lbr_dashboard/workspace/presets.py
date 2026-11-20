"""Workspace presets shipped with the dashboard.

Presets live in code so the portable build needs no data files. Each one is
validated like any user file. ``sample_data/workspaces/generic_demo.json`` is
the exact serialized form of the demo preset, kept in sync by a test.
"""

from __future__ import annotations

from typing import Any

from .codec import workspace_from_dict
from .model import CURRENT_VERSION, WORKSPACE_FORMAT, Workspace

#: Overview of the bundled demo sources. It binds the signal IDs of the
#: fictional process and motion simulators and of
#: ``sample_data/timestamped_demo.csv``. Whichever source is open, its
#: widgets fill in and the others show repairable missing-signal placeholders.
#: The statistics tool follows whatever signal is selected in the browser.
DEMO_DOCUMENT: dict[str, Any] = {
    "format": WORKSPACE_FORMAT,
    "version": CURRENT_VERSION,
    "name": "Demo overview",
    "description": (
        "Sensor-agnostic overview for the bundled demo sources: the fictional process "
        "and motion simulators and sample_data/timestamped_demo.csv. Widgets for the "
        "open source fill in; the others show missing-signal placeholders that you can "
        "rebind. Every widget can be rebound, moved, resized or removed."
    ),
    "widgets": [
        {
            "id": "process-plot",
            "type": "time_plot",
            "title": "Process: pressure and temperature",
            "area": "right",
            "axes": [
                {"id": "pressure", "side": "left", "label": "Pressure"},
                {"id": "temperature", "side": "right", "label": "Temperature"},
            ],
            "bindings": [
                {"signal": "process.pressure", "axis": "pressure"},
                {"signal": "process.temperature", "axis": "temperature"},
            ],
        },
        {
            "id": "motion-plot",
            "type": "time_plot",
            "title": "Motion: speed and enabled state",
            "area": "right",
            "axes": [
                {"id": "speed", "side": "left", "label": "Speed"},
                {"id": "state", "side": "right", "label": "Enabled"},
            ],
            "bindings": [
                {"signal": "motion.speed", "axis": "speed"},
                {"signal": "motion.enabled", "axis": "state"},
            ],
        },
        {
            "id": "csv-plot",
            "type": "time_plot",
            "title": "CSV demo: temperature and flow",
            "area": "right",
            "axes": [
                {"id": "first", "side": "left"},
                {"id": "second", "side": "right"},
            ],
            "bindings": [
                {"signal": "csv.column.1", "axis": "first"},
                {"signal": "csv.column.2", "axis": "second"},
            ],
        },
        {
            "id": "process-status",
            "type": "value_cards",
            "title": "Process: status",
            "area": "right",
            "bindings": [{"signal": "process.status"}],
        },
        {
            "id": "motion-vector",
            "type": "value_cards",
            "title": "Motion: vector",
            "area": "right",
            "bindings": [{"signal": "motion.vector"}],
        },
        {
            "id": "csv-valve",
            "type": "value_cards",
            "title": "CSV demo: valve state",
            "area": "right",
            "bindings": [{"signal": "csv.column.3"}],
        },
        {
            "id": "analysis.statistics",
            "type": "analysis_tool",
            "title": "Statistics",
            "tool": "statistics",
            "area": "bottom",
            "bindings": [],
        },
    ],
    "layout": {
        "kind": "split",
        "orientation": "vertical",
        "children": [
            {
                "kind": "split",
                "orientation": "vertical",
                "children": [
                    {
                        "kind": "tabs",
                        "children": [
                            {"kind": "widget", "widget": "process-plot"},
                            {"kind": "widget", "widget": "motion-plot"},
                            {"kind": "widget", "widget": "csv-plot"},
                        ],
                        "current": 0,
                    },
                    {
                        "kind": "tabs",
                        "children": [
                            {"kind": "widget", "widget": "process-status"},
                            {"kind": "widget", "widget": "motion-vector"},
                            {"kind": "widget", "widget": "csv-valve"},
                        ],
                        "current": 0,
                    },
                ],
                "weights": [3.0, 1.0],
            },
            {"kind": "widget", "widget": "analysis.statistics"},
        ],
    },
}


def demo_workspace() -> Workspace:
    """Return the validated demo overview preset."""

    return workspace_from_dict(DEMO_DOCUMENT)
