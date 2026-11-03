"""Decode, validate, migrate and encode workspace documents without Qt.

Untrusted input is only ever parsed as JSON data. Nothing is imported,
evaluated or unpickled, and a document is either accepted whole or rejected
with structured issues; there is no partially applied result.
"""

from __future__ import annotations

import copy
import json
import re
from collections.abc import Callable, Collection, Mapping
from math import isfinite
from typing import Any

from .model import (
    AXIS_SIDES,
    AXIS_WIDGET_TYPES,
    CURRENT_VERSION,
    SPLIT_ORIENTATIONS,
    WIDGET_TYPES,
    WORKSPACE_FORMAT,
    AxisPreference,
    LayoutNode,
    SignalBinding,
    SplitLayout,
    TabLayout,
    WidgetConfig,
    WidgetRef,
    Workspace,
    WorkspaceError,
    WorkspaceIssue,
    iter_layout_refs,
    thaw_json,
)

MAX_DOCUMENT_BYTES = 1_048_576
MAX_JSON_DEPTH = 32
MAX_WIDGETS = 64
MAX_BINDINGS_PER_WIDGET = 64
MAX_AXES_PER_WIDGET = 8
MAX_LAYOUT_DEPTH = 8
MAX_LAYOUT_CHILDREN = 16
MAX_ID_LENGTH = 128
MAX_TEXT_LENGTH = 200
MAX_DESCRIPTION_LENGTH = 2000
MAX_EXTENSIONS_BYTES = 16_384
MAX_EXTENSION_LIST_LENGTH = 64

#: Upgrade functions keyed by the version they upgrade *from*. Each receives a
#: private copy of version N data and returns version N + 1 data. Version 1 is
#: the first published format, so there is nothing to migrate yet.
Migration = Callable[[dict[str, Any]], dict[str, Any]]
MIGRATIONS: Mapping[int, Migration] = {}

_INSTANCE_ID = re.compile(r"^[a-z][a-z0-9]*(?:[._-][a-z0-9]+)*$")
_SIGNAL_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:-]*$")
_EXTENSION_KEY = re.compile(r"^[a-z][a-z0-9_-]*(?:\.[a-z0-9_-]+)+$")
_COLOR = re.compile(r"^#[0-9A-Fa-f]{6}$")
_MACHINE_LOCAL = re.compile(
    r"^(?:[A-Za-z]:[\\/]|\\\\|~[\\/]|file:|/dev/"
    r"|/(?:home|Users|tmp|mnt|media|var|etc|opt|root|private|Volumes)/)"
    r"|^(?:COM|LPT)[0-9]+$",
    re.IGNORECASE,
)
_SECRET_TOKENS = frozenset(
    {"password", "passwd", "secret", "token", "credential", "credentials", "apikey", "auth"}
)
_LOCAL_TOKENS = frozenset(
    {"path", "filepath", "file", "filename", "dir", "directory", "folder", "port"}
)
_KEY_TOKEN = re.compile(r"[a-z0-9]+")

_TOP_FIELDS = frozenset({"format", "version", "name", "description", "widgets", "layout"})
_WIDGET_FIELDS = frozenset({"id", "type", "title", "bindings", "axes"})
_BINDING_FIELDS = frozenset({"signal", "label", "color", "axis"})
_AXIS_FIELDS = frozenset({"id", "side", "label", "unit", "range"})
_LAYOUT_FIELDS = {
    "widget": frozenset({"kind", "widget"}),
    "split": frozenset({"kind", "orientation", "children", "weights"}),
    "tabs": frozenset({"kind", "children", "current"}),
}


def _fail(location: str, code: str, message: str) -> WorkspaceError:
    return WorkspaceError([WorkspaceIssue(location, code, message)])


def _member(value: object, options: Collection[str]) -> bool:
    """Membership test that is safe for unhashable JSON values such as lists."""

    return isinstance(value, str) and value in options


def _show(value: object) -> str:
    text = value if isinstance(value, str) else type(value).__name__
    return text if len(text) <= 40 else text[:37] + "..."


def _is_number(value: object) -> bool:
    return isinstance(value, (int, float)) and not isinstance(value, bool)


