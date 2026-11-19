"""Exercise the tutorial's own registration through the production picker."""

from math import isnan
from pathlib import Path
from runpy import run_path

from lbr_dashboard.core import SourceState
from lbr_dashboard.source_controller import SourceController

TESTS = Path(__file__).parents[1]


def test_tutorial_source_feeds_the_existing_plot(qtbot):
    tutorial = run_path(str(TESTS / "source_contract" / "tutorial.py"))
    window = tutorial["create_demo_window"](TESTS / "fixtures" / "tutorial-bench-log.txt")
    owner = window.source_owner
    assert isinstance(owner, SourceController)
    window.show()
    try:
        picker = window.source_picker
        index = picker.choices.findData("bench_log")
        assert index >= 0, "the tutorial source was not registered in the picker"
        picker.choices.setCurrentIndex(index)
        picker.connect_button.click()
        panel = window.right_panel
        qtbot.waitUntil(
            lambda: (
                owner.source is not None
                and owner.source.state is SourceState.ENDED
                and panel.table.rowCount() == 4
            )
        )
        plot = panel.plot_widget.getPlotItem()
        assert plot is not None
        items = plot.listDataItems()
        assert len(items) == 1
        times, values = items[0].getData()
        assert list(times) == [0, 0.01, 0.025, 0.04]
        assert [values[0], values[2], values[3]] == [0, 7, 11]
        assert isnan(values[1])
        missing = panel.table.item(1, 1)
        assert missing is not None and missing.text() == ""
    finally:
        owner.disconnect()
        qtbot.waitUntil(lambda: owner.source is None and owner._preparing is None)
        window.close()
        window.deleteLater()
