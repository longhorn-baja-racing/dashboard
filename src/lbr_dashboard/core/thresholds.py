"""Read-only threshold alarms evaluated over generic scalar samples.

A `ThresholdRule` compares one scalar signal against a level with optional
hysteresis, debounce, and staleness. `evaluate` turns parallel timestamp and
value sequences into the list of state changes; nothing here sends commands,
writes files, or talks to a vehicle. See docs/threshold-alarms.md for the full
rules.

Time is integer nanoseconds and every interval is closed: a sample at exactly
an interval edge is inside it.
"""

from __future__ import annotations

import json
import math
from bisect import bisect_right
from collections.abc import Iterable, Sequence
from dataclasses import dataclass, fields
from enum import Enum
from typing import Any

THRESHOLD_FILE_FORMAT = "lbr-dashboard-thresholds"
THRESHOLD_FILE_VERSION = 1
MAX_THRESHOLD_FILE_CHARS = 1_048_576

COMPARISONS = ("above", "below")
SEVERITIES = ("info", "warning", "critical")

REASON_CROSSED = "crossed"
REASON_CLEARED = "cleared"
REASON_MISSING = "missing"
REASON_STALE = "stale"


class AlarmState(Enum):
    """Evaluated state of one rule. Missing or stale input is UNKNOWN, never NORMAL."""

    NORMAL = "normal"
    ACTIVE = "active"
    UNKNOWN = "unknown"


class ThresholdRuleError(ValueError):
    """A rule field is invalid; `field` names it."""

    def __init__(self, field: str, message: str) -> None:
        super().__init__(f"{field}: {message}")
        self.field = field
        self.message = message


class ThresholdFileError(ValueError):
    """A saved threshold document is invalid; `index` and `field` locate the problem."""

    def __init__(self, message: str, *, index: int | None = None, field: str | None = None) -> None:
        location = field or ""
        if index is not None:
            location = f"rules[{index}]" + (f".{field}" if field else "")
        super().__init__(f"{location}: {message}" if location else message)
        self.index = index
        self.field = field
        self.message = message


def _is_int(value: object) -> bool:
    return isinstance(value, int) and not isinstance(value, bool)


def _finite_number(field: str, value: object) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ThresholdRuleError(field, "must be a number")
    try:
        number = float(value)
    except OverflowError:
        raise ThresholdRuleError(field, "must be a finite number") from None
    if not math.isfinite(number):
        raise ThresholdRuleError(field, "must be a finite number")
    return number


def _format_number(value: float) -> str:
    if value.is_integer() and abs(value) < 1e15:
        return str(int(value))
    return repr(value)


def _format_seconds(duration_ns: int) -> str:
    return _format_number(duration_ns / 1_000_000_000)


@dataclass(frozen=True, slots=True)
class ThresholdRule:
    """One user-defined threshold on one scalar signal.

    `level` and `hysteresis` are in the signal's own unit; `unit` is a free-text
    copy of it so a saved rule stays readable and can be checked against the
    signal later. Durations are integer nanoseconds.
    """

    rule_id: str
    signal_id: str
    comparison: str
    level: float
    hysteresis: float = 0.0
    debounce_ns: int = 0
    severity: str = "warning"
    stale_after_ns: int | None = None
    label: str = ""
    unit: str = ""

    def __post_init__(self) -> None:
        for name in ("rule_id", "signal_id"):
            value = getattr(self, name)
            if not isinstance(value, str) or not value or value != value.strip():
                raise ThresholdRuleError(
                    name, "must be a non-empty string without surrounding whitespace"
                )
        if self.comparison not in COMPARISONS:
            raise ThresholdRuleError(
                "comparison", f"must be 'above' or 'below', got {self.comparison!r}"
            )
        object.__setattr__(self, "level", _finite_number("level", self.level))
        hysteresis = _finite_number("hysteresis", self.hysteresis)
        if hysteresis < 0:
            raise ThresholdRuleError("hysteresis", "must be zero or positive")
        object.__setattr__(self, "hysteresis", hysteresis)
        if not _is_int(self.debounce_ns) or self.debounce_ns < 0:
            raise ThresholdRuleError("debounce_ns", "must be an integer >= 0")
        if self.severity not in SEVERITIES:
            raise ThresholdRuleError(
                "severity", f"must be 'info', 'warning' or 'critical', got {self.severity!r}"
            )
        stale = self.stale_after_ns
        if stale is not None and (not _is_int(stale) or stale <= 0):
            raise ThresholdRuleError("stale_after_ns", "must be an integer > 0 or null")
        for name in ("label", "unit"):
            if not isinstance(getattr(self, name), str):
                raise ThresholdRuleError(name, "must be a string")

    @property
    def clear_level(self) -> float:
        """Level the value must pass (strictly) for an active alarm to clear."""

        if self.comparison == "above":
            return self.level - self.hysteresis
        return self.level + self.hysteresis

    def describe(self) -> str:
        """Return a short plain-text sentence such as "Above 95 for 0.5 s, clears below 93"."""

        unit = f" {self.unit}" if self.unit else ""
        hold = f" for {_format_seconds(self.debounce_ns)} s" if self.debounce_ns else ""
        clear = "below" if self.comparison == "above" else "above"
        text = (
            f"{self.comparison.capitalize()} {_format_number(self.level)}{unit}{hold}, "
            f"clears {clear} {_format_number(self.clear_level)}{unit}{hold}"
        )
        if self.stale_after_ns is not None:
            text += f", unknown after {_format_seconds(self.stale_after_ns)} s without data"
        return text


