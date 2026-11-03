"""Workspace documents are validated whole, deterministically and without Qt."""

from __future__ import annotations

import copy
import json
from pathlib import Path
from typing import Any

import pytest

from lbr_dashboard.workspace import (
    CURRENT_VERSION,
    WORKSPACE_FORMAT,
    SignalBinding,
    SplitLayout,
    TabLayout,
    WidgetConfig,
    WidgetRef,
    Workspace,
    WorkspaceError,
    WorkspaceIssue,
    codec,
    dumps_workspace,
    iter_layout_refs,
    loads_workspace,
    migrate_document,
    resolve_bindings,
    workspace_from_dict,
    workspace_to_dict,
)
from lbr_dashboard.workspace.model import freeze_json

EXAMPLES = Path(__file__).resolve().parents[2] / "sample_data" / "workspaces"


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


def codes_for(data: object) -> set[str]:
    with pytest.raises(WorkspaceError) as caught:
        workspace_from_dict(data)
    return set(caught.value.codes)


def issue_at(data: object, location: str) -> WorkspaceIssue:
    with pytest.raises(WorkspaceError) as caught:
        workspace_from_dict(data)
    matches = [issue for issue in caught.value.issues if issue.location == location]
    assert matches, [str(issue) for issue in caught.value.issues]
    return matches[0]


# Round trips ---------------------------------------------------------------------


@pytest.mark.parametrize("name", ["numeric_environment.json", "mixed_status.json"])
def test_example_workspaces_round_trip_byte_for_byte(name: str) -> None:
    text = (EXAMPLES / name).read_text(encoding="ascii")
    workspace = loads_workspace(text)
    assert dumps_workspace(workspace) == text
    assert loads_workspace(dumps_workspace(workspace)) == workspace


def test_mixed_example_covers_numeric_status_tabs_and_extensions() -> None:
    workspace = loads_workspace((EXAMPLES / "mixed_status.json").read_bytes())
    assert {widget.type for widget in workspace.widgets} == {
        "time_plot",
        "event_timeline",
        "value_cards",
        "raw_table",
    }
    assert [ref.widget for ref in iter_layout_refs(workspace.layout)] == [
        "drive-plot",
        "status-timeline",
        "status-values",
        "raw",
    ]
    assert isinstance(workspace.layout, SplitLayout)
    assert isinstance(workspace.layout.children[1], TabLayout)
    assert workspace.extensions["team.notes"]["purpose"].startswith("Onboarding")  # type: ignore[index]
    raw = workspace.widget("raw")
    assert raw is not None and raw.bindings == ()
    assert workspace.widget("missing") is None


def test_minimal_workspace_defaults_and_serialization_are_deterministic() -> None:
    workspace = workspace_from_dict(minimal())
    plot = workspace.widgets[0]
    assert plot.title is None
    assert plot.axes[0].side == "left" and plot.axes[0].range is None
    assert plot.bindings == (SignalBinding("speed", axis="left"),)
    assert workspace.description is None and dict(workspace.extensions) == {}
    first = dumps_workspace(workspace)
    assert first == dumps_workspace(workspace_from_dict(minimal()))
    assert first.isascii() and first.endswith("\n")
    document = json.loads(first)
    assert list(document) == ["format", "version", "name", "widgets", "layout"]


def test_validation_does_not_modify_the_input() -> None:
    data = minimal()
    before = copy.deepcopy(data)
    workspace_from_dict(data)
    assert data == before


def test_workspace_is_deeply_immutable() -> None:
    data = minimal()
    data["extensions"] = {"team.notes": {"tags": ["a", "b"]}}
    workspace = workspace_from_dict(data)
    with pytest.raises(TypeError):
        workspace.extensions["team.other"] = 1  # type: ignore[index]
    assert workspace.extensions["team.notes"]["tags"] == ("a", "b")  # type: ignore[index]
    assert workspace_to_dict(workspace)["extensions"] == {"team.notes": {"tags": ["a", "b"]}}


def test_unicode_text_is_kept_but_written_as_ascii_escapes() -> None:
    data = minimal()
    data["name"] = "Pr\u00fcfstand"
    text = dumps_workspace(workspace_from_dict(data))
    assert text.isascii() and "\\u00fc" in text
    assert loads_workspace(text).name == "Pr\u00fcfstand"


# Unresolved signals --------------------------------------------------------------


