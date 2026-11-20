"""The Thresholds tool in the real window, on the CSV demo and the simulator."""

import json
from pathlib import Path

import pytest
from PySide6.QtCore import Qt
from PySide6.QtWidgets import QFileDialog

from lbr_dashboard.app import create_main_window
from lbr_dashboard.core.thresholds import (
    AlarmState,
    AlarmTransition,
    ThresholdRule,
    evaluate,
    rules_to_json,
    state_at,
)
from lbr_dashboard.source_controller import SourceController
from lbr_dashboard.ui.signal_browser import ID_ROLE
from lbr_dashboard.ui.tools.thresholds import ThresholdsTool

SAMPLES = Path(__file__).resolve().parents[2] / "sample_data"
DEMO = SAMPLES / "timestamped_demo.csv"
TEMPERATURE = "csv.column.1"
FLOW = "csv.column.2"
VALVE = "csv.column.3"
PRESSURE = "process.pressure"
PROCESS_TEMPERATURE = "process.temperature"
MS = 1_000_000
S = 1_000_000_000


def connect(qtbot, monkeypatch, key, *, wait_for_replay=True):
    window = create_main_window()
    window.show()
    owner = window.source_owner
    assert isinstance(owner, SourceController)
    owner.replay._now = lambda: 0
    monkeypatch.setattr(QFileDialog, "getOpenFileName", lambda *args: (str(DEMO), ""))
    picker = window.source_picker
    picker.choices.setCurrentIndex(picker.choices.findData(key))
    picker.connect_button.click()
    if wait_for_replay:
        qtbot.waitUntil(lambda: window.replay_bar.slider.isEnabled(), timeout=10_000)
    return window, owner


def close(qtbot, window):
    window.close()
    qtbot.waitUntil(lambda: not window.isVisible())
    window.deleteLater()


@pytest.fixture
def demo(qtbot, monkeypatch):
    window, owner = connect(qtbot, monkeypatch, "csv")
    yield window, owner
    close(qtbot, window)


@pytest.fixture
def simulator(qtbot, monkeypatch):
    window, owner = connect(qtbot, monkeypatch, "process")
    qtbot.waitUntil(
        lambda: window.analysis.bounds is not None and window.analysis.bounds.end_ns == 3 * S,
        timeout=10_000,
    )
    qtbot.waitUntil(lambda: owner._finished_rendered, timeout=10_000)
    yield window, owner
    close(qtbot, window)


def select(window, signal_id):
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


def thresholds(window) -> ThresholdsTool:
    dock = window.analysis_docks.open("thresholds")
    tool = dock.widget()
    assert isinstance(tool, ThresholdsTool)
    tool.refresh()
    return tool


def add(
    tool: ThresholdsTool,
    signal_id: str,
    comparison: str,
    level: float,
    *,
    hysteresis: float = 0.0,
    debounce_s: float = 0.0,
    validity_s: float = 0.0,
    label: str = "",
) -> ThresholdRule:
    """Fill the editor the way a user would and press Add rule."""

    tool.table.clearSelection()
    tool.signal.setCurrentIndex(tool.signal.findData(signal_id))
    tool.comparison.setCurrentIndex(tool.comparison.findData(comparison))
    tool.level.setValue(level)
    tool.hysteresis.setValue(hysteresis)
    tool.debounce.setValue(debounce_s)
    tool.validity.setValue(validity_s)
    tool.label.setText(label)
    count = len(tool.rules)
    tool.add_button.click()
    assert len(tool.rules) == count + 1, tool.message.text()
    return tool.rules[-1]


def seek(window, owner, time_ns: int) -> ThresholdsTool:
    owner.replay.clock.seek(time_ns)
    owner.replay.present()
    assert window.analysis.cursor_ns == time_ns
    tool = thresholds(window)
    assert tool.evaluation_ns == time_ns
    return tool


