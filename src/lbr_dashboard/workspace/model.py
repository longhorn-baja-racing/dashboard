"""Immutable, Qt-free workspace model shared by the codec and future UI."""

from __future__ import annotations

from collections.abc import Iterable, Iterator, Mapping
from dataclasses import dataclass, field
from types import MappingProxyType
from typing import TypeAlias

JsonScalar: TypeAlias = None | bool | int | float | str
JsonValue: TypeAlias = JsonScalar | tuple["JsonValue", ...] | Mapping[str, "JsonValue"]

WORKSPACE_FORMAT = "lbr-dashboard-workspace"
CURRENT_VERSION = 1

#: Widget type IDs understood by version 1. Coordinate new names with the
#: widget owners (#37 docking, #38/#39 plots, #109 values, #110 timelines).
WIDGET_TYPES = frozenset({"time_plot", "value_cards", "event_timeline", "raw_table"})

#: Widget types whose bindings may reference a per-widget axis.
AXIS_WIDGET_TYPES = frozenset({"time_plot"})

AXIS_SIDES = frozenset({"left", "right"})
SPLIT_ORIENTATIONS = frozenset({"horizontal", "vertical"})


@dataclass(frozen=True, slots=True)
class WorkspaceIssue:
    """One structured problem, addressed by a JSON-style location."""

    location: str
    code: str
    message: str

    def __str__(self) -> str:
        return f"{self.location}: {self.message} [{self.code}]"


class WorkspaceError(ValueError):
    """Raised when a workspace cannot be decoded, validated, read or written.

    ``issues`` lists every problem found so a future import/preview UI can show
    all of them at once instead of failing on the first.
    """

    def __init__(self, issues: Iterable[WorkspaceIssue]) -> None:
        self.issues = tuple(issues)
        if not self.issues:
            raise ValueError("WorkspaceError requires at least one issue")
        super().__init__("; ".join(str(issue) for issue in self.issues))

    @property
    def codes(self) -> frozenset[str]:
        return frozenset(issue.code for issue in self.issues)


@dataclass(frozen=True, slots=True)
class AxisPreference:
    """Display preference for one value axis of a plot widget."""

    id: str
    side: str = "left"
    label: str | None = None
    unit: str | None = None
    range: tuple[float, float] | None = None


@dataclass(frozen=True, slots=True)
class SignalBinding:
    """Reference to a signal by its generic descriptor ID.

    The ID is opaque here. Whether it currently exists is decided at load time
    against the active signal registry; see :func:`resolve_bindings`.
    """

    signal: str
    label: str | None = None
    color: str | None = None
    axis: str | None = None


@dataclass(frozen=True, slots=True)
class WidgetConfig:
    """One widget instance with a stable ID and its signal bindings."""

    id: str
    type: str
    title: str | None = None
    bindings: tuple[SignalBinding, ...] = ()
    axes: tuple[AxisPreference, ...] = ()


@dataclass(frozen=True, slots=True)
class WidgetRef:
    """Layout leaf that places one widget."""

    widget: str


@dataclass(frozen=True, slots=True)
class SplitLayout:
    """Children laid out side by side or stacked, with optional relative weights."""

    orientation: str
    children: tuple[LayoutNode, ...]
    weights: tuple[float, ...] | None = None


@dataclass(frozen=True, slots=True)
class TabLayout:
    """Children shown as tabs; ``current`` is the selected tab index."""

    children: tuple[LayoutNode, ...]
    current: int = 0


LayoutNode: TypeAlias = WidgetRef | SplitLayout | TabLayout


def freeze_json(value: object) -> JsonValue:
    """Return a deeply immutable copy of already-validated JSON data."""

    if isinstance(value, Mapping):
        return MappingProxyType({str(key): freeze_json(item) for key, item in value.items()})
    if isinstance(value, (list, tuple)):
        return tuple(freeze_json(item) for item in value)
    if value is None or isinstance(value, (bool, int, float, str)):
        return value
    raise TypeError(f"not JSON data: {type(value).__name__}")


def thaw_json(value: JsonValue) -> object:
    """Return plain ``dict``/``list`` JSON data for serialization."""

    if isinstance(value, Mapping):
        return {key: thaw_json(item) for key, item in value.items()}
    if isinstance(value, tuple):
        return [thaw_json(item) for item in value]
    return value


@dataclass(frozen=True, slots=True)
class Workspace:
    """A portable, declarative dashboard workspace.

    It describes which widgets exist, how they are arranged and which signal IDs
    they show. It never contains telemetry samples, file paths, serial ports or
    credentials, and it holds no Qt objects.
    """

    name: str
    widgets: tuple[WidgetConfig, ...]
    layout: LayoutNode
    description: str | None = None
    extensions: Mapping[str, JsonValue] = field(default_factory=lambda: MappingProxyType({}))

    def __post_init__(self) -> None:
        frozen = freeze_json(dict(self.extensions))
        object.__setattr__(self, "extensions", frozen)

    def widget(self, widget_id: str) -> WidgetConfig | None:
        """Return the widget with this ID, if present."""

        return next((widget for widget in self.widgets if widget.id == widget_id), None)

    def signal_ids(self) -> tuple[str, ...]:
        """Return every bound signal ID once, in first-use order."""

        return tuple(dict.fromkeys(b.signal for w in self.widgets for b in w.bindings))


def iter_layout_refs(node: LayoutNode) -> Iterator[WidgetRef]:
    """Yield every widget reference in layout order."""

    stack: list[LayoutNode] = [node]
    while stack:
        current = stack.pop()
        if isinstance(current, WidgetRef):
            yield current
        else:
            stack.extend(reversed(current.children))


@dataclass(frozen=True, slots=True)
class BoundSignal:
    """Location of one binding inside a workspace."""

    widget_id: str
    binding_index: int
    signal: str


@dataclass(frozen=True, slots=True)
class BindingResolution:
    """Result of matching workspace bindings against currently available signals."""

    resolved: tuple[BoundSignal, ...]
    unresolved: tuple[BoundSignal, ...]

    @property
    def complete(self) -> bool:
        return not self.unresolved


def resolve_bindings(
    workspace: Workspace, available_signal_ids: Iterable[str]
) -> BindingResolution:
    """Split bindings into resolved and unresolved without changing the workspace.

    A missing signal (renamed channel, different log, absent source) stays in the
    workspace as an unresolved binding so the user can rebind it later.
    """

    available = frozenset(available_signal_ids)
    resolved: list[BoundSignal] = []
    unresolved: list[BoundSignal] = []
    for widget in workspace.widgets:
        for index, binding in enumerate(widget.bindings):
            bound = BoundSignal(widget.id, index, binding.signal)
            (resolved if binding.signal in available else unresolved).append(bound)
    return BindingResolution(tuple(resolved), tuple(unresolved))