class _Validator:
    """Collect every issue in one pass over already-bounded JSON data."""

    def __init__(self) -> None:
        self.issues: list[WorkspaceIssue] = []

    def issue(self, location: str, code: str, message: str) -> None:
        self.issues.append(WorkspaceIssue(location, code, message))

    def fields(
        self, value: object, location: str, allowed: frozenset[str], required: tuple[str, ...]
    ) -> dict[str, Any] | None:
        if not isinstance(value, dict):
            self.issue(location, "type", "must be an object")
            return None
        for key in sorted(set(value) - allowed):
            self.issue(f"{location}.{key}", "unknown_field", "is not part of this format")
        for key in required:
            if key not in value:
                self.issue(f"{location}.{key}", "required", "is required")
        return value

    def text(
        self, value: object, location: str, *, limit: int = MAX_TEXT_LENGTH, multiline: bool = False
    ) -> str | None:
        if not isinstance(value, str) or not value.strip():
            self.issue(location, "type", "must be a non-empty string")
            return None
        if len(value) > limit:
            self.issue(location, "too_long", f"must be at most {limit} characters")
            return None
        allowed = "\n\t" if multiline else ""
        if any(not ch.isprintable() and ch not in allowed for ch in value):
            self.issue(location, "invalid_value", "must not contain control characters")
            return None
        return value

    def optional_text(self, data: dict[str, Any], key: str, location: str) -> str | None:
        return self.text(data[key], f"{location}.{key}") if key in data else None

    def identifier(self, value: object, location: str, pattern: re.Pattern[str]) -> str | None:
        if not isinstance(value, str) or not value:
            self.issue(location, "type", "must be a non-empty string")
            return None
        if len(value) > MAX_ID_LENGTH:
            self.issue(location, "too_long", f"must be at most {MAX_ID_LENGTH} characters")
            return None
        if pattern.fullmatch(value) is None:
            self.issue(location, "invalid_value", f"'{value}' is not a valid ID")
            return None
        return value

    def items(self, value: object, location: str, limit: int, *, allow_empty: bool) -> list[Any]:
        if not isinstance(value, list):
            self.issue(location, "type", "must be an array")
            return []
        if not value and not allow_empty:
            self.issue(location, "required", "must not be empty")
        if len(value) > limit:
            self.issue(location, "too_many", f"must have at most {limit} entries")
            return []
        return value

    # Document parts -----------------------------------------------------------

    def workspace(self, data: dict[str, Any]) -> Workspace | None:
        top = self.fields(data, "$", _TOP_FIELDS | {"extensions"}, ("name", "widgets", "layout"))
        if top is None:  # pragma: no cover - migrate_document only passes objects
            return None
        name = self.text(top.get("name"), "$.name") if "name" in top else None
        description = None
        if "description" in top:
            description = self.text(
                top["description"],
                "$.description",
                limit=MAX_DESCRIPTION_LENGTH,
                multiline=True,
            )
        raw_widgets = (
            self.items(top["widgets"], "$.widgets", MAX_WIDGETS, allow_empty=False)
            if "widgets" in top
            else []
        )
        widgets = [
            self.widget(item, f"$.widgets[{index}]") for index, item in enumerate(raw_widgets)
        ]
        layout = self.layout(top["layout"], "$.layout", 1) if "layout" in top else None
        extensions = self.extensions(top.get("extensions", {}), "$.extensions")

        valid_widgets = [widget for widget in widgets if widget is not None]
        self.cross_check(valid_widgets, layout)
        if self.issues or name is None or layout is None or extensions is None:
            return None
        return Workspace(
            name=name,
            description=description,
            widgets=tuple(valid_widgets),
            layout=layout,
            extensions=extensions,
        )

    def widget(self, value: object, location: str) -> WidgetConfig | None:
        data = self.fields(value, location, _WIDGET_FIELDS, ("id", "type"))
        if data is None:
            return None
        before = len(self.issues)
        widget_id = self.identifier(data.get("id"), f"{location}.id", _INSTANCE_ID)
        widget_type = data.get("type")
        if "type" in data and not _member(widget_type, WIDGET_TYPES):
            self.issue(
                f"{location}.type",
                "unknown_widget_type",
                f"'{_show(widget_type)}' is not one of {sorted(WIDGET_TYPES)}",
            )
        title = self.optional_text(data, "title", location)

        axes: list[AxisPreference] = []
        if "axes" in data:
            if not _member(widget_type, AXIS_WIDGET_TYPES):
                self.issue(f"{location}.axes", "unknown_field", "only plot widgets have axes")
            raw = self.items(
                data["axes"], f"{location}.axes", MAX_AXES_PER_WIDGET, allow_empty=True
            )
            for index, item in enumerate(raw):
                axis = self.axis(item, f"{location}.axes[{index}]")
                if axis is not None:
                    axes.append(axis)
        axis_ids = [axis.id for axis in axes]
        for axis_id in sorted({a for a in axis_ids if axis_ids.count(a) > 1}):
            self.issue(f"{location}.axes", "duplicate_id", f"axis '{axis_id}' is defined twice")

        bindings: list[SignalBinding] = []
        raw = self.items(
            data.get("bindings", []),
            f"{location}.bindings",
            MAX_BINDINGS_PER_WIDGET,
            allow_empty=True,
        )
        for index, item in enumerate(raw):
            binding = self.binding(
                item, f"{location}.bindings[{index}]", widget_type, set(axis_ids)
            )
            if binding is not None:
                bindings.append(binding)

        if len(self.issues) != before or widget_id is None or not isinstance(widget_type, str):
            return None
        return WidgetConfig(widget_id, widget_type, title, tuple(bindings), tuple(axes))

    def binding(
        self, value: object, location: str, widget_type: object, axis_ids: set[str]
    ) -> SignalBinding | None:
        data = self.fields(value, location, _BINDING_FIELDS, ("signal",))
        if data is None:
            return None
        before = len(self.issues)
        signal = self.identifier(data.get("signal"), f"{location}.signal", _SIGNAL_ID)
        label = self.optional_text(data, "label", location)
        color = data.get("color")
        if "color" in data and (not isinstance(color, str) or _COLOR.fullmatch(color) is None):
            self.issue(f"{location}.color", "invalid_value", "must look like #rrggbb")
        axis = data.get("axis")
        if "axis" in data:
            if not _member(widget_type, AXIS_WIDGET_TYPES):
                self.issue(f"{location}.axis", "unknown_field", "only plot bindings use an axis")
            elif not _member(axis, axis_ids):
                self.issue(
                    f"{location}.axis",
                    "unknown_reference",
                    f"no axis '{_show(axis)}' in this widget",
                )
        if len(self.issues) != before or signal is None:
            return None
        return SignalBinding(signal, label, color, axis)

    def axis(self, value: object, location: str) -> AxisPreference | None:
        data = self.fields(value, location, _AXIS_FIELDS, ("id",))
        if data is None:
            return None
        before = len(self.issues)
        axis_id = self.identifier(data.get("id"), f"{location}.id", _INSTANCE_ID)
        side = data.get("side", "left")
        if not _member(side, AXIS_SIDES):
            self.issue(f"{location}.side", "invalid_value", f"must be one of {sorted(AXIS_SIDES)}")
        label = self.optional_text(data, "label", location)
        unit = self.optional_text(data, "unit", location)
        bounds: tuple[float, float] | None = None
        if "range" in data:
            raw = data["range"]
            if (
                not isinstance(raw, list)
                or len(raw) != 2
                or not all(_is_number(item) and isfinite(item) for item in raw)
            ):
                self.issue(f"{location}.range", "type", "must be [minimum, maximum] numbers")
            elif not raw[0] < raw[1]:
                self.issue(f"{location}.range", "invalid_value", "minimum must be below maximum")
            else:
                bounds = (float(raw[0]), float(raw[1]))
        if len(self.issues) != before or axis_id is None:
            return None
        return AxisPreference(axis_id, side, label, unit, bounds)

    def layout(self, value: object, location: str, depth: int) -> LayoutNode | None:
        if depth > MAX_LAYOUT_DEPTH:
            self.issue(location, "too_deep", f"layout nesting is limited to {MAX_LAYOUT_DEPTH}")
            return None
        if not isinstance(value, dict):
            self.issue(location, "type", "must be an object")
            return None
        kind = value.get("kind")
        if not isinstance(kind, str) or kind not in _LAYOUT_FIELDS:
            self.issue(
                f"{location}.kind", "invalid_value", f"must be one of {sorted(_LAYOUT_FIELDS)}"
            )
            return None
        required = {
            "widget": ("widget",),
            "split": ("orientation", "children"),
            "tabs": ("children",),
        }
        data = self.fields(value, location, _LAYOUT_FIELDS[kind], required[kind])
        if data is None:  # pragma: no cover - checked above
            return None
        before = len(self.issues)
        if kind == "widget":
            widget_id = self.identifier(data.get("widget"), f"{location}.widget", _INSTANCE_ID)
            return WidgetRef(widget_id) if widget_id is not None else None

        raw_children = self.items(
            data.get("children", []), f"{location}.children", MAX_LAYOUT_CHILDREN, allow_empty=False
        )
        children = [
            self.layout(child, f"{location}.children[{index}]", depth + 1)
            for index, child in enumerate(raw_children)
        ]
        if kind == "split":
            orientation = data.get("orientation")
            if "orientation" in data and not _member(orientation, SPLIT_ORIENTATIONS):
                self.issue(
                    f"{location}.orientation",
                    "invalid_value",
                    f"must be one of {sorted(SPLIT_ORIENTATIONS)}",
                )
            weights = data.get("weights")
            if "weights" in data and (
                not isinstance(weights, list)
                or len(weights) != len(raw_children)
                or not all(_is_number(w) and isfinite(w) and w > 0 for w in weights)
            ):
                self.issue(
                    f"{location}.weights",
                    "invalid_value",
                    "must be one positive number per child",
                )
            if len(self.issues) != before:
                return None
            return SplitLayout(
                str(orientation),
                tuple(child for child in children if child is not None),
                tuple(float(w) for w in weights) if isinstance(weights, list) else None,
            )

        current = data.get("current", 0)
        if not isinstance(current, int) or isinstance(current, bool):
            self.issue(f"{location}.current", "type", "must be an integer")
        elif raw_children and not 0 <= current < len(raw_children):
            self.issue(f"{location}.current", "invalid_value", "must index one of the children")
        if len(self.issues) != before:
            return None
        return TabLayout(tuple(child for child in children if child is not None), current)

    def extensions(self, value: object, location: str) -> dict[str, Any] | None:
        if not isinstance(value, dict):
            self.issue(location, "type", "must be an object")
            return None
        before = len(self.issues)
        for key in value:
            if _EXTENSION_KEY.fullmatch(key) is None:
                self.issue(
                    f"{location}.{key}",
                    "invalid_value",
                    "extension keys must be namespaced, for example 'team.notes'",
                )
        encoded = json.dumps(value, ensure_ascii=True, allow_nan=False, separators=(",", ":"))
        if len(encoded) > MAX_EXTENSIONS_BYTES:
            self.issue(
                location, "too_large", f"must encode to at most {MAX_EXTENSIONS_BYTES} bytes"
            )
        stack: list[tuple[object, str]] = [(value, location)]
        while stack:
            current, where = stack.pop()
            if isinstance(current, dict):
                for key, item in current.items():
                    tokens = set(_KEY_TOKEN.findall(key.lower()))
                    secret = tokens & _SECRET_TOKENS or {"api", "key"} <= tokens
                    if secret or {"private", "key"} <= tokens:
                        self.issue(
                            f"{where}.{key}", "secret", "credentials do not belong in a workspace"
                        )
                    elif tokens & _LOCAL_TOKENS:
                        self.issue(
                            f"{where}.{key}",
                            "machine_local",
                            "paths and ports are machine-specific; keep them in local settings",
                        )
                    stack.append((item, f"{where}.{key}"))
            elif isinstance(current, list):
                if len(current) > MAX_EXTENSION_LIST_LENGTH:
                    self.issue(
                        where,
                        "too_many",
                        f"arrays are limited to {MAX_EXTENSION_LIST_LENGTH} entries; "
                        "workspaces do not store telemetry",
                    )
                stack.extend((item, f"{where}[{i}]") for i, item in enumerate(current))
        return value if len(self.issues) == before else None

    def cross_check(self, widgets: list[WidgetConfig], layout: LayoutNode | None) -> None:
        ids = [widget.id for widget in widgets]
        for widget_id in sorted({i for i in ids if ids.count(i) > 1}):
            self.issue("$.widgets", "duplicate_id", f"widget '{widget_id}' is defined twice")
        if layout is None:
            return
        placed = [ref.widget for ref in iter_layout_refs(layout)]
        known = set(ids)
        for widget_id in sorted(set(placed) - known):
            self.issue("$.layout", "unknown_reference", f"no widget '{widget_id}'")
        for widget_id in sorted({i for i in placed if placed.count(i) > 1}):
            self.issue("$.layout", "duplicate_placement", f"widget '{widget_id}' is placed twice")
        for widget_id in sorted(known - set(placed)):
            self.issue("$.layout", "unplaced_widget", f"widget '{widget_id}' is not in the layout")


