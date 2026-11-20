"""Version 2 fields, the version 1 upgrade and overwrite protection."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from lbr_dashboard.workspace import (
    CURRENT_VERSION,
    WORKSPACE_FORMAT,
    SignalBinding,
    WidgetConfig,
    WidgetRef,
    Workspace,
    WorkspaceError,
    backup_path,
    dumps_workspace,
    load_workspace,
    loads_workspace,
    read_workspace,
    save_workspace,
    workspace_from_dict,
    workspace_to_dict,
)

ROOT = Path(__file__).resolve().parents[2]
EXAMPLES = ROOT / "sample_data" / "workspaces"
FIXTURES = Path(__file__).resolve().parent / "fixtures"


def minimal() -> dict[str, Any]:
    return {
        "format": WORKSPACE_FORMAT,
        "version": CURRENT_VERSION,
        "name": "Minimal",
        "widgets": [
            {
                "id": "plot",
                "type": "time_plot",
                "axes": [{"id": "left"}],
                "bindings": [{"signal": "speed", "axis": "left"}],
            }
        ],
        "layout": {"kind": "widget", "widget": "plot"},
    }


def tool(**fields: Any) -> dict[str, Any]:
    widget: dict[str, Any] = {"id": "plot", "type": "analysis_tool", "bindings": []}
    widget.update(fields)
    return widget


def make(name: str) -> Workspace:
    return Workspace(
        name=name,
        widgets=(WidgetConfig("values", "value_cards", bindings=(SignalBinding("speed"),)),),
        layout=WidgetRef("values"),
    )


@pytest.mark.parametrize("name", ["numeric_environment.json", "mixed_status.json"])
def test_version_1_files_upgrade_to_the_current_version_unchanged(name: str) -> None:
    old_text = (FIXTURES / f"v1_{name}").read_text(encoding="ascii")
    assert json.loads(old_text)["version"] == 1
    upgraded = loads_workspace(old_text)
    assert upgraded == loads_workspace((EXAMPLES / name).read_text(encoding="ascii"))
    assert workspace_to_dict(upgraded)["version"] == CURRENT_VERSION == 2


def test_dock_placement_hidden_traces_and_analysis_tools_round_trip() -> None:
    data = minimal()
    data["widgets"][0].update(area="left", hidden=True, floating=True)
    data["widgets"][0]["axes"] = [{"id": "left", "side": "left"}]
    data["widgets"][0]["bindings"][0]["hidden"] = True
    data["widgets"].append(
        {
            "id": "analysis.histogram",
            "type": "analysis_tool",
            "title": "Histogram",
            "tool": "histogram",
            "area": "bottom",
            "settings": {"bins": 20, "normalize": False, "range": [0, 1.5]},
            "bindings": [],
        }
    )
    data["layout"] = {
        "kind": "split",
        "orientation": "horizontal",
        "children": [
            {"kind": "widget", "widget": "plot"},
            {"kind": "widget", "widget": "analysis.histogram"},
        ],
    }
    workspace = workspace_from_dict(data)
    plot, histogram = workspace.widgets
    assert (plot.area, plot.hidden, plot.floating, plot.bindings[0].hidden) == (
        "left",
        True,
        True,
        True,
    )
    assert histogram.tool == "histogram" and histogram.settings is not None
    assert histogram.settings["range"] == (0, 1.5)
    with pytest.raises(TypeError):
        histogram.settings["bins"] = 3  # type: ignore[index]
    assert workspace_to_dict(workspace) == data
    assert loads_workspace(dumps_workspace(workspace)) == workspace


@pytest.mark.parametrize(
    ("widget", "location", "code"),
    [
        ({"area": "middle"}, "$.widgets[0].area", "invalid_value"),
        ({"hidden": "yes"}, "$.widgets[0].hidden", "type"),
        ({"floating": 1}, "$.widgets[0].floating", "type"),
        ({"tool": "statistics"}, "$.widgets[0].tool", "unknown_field"),
        ({"settings": {}}, "$.widgets[0].settings", "unknown_field"),
        (
            {"bindings": [{"signal": "speed", "axis": "left", "hidden": None}]},
            "$.widgets[0].bindings[0].hidden",
            "type",
        ),
        (
            {"type": "value_cards", "bindings": [{"signal": "speed", "hidden": True}]},
            "$.widgets[0].bindings[0].hidden",
            "unknown_field",
        ),
        (tool(), "$.widgets[0].tool", "required"),
        (tool(tool="Bad Tool"), "$.widgets[0].tool", "invalid_value"),
        (tool(tool="xy", settings=[1]), "$.widgets[0].settings", "type"),
        (tool(tool="xy", settings={"token": "x"}), "$.widgets[0].settings.token", "secret"),
        (tool(tool="xy", settings={"file": "a"}), "$.widgets[0].settings.file", "machine_local"),
        (tool(tool="xy", settings={"v": [0] * 65}), "$.widgets[0].settings.v", "too_many"),
        (
            tool(tool="xy", settings={f"k{i}": "x" * 60 for i in range(300)}),
            "$.widgets[0].settings",
            "too_large",
        ),
        (
            tool(tool="xy", settings={"source": "C:\\Users\\driver\\log.csv"}),
            "$.widgets[0].settings.source",
            "machine_local",
        ),
    ],
)
def test_version_2_fields_are_validated(widget: dict[str, Any], location: str, code: str) -> None:
    data = minimal()
    data["widgets"][0].update(widget)
    if data["widgets"][0]["type"] != "time_plot":
        data["widgets"][0].pop("axes")
    with pytest.raises(WorkspaceError) as caught:
        workspace_from_dict(data)
    found = [issue.code for issue in caught.value.issues if issue.location == location]
    assert found == [code], [str(issue) for issue in caught.value.issues]


def leftovers(directory: Path) -> list[str]:
    return sorted(p.name for p in directory.iterdir() if p.name.endswith(".tmp"))


def test_a_newer_version_file_is_never_overwritten_or_replaced_by_its_backup(
    tmp_path: Path,
) -> None:
    path = tmp_path / "layout.json"
    save_workspace(path, make("First"))
    save_workspace(path, make("Second"))
    document = json.loads(path.read_text())
    document["version"] = CURRENT_VERSION + 1
    newer = json.dumps(document).encode()
    path.write_bytes(newer)
    with pytest.raises(WorkspaceError) as caught:
        load_workspace(path)
    assert caught.value.codes == {"future_version"}
    with pytest.raises(WorkspaceError) as caught:
        save_workspace(path, make("Third"))
    assert caught.value.codes == {"overwrite_refused"}
    assert "newer dashboard version" in str(caught.value)
    assert path.read_bytes() == newer
    assert read_workspace(backup_path(path)).name == "First"
    assert leftovers(tmp_path) == []


def test_a_file_that_is_not_a_workspace_is_never_overwritten(tmp_path: Path) -> None:
    path = tmp_path / "notes.json"
    path.write_text('{"shopping": ["tires"]}')
    with pytest.raises(WorkspaceError) as caught:
        save_workspace(path, make("First"))
    assert caught.value.codes == {"overwrite_refused"}
    assert "not a dashboard workspace" in str(caught.value)
    assert path.read_text() == '{"shopping": ["tires"]}'
