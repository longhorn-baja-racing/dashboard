"""Derived signals: allowlisted expressions, units, alignment and definitions."""

from __future__ import annotations

import ast
import builtins
import importlib.abc
import json
import math
import sys
from pathlib import Path
from typing import Any

import pytest

from lbr_dashboard.core import derived
from lbr_dashboard.core.derived import (
    CycleError,
    DefinitionError,
    DerivedSignalDefinition,
    EvaluationFailure,
    ExpressionError,
    Missing,
    MissingReason,
    SignalInput,
    UnitError,
    UnresolvedInputError,
    evaluate,
    evaluate_all,
    format_unit,
    normalize_unit,
    order_definitions,
    parse_expression,
    parse_unit,
)

# Two unrelated signal identities from different kinds of sources.
TEMP = "process.temperature"
PRESSURE = "can:0x18F/hydraulics-pressure"
NON_ASCII = chr(0xE9)


def define(expression: str, output_id: str = "out", **kwargs: object) -> DerivedSignalDefinition:
    return DerivedSignalDefinition(output_id, "Output", expression, **kwargs)  # type: ignore[arg-type]


def series(times: list[int], values: list[float | None], unit: str | None = "1") -> SignalInput:
    return SignalInput(times, values, unit)


# ---------------------------------------------------------------------------
# Arithmetic


@pytest.mark.parametrize(
    ("expression", "expected"),
    [
        pytest.param('sig("a") + 2', 5.0, id="add"),
        pytest.param('sig("a") - 5', -2.0, id="sub"),
        pytest.param('sig("a") * 4', 12.0, id="mul"),
        pytest.param('sig("a") / 2', 1.5, id="div"),
        pytest.param('-sig("a")', -3.0, id="neg"),
        pytest.param('2 + sig("a") * 3', 11.0, id="precedence"),
        pytest.param('(2 + sig("a")) * 3', 15.0, id="parens"),
        pytest.param('abs(-sig("a"))', 3.0, id="abs"),
        pytest.param('sqrt(sig("a") * sig("a"))', 3.0, id="sqrt"),
        pytest.param('min(sig("a"), 1, 7)', 1.0, id="min"),
        pytest.param('max(sig("a"), 1, 7)', 7.0, id="max"),
        pytest.param('hypot(sig("a"), 4)', 5.0, id="hypot"),
        pytest.param('  sig("a")  ', 3.0, id="padded"),
    ],
)
def test_known_arithmetic(expression: str, expected: float) -> None:
    result = evaluate(define(expression), {"a": series([0], [3.0])})
    assert result.values == (expected,)
    assert result.missing == (None,)


def test_magnitude_of_vector_with_units() -> None:
    accel = {
        "imu.accel.x": series([0, 10], [3.0, 1.0], "m/s^2"),
        "imu.accel.y": series([0, 10], [4.0, 2.0], "m/s/s"),
        "imu.accel.z": series([0, 10], [12.0, 2.0], "m*s^-2"),
    }
    definition = define(
        'hypot(sig("imu.accel.x"), sig("imu.accel.y"), sig("imu.accel.z"))',
        unit="m / s^2",
    )
    result = evaluate(definition, accel)
    assert result.values == (13.0, 3.0)
    assert result.unit == "m/s^2"
    assert result.times_ns == (0, 10)


def test_two_unrelated_signal_identities() -> None:
    definition = define(f'sig("{PRESSURE}") * sig("{TEMP}") / 2', unit="kPa*degC")
    assert definition.inputs == (PRESSURE, TEMP)
    result = evaluate(
        definition,
        {
            TEMP: series([5, 15], [20.0, 30.0], "degC"),
            PRESSURE: series([5, 15], [100.0, 50.0], "kPa"),
        },
    )
    assert result.values == (1000.0, 750.0)
    assert result.unit == "degC*kPa"


def test_inputs_are_not_modified() -> None:
    times = [0, 10, 20]
    values: list[float | None] = [1.0, None, 3.0]
    other_times = (0, 20)
    other_values = (2.0, 0.0)
    inputs = {
        TEMP: SignalInput(times, values, "degC"),
        PRESSURE: SignalInput(other_times, other_values, "kPa"),
    }
    definition = define(f'sig("{TEMP}") / sig("{PRESSURE}")', max_age_ns=10)
    evaluate(definition, inputs)
    evaluate_all([definition], inputs)
    assert times == [0, 10, 20]
    assert values == [1.0, None, 3.0]
    assert inputs[TEMP].values is values
    assert other_values == (2.0, 0.0)
    assert set(inputs) == {TEMP, PRESSURE}


# ---------------------------------------------------------------------------
# Missing values and arithmetic failures