def test_missing_signals_stay_as_unresolved_bindings() -> None:
    workspace = loads_workspace((EXAMPLES / "mixed_status.json").read_text())
    resolution = resolve_bindings(workspace, ["throttle_pct", "engine_rpm", "button_mode"])
    assert not resolution.complete
    assert [(b.widget_id, b.binding_index, b.signal) for b in resolution.unresolved] == [
        ("drive-plot", 1, "brake_pct"),
        ("status-timeline", 0, "button_start"),
    ]
    assert len(resolution.resolved) == 5
    assert resolve_bindings(workspace, workspace.signal_ids()).complete
    assert loads_workspace(dumps_workspace(workspace)) == workspace


# Structure and field validation ---------------------------------------------------


def test_every_problem_is_reported_at_once() -> None:
    data = minimal()
    data["name"] = ""
    data["widgets"][0]["type"] = "gauge"
    data["widgets"][0]["bindings"][0]["color"] = "red"
    data["layout"] = {"kind": "widget", "widget": "nope"}
    assert {"type", "unknown_widget_type", "invalid_value", "unknown_reference"} <= codes_for(data)


@pytest.mark.parametrize(
    ("mutate", "location", "code"),
    [
        (lambda d: d.update(colour="x"), "$.colour", "unknown_field"),
        (lambda d: d.pop("name"), "$.name", "required"),
        (lambda d: d.pop("widgets"), "$.widgets", "required"),
        (lambda d: d.pop("layout"), "$.layout", "required"),
        (lambda d: d.update(widgets=[]), "$.widgets", "required"),
        (lambda d: d.update(widgets={}), "$.widgets", "type"),
        (lambda d: d.update(widgets=[1]), "$.widgets[0]", "type"),
        (
            lambda d: d["widgets"][0].update(type="gauge"),
            "$.widgets[0].type",
            "unknown_widget_type",
        ),
        (lambda d: d["widgets"][0].update(type=["x"]), "$.widgets[0].type", "unknown_widget_type"),
        (lambda d: d["widgets"][0].update(id="Plot One"), "$.widgets[0].id", "invalid_value"),
        (lambda d: d["widgets"][0].update(id=""), "$.widgets[0].id", "type"),
        (lambda d: d["widgets"][0].update(id="p" * 129), "$.widgets[0].id", "too_long"),
        (lambda d: d["widgets"][0].update(title="a\x00b"), "$.widgets[0].title", "invalid_value"),
        (lambda d: d["widgets"][0].update(title="x" * 201), "$.widgets[0].title", "too_long"),
        (lambda d: d["widgets"][0].update(script="x"), "$.widgets[0].script", "unknown_field"),
        (lambda d: d["widgets"][0].update(bindings="speed"), "$.widgets[0].bindings", "type"),
        (
            lambda d: d["widgets"][0]["bindings"][0].update(signal="has space"),
            "$.widgets[0].bindings[0].signal",
            "invalid_value",
        ),
        (
            lambda d: d["widgets"][0]["bindings"][0].update(color="#12345"),
            "$.widgets[0].bindings[0].color",
            "invalid_value",
        ),
        (
            lambda d: d["widgets"][0]["bindings"][0].update(axis="right"),
            "$.widgets[0].bindings[0].axis",
            "unknown_reference",
        ),
        (
            lambda d: d["widgets"][0]["bindings"][0].update(axis=["left"]),
            "$.widgets[0].bindings[0].axis",
            "unknown_reference",
        ),
        (
            lambda d: d["widgets"][0]["bindings"][0].pop("signal"),
            "$.widgets[0].bindings[0].signal",
            "required",
        ),
        (
            lambda d: d["widgets"][0]["axes"][0].update(side="top"),
            "$.widgets[0].axes[0].side",
            "invalid_value",
        ),
        (
            lambda d: d["widgets"][0]["axes"][0].update(side={}),
            "$.widgets[0].axes[0].side",
            "invalid_value",
        ),
        (
            lambda d: d["widgets"][0]["axes"][0].update(range=[5, 1]),
            "$.widgets[0].axes[0].range",
            "invalid_value",
        ),
        (
            lambda d: d["widgets"][0]["axes"][0].update(range=[0]),
            "$.widgets[0].axes[0].range",
            "type",
        ),
        (
            lambda d: d["widgets"][0]["axes"][0].update(range=[0, True]),
            "$.widgets[0].axes[0].range",
            "type",
        ),
        (
            lambda d: d["widgets"][0]["axes"][0].update(range="0-1"),
            "$.widgets[0].axes[0].range",
            "type",
        ),
        (lambda d: d["widgets"][0]["axes"][0].update(unit=""), "$.widgets[0].axes[0].unit", "type"),
        (lambda d: d["widgets"][0].update(axes=[{"id": "a"}] * 9), "$.widgets[0].axes", "too_many"),
        (
            lambda d: d["widgets"][0].update(axes=[{"id": "a"}, {"id": "a"}]),
            "$.widgets[0].axes",
            "duplicate_id",
        ),
    ],
)
def test_invalid_fields_report_their_location(mutate: Any, location: str, code: str) -> None:
    data = minimal()
    mutate(data)
    assert issue_at(data, location).code == code


