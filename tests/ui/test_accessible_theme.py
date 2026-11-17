"""Verify plot and native widget colors across persisted explicit themes."""

import pytest
from PySide6.QtGui import QColor, QPalette

from lbr_dashboard.app import create_main_window
from lbr_dashboard.core import PreferencesStore
from lbr_dashboard.preferences_controller import PreferencesController
from lbr_dashboard.ui.right_panel.right_panel import TRACE_COLOR


def contrast(first, second):
    def luminance(color):
        rgb = (color.redF(), color.greenF(), color.blueF())
        linear = (v / 12.92 if v <= 0.04045 else ((v + 0.055) / 1.055) ** 2.4 for v in rgb)
        return sum(weight * value for weight, value in zip((0.2126, 0.7152, 0.0722), linear))

    low, high = sorted((luminance(first), luminance(second)))
    return (high + 0.05) / (low + 0.05)


@pytest.mark.parametrize("theme", ["light", "dark"])
def test_saved_theme_has_readable_palette_and_existing_trace(qtbot, tmp_path, theme):
    path = tmp_path / "preferences.json"
    window = create_main_window(preferences_store=PreferencesStore(path))
    qtbot.addWidget(window)
    controller = window.preferences_controller
    assert isinstance(controller, PreferencesController)
    controller.set_theme(theme)
    window.close()
    restored = create_main_window(preferences_store=PreferencesStore(path))
    qtbot.addWidget(restored)

    palette = restored.palette()
    role = QPalette.ColorRole
    base = palette.color(role.Base)
    text = palette.color(role.Text)
    accent = palette.color(role.Highlight)
    assert contrast(text, base) >= 4.5
    assert contrast(accent, palette.color(role.HighlightedText)) >= 4.5
    assert contrast(accent, palette.color(role.Window)) >= 3
    assert (
        contrast(
            palette.color(QPalette.ColorGroup.Disabled, role.ButtonText), palette.color(role.Button)
        )
        >= 4.5
    )

    panel = restored.right_panel
    panel.plot_column("Pressure", (0, 7), None)
    plot = panel.plot_widget.getPlotItem()
    assert plot is not None
    curve = plot.listDataItems()[0]
    assert curve.opts["pen"].color() == QColor(TRACE_COLOR)
    assert contrast(curve.opts["pen"].color(), base) >= 3
    assert plot.getAxis("left").labelStyle["color"] == text.name()
    new_theme = "light" if theme == "dark" else "dark"
    controller = restored.preferences_controller
    assert isinstance(controller, PreferencesController)
    controller.set_theme(new_theme)
    assert plot.listDataItems()[0] is curve
    assert curve.opts["pen"].color() == QColor(TRACE_COLOR)