@pytest.mark.parametrize(
    ("expression", "value", "reason"),
    [
        pytest.param('1 / sig("a")', 0.0, MissingReason.DIVISION_BY_ZERO, id="div-zero"),
        pytest.param('1 / -sig("a")', 0.0, MissingReason.DIVISION_BY_ZERO, id="div-neg-zero"),
        pytest.param('sqrt(sig("a"))', -1.0, MissingReason.DOMAIN_ERROR, id="sqrt-negative"),
        pytest.param('sig("a") * 1e308', 10.0, MissingReason.NON_FINITE, id="overflow-mul"),
        pytest.param('sig("a") + 1e308', 1e308, MissingReason.NON_FINITE, id="overflow-add"),
        pytest.param('sig("a") - 1e308', -1e308, MissingReason.NON_FINITE, id="overflow-sub"),
        pytest.param('sig("a") / 1e-308', 1e10, MissingReason.NON_FINITE, id="overflow-div"),
        pytest.param(
            'hypot(sig("a"), 1e308)', 1.7e308, MissingReason.NON_FINITE, id="overflow-hyp"
        ),
    ],
)
def test_arithmetic_failures_are_missing(expression: str, value: float, reason: object) -> None:
    result = evaluate(define(expression), {"a": series([0, 1], [value, 1e-3])})
    assert result.values[0] is None
    problem = result.missing[0]
    assert problem is not None and problem.reason is reason
    assert problem.where is not None and problem.where in expression
    assert result.values[1] is not None and result.missing[1] is None
    assert result.missing_count == 1


def test_failure_points_at_the_failing_part() -> None:
    result = evaluate(define('sig("a") + 1 / (sig("a") - 2)'), {"a": series([0], [2.0])})
    assert result.missing == (Missing(MissingReason.DIVISION_BY_ZERO, where='1 / (sig("a") - 2)'),)


def test_evaluate_row_reports_failure() -> None:
    expression = parse_expression('1 / sig("a")')
    assert repr(expression) == "Expression('1 / sig(\"a\")')"
    assert expression.signal_ids == ("a",)
    assert expression.evaluate_row({"a": 4.0}) == 0.25
    with pytest.raises(EvaluationFailure, match="division_by_zero"):
        expression.evaluate_row({"a": 0.0})


@pytest.mark.parametrize(
    "bad",
    [
        pytest.param(None, id="none"),
        pytest.param(math.nan, id="nan"),
        pytest.param(math.inf, id="inf"),
        pytest.param(-math.inf, id="neg-inf"),
    ],
)
def test_missing_input_values(bad: float | None) -> None:
    result = evaluate(define(f'sig("{TEMP}") + 1'), {TEMP: series([0, 1], [bad, 1.0])})
    assert result.values == (None, 2.0)
    assert result.missing[0] == Missing(MissingReason.MISSING_INPUT, TEMP)


# ---------------------------------------------------------------------------
# Alignment: gaps and mixed rates


def test_exact_match_is_the_default() -> None:
    definition = define(f'sig("{TEMP}") + sig("{PRESSURE}")')
    assert definition.max_age_ns == 0
    result = evaluate(
        definition,
        {TEMP: series([0, 10, 20], [1.0, 2.0, 3.0]), PRESSURE: series([0, 15], [10.0, 20.0])},
    )
    assert result.values == (11.0, None, None)
    assert result.missing[1] == Missing(MissingReason.STALE_INPUT, PRESSURE)


def test_fast_reference_holds_slow_input_within_max_age() -> None:
    fast = series(list(range(0, 100, 10)), [float(i) for i in range(10)])
    slow = series([0, 30, 60], [100.0, 200.0, 300.0])
    definition = define('sig("fast") + sig("slow")', max_age_ns=20)
    result = evaluate(definition, {"fast": fast, "slow": slow})
    assert result.times_ns == tuple(range(0, 100, 10))
    assert result.values == (
        100.0,
        101.0,
        102.0,  # slow sample at 0 is exactly 20 ns old: still used
        203.0,
        204.0,
        205.0,
        306.0,
        307.0,
        308.0,
        None,  # 90 - 60 = 30 > 20: stale
    )
    assert result.missing[-1] == Missing(MissingReason.STALE_INPUT, "slow")


def test_slow_reference_takes_latest_fast_sample() -> None:
    fast = series(list(range(0, 100, 10)), [float(i) for i in range(10)])
    slow = series([5, 35, 65], [100.0, 200.0, 300.0])
    definition = define('sig("fast") + sig("slow")', reference_id="slow", max_age_ns=5)
    result = evaluate(definition, {"fast": fast, "slow": slow})
    assert result.times_ns == (5, 35, 65)
    assert result.values == (100.0, 203.0, 306.0)
    assert result.reference_id == "slow"


