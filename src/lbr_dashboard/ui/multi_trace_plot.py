"""Two labeled scales on the existing windowed PyQtGraph time plot."""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass
from hashlib import blake2b
from html import escape
from re import fullmatch
from typing import Literal

import numpy as np
import pyqtgraph as pg
from pyqtgraph.graphicsItems.ViewBox.ViewBox import ViewBox

from ..core import SessionSnapshot, SignalDescriptor
from .time_plot import NANOSECONDS, TimePlot, render_points

Axis = Literal["left", "right"]
COLORS = ("#4fc3f7", "#ffb74d", "#ce93d8", "#81c784", "#ef9a9a", "#fff176", "#80cbc4", "#b0bec5")
# Darker counterparts drawn on light plot backgrounds so every trace keeps at
# least 3:1 contrast. Saved configurations keep the COLORS value.
LIGHT_COLORS: dict[str, str] = dict(
    zip(
        COLORS,
        ("#0277bd", "#c25e00", "#8e24aa", "#2e7d32", "#c62828", "#7c6a00", "#00796b", "#546e7a"),
        strict=True,
    )
)


@dataclass(frozen=True)
class TraceBinding:
    signal_id: str
    source_id: str
    unit: str | None
    axis: Axis
    color: str


def measured_unit(snapshot: SessionSnapshot, descriptor: SignalDescriptor) -> str | None:
    units = {
        record.sample.unit or descriptor.canonical_unit
        for record in snapshot.series[descriptor.identifier]
    }
    if descriptor.canonical_unit is not None:
        units.add(descriptor.canonical_unit)
    if len(units) > 1:
        raise ValueError("Readings have mixed or unspecified units. Choose a consistent signal.")
    return next(iter(units), descriptor.canonical_unit)


