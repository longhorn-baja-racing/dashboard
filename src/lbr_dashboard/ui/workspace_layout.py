"""Convert main-window dock arrangements to and from portable layout trees.

Qt's own ``saveState`` blob stores pixel geometry for one screen setup. A
workspace instead records the arrangement: which edge of the window each dock
sits on, how docks are split side by side or stacked (with relative weights)
and which are grouped as tabs. That survives other monitors, DPI scaling and
operating systems.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass, field

import shiboken6
from PySide6.QtCore import QRect, Qt, QTimer
from PySide6.QtWidgets import QDockWidget, QMainWindow, QTabBar

from ..workspace import LayoutNode, SplitLayout, TabLayout, WidgetRef, iter_layout_refs

AREAS: dict[str, Qt.DockWidgetArea] = {
    "left": Qt.DockWidgetArea.LeftDockWidgetArea,
    "right": Qt.DockWidgetArea.RightDockWidgetArea,
    "top": Qt.DockWidgetArea.TopDockWidgetArea,
    "bottom": Qt.DockWidgetArea.BottomDockWidgetArea,
}
AREA_NAMES = {area: name for name, area in AREAS.items()}

#: Format limits (see docs/workspace-format.md).
MAX_CHILDREN = 16


@dataclass(frozen=True)
class DockPlacement:
    """Where one dock sits, as stored on its workspace widget."""

    area: str
    hidden: bool = False
    floating: bool = False


@dataclass
class CapturedLayout:
    layout: LayoutNode
    placements: dict[str, DockPlacement] = field(default_factory=dict)


def _pointer(dock: QDockWidget) -> int:
    return shiboken6.getCppPointer(dock)[0]


def _tab_groups(window: QMainWindow, docks: Sequence[QDockWidget]) -> list[tuple[list, int]]:
    """Return tabbed dock groups in tab order with the index of the shown tab."""

    # Membership comes from Qt's own answer; Qt may keep stale, unused tab bars.
    # A tab bar with exactly the same docks supplies the tab order and the
    # shown tab, which tabifiedDockWidgets() does not report.
    bars: dict[frozenset[int], tuple[list[int], int]] = {}
    for bar in window.findChildren(QTabBar):
        pointers = [bar.tabData(index) for index in range(bar.count())]
        if bar.count() >= 2 and all(isinstance(p, int) for p in pointers):
            bars.setdefault(frozenset(pointers), (pointers, bar.currentIndex()))
    groups: list[tuple[list, int]] = []
    grouped: set[int] = set()
    for dock in docks:
        if id(dock) in grouped:
            continue
        mates = [mate for mate in window.tabifiedDockWidgets(dock) if mate in docks]
        if not mates:
            continue
        members = [dock, *mates]
        grouped.update(id(member) for member in members)
        by_pointer = {_pointer(member): member for member in members}
        match = bars.get(frozenset(by_pointer))
        if match is not None:
            members = [by_pointer[pointer] for pointer in match[0]]
            current = max(match[1], 0)
        else:
            shown = [i for i, member in enumerate(members) if not member.visibleRegion().isEmpty()]
            current = shown[0] if shown else 0
        groups.append((members, current))
    return groups


def _chunk(kind: str, nodes: list[LayoutNode], weights: list[float] | None) -> LayoutNode:
    """Build a split or tab node, nesting when the format's child limit is hit."""

    if len(nodes) == 1:
        return nodes[0]
    if len(nodes) > MAX_CHILDREN:
        size = MAX_CHILDREN
        parts = [nodes[i : i + size] for i in range(0, len(nodes), size)]
        part_weights = (
            [weights[i : i + size] for i in range(0, len(nodes), size)] if weights else None
        )
        children = [
            _chunk(kind, part, part_weights[index] if part_weights else None)
            for index, part in enumerate(parts)
        ]
        totals = [sum(w) for w in part_weights] if part_weights else None
        return _chunk(kind, children, totals)
    if kind == "tabs":
        return TabLayout(tuple(nodes), 0)
    orientation = "horizontal" if kind == "horizontal" else "vertical"
    return SplitLayout(orientation, tuple(nodes), _normalize(weights))