def test_no_look_ahead_before_first_sample() -> None:
    definition = define('sig("a") - sig("b")', max_age_ns=1000)
    result = evaluate(definition, {"a": series([0, 10], [1.0, 2.0]), "b": series([5], [1.0])})
    assert result.values == (None, 1.0)
    assert result.missing[0] == Missing(MissingReason.STALE_INPUT, "b")


def test_gap_marks_outputs_missing_then_recovers() -> None:
    a = series([0, 10, 20, 30, 40], [1.0, 1.0, 1.0, 1.0, 1.0])
    b = series([0, 10, 40], [2.0, None, 3.0])
    result = evaluate(define('sig("a") * sig("b")', max_age_ns=10), {"a": a, "b": b})
    assert result.values == (2.0, None, None, None, 3.0)
    assert [m.reason if m else None for m in result.missing] == [
        None,
        MissingReason.MISSING_INPUT,  # held sample at 10 is missing; never falls back
        MissingReason.MISSING_INPUT,
        MissingReason.STALE_INPUT,
        None,
    ]


def test_first_failing_input_in_inputs_order_is_reported() -> None:
    definition = define('sig("a") + sig("b")', inputs=("b", "a"), reference_id="a")
    result = evaluate(definition, {"a": series([0], [None]), "b": series([5], [1.0])})
    assert result.missing == (Missing(MissingReason.STALE_INPUT, "b"),)


def test_duplicate_timestamps_use_last_held_sample() -> None:
    a = series([0, 10, 10], [1.0, 2.0, 3.0])
    b = series([0, 10, 10], [10.0, 20.0, 30.0])
    result = evaluate(define('sig("a") + sig("b")'), {"a": a, "b": b})
    assert result.times_ns == (0, 10, 10)
    assert result.values == (11.0, 32.0, 33.0)


def test_window_is_closed_and_bounded() -> None:
    a = series([0, 10, 20, 30], [0.0, 1.0, 2.0, 3.0])
    definition = define('sig("a") * 2')
    assert evaluate(definition, {"a": a}, start_ns=10, end_ns=20).values == (2.0, 4.0)
    assert evaluate(definition, {"a": a}, start_ns=25).values == (6.0,)
    assert evaluate(definition, {"a": a}, end_ns=-1).values == ()
    with pytest.raises(ValueError, match="after end_ns"):
        evaluate(definition, {"a": a}, start_ns=5, end_ns=4)


def test_held_value_from_before_window_is_used() -> None:
    definition = define('sig("a") + sig("b")', max_age_ns=15)
    a = series([0, 10, 20], [1.0, 2.0, 3.0])
    b = series([0], [10.0])
    assert evaluate(definition, {"a": a, "b": b}, start_ns=10).values == (12.0, None)


def test_unresolved_inputs_raise() -> None:
    with pytest.raises(UnresolvedInputError, match="'b'") as caught:
        evaluate(define('sig("a") + sig("b")'), {"a": series([0], [1.0])})
    assert caught.value.missing == ("b",)


@pytest.mark.parametrize(
    ("times", "values", "match"),
    [
        pytest.param([0, 1], [1.0], "same length", id="length"),
        pytest.param([5, 4], [1.0, 2.0], "non-decreasing", id="decreasing"),
    ],
)
def test_signal_input_validation(times: list[int], values: list[float], match: str) -> None:
    with pytest.raises(ValueError, match=match):
        SignalInput(times, values)


# ---------------------------------------------------------------------------
# Units


@pytest.mark.parametrize(
    ("text", "canonical"),
    [
        pytest.param("m/s/s", "m/s^2", id="repeat-div"),
        pytest.param("m*s^-2", "m/s^2", id="neg-exp"),
        pytest.param("kg * m / s^2", "kg*m/s^2", id="spaces"),
        pytest.param("N*m", "N*m", id="product"),
        pytest.param("m*N", "N*m", id="sorted"),
        pytest.param("1/s", "1/s", id="per-second"),
        pytest.param("s/kg/m^2", "s/kg/m^2", id="two-denominators"),
        pytest.param("m/m", "1", id="cancel"),
        pytest.param("1", "1", id="dimensionless"),
        pytest.param("deg  C", "deg C", id="spaced-name"),
        pytest.param("%", "%", id="percent"),
        pytest.param(None, "unknown", id="none"),
        pytest.param("", "unknown", id="empty"),
        pytest.param("unknown", "unknown", id="unknown"),
    ],
)
def test_unit_normalization(text: str | None, canonical: str) -> None:
    assert normalize_unit(text) == canonical