class MultiTracePlot(TimePlot):
    def __init__(self, widget: pg.PlotWidget) -> None:
        self.bindings: dict[str, TraceBinding] = {}
        self.curves: dict[str, pg.PlotDataItem] = {}
        self.snapshot: SessionSnapshot | None = None
        self.right_view: ViewBox | None = None
        self.has_origin = False
        self.problems: list[str] = []
        self._colors: dict[tuple[str, str], str] = {}
        self._light_background = False
        super().__init__(widget)
        self.legend = self.plot.addLegend(labelTextColor="#ffffff", brush=pg.mkBrush("#202020"))
        self.view.disableAutoRange()
        self.view.sigResized.connect(self._resize_right)
        self.plot.setLabel("bottom", "Measurement time since first reading", units="s")

    def _view_changed(self) -> None:
        if self.bindings and not self._setting_range:
            self.fitted = False
            self._fit_visible_y = True
            self._schedule()

    def add(self, descriptor: SignalDescriptor, axis: Axis | None = None) -> None:
        if self.snapshot is None:
            raise ValueError("Choose a source before adding a trace.")
        if descriptor.identifier in self.bindings:
            raise ValueError("That signal already has a trace.")
        if descriptor.data_type not in {"numeric", "boolean"} or descriptor.shape:
            raise ValueError("Choose a scalar numeric or boolean signal for a plot.")
        if axis not in (None, "left", "right"):
            raise ValueError("Choose Left axis or Right axis.")
        unit = measured_unit(self.snapshot, descriptor)
        if axis is None:
            axis = next((side for side in ("left", "right") if self._compatible(side, unit)), None)
        if axis is None or not self._compatible(axis, unit):
            raise ValueError(
                "Both axes are assigned. Use a compatible unit or another plot widget."
            )
        candidate = TraceBinding(
            descriptor.identifier, descriptor.source_id, unit, axis, self._color(descriptor)
        )
        self.bindings[descriptor.identifier] = candidate
        try:
            self._validate_timeline(self.bindings.values())
        except ValueError:
            del self.bindings[descriptor.identifier]
            raise
        self._rebuild_curves()
        self.fit()

    def display_color(self, color: str) -> str:
        """Return the color drawn for a binding on the current background."""
        return LIGHT_COLORS.get(color, color) if self._light_background else color

    def set_background(self, background: str) -> None:
        light = pg.mkColor(background).lightnessF() > 0.5
        if light != self._light_background:
            self._light_background = light
            self._rebuild_curves()
            self.refresh()

    def _color(self, descriptor: SignalDescriptor) -> str:
        identity = (descriptor.source_id, descriptor.identifier)
        if identity not in self._colors:
            digest = blake2b("\0".join(identity).encode(), digest_size=4).digest()
            start = int.from_bytes(digest, "big")
            used = set(self._colors.values())
            candidates = [COLORS[(start + i) % len(COLORS)] for i in range(len(COLORS))]
            candidates.extend(pg.intColor(i * 137, hues=360).name() for i in range(360))
            self._colors[identity] = next((c for c in candidates if c not in used), candidates[0])
        return self._colors[identity]

    def _compatible(self, axis: str, unit: str | None, excluding: str = "") -> bool:
        assigned = [
            b for b in self.bindings.values() if b.axis == axis and b.signal_id != excluding
        ]
        return not assigned or (unit is not None and all(b.unit == unit for b in assigned))

    def assign_axis(self, signal_id: str, axis: Axis) -> None:
        if axis not in ("left", "right"):
            raise ValueError("Choose Left axis or Right axis.")
        binding = self.bindings[signal_id]
        if not self._compatible(axis, binding.unit, signal_id):
            raise ValueError("This axis has a different or unspecified unit.")
        self.bindings[signal_id] = TraceBinding(
            binding.signal_id, binding.source_id, binding.unit, axis, binding.color
        )
        self._rebuild_curves()
        self.fit()

    def remove(self, signal_id: str) -> None:
        del self.bindings[signal_id]
        self._rebuild_curves()
        if not self.bindings:
            self.has_origin = False
        self.fit()

    def clear(self) -> None:
        super().clear()
        self.bindings.clear()
        self._rebuild_curves()
        self.has_origin = False

    def _rebuild_curves(self) -> None:
        visibility = {key: curve.isVisible() for key, curve in self.curves.items()}
        for curve in self.curves.values():
            assert self.legend is not None
            self.legend.removeItem(curve)
            view = self.plot.getViewBox() if curve.getViewBox() is self.view else self.right_view
            if view is not None:
                view.removeItem(curve)
        self.curves.clear()
        uses_right = any(b.axis == "right" for b in self.bindings.values())
        if uses_right and self.right_view is None:
            self.right_view = ViewBox()
            self.right_view.disableAutoRange()
            self.plot.scene().addItem(self.right_view)
            self.plot.showAxis("right")
            self.plot.getAxis("right").linkToView(self.right_view)
            self.right_view.setXLink(self.view)
            self._resize_right()
        if not uses_right and self.right_view is not None:
            self.plot.getAxis("right").unlinkFromView()
            self.plot.hideAxis("right")
            self.right_view.setXLink(None)
            self.plot.scene().removeItem(self.right_view)
            self.right_view.deleteLater()
            self.right_view = None
        for key, binding in self.bindings.items():
            curve = pg.PlotDataItem(pen=pg.mkPen(self.display_color(binding.color), width=2))
            view = self.view if binding.axis == "left" else self.right_view
            assert view is not None and self.legend is not None
            view.addItem(curve)
            curve.setVisible(visibility.get(key, True))
            self.curves[key] = curve
            self.legend.addItem(curve, escape(self.label(key)))
        for side in ("left", "right"):
            assigned = [b for b in self.bindings.values() if b.axis == side]
            axis = self.plot.getAxis(side)
            axis.enableAutoSIPrefix(False)
            unit = assigned[0].unit or "unit unspecified" if assigned else ""
            axis.setLabel(f"{side.title()} axis", units=escape(unit))

    def label(self, signal_id: str) -> str:
        binding = self.bindings[signal_id]
        descriptor = self._descriptor(binding)
        name = descriptor.display_name if descriptor is not None else signal_id
        return f"{name} [{binding.unit or 'unit unspecified'}] ({binding.axis}) — {signal_id}"

    def _resize_right(self) -> None:
        if self.right_view is not None:
            self.right_view.setGeometry(self.view.sceneBoundingRect())
            self.right_view.linkedViewChanged(self.view, self.right_view.XAxis)

    def _descriptor(self, binding: TraceBinding) -> SignalDescriptor | None:
        if self.snapshot is None:
            return None
        return next(
            (
                d
                for d in self.snapshot.schema.signals
                if d.identifier == binding.signal_id and d.source_id == binding.source_id
            ),
            None,
        )

    def _validate_timeline(self, bindings: Iterable[TraceBinding]) -> None:
        if self.snapshot is None:
            return
        references = {
            (d.source_id, self.snapshot.domains.get(d.identifier, d.timestamp_domain))
            for binding in bindings
            if (d := self._descriptor(binding)) is not None
        }
        if len(references) > 1:
            raise ValueError(
                "Signals need the same source and timestamp domain to share a time axis."
            )

    def update_snapshot(self, snapshot: SessionSnapshot | None) -> list[str]:
        self.snapshot = snapshot
        self._rebuild_curves()
        self.refresh()
        return self.problems

    def refresh(self) -> None:
        self.timer.stop()
        snapshot = self.snapshot
        problems: list[str] = []
        self.problems = problems
        active: list[TraceBinding] = []
        timeline_valid = True
        try:
            self._validate_timeline(self.bindings.values())
        except ValueError as error:
            problems.append(str(error))
            timeline_valid = False
        for binding in self.bindings.values():
            curve = self.curves[binding.signal_id]
            descriptor = self._descriptor(binding)
            try:
                if snapshot is None or descriptor is None:
                    raise ValueError(f"Missing signal: {binding.signal_id}")
                if descriptor.data_type not in {"numeric", "boolean"} or descriptor.shape:
                    raise ValueError(f"Unsupported signal type: {binding.signal_id}")
                if measured_unit(snapshot, descriptor) != binding.unit:
                    raise ValueError(f"Unit changed: {binding.signal_id}. Rebind the signal.")
                if not timeline_valid or not snapshot.series[binding.signal_id]:
                    curve.setData([], [])
                    continue
                active.append(binding)
            except ValueError as error:
                problems.append(str(error))
                curve.setData([], [])
        if not active or snapshot is None:
            return
        bounds = [snapshot.bounds(b.signal_id) for b in active]
        self.bounds = (
            min(b[0] for b in bounds if b is not None),
            max(b[1] for b in bounds if b is not None),
        )
        if not self.has_origin:
            self.origin_ns = self.bounds[0]
            self.has_origin = True
        values: dict[str, list[np.ndarray]] = {"left": [], "right": []}
        for binding in active:
            start, end = self.visible_bounds(self.bounds)
            records = (
                snapshot.range(binding.signal_id, start, end, neighbors=True)
                if start <= end
                else ()
            )
            xs, ys, connects = render_points(records, self.origin_ns)
            curve = self.curves[binding.signal_id]
            curve.setData(
                xs,
                ys,
                connect=connects,
                symbol="o",
                symbolSize=3,
                symbolBrush=self.display_color(binding.color),
            )
            finite = np.isfinite(ys)
            if not self.fitted:
                low, high = self.view.viewRange()[0]
                finite &= (xs >= low) & (xs <= high)
            if curve.isVisible():
                values[binding.axis].append(ys[finite])
        self._setting_range = True
        try:
            if self.fitted:
                left, right = ((t - self.origin_ns) / NANOSECONDS for t in self.bounds)
                self.view.setXRange(left, max(right, left + 0.001), padding=0.02)
            if self.fitted or self._fit_visible_y:
                for side, arrays in values.items():
                    valid = np.concatenate(arrays) if arrays else np.array([])
                    view = self.view if side == "left" else self.right_view
                    if valid.size and view is not None:
                        view.setYRange(float(valid.min()), float(valid.max()), padding=0.05)
                self._fit_visible_y = False
        finally:
            self._setting_range = False

    def configuration(self) -> list[dict[str, object]]:
        return [
            {
                "signal_id": b.signal_id,
                "source_id": b.source_id,
                "unit": b.unit,
                "axis": b.axis,
                "color": b.color,
                "visible": self.curves[b.signal_id].isVisible(),
            }
            for b in self.bindings.values()
        ]

    def restore_configuration(self, config: object) -> None:
        if not isinstance(config, list):
            raise ValueError("Trace configuration must be a list.")
        bindings: dict[str, TraceBinding] = {}
        for entry in config:
            if not isinstance(entry, dict) or set(entry) != {
                "signal_id",
                "source_id",
                "unit",
                "axis",
                "color",
                "visible",
            }:
                raise ValueError("Invalid trace configuration fields.")
            if (
                not all(
                    isinstance(entry[k], str) and entry[k].strip()
                    for k in ("signal_id", "source_id")
                )
                or entry["axis"] not in ("left", "right")
                or type(entry["visible"]) is not bool
                or not isinstance(entry["color"], str)
                or fullmatch(r"#[0-9a-fA-F]{6}", entry["color"]) is None
                or not (
                    entry["unit"] is None
                    or isinstance(entry["unit"], str)
                    and entry["unit"].strip()
                )
                or entry["signal_id"] in bindings
            ):
                raise ValueError("Invalid or duplicate trace binding.")
            binding = TraceBinding(
                entry["signal_id"], entry["source_id"], entry["unit"], entry["axis"], entry["color"]
            )
            assigned = [b for b in bindings.values() if b.axis == binding.axis]
            if assigned and (binding.unit is None or any(b.unit != binding.unit for b in assigned)):
                raise ValueError("An axis must contain signals with the same known unit.")
            bindings[binding.signal_id] = binding
        self._validate_timeline(bindings.values())
        self.clear()
        self.bindings = bindings
        self._colors.update({(b.source_id, b.signal_id): b.color for b in bindings.values()})
        self._rebuild_curves()
        for entry in config:
            self.curves[entry["signal_id"]].setVisible(entry["visible"])
        self.fit()