def _check_bounds(data: object) -> None:
    """Reject over-deep data and machine-local strings before any interpretation."""

    issues: list[WorkspaceIssue] = []
    stack: list[tuple[object, str, int]] = [(data, "$", 1)]
    while stack:
        value, where, depth = stack.pop()
        if depth > MAX_JSON_DEPTH:
            raise _fail(where, "too_deep", f"nesting is limited to {MAX_JSON_DEPTH} levels")
        if isinstance(value, dict):
            for key, item in value.items():
                if not isinstance(key, str):
                    raise _fail(where, "type", "object keys must be strings")
                if _MACHINE_LOCAL.search(key):
                    issues.append(
                        WorkspaceIssue(f"{where}.{key}", "machine_local", "looks like a local path")
                    )
                stack.append((item, f"{where}.{key}", depth + 1))
        elif isinstance(value, list):
            stack.extend((item, f"{where}[{i}]", depth + 1) for i, item in enumerate(value))
        elif isinstance(value, str):
            if _MACHINE_LOCAL.search(value):
                issues.append(
                    WorkspaceIssue(
                        where,
                        "machine_local",
                        "looks like a local file path or port; workspaces must be portable",
                    )
                )
        elif isinstance(value, float) and not isfinite(value):
            issues.append(WorkspaceIssue(where, "invalid_value", "must be a finite number"))
        elif value is not None and not isinstance(value, (bool, int, float)):
            raise _fail(where, "type", f"{type(value).__name__} is not JSON data")
    if issues:
        raise WorkspaceError(issues)


