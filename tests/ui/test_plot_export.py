"""Plot image export renders axes, title and legend to an atomically written PNG."""

from __future__ import annotations

from pathlib import Path

import pyqtgraph as pg
import pytest
from PySide6.QtGui import QImage
from pytestqt.qtbot import QtBot

from lbr_dashboard.core.export import ExportError
from lbr_dashboard.ui import plot_export
from lbr_dashboard.ui.plot_export import export_plot_image


@pytest.fixture
def plot(qtbot: QtBot) -> pg.PlotWidget:
    widget = pg.PlotWidget(title="Engine speed")
    qtbot.addWidget(widget)
    widget.resize(600, 400)
    widget.show()
    widget.setLabel("bottom", "Time", units="s")
    widget.setLabel("left", "Speed", units="1/min")
    widget.addLegend()
    widget.plot([0, 1, 2, 3], [900, 950, 1000, 980], pen="c", name="engine_rpm")
    widget.plot([0, 1.5, 3], [800, 820, 840], pen="m", name="secondary_rpm")
    return widget


def _distinct_colors(image: QImage) -> int:
    step = 7
    return len(
        {
            image.pixel(x, y)
            for x in range(0, image.width(), step)
            for y in range(0, image.height(), step)
        }
    )


def test_plot_with_labels_title_and_legend_exports_png_of_requested_width(
    plot: pg.PlotWidget, tmp_path: Path
) -> None:
    path = tmp_path / ("plot " + chr(0xE9) + ".png")
    size = export_plot_image(plot, path, width=800)

    image = QImage(str(path))
    assert not image.isNull()
    assert size == (image.width(), image.height())
    assert image.width() == 800
    assert image.height() > 0
    assert _distinct_colors(image) > 2  # not a blank canvas
    assert path.read_bytes().startswith(b"\x89PNG")
    assert [item.name for item in tmp_path.iterdir()] == [path.name]


def test_plot_item_and_default_width(plot: pg.PlotWidget, tmp_path: Path) -> None:
    item = plot.getPlotItem()
    assert item is not None
    width, height = export_plot_image(item, tmp_path / "item.png")
    assert width > 0
    assert height > 0


def test_existing_image_needs_overwrite(plot: pg.PlotWidget, tmp_path: Path) -> None:
    path = tmp_path / "plot.png"
    path.write_bytes(b"old")
    with pytest.raises(ExportError, match="already exists"):
        export_plot_image(plot, path, width=300)
    assert path.read_bytes() == b"old"

    assert export_plot_image(plot, path, width=300, overwrite=True)[0] == 300
    assert QImage(str(path)).width() == 300


def test_invalid_width_and_render_failures(
    plot: pg.PlotWidget, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = tmp_path / "plot.png"
    with pytest.raises(ValueError, match="positive"):
        export_plot_image(plot, path, width=0)

    monkeypatch.setattr(plot_export.ImageExporter, "export", lambda *_, **__: QImage())
    with pytest.raises(ExportError, match="render"):
        export_plot_image(plot, path)

    monkeypatch.undo()
    monkeypatch.setattr(plot_export.QImageWriter, "write", lambda *_: False)
    with pytest.raises(ExportError, match="encode"):
        export_plot_image(plot, path)
    assert not path.exists()