def state(tool: ThresholdsTool, rule: ThresholdRule) -> tuple[str, int | None]:
    status = tool.status(rule.rule_id)
    return status.state, status.since_ns


def test_menu_lists_the_tool_and_explains_an_empty_window(qtbot):
    window = create_main_window()
    qtbot.addWidget(window)
    labels = [action.text() for action in window.analysis_docks.menu.actions()]
    assert "Thresholds" in labels
    tool = thresholds(window)
    assert tool.hint.text() == "Load a session to evaluate rules."
    assert tool.signal.count() == 0
    tool.add_button.click()
    assert tool.rules == [] and "choose a numeric signal" in tool.message.text()


def test_csv_rule_activates_goes_unknown_on_the_gap_and_never_clears_to_normal(demo):
    window, owner = demo
    select(window, TEMPERATURE)
    tool = thresholds(window)
    # Only numeric signals are offered; the valve column is a string signal.
    assert tool.signal.findData(VALVE) < 0 and tool.signal.findData(FLOW) >= 0
    assert tool.signal.currentData() == TEMPERATURE
    rule = add(tool, TEMPERATURE, "above", 21.8, label="Chamber hot")
    assert rule.unit == window.analysis.descriptor(TEMPERATURE).canonical_unit or rule.unit == ""
    assert tool.transitions(rule) == (
        AlarmTransition(0, AlarmState.NORMAL, 21.5, "cleared"),
        AlarmTransition(100 * MS, AlarmState.ACTIVE, 22.0, "crossed"),
        AlarmTransition(250 * MS, AlarmState.UNKNOWN, None, "missing"),
        AlarmTransition(500 * MS, AlarmState.ACTIVE, 23.5, "crossed"),
    )

    assert state(seek(window, owner, 0), rule) == ("normal", 0)
    assert state(seek(window, owner, 99 * MS), rule) == ("normal", 0)
    tool = seek(window, owner, 100 * MS)
    assert state(tool, rule) == ("active", 100 * MS)
    row = tool.rows()[0]
    assert row[0] == "[!] ACTIVE - not acknowledged"
    assert row[1] == "0.100 s"
    assert row[2].startswith("Chamber hot: Above 21.8")
    state_item = tool.table.item(0, 0)
    assert state_item is not None and not state_item.icon().isNull()
    # The missing reading at 250 ms makes the rule unknown, not normal, until data returns.
    for time_ns in (250 * MS, 300 * MS, 499 * MS):
        tool = seek(window, owner, time_ns)
        assert state(tool, rule) == ("unknown", 250 * MS)
        assert tool.rows()[0][0] == "[?] Unknown"
        assert tool.rows()[0][5] == "missing value"
    assert state(seek(window, owner, 1000 * MS), rule) == ("active", 500 * MS)
    assert "Evaluated at the replay cursor, 1.000 s" in tool.hint.text()
    assert "nothing is sent to the source" in tool.hint.text()


def test_csv_seek_order_does_not_change_states(demo):
    window, owner = demo
    select(window, TEMPERATURE)
    tool = thresholds(window)
    rules = [
        add(tool, TEMPERATURE, "above", 21.8),
        add(tool, TEMPERATURE, "below", 23.0, hysteresis=1.0),
        add(tool, FLOW, "above", 13.0, validity_s=0.2),
    ]
    times = [0, 100 * MS, 180 * MS, 250 * MS, 400 * MS, 500 * MS, 750 * MS, 1000 * MS]

    def sweep(order):
        result = {}
        for time_ns in order:
            current = seek(window, owner, time_ns)
            result[time_ns] = [state(current, rule) for rule in rules]
        return result

    forward = sweep(times)
    assert sweep(list(reversed(times))) == forward
    assert sweep([500 * MS, 0, 1000 * MS, 250 * MS, 100 * MS, 750 * MS, 400 * MS, 180 * MS]) == (
        forward
    )
    # Every state equals evaluating only the samples up to that time.
    for rule in rules:
        times_ns, values = window.analysis.numeric_series(rule.signal_id)
        for index, time_ns in enumerate(times):
            count = sum(1 for item in times_ns if item <= time_ns)
            prefix = evaluate(rule, times_ns[:count], values[:count], end_ns=time_ns)
            shown = forward[time_ns][rules.index(rule)][0]
            assert shown.replace("stale", "unknown") == state_at(prefix, time_ns).value, (
                rule.rule_id,
                index,
            )
    # Flow is 14 at 250 ms and missing at 500 ms; the 0.2 s validity window of the
    # 250 ms reading ends first, so the rule is stale from 450 ms + 1 ns.
    assert forward[400 * MS][2] == ("active", 250 * MS)
    assert forward[500 * MS][2] == ("stale", 450 * MS + 1)


