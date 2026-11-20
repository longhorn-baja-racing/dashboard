"""Derived signals defined in the Analysis tool behave like recorded signals in the real app."""

import json
import math
from pathlib import Path

import pytest
from PySide6.QtCore import Qt
from PySide6.QtWidgets import QFileDialog

from lbr_dashboard.app import create_main_window
from lbr_dashboard.core.derived import DerivedSignalDefinition
from lbr_dashboard.core.derived_session import is_derived
from lbr_dashboard.source_controller import SourceController
from lbr_dashboard.ui.signal_browser import ID_ROLE
from lbr_dashboard.ui.tools.derived_signals import DerivedSignalsTool, default_id
from lbr_dashboard.ui.tools.statistics import StatisticsTool

SAMPLES = Path(__file__).resolve().parents[2] / "sample_data"
DEMO = SAMPLES / "timestamped_demo.csv"
TEMPERATURE = "csv.column.1"
FLOW = "csv.column.2"
VALVE = "csv.column.3"
MS = 1_000_000


def launch(qtbot, monkeypatch, key: str):
    window = create_main_window()
    window.show()
    owner = window.source_owner
    assert isinstance(owner, SourceController)
    owner.replay._now = lambda: 0
    monkeypatch.setattr(QFileDialog, "getOpenFileName", lambda *args: (str(DEMO), ""))
    connect(qtbot, window, key)
    return window, owner


def connect(qtbot, window, key: str) -> None:
    owner = window.source_owner
    previous = owner.session
    picker = window.source_picker
    picker.choices.setCurrentIndex(picker.choices.findData(key))
    picker.connect_button.click()
    qtbot.waitUntil(
        lambda: (
            owner.session is not None
            and owner.session is not previous
            and owner._finished_rendered
            and window.replay_bar.slider.isEnabled()
        ),
        timeout=10000,
    )


def close(qtbot, window) -> None:
    window.close()
    qtbot.waitUntil(lambda: not window.isVisible(), timeout=10000)
    window.deleteLater()


@pytest.fixture
def demo(qtbot, monkeypatch):
    window, owner = launch(qtbot, monkeypatch, "csv")
    yield window, owner
    close(qtbot, window)


@pytest.fixture
def process(qtbot, monkeypatch):
    window, owner = launch(qtbot, monkeypatch, "process")
    yield window, owner
    close(qtbot, window)


def tool(window) -> DerivedSignalsTool:
    widget = window.analysis_docks.open("derived_signals").widget()
    assert isinstance(widget, DerivedSignalsTool)
    return widget


def fill(
    widget: DerivedSignalsTool,
    name: str,
    expression: str,
    *,
    output_id: str | None = None,
    unit: str = "",
    reference: str | None = None,
    hold_ms: float = 0.0,
) -> None:
    widget.name.setText(name)
    widget.output_id.setText(output_id or default_id(name))
    widget.expression.setText(expression)
    widget.unit.setText(unit)
    widget.hold.setValue(hold_ms)
    widget.refresh()
    if reference is not None:
        widget.reference.setCurrentIndex(widget.reference.findData(reference))
        widget.refresh()


def select(window, signal_id: str) -> None:
    browser = window.left_panel.signal_browser
    browser.search.setText(signal_id)
    model = browser.tree.model()
    assert model is not None
    matches = model.match(
        model.index(0, 0),
        ID_ROLE,
        signal_id,
        2,
        Qt.MatchFlag.MatchExactly | Qt.MatchFlag.MatchRecursive,
    )
    assert len(matches) == 1
    browser.tree.setCurrentIndex(matches[0])


def cell(table, row: int, column: int) -> str:
    item = table.item(row, column)
    assert item is not None
    return item.text()


def raw_values(owner, signal_id: str):
    return [
        (r.sample.timestamp_ns, r.sample.value) for r in owner.session.snapshot().series[signal_id]
    ]


