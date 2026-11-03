"""Export a plot, with its axes, title and legend, to a PNG image."""

from __future__ import annotations

import os

from pyqtgraph import PlotWidget
from pyqtgraph.exporters import ImageExporter
from pyqtgraph.graphicsItems.PlotItem import PlotItem
from PySide6.QtCore import QBuffer, QByteArray, QIODevice
from PySide6.QtGui import QImage, QImageWriter

from ..core.export import ExportError, write_file_atomically


def export_plot_image(
    plot: PlotWidget | PlotItem,
    path: str | os.PathLike[str],
    *,
    width: int | None = None,
    overwrite: bool = False,
) -> tuple[int, int]:
    """Render ``plot`` to a PNG file at ``path`` and return its (width, height).

    The image shows exactly what the plot shows: axes, axis labels, title and
    legend come from the plot itself. ``width`` is in pixels; the height keeps
    the plot's aspect ratio. ``None`` uses the plot's current size.

    The file is written atomically. Raises ``ExportError`` if ``path`` exists
    and ``overwrite`` is False, or if rendering or writing fails.
    """

    item = plot.getPlotItem() if isinstance(plot, PlotWidget) else plot
    if width is not None and width <= 0:
        raise ValueError("Image width must be positive")
    exporter = ImageExporter(item)
    if width is not None:
        exporter.parameters()["width"] = width
    image = exporter.export(toBytes=True)
    if not isinstance(image, QImage) or image.isNull():
        raise ExportError("Could not render the plot image")

    buffer = QBuffer()
    buffer.open(QIODevice.OpenModeFlag.WriteOnly)
    writer = QImageWriter(buffer, QByteArray(b"png"))
    if not writer.write(image):
        raise ExportError(f"Could not encode the plot image as PNG: {writer.errorString()}")
    write_file_atomically(path, bytes(buffer.data().data()), overwrite=overwrite)
    return image.width(), image.height()
