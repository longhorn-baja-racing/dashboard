"""Threshold rules, crossing events, staleness, seeking, and saved definitions."""

import json
import math
import random
from array import array
from bisect import bisect_right
from dataclasses import FrozenInstanceError, replace

import pytest

from lbr_dashboard.core.thresholds import (
    MAX_THRESHOLD_FILE_CHARS,
    REASON_CLEARED,
    REASON_CROSSED,
    REASON_MISSING,
    REASON_STALE,
    AlarmState,
    AlarmTransition,
    ThresholdFileError,
    ThresholdRule,
    ThresholdRuleError,
    acknowledge,
    evaluate,
    is_acknowledged,
    rules_from_json,
    rules_to_json,
    state_at,
    transition_at,
)

NORMAL = AlarmState.NORMAL
ACTIVE = AlarmState.ACTIVE
UNKNOWN = AlarmState.UNKNOWN
MS = 1_000_000
DEGREE_C = chr(0xB0) + "C"

# Two unrelated signal identities: nothing in the evaluator depends on either.
PRESSURE = ThresholdRule("pressure-high", "line_pressure", "above", 250.0, unit="kPa")
TEMPERATURE = ThresholdRule("temp-low", "coolant_temp", "below", 5.0, unit="deg C")


def states(transitions: tuple[AlarmTransition, ...]) -> list[tuple[int, AlarmState]]:
    return [(item.time_ns, item.state) for item in transitions]


def final_state(transitions: tuple[AlarmTransition, ...]) -> AlarmState:
    return transitions[-1].state if transitions else UNKNOWN


# Rule validation and description


def test_rule_defaults_and_conversion() -> None:
    rule = ThresholdRule("r", "s", "above", 95)
    assert rule.level == 95.0 and isinstance(rule.level, float)
    assert rule.hysteresis == 0.0 and isinstance(rule.hysteresis, float)
    assert (rule.debounce_ns, rule.severity, rule.stale_after_ns) == (0, "warning", None)
    assert (rule.label, rule.unit) == ("", "")
    with pytest.raises(FrozenInstanceError):
        rule.level = 1.0  # type: ignore[misc]


@pytest.mark.parametrize(
    ("changes", "field"),
    [
        ({"rule_id": ""}, "rule_id"),
        ({"rule_id": " r"}, "rule_id"),
        ({"rule_id": 3}, "rule_id"),
        ({"signal_id": ""}, "signal_id"),
        ({"comparison": "ABOVE"}, "comparison"),
        ({"comparison": ["above"]}, "comparison"),
        ({"level": math.nan}, "level"),
        ({"level": math.inf}, "level"),
        ({"level": "95"}, "level"),
        ({"level": True}, "level"),
        ({"level": 10**400}, "level"),
        ({"hysteresis": -0.1}, "hysteresis"),
        ({"hysteresis": math.inf}, "hysteresis"),
        ({"debounce_ns": -1}, "debounce_ns"),
        ({"debounce_ns": 1.5}, "debounce_ns"),
        ({"debounce_ns": True}, "debounce_ns"),
        ({"severity": "fatal"}, "severity"),
        ({"stale_after_ns": 0}, "stale_after_ns"),
        ({"stale_after_ns": 1.0}, "stale_after_ns"),
        ({"label": None}, "label"),
        ({"unit": 1}, "unit"),
    ],
)
def test_rule_rejects_invalid_fields(changes: dict[str, object], field: str) -> None:
    arguments: dict[str, object] = {
        "rule_id": "r",
        "signal_id": "s",
        "comparison": "above",
        "level": 1.0,
    }
    arguments.update(changes)
    with pytest.raises(ThresholdRuleError) as caught:
        ThresholdRule(**arguments)  # type: ignore[arg-type]
    assert caught.value.field == field
    assert str(caught.value).startswith(f"{field}: ")


