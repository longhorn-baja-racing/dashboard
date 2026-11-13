"""Derived signals: a small allowlisted arithmetic language over telemetry signals.

An expression such as ``hypot(sig("imu.accel.x"), sig("imu.accel.y"))`` is
parsed with :func:`ast.parse` (``mode="eval"``) only to get a syntax tree. The
tree is checked against an allowlist and turned into a few plain node objects
that this module evaluates itself. Python's ``eval``/``exec`` are never used and
no code object is ever built, so an expression cannot run anything.

Grammar (see ``docs/derived-signals.md``):

- numbers (``2``, ``0.5``, ``1e-3``), ``+ - * /``, unary ``-`` and parentheses;
- ``sig("<signal id>")`` references an input signal by its generic id;
- ``abs(x)``, ``sqrt(x)``, ``min(a, b, ...)``, ``max(a, b, ...)``,
  ``hypot(a, b, ...)``.

Units are strings such as ``"m/s^2"``, ``"N*m"`` or ``"deg C"``; ``"1"`` is
dimensionless and ``None``, ``""`` or ``"unknown"`` mean the unit is unknown.
Mixed-rate inputs are aligned at the timestamps of one reference input with a
sample-and-hold lookup limited by ``max_age_ns``. Samples that cannot be
computed are kept as missing values with a :class:`Missing` reason.
"""

from __future__ import annotations

import ast
import math
import re
from bisect import bisect_left, bisect_right
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from enum import Enum
from itertools import pairwise
from typing import Any

SIGNAL_FUNCTION = "sig"
FUNCTIONS = ("abs", "sqrt", "min", "max", "hypot")
MAX_EXPRESSION_LENGTH = 1000
MAX_DEPTH = 32
MAX_NODES = 200
DIMENSIONLESS = "1"
UNKNOWN_UNIT = "unknown"
DEFINITION_VERSION = 1

# Name -> (minimum arguments, maximum arguments or None for no limit).
_ARITY: dict[str, tuple[int, int | None]] = {
    "abs": (1, 1),
    "sqrt": (1, 1),
    "min": (2, None),
    "max": (2, None),
    "hypot": (2, None),
}
_BINARY = {ast.Add: "+", ast.Sub: "-", ast.Mult: "*", ast.Div: "/"}
_OPERATOR_TEXT = {
    ast.Pow: "**",
    ast.Mod: "%",
    ast.FloorDiv: "//",
    ast.MatMult: "@",
    ast.LShift: "<<",
    ast.RShift: ">>",
    ast.BitOr: "|",
    ast.BitAnd: "&",
    ast.BitXor: "^",
    ast.UAdd: "unary +",
    ast.Not: "not",
    ast.Invert: "~",
}
_NODE_TEXT = {
    ast.Attribute: "attribute access",
    ast.Subscript: "indexing",
    ast.Lambda: "lambda",
    ast.ListComp: "comprehension",
    ast.SetComp: "comprehension",
    ast.DictComp: "comprehension",
    ast.GeneratorExp: "comprehension",
    ast.Compare: "comparison",
    ast.BoolOp: "boolean operator",
    ast.IfExp: "conditional expression",
    ast.NamedExpr: "assignment expression",
    ast.JoinedStr: "f-string",
    ast.Tuple: "tuple",
    ast.List: "list",
    ast.Dict: "dict",
    ast.Set: "set",
    ast.Starred: "starred argument",
    ast.Await: "await",
    ast.Yield: "yield",
    ast.YieldFrom: "yield",
}
_UNIT_NAME = re.compile(r"[A-Za-z%_][A-Za-z0-9%_.]*(?: [A-Za-z0-9%_.]+)*\Z")
_UNIT_FACTOR = re.compile(r"(?P<name>[^\^]+?)(?:\s*\^\s*(?P<exp>-?[0-9]+))?\Z")

UnitTerms = tuple[tuple[str, int], ...]


class DerivedSignalError(ValueError):
    """Base class for every derived-signal error."""


