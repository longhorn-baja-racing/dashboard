"""Table-driven checks for turning descriptors and samples into display text."""

from dataclasses import FrozenInstanceError, replace

import pytest

from lbr_dashboard.core.value_display import (
    EMPTY_TEXT,
    MAX_TEXT_LENGTH,
    MAX_VECTOR_COMPONENTS,
    StaticDescriptor,
    StaticSample,
    ValueState,
    describe_value,
    format_number,
    format_seconds,
)

S = 1_000_000_000
NAN = float("nan")
INF = float("inf")

PRESSURE = StaticDescriptor("front.pressure", "Front pressure", "csv-replay", "numeric", "kPa")
COUNT = StaticDescriptor("lap.count", "Lap count", "live-adapter", "integer")
BRAKE = StaticDescriptor("brake.switch", "Brake switch", "live-adapter", "boolean")
NOTE = StaticDescriptor("pit.note", "Pit note", "simulator", "text")
MODE = StaticDescriptor("ecu.mode", "ECU mode", "live-adapter", "status")
ACCEL = StaticDescriptor(
    "imu.accel", "Acceleration", "simulator", "vector", "m/s^2", ("x", "y", "z"), 2
)
RAW_VECTOR = StaticDescriptor("raw.vector", "Raw vector", "simulator", "vector")
SPECTRUM = StaticDescriptor("future.spectrum", "Spectrum", "future-adapter", "spectrum", "dB")


def sample(value: object, timestamp_ns: int = S, **kwargs: object) -> StaticSample:
    return StaticSample(timestamp_ns, value, **kwargs)  # type: ignore[arg-type]


OK_CASES = [
    pytest.param(PRESSURE, 101.325, "101.325 kPa", id="float"),
    pytest.param(PRESSURE, -4.5, "-4.5 kPa", id="float-negative"),
    pytest.param(PRESSURE, 0.0, "0 kPa", id="float-zero"),
    pytest.param(PRESSURE, 7, "7 kPa", id="float-from-int"),
    pytest.param(PRESSURE, 1 / 3, "0.333333 kPa", id="float-bounded"),
    pytest.param(PRESSURE, 1.5e20, "1.5e+20 kPa", id="float-large"),
    pytest.param(replace(PRESSURE, decimal_places=2), 1 / 3, "0.33 kPa", id="float-places"),
    pytest.param(replace(PRESSURE, decimal_places=0), 2.5, "2 kPa", id="float-places-0"),
    pytest.param(replace(PRESSURE, decimal_places=3), 2e15, "2e+15 kPa", id="float-places-big"),
    pytest.param(replace(PRESSURE, canonical_unit=None), 3.25, "3.25", id="no-unit"),
    pytest.param(replace(PRESSURE, canonical_unit=""), 3.25, "3.25", id="empty-unit"),
    pytest.param(COUNT, 0, "0", id="int-zero"),
    pytest.param(COUNT, -12, "-12", id="int-negative"),
    pytest.param(COUNT, 3.0, "3", id="int-from-whole-float"),
    pytest.param(COUNT, 10**20, "1.00000e+20", id="int-large"),
    pytest.param(BRAKE, False, "false", id="bool-false"),
    pytest.param(BRAKE, True, "true", id="bool-true"),
    pytest.param(NOTE, "0", '"0"', id="text-zero"),
    pytest.param(NOTE, "", '""', id="text-empty"),
    pytest.param(NOTE, "a\nb", '"a\\x0ab"', id="text-control"),
    pytest.param(MODE, "RUNNING", "[RUNNING]", id="status-label"),
    pytest.param(MODE, 0, "[code 0]", id="status-code"),
    pytest.param(ACCEL, (0.1, -9.81, 0), "(x=0.10, y=-9.81, z=0.00) m/s^2", id="vector"),
    pytest.param(RAW_VECTOR, [1, 2.5], "(1, 2.5)", id="vector-no-labels"),
]


@pytest.mark.parametrize(("descriptor", "value", "expected"), OK_CASES)
def test_valid_values_are_formatted(descriptor, value, expected) -> None:
    display = describe_value(descriptor, sample(value))
    assert display.state is ValueState.OK
    assert display.state_text == "OK"
    assert display.value_text == expected
    assert display.has_value