def test_acknowledgement_covers_one_activation_and_is_deterministic(demo):
    window, owner = demo
    select(window, TEMPERATURE)
    tool = thresholds(window)
    rule = add(tool, TEMPERATURE, "above", 21.8)
    tool.select_rule(rule.rule_id)
    tool = seek(window, owner, 0)
    assert not tool.acknowledge_button.isEnabled()
    assert not tool.acknowledge_selected()

    tool = seek(window, owner, 150 * MS)
    assert tool.acknowledge_button.isEnabled()
    tool.acknowledge_button.click()
    assert tool.status(rule.rule_id).acknowledged
    assert tool.rows()[0][0] == "[!] ACTIVE - acknowledged"
    assert not tool.acknowledge_button.isEnabled()
    assert tool.acknowledged == frozenset({(rule.rule_id, 100 * MS)})

    # The alarm comes back after the unknown gap: a new, unacknowledged activation.
    tool = seek(window, owner, 600 * MS)
    assert state(tool, rule) == ("active", 500 * MS)
    assert not tool.status(rule.rule_id).acknowledged
    assert tool.acknowledge_button.isEnabled()
    assert seek(window, owner, 200 * MS).status(rule.rule_id).acknowledged
    assert not seek(window, owner, 300 * MS).status(rule.rule_id).acknowledged

    # Editing the rule forgets its acknowledgements.
    tool.select_rule(rule.rule_id)
    tool.level.setValue(21.9)
    tool.update_button.click()
    assert tool.acknowledged == frozenset()
    assert not seek(window, owner, 150 * MS).status(rule.rule_id).acknowledged


def test_rule_levels_are_drawn_on_the_main_plot_and_survive_redraws(demo):
    window, _owner = demo
    select(window, TEMPERATURE)
    tool = thresholds(window)
    plot = window.right_panel.plot_widget
    rule = add(tool, TEMPERATURE, "above", 23.0, hysteresis=0.5, label="Hot")
    add(tool, FLOW, "above", 13.0)
    assert [line.value() for line in tool.lines] == [23.0, 22.5]
    assert all(line.scene() is plot.scene() for line in tool.lines)
    assert [line.toolTip() for line in tool.lines] == [
        f"Hot: above 23{' ' + rule.unit if rule.unit else ''}",
        f"Hot: clears below 22.5{' ' + rule.unit if rule.unit else ''}",
    ]

    # The plot is cleared when another signal is shown; only that signal's levels return.
    select(window, FLOW)
    window.source_owner.poll()
    tool.refresh()
    assert [line.value() for line in tool.lines] == [13.0]
    select(window, VALVE)
    window.source_owner.poll()
    tool.refresh()
    assert tool.lines == []
    select(window, TEMPERATURE)
    window.source_owner.poll()
    assert [line.value() for line in tool.lines] == [23.0, 22.5]
    assert all(line.scene() is plot.scene() for line in tool.lines)

    # A plain reset of the plot (as on a source change) is repaired on the next redraw.
    window.right_panel._reset_plot()
    assert all(line.scene() is None for line in tool.lines)
    window.source_owner._render()
    assert all(line.scene() is plot.scene() for line in tool.lines)

    tool.select_rule(rule.rule_id)
    tool.remove_button.click()
    assert tool.lines == []