def test_flow_rate_times_two_is_previewed_applied_plotted_and_analysed(demo):
    window, owner = demo
    before = raw_values(owner, FLOW)
    widget = tool(window)
    assert widget.insert_combo.findData(FLOW) >= 0
    assert widget.insert_combo.findData(VALVE) < 0  # text columns cannot be inputs
    widget.name.setText("Flow rate x2")
    widget._name_edited("Flow rate x2")
    assert widget.output_id.text() == "derived.flow_rate_x2"
    widget.insert_combo.setCurrentIndex(widget.insert_combo.findData(FLOW))
    widget.insert_button.click()
    widget.expression.insert(" * 2")
    assert widget.expression.text() == f'sig("{FLOW}") * 2'
    widget.refresh()

    text = widget.preview_label.text()
    assert "5 samples, 1 missing, unit unknown, min 0, max 28" in text
    assert f"Timestamps of {FLOW}" in text and f"Input: {FLOW} - flow_rate" in text
    cells = [
        tuple(cell(widget.preview_table, row, col) for col in range(3))
        for row in range(widget.preview_table.rowCount())
    ]
    assert cells == [
        ("0.000", "0", ""),
        ("0.100", "24", ""),
        ("0.250", "28", ""),
        ("0.500", "", f"missing_input ({FLOW})"),
        ("1.000", "0", ""),
    ]
    assert window.analysis.descriptor("derived.flow_rate_x2") is None  # preview only
    assert widget.apply_button.isEnabled()
    widget.apply_button.click()

    descriptor = window.analysis.descriptor("derived.flow_rate_x2")
    assert is_derived(descriptor) and descriptor is not None
    assert descriptor.path == ("derived",) and descriptor.data_type == "numeric"
    assert tuple(descriptor.display["derived"]["inputs"]) == (FLOW,)
    assert cell(widget.applied, 0, 4) == "5 samples, 1 missing, unit unknown"

    select(window, "derived.flow_rate_x2")
    browser = window.left_panel.signal_browser
    assert browser.selected_id == "derived.flow_rate_x2"
    details = browser.details.text()
    assert f'Derived: sig("{FLOW}") * 2 | inputs: {FLOW}' in details
    assert f"timestamps of {FLOW}, other inputs held up to 0 ms" in details
    panel = window.right_panel
    assert panel.time_plot.identity == (owner.session, "derived.flow_rate_x2")
    assert panel.time_plot.curve is not None
    xs, ys = panel.time_plot.curve.getData()
    assert list(xs) == pytest.approx([0.0, 0.1, 0.25, 0.5, 1.0])
    assert [None if math.isnan(y) else y for y in ys] == [0, 24, 28, None, 0]

    window.analysis.selection.select(0, 1000 * MS)
    stats = window.analysis_docks.open("statistics").widget()
    assert isinstance(stats, StatisticsTool)
    stats.refresh()
    values = dict(stats.panel.rows())
    assert values["Samples"] == "4 valid, 1 missing, 0 invalid (5 in interval)"
    assert values["Mean"] == "13"

    # Recorded signals and the session schema are untouched.
    assert raw_values(owner, FLOW) == before
    assert "derived.flow_rate_x2" not in {d.identifier for d in owner.session.schema.signals}


def test_chamber_temperature_minus_twenty_keeps_the_missing_reading_missing(demo):
    window, owner = demo
    widget = tool(window)
    fill(widget, "Temperature offset", f'sig("{TEMPERATURE}") - 20')
    assert widget.apply()
    times, values = window.analysis.numeric_series("derived.temperature_offset")
    assert times == [0, 100 * MS, 250 * MS, 500 * MS, 1000 * MS]
    assert values == [1.5, 2.0, None, 3.5, 4.0]
    missing = window.analysis.snapshot.series["derived.temperature_offset"][2].sample
    assert not missing.available and missing.quality == f"missing: missing_input ({TEMPERATURE})"
    assert window.analysis.reading_at("derived.temperature_offset", 300 * MS) == (
        [250 * MS],
        [None],
    )
    assert owner.session.snapshot().series[TEMPERATURE][2].sample.value is None


