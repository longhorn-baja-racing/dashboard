"""Derived signals applied to session snapshots: evaluation, provenance and persistence.

:class:`DerivedSignalSet` holds the applied :class:`DerivedSignalDefinition` values
of one app run. :meth:`DerivedSignalSet.augment` evaluates them against a
:class:`SessionSnapshot` and returns a new snapshot whose schema and series also
contain the derived signals. Raw records are shared with the input snapshot and
never changed. Each derived descriptor sits under the ``derived`` path, keeps its
expression, inputs and alignment in ``display["derived"]`` and in its
description, and uses the reference input's source and clock domain, so replay,
plots and analysis tools treat it like any other numeric signal.

A definition is evaluated again only when the records of one of its inputs
changed (or with ``reuse_stale=True``, not at all while a cached result exists),
so cursor moves and selection changes cost nothing.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from typing import Any

from .derived import (
    UNKNOWN_UNIT,
    CycleError,
    DefinitionError,
    DerivedSeries,
    DerivedSignalDefinition,
    DerivedSignalError,
    MissingReason,
    SignalInput,
    UnitError,
    evaluate,
    order_definitions,
    parse_expression,
    parse_unit,
)
from .session import SessionRecord, SessionSnapshot
from .signals import (
    SignalAvailability,
    SignalDescriptor,
    SignalSample,
    SignalSchema,
    SignalSchemaError,
    _validate_identifier,
)

DERIVED_PATH = ("derived",)
DERIVED_KEY = "derived"
DERIVED_SOURCE = "derived"
CONFIGURATION_VERSION = 1
PREVIEW_LIMIT = 20_000

Token = tuple[object, ...]


def is_derived(descriptor: SignalDescriptor | None) -> bool:
    """Return whether ``descriptor`` was produced by :meth:`DerivedSignalSet.augment`."""

    return descriptor is not None and DERIVED_KEY in descriptor.display


def provenance(definition: DerivedSignalDefinition) -> str:
    """One line naming the expression, inputs, declared unit and alignment."""

    hold = definition.max_age_ns / 1_000_000
    unit = definition.unit or "inferred"
    return (
        f"Derived: {definition.expression} | inputs: {', '.join(definition.inputs)} | "
        f"unit: {unit} | timestamps of {definition.reference}, "
        f"other inputs held up to {hold:g} ms"
    )


def missing_text(series: DerivedSeries, index: int) -> str:
    """Describe why output sample ``index`` is missing ("" when it has a value)."""

    item = series.missing[index]
    if item is None:
        return ""
    if item.reason in (MissingReason.MISSING_INPUT, MissingReason.STALE_INPUT):
        return f"{item.reason.value} ({item.signal_id})"
    return f"{item.reason.value} in {item.where}"


@dataclass(frozen=True, slots=True)
class DerivedStatus:
    """The latest evaluation result of one definition."""

    output_id: str
    available: bool
    message: str
    unit: str | None = None
    count: int = 0
    missing: int = 0
    notes: tuple[str, ...] = ()


@dataclass(frozen=True, slots=True)
class DerivedPreview:
    """A checked candidate definition and its output, or the reason it cannot be applied."""

    definition: DerivedSignalDefinition | None
    error: str | None
    series: DerivedSeries | None = None
    inputs: tuple[str, ...] = ()
    notes: tuple[str, ...] = ()
    total: int = 0

    @property
    def ok(self) -> bool:
        return self.error is None and self.definition is not None

    @property
    def truncated(self) -> bool:
        return self.series is not None and len(self.series) < self.total


@dataclass(frozen=True, slots=True)
class _Resolved:
    inputs: dict[str, SignalInput]
    reference: SignalDescriptor
    reference_records: Sequence[SessionRecord]
    domain: str
    lines: tuple[str, ...]
    notes: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class _Entry:
    session: object
    definition: DerivedSignalDefinition
    tokens: Token
    descriptor: SignalDescriptor | None
    records: tuple[SessionRecord, ...]
    domain: str | None
    status: DerivedStatus
    serial: int


class _View:
    """Descriptors, series and domains visible to a definition being resolved."""

    def __init__(self, snapshot: SessionSnapshot) -> None:
        self.descriptors = {item.identifier: item for item in snapshot.schema.signals}
        self.series: dict[str, Sequence[SessionRecord]] = dict(snapshot.series)
        self.domains = dict(snapshot.domains)

    def domain(self, signal_id: str) -> str:
        return self.domains.get(signal_id, self.descriptors[signal_id].timestamp_domain)


def _input_unit(signal_id: str, unit: str | None, notes: list[str]) -> str | None:
    try:
        parse_unit(unit)
    except UnitError:
        notes.append(
            f"unit {unit!r} of {signal_id!r} is not in the derived unit notation; "
            "it is treated as unknown"
        )
        return None
    return unit


def _resolve(definition: DerivedSignalDefinition, view: _View) -> _Resolved:
    """Find numeric, same-clock data for every input or raise with a useful message."""

    inputs: dict[str, SignalInput] = {}
    lines: list[str] = []
    notes: list[str] = []
    clocks: dict[tuple[str, str], list[str]] = {}
    for signal_id in definition.inputs:
        descriptor = view.descriptors.get(signal_id)
        if descriptor is None:
            raise DerivedSignalError(
                f"unknown signal {signal_id!r}; use an id shown in the signal browser"
            )
        if not descriptor.availability.available:
            reason = descriptor.availability.reason or "not available"
            raise DerivedSignalError(f"input {signal_id!r} is unavailable: {reason}")
        if descriptor.data_type != "numeric" or descriptor.shape:
            kind = "vector" if descriptor.shape else descriptor.data_type
            raise DerivedSignalError(
                f"input {signal_id!r} ({descriptor.display_name}) is {kind}; "
                "derived signals need scalar numeric inputs"
            )
        records = view.series.get(signal_id, ())
        unit = _input_unit(signal_id, descriptor.canonical_unit, notes)
        times = [record.sample.timestamp_ns for record in records]
        values = [record.sample.value if record.sample.available else None for record in records]
        inputs[signal_id] = SignalInput(times, values, unit)
        clocks.setdefault((descriptor.source_id, view.domain(signal_id)), []).append(signal_id)
        lines.append(
            f"{signal_id} - {descriptor.display_name} "
            f"[{descriptor.canonical_unit or 'unit unspecified'}], {len(records)} samples"
        )
    if len(clocks) > 1:
        groups = "; ".join(
            f"{', '.join(ids)} on {source}/{domain}" for (source, domain), ids in clocks.items()
        )
        raise DerivedSignalError(
            f"inputs use different sources or clock domains ({groups}); "
            "derived signals never align separate clocks"
        )
    reference = view.descriptors[definition.reference]
    return _Resolved(
        inputs,
        reference,
        view.series.get(definition.reference, ()),
        view.domain(definition.reference),
        tuple(lines),
        tuple(notes),
    )


def _token(records: Sequence[SessionRecord]) -> Token:
    if not records:
        return (0,)
    return (len(records), records[0].sequence, records[-1].sequence)


def _canonical(unit: str) -> str | None:
    if unit == UNKNOWN_UNIT or any(character.isspace() for character in unit):
        return None
    return unit


def _descriptor(
    definition: DerivedSignalDefinition,
    *,
    source_id: str,
    domain: str,
    unit: str | None,
    available: bool = True,
    reason: str | None = None,
) -> SignalDescriptor:
    display: dict[str, Any] = {
        DERIVED_KEY: {
            "expression": definition.expression,
            "inputs": list(definition.inputs),
            "reference_id": definition.reference,
            "max_age_ns": definition.max_age_ns,
            "unit": definition.unit,
        }
    }
    return SignalDescriptor(
        definition.output_id,
        definition.name,
        DERIVED_PATH,
        "numeric",
        source_id,
        canonical_unit=unit,
        description=provenance(definition),
        availability=SignalAvailability(available, None if available else reason),
        display=display,
        timestamp_domain=domain,
    )


def _records(
    series: DerivedSeries,
    reference: Sequence[SessionRecord],
    source_id: str,
    domain: str,
) -> tuple[SessionRecord, ...]:
    records: list[SessionRecord] = []
    for index, (time_ns, value) in enumerate(zip(series.times_ns, series.values)):
        origin = reference[index]
        quality = "derived" if value is not None else "missing: " + missing_text(series, index)
        sample = SignalSample(
            time_ns,
            value,
            available=value is not None,
            quality=quality,
            timestamp_domain=domain,
            source_id=source_id,
        )
        records.append(SessionRecord(series.output_id, source_id, sample, origin.sequence))
    return tuple(records)


def check_output_id(output_id: str) -> None:
    """Raise :class:`DefinitionError` unless ``output_id`` is a valid signal identifier."""

    try:
        _validate_identifier(output_id, "id")
    except SignalSchemaError as error:
        raise DefinitionError(str(error)) from None


class DerivedSignalSet:
    """The applied derived signals of one app run, independent of the loaded source."""

    def __init__(self) -> None:
        self._definitions: tuple[DerivedSignalDefinition, ...] = ()
        self._entries: dict[str, _Entry] = {}
        self._listeners: dict[object, Callable[[], None]] = {}
        self.statuses: dict[str, DerivedStatus] = {}
        self.evaluations = 0

    @property
    def definitions(self) -> tuple[DerivedSignalDefinition, ...]:
        return self._definitions

    def definition(self, output_id: str) -> DerivedSignalDefinition | None:
        return next((d for d in self._definitions if d.output_id == output_id), None)

    def subscribe(self, listener: Callable[[], None]) -> Callable[[], None]:
        """Call ``listener`` after every change of the definitions; return an unsubscribe."""

        token = object()
        self._listeners[token] = listener

        def unsubscribe() -> None:
            self._listeners.pop(token, None)

        return unsubscribe

    def _changed(self) -> None:
        for listener in list(self._listeners.values()):
            listener()

    def _candidate(
        self, definition: DerivedSignalDefinition, replacing: str | None
    ) -> tuple[DerivedSignalDefinition, ...]:
        check_output_id(definition.output_id)
        if definition.unit is not None and any(c.isspace() for c in definition.unit):
            raise DefinitionError(
                f"unit {definition.unit!r} contains spaces; signal units here are written "
                "without spaces (for example degC)"
            )
        result: list[DerivedSignalDefinition] = []
        placed = False
        for existing in self._definitions:
            if existing.output_id == replacing:
                result.append(definition)
                placed = True
            elif existing.output_id == definition.output_id:
                raise DefinitionError(
                    f"a derived signal with id {definition.output_id!r} already exists; "
                    "choose another id or edit that signal"
                )
            else:
                result.append(existing)
        if not placed:
            result.append(definition)
        order_definitions(result)
        return tuple(result)

    def apply(self, definition: DerivedSignalDefinition, *, replacing: str | None = None) -> None:
        """Add ``definition`` or replace the one with id ``replacing``.

        Raises :class:`DefinitionError` (or :class:`CycleError`) and changes
        nothing when the result would be invalid.
        """

        self._definitions = self._candidate(definition, replacing)
        self._changed()

    def remove(self, output_id: str) -> None:
        remaining = tuple(d for d in self._definitions if d.output_id != output_id)
        if remaining != self._definitions:
            self._definitions = remaining
            self._entries.pop(output_id, None)
            self.statuses.pop(output_id, None)
            self._changed()

    def configuration(self) -> dict[str, Any]:
        """Return a versioned JSON-compatible dict for :meth:`restore_configuration`."""

        return {
            "version": CONFIGURATION_VERSION,
            "definitions": [definition.to_dict() for definition in self._definitions],
        }

    def restore_configuration(self, config: object) -> None:
        """Replace every definition from :meth:`configuration` output, all or nothing.

        Malformed data, duplicate ids and cycles raise :class:`DefinitionError`.
        Definitions whose inputs are not in the current session are kept and
        reported as unavailable by :meth:`augment`.
        """

        if not isinstance(config, Mapping) or set(config) != {"version", "definitions"}:
            raise DefinitionError("derived signal configuration needs version and definitions")
        version = config["version"]
        if type(version) is not int or version != CONFIGURATION_VERSION:
            raise DefinitionError(f"unsupported derived signal configuration version {version!r}")
        items = config["definitions"]
        if not isinstance(items, (list, tuple)):
            raise DefinitionError("definitions must be a list")
        definitions = tuple(DerivedSignalDefinition.from_dict(item) for item in items)
        for definition in definitions:
            check_output_id(definition.output_id)
        order_definitions(definitions)
        self._definitions = definitions
        self._entries.clear()
        self.statuses.clear()
        self._changed()

    def preview(
        self,
        snapshot: SessionSnapshot | None,
        *,
        output_id: str,
        name: str,
        expression: str,
        unit: str | None = None,
        reference_id: str | None = None,
        max_age_ns: int = 0,
        replacing: str | None = None,
        limit: int = PREVIEW_LIMIT,
    ) -> DerivedPreview:
        """Check a candidate against the applied set and ``snapshot``; never raises.

        ``snapshot`` may already contain derived signals (the augmented view).
        Only the first ``limit`` reference samples are evaluated.
        """

        try:
            parsed = parse_expression(expression)
            if reference_id is not None and reference_id not in parsed.signal_ids:
                reference_id = None
            definition = DerivedSignalDefinition(
                output_id=output_id,
                name=name,
                expression=expression,
                unit=unit or None,
                reference_id=reference_id,
                max_age_ns=max_age_ns,
            )
            self._candidate(definition, replacing)
        except CycleError as error:
            return DerivedPreview(None, str(error))
        except DerivedSignalError as error:
            return DerivedPreview(None, str(error))
        if snapshot is None:
            return DerivedPreview(definition, "load a session to preview this signal")
        view = _View(snapshot)
        clash = view.descriptors.get(definition.output_id)
        if clash is not None and not is_derived(clash):
            return DerivedPreview(
                definition, f"id {definition.output_id!r} is already a signal of this source"
            )
        try:
            resolved = _resolve(definition, view)
            times = resolved.inputs[definition.reference].times_ns
            end_ns = times[limit - 1] if len(times) > limit else None
            series = evaluate(definition, resolved.inputs, end_ns=end_ns)
        except DerivedSignalError as error:
            return DerivedPreview(definition, str(error))
        return DerivedPreview(
            definition,
            None,
            series,
            resolved.lines,
            resolved.notes,
            len(resolved.inputs[definition.reference].times_ns),
        )

    def augment(
        self, session: object, snapshot: SessionSnapshot, *, reuse_stale: bool = False
    ) -> SessionSnapshot:
        """Return ``snapshot`` plus every derived signal; ``snapshot`` itself is unchanged.

        ``session`` identifies the data so cached results are never reused across
        sessions. With ``reuse_stale`` a cached result is shown even when its
        inputs grew, which bounds the work while a live source streams.
        """

        if not self._definitions:
            self.statuses = {}
            return snapshot
        view = _View(snapshot)
        raw_ids = set(view.descriptors)
        tokens: dict[str, Token] = {}
        added: list[SignalDescriptor] = []
        added_series: dict[str, tuple[SessionRecord, ...]] = {}
        statuses: dict[str, DerivedStatus] = {}
        for definition in order_definitions(self._definitions):
            output_id = definition.output_id
            if output_id in raw_ids:
                statuses[output_id] = DerivedStatus(
                    output_id,
                    False,
                    f"unavailable: id {output_id!r} is already a signal of this source",
                )
                continue
            key = tuple(
                tokens.get(signal_id) or _token(view.series.get(signal_id, ()))
                for signal_id in definition.inputs
            )
            cached = self._entries.get(output_id)
            if (
                cached is not None
                and cached.session is session
                and cached.definition == definition
                and (cached.tokens == key or (reuse_stale and cached.status.available))
            ):
                entry = cached
            else:
                entry = self._evaluate(session, definition, view, key)
                self._entries[output_id] = entry
            tokens[output_id] = ("derived", entry.serial)
            statuses[output_id] = entry.status
            if entry.descriptor is None:
                continue
            added.append(entry.descriptor)
            added_series[output_id] = entry.records
            view.descriptors[output_id] = entry.descriptor
            view.series[output_id] = entry.records
            if entry.domain is not None and entry.records:
                view.domains[output_id] = entry.domain
        self.statuses = statuses
        if not added:
            return snapshot
        schema = snapshot.schema
        series = dict(snapshot.series)
        series.update(added_series)
        evicted = dict(snapshot.evicted)
        evicted.update({descriptor.identifier: 0 for descriptor in added})
        return SessionSnapshot(
            snapshot.revision,
            SignalSchema(
                schema.schema_version,
                (*schema.signals, *added),
                metadata=schema.metadata,
                extra=schema.extra,
            ),
            snapshot.metadata,
            series,
            view.domains,
            snapshot.source_health,
            snapshot.source_events,
            evicted,
        )

    def _evaluate(
        self,
        session: object,
        definition: DerivedSignalDefinition,
        view: _View,
        key: Token,
    ) -> _Entry:
        self.evaluations += 1
        output_id = definition.output_id
        reference = view.descriptors.get(definition.reference)
        source_id = reference.source_id if reference is not None else DERIVED_SOURCE
        domain = reference.timestamp_domain if reference is not None else "unspecified"
        try:
            resolved = _resolve(definition, view)
            series = evaluate(definition, resolved.inputs)
        except DerivedSignalError as error:
            message = str(error)
            descriptor = _descriptor(
                definition,
                source_id=source_id,
                domain=domain,
                unit=_canonical(definition.unit or UNKNOWN_UNIT),
                available=False,
                reason=message,
            )
            status = DerivedStatus(output_id, False, f"unavailable: {message}", definition.unit)
            return _Entry(session, definition, key, descriptor, (), None, status, self.evaluations)
        unit = _canonical(series.unit)
        descriptor = _descriptor(
            definition,
            source_id=resolved.reference.source_id,
            domain=resolved.reference.timestamp_domain,
            unit=unit,
        )
        records = _records(
            series, resolved.reference_records, resolved.reference.source_id, resolved.domain
        )
        missing = series.missing_count
        status = DerivedStatus(
            output_id,
            True,
            f"{len(series)} samples, {missing} missing, unit {series.unit}",
            series.unit,
            len(series),
            missing,
            resolved.notes,
        )
        return _Entry(
            session, definition, key, descriptor, records, resolved.domain, status, self.evaluations
        )
