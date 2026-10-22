"""Headless smoke tests for the application foundation."""

import os
from pathlib import Path

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from lbr_dashboard.app import create_application, create_main_window
from lbr_dashboard.core.log import LogSession
from lbr_dashboard.resources import resource_text
from lbr_dashboard.version import __version__


def test_application_shell_can_launch_and_close() -> None:
    application = create_application([])
    window = create_main_window()
    assert window.windowTitle() == "LBR Dashboard"
    assert window.centralWidget() is not None
    window.close()
    application.processEvents()


def test_version_is_available() -> None:
    assert __version__


def test_package_resource_lookup() -> None:
    assert "__version__" in resource_text("version.py")


def test_sample_csv_can_be_loaded() -> None:
    application = create_application([])
    window = create_main_window()
    sample = Path(__file__).parents[2] / "sample_data" / "sample_baja_log.csv"
    window.load_csv(sample)
    assert window.left_panel.list_widget.count() == 16
    assert window.right_panel.table.rowCount() == 10
    plot_item = window.right_panel.plot_widget.getPlotItem()
    assert plot_item is not None
    assert len(plot_item.listDataItems()) == 1
    assert not hasattr(window, "_log_store")
    assert hasattr(window, "_controller")
    window.close()
    application.processEvents()


def test_time_axis_is_normalized_to_zero() -> None:
    application = create_application([])
    window = create_main_window()
    log = LogSession.from_rows(
        ("timestamp_ms", "engine_rpm"),
        (("-20", "900"), ("-10", "950"), ("0", "1000")),
    )
    window.right_panel.set_table(log.headers, log.rows)
    window.right_panel.plot_column(
        "engine_rpm", log.numeric_column("engine_rpm"), log.numeric_column("timestamp_ms")
    )
    plot_item = window.right_panel.plot_widget.getPlotItem()
    assert plot_item is not None
    item = plot_item.listDataItems()[0]
    x_values, _ = item.getData()
    assert list(x_values) == [0.0, 10.0, 20.0]
    window.close()
    application.processEvents()