def test_describe_is_plain_ascii_sentence() -> None:
    rule = ThresholdRule("r", "s", "above", 95, hysteresis=2, debounce_ns=500 * MS)
    assert rule.describe() == "Above 95 for 0.5 s, clears below 93 for 0.5 s"
    assert PRESSURE.describe() == "Above 250 kPa, clears below 250 kPa"
    below = replace(TEMPERATURE, hysteresis=0.25, stale_after_ns=2_000 * MS)
    assert below.describe() == (
        "Below 5 deg C, clears above 5.25 deg C, unknown after 2 s without data"
    )
    assert ThresholdRule("r", "s", "below", 1e20).describe().startswith("Below 1e+20,")
    assert below.describe().isascii()


# Crossing and hysteresis


def test_above_is_strict_at_level_and_clear_level() -> None:
    rule = replace(PRESSURE, hysteresis=10.0)
    times = [0, 1, 2, 3, 4, 5, 6]
    values = [250.0, 250.5, 240.0, 245.0, 239.9, 250.0, 251.0]
    assert states(evaluate(rule, times, values)) == [
        (0, NORMAL),
        (1, ACTIVE),
        (4, NORMAL),
        (6, ACTIVE),
    ]


def test_hysteresis_band_keeps_current_state() -> None:
    rule = replace(PRESSURE, hysteresis=10.0)
    values = [200.0, 245.0, 260.0, 245.0, 241.0, 260.0]
    transitions = evaluate(rule, range(6), values)
    assert states(transitions) == [(0, NORMAL), (2, ACTIVE)]
    assert transitions[1] == AlarmTransition(2, ACTIVE, 260.0, REASON_CROSSED)


def test_zero_hysteresis_holds_state_exactly_at_level() -> None:
    values = [251.0, 250.0, 249.0, 250.0, 250.0]
    assert states(evaluate(PRESSURE, range(5), values)) == [(0, ACTIVE), (2, NORMAL)]


def test_below_mode_mirrors_above_mode() -> None:
    rule = replace(TEMPERATURE, hysteresis=1.0)
    values = [5.0, 4.5, 5.5, 6.0, 6.5, 5.0, 4.0]
    transitions = evaluate(rule, range(7), values)
    assert states(transitions) == [(0, NORMAL), (1, ACTIVE), (4, NORMAL), (6, ACTIVE)]
    assert transitions[2].reason == REASON_CLEARED
    mirrored = replace(PRESSURE, level=-5.0, hysteresis=1.0)
    assert states(evaluate(mirrored, range(7), [-v for v in values])) == states(transitions)


def test_both_signal_identities_evaluate_from_importer_arrays() -> None:
    times = array("q", [0, 10 * MS, 20 * MS, 30 * MS])
    pressure = array("d", [200.0, 260.0, 270.0, 100.0])
    temperature = array("d", [20.0, 4.0, math.nan, 20.0])
    assert states(evaluate(PRESSURE, times, pressure)) == [
        (0, NORMAL),
        (10 * MS, ACTIVE),
        (30 * MS, NORMAL),
    ]
    assert states(evaluate(TEMPERATURE, times, temperature)) == [
        (0, NORMAL),
        (10 * MS, ACTIVE),
        (20 * MS, UNKNOWN),
        (30 * MS, NORMAL),
    ]


def test_integer_values_and_negative_times_are_accepted() -> None:
    assert states(evaluate(PRESSURE, [-5, 0], [300, 0])) == [(-5, ACTIVE), (0, NORMAL)]


# Debounce


def test_debounce_held_for_exactly_debounce_ns_activates_at_completion() -> None:
    rule = replace(PRESSURE, debounce_ns=100)
    values = [0.0, 0.0, 300.0, 300.0, 300.0]
    assert states(evaluate(rule, [0, 100, 150, 200, 249], values)) == [(100, NORMAL)]
    assert states(evaluate(rule, [0, 100, 150, 200, 250], values)) == [
        (100, NORMAL),
        (250, ACTIVE),
    ]
    transitions = evaluate(rule, [0, 100, 200], [300.0, 300.0, 300.0])
    assert states(transitions) == [(100, ACTIVE)]
    assert transitions[0].value == 300.0