def test_non_object_bindings_and_axes_are_rejected() -> None:
    data = minimal()
    data["widgets"][0]["bindings"] = ["speed"]
    data["widgets"][0]["axes"] = ["left"]
    assert issue_at(data, "$.widgets[0].bindings[0]").code == "type"
    assert issue_at(data, "$.widgets[0].axes[0]").code == "type"


def test_split_without_weights_serializes_without_weights() -> None:
    data = two_widgets(
        {"kind": "split", "orientation": "vertical", "children": [leaf("plot"), leaf("table")]}
    )
    text = dumps_workspace(workspace_from_dict(data))
    assert "weights" not in json.loads(text)["layout"]
    assert loads_workspace(text) == workspace_from_dict(data)


def test_axes_and_axis_bindings_only_belong_to_plots() -> None:
    data = minimal()
    data["widgets"][0]["type"] = "value_cards"
    codes = {
        issue_at(data, "$.widgets[0].axes").code,
        issue_at(data, "$.widgets[0].bindings[0].axis").code,
    }
    assert codes == {"unknown_field"}


def test_widget_and_binding_counts_are_bounded() -> None:
    data = minimal()
    data["widgets"][0]["bindings"] = [{"signal": f"s{i}"} for i in range(65)]
    assert issue_at(data, "$.widgets[0].bindings").code == "too_many"
    data = minimal()
    data["widgets"] = [{"id": f"w{i}", "type": "raw_table"} for i in range(65)]
    assert issue_at(data, "$.widgets").code == "too_many"


def test_multiline_description_is_allowed_but_bounded() -> None:
    data = minimal()
    data["description"] = "line one\n\tline two"
    assert workspace_from_dict(data).description == "line one\n\tline two"
    data["description"] = "x" * 2001
    assert issue_at(data, "$.description").code == "too_long"


# Layout ---------------------------------------------------------------------------


def two_widgets(layout: dict[str, Any]) -> dict[str, Any]:
    data = minimal()
    data["widgets"].append({"id": "table", "type": "raw_table"})
    data["layout"] = layout
    return data


def leaf(widget: str) -> dict[str, str]:
    return {"kind": "widget", "widget": widget}


def test_split_and_tab_layouts_validate() -> None:
    layout = {
        "kind": "tabs",
        "current": 1,
        "children": [
            leaf("plot"),
            {"kind": "split", "orientation": "horizontal", "children": [leaf("table")]},
        ],
    }
    workspace = workspace_from_dict(two_widgets(layout))
    assert isinstance(workspace.layout, TabLayout) and workspace.layout.current == 1
    inner = workspace.layout.children[1]
    assert isinstance(inner, SplitLayout) and inner.weights is None
    assert inner.children == (WidgetRef("table"),)


@pytest.mark.parametrize(
    ("layout", "code"),
    [
        ({"kind": "grid", "children": []}, "invalid_value"),
        ({"kind": ["split"]}, "invalid_value"),
        ([], "type"),
        (
            {"kind": "split", "orientation": "diagonal", "children": [leaf("plot"), leaf("table")]},
            "invalid_value",
        ),
        (
            {"kind": "split", "orientation": ["x"], "children": [leaf("plot"), leaf("table")]},
            "invalid_value",
        ),
        ({"kind": "split", "orientation": "vertical", "children": []}, "required"),
        ({"kind": "split", "children": [leaf("plot"), leaf("table")]}, "required"),
        (
            {
                "kind": "split",
                "orientation": "vertical",
                "children": [leaf("plot"), leaf("table")],
                "weights": [1],
            },
            "invalid_value",
        ),
        (
            {
                "kind": "split",
                "orientation": "vertical",
                "children": [leaf("plot"), leaf("table")],
                "weights": [1, 0],
            },
            "invalid_value",
        ),
        (
            {"kind": "tabs", "children": [leaf("plot"), leaf("table")], "current": 2},
            "invalid_value",
        ),
        ({"kind": "tabs", "children": [leaf("plot"), leaf("table")], "current": True}, "type"),
        (
            {"kind": "tabs", "children": [leaf("plot"), leaf("table")], "sizes": [1]},
            "unknown_field",
        ),
        ({"kind": "tabs", "children": [leaf("plot"), leaf("plot")]}, "duplicate_placement"),
        ({"kind": "tabs", "children": [leaf("plot")]}, "unplaced_widget"),
        (
            {"kind": "tabs", "children": [leaf("plot"), leaf("table"), leaf("ghost")]},
            "unknown_reference",
        ),
        (
            {"kind": "tabs", "children": [leaf("plot"), leaf("table"), leaf("Bad Id")]},
            "invalid_value",
        ),
        ({"kind": "tabs", "children": [leaf("plot"), leaf("table"), 3]}, "type"),
        (
            {"kind": "tabs", "children": [leaf("plot"), leaf("table")] + [leaf("x")] * 15},
            "too_many",
        ),
    ],
)
def test_invalid_layouts_are_rejected(layout: Any, code: str) -> None:
    assert code in codes_for(two_widgets(layout))