@pytest.mark.parametrize(
    "text",
    [
        pytest.param("m//s", id="empty-factor"),
        pytest.param("m^0", id="zero-exp"),
        pytest.param("2", id="number"),
        pytest.param("m^x", id="bad-exp"),
        pytest.param("(m)", id="parens"),
        pytest.param(5, id="not-text"),
    ],
)
def test_malformed_units(text: object) -> None:
    with pytest.raises(UnitError, match="unit"):
        parse_unit(text)  # type: ignore[arg-type]


def test_format_unit_round_trip() -> None:
    assert format_unit(parse_unit("kg*m^2/s^3")) == "kg*m^2/s^3"
    assert format_unit(None) == "unknown"
    assert format_unit(()) == "1"


@pytest.mark.parametrize(
    ("expression", "units", "expected"),
    [
        pytest.param('sig("a") + sig("b")', {"a": "m", "b": "m"}, "m", id="add-same"),
        pytest.param('sig("a") - 273.15', {"a": "K"}, "K", id="const-adopts"),
        pytest.param('2 * 3 + sig("a")', {"a": "K"}, "K", id="const-expr-adopts"),
        pytest.param('sig("a") * sig("b")', {"a": "N", "b": "m"}, "N*m", id="mul"),
        pytest.param('sig("a") / sig("b")', {"a": "m", "b": "s"}, "m/s", id="div"),
        pytest.param('sig("a") / sig("a")', {"a": "m"}, "1", id="ratio"),
        pytest.param('2 / sig("a")', {"a": "s"}, "1/s", id="reciprocal"),
        pytest.param('-sig("a") * 3', {"a": "kPa"}, "kPa", id="scale"),
        pytest.param('sqrt(sig("a") * sig("a"))', {"a": "m/s"}, "m/s", id="sqrt"),
        pytest.param('sqrt(sig("a"))', {"a": "m^2/s^4"}, "m/s^2", id="sqrt-power"),
        pytest.param('sqrt(4) * sig("a")', {"a": "V"}, "V", id="sqrt-const"),
        pytest.param('abs(sig("a"))', {"a": "A"}, "A", id="abs"),
        pytest.param('min(sig("a"), 0, sig("b"))', {"a": "m", "b": "m"}, "m", id="min"),
        pytest.param('max(1, 2) + sig("a")', {"a": "m"}, "m", id="max-const"),
        pytest.param('hypot(sig("a"), sig("b"))', {"a": "m", "b": "m"}, "m", id="hypot"),
        pytest.param('sig("a") + sig("b")', {"a": "m", "b": None}, "unknown", id="unknown-add"),
        pytest.param('sig("a") * sig("b")', {"a": "m", "b": ""}, "unknown", id="unknown-mul"),
        pytest.param('sqrt(sig("a"))', {"a": "unknown"}, "unknown", id="unknown-sqrt"),
        pytest.param('min(sig("a"), sig("b"))', {"a": "m"}, "unknown", id="unknown-missing"),
    ],
)
def test_unit_inference(expression: str, units: dict[str, str | None], expected: str) -> None:
    assert parse_expression(expression).infer_unit(units) == expected


@pytest.mark.parametrize(
    ("expression", "units", "segment"),
    [
        pytest.param('sig("a") + sig("b")', {"a": "m", "b": "s"}, None, id="add"),
        pytest.param('1 + (sig("a") - sig("b"))', {"a": "K", "b": "degC"}, "-", id="nested-sub"),
        pytest.param('sig("a") + sig("a") * sig("a")', {"a": "m"}, None, id="add-square"),
        pytest.param('sig("a") + 1 / sig("a")', {"a": "s"}, None, id="add-reciprocal"),
        pytest.param('sqrt(sig("a"))', {"a": "m"}, "sqrt", id="sqrt-odd"),
        pytest.param('hypot(sig("a"), sig("b"))', {"a": "m", "b": "ft"}, "hypot", id="hypot"),
        pytest.param('max(sig("a"), sig("b"))', {"a": "m", "b": "1"}, "max", id="max-dimless"),
    ],
)
def test_incompatible_units(expression: str, units: dict[str, str], segment: str | None) -> None:
    parsed = parse_expression(expression)
    with pytest.raises(UnitError, match="unit") as caught:
        parsed.infer_unit(units)
    assert caught.value.segment is not None and caught.value.segment in expression
    if segment is not None:
        assert segment in caught.value.segment


def test_malformed_input_unit_names_the_input() -> None:
    with pytest.raises(UnitError, match="input 'a'"):
        parse_expression('sig("a") + 1').infer_unit({"a": "m//s"})