def test_zero_false_missing_invalid_and_stale_are_all_different() -> None:
    zero = describe_value(COUNT, sample(0))
    false = describe_value(BRAKE, sample(False))
    text_zero = describe_value(NOTE, sample("0"))
    status_zero = describe_value(MODE, sample(0))
    missing = describe_value(PRESSURE, None)
    unavailable = describe_value(PRESSURE, sample(None, available=False))
    invalid = describe_value(PRESSURE, sample(NAN))
    stale = describe_value(PRESSURE, sample(0.0), now_ns=10 * S, max_age_ns=S)

    texts = {d.value_text for d in (zero, false, text_zero, status_zero)}
    assert texts == {"0", "false", '"0"', "[code 0]"}
    states = [d.state for d in (zero, missing, unavailable, invalid, stale)]
    assert states == [
        ValueState.OK,
        ValueState.NO_DATA,
        ValueState.UNAVAILABLE,
        ValueState.INVALID,
        ValueState.STALE,
    ]
    assert len({d.state_text for d in (zero, missing, unavailable, invalid, stale)}) == 5
    for display in (missing, unavailable, invalid):
        assert display.value_text == EMPTY_TEXT
        assert not display.has_value
    assert stale.value_text == "0 kPa"
    assert stale.has_value


INVALID_CASES = [
    pytest.param(PRESSURE, NAN, "not finite", id="float-nan"),
    pytest.param(PRESSURE, -INF, "not finite", id="float-inf"),
    pytest.param(PRESSURE, None, "got no value", id="float-none"),
    pytest.param(PRESSURE, True, "got a boolean", id="float-bool"),
    pytest.param(PRESSURE, "1.0", "got str", id="float-text"),
    pytest.param(PRESSURE, 10**400, "too large", id="float-overflow"),
    pytest.param(COUNT, 2.5, "expected an integer", id="int-fraction"),
    pytest.param(COUNT, NAN, "non-finite", id="int-nan"),
    pytest.param(COUNT, False, "got a boolean", id="int-bool"),
    pytest.param(BRAKE, 0, "expected true or false", id="bool-int"),
    pytest.param(BRAKE, "false", "got str", id="bool-text"),
    pytest.param(NOTE, 5, "expected text", id="text-int"),
    pytest.param(MODE, "  ", "status label", id="status-blank"),
    pytest.param(MODE, 1.5, "status label", id="status-float"),
    pytest.param(ACCEL, (1.0, 2.0), "2 components, expected 3", id="vector-length"),
    pytest.param(ACCEL, (1.0, NAN, 2.0), "component 1", id="vector-nan"),
    pytest.param(ACCEL, "xyz", "sequence of numbers", id="vector-text"),
    pytest.param(RAW_VECTOR, (), "no components", id="vector-empty"),
    pytest.param(RAW_VECTOR, 4.0, "sequence of numbers", id="vector-scalar"),
]


@pytest.mark.parametrize(("descriptor", "value", "reason"), INVALID_CASES)
def test_values_that_do_not_match_the_declared_type_are_invalid(descriptor, value, reason) -> None:
    display = describe_value(descriptor, sample(value))
    assert display.state is ValueState.INVALID
    assert display.value_text == EMPTY_TEXT
    assert display.quality_text.startswith("invalid: ")
    assert reason in display.quality_text
    assert display.time_text == "1.000 s"


def test_no_sample_yet() -> None:
    display = describe_value(PRESSURE)
    assert display.state is ValueState.NO_DATA
    assert (display.value_text, display.time_text) == (EMPTY_TEXT, EMPTY_TEXT)
    assert display.quality_text == "no sample yet"
    assert (display.timestamp_ns, display.age_ns) == (None, None)
    assert display.unit_text == "kPa"


@pytest.mark.parametrize(
    ("quality", "expected"),
    [
        pytest.param("sensor disconnected", "unavailable: sensor disconnected", id="reason"),
        pytest.param(None, "unavailable: no reason given", id="no-reason"),
    ],
)
def test_unavailable_sample_never_shows_a_value(quality, expected) -> None:
    display = describe_value(PRESSURE, sample(12.0, 2 * S, available=False, quality=quality))
    assert display.state is ValueState.UNAVAILABLE
    assert display.value_text == EMPTY_TEXT
    assert display.quality_text == expected
    assert display.time_text == "2.000 s"