def test_rules_round_trip_through_files_and_configuration(demo, tmp_path, monkeypatch):
    window, owner = demo
    select(window, TEMPERATURE)
    tool = thresholds(window)
    first = add(tool, TEMPERATURE, "above", 21.8, hysteresis=0.2, debounce_s=0.1, label="Hot")
    second = add(tool, FLOW, "below", 5.0, validity_s=0.3)
    assert second.stale_after_ns == 300 * MS and first.debounce_ns == 100 * MS

    target = tmp_path / "rules"
    monkeypatch.setattr(QFileDialog, "getSaveFileName", lambda *args: (str(target), ""))
    assert tool.export_rules()
    saved = tmp_path / "rules.json"
    text = saved.read_text(encoding="ascii")
    assert text == rules_to_json([first, second])
    assert "Exported 2 rule(s)" in tool.message.text()

    config = tool.configuration()
    assert config["format"] == "lbr-dashboard-thresholds"
    assert json.loads(json.dumps(config)) == config

    tool.set_rules([])
    assert tool.rules == [] and not tool.export_button.isEnabled()
    monkeypatch.setattr(QFileDialog, "getOpenFileName", lambda *args: (str(saved), ""))
    assert tool.import_rules()
    assert tool.rules == [first, second]
    assert "Imported 2 rule(s) from rules.json" in tool.message.text()
    # The 0.1 s debounce needs two readings past the level: 23.5 at 500 ms, 24 at 1000 ms.
    assert state(seek(window, owner, 150 * MS), first) == ("unknown", None)
    assert state(seek(window, owner, 1000 * MS), first) == ("active", 1000 * MS)

    broken = tmp_path / "broken.json"
    broken.write_text(text.replace('"level"', '"levle"', 1), encoding="ascii")
    monkeypatch.setattr(QFileDialog, "getOpenFileName", lambda *args: (str(broken), ""))
    assert not tool.import_rules()
    assert "rules[0].levle: unknown field" in tool.message.text()
    assert tool.rules == [first, second]

    other = thresholds(window)
    other.set_rules([])
    other.restore_configuration(config)
    assert other.rules == [first, second]
    with pytest.raises(ValueError, match="duplicate rule_id"):
        other.restore_configuration({**config, "rules": [config["rules"][0]] * 2})
    with pytest.raises(ValueError, match="JSON object"):
        other.restore_configuration([])
    assert other.rules == [first, second]


def test_imported_rule_for_a_missing_or_non_numeric_signal_stays_unknown(demo):
    window, owner = demo
    tool = thresholds(window)
    tool.set_rules(
        [
            ThresholdRule("gone", "not.here", "above", 1.0),
            ThresholdRule("text", VALVE, "above", 1.0),
        ]
    )
    tool = seek(window, owner, 500 * MS)
    gone, text = (tool.status(rule.rule_id) for rule in tool.rules)
    assert gone.state == text.state == "unknown"
    assert gone.detail == "signal is not in this session"
    assert "thresholds need a numeric signal" in text.detail
    assert tool.signal.findData("not.here") >= 0
    assert [row[0] for row in tool.rows()] == ["[?] Unknown", "[?] Unknown"]