def test_debounce_interrupted_by_normal_or_band_value_restarts() -> None:
    rule = replace(PRESSURE, debounce_ns=100, hysteresis=20.0)
    times = [0, 100, 150, 200, 250, 300, 350]
    values = [0.0, 0.0, 300.0, 240.0, 300.0, 300.0, 300.0]
    assert states(evaluate(rule, times, values)) == [(100, NORMAL), (350, ACTIVE)]


def test_debounce_applies_to_clearing() -> None:
    rule = replace(PRESSURE, debounce_ns=10)
    times = [0, 10, 20, 25, 30, 40]
    values = [300.0, 300.0, 0.0, 300.0, 0.0, 0.0]
    assert states(evaluate(rule, times, values)) == [(10, ACTIVE), (40, NORMAL)]


def test_sparse_samples_complete_debounce_at_sample_time() -> None:
    rule = replace(PRESSURE, debounce_ns=100)
    assert states(evaluate(rule, [0, 1_000], [300.0, 300.0])) == [(1_000, ACTIVE)]


def test_missing_value_resets_pending_debounce() -> None:
    rule = replace(PRESSURE, debounce_ns=100)
    times = [0, 100, 150, 200, 250, 300, 350]
    values = [0.0, 0.0, 300.0, None, 300.0, 300.0, 300.0]
    assert states(evaluate(rule, times, values)) == [
        (100, NORMAL),
        (200, UNKNOWN),
        (350, ACTIVE),
    ]


def test_debounce_applies_when_leaving_unknown() -> None:
    rule = replace(PRESSURE, debounce_ns=100)
    assert states(evaluate(rule, [0, 50, 99], [0.0, 0.0, 0.0])) == []
    assert states(evaluate(rule, [0, 50, 100], [0.0, 0.0, 0.0])) == [(100, NORMAL)]


# Missing and stale input


@pytest.mark.parametrize("missing", [None, math.nan, math.inf, -math.inf])
def test_missing_value_is_unknown_never_normal(missing: float | None) -> None:
    transitions = evaluate(PRESSURE, [0, 1, 2], [300.0, missing, 300.0])
    assert states(transitions) == [(0, ACTIVE), (1, UNKNOWN), (2, ACTIVE)]
    assert transitions[1] == AlarmTransition(1, UNKNOWN, None, REASON_MISSING)


def test_only_missing_values_stay_unknown() -> None:
    assert evaluate(PRESSURE, [0, 1, 2], [None, math.nan, None]) == ()
    assert evaluate(PRESSURE, [], []) == ()
    assert evaluate(PRESSURE, [], [], end_ns=10) == ()


def test_value_inside_band_after_unknown_compares_against_level_only() -> None:
    rule = replace(PRESSURE, hysteresis=10.0)
    values = [300.0, None, 245.0, 300.0, None, 250.0]
    assert states(evaluate(rule, range(6), values)) == [
        (0, ACTIVE),
        (1, UNKNOWN),
        (2, NORMAL),
        (3, ACTIVE),
        (4, UNKNOWN),
        (5, NORMAL),
    ]


def test_stale_gap_becomes_unknown_just_after_validity_window() -> None:
    rule = replace(PRESSURE, stale_after_ns=100)
    transitions = evaluate(rule, [0, 100, 300, 350], [300.0, 300.0, 300.0, 0.0])
    assert states(transitions) == [(0, ACTIVE), (201, UNKNOWN), (300, ACTIVE), (350, NORMAL)]
    assert transitions[1] == AlarmTransition(201, UNKNOWN, None, REASON_STALE)
    assert state_at(transitions, 200) is ACTIVE
    assert state_at(transitions, 201) is UNKNOWN


def test_gap_exactly_stale_after_is_still_fresh() -> None:
    rule = replace(PRESSURE, stale_after_ns=100)
    assert states(evaluate(rule, [0, 100, 200], [300.0] * 3, end_ns=300)) == [(0, ACTIVE)]