@pytest.mark.parametrize(
    ("now_ns", "max_age_ns", "state", "time_text"),
    [
        pytest.param(None, S, ValueState.OK, "1.000 s", id="no-reference-time"),
        pytest.param(3 * S, None, ValueState.OK, "1.000 s (age 2.000 s)", id="no-limit"),
        pytest.param(2 * S, S, ValueState.OK, "1.000 s (age 1.000 s)", id="age-at-limit"),
        pytest.param(2 * S + 1, S, ValueState.STALE, "1.000 s (age 1.000000001 s)", id="stale"),
        pytest.param(S - 1, S, ValueState.INVALID, "1.000 s (age -0.000000001 s)", id="future"),
    ],
)
def test_age_and_staleness(now_ns, max_age_ns, state, time_text) -> None:
    display = describe_value(PRESSURE, sample(5.0), now_ns=now_ns, max_age_ns=max_age_ns)
    assert display.state is state
    assert display.time_text == time_text
    assert display.timestamp_ns == S


def test_stale_and_future_quality_text() -> None:
    stale = describe_value(
        PRESSURE, sample(5.0, quality="estimated"), now_ns=4 * S, max_age_ns=S // 2
    )
    assert stale.quality_text == "stale: sample is 3.000 s old (limit 0.500 s); quality estimated"
    assert stale.value_text == "5 kPa"
    future = describe_value(PRESSURE, sample(5.0, 3 * S), now_ns=S)
    assert future.quality_text == "invalid: sample time is 2.000 s after the reference time"
    assert future.value_text == EMPTY_TEXT


def test_ok_quality_text_reports_sample_quality() -> None:
    assert describe_value(PRESSURE, sample(1.0)).quality_text == "ok"
    assert describe_value(PRESSURE, sample(1.0, quality="good")).quality_text == "ok; quality good"


@pytest.mark.parametrize(
    "timestamp",
    [pytest.param(1.5, id="float"), pytest.param(True, id="bool"), pytest.param(None, id="none")],
)
def test_non_integer_timestamp_is_invalid(timestamp) -> None:
    display = describe_value(PRESSURE, sample(1.0, timestamp))
    assert display.state is ValueState.INVALID
    assert display.time_text == EMPTY_TEXT
    assert "timestamp is not an integer" in display.quality_text


def test_unknown_type_is_explicitly_unsupported() -> None:
    display = describe_value(SPECTRUM, sample([1.0, 2.0], 2 * S), now_ns=3 * S)
    assert display.state is ValueState.UNSUPPORTED_TYPE
    assert display.state_text == "UNSUPPORTED TYPE"
    assert display.value_text == EMPTY_TEXT
    assert display.type_text == "spectrum (unknown)"
    assert display.quality_text == "unsupported value type 'spectrum'; value is not shown"
    assert display.time_text == "2.000 s (age 1.000 s)"
    assert describe_value(SPECTRUM).state is ValueState.UNSUPPORTED_TYPE


@pytest.mark.parametrize(
    ("declared", "value", "expected"),
    [
        pytest.param("string", "abc", '"abc"', id="string"),
        pytest.param("float", 1.25, "1.25", id="float"),
        pytest.param("int", 4, "4", id="int"),
        pytest.param("bool", True, "true", id="bool"),
    ],
)
def test_type_aliases(declared, value, expected) -> None:
    descriptor = StaticDescriptor("a.b", "A", "src", declared)
    display = describe_value(descriptor, sample(value))
    assert (display.state, display.value_text) == (ValueState.OK, expected)


def test_units_are_only_shown_for_numbers_and_vectors() -> None:
    for kind, value in (("boolean", True), ("text", "x"), ("status", "OK")):
        descriptor = StaticDescriptor("a.b", "A", "src", kind, "V")
        display = describe_value(descriptor, sample(value))
        assert display.unit_text == ""
        assert "V" not in display.value_text
    assert describe_value(ACCEL, sample((1, 2, 3))).unit_text == "m/s^2"


def test_unicode_names_and_values_are_kept() -> None:
    # Built with chr() so this source file stays plain ASCII.
    name = "Temp" + chr(0xE9) + "rature " + chr(0x6E29) + chr(0x5EA6)
    unit = chr(0xB0) + "C"
    label = chr(0x3B1)
    descriptor = StaticDescriptor("engine.temp", name, "csv-replay", "numeric", unit)
    display = describe_value(descriptor, sample(85.5))
    assert display.name_text == name
    assert display.value_text == "85.5 " + unit
    assert display.unit_text == unit
    vector = StaticDescriptor("v.w", name, "s", "vector", None, (label, "b"))
    assert describe_value(vector, sample((1, 2))).value_text == f"({label}=1, b=2)"
    status = describe_value(MODE, sample(chr(0x2713)))
    assert status.value_text == "[" + chr(0x2713) + "]"