def _normalize(weights: Sequence[float] | None) -> tuple[float, ...] | None:
    if not weights or any(weight <= 0 for weight in weights):
        return None
    total = sum(weights)
    rounded = tuple(max(round(weight / total, 3), 0.001) for weight in weights)
    return rounded if len(set(rounded)) > 1 else None


@dataclass
class _Block:
    node: LayoutNode
    rect: QRect


def _cut(blocks: list[_Block]) -> _Block:
    """Rebuild a split tree from dock rectangles by finding straight cuts."""

    if len(blocks) == 1:
        return blocks[0]
    for orientation in ("horizontal", "vertical"):

        def start(block: _Block) -> int:
            return block.rect.x() if orientation == "horizontal" else block.rect.y()

        def end(block: _Block) -> int:
            rect = block.rect
            return (
                rect.x() + rect.width() if orientation == "horizontal" else rect.y() + rect.height()
            )

        ordered = sorted(blocks, key=start)
        parts: list[list[_Block]] = [[ordered[0]]]
        reach = end(ordered[0])
        for block in ordered[1:]:
            if start(block) >= reach - 1:
                parts.append([block])
            else:
                parts[-1].append(block)
            reach = max(reach, end(block))
        if len(parts) > 1:
            children = [_cut(part) for part in parts]
            extents = [
                float(child.rect.width() if orientation == "horizontal" else child.rect.height())
                for child in children
            ]
            rect = QRect(children[0].rect)
            for child in children[1:]:
                rect = rect.united(child.rect)
            node = _chunk(orientation, [child.node for child in children], extents)
            return _Block(node, rect)
    # Overlapping rectangles (no usable geometry yet): stack them in order.
    ordered = sorted(blocks, key=lambda block: (block.rect.y(), block.rect.x()))
    rect = QRect(ordered[0].rect)
    for block in ordered[1:]:
        rect = rect.united(block.rect)
    return _Block(_chunk("vertical", [block.node for block in ordered], None), rect)


def capture_layout(
    window: QMainWindow, docks: Mapping[str, QDockWidget], default_areas: Mapping[str, str]
) -> CapturedLayout:
    """Describe how ``docks`` (by widget ID) are arranged in ``window``."""

    layout = window.layout()
    if layout is not None:
        layout.activate()  # Geometry must reflect docks moved since the last event.
    ids = {id(dock): key for key, dock in docks.items()}
    placements: dict[str, DockPlacement] = {}
    for key, dock in docks.items():
        area = AREA_NAMES.get(window.dockWidgetArea(dock), default_areas.get(key, "right"))
        placements[key] = DockPlacement(area, dock.isHidden(), dock.isFloating())
    docked = [
        dock
        for key, dock in docks.items()
        if not placements[key].hidden and not placements[key].floating
    ]
    blocks_by_area: dict[str, list[_Block]] = {name: [] for name in AREAS}
    seen: set[int] = set()
    for members, current in _tab_groups(window, docked):
        seen.update(id(member) for member in members)
        node = _chunk("tabs", [WidgetRef(ids[id(member)]) for member in members], None)
        if isinstance(node, TabLayout):
            node = TabLayout(node.children, min(current, len(node.children) - 1))
        shown = members[min(current, len(members) - 1)]
        area = placements[ids[id(members[0])]].area
        blocks_by_area[area].append(_Block(node, QRect(shown.geometry())))
    for dock in docked:
        if id(dock) not in seen:
            key = ids[id(dock)]
            blocks_by_area[placements[key].area].append(
                _Block(WidgetRef(key), QRect(dock.geometry()))
            )

    area_nodes: list[LayoutNode] = []
    for name in AREAS:
        nodes: list[LayoutNode] = []
        if blocks_by_area[name]:
            nodes.append(_cut(blocks_by_area[name]).node)
        # Hidden and floating docks keep their edge but no position within it.
        nodes.extend(
            WidgetRef(key)
            for key in docks
            if placements[key].area == name and (placements[key].hidden or placements[key].floating)
        )
        if nodes:
            stack = "vertical" if name in ("left", "right") else "horizontal"
            area_nodes.append(_chunk(stack, nodes, None))
    if not area_nodes:
        raise ValueError("There are no widgets to save.")
    return CapturedLayout(_chunk("vertical", area_nodes, None), placements)