@dataclass(frozen=True, slots=True)
class AlarmTransition:
    """A change of state at `time_ns`; `value` is the sample that caused it, if any."""

    time_ns: int
    state: AlarmState
    value: float | None
    reason: str


class _Evaluation:
    """Mutable state for one `evaluate` call; never shared or kept."""

    def __init__(self, rule: ThresholdRule) -> None:
        self.rule = rule
        self.state = AlarmState.UNKNOWN
        self.pending: AlarmState | None = None
        self.pending_since_ns = 0
        self.transitions: list[AlarmTransition] = []

    def check_stale(self, previous_ns: int, time_ns: int) -> None:
        stale_after = self.rule.stale_after_ns
        if stale_after is not None and time_ns - previous_ns > stale_after:
            # The previous sample is valid through previous_ns + stale_after (closed).
            self._mark_unknown(previous_ns + stale_after + 1, REASON_STALE)

    def sample(self, time_ns: int, value: float | None) -> None:
        if value is None:
            self._mark_unknown(time_ns, REASON_MISSING)
            return
        target = self._target(value)
        if target is self.state:
            self.pending = None
            return
        if self.pending is not target:
            self.pending = target
            self.pending_since_ns = time_ns
        if time_ns - self.pending_since_ns >= self.rule.debounce_ns:
            self.pending = None
            reason = REASON_CROSSED if target is AlarmState.ACTIVE else REASON_CLEARED
            self._change(time_ns, target, value, reason)

    def _target(self, value: float) -> AlarmState:
        rule = self.rule
        if rule.comparison == "above":
            beyond, cleared = value > rule.level, value < rule.clear_level
        else:
            beyond, cleared = value < rule.level, value > rule.clear_level
        if beyond:
            return AlarmState.ACTIVE
        if cleared or self.state is AlarmState.UNKNOWN:
            # Leaving UNKNOWN compares against the level only: the band is not active.
            return AlarmState.NORMAL
        return self.state

    def _mark_unknown(self, time_ns: int, reason: str) -> None:
        self.pending = None
        self._change(time_ns, AlarmState.UNKNOWN, None, reason)

    def _change(self, time_ns: int, state: AlarmState, value: float | None, reason: str) -> None:
        if state is self.state:
            return
        self.state = state
        transitions = self.transitions
        if transitions and transitions[-1].time_ns == time_ns:
            # Several changes at one timestamp collapse to the final state at that time.
            transitions.pop()
            before = transitions[-1].state if transitions else AlarmState.UNKNOWN
            if before is state:
                return
        transitions.append(AlarmTransition(time_ns, state, value, reason))


def _sample_value(index: int, raw: object) -> float | None:
    if raw is None:
        return None
    if isinstance(raw, bool) or not isinstance(raw, (int, float)):
        raise TypeError(f"values[{index}] must be a number or None")
    try:
        value = float(raw)
    except OverflowError:
        return None
    return value if math.isfinite(value) else None