def test_empty_display_name_falls_back_to_identifier() -> None:
    display = describe_value(replace(PRESSURE, display_name=""), sample(1.0))
    assert display.name_text == "front.pressure"


def test_long_text_and_vectors_are_bounded() -> None:
    text = describe_value(NOTE, sample("x" * 1000)).value_text
    assert len(text) == MAX_TEXT_LENGTH + 2
    assert text.endswith('..."')
    exact = describe_value(NOTE, sample("y" * MAX_TEXT_LENGTH)).value_text
    assert exact == '"' + "y" * MAX_TEXT_LENGTH + '"'
    count = MAX_VECTOR_COMPONENTS + 4
    long_vector = describe_value(RAW_VECTOR, sample(list(range(count)))).value_text
    assert long_vector.endswith(", ... (+4 more))")
    assert long_vector.count(",") == MAX_VECTOR_COMPONENTS
    wide = describe_value(NOTE, sample(chr(0x0) + chr(0x2028))).value_text
    assert wide == '"\\x00\\u2028"'


SOURCES = [
    pytest.param(PRESSURE, None, ValueState.OK, id="sample-without-source"),
    pytest.param(PRESSURE, "csv-replay", ValueState.OK, id="same-source"),
    pytest.param(PRESSURE, "live-adapter", ValueState.INVALID, id="other-source"),
    pytest.param(
        replace(PRESSURE, source_id="live-adapter"), "live-adapter", ValueState.OK, id="live"
    ),
]


@pytest.mark.parametrize(("descriptor", "source", "state"), SOURCES)
def test_mixed_sources_keep_their_identity(descriptor, source, state) -> None:
    display = describe_value(descriptor, sample(2.0, source_id=source))
    assert display.state is state
    assert display.signal_id == "front.pressure"
    assert display.identity_text == f"front.pressure from {descriptor.source_id}"
    if state is ValueState.INVALID:
        assert display.quality_text == (
            "invalid: sample is from source 'live-adapter', expected 'csv-replay'"
        )


def test_signals_from_different_sources_render_independently() -> None:
    displays = [
        describe_value(PRESSURE, sample(1.0, source_id="csv-replay")),
        describe_value(COUNT, sample(2, source_id="live-adapter")),
        describe_value(ACCEL, sample((0, 0, 1), source_id="simulator")),
        describe_value(SPECTRUM, sample(b"\x00")),
    ]
    assert [d.identity_text for d in displays] == [
        "front.pressure from csv-replay",
        "lap.count from live-adapter",
        "imu.accel from simulator",
        "future.spectrum from future-adapter",
    ]
    assert [d.type_text for d in displays] == [
        "numeric",
        "integer",
        "vector",
        "spectrum (unknown)",
    ]


@pytest.mark.parametrize(
    ("kwargs", "message"),
    [
        pytest.param({"max_age_ns": -1}, "max_age_ns", id="negative-age"),
        pytest.param({"max_age_ns": 1.5}, "max_age_ns", id="float-age"),
        pytest.param({"now_ns": 1.5}, "now_ns", id="float-now"),
    ],
)
def test_bad_arguments_are_rejected(kwargs, message) -> None:
    with pytest.raises(ValueError, match=message):
        describe_value(PRESSURE, sample(1.0), **kwargs)


@pytest.mark.parametrize("places", [pytest.param(-1, id="neg"), pytest.param(10, id="big")])
def test_bad_decimal_places_are_rejected(places) -> None:
    with pytest.raises(ValueError, match="decimal_places"):
        describe_value(replace(PRESSURE, decimal_places=places), sample(1.0))


@pytest.mark.parametrize(
    ("value_ns", "expected"),
    [
        pytest.param(0, "0.000 s", id="zero"),
        pytest.param(1_500_000_000, "1.500 s", id="half"),
        pytest.param(1_000_000_001, "1.000000001 s", id="ns"),
        pytest.param(-2_250_000_000, "-2.250 s", id="negative"),
    ],
)
def test_format_seconds(value_ns, expected) -> None:
    assert format_seconds(value_ns) == expected


def test_format_number() -> None:
    assert format_number(1234567.0) == "1.23457e+06"
    assert format_number(1.0, 3) == "1.000"


def test_display_record_is_immutable() -> None:
    display = describe_value(PRESSURE, sample(1.0))
    with pytest.raises(FrozenInstanceError):
        display.value_text = "0"  # type: ignore[misc]