def test_incompatible_units_raise_before_evaluating() -> None:
    definition = define(f'sig("{TEMP}") + sig("{PRESSURE}")')
    with pytest.raises(UnitError, match="identical units"):
        evaluate(
            definition, {TEMP: series([0], [1.0], "degC"), PRESSURE: series([0], [2.0], "kPa")}
        )


def test_declared_unit_is_validated() -> None:
    units = {"vx": "m/s", "vy": "m/s"}
    good = define('hypot(sig("vx"), sig("vy"))', unit="m*s^-1")
    assert good.output_unit(units) == "m/s"
    wrong = define('hypot(sig("vx"), sig("vy")) * 3.6', unit="km/h")
    with pytest.raises(UnitError, match="does not match"):
        wrong.output_unit(units)
    with pytest.raises(UnitError, match="cannot confirm"):
        good.output_unit({"vx": "m/s", "vy": None})
    assert define('sig("a") + 1').output_unit({"a": None}) == "unknown"


@pytest.mark.parametrize(
    "unit",
    [pytest.param("", id="empty"), pytest.param("unknown", id="unknown")],
)
def test_declared_unit_must_be_a_unit(unit: str) -> None:
    with pytest.raises(DefinitionError, match="not a unit"):
        define('sig("a")', unit=unit)


# ---------------------------------------------------------------------------
# Invalid and unsafe expressions


@pytest.mark.parametrize(
    ("expression", "match", "segment"),
    [
        pytest.param("__import__('os')", "unknown function", "__import__", id="import"),
        pytest.param("__import__('os').system('echo hacked')", "only calls", None, id="system"),
        pytest.param('sig("a").real', "attribute access", 'sig("a").real', id="attribute"),
        pytest.param('sig("a").__class__', "attribute access", None, id="dunder"),
        pytest.param("(lambda: 1)()", "only calls to", "(lambda: 1)()", id="call-lambda"),
        pytest.param('sig("a") + (lambda: 1)', "lambda", "lambda: 1", id="lambda"),
        pytest.param('sig("a")[0]', "indexing", 'sig("a")[0]', id="subscript"),
        pytest.param("[x for x in ()]", "comprehension", None, id="listcomp"),
        pytest.param("sum(x for x in ())", "unknown function", "sum", id="genexp-call"),
        pytest.param('eval("1")', "unknown function", "eval", id="eval"),
        pytest.param('exec("1")', "unknown function", "exec", id="exec"),
        pytest.param('open("f")', "unknown function", "open", id="open"),
        pytest.param('sig("a") ** 2', r"operator \*\*", 'sig("a") ** 2', id="pow"),
        pytest.param('sig("a") % 2', "operator %", None, id="mod"),
        pytest.param('sig("a") // 2', "operator //", None, id="floordiv"),
        pytest.param('+sig("a")', "operator unary +", None, id="uadd"),
        pytest.param('not sig("a")', "operator not", None, id="not"),
        pytest.param('sig("a") > 1', "comparison", None, id="compare"),
        pytest.param('sig("a") and 1', "boolean operator", None, id="boolop"),
        pytest.param('sig("a") if 1 else 2', "conditional", None, id="ifexp"),
        pytest.param('(x := sig("a"))', "assignment", None, id="walrus"),
        pytest.param('sig("a") + x', "unknown name 'x'", "x", id="name"),
        pytest.param('sig("a") + "s"', "text is only allowed", '"s"', id="string"),
        pytest.param('sig("a") + f"{1}"', "f-string", None, id="fstring"),
        pytest.param('sig("a") + True', "real numbers", "True", id="bool"),
        pytest.param('sig("a") + None', "real numbers", "None", id="none"),
        pytest.param('sig("a") * 2j', "real numbers", "2j", id="complex"),
        pytest.param('sig("a") + 1e999', "too large", "1e999", id="inf-literal"),
        pytest.param('sig("a") + ' + "9" * 400, "too large", None, id="huge-int"),
        pytest.param('(sig("a"), 1)', "tuple", None, id="tuple"),
        pytest.param('[sig("a")]', "list", None, id="list"),
        pytest.param('{"a": 1}', "dict", None, id="dict"),
        pytest.param('sig(sig("a"))', "quoted text literal", None, id="sig-nested"),
        pytest.param('sig("a", "b")', "exactly one", None, id="sig-two"),
        pytest.param("sig()", "exactly one", None, id="sig-empty"),
        pytest.param('sig("")', "empty or padded", None, id="sig-blank"),
        pytest.param('sig(" a")', "empty or padded", None, id="sig-padded"),
        pytest.param('sig(id="a")', "keyword", None, id="sig-keyword"),
        pytest.param('abs(*[sig("a")])', "starred", None, id="starred"),
        pytest.param("abs()", "takes 1 argument", None, id="abs-arity"),
        pytest.param('min(sig("a"))', "at least 2", None, id="min-arity"),
        pytest.param("1 + 2", "at least one signal", None, id="no-signal"),
        pytest.param('sig("a") +', "syntax error", None, id="syntax"),
        pytest.param("", "empty", None, id="empty"),
        pytest.param("   ", "empty", None, id="blank"),
        pytest.param('sig("a")\n+ 1', "single line", None, id="newline"),
        pytest.param('sig("' + NON_ASCII + '")', "printable ASCII", None, id="non-ascii"),
        pytest.param('sig("a") + ' * 100 + "1", "too long", None, id="too-long"),
        pytest.param("-" * 40 + 'sig("a")', "nested too deeply", None, id="deep-unary"),
        pytest.param("(" * 300 + "1" + ")" * 300, "nested", None, id="deep-parens"),
        pytest.param('min(sig("a"), ' + "1, " * 250 + "1)", "too many parts", None, id="too-many"),
    ],
)
def test_invalid_expressions(expression: str, match: str, segment: str | None) -> None:
    with pytest.raises(ExpressionError, match=match) as caught:
        parse_expression(expression)
    if segment is not None:
        assert caught.value.segment == segment
        assert "column" in str(caught.value)