def test_layout_depth_is_bounded() -> None:
    layout: dict[str, Any] = leaf("plot")
    for _ in range(8):
        layout = {"kind": "tabs", "children": [layout]}
    data = minimal()
    data["layout"] = layout
    assert "too_deep" in codes_for(data)


def test_duplicate_widget_ids_are_rejected() -> None:
    data = two_widgets({"kind": "tabs", "children": [leaf("plot"), leaf("table")]})
    data["widgets"][1]["id"] = "plot"
    assert "duplicate_id" in codes_for(data)


# Version and format ---------------------------------------------------------------


@pytest.mark.parametrize(
    ("change", "code"),
    [
        ({"format": "something-else"}, "unknown_format"),
        ({"version": "1"}, "type"),
        ({"version": True}, "type"),
        ({"version": 0}, "type"),
        ({"version": CURRENT_VERSION + 1}, "future_version"),
    ],
)
def test_unknown_formats_and_future_versions_are_refused(change: dict[str, Any], code: str) -> None:
    data = minimal()
    data.update(change)
    assert codes_for(data) == {code}


def test_document_must_be_an_object() -> None:
    assert codes_for([minimal()]) == {"type"}


def test_migration_is_deterministic_and_leaves_input_untouched() -> None:
    def rename_name(data: dict[str, Any]) -> dict[str, Any]:
        data["name"] = data.pop("title")
        return data

    old = minimal()
    old["version"] = 1
    old["title"] = old.pop("name")
    before = copy.deepcopy(old)
    migrated = migrate_document(old, migrations={1: rename_name}, current_version=2)
    assert migrated["version"] == 2 and migrated["name"] == "Minimal"
    assert old == before
    assert migrate_document(old, migrations={1: rename_name}, current_version=2) == migrated


def test_migration_gaps_and_bad_results_are_refused() -> None:
    with pytest.raises(WorkspaceError) as caught:
        migrate_document(minimal(), migrations={}, current_version=2)
    assert caught.value.codes == {"unsupported_version"}
    with pytest.raises(WorkspaceError) as caught:
        migrate_document(minimal(), migrations={1: lambda data: []}, current_version=2)  # type: ignore[dict-item]
    assert caught.value.codes == {"type"}


def test_migrated_documents_are_validated_like_any_other() -> None:
    def add_secret(data: dict[str, Any]) -> dict[str, Any]:
        data["title"] = "C:\\Users\\driver\\log.csv"
        return data

    with pytest.raises(WorkspaceError) as caught:
        workspace_from_dict(minimal(), migrations={1: add_secret}, current_version=2)
    assert caught.value.codes == {"machine_local"}


# Untrusted text ---------------------------------------------------------------------


def test_text_round_trip_and_bom() -> None:
    text = json.dumps(minimal())
    assert loads_workspace("\ufeff" + text) == loads_workspace(text.encode("utf-8"))


@pytest.mark.parametrize(
    ("raw", "code"),
    [
        (b"{not json", "json"),
        (b'{"name": "a", "name": "b"}', "json"),
        (b'{"value": NaN}', "json"),
        (b'{"value": Infinity}', "json"),
        (b"\xff\xfe\x00", "decode"),
        (b" " * (codec.MAX_DOCUMENT_BYTES + 1), "too_large"),
    ],
    ids=["syntax", "duplicate-key", "nan", "infinity", "not-utf8", "too-large"],
)
def test_malformed_text_is_rejected_with_one_structured_issue(raw: bytes, code: str) -> None:
    with pytest.raises(WorkspaceError) as caught:
        loads_workspace(raw)
    assert caught.value.codes == {code}
    assert caught.value.issues[0].location == "$"