def _leaves(node: LayoutNode, docks: Mapping[str, QDockWidget]) -> list[str]:
    return [ref.widget for ref in iter_layout_refs(node) if ref.widget in docks]


class _Placer:
    def __init__(
        self,
        window: QMainWindow,
        docks: Mapping[str, QDockWidget],
        areas: Mapping[str, Qt.DockWidgetArea],
    ) -> None:
        self.window = window
        self.docks = docks
        self.areas = areas
        self.resizes: list[tuple[list[QDockWidget], tuple[float, ...], Qt.Orientation]] = []
        self.current_tabs: list[QDockWidget] = []

    def place(self, node: LayoutNode) -> None:
        leaves = _leaves(node, self.docks)
        if not leaves:
            return
        areas = {self.areas[key] for key in leaves}
        if len(areas) > 1 and not isinstance(node, WidgetRef):
            for child in node.children:
                self.place(child)
            return
        self.window.addDockWidget(areas.pop(), self.docks[leaves[0]])
        self.arrange(node)

    def arrange(self, node: LayoutNode) -> None:
        if isinstance(node, WidgetRef):
            return
        children = [child for child in node.children if _leaves(child, self.docks)]
        if not children:
            return
        if isinstance(node, TabLayout):
            # Docks cannot split inside a tab, so nested nodes become tabs too.
            leaves = _leaves(node, self.docks)
            anchor = self.docks[leaves[0]]
            for key in leaves[1:]:
                self.window.tabifyDockWidget(anchor, self.docks[key])
            chosen = node.children[node.current] if node.current < len(node.children) else None
            chosen_leaves = _leaves(chosen, self.docks) if chosen is not None else []
            self.current_tabs.append(self.docks[(chosen_leaves or leaves)[0]])
            return
        orientation = (
            Qt.Orientation.Horizontal
            if node.orientation == "horizontal"
            else Qt.Orientation.Vertical
        )
        firsts = [self.docks[_leaves(child, self.docks)[0]] for child in children]
        for previous, following in zip(firsts, firsts[1:]):
            self.window.splitDockWidget(previous, following, orientation)
        for child in children:
            self.arrange(child)
        if node.weights is not None and len(children) == len(node.children) > 1:
            self.resizes.append((firsts, node.weights, orientation))


def _apply_sizes(
    window: QMainWindow,
    resizes: Iterable[tuple[list[QDockWidget], tuple[float, ...], Qt.Orientation]],
) -> None:
    if not shiboken6.isValid(window):
        return
    for docks, weights, orientation in resizes:
        if not all(shiboken6.isValid(dock) for dock in docks):
            continue
        horizontal = orientation == Qt.Orientation.Horizontal
        total = sum(dock.width() if horizontal else dock.height() for dock in docks)
        if total <= len(docks):
            total = window.width() if horizontal else window.height()
        scale = total / sum(weights)
        window.resizeDocks(docks, [max(1, round(w * scale)) for w in weights], orientation)


def place_layout(
    window: QMainWindow,
    layout: LayoutNode,
    docks: Mapping[str, QDockWidget],
    placements: Mapping[str, DockPlacement],
) -> None:
    """Arrange ``docks`` as ``layout`` describes; unknown IDs are skipped."""

    areas = {
        key: AREAS.get(placements[key].area, Qt.DockWidgetArea.RightDockWidgetArea) for key in docks
    }
    placer = _Placer(window, docks, areas)
    placer.place(layout)
    for key, dock in docks.items():
        placement = placements[key]
        if placement.floating:
            dock.setFloating(True)
        if placement.hidden:
            dock.hide()
        else:
            dock.show()
    for dock in placer.current_tabs:
        dock.raise_()
    _apply_sizes(window, placer.resizes)
    # Sizes settle once the window has laid out; apply them again then.
    QTimer.singleShot(0, lambda: _apply_sizes(window, placer.resizes))