class ExpressionError(DerivedSignalError):
    """An expression is invalid. ``start``/``end`` index the offending text."""

    def __init__(
        self, message: str, expression: str = "", start: int | None = None, end: int | None = None
    ) -> None:
        self.reason = message
        self.expression = expression
        self.start = start
        self.end = end
        if start is not None:
            segment = expression[start:end]
            if len(segment) > 60:
                segment = segment[:57] + "..."
            message = f"{message} at column {start + 1}: {segment}"
        super().__init__(message)

    @property
    def segment(self) -> str | None:
        """The offending part of the expression, or ``None`` when not tied to one."""

        return None if self.start is None else self.expression[self.start : self.end]


class UnitError(ExpressionError):
    """Units are malformed, incompatible, or do not match the declared unit."""


class DefinitionError(DerivedSignalError):
    """A definition (or a set of definitions) is invalid."""


class CycleError(DefinitionError):
    """Definitions depend on each other in a loop. ``cycle`` lists the ids in order."""

    def __init__(self, cycle: Sequence[str]) -> None:
        self.cycle = tuple(cycle)
        super().__init__("derived signals form a cycle: " + " -> ".join(self.cycle))


class UnresolvedInputError(DerivedSignalError):
    """Evaluation was asked for without data for some inputs."""

    def __init__(self, output_id: str, missing: Sequence[str]) -> None:
        self.output_id = output_id
        self.missing = tuple(missing)
        names = ", ".join(repr(signal_id) for signal_id in self.missing)
        super().__init__(f"derived signal {output_id!r} has no data for input(s) {names}")


# ---------------------------------------------------------------------------
# Units


def parse_unit(text: str | None) -> UnitTerms | None:
    """Parse a unit string into sorted ``(name, exponent)`` terms.

    Returns ``None`` when the unit is unknown (``None``, ``""`` or ``"unknown"``)
    and ``()`` for dimensionless (``"1"``). Factors are separated by ``*`` and
    ``/`` and read left to right; ``^n`` gives an integer exponent, so
    ``"m/s/s"`` and ``"m*s^-2"`` both become ``(("m", 1), ("s", -2))``.
    """

    if text is None:
        return None
    if not isinstance(text, str):
        raise UnitError(f"unit must be a string or None, got {type(text).__name__}")
    stripped = text.strip()
    if stripped in ("", UNKNOWN_UNIT):
        return None
    exponents: dict[str, int] = {}
    parts = re.split(r"([*/])", stripped)
    signs = [1] + [1 if op == "*" else -1 for op in parts[1::2]]
    for sign, factor in zip(signs, parts[0::2]):
        factor = factor.strip()
        match = _UNIT_FACTOR.match(factor)
        if not factor or match is None:
            raise UnitError(f"malformed unit {text!r}: empty factor")
        name = " ".join(match.group("name").split())
        exponent = int(match.group("exp") or 1)
        if name == DIMENSIONLESS and match.group("exp") is None:
            continue
        if exponent == 0 or not _UNIT_NAME.match(name):
            raise UnitError(f"malformed unit {text!r}: bad factor {factor!r}")
        exponents[name] = exponents.get(name, 0) + sign * exponent
    return tuple(sorted((name, exp) for name, exp in exponents.items() if exp))


def format_unit(terms: UnitTerms | None) -> str:
    """Format parsed terms canonically: ``"kg*m/s^2"``, ``"1/s"``, ``"1"``, ``"unknown"``."""

    if terms is None:
        return UNKNOWN_UNIT
    if not terms:
        return DIMENSIONLESS

    def power(name: str, exp: int) -> str:
        return name if exp == 1 else f"{name}^{exp}"

    upper = "*".join(power(name, exp) for name, exp in terms if exp > 0) or DIMENSIONLESS
    lower = "".join("/" + power(name, -exp) for name, exp in terms if exp < 0)
    return upper + lower


def normalize_unit(text: str | None) -> str:
    """Return the canonical spelling of a unit string (see :func:`format_unit`)."""

    return format_unit(parse_unit(text))


# ---------------------------------------------------------------------------
# Expressions


class MissingReason(Enum):
    """Why an output sample has no value."""

    MISSING_INPUT = "missing_input"  # the selected input sample is None, NaN or +/-inf
    STALE_INPUT = "stale_input"  # no input sample at or within max_age_ns before t
    DIVISION_BY_ZERO = "division_by_zero"
    DOMAIN_ERROR = "domain_error"  # sqrt of a negative number
    NON_FINITE = "non_finite"  # an intermediate result overflowed to +/-inf