def test_error_column_counts_leading_spaces() -> None:
    with pytest.raises(ExpressionError) as caught:
        parse_expression('   sig("a") + x')
    assert caught.value.start == 14
    assert "column 15: x" in str(caught.value)


def test_expression_must_be_text() -> None:
    with pytest.raises(ExpressionError, match="string"):
        parse_expression(3)  # type: ignore[arg-type]


UNSAFE = [
    pytest.param("__import__('lbr_never_imported')", id="import"),
    pytest.param("__import__('os').system('exit 3')", id="system"),
    pytest.param("(lambda: __import__('lbr_never_imported'))()", id="lambda"),
    pytest.param("[__import__('lbr_never_imported') for _ in '1']", id="comprehension"),
    pytest.param("sig('a').__class__.__bases__[0].__subclasses__()", id="subclasses"),
    pytest.param("open('lbr_never_created.txt', 'w')", id="open"),
    pytest.param("exec('import lbr_never_imported')", id="exec"),
    pytest.param("eval('1')", id="eval"),
    pytest.param("sig('a') + (lambda: open('lbr_never_created.txt', 'w'))()", id="nested"),
]


class _ImportRecorder(importlib.abc.MetaPathFinder):
    """Record attempts to import the sentinel modules named in UNSAFE."""

    def __init__(self) -> None:
        self.attempts: list[str] = []

    def find_spec(self, fullname: str, path: object = None, target: object = None) -> None:
        if fullname.startswith("lbr_never"):
            self.attempts.append(fullname)
        return None