def migrate_document(
    data: object,
    *,
    migrations: Mapping[int, Migration] = MIGRATIONS,
    current_version: int = CURRENT_VERSION,
) -> dict[str, Any]:
    """Return a private copy of ``data`` upgraded to ``current_version``.

    The input is never modified. Unknown formats, non-integer versions and
    versions newer than this build are refused instead of guessed at.
    """

    if not isinstance(data, dict):
        raise _fail("$", "type", "a workspace must be a JSON object")
    if data.get("format") != WORKSPACE_FORMAT:
        raise _fail("$.format", "unknown_format", f"must be '{WORKSPACE_FORMAT}'")
    version = data.get("version")
    if not isinstance(version, int) or isinstance(version, bool) or version < 1:
        raise _fail("$.version", "type", "must be a positive integer")
    if version > current_version:
        raise _fail(
            "$.version",
            "future_version",
            f"version {version} is newer than this dashboard supports ({current_version}); "
            "update the dashboard to open it",
        )
    document = copy.deepcopy(data)
    while version < current_version:
        step = migrations.get(version)
        if step is None:
            raise _fail("$.version", "unsupported_version", f"no upgrade from version {version}")
        document = step(copy.deepcopy(document))
        if not isinstance(document, dict):
            raise _fail("$", "type", f"upgrade from version {version} did not return an object")
        version += 1
        document["format"] = WORKSPACE_FORMAT
        document["version"] = version
    return document


