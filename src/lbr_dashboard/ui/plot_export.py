"""Export a plot, with its axes, title and legend, to a PNG image."""

from __future__ import annotations

import os
from contextlib import suppress

from pyqtgraph import LegendItem, PlotWidget
from pyqtgraph.exporters import ImageExporter
from pyqtgraph.graphicsItems.PlotItem import PlotItem
from PySide6.QtCore import QBuffer, QByteArray, QIODevice
from PySide6.QtGui import QImage, QImageWriter
from PySide6.QtWidgets import QGraphicsWidget

from ..core.export import ExportError, write_file_atomically


def export_plot_image(
    plot: PlotWidget | PlotItem,
    path: str | os.PathLike[str],
    *,
    width: int | None = None,
    overwrite: bool = False,
    legend: bool = False,
) -> tuple[int, int]:
    """Render ``plot`` to a PNG file at ``path`` and return its (width, height).

    The image shows exactly what the plot shows: axes, axis labels, title and
    legend come from the plot itself. ``width`` is in pixels; the height keeps
    the plot's aspect ratio. ``None`` uses the plot's current size.

    With ``legend=True`` a plot without a legend gets one for the image only,
    listing its named data items; the plot on screen is left unchanged.

    The file is written atomically. Raises ``ExportError`` if ``path`` exists
    and ``overwrite`` is False, or if rendering or writing fails.
    """

    item = plot.getPlotItem() if isinstance(plot, PlotWidget) else plot
    assert item is not None
    if width is not None and width <= 0:
        raise ValueError("Image width must be positive")
    exporter = ImageExporter(item)
    if width is not None:
        exporter.parameters()["width"] = width
    added = _temporary_legend(item) if legend else None
    try:
        image = exporter.export(toBytes=True)
    finally:
        if added is not None:
            _remove_legend(added)
    if not isinstance(image, QImage) or image.isNull():
        raise ExportError("Could not render the plot image")

    buffer = QBuffer()
    buffer.open(QIODevice.OpenModeFlag.WriteOnly)
    writer = QImageWriter(buffer, QByteArray(b"png"))
    if not writer.write(image):
        raise ExportError(f"Could not encode the plot image as PNG: {writer.errorString()}")
    write_file_atomically(path, bytes(buffer.data().data()), overwrite=overwrite)
    return image.width(), image.height()


def _temporary_legend(item: PlotItem) -> LegendItem | None:
    """Add a legend for the named data items when the plot has none."""

    if item.legend is not None:
        return None
    named = [data for data in item.listDataItems() if data.name()]
    if not named:
        return None
    legend = LegendItem(offset=(30, 30))
    legend.setParentItem(item.getViewBox())
    for data in named:
        legend.addItem(data, data.name())
    return legend


def _remove_legend(legend: LegendItem) -> None:
    parent = legend.parentItem()
    scene = legend.scene()
    if scene is not None:
        scene.removeItem(legend)
    # The anchor follows the view box through its geometryChanged signal.
    follow = getattr(legend, "_GraphicsWidgetAnchor__geometryChanged", None)
    if isinstance(parent, QGraphicsWidget) and follow is not None:
        with suppress(RuntimeError, TypeError):
            parent.geometryChanged.disconnect(follow)