def evaluate(
    rule: ThresholdRule,
    times_ns: Sequence[int],
    values: Sequence[float | None],
    *,
    end_ns: int | None = None,
) -> tuple[AlarmTransition, ...]:
    """Return the state changes of `rule` over one signal's samples.

    `times_ns` must be non-decreasing integer nanoseconds; `values` are parallel.
    None, NaN, and infinities are missing values. The state is UNKNOWN before the
    first transition. `end_ns` (closed, >= the last sample) is where the data
    ends, for example the replay end or "now" when live; it lets staleness
    after the last sample be reported. The result is deterministic, ordered by
    strictly increasing time, and lists only changes.
    """

    if not isinstance(rule, ThresholdRule):
        raise TypeError("rule must be a ThresholdRule")
    if len(times_ns) != len(values):
        raise ValueError("times_ns and values must have the same length")
    evaluation = _Evaluation(rule)
    previous_ns: int | None = None
    for index, (time_ns, raw) in enumerate(zip(times_ns, values)):
        if not _is_int(time_ns):
            raise TypeError(f"times_ns[{index}] must be integer nanoseconds")
        if previous_ns is not None:
            if time_ns < previous_ns:
                raise ValueError(f"times_ns must be non-decreasing (times_ns[{index}])")
            evaluation.check_stale(previous_ns, time_ns)
        evaluation.sample(time_ns, _sample_value(index, raw))
        previous_ns = time_ns
    if end_ns is not None:
        if not _is_int(end_ns):
            raise TypeError("end_ns must be integer nanoseconds")
        if previous_ns is not None:
            if end_ns < previous_ns:
                raise ValueError("end_ns must not be before the last sample")
            evaluation.check_stale(previous_ns, end_ns)
    return tuple(evaluation.transitions)


def transition_at(transitions: Sequence[AlarmTransition], time_ns: int) -> AlarmTransition | None:
    """Return the transition in effect at `time_ns` (one exactly at it counts), if any."""

    index = bisect_right(transitions, time_ns, key=lambda item: item.time_ns)
    return transitions[index - 1] if index else None


def state_at(transitions: Sequence[AlarmTransition], time_ns: int) -> AlarmState:
    """Return the state at `time_ns`; seeking forward or backward gives the same answer."""

    current = transition_at(transitions, time_ns)
    return current.state if current is not None else AlarmState.UNKNOWN


def acknowledge(
    acknowledged: frozenset[tuple[str, int]],
    rule_id: str,
    transitions: Sequence[AlarmTransition],
    activation_ns: int,
) -> frozenset[tuple[str, int]]:
    """Return `acknowledged` plus the activation of `rule_id` that started at `activation_ns`.

    Keys are (rule_id, activation time). Acknowledging one activation never
    covers a later one, even after a brief UNKNOWN; a time that is not an
    activation start is rejected.
    """

    current = transition_at(transitions, activation_ns)
    if (
        current is None
        or current.time_ns != activation_ns
        or current.state is not AlarmState.ACTIVE
    ):
        raise ValueError(f"no activation of {rule_id!r} starts at {activation_ns} ns")
    return acknowledged | {(rule_id, activation_ns)}


def is_acknowledged(
    acknowledged: frozenset[tuple[str, int]],
    rule_id: str,
    transitions: Sequence[AlarmTransition],
    time_ns: int,
) -> bool:
    """Return whether the alarm is ACTIVE at `time_ns` and that activation is acknowledged."""

    current = transition_at(transitions, time_ns)
    return (
        current is not None
        and current.state is AlarmState.ACTIVE
        and (rule_id, current.time_ns) in acknowledged
    )


_RULE_FIELDS = tuple(item.name for item in fields(ThresholdRule))
_REQUIRED_RULE_FIELDS = ("rule_id", "signal_id", "comparison", "level")
_DOCUMENT_FIELDS = ("format", "version", "rules")


class _JsonObject(dict[str, Any]):
    duplicate_keys: tuple[str, ...] = ()


def _json_object(pairs: list[tuple[str, Any]]) -> _JsonObject:
    result = _JsonObject()
    duplicates: list[str] = []
    for key, value in pairs:
        if key in result:
            duplicates.append(key)
        result[key] = value
    result.duplicate_keys = tuple(duplicates)
    return result


