"""Current values and basic interval statistics for one numeric signal.

Inputs are plain parallel sequences: ``times_ns`` holds integer nanosecond
timestamps in non-decreasing order and ``values`` holds one reading per
timestamp. Each reading is classified as:

- valid: a finite number;
- missing: ``None`` or NaN (no reading was available);
- invalid: positive or negative infinity (a reading that cannot be aggregated).

Only valid readings contribute to minimum, maximum, and mean. Missing and
invalid readings are counted, never replaced by zero or by an older value.

Intervals are closed: a sample at exactly ``start_ns`` or ``end_ns`` is inside.
Statistics are sample-weighted: every valid sample counts once, regardless of
how much time separates it from its neighbors, so a signal logged at a higher
rate contributes more samples than a slower one over the same interval.
"""

from __future__ import annotations

from bisect import bisect_left, bisect_right
from collections.abc import Sequence
from dataclasses import dataclass
from enum import Enum
from math import fsum, isfinite, isnan
from typing import Literal

SIGNIFICANT_DIGITS = 6
EMPTY_TEXT = "--"
NO_VALID_TEXT = "-- (no valid samples)"
WEIGHTING_TEXT = "per sample: each valid sample counts once; time between samples is ignored"
COUNT_DEFINITION_TEXT = (
    "valid = finite value; missing = no value or NaN; invalid = +/-inf; "
    "closed interval, samples at both ends included"
)


@dataclass(frozen=True, slots=True)
class IntervalStatistics:
    """Sample-weighted statistics for the samples inside a closed interval.

    ``minimum``, ``maximum``, ``mean``, ``first_valid_ns``, and ``last_valid_ns``
    are ``None`` when the interval holds no valid sample; they are never zero
    as a stand-in for absent data.
    """

    start_ns: int
    end_ns: int
    unit: str
    valid_count: int
    missing_count: int
    invalid_count: int
    minimum: float | None
    maximum: float | None
    mean: float | None
    first_valid_ns: int | None
    last_valid_ns: int | None
    weighting: Literal["sample"] = "sample"

    @property
    def sample_count(self) -> int:
        """All samples inside the interval, whatever their classification."""

        return self.valid_count + self.missing_count + self.invalid_count


class CurrentValueState(Enum):
    """How the reading at a point in time should be presented."""

    VALID = "valid"
    STALE = "stale"
    MISSING = "missing"
    INVALID = "invalid"
    NO_DATA = "no data"


@dataclass(frozen=True, slots=True)
class CurrentValue:
    """The latest sample at or before ``at_ns`` and how trustworthy it is.

    ``value`` is set only for VALID and STALE. ``time_ns`` and ``age_ns`` refer
    to the latest sample at or before ``at_ns`` and are ``None`` for NO_DATA.
    """

    state: CurrentValueState
    at_ns: int
    unit: str
    time_ns: int | None = None
    value: float | None = None
    age_ns: int | None = None
    max_age_ns: int | None = None


def _check_lengths(times_ns: Sequence[int], values: Sequence[float | None]) -> None:
    if len(times_ns) != len(values):
        raise ValueError(
            f"times_ns and values must have the same length ({len(times_ns)} != {len(values)})"
        )


def _out_of_order(index: int, times_ns: Sequence[int]) -> ValueError:
    return ValueError(
        f"times_ns must be non-decreasing: sample {index} ({times_ns[index]}) "
        f"comes after {times_ns[index - 1]}"
    )


def interval_statistics(
    times_ns: Sequence[int],
    values: Sequence[float | None],
    start_ns: int,
    end_ns: int,
    *,
    unit: str = "",
) -> IntervalStatistics:
    """Summarize the samples with ``start_ns <= time <= end_ns``.

    The interval is located by binary search, so cost grows with the number of
    samples inside it, not with the length of the whole series. ``times_ns``
    must be non-decreasing; ordering is verified for the selected samples and a
    ``ValueError`` is raised when it is violated there. Out-of-order samples
    elsewhere in the series are not detected.
    """

    _check_lengths(times_ns, values)
    if start_ns > end_ns:
        raise ValueError(f"start_ns must not be after end_ns ({start_ns} > {end_ns})")

    lo = bisect_left(times_ns, start_ns)
    hi = bisect_right(times_ns, end_ns, lo)
    # Binary search already guarantees times_ns[lo - 1] < start_ns <= times_ns[lo] and
    # times_ns[hi - 1] <= end_ns < times_ns[hi], so only the inside needs checking.
    for index in range(lo + 1, hi):
        if times_ns[index] < times_ns[index - 1]:
            raise _out_of_order(index, times_ns)

    valid: list[float] = []
    missing = 0
    invalid = 0
    first_valid: int | None = None
    last_valid: int | None = None
    for index in range(lo, hi):
        value = values[index]
        if value is None or isnan(value):
            missing += 1
        elif not isfinite(value):
            invalid += 1
        else:
            valid.append(value)
            if first_valid is None:
                first_valid = times_ns[index]
            last_valid = times_ns[index]

    minimum = maximum = mean = None
    if valid:
        minimum = min(valid)
        maximum = max(valid)
        count = len(valid)
        try:
            mean = fsum(valid) / count
        except OverflowError:
            # The exact sum exceeds the float range; scaling first keeps the mean finite.
            mean = fsum(value / count for value in valid)

    return IntervalStatistics(
        start_ns=start_ns,
        end_ns=end_ns,
        unit=unit,
        valid_count=len(valid),
        missing_count=missing,
        invalid_count=invalid,
        minimum=minimum,
        maximum=maximum,
        mean=mean,
        first_valid_ns=first_valid,
        last_valid_ns=last_valid,
    )


