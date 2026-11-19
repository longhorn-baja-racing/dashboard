"""Verify plot and native widget colors across persisted explicit themes."""

import pytest
from PySide6.QtGui import QColor, QPalette

from lbr_dashboard.app import create_main_window
from lbr_dashboard.core import PreferencesStore, SignalDescriptor, SignalSchema
from lbr_dashboard.preferences_controller import PreferencesController
from lbr_dashboard.source_controller import SourceController
from lbr_dashboard.ui.plot_style import CURSOR_COLOR, TRACE_COLOR


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


def test_theme_switch_updates_live_widgets_and_late_docks(qtbot, tmp_path):
    window = create_main_window(preferences_store=PreferencesStore(tmp_path / "prefs.json"))
    owner = window.source_owner
    controller = window.preferences_controller
    assert isinstance(owner, SourceController) and isinstance(controller, PreferencesController)
    path = tmp_path / "theme.csv"
    path.write_text("timestamp_ms,signal\n0,0\n10,2\n20,3\n", encoding="utf-8")
    window.show()
    try:
        controller.set_theme("light")
        owner.open_csv(path)
        qtbot.waitUntil(lambda: owner._finished_rendered and owner.replay.cursor is not None)
        workspace = window.workspace_docks
        dock = workspace.add("plot")
        dock.bind_selected()
        qtbot.waitUntil(lambda: dock.cursor is not None)
        assert dock.plot is not None and dock.cursor is not None
        browser = window.left_panel.signal_browser
        base = QColor("#ffffff")
        assert contrast(QColor(CURSOR_COLOR), base) >= 3
        assert dock.plot.backgroundBrush().color() == base
        assert dock.multi_plot is not None and dock.multi_plot.curves
        for item in dock.multi_plot.curves.values():
            assert contrast(item.opts["pen"].color(), base) >= 3

        controller.set_theme("dark")
        base = QColor("#202020")
        assert contrast(QColor(CURSOR_COLOR), base) >= 3
        assert dock.plot.backgroundBrush().color() == base
        curve = window.right_panel.time_plot.curve
        assert curve is not None
        assert dock.multi_plot is not None and dock.multi_plot.curves
        for item in dock.multi_plot.curves.values():
            assert contrast(item.opts["pen"].color(), base) >= 3

        descriptor = SignalDescriptor("bench.missing", "Missing", ("bench",), "numeric", "demo")
        browser.refresh(SignalSchema(1, (descriptor,)), {descriptor.identifier: (False, "Offline")})
        row = browser.model.item(0).child(0, 2)
        assert row.text() == "Unavailable"
        assert contrast(row.foreground().color(), base) >= 4.5
    finally:
        owner.disconnect()
        qtbot.waitUntil(lambda: owner.source is None and owner._preparing is None)
        window.close()
        window.deleteLater()