def test_stale_gap_resets_pending_debounce() -> None:
    rule = replace(PRESSURE, stale_after_ns=100, debounce_ns=150)
    times = [0, 100, 150, 200, 300, 450, 500, 550, 600]
    values = [0.0, 0.0, 0.0, 300.0, 300.0, 300.0, 300.0, 300.0, 300.0]
    assert states(evaluate(rule, times, values)) == [
        (150, NORMAL),
        (401, UNKNOWN),
        (600, ACTIVE),
    ]


def test_stale_gap_while_already_unknown_adds_no_transition() -> None:
    rule = replace(PRESSURE, stale_after_ns=10)
    assert states(evaluate(rule, [0, 1, 50], [300.0, None, 300.0])) == [
        (0, ACTIVE),
        (1, UNKNOWN),
        (50, ACTIVE),
    ]


def test_stale_without_debounce_resumes_on_next_valid_sample() -> None:
    rule = replace(TEMPERATURE, stale_after_ns=10)
    # The first stale nanosecond coincides with a fresh sample: no gap remains to report.
    assert states(evaluate(rule, [0, 11], [1.0, 1.0])) == [(0, ACTIVE)]
    assert states(evaluate(rule, [0, 12], [1.0, 1.0])) == [
        (0, ACTIVE),
        (11, UNKNOWN),
        (12, ACTIVE),
    ]


def test_replay_end_marks_staleness_after_last_sample() -> None:
    rule = replace(PRESSURE, stale_after_ns=100)
    assert states(evaluate(rule, [0, 50], [300.0, 300.0])) == [(0, ACTIVE)]
    assert states(evaluate(rule, [0, 50], [300.0, 300.0], end_ns=150)) == [(0, ACTIVE)]
    assert states(evaluate(rule, [0, 50], [300.0, 300.0], end_ns=151)) == [
        (0, ACTIVE),
        (151, UNKNOWN),
    ]
    assert evaluate(PRESSURE, [0, 50], [300.0, 300.0], end_ns=10**12) == evaluate(
        PRESSURE, [0, 50], [300.0, 300.0]
    )


# Duplicate timestamps


def test_duplicate_timestamps_report_final_state_at_that_time() -> None:
    times = [0, 5, 5, 5, 10]
    transitions = evaluate(PRESSURE, times, [0.0, 300.0, None, 300.0, 300.0])
    assert states(transitions) == [(0, NORMAL), (5, ACTIVE)]
    assert transitions[1].value == 300.0
    transitions = evaluate(PRESSURE, [0, 5, 5], [0.0, 300.0, 0.0])
    assert states(transitions) == [(0, NORMAL)]
    transitions = evaluate(PRESSURE, [0, 5, 5], [0.0, 300.0, None])
    assert transitions[-1] == AlarmTransition(5, UNKNOWN, None, REASON_MISSING)
    assert states(evaluate(PRESSURE, [5, 5], [300.0, 0.0])) == [(5, NORMAL)]
    assert evaluate(PRESSURE, [5, 5], [300.0, None]) == ()


def test_duplicate_timestamps_do_not_advance_debounce() -> None:
    rule = replace(PRESSURE, debounce_ns=1)
    assert evaluate(rule, [0, 0, 0], [300.0, 300.0, 300.0]) == ()


# Input validation


def test_evaluate_rejects_invalid_input() -> None:
    with pytest.raises(TypeError, match="ThresholdRule"):
        evaluate("rule", [0], [1.0])  # type: ignore[arg-type]
    with pytest.raises(ValueError, match="same length"):
        evaluate(PRESSURE, [0, 1], [1.0])
    with pytest.raises(ValueError, match=r"non-decreasing \(times_ns\[2\]\)"):
        evaluate(PRESSURE, [0, 2, 1], [1.0, 1.0, 1.0])
    with pytest.raises(TypeError, match=r"times_ns\[1\]"):
        evaluate(PRESSURE, [0, 1.5], [1.0, 1.0])  # type: ignore[list-item]
    with pytest.raises(TypeError, match=r"times_ns\[0\]"):
        evaluate(PRESSURE, [True], [1.0])
    with pytest.raises(TypeError, match=r"values\[1\]"):
        evaluate(PRESSURE, [0, 1], [1.0, "2"])  # type: ignore[list-item]
    with pytest.raises(TypeError, match=r"values\[0\]"):
        evaluate(PRESSURE, [0], [False])
    with pytest.raises(TypeError, match="end_ns"):
        evaluate(PRESSURE, [0], [1.0], end_ns=1.0)  # type: ignore[arg-type]
    with pytest.raises(ValueError, match="end_ns"):
        evaluate(PRESSURE, [0, 10], [1.0, 1.0], end_ns=9)