def current_value(
    times_ns: Sequence[int],
    values: Sequence[float | None],
    at_ns: int,
    *,
    max_age_ns: int | None = None,
    unit: str = "",
) -> CurrentValue:
    """Return the latest sample at or before ``at_ns`` with its state.

    When several samples share the latest timestamp, the last one wins. If that
    sample is missing or invalid, the state says so and no older valid value is
    substituted. A valid sample older than ``max_age_ns`` is reported as STALE;
    ``max_age_ns=None`` disables the staleness check. ``times_ns`` must be
    non-decreasing.
    """

    _check_lengths(times_ns, values)
    if max_age_ns is not None and max_age_ns < 0:
        raise ValueError(f"max_age_ns must not be negative ({max_age_ns})")

    index = bisect_right(times_ns, at_ns) - 1
    if index < 0:
        return CurrentValue(CurrentValueState.NO_DATA, at_ns, unit, max_age_ns=max_age_ns)

    time_ns = times_ns[index]
    age_ns = at_ns - time_ns
    value = values[index]
    if value is None or isnan(value):
        state = CurrentValueState.MISSING
        value = None
    elif not isfinite(value):
        state = CurrentValueState.INVALID
        value = None
    elif max_age_ns is not None and age_ns > max_age_ns:
        state = CurrentValueState.STALE
    else:
        state = CurrentValueState.VALID
    return CurrentValue(state, at_ns, unit, time_ns, value, age_ns, max_age_ns)


def format_number(value: float, unit: str = "") -> str:
    """Format with bounded precision (six significant digits) and an optional unit."""

    text = f"{value:.{SIGNIFICANT_DIGITS}g}"
    return f"{text} {unit}" if unit else text


def format_seconds(time_ns: int) -> str:
    """Format integer nanoseconds as exact decimal seconds, e.g. ``1.250 s``."""

    sign = "-" if time_ns < 0 else ""
    whole, fraction = divmod(abs(time_ns), 1_000_000_000)
    digits = f"{fraction:09d}".rstrip("0").ljust(3, "0")
    return f"{sign}{whole}.{digits} s"


def format_statistics(stats: IntervalStatistics) -> list[tuple[str, str]]:
    """Return label/text rows describing ``stats``, including the definitions used."""

    def stat(value: float | None) -> str:
        return NO_VALID_TEXT if value is None else format_number(value, stats.unit)

    counts = (
        f"{stats.valid_count} valid, {stats.missing_count} missing, "
        f"{stats.invalid_count} invalid ({stats.sample_count} in interval)"
    )
    return [
        ("Interval", f"{format_seconds(stats.start_ns)} to {format_seconds(stats.end_ns)}"),
        ("Duration", format_seconds(stats.end_ns - stats.start_ns)),
        ("Samples", counts),
        ("Minimum", stat(stats.minimum)),
        ("Maximum", stat(stats.maximum)),
        ("Mean", stat(stats.mean)),
        ("Weighting", WEIGHTING_TEXT),
        ("Counts", COUNT_DEFINITION_TEXT),
    ]


def format_current_value(current: CurrentValue) -> list[tuple[str, str]]:
    """Return label/text rows for ``current`` that state its condition in words."""

    state = current.state
    if state is CurrentValueState.NO_DATA:
        return [
            ("Current value", EMPTY_TEXT),
            ("Current state", f"no data at or before {format_seconds(current.at_ns)}"),
        ]
    assert current.time_ns is not None and current.age_ns is not None
    sample_at = format_seconds(current.time_ns)
    age = format_seconds(current.age_ns)
    if state is CurrentValueState.MISSING:
        return [
            ("Current value", EMPTY_TEXT),
            ("Current state", f"missing: latest sample at {sample_at} has no value"),
        ]
    if state is CurrentValueState.INVALID:
        return [
            ("Current value", EMPTY_TEXT),
            ("Current state", f"invalid: latest sample at {sample_at} is not finite"),
        ]
    assert current.value is not None
    text = format_number(current.value, current.unit)
    if state is CurrentValueState.STALE:
        assert current.max_age_ns is not None
        limit = format_seconds(current.max_age_ns)
        return [
            ("Current value", f"{text} (stale)"),
            ("Current state", f"stale: sample at {sample_at} is {age} old (limit {limit})"),
        ]
    return [
        ("Current value", text),
        ("Current state", f"valid: sample at {sample_at}, {age} old"),
    ]