def workspace_from_dict(
    data: object,
    *,
    migrations: Mapping[int, Migration] = MIGRATIONS,
    current_version: int = CURRENT_VERSION,
) -> Workspace:
    """Validate decoded JSON data and return an immutable workspace."""

    _check_bounds(data)
    document = migrate_document(data, migrations=migrations, current_version=current_version)
    _check_bounds(document)
    validator = _Validator()
    workspace = validator.workspace(document)
    if workspace is None:
        raise WorkspaceError(validator.issues)
    return workspace


def _layout_to_dict(node: LayoutNode) -> dict[str, Any]:
    if isinstance(node, WidgetRef):
        return {"kind": "widget", "widget": node.widget}
    children = [_layout_to_dict(child) for child in node.children]
    if isinstance(node, SplitLayout):
        result: dict[str, Any] = {
            "kind": "split",
            "orientation": node.orientation,
            "children": children,
        }
        if node.weights is not None:
            result["weights"] = list(node.weights)
        return result
    return {"kind": "tabs", "children": children, "current": node.current}


def _drop_none(data: dict[str, Any]) -> dict[str, Any]:
    return {key: value for key, value in data.items() if value is not None}


def workspace_to_dict(workspace: Workspace) -> dict[str, Any]:
    """Return plain JSON data for a workspace in the current format version."""

    widgets = []
    for widget in workspace.widgets:
        item = _drop_none({"id": widget.id, "type": widget.type, "title": widget.title})
        if widget.axes:
            item["axes"] = [
                _drop_none(
                    {
                        "id": axis.id,
                        "side": axis.side,
                        "label": axis.label,
                        "unit": axis.unit,
                        "range": list(axis.range) if axis.range is not None else None,
                    }
                )
                for axis in widget.axes
            ]
        item["bindings"] = [
            _drop_none(
                {
                    "signal": binding.signal,
                    "label": binding.label,
                    "color": binding.color,
                    "axis": binding.axis,
                }
            )
            for binding in widget.bindings
        ]
        widgets.append(item)
    document: dict[str, Any] = {
        "format": WORKSPACE_FORMAT,
        "version": CURRENT_VERSION,
        "name": workspace.name,
    }
    if workspace.description is not None:
        document["description"] = workspace.description
    document["widgets"] = widgets
    document["layout"] = _layout_to_dict(workspace.layout)
    if workspace.extensions:
        document["extensions"] = thaw_json(workspace.extensions)
    return document


