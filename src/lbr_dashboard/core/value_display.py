"""Turn one signal descriptor and its selected sample into display text.

``describe_value`` is a pure function: it takes a descriptor, an optional
sample, and an optional reference time, and returns a frozen ``ValueDisplay``
whose fields are ready to show. It never invents a value: no data, an
unavailable sample, an invalid sample, a stale sample, and an unsupported
value type each get their own ``ValueState`` and are spelled out in words.

The descriptor and sample are read through the ``DisplayDescriptor`` and
``DisplaySample`` protocols. ``StaticDescriptor`` and ``StaticSample`` are
minimal stand-ins for tests and mock data only; the real signal descriptor and
sample types come from the signal model (#18) and only need to provide the
same read-only attributes, directly or through a thin adapter.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from enum import Enum
from math import isfinite
from typing import Protocol

EMPTY_TEXT = "--"
DEFAULT_SIGNIFICANT_DIGITS = 6
MAX_DECIMAL_PLACES = 9
MAX_TEXT_LENGTH = 80
MAX_VECTOR_COMPONENTS = 16
# Above this magnitude fixed-point output gets long, so scientific notation is used.
FIXED_POINT_LIMIT = 1e15

NUMERIC = "numeric"
INTEGER = "integer"
BOOLEAN = "boolean"
TEXT = "text"
STATUS = "status"
VECTOR = "vector"

SUPPORTED_TYPES = frozenset({NUMERIC, INTEGER, BOOLEAN, TEXT, STATUS, VECTOR})
# Alternative spellings accepted for a supported type.
TYPE_ALIASES = {"string": TEXT, "float": NUMERIC, "int": INTEGER, "bool": BOOLEAN}


class ValueState(str, Enum):
    """Why a value is, or is not, shown."""

    OK = "ok"
    NO_DATA = "no_data"
    UNAVAILABLE = "unavailable"
    INVALID = "invalid"
    STALE = "stale"
    UNSUPPORTED_TYPE = "unsupported_type"


STATE_LABELS = {
    ValueState.OK: "OK",
    ValueState.NO_DATA: "NO DATA",
    ValueState.UNAVAILABLE: "UNAVAILABLE",
    ValueState.INVALID: "INVALID",
    ValueState.STALE: "STALE",
    ValueState.UNSUPPORTED_TYPE: "UNSUPPORTED TYPE",
}


class DisplayDescriptor(Protocol):
    """Read-only signal metadata needed to display a value."""

    @property
    def identifier(self) -> str: ...

    @property
    def display_name(self) -> str: ...

    @property
    def source_id(self) -> str: ...

    @property
    def data_type(self) -> str: ...

    @property
    def canonical_unit(self) -> str | None: ...

    @property
    def component_labels(self) -> Sequence[str]: ...

    @property
    def decimal_places(self) -> int | None: ...


class DisplaySample(Protocol):
    """Read-only view of the sample selected for display."""

    @property
    def timestamp_ns(self) -> int: ...

    @property
    def value(self) -> object: ...

    @property
    def available(self) -> bool: ...

    @property
    def quality(self) -> str | None: ...

    @property
    def source_id(self) -> str | None: ...


@dataclass(frozen=True)
class StaticDescriptor:
    """Minimal descriptor for tests and mock data; not a signal model."""

    identifier: str
    display_name: str
    source_id: str
    data_type: str
    canonical_unit: str | None = None
    component_labels: tuple[str, ...] = ()
    decimal_places: int | None = None


@dataclass(frozen=True)
class StaticSample:
    """Minimal sample for tests and mock data; not a signal model."""

    timestamp_ns: int
    value: object
    available: bool = True
    quality: str | None = None
    source_id: str | None = None


@dataclass(frozen=True)
class ValueDisplay:
    """Display-ready description of one signal value."""

    signal_id: str
    state: ValueState
    state_text: str
    name_text: str
    identity_text: str
    type_text: str
    value_text: str
    unit_text: str
    time_text: str
    quality_text: str
    timestamp_ns: int | None = None
    age_ns: int | None = None

    @property
    def has_value(self) -> bool:
        """True when ``value_text`` holds a decoded value (ok or stale)."""

        return self.state in (ValueState.OK, ValueState.STALE)


class _InvalidValue(Exception):
    """Internal: the sample value does not match the declared type."""


def describe_value(
    descriptor: DisplayDescriptor,
    sample: DisplaySample | None = None,
    *,
    now_ns: int | None = None,
    max_age_ns: int | None = None,
) -> ValueDisplay:
    """Return the display record for ``sample`` of the signal ``descriptor``.

    ``now_ns`` is the reference time (live clock or selected replay time) on
    the sample's time axis. With ``now_ns``, the sample age is reported and a
    sample after the reference time is invalid. With ``now_ns`` and
    ``max_age_ns``, a sample older than ``max_age_ns`` is stale; an age equal
    to the limit is not.
    """

    if max_age_ns is not None and (_not_int(max_age_ns) or max_age_ns < 0):
        raise ValueError("max_age_ns must be a non-negative integer")
    if now_ns is not None and _not_int(now_ns):
        raise ValueError("now_ns must be an integer")
    places = descriptor.decimal_places
    if places is not None and (_not_int(places) or not 0 <= places <= MAX_DECIMAL_PLACES):
        raise ValueError(f"decimal_places must be an integer from 0 to {MAX_DECIMAL_PLACES}")

    declared = descriptor.data_type
    value_type = TYPE_ALIASES.get(declared, declared)
    unit = descriptor.canonical_unit or ""

    def record(
        state: ValueState,
        value_text: str,
        quality_text: str,
        timestamp_ns: int | None = None,
        age_ns: int | None = None,
    ) -> ValueDisplay:
        if timestamp_ns is None:
            time_text = EMPTY_TEXT
        elif age_ns is None:
            time_text = format_seconds(timestamp_ns)
        else:
            time_text = f"{format_seconds(timestamp_ns)} (age {format_seconds(age_ns)})"
        return ValueDisplay(
            signal_id=descriptor.identifier,
            state=state,
            state_text=STATE_LABELS[state],
            name_text=descriptor.display_name or descriptor.identifier,
            identity_text=f"{descriptor.identifier} from {descriptor.source_id}",
            type_text=value_type if value_type in SUPPORTED_TYPES else f"{declared} (unknown)",
            value_text=value_text,
            unit_text=unit if value_type not in (BOOLEAN, TEXT, STATUS) else "",
            time_text=time_text,
            quality_text=quality_text,
            timestamp_ns=timestamp_ns,
            age_ns=age_ns,
        )

    timestamp_ns = None
    if sample is not None and not _not_int(sample.timestamp_ns):
        timestamp_ns = sample.timestamp_ns
    age_ns = None if now_ns is None or timestamp_ns is None else now_ns - timestamp_ns

    if value_type not in SUPPORTED_TYPES:
        return record(
            ValueState.UNSUPPORTED_TYPE,
            EMPTY_TEXT,
            f"unsupported value type '{_clean(str(declared))}'; value is not shown",
            timestamp_ns,
            age_ns,
        )
    if sample is None:
        return record(ValueState.NO_DATA, EMPTY_TEXT, "no sample yet")
    if timestamp_ns is None:
        return record(ValueState.INVALID, EMPTY_TEXT, "invalid: sample timestamp is not an integer")

    if sample.source_id is not None and sample.source_id != descriptor.source_id:
        return record(
            ValueState.INVALID,
            EMPTY_TEXT,
            f"invalid: sample is from source '{_clean(sample.source_id)}', "
            f"expected '{_clean(descriptor.source_id)}'",
            timestamp_ns,
            age_ns,
        )
    if not sample.available:
        reason = sample.quality or "no reason given"
        return record(
            ValueState.UNAVAILABLE,
            EMPTY_TEXT,
            f"unavailable: {_clean(reason)}",
            timestamp_ns,
            age_ns,
        )
    if age_ns is not None and age_ns < 0:
        return record(
            ValueState.INVALID,
            EMPTY_TEXT,
            f"invalid: sample time is {format_seconds(-age_ns)} after the reference time",
            timestamp_ns,
            age_ns,
        )
    try:
        text = _format_value(value_type, sample.value, descriptor)
    except _InvalidValue as error:
        return record(ValueState.INVALID, EMPTY_TEXT, f"invalid: {error}", timestamp_ns, age_ns)

    quality = "" if sample.quality is None else f"; quality {_clean(sample.quality)}"
    if age_ns is not None and max_age_ns is not None and age_ns > max_age_ns:
        return record(
            ValueState.STALE,
            text,
            f"stale: sample is {format_seconds(age_ns)} old "
            f"(limit {format_seconds(max_age_ns)}){quality}",
            timestamp_ns,
            age_ns,
        )
    return record(ValueState.OK, text, f"ok{quality}", timestamp_ns, age_ns)


def format_number(value: float, decimal_places: int | None = None) -> str:
    """Format a finite number with bounded precision."""

    if decimal_places is None or abs(value) >= FIXED_POINT_LIMIT:
        return f"{value:.{DEFAULT_SIGNIFICANT_DIGITS}g}"
    return f"{value:.{decimal_places}f}"


def format_seconds(value_ns: int) -> str:
    """Exact decimal seconds from integer nanoseconds, at least three decimals."""

    sign = "-" if value_ns < 0 else ""
    whole, fraction = divmod(abs(value_ns), 1_000_000_000)
    digits = f"{fraction:09d}".rstrip("0").ljust(3, "0")
    return f"{sign}{whole}.{digits} s"


def _format_value(value_type: str, value: object, descriptor: DisplayDescriptor) -> str:
    places = descriptor.decimal_places
    unit = descriptor.canonical_unit
    if value_type == NUMERIC:
        return _with_unit(format_number(_finite(value, "value"), places), unit)
    if value_type == INTEGER:
        return _with_unit(_format_integer(value), unit)
    if value_type == BOOLEAN:
        if not isinstance(value, bool):
            raise _InvalidValue(f"expected true or false, got {_kind(value)}")
        return "true" if value else "false"
    if value_type == TEXT:
        if not isinstance(value, str):
            raise _InvalidValue(f"expected text, got {_kind(value)}")
        return f'"{_clean(value)}"'
    if value_type == STATUS:
        if isinstance(value, str) and value.strip():
            return f"[{_clean(value)}]"
        if isinstance(value, int) and not isinstance(value, bool):
            return f"[code {value}]"
        raise _InvalidValue(f"expected a status label or code, got {_kind(value)}")
    return _format_vector(value, descriptor, places, unit)


def _format_integer(value: object) -> str:
    if isinstance(value, float) and isfinite(value) and value.is_integer():
        value = int(value)
    if _not_int(value):
        raise _InvalidValue(f"expected an integer, got {_kind(value)}")
    assert isinstance(value, int)
    if abs(value) >= FIXED_POINT_LIMIT:
        return f"{value:.{DEFAULT_SIGNIFICANT_DIGITS - 1}e}"
    return str(value)


def _format_vector(
    value: object, descriptor: DisplayDescriptor, places: int | None, unit: str | None
) -> str:
    if isinstance(value, (str, bytes, bytearray)) or not isinstance(value, Sequence):
        raise _InvalidValue(f"expected a sequence of numbers, got {_kind(value)}")
    if not value:
        raise _InvalidValue("vector has no components")
    labels = tuple(descriptor.component_labels)
    if labels and len(labels) != len(value):
        raise _InvalidValue(f"vector has {len(value)} components, expected {len(labels)}")
    parts = [
        format_number(_finite(item, f"component {index}"), places)
        for index, item in enumerate(value)
    ]
    if labels:
        parts = [f"{_clean(label)}={part}" for label, part in zip(labels, parts)]
    hidden = len(parts) - MAX_VECTOR_COMPONENTS
    if hidden > 0:
        parts = [*parts[:MAX_VECTOR_COMPONENTS], f"... (+{hidden} more)"]
    return _with_unit(f"({', '.join(parts)})", unit)


def _finite(value: object, what: str) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise _InvalidValue(f"{what}: expected a number, got {_kind(value)}")
    try:
        number = float(value)
    except OverflowError as error:
        raise _InvalidValue(f"{what}: number is too large") from error
    if not isfinite(number):
        raise _InvalidValue(f"{what}: number is not finite ({number})")
    return number


def _with_unit(text: str, unit: str | None) -> str:
    return f"{text} {unit}" if unit else text


def _not_int(value: object) -> bool:
    return isinstance(value, bool) or not isinstance(value, int)


def _kind(value: object) -> str:
    if value is None:
        return "no value"
    if isinstance(value, bool):
        return "a boolean"
    if isinstance(value, float) and not isfinite(value):
        return f"non-finite number ({value})"
    return type(value).__name__


def _clean(text: str) -> str:
    """Escape control characters and cap the length; other text is kept as is."""

    escaped = "".join(_escape(char) for char in text[: MAX_TEXT_LENGTH * 2])
    if len(escaped) > MAX_TEXT_LENGTH or len(text) > MAX_TEXT_LENGTH * 2:
        return escaped[: MAX_TEXT_LENGTH - 3] + "..."
    return escaped


def _escape(char: str) -> str:
    if char.isprintable():
        return char
    code = ord(char)
    return f"\\x{code:02x}" if code < 0x100 else f"\\u{code:04x}"