def test_overflowing_integer_value_is_missing() -> None:
    assert states(evaluate(PRESSURE, [0, 1], [300.0, 10**400])) == [(0, ACTIVE), (1, UNKNOWN)]


# Seeking and determinism


def _random_stream(seed: int) -> tuple[list[int], list[float | None]]:
    generator = random.Random(seed)
    times: list[int] = []
    values: list[float | None] = []
    time_ns = 0
    for _ in range(400):
        time_ns += generator.choice([0, 1, 5, 10, 10, 10, 40, 200])
        roll = generator.random()
        value: float | None = generator.uniform(230.0, 270.0)
        if roll < 0.05:
            value = None
        elif roll < 0.08:
            value = math.nan
        times.append(time_ns)
        values.append(value)
    return times, values


@pytest.mark.parametrize("seed", [1, 2, 3])
@pytest.mark.parametrize(
    "rule",
    [
        replace(PRESSURE, hysteresis=8.0, debounce_ns=15, stale_after_ns=50),
        replace(PRESSURE, comparison="below", hysteresis=3.0, stale_after_ns=30),
        PRESSURE,
    ],
)
def test_seek_matches_evaluation_up_to_seek_time(seed: int, rule: ThresholdRule) -> None:
    times, values = _random_stream(seed)
    full = evaluate(rule, times, values)
    assert full == evaluate(rule, times, values)
    assert all(a.time_ns < b.time_ns for a, b in zip(full, full[1:]))
    assert all(a.state is not b.state for a, b in zip(full, full[1:]))
    generator = random.Random(seed + 100)
    seeks = [generator.randint(-5, times[-1] + 5) for _ in range(150)]
    seeks += [item.time_ns for item in full] + [item.time_ns - 1 for item in full]
    for seek_ns in seeks:  # random order: forward and backward seeks
        count = bisect_right(times, seek_ns)
        partial = evaluate(rule, times[:count], values[:count], end_ns=seek_ns)
        assert state_at(full, seek_ns) is final_state(partial), seek_ns


def test_state_at_before_first_transition_is_unknown() -> None:
    transitions = evaluate(PRESSURE, [10, 20], [0.0, 300.0])
    assert state_at(transitions, 9) is UNKNOWN
    assert state_at(transitions, 10) is NORMAL
    assert state_at(transitions, 19) is NORMAL
    assert state_at(transitions, 20) is ACTIVE
    assert state_at((), 0) is UNKNOWN
    assert transition_at(transitions, 25) == transitions[1]
    assert transition_at(transitions, 0) is None


# Acknowledgement

ACK_TIMES = [0, 10, 20, 30, 40, 50]
ACK_VALUES = [300.0, 300.0, None, 300.0, 0.0, 300.0]


def test_acknowledging_one_activation_does_not_cover_a_later_one() -> None:
    transitions = evaluate(PRESSURE, ACK_TIMES, ACK_VALUES)
    assert states(transitions) == [
        (0, ACTIVE),
        (20, UNKNOWN),
        (30, ACTIVE),
        (40, NORMAL),
        (50, ACTIVE),
    ]
    rule_id = PRESSURE.rule_id
    acks = acknowledge(frozenset(), rule_id, transitions, 0)
    assert acks == frozenset({("pressure-high", 0)})
    assert is_acknowledged(acks, rule_id, transitions, 0)
    assert is_acknowledged(acks, rule_id, transitions, 19)
    assert not is_acknowledged(acks, rule_id, transitions, 20)
    assert not is_acknowledged(acks, rule_id, transitions, 30)
    assert not is_acknowledged(acks, rule_id, transitions, 50)
    assert not is_acknowledged(acks, "other-rule", transitions, 0)
    assert not is_acknowledged(acks, rule_id, transitions, -1)
    later = acknowledge(acks, rule_id, transitions, 30)
    assert is_acknowledged(later, rule_id, transitions, 35)
    assert not is_acknowledged(later, rule_id, transitions, 55)
    assert acknowledge(later, rule_id, transitions, 30) == later