def test_deep_json_text_is_refused_before_parsing() -> None:
    with pytest.raises(WorkspaceError) as caught:
        loads_workspace("[" * 40 + "]" * 40)
    assert caught.value.codes == {"too_deep"}
    assert caught.value.issues[0].location == "$"


def test_deep_decoded_data_is_refused_at_the_offending_location() -> None:
    nested: list[Any] = []
    for _ in range(40):
        nested = [nested]
    with pytest.raises(WorkspaceError) as caught:
        workspace_from_dict(nested)
    assert caught.value.codes == {"too_deep"}
    assert caught.value.issues[0].location == "$" + "[0]" * 32


def test_pathological_nesting_is_refused_without_recursing() -> None:
    depth = 400_000
    with pytest.raises(WorkspaceError) as caught:
        loads_workspace("[" * depth + "]" * depth)
    assert caught.value.codes == {"too_deep"}


def test_brackets_inside_strings_do_not_count_as_nesting() -> None:
    data = minimal()
    data["widgets"][0]["title"] = '[{"x": [[[[' * 10 + 'escaped \\" quote'
    workspace = loads_workspace(json.dumps(data))
    assert workspace.widgets[0].title == data["widgets"][0]["title"]


def test_non_json_python_values_are_refused() -> None:
    data = minimal()
    data["extensions"] = {"team.notes": {1, 2}}
    assert codes_for(data) == {"type"}
    data["extensions"] = {"team.notes": {1: "x"}}
    assert codes_for(data) == {"type"}
    data["extensions"] = {"team.notes": float("nan")}
    assert codes_for(data) == {"invalid_value"}


# Portability ----------------------------------------------------------------------


@pytest.mark.parametrize(
    "value",
    [
        "C:\\Users\\driver\\run.csv",
        "D:/logs/run.csv",
        "\\\\team-share\\logs",
        "/home/driver/run.csv",
        "/Users/driver/run.csv",
        "/dev/ttyUSB0",
        "COM3",
        "~/logs",
        "file:///tmp/run.csv",
    ],
)
def test_machine_local_paths_and_ports_are_refused(value: str) -> None:
    data = minimal()
    data["widgets"][0]["title"] = value
    assert issue_at(data, "$.widgets[0].title").code == "machine_local"


def test_unit_and_label_slashes_are_not_mistaken_for_paths() -> None:
    data = minimal()
    data["widgets"][0]["axes"][0].update(unit="m/s", label="Speed / time")
    assert workspace_from_dict(data).widgets[0].axes[0].unit == "m/s"


@pytest.mark.parametrize(
    ("extensions", "code"),
    [
        ({"notes": "x"}, "invalid_value"),
        ({"team.notes": {"password": "x"}}, "secret"),
        ({"team.notes": {"apiKey": "x"}}, "secret"),
        ({"team.notes": {"api_key": "x"}}, "secret"),
        ({"team.notes": {"private-key": "x"}}, "secret"),
        ({"team.auth": {"token": "x"}}, "secret"),
        ({"team.source": {"serial_port": "x"}}, "machine_local"),
        ({"team.source": {"log_path": "x"}}, "machine_local"),
        ({"team.source": {"C:\\logs": 1}}, "machine_local"),
        ({"team.samples": list(range(65))}, "too_many"),
        ({"team.blob": "x" * 16_400}, "too_large"),
        ([], "type"),
    ],
)
def test_extensions_cannot_carry_secrets_local_details_or_telemetry(
    extensions: Any, code: str
) -> None:
    data = minimal()
    data["extensions"] = extensions
    assert code in codes_for(data)


# In-code construction ---------------------------------------------------------------


def test_invalid_workspaces_built_in_code_cannot_be_serialized() -> None:
    workspace = Workspace(
        name="Bad",
        widgets=(WidgetConfig("plot", "time_plot", bindings=(SignalBinding("x", axis="nope"),)),),
        layout=WidgetRef("plot"),
    )
    with pytest.raises(WorkspaceError) as caught:
        dumps_workspace(workspace)
    assert caught.value.codes == {"unknown_reference"}


def test_error_and_freeze_helpers_guard_their_inputs() -> None:
    with pytest.raises(ValueError):
        WorkspaceError([])
    issue = WorkspaceIssue("$.name", "required", "is required")
    assert str(issue) == "$.name: is required [required]"
    assert str(WorkspaceError([issue])) == str(issue)
    with pytest.raises(TypeError):
        freeze_json(object())