@pytest.mark.parametrize("expression", UNSAFE)
def test_unsafe_expressions_never_execute(
    expression: str, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    calls: list[str] = []
    real_compile = builtins.compile
    recorder = _ImportRecorder()

    def forbidden(name: str):
        def trap(*args: object, **kwargs: object) -> None:
            calls.append(name)
            raise AssertionError(f"{name} called")

        return trap

    def ast_only_compile(source: Any, filename: Any, mode: str, flags: int = 0, **kw: Any) -> Any:
        if not flags & ast.PyCF_ONLY_AST:
            calls.append("compile")
            raise AssertionError("compiled to code")
        return real_compile(source, filename, mode, flags, **kw)

    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(builtins, "eval", forbidden("eval"))
    monkeypatch.setattr(builtins, "exec", forbidden("exec"))
    monkeypatch.setattr(builtins, "compile", ast_only_compile)
    monkeypatch.setattr("os.system", forbidden("os.system"))
    monkeypatch.setattr(sys, "meta_path", [recorder, *sys.meta_path])
    with pytest.raises(ExpressionError):
        parse_expression(expression)
    with pytest.raises(ExpressionError):
        define(expression)
    monkeypatch.undo()
    assert calls == []
    assert recorder.attempts == []
    assert "lbr_never_imported" not in sys.modules
    assert list(tmp_path.iterdir()) == []


def test_module_never_calls_eval_exec_or_compile() -> None:
    tree = ast.parse(Path(derived.__file__).read_text(encoding="utf-8"))
    calls = [node.func for node in ast.walk(tree) if isinstance(node, ast.Call)]
    names = {func.id for func in calls if isinstance(func, ast.Name)}
    attributes = {
        ast.unparse(func)
        for func in calls
        if isinstance(func, ast.Attribute) and ast.unparse(func) != "re.compile"
    }
    assert not names & {"eval", "exec", "compile", "__import__", "getattr"}
    assert not {name.rsplit(".", 1)[-1] for name in attributes} & {
        "eval",
        "exec",
        "compile",
        "import_module",
        "literal_eval",
    }


# ---------------------------------------------------------------------------
# Definitions, cycles and dict round trip


def test_definition_defaults_and_inputs() -> None:
    definition = define('sig("b") * 2 + sig("a") - sig("b")')
    assert definition.inputs == ("b", "a")
    assert definition.reference_id == "b" == definition.reference
    assert definition.max_age_ns == 0
    explicit = define('sig("b") + sig("a")', inputs=["a", "b"], reference_id="b")
    assert explicit.inputs == ("a", "b")


@pytest.mark.parametrize(
    ("kwargs", "match"),
    [
        pytest.param({"output_id": ""}, "output_id", id="empty-id"),
        pytest.param({"output_id": " out"}, "output_id", id="padded-id"),
        pytest.param({"output_id": 3}, "output_id", id="int-id"),
        pytest.param({"name": " "}, "name", id="blank-name"),
        pytest.param({"inputs": ("a", "a")}, "duplicates", id="dup-inputs"),
        pytest.param({"inputs": ("a", "c")}, "must match", id="extra-input"),
        pytest.param({"inputs": ("b",)}, "must match", id="missing-input"),
        pytest.param({"inputs": "ab"}, "list of signal ids", id="inputs-str"),
        pytest.param({"inputs": ("a", "")}, "each input", id="blank-input"),
        pytest.param({"reference_id": "z"}, "reference_id", id="bad-reference"),
        pytest.param({"max_age_ns": -1}, ">= 0", id="negative-age"),
        pytest.param({"max_age_ns": 1.5}, "integer", id="float-age"),
        pytest.param({"max_age_ns": True}, "integer", id="bool-age"),
        pytest.param({"unit": "m//s"}, "unit", id="bad-unit"),
    ],
)
def test_definition_validation(kwargs: dict[str, object], match: str) -> None:
    fields: dict[str, object] = {
        "output_id": "out",
        "name": "Out",
        "expression": 'sig("a") + sig("b")',
    }
    fields.update(kwargs)
    with pytest.raises((DefinitionError, UnitError), match=match):
        DerivedSignalDefinition(**fields)  # type: ignore[arg-type]


def test_definition_is_frozen() -> None:
    definition = define('sig("a")')
    with pytest.raises(AttributeError):
        definition.expression = 'sig("b")'  # type: ignore[misc]


def test_self_reference_is_a_cycle() -> None:
    with pytest.raises(CycleError, match="speed -> speed"):
        define('sig("speed") * 2', output_id="speed")


@pytest.mark.parametrize(
    ("graph", "cycle"),
    [
        pytest.param({"x": "y", "y": "x"}, ("x", "y", "x"), id="two"),
        pytest.param({"x": "y", "y": "z", "z": "x"}, ("x", "y", "z", "x"), id="three"),
        pytest.param({"ok": "raw", "x": "y", "y": "z", "z": "y"}, ("y", "z", "y"), id="tail"),
    ],
)
def test_cycles_are_detected(graph: dict[str, str], cycle: tuple[str, ...]) -> None:
    definitions = [define(f'sig("{dep}") + 1', output_id=out) for out, dep in graph.items()]
    with pytest.raises(CycleError) as caught:
        order_definitions(definitions)
    assert caught.value.cycle == cycle
    assert " -> ".join(cycle) in str(caught.value)
    with pytest.raises(CycleError):
        evaluate_all(definitions, {"raw": series([0], [1.0])})


def test_dependency_order_and_chained_evaluation() -> None:
    speed = define('hypot(sig("vx"), sig("vy"))', output_id="speed", unit="m/s", max_age_ns=5)
    energy = define('0.5 * sig("mass") * sig("speed") * sig("speed")', output_id="energy")
    doubled = define('sig("energy") + sig("energy")', output_id="doubled", unit="kg*m^2/s^2")
    assert [d.output_id for d in order_definitions([doubled, energy, speed])] == [
        "speed",
        "energy",
        "doubled",
    ]
    raw = {
        "vx": series([0, 10, 20], [3.0, 6.0, None], "m/s"),
        "vy": series([0, 12], [4.0, 8.0], "m/s"),
        "mass": series([0, 10, 20], [2.0, 2.0, 2.0], "kg"),
    }
    results = evaluate_all([doubled, energy, speed], raw)
    assert results["speed"].values == (5.0, None, None)
    assert results["speed"].missing[1] == Missing(MissingReason.STALE_INPUT, "vy")
    assert results["speed"].missing[2] == Missing(MissingReason.MISSING_INPUT, "vx")
    assert results["energy"].unit == "kg*m^2/s^2"
    assert results["energy"].values == (25.0, None, None)
    assert results["energy"].missing[1] == Missing(MissingReason.MISSING_INPUT, "speed")
    assert results["doubled"].values == (50.0, None, None)
    assert set(raw) == {"vx", "vy", "mass"}


def test_duplicate_and_clashing_output_ids() -> None:
    with pytest.raises(DefinitionError, match="duplicate"):
        order_definitions([define('sig("a")'), define('sig("b")')])
    raw = {"a": series([0], [1.0]), "b": series([0], [1.0])}
    with pytest.raises(DefinitionError, match="raw signal"):
        evaluate_all([define('sig("a")', output_id="b")], raw)


def test_dict_round_trip_through_json() -> None:
    definition = define(
        f'hypot(sig("{TEMP}"), sig("{PRESSURE}"))',
        output_id="derived.magnitude",
        unit="1",
        reference_id=PRESSURE,
        max_age_ns=1_000_000,
    )
    data = json.loads(json.dumps(definition.to_dict()))
    assert data == {
        "version": 1,
        "output_id": "derived.magnitude",
        "name": "Output",
        "expression": f'hypot(sig("{TEMP}"), sig("{PRESSURE}"))',
        "unit": "1",
        "inputs": [TEMP, PRESSURE],
        "reference_id": PRESSURE,
        "max_age_ns": 1_000_000,
    }
    restored = DerivedSignalDefinition.from_dict(data)
    assert restored == definition
    assert restored.to_dict() == data


def _valid_dict() -> dict[str, object]:
    return define('sig("a") + 1').to_dict()


def _with(**changes: object) -> dict[str, object]:
    data = _valid_dict()
    data.update(changes)
    return data


def _without(key: str) -> dict[str, object]:
    data = _valid_dict()
    del data[key]
    return data


@pytest.mark.parametrize(
    ("data", "error", "match"),
    [
        pytest.param([1], DefinitionError, "mapping", id="not-mapping"),
        pytest.param(_without("name"), DefinitionError, "missing", id="missing-key"),
        pytest.param(_with(extra=1), DefinitionError, "unexpected", id="extra-key"),
        pytest.param(_with(version=2), DefinitionError, "version", id="version"),
        pytest.param(_with(version=True), DefinitionError, "version", id="version-bool"),
        pytest.param(_with(version=1.0), DefinitionError, "version", id="version-float"),
        pytest.param(_with(name=5), DefinitionError, "name", id="name-type"),
        pytest.param(_with(unit=5), DefinitionError, "unit", id="unit-type"),
        pytest.param(_with(inputs="a"), DefinitionError, "inputs", id="inputs-str"),
        pytest.param(_with(inputs=[]), DefinitionError, "inputs", id="inputs-empty"),
        pytest.param(_with(inputs=[1]), DefinitionError, "inputs", id="inputs-int"),
        pytest.param(_with(inputs=["b"]), DefinitionError, "must match", id="inputs-wrong"),
        pytest.param(_with(max_age_ns=False), DefinitionError, "max_age_ns", id="age-bool"),
        pytest.param(_with(max_age_ns="1"), DefinitionError, "max_age_ns", id="age-str"),
        pytest.param(_with(reference_id=None), DefinitionError, "reference_id", id="ref-none"),
        pytest.param(_with(expression="a.b"), ExpressionError, "attribute", id="expression"),
    ],
)
def test_from_dict_is_strict(data: object, error: type[Exception], match: str) -> None:
    with pytest.raises(error, match=match):
        DerivedSignalDefinition.from_dict(data)


def test_large_input_evaluates() -> None:
    count = 50_000
    times = list(range(0, count * 10, 10))
    fast = SignalInput(times, [float(i) for i in range(count)], "rpm")
    slow = SignalInput(times[::7], [2.0] * len(times[::7]), "1")
    definition = define('sig("fast") * sig("slow")', max_age_ns=70)
    result = evaluate(definition, {"fast": fast, "slow": slow})
    assert len(result) == count
    assert result.missing_count == 0
    assert result.values[-1] == 2.0 * (count - 1)
    assert result.unit == "rpm"


def test_documented_alignment_example() -> None:
    a = series([0, 10, 20, 30], [1.0, 1.0, 1.0, 1.0])
    b = series([0, 25], [2.0, 3.0])
    result = evaluate(define('sig("a") * sig("b")', max_age_ns=10), {"a": a, "b": b})
    assert result.values == (2.0, 2.0, None, 3.0)
    assert result.missing[2] == Missing(MissingReason.STALE_INPUT, "b")