@pytest.mark.parametrize("activation_ns", [-1, 5, 20, 40, 51])
def test_acknowledging_a_non_activation_is_rejected(activation_ns: int) -> None:
    transitions = evaluate(PRESSURE, ACK_TIMES, ACK_VALUES)
    with pytest.raises(ValueError, match="no activation"):
        acknowledge(frozenset(), PRESSURE.rule_id, transitions, activation_ns)


# Saved definitions


def test_json_round_trip_preserves_rules() -> None:
    rules = (
        replace(PRESSURE, hysteresis=12.5, debounce_ns=250 * MS, severity="critical", label="Line"),
        replace(TEMPERATURE, stale_after_ns=2_000 * MS, unit=DEGREE_C, label="Coolant"),
        ThresholdRule("r", "s", "above", 0.1),
    )
    text = rules_to_json(rules)
    assert text.isascii() and text.endswith("\n")
    assert rules_from_json(text) == rules
    document = json.loads(text)
    assert (document["format"], document["version"]) == ("lbr-dashboard-thresholds", 1)
    assert list(document["rules"][0]) == [
        "rule_id",
        "signal_id",
        "comparison",
        "level",
        "hysteresis",
        "debounce_ns",
        "severity",
        "stale_after_ns",
        "label",
        "unit",
    ]
    assert rules_from_json(rules_to_json([])) == ()


def test_optional_fields_use_defaults_when_omitted() -> None:
    text = json.dumps(
        {
            "format": "lbr-dashboard-thresholds",
            "version": 1,
            "rules": [{"rule_id": "r", "signal_id": "s", "comparison": "below", "level": 3}],
        }
    )
    assert rules_from_json(text) == (ThresholdRule("r", "s", "below", 3.0),)


def test_rules_to_json_rejects_invalid_rule_lists() -> None:
    with pytest.raises(TypeError, match=r"rules\[1\]"):
        rules_to_json([PRESSURE, "rule"])  # type: ignore[list-item]
    with pytest.raises(ThresholdFileError) as caught:
        rules_to_json([PRESSURE, replace(TEMPERATURE, rule_id=PRESSURE.rule_id)])
    assert (caught.value.index, caught.value.field) == (1, "rule_id")


def _document(*rules: object, **top: object) -> str:
    document: dict[str, object] = {"format": "lbr-dashboard-thresholds", "version": 1}
    document["rules"] = list(rules)
    document.update(top)
    return json.dumps(document)


def _rule(**changes: object) -> dict[str, object]:
    rule: dict[str, object] = {
        "rule_id": "r",
        "signal_id": "s",
        "comparison": "above",
        "level": 1.0,
    }
    rule.update(changes)
    return rule


HEADER = '{"format": "lbr-dashboard-thresholds", "version": 1'
RULE_START = '{"rule_id": "r", "signal_id": "s", "comparison": "above"'