@dataclass(frozen=True, slots=True)
class Missing:
    """A missing output sample: the reason, the input involved, and where."""

    reason: MissingReason
    signal_id: str | None = None
    where: str | None = None


class _Fail(Exception):
    def __init__(self, missing: Missing) -> None:
        self.missing = missing


class _Node:
    __slots__ = ()


@dataclass(frozen=True, slots=True)
class _Const(_Node):
    value: float
    span: tuple[int, int]


@dataclass(frozen=True, slots=True)
class _Ref(_Node):
    signal_id: str
    span: tuple[int, int]


@dataclass(frozen=True, slots=True)
class _Neg(_Node):
    operand: _Node
    span: tuple[int, int]


@dataclass(frozen=True, slots=True)
class _Binary(_Node):
    op: str
    left: _Node
    right: _Node
    span: tuple[int, int]
    where: str


@dataclass(frozen=True, slots=True)
class _Call(_Node):
    name: str
    args: tuple[_Node, ...]
    span: tuple[int, int]
    where: str


def _describe(node: ast.AST) -> str:
    return _NODE_TEXT.get(type(node), type(node).__name__)


class _Builder:
    """Check a syntax tree against the allowlist and convert it to plain nodes."""

    def __init__(self, text: str, offset: int) -> None:
        self.text = text
        self.offset = offset
        self.count = 0
        self.signal_ids: list[str] = []

    def span(self, node: ast.expr | ast.keyword) -> tuple[int, int]:
        start = node.col_offset + self.offset
        end = node.end_col_offset
        return start, len(self.text) if end is None else end + self.offset

    def error(self, message: str, node: ast.expr | ast.keyword) -> ExpressionError:
        start, end = self.span(node)
        return ExpressionError(message, self.text, start, end)

    def build(self, node: ast.expr, depth: int) -> _Node:
        if depth > MAX_DEPTH:
            raise self.error(f"expression is nested too deeply (limit {MAX_DEPTH})", node)
        self.count += 1
        if self.count > MAX_NODES:
            raise self.error(f"expression has too many parts (limit {MAX_NODES})", node)
        span = self.span(node)
        if isinstance(node, ast.Constant):
            return _Const(self.number(node), span)
        if isinstance(node, ast.UnaryOp):
            if not isinstance(node.op, ast.USub):
                text = _OPERATOR_TEXT.get(type(node.op), type(node.op).__name__)
                raise self.error(f"operator {text} is not allowed", node)
            return _Neg(self.build(node.operand, depth + 1), span)
        if isinstance(node, ast.BinOp):
            op = _BINARY.get(type(node.op))
            if op is None:
                text = _OPERATOR_TEXT.get(type(node.op), type(node.op).__name__)
                raise self.error(f"operator {text} is not allowed; use + - * /", node)
            left = self.build(node.left, depth + 1)
            right = self.build(node.right, depth + 1)
            return _Binary(op, left, right, span, self.text[span[0] : span[1]])
        if isinstance(node, ast.Call):
            return self.call(node, span, depth)
        if isinstance(node, ast.Name):
            raise self.error(
                f'unknown name {node.id!r}; reference signals as {SIGNAL_FUNCTION}("<id>")', node
            )
        raise self.error(f"{_describe(node)} is not allowed", node)

    def number(self, node: ast.Constant) -> float:
        value = node.value
        if isinstance(value, str):
            raise self.error(
                f'text is only allowed as the signal id in {SIGNAL_FUNCTION}("<id>")', node
            )
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            raise self.error("only real numbers are allowed as constants", node)
        try:
            number = float(value)
        except OverflowError:
            raise self.error("number is too large", node) from None
        if not math.isfinite(number):
            raise self.error("number is too large", node)
        return number

    def call(self, node: ast.Call, span: tuple[int, int], depth: int) -> _Node:
        allowed = ", ".join((SIGNAL_FUNCTION, *FUNCTIONS))
        if not isinstance(node.func, ast.Name):
            raise self.error(f"only calls to {allowed} are allowed", node)
        name = node.func.id
        if name != SIGNAL_FUNCTION and name not in _ARITY:
            raise self.error(f"unknown function {name!r}; allowed: {allowed}", node.func)
        if node.keywords:
            raise self.error(f"{name}() takes no keyword arguments", node.keywords[0])
        for arg in node.args:
            if isinstance(arg, ast.Starred):
                raise self.error(f"{_describe(arg)} is not allowed", arg)
        if name == SIGNAL_FUNCTION:
            return self.reference(node, span)
        low, high = _ARITY[name]
        count = len(node.args)
        if count < low or (high is not None and count > high):
            wanted = str(low) if low == high else f"at least {low}"
            raise self.error(f"{name}() takes {wanted} argument(s), got {count}", node)
        args = tuple(self.build(arg, depth + 1) for arg in node.args)
        return _Call(name, args, span, self.text[span[0] : span[1]])

    def reference(self, node: ast.Call, span: tuple[int, int]) -> _Node:
        usage = f'{SIGNAL_FUNCTION}("<signal id>")'
        if len(node.args) != 1:
            raise self.error(f"{SIGNAL_FUNCTION}() takes exactly one signal id: {usage}", node)
        arg = node.args[0]
        if not isinstance(arg, ast.Constant) or not isinstance(arg.value, str):
            raise self.error(f"the signal id must be a quoted text literal: {usage}", arg)
        signal_id = arg.value
        if not signal_id or signal_id != signal_id.strip():
            raise self.error("signal id must not be empty or padded with whitespace", arg)
        if signal_id not in self.signal_ids:
            self.signal_ids.append(signal_id)
        return _Ref(signal_id, span)