def test_simulator_rules_activate_clear_go_stale_and_never_command_the_source(
    simulator, monkeypatch
):
    window, owner = simulator
    commands = []
    source = owner.source
    if source is not None:
        monkeypatch.setattr(source, "seek", lambda *args: commands.append(("seek", args)))
        monkeypatch.setattr(source, "set_rate", lambda *args: commands.append(("rate", args)))
    select(window, PRESSURE)
    tool = thresholds(window)
    # Pressure cycles 0, 5, 8, 3 every 10 ms, with lost records at 200-240 ms and
    # unavailable readings at 500-540 ms.
    pressure = add(tool, PRESSURE, "above", 6.0, hysteresis=1.0, validity_s=0.015)
    # Temperature is about 20.07, 21.00, 18.93 and 19.99 at 0, 1, 2 and 3 s.
    hot = add(tool, PROCESS_TEMPERATURE, "above", 20.5, hysteresis=1.0)
    cold = add(tool, PROCESS_TEMPERATURE, "below", 20.5, debounce_s=0.5)
    assert pressure.unit == "Pa"
    tool.set_rules([*tool.rules, ThresholdRule("kpa", PRESSURE, "above", 0.006, unit="kPa")])

    assert state(seek(window, owner, 10 * MS), pressure) == ("normal", 0)
    tool = seek(window, owner, 25 * MS)
    assert state(tool, pressure) == ("active", 20 * MS)
    assert tool.status("kpa").state == "unknown"
    assert tool.status("kpa").detail == "unit mismatch: rule uses 'kPa', signal reports 'Pa'"
    assert state(seek(window, owner, 30 * MS), pressure) == ("normal", 30 * MS)
    tool = seek(window, owner, 210 * MS)
    assert state(tool, pressure) == ("stale", 205 * MS + 1)
    assert tool.rows()[0][0] == "[~] Stale"
    assert "no sample after 0.190 s" in tool.status(pressure.rule_id).detail
    assert state(seek(window, owner, 250 * MS), pressure) == ("normal", 250 * MS)
    assert state(seek(window, owner, 520 * MS), pressure) == ("unknown", 500 * MS)
    assert state(seek(window, owner, 580 * MS), pressure) == ("active", 580 * MS)

    assert state(seek(window, owner, 500 * MS), hot) == ("normal", 0)
    assert state(seek(window, owner, 1500 * MS), hot) == ("active", 1 * S)
    assert state(seek(window, owner, 2 * S), hot) == ("normal", 2 * S)
    # Debounce: below 20.5 must hold for 0.5 s, which first happens at the 3 s sample.
    assert state(seek(window, owner, 2500 * MS), cold) == ("unknown", None)
    assert state(seek(window, owner, 3 * S), cold) == ("active", 3 * S)

    tool.select_rule(hot.rule_id)
    seek(window, owner, 1500 * MS).acknowledge_selected()
    assert tool.status(hot.rule_id).acknowledged
    assert commands == []


def test_live_evaluation_uses_the_newest_data(simulator):
    window, owner = simulator
    select(window, PRESSURE)
    tool = thresholds(window)
    rule = add(tool, PROCESS_TEMPERATURE, "above", 20.5)
    seek(window, owner, 1500 * MS)
    assert state(tool, rule) == ("active", 1 * S)
    # While the clock follows live data the cursor is not used.
    window.analysis.set_cursor(500 * MS, live=True)
    tool.refresh()
    assert tool.evaluation_ns == 3 * S
    assert state(tool, rule) == ("normal", 2 * S)
    assert tool.hint.text().startswith(
        "Following live data: evaluated at the newest sample, 3.000 s"
    )


def test_live_simulator_is_evaluated_at_the_newest_sample(qtbot, monkeypatch):
    window, owner = connect(qtbot, monkeypatch, "live", wait_for_replay=False)
    try:
        qtbot.waitUntil(lambda: window.analysis.following_live, timeout=10_000)
        select(window, PROCESS_TEMPERATURE)
        tool = thresholds(window)
        rule = add(tool, PROCESS_TEMPERATURE, "above", -1000.0)
        qtbot.waitUntil(lambda: tool.status(rule.rule_id).state == "active", timeout=10_000)
        bounds = window.analysis.bounds
        assert bounds is not None and tool.evaluation_ns == bounds.end_ns
        assert tool.hint.text().startswith("Following live data")
        assert [line.value() for line in tool.lines] == [-1000.0]
    finally:
        close(qtbot, window)