@pytest.mark.parametrize(
    ("text", "index", "field", "message"),
    [
        ("[]", None, None, "top level"),
        ("{", None, None, "not valid JSON"),
        # A short id: pytest puts test ids in an environment variable, which Windows caps.
        pytest.param("[" * 100_000, None, None, "not valid JSON", id="deeply-nested"),
        (_document(format="other"), None, "format", "expected"),
        ('{"version": 1, "rules": []}', None, "format", "missing"),
        ('{"format": "lbr-dashboard-thresholds", "rules": []}', None, "version", "missing"),
        (_document(version=2), None, "version", "unsupported version 2"),
        (_document(version="1"), None, "version", "integer"),
        (_document(version=True), None, "version", "integer"),
        (_document(version=1.0), None, "version", "integer"),
        (_document(extra=1), None, "extra", "unknown field"),
        (HEADER + "}", None, "rules", "missing"),
        (_document(rules={}), None, "rules", "list"),
        (HEADER + ', "rules": [], "rules": []}', None, "rules", "more than once"),
        (_document(_rule(), "rule"), 1, None, "JSON object"),
        (_document(_rule(hysterisis=1.0)), 0, "hysterisis", "unknown field"),
        (_document({"rule_id": "r", "signal_id": "s", "level": 1.0}), 0, "comparison", "missing"),
        (_document(_rule(), _rule(signal_id="t")), 1, "rule_id", "duplicate rule_id"),
        (_document(_rule(rule_id=7)), 0, "rule_id", "non-empty string"),
        (_document(_rule(signal_id=None)), 0, "signal_id", "non-empty string"),
        (_document(_rule(comparison="over")), 0, "comparison", "'above' or 'below'"),
        (_document(_rule(level="1")), 0, "level", "number"),
        (_document(_rule(level=None)), 0, "level", "number"),
        (_document(_rule(hysteresis=-1)), 0, "hysteresis", "zero or positive"),
        (_document(_rule(debounce_ns=0.5)), 0, "debounce_ns", "integer"),
        (_document(_rule(debounce_ns=-1)), 0, "debounce_ns", "integer"),
        (_document(_rule(severity="loud")), 0, "severity", "'info', 'warning'"),
        (_document(_rule(stale_after_ns=0)), 0, "stale_after_ns", "integer > 0"),
        (_document(_rule(label=[])), 0, "label", "string"),
        (_document(_rule(unit={})), 0, "unit", "string"),
        (_document(_rule()).replace("1.0", "NaN"), None, None, "NaN is not allowed"),
        (_document(_rule()).replace("1.0", "-Infinity"), None, None, "not allowed"),
        (_document(_rule()).replace("1.0", "1e400"), 0, "level", "finite"),
        pytest.param(
            _document(_rule()).replace("1.0", "1" + "0" * 400), 0, "level", "finite", id="huge-int"
        ),
        (
            HEADER + ', "rules": [' + RULE_START + ', "level": 1, "level": 2}]}',
            0,
            "level",
            "more than once",
        ),
    ],
)
def test_rules_from_json_rejects_invalid_documents(
    text: str, index: int | None, field: str | None, message: str
) -> None:
    with pytest.raises(ThresholdFileError) as caught:
        rules_from_json(text)
    assert (caught.value.index, caught.value.field) == (index, field)
    assert message in str(caught.value)
    if index is not None:
        prefix = f"rules[{index}]" + (f".{field}" if field else "")
        assert str(caught.value).startswith(prefix + ": ")
    elif field is not None:
        assert str(caught.value).startswith(field + ": ")


def test_rules_from_json_rejects_oversized_and_non_text_input() -> None:
    with pytest.raises(ThresholdFileError, match="larger than"):
        rules_from_json(" " * (MAX_THRESHOLD_FILE_CHARS + 1))
    with pytest.raises(ThresholdFileError, match="must be text"):
        rules_from_json(b"{}")  # type: ignore[arg-type]
    padded = rules_to_json([PRESSURE])
    padded += " " * (MAX_THRESHOLD_FILE_CHARS - len(padded))
    assert rules_from_json(padded) == (PRESSURE,)


def test_saved_rules_evaluate_identically() -> None:
    rule = replace(PRESSURE, hysteresis=5.0, debounce_ns=10, stale_after_ns=40)
    (loaded,) = rules_from_json(rules_to_json([rule]))
    times, values = _random_stream(7)
    end_ns = times[-1] + 100
    assert evaluate(loaded, times, values, end_ns=end_ns) == evaluate(
        rule, times, values, end_ns=end_ns
    )