@dataclass(frozen=True, slots=True)
class _Unit:
    terms: UnitTerms | None
    constant: bool


class Expression:
    """A validated expression. Build one with :func:`parse_expression`."""

    __slots__ = ("text", "signal_ids", "_root")

    def __init__(self, text: str, signal_ids: tuple[str, ...], root: _Node) -> None:
        self.text = text
        self.signal_ids = signal_ids
        self._root = root

    def __repr__(self) -> str:
        return f"Expression({self.text!r})"

    def infer_unit(self, units: Mapping[str, str | None]) -> str:
        """Return the result unit for the given input units, or raise :class:`UnitError`.

        ``units`` maps each referenced signal id to its unit string. A missing
        entry counts as an unknown unit.
        """

        parsed = {}
        for signal_id in self.signal_ids:
            try:
                parsed[signal_id] = parse_unit(units.get(signal_id))
            except UnitError as error:
                raise UnitError(f"input {signal_id!r}: {error}") from None
        return format_unit(self._unit(self._root, parsed).terms)

    def _unit_error(self, message: str, span: tuple[int, int]) -> UnitError:
        return UnitError(message, self.text, span[0], span[1])

    def _unit(self, node: _Node, units: Mapping[str, UnitTerms | None]) -> _Unit:
        if isinstance(node, _Const):
            return _Unit((), True)
        if isinstance(node, _Ref):
            return _Unit(units[node.signal_id], False)
        if isinstance(node, _Neg):
            return self._unit(node.operand, units)
        if isinstance(node, _Binary):
            left = self._unit(node.left, units)
            right = self._unit(node.right, units)
            if node.op in "+-":
                return self._same([left, right], node.span, f"'{node.op}'")
            constant = left.constant and right.constant
            if left.terms is None or right.terms is None:
                return _Unit(None, constant)
            sign = 1 if node.op == "*" else -1
            merged = dict(left.terms)
            for name, exp in right.terms:
                merged[name] = merged.get(name, 0) + sign * exp
            return _Unit(tuple(sorted((n, e) for n, e in merged.items() if e)), constant)
        assert isinstance(node, _Call)
        args = [self._unit(arg, units) for arg in node.args]
        if node.name == "sqrt":
            (arg,) = args
            if arg.terms is None:
                return arg
            odd = [name for name, exp in arg.terms if exp % 2]
            if odd:
                raise self._unit_error(
                    f"sqrt() needs even unit powers, got {format_unit(arg.terms)!r}", node.span
                )
            return _Unit(tuple((name, exp // 2) for name, exp in arg.terms), arg.constant)
        return self._same(args, node.span, f"{node.name}()")

    def _same(self, args: list[_Unit], span: tuple[int, int], what: str) -> _Unit:
        """All non-constant operands must share one unit; constants adopt it."""

        measured = [arg for arg in args if not arg.constant]
        if not measured:
            return _Unit((), True)
        if any(arg.terms is None for arg in measured):
            return _Unit(None, False)
        first = measured[0].terms
        for arg in measured[1:]:
            if arg.terms != first:
                raise self._unit_error(
                    f"{what} needs identical units, got {format_unit(first)!r} "
                    f"and {format_unit(arg.terms)!r}",
                    span,
                )
        return _Unit(first, False)

    def evaluate_row(self, values: Mapping[str, float]) -> float:
        """Evaluate once with finite input values; raise :class:`EvaluationFailure`."""

        try:
            return _compute(self._root, values)
        except _Fail as failure:
            raise EvaluationFailure(failure.missing) from None


class EvaluationFailure(DerivedSignalError):
    """A single evaluation produced no value; ``missing`` says why."""

    def __init__(self, missing: Missing) -> None:
        self.missing = missing
        super().__init__(f"{missing.reason.value} in {missing.where}")


def _compute(node: _Node, row: Mapping[str, float]) -> float:
    if isinstance(node, _Const):
        return node.value
    if isinstance(node, _Ref):
        return row[node.signal_id]
    if isinstance(node, _Neg):
        return -_compute(node.operand, row)
    if isinstance(node, _Binary):
        left = _compute(node.left, row)
        right = _compute(node.right, row)
        if node.op == "+":
            result = left + right
        elif node.op == "-":
            result = left - right
        elif node.op == "*":
            result = left * right
        else:
            if right == 0:
                raise _Fail(Missing(MissingReason.DIVISION_BY_ZERO, where=node.where))
            result = left / right
        return _finite(result, node.where)
    assert isinstance(node, _Call)
    args = [_compute(arg, row) for arg in node.args]
    if node.name == "abs":
        return abs(args[0])
    if node.name == "sqrt":
        if args[0] < 0:
            raise _Fail(Missing(MissingReason.DOMAIN_ERROR, where=node.where))
        return math.sqrt(args[0])
    if node.name == "min":
        return min(args)
    if node.name == "max":
        return max(args)
    return _finite(math.hypot(*args), node.where)


def _finite(value: float, where: str) -> float:
    if not math.isfinite(value):
        raise _Fail(Missing(MissingReason.NON_FINITE, where=where))
    return value


def parse_expression(text: str) -> Expression:
    """Validate ``text`` against the allowlist and return an :class:`Expression`.

    Raises :class:`ExpressionError` with the column and text of the offending
    part. Nothing in the expression is executed.
    """

    if not isinstance(text, str):
        raise ExpressionError(f"expression must be a string, got {type(text).__name__}")
    if len(text) > MAX_EXPRESSION_LENGTH:
        raise ExpressionError(
            f"expression is too long ({len(text)} characters, limit {MAX_EXPRESSION_LENGTH})"
        )
    for index, char in enumerate(text):
        if not (" " <= char <= "~"):
            raise ExpressionError(
                "expression must be a single line of printable ASCII", text, index, index + 1
            )
    stripped = text.strip()
    if not stripped:
        raise ExpressionError("expression is empty")
    offset = len(text) - len(text.lstrip())
    try:
        tree = ast.parse(stripped, mode="eval")
    except SyntaxError as error:
        column = offset + max((error.offset or 1) - 1, 0)
        column = min(column, len(text) - 1)
        raise ExpressionError(f"syntax error: {error.msg}", text, column, column + 1) from None
    except (ValueError, RecursionError, MemoryError) as error:  # pragma: no cover - defensive
        raise ExpressionError(f"cannot parse expression: {error}") from None
    builder = _Builder(text, offset)
    root = builder.build(tree.body, 1)
    if not builder.signal_ids:
        raise ExpressionError(
            f'expression must reference at least one signal with {SIGNAL_FUNCTION}("<id>")'
        )
    return Expression(text, tuple(builder.signal_ids), root)


# ---------------------------------------------------------------------------
# Definitions


def _check_id(label: str, value: object) -> str:
    if not isinstance(value, str) or not value or value != value.strip():
        raise DefinitionError(f"{label} must be a non-empty string without surrounding spaces")
    return value


@dataclass(frozen=True, slots=True)
class DerivedSignalDefinition:
    """A declarative derived signal.

    ``inputs`` defaults to the signal ids referenced by ``expression`` in order
    of first use; when given it must list exactly those ids. ``reference_id``
    (default: the first input) supplies the output timestamps, and other inputs
    are held for at most ``max_age_ns`` (0 = exact timestamp match). ``unit``
    is the declared output unit; ``None`` accepts the inferred unit.
    """

    output_id: str
    name: str
    expression: str
    unit: str | None = None
    inputs: tuple[str, ...] = ()
    reference_id: str | None = None
    max_age_ns: int = 0
    parsed: Expression = field(init=False, repr=False, compare=False)

    def __post_init__(self) -> None:
        _check_id("output_id", self.output_id)
        if not isinstance(self.name, str) or not self.name.strip():
            raise DefinitionError("name must be a non-empty string")
        parsed = parse_expression(self.expression)
        object.__setattr__(self, "parsed", parsed)
        if self.unit is not None:
            if parse_unit(self.unit) is None:
                raise DefinitionError(
                    f"declared unit {self.unit!r} is not a unit; use None to accept the "
                    "inferred unit"
                )
        if not isinstance(self.inputs, (tuple, list)):
            raise DefinitionError("inputs must be a list of signal ids")
        inputs = tuple(self.inputs) or parsed.signal_ids
        for signal_id in inputs:
            _check_id("each input", signal_id)
        if len(set(inputs)) != len(inputs):
            raise DefinitionError(f"inputs contain duplicates: {list(inputs)}")
        unused = [s for s in inputs if s not in parsed.signal_ids]
        undeclared = [s for s in parsed.signal_ids if s not in inputs]
        if unused or undeclared:
            raise DefinitionError(
                f"inputs must match the expression: not used {unused}, not listed {undeclared}"
            )
        object.__setattr__(self, "inputs", inputs)
        if self.output_id in inputs:
            raise CycleError((self.output_id, self.output_id))
        reference = inputs[0] if self.reference_id is None else self.reference_id
        if reference not in inputs:
            raise DefinitionError(f"reference_id {reference!r} is not one of the inputs")
        object.__setattr__(self, "reference_id", reference)
        if isinstance(self.max_age_ns, bool) or not isinstance(self.max_age_ns, int):
            raise DefinitionError("max_age_ns must be an integer")
        if self.max_age_ns < 0:
            raise DefinitionError(f"max_age_ns must be >= 0, got {self.max_age_ns}")

    @property
    def reference(self) -> str:
        """The input whose timestamps the output uses."""

        assert self.reference_id is not None
        return self.reference_id

    def output_unit(self, input_units: Mapping[str, str | None]) -> str:
        """Check units for these inputs and return the canonical output unit.

        Raises :class:`UnitError` when units are incompatible, when the
        declared unit differs from the inferred one, or when a declared unit
        cannot be confirmed because an input unit is unknown.
        """

        inferred = self.parsed.infer_unit(input_units)
        if self.unit is None:
            return inferred
        declared = normalize_unit(self.unit)
        if inferred == UNKNOWN_UNIT:
            unknown = [s for s in self.inputs if parse_unit(input_units.get(s)) is None]
            raise UnitError(
                f"cannot confirm declared unit {declared!r} for {self.output_id!r}: "
                f"input(s) {unknown} have an unknown unit"
            )
        if declared != inferred:
            raise UnitError(
                f"declared unit {declared!r} for {self.output_id!r} does not match "
                f"the expression unit {inferred!r}"
            )
        return declared

    def to_dict(self) -> dict[str, Any]:
        """Return a JSON-compatible dict that :meth:`from_dict` accepts."""

        return {
            "version": DEFINITION_VERSION,
            "output_id": self.output_id,
            "name": self.name,
            "expression": self.expression,
            "unit": self.unit,
            "inputs": list(self.inputs),
            "reference_id": self.reference,
            "max_age_ns": self.max_age_ns,
        }

    @classmethod
    def from_dict(cls, data: object) -> DerivedSignalDefinition:
        """Build a definition from :meth:`to_dict` output, rejecting anything else."""

        if not isinstance(data, Mapping):
            raise DefinitionError(f"definition must be a mapping, got {type(data).__name__}")
        expected = set(_FIELD_TYPES) | {"version"}
        keys = set(data)
        if keys != expected:
            raise DefinitionError(
                f"definition keys are wrong: missing {sorted(expected - keys)}, "
                f"unexpected {sorted(map(str, keys - expected))}"
            )
        version = data["version"]
        if type(version) is not int or version != DEFINITION_VERSION:
            raise DefinitionError(f"unsupported definition version {version!r}")
        for key, types in _FIELD_TYPES.items():
            value = data[key]
            if isinstance(value, bool) or not isinstance(value, types):
                raise DefinitionError(f"{key} has the wrong type: {type(value).__name__}")
        inputs = data["inputs"]
        if not inputs or not all(isinstance(item, str) for item in inputs):
            raise DefinitionError("inputs must be a non-empty list of strings")
        return cls(
            output_id=data["output_id"],
            name=data["name"],
            expression=data["expression"],
            unit=data["unit"],
            inputs=tuple(inputs),
            reference_id=data["reference_id"],
            max_age_ns=data["max_age_ns"],
        )


_FIELD_TYPES: dict[str, tuple[type, ...]] = {
    "output_id": (str,),
    "name": (str,),
    "expression": (str,),
    "unit": (str, type(None)),
    "inputs": (list, tuple),
    "reference_id": (str,),
    "max_age_ns": (int,),
}


def order_definitions(
    definitions: Iterable[DerivedSignalDefinition],
) -> tuple[DerivedSignalDefinition, ...]:
    """Return definitions so each comes after the derived outputs it uses.

    Raises :class:`DefinitionError` for duplicate output ids and
    :class:`CycleError` (naming the loop) when definitions depend on each other
    in a cycle. Inputs that are not outputs of these definitions are raw signals.
    """

    by_id: dict[str, DerivedSignalDefinition] = {}
    for definition in definitions:
        if definition.output_id in by_id:
            raise DefinitionError(f"duplicate derived output id {definition.output_id!r}")
        by_id[definition.output_id] = definition
    ordered: list[DerivedSignalDefinition] = []
    done: set[str] = set()
    for root in by_id:
        if root in done:
            continue
        path = [root]
        stack = [iter(by_id[root].inputs)]
        while stack:
            child = next(stack[-1], None)
            if child is None:
                stack.pop()
                finished = path.pop()
                done.add(finished)
                ordered.append(by_id[finished])
            elif child in path:
                raise CycleError((*path[path.index(child) :], child))
            elif child in by_id and child not in done:
                path.append(child)
                stack.append(iter(by_id[child].inputs))
    return tuple(ordered)


# ---------------------------------------------------------------------------
# Evaluation


@dataclass(frozen=True, slots=True)
class SignalInput:
    """One input signal: non-decreasing ``times_ns``, ``values`` and a unit.

    ``None``, NaN and +/-inf values are missing. The sequences are read, never
    modified or copied.
    """

    times_ns: Sequence[int]
    values: Sequence[float | None]
    unit: str | None = None

    def __post_init__(self) -> None:
        if len(self.times_ns) != len(self.values):
            raise ValueError(
                f"{len(self.times_ns)} timestamps but {len(self.values)} values; "
                "they must be the same length"
            )
        for index, (earlier, later) in enumerate(pairwise(self.times_ns)):
            if later < earlier:
                raise ValueError(
                    f"timestamps must be non-decreasing; index {index + 1} ({later}) "
                    f"is before index {index} ({earlier})"
                )
        parse_unit(self.unit)


@dataclass(frozen=True, slots=True)
class DerivedSeries:
    """Evaluated output: one entry per reference sample in the window.

    ``values[i]`` is ``None`` exactly when ``missing[i]`` explains why.
    """

    output_id: str
    unit: str
    times_ns: tuple[int, ...]
    values: tuple[float | None, ...]
    missing: tuple[Missing | None, ...]
    reference_id: str
    max_age_ns: int

    def __len__(self) -> int:
        return len(self.times_ns)

    @property
    def missing_count(self) -> int:
        return sum(item is not None for item in self.missing)

    def as_input(self) -> SignalInput:
        """Use this output as an input to another derived signal."""

        return SignalInput(self.times_ns, self.values, self.unit)


def evaluate(
    definition: DerivedSignalDefinition,
    inputs: Mapping[str, SignalInput],
    *,
    start_ns: int | None = None,
    end_ns: int | None = None,
) -> DerivedSeries:
    """Evaluate ``definition`` at each reference sample with ``start_ns <= t <= end_ns``.

    Each other input contributes its latest sample at or before ``t`` if it is
    at most ``max_age_ns`` old (equal timestamps: the last one in input order).
    The output at ``t`` is missing when any input sample is missing or stale,
    or the arithmetic fails; the first failing input in ``inputs`` order is
    reported. Raises :class:`UnresolvedInputError` for absent inputs and
    :class:`UnitError` before computing anything if units do not check.
    """

    absent = [signal_id for signal_id in definition.inputs if signal_id not in inputs]
    if absent:
        raise UnresolvedInputError(definition.output_id, absent)
    if start_ns is not None and end_ns is not None and start_ns > end_ns:
        raise ValueError(f"start_ns ({start_ns}) is after end_ns ({end_ns})")
    unit = definition.output_unit({s: inputs[s].unit for s in definition.inputs})
    reference_id = definition.reference
    reference = inputs[reference_id]
    max_age = definition.max_age_ns
    ref_times = reference.times_ns
    first = 0 if start_ns is None else bisect_left(ref_times, start_ns)
    stop = len(ref_times) if end_ns is None else bisect_right(ref_times, end_ns)

    order = definition.inputs
    signals = [inputs[signal_id] for signal_id in order]
    lows = [0] * len(order)
    stale = [Missing(MissingReason.STALE_INPUT, signal_id) for signal_id in order]
    absent_value = [Missing(MissingReason.MISSING_INPUT, signal_id) for signal_id in order]
    root = definition.parsed
    row: dict[str, float] = {}
    times: list[int] = []
    values: list[float | None] = []
    missing: list[Missing | None] = []
    for index in range(first, stop):
        t_ns = ref_times[index]
        problem: Missing | None = None
        for slot, signal_id in enumerate(order):
            signal = signals[slot]
            if signal_id == reference_id:
                found = index
            else:
                found = bisect_right(signal.times_ns, t_ns, lows[slot]) - 1
                if found < 0 or t_ns - signal.times_ns[found] > max_age:
                    problem = stale[slot]
                    break
                lows[slot] = found
            value = signal.values[found]
            if value is None or not math.isfinite(value):
                problem = absent_value[slot]
                break
            row[signal_id] = float(value)
        result: float | None = None
        if problem is None:
            try:
                result = root.evaluate_row(row)
            except EvaluationFailure as failure:
                problem = failure.missing
        times.append(t_ns)
        values.append(result)
        missing.append(problem)
    return DerivedSeries(
        output_id=definition.output_id,
        unit=unit,
        times_ns=tuple(times),
        values=tuple(values),
        missing=tuple(missing),
        reference_id=reference_id,
        max_age_ns=max_age,
    )


def evaluate_all(
    definitions: Iterable[DerivedSignalDefinition],
    inputs: Mapping[str, SignalInput],
    *,
    start_ns: int | None = None,
    end_ns: int | None = None,
) -> dict[str, DerivedSeries]:
    """Evaluate definitions that may use each other's outputs, in dependency order.

    Raw ``inputs`` are never modified. A derived output id may not reuse a raw
    signal id. Every output uses the same window, so a derived input has no
    samples before ``start_ns`` to hold.
    """

    ordered = order_definitions(definitions)
    clashes = [d.output_id for d in ordered if d.output_id in inputs]
    if clashes:
        raise DefinitionError(f"derived output id(s) {clashes} are also raw signal ids")
    available: dict[str, SignalInput] = dict(inputs)
    results: dict[str, DerivedSeries] = {}
    for definition in ordered:
        series = evaluate(definition, available, start_ns=start_ns, end_ns=end_ns)
        results[definition.output_id] = series
        available[definition.output_id] = series.as_input()
    return results