@pytest.mark.parametrize(
    ("output_id", "expression", "unit", "message"),
    [
        ("derived.ghost", 'sig("csv.column.9") * 2', "", "unknown signal 'csv.column.9'"),
        ("derived.text", f'sig("{VALVE}") + 1', "", "is string; derived signals need"),
        ("derived.loop", 'sig("derived.loop") * 2', "", "cycle: derived.loop -> derived.loop"),
        ("derived.unsafe", '__import__("os")', "", "unknown function '__import__'"),
        ("derived.call", '__import__("os").system("echo")', "", "only calls to sig, abs"),
        ("derived.attr", f'sig("{FLOW}").real', "", "attribute access is not allowed"),
        ("derived.power", f'sig("{FLOW}") ** 2', "", "operator ** is not allowed"),
        ("derived.unit", f'sig("{FLOW}") * 2', "kPa", "cannot confirm declared unit"),
        ("derived.spaced", f'sig("{FLOW}") * 2', "deg C", "contains spaces"),
        ("Bad Id", f'sig("{FLOW}") * 2', "", "must use lowercase"),
        (FLOW, f'sig("{TEMPERATURE}") * 2', "", "already a signal of this source"),
    ],
)
def test_invalid_definitions_report_errors_and_cannot_be_applied(
    demo, output_id, expression, unit, message
):
    window, _ = demo
    widget = tool(window)
    fill(widget, "Candidate", expression, output_id=output_id, unit=unit)
    assert widget.preview_label.text().startswith("Cannot apply: ")
    assert message in widget.preview_label.text()
    assert not widget.apply_button.isEnabled()
    assert not widget.apply()
    assert window.analysis.derived.definitions == ()


def test_definitions_can_chain_and_a_loop_between_them_is_rejected(demo):
    window, _ = demo
    widget = tool(window)
    fill(widget, "Flow a", f'sig("{FLOW}") * 2')
    assert widget.apply()
    widget.new()
    fill(widget, "Flow b", 'sig("derived.flow_a") + 1')
    assert widget.apply()
    assert window.analysis.numeric_series("derived.flow_b")[1] == [1, 25, 29, None, 1]

    widget.applied.selectRow(0)
    widget.edit_button.click()
    assert widget.editing == "derived.flow_a"
    widget.expression.setText('sig("derived.flow_b") * 2')
    widget.refresh()
    assert "cycle: derived.flow_a -> derived.flow_b -> derived.flow_a" in (
        widget.preview_label.text()
    )
    assert not widget.apply()

    widget.expression.setText(f'sig("{FLOW}") * 3')
    assert widget.apply()
    assert window.analysis.numeric_series("derived.flow_b")[1] == [1, 37, 43, None, 1]
    assert [d.output_id for d in window.analysis.derived.definitions] == [
        "derived.flow_a",
        "derived.flow_b",
    ]

    widget.applied.selectRow(0)
    widget.remove_button.click()
    assert window.analysis.descriptor("derived.flow_a") is None
    status = window.analysis.derived.statuses["derived.flow_b"]
    assert not status.available and "unknown signal 'derived.flow_a'" in status.message


def test_definitions_survive_a_source_reload_and_restore_with_missing_inputs(qtbot, demo):
    window, owner = demo
    widget = tool(window)
    fill(widget, "Flow rate x2", f'sig("{FLOW}") * 2')
    assert widget.apply()
    config = json.loads(json.dumps(window.analysis.derived.configuration()))
    assert config["version"] == 1
    assert config["definitions"][0]["expression"] == f'sig("{FLOW}") * 2'

    first = owner.session
    connect(qtbot, window, "csv")
    assert owner.session is not first
    assert window.analysis.numeric_series("derived.flow_rate_x2")[1] == [0, 24, 28, None, 0]

    broken = dict(config["definitions"][0])
    broken.update(
        output_id="derived.lost",
        name="Lost input",
        expression='sig("csv.column.9") * 2',
        inputs=["csv.column.9"],
        reference_id="csv.column.9",
    )
    window.analysis.derived.restore_configuration(
        {"version": 1, "definitions": [config["definitions"][0], broken]}
    )
    descriptor = window.analysis.descriptor("derived.lost")
    assert descriptor is not None and not descriptor.availability.available
    assert window.analysis.numeric_series("derived.lost") == ([], [])
    browser = window.left_panel.signal_browser
    assert browser._availability["derived.lost"][0] is False
    assert "unknown signal 'csv.column.9'" in browser._availability["derived.lost"][1]
    widget.refresh()
    assert widget.applied.rowCount() == 2
    assert cell(widget.applied, 1, 4).startswith("unavailable: unknown signal")
    select(window, "derived.lost")
    assert window.right_panel.plot_widget.getPlotItem().titleLabel.text.endswith(
        "waiting for readings"
    )
    assert window.analysis.numeric_series("derived.flow_rate_x2")[1] == [0, 24, 28, None, 0]

    with pytest.raises(ValueError, match="unsupported derived signal configuration version"):
        window.analysis.derived.restore_configuration({"version": 2, "definitions": []})
    assert len(window.analysis.derived.definitions) == 2