def _reject_constant(name: str) -> float:
    raise ValueError(f"{name} is not allowed")


def _unique_object(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise ValueError(f"duplicate key '{key}'")
        result[key] = value
    return result


_JSON_STRING = re.compile(r'"(?:[^"\\]|\\.)*"', re.DOTALL)
_BRACKET = re.compile(r"[\[\]{}]")


def _nesting_depth(text: str) -> int:
    """Deepest bracket nesting outside string literals, computed without recursion.

    Checked before ``json.loads`` so pathological input is refused without
    relying on interpreter recursion limits, which differ by platform and
    Python version (and on Windows protect a smaller native stack).
    """

    depth = deepest = 0
    for match in _BRACKET.finditer(_JSON_STRING.sub("", text)):
        if match.group() in "[{":
            depth += 1
            deepest = max(deepest, depth)
        else:
            depth -= 1
    return deepest


def loads_workspace(text: str | bytes, **options: Any) -> Workspace:
    """Parse and validate workspace JSON text or UTF-8 bytes."""

    raw = text.encode("utf-8") if isinstance(text, str) else bytes(text)
    if len(raw) > MAX_DOCUMENT_BYTES:
        raise _fail("$", "too_large", f"workspace files are limited to {MAX_DOCUMENT_BYTES} bytes")
    try:
        decoded = raw.decode("utf-8-sig")
    except UnicodeDecodeError as error:
        raise _fail(
            "$", "decode", f"not UTF-8 text ({error.reason} at byte {error.start})"
        ) from None
    if _nesting_depth(decoded) > MAX_JSON_DEPTH:
        raise _fail("$", "too_deep", f"nesting is limited to {MAX_JSON_DEPTH} levels")
    try:
        data = json.loads(
            decoded, object_pairs_hook=_unique_object, parse_constant=_reject_constant
        )
    except RecursionError:  # pragma: no cover - the depth pre-check makes this unreachable
        raise _fail("$", "too_deep", f"nesting is limited to {MAX_JSON_DEPTH} levels") from None
    except json.JSONDecodeError as error:
        raise _fail(
            "$", "json", f"invalid JSON at line {error.lineno} column {error.colno}: {error.msg}"
        ) from None
    except ValueError as error:
        raise _fail("$", "json", str(error)) from None
    return workspace_from_dict(data, **options)


def dumps_workspace(workspace: Workspace) -> str:
    """Return deterministic, ASCII-only JSON for a valid workspace.

    The output is validated before it is returned, so a workspace built in code
    with invalid values can never be written out.
    """

    document = workspace_to_dict(workspace)
    workspace_from_dict(document)
    return json.dumps(document, indent=2, ensure_ascii=True, allow_nan=False) + "\n"