def _reject_constant(name: str) -> Any:
    raise ThresholdFileError(f"{name} is not allowed; numbers must be finite")


def _check_fields(
    item: dict[str, Any],
    allowed: Sequence[str],
    required: Sequence[str],
    index: int | None,
) -> None:
    duplicates = getattr(item, "duplicate_keys", ())
    if duplicates:
        raise ThresholdFileError("field appears more than once", index=index, field=duplicates[0])
    unknown = sorted(key for key in item if key not in allowed)
    if unknown:
        raise ThresholdFileError("unknown field", index=index, field=unknown[0])
    for name in required:
        if name not in item:
            raise ThresholdFileError("missing required field", index=index, field=name)


def _check_unique_ids(rules: Sequence[ThresholdRule]) -> None:
    first_index: dict[str, int] = {}
    for index, rule in enumerate(rules):
        first = first_index.get(rule.rule_id)
        if first is not None:
            raise ThresholdFileError(
                f"duplicate rule_id {rule.rule_id!r}, first used by rules[{first}]",
                index=index,
                field="rule_id",
            )
        first_index[rule.rule_id] = index


def rules_to_json(rules: Iterable[ThresholdRule]) -> str:
    """Serialize rules to a versioned, ASCII-only JSON document."""

    items = tuple(rules)
    for index, rule in enumerate(items):
        if not isinstance(rule, ThresholdRule):
            raise TypeError(f"rules[{index}] must be a ThresholdRule")
    _check_unique_ids(items)
    document = {
        "format": THRESHOLD_FILE_FORMAT,
        "version": THRESHOLD_FILE_VERSION,
        "rules": [{name: getattr(rule, name) for name in _RULE_FIELDS} for rule in items],
    }
    return json.dumps(document, indent=2, allow_nan=False) + "\n"


def rules_from_json(text: str) -> tuple[ThresholdRule, ...]:
    """Parse and strictly validate a document written by `rules_to_json`.

    Unknown fields, duplicate keys, duplicate rule IDs, wrong types, non-finite
    numbers, unsupported versions, and oversized input raise `ThresholdFileError`.
    """

    if not isinstance(text, str):
        raise ThresholdFileError("threshold document must be text")
    if len(text) > MAX_THRESHOLD_FILE_CHARS:
        raise ThresholdFileError(
            f"threshold document is larger than {MAX_THRESHOLD_FILE_CHARS} characters"
        )
    try:
        document = json.loads(text, object_pairs_hook=_json_object, parse_constant=_reject_constant)
    except ThresholdFileError:
        raise
    except (ValueError, RecursionError) as error:
        raise ThresholdFileError(f"not valid JSON: {error}") from None
    if not isinstance(document, dict):
        raise ThresholdFileError("top level must be a JSON object")
    for name in ("format", "version"):
        if name not in document:
            raise ThresholdFileError("missing required field", field=name)
    if document["format"] != THRESHOLD_FILE_FORMAT:
        raise ThresholdFileError(f"expected {THRESHOLD_FILE_FORMAT!r}", field="format")
    version = document["version"]
    if not _is_int(version):
        raise ThresholdFileError("must be an integer", field="version")
    if version != THRESHOLD_FILE_VERSION:
        raise ThresholdFileError(
            f"unsupported version {version}; this build reads version {THRESHOLD_FILE_VERSION}",
            field="version",
        )
    _check_fields(document, _DOCUMENT_FIELDS, _DOCUMENT_FIELDS, None)
    entries = document["rules"]
    if not isinstance(entries, list):
        raise ThresholdFileError("must be a list", field="rules")
    rules: list[ThresholdRule] = []
    for index, entry in enumerate(entries):
        if not isinstance(entry, dict):
            raise ThresholdFileError("must be a JSON object", index=index)
        _check_fields(entry, _RULE_FIELDS, _REQUIRED_RULE_FIELDS, index)
        try:
            rules.append(ThresholdRule(**entry))
        except ThresholdRuleError as error:
            raise ThresholdFileError(error.message, index=index, field=error.field) from None
    _check_unique_ids(rules)
    return tuple(rules)