def test_simulator_mixed_rate_expression_aligns_to_the_chosen_reference(process):
    window, owner = process
    snapshot = owner.session.snapshot()
    temperature = [r.sample.timestamp_ns for r in snapshot.series["process.temperature"]]
    pressure = [r.sample.timestamp_ns for r in snapshot.series["process.pressure"]]
    assert len(pressure) > 10 * len(temperature) > 0
    widget = tool(window)
    expression = 'sig("process.temperature") - sig("process.pressure") / 1000'
    fill(
        widget,
        "Slow mix",
        expression,
        reference="process.temperature",
        hold_ms=10,
    )
    text = widget.preview_label.text()
    assert f"{len(temperature)} samples" in text and "Timestamps of process.temperature" in text
    assert "held up to 10 ms" in text and "treated as unknown" in text
    assert widget.apply()
    times, values = window.analysis.numeric_series("derived.slow_mix")
    assert times == temperature
    assert any(value is not None for value in values)

    widget.new()
    fill(widget, "Fast mix", expression, reference="process.pressure", hold_ms=1000)
    assert widget.apply()
    times, values = window.analysis.numeric_series("derived.fast_mix")
    assert times == pressure
    assert sum(value is not None for value in values) > len(temperature)

    select(window, "derived.fast_mix")
    assert window.right_panel.time_plot.identity == (owner.session, "derived.fast_mix")
    owner.poll()
    assert window.right_panel.selected_reading.text() != "Selected: no aligned reading"


@pytest.mark.parametrize(
    ("expression", "unit", "message"),
    [
        (
            'sig("process.pressure") + sig("process.pressure") * sig("process.pressure")',
            "",
            "'+' needs identical units, got 'Pa' and 'Pa^2'",
        ),
        ('sig("process.pressure") / 1000', "kPa", "declared unit 'kPa'"),
        ('sig("process.status") + 1', "", "is status; derived signals need"),
    ],
)
def test_simulator_unit_errors_are_reported(process, expression, unit, message):
    window, _ = process
    widget = tool(window)
    fill(widget, "Pressure check", expression, unit=unit)
    assert message in widget.preview_label.text()
    assert not widget.apply_button.isEnabled()


def test_simulator_declared_unit_that_matches_is_kept(process):
    window, _ = process
    widget = tool(window)
    fill(
        widget, "Pressure squared", 'sig("process.pressure") * sig("process.pressure")', unit="Pa^2"
    )
    assert widget.apply()
    descriptor = window.analysis.descriptor("derived.pressure_squared")
    assert descriptor is not None and descriptor.canonical_unit == "Pa^2"


def test_live_source_evaluation_is_coalesced(qtbot, monkeypatch):
    window = create_main_window()
    window.show()
    owner = window.source_owner
    assert isinstance(owner, SourceController)
    window.analysis.derived.apply(
        DerivedSignalDefinition(
            output_id="derived.live_pressure",
            name="Live pressure",
            expression='sig("process.pressure") * 2',
        )
    )
    renders = []
    original = owner._render

    def counting(*args, **kwargs):
        renders.append(1)
        original(*args, **kwargs)

    monkeypatch.setattr(owner, "_render", counting)
    picker = window.source_picker
    picker.choices.setCurrentIndex(picker.choices.findData("live"))
    picker.connect_button.click()
    qtbot.waitUntil(lambda: len(renders) >= 30, timeout=10000)
    evaluations = window.analysis.derived.evaluations
    assert 1 <= evaluations < len(renders) / 2
    qtbot.waitUntil(lambda: window.analysis.derived.evaluations > evaluations, timeout=5000)
    assert window.analysis.numeric_series("derived.live_pressure")[0]
    close(qtbot, window)
