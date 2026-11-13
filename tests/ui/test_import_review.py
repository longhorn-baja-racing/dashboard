from pathlib import Path
from typing import cast
from unittest.mock import Mock

import pytest

from lbr_dashboard.app import create_main_window
from lbr_dashboard.core.log import LogSession
from lbr_dashboard.dashboard_controller import DashboardController
from lbr_dashboard.ui.main_window import MainWindow


@pytest.fixture
def window(qtbot):
    widget = create_main_window()
    qtbot.addWidget(widget)
    widget.show()
    yield widget
    assert controller(widget).import_controller.close()
    widget.close()


def load(window, path, qtbot):
    controller(window).import_controller.start(path)
    qtbot.waitUntil(lambda: window.import_state != "loading")


def test_large_log_keeps_full_plot_and_bounds_only_table(window, qtbot, tmp_path):
    path = tmp_path / "large.csv"
    path.write_text(
        ",".join(["timestamp_ms"] + [f"signal{i}" for i in range(15)])
        + "\n"
        + "".join(",".join([str(i)] * 16) + "\n" for i in range(4000))
    )
    load(window, path, qtbot)
    assert window.import_state == "ready", window.import_message.text()
    trace = window.right_panel.plot_widget.getPlotItem().listDataItems()[0]
    assert len(trace.getData()[1]) == 4000 and trace.getData()[1][-1] == 3999
    assert window.right_panel.table.rowCount() <= 1000
    assert "4,000" in window.right_panel.table_summary.text()


@pytest.mark.parametrize("tail", ["\n\n", "20,3\n\n"])
def test_blank_lines_and_short_final_record_are_recoverable(window, qtbot, tmp_path, tail):
    path = tmp_path / "interrupted.csv"
    path.write_text("timestamp_ms,value,note\n0,1,start\n10,2,end\n" + tail)
    load(window, path, qtbot)
    assert window.import_state == "ready", window.import_message.text()
    assert controller(window)._log_store.numeric_column("value") == (1, 2)
    if tail.startswith("20"):
        assert "final row" in window.import_message.text()


def test_nonfinite_values_warn_but_text_and_zero_survive(window, qtbot, tmp_path):
    path = tmp_path / "values.csv"
    path.write_text("Timestamp_ms,value,note\n0,0,inf\n10,nan,ready\n20,3,done\n")
    load(window, path, qtbot)
    assert window.import_state == "ready", window.import_message.text()
    assert controller(window)._log_store.numeric_column("value") == (0, None, 3)
    assert window.right_panel.table.item(0, 2).text() == "inf"
    assert "non-finite" in window.import_message.text()
    assert "sample index" not in window.import_message.text()
    xs, ys = window.right_panel.plot_widget.getPlotItem().listDataItems()[0].getData()
    assert xs.tolist() == [0, 20] and ys.tolist() == [0, 3]


def test_each_load_plots_only_once_and_injected_importer_is_used(qtbot, tmp_path):
    class Importer:
        def __init__(self):
            self.calls = 0

        def import_session(self, path: Path, *, cancelled=lambda: False, progress=lambda n: None):
            self.calls += 1
            return LogSession.from_rows(("timestamp_ms", "custom"), (("0", "7"),))

    importer = Importer()
    window = MainWindow()
    window.attach_controller(DashboardController(window, importer))
    qtbot.addWidget(window)
    spy = Mock(wraps=window.right_panel.plot_column)
    window.right_panel.plot_column = spy
    window.load_csv(tmp_path / "ignored.csv")
    assert spy.call_count == 1
    spy.reset_mock()
    window.try_demo()
    assert spy.call_count == 1
    load(window, tmp_path / "ignored.csv", qtbot)
    assert importer.calls == 2
    assert controller(window)._log_store.numeric_column("custom") == (7,)
    window.close()


@pytest.mark.parametrize(
    ("content", "diagnostic"),
    [(b'timestamp_ms,value\n0,"unterminated', "CSV line 2"), (b"a,b\n0,\xff", "position")],
)
def test_copied_details_include_file_and_underlying_parser_error(
    window, qtbot, tmp_path, content, diagnostic
):
    from PySide6.QtWidgets import QApplication

    path = tmp_path / "bad.csv"
    path.write_bytes(content)
    load(window, path, qtbot)
    assert window.import_state == "error"
    assert path.name in window.import_message.text()
    window.copy_button.click()
    details = QApplication.clipboard().text()
    assert str(path) in details and diagnostic in details


def controller(window) -> DashboardController:
    return cast(DashboardController, window._controller)
