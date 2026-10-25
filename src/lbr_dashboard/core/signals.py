"""Schema-driven signal descriptors, sparse samples, and signal registry."""

from __future__ import annotations

import json
import re
from collections import Counter
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from math import isfinite
from types import MappingProxyType
from typing import Any, TypeAlias

from .registry import RegistryError

JsonScalar: TypeAlias = None | bool | int | float | str
JsonValue: TypeAlias = JsonScalar | tuple["JsonValue", ...] | Mapping[str, "JsonValue"]
SchemaVersion: TypeAlias = int | str

KNOWN_SIGNAL_TYPES = frozenset({"numeric", "boolean", "string", "event", "vector", "status"})
_DESCRIPTOR_FIELDS = {
    "availability",
    "calibration",
    "canonical_unit",
    "device_id",
    "display",
    "display_name",
    "description",
    "id",
    "nominal_rate_hz",
    "path",
    "shape",
    "source_id",
    "type",
    "timestamp_domain",
}
_IDENTIFIER = re.compile(r"^[a-z][a-z0-9]*(?:[._-][a-z0-9]+)*$")
_PATH_SEGMENT = re.compile(r"^[a-z][a-z0-9_-]*$")
_TOKEN = re.compile(r"[a-z0-9]+")


class SignalSchemaError(ValueError):
    """Raised when a signal descriptor or schema is invalid."""


def _require_text(value: object, field_name: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise SignalSchemaError(f"{field_name} must be a non-empty string")
    return value


def _validate_identifier(value: object, field_name: str) -> str:
    identifier = _require_text(value, field_name)
    if _IDENTIFIER.fullmatch(identifier) is None:
        raise SignalSchemaError(
            f"{field_name} '{identifier}' must use lowercase dot, dash, or underscore segments"
        )
    return identifier


def _validate_path(value: object) -> tuple[str, ...]:
    if isinstance(value, (str, bytes)) or not isinstance(value, Sequence) or not value:
        raise SignalSchemaError("path must be a non-empty sequence of segments")
    path = tuple(_require_text(segment, "path segment") for segment in value)
    invalid = [segment for segment in path if _PATH_SEGMENT.fullmatch(segment) is None]
    if invalid:
        raise SignalSchemaError(f"path segments are invalid: {invalid}")
    return path


def _validate_shape(value: object) -> tuple[int, ...]:
    if isinstance(value, (str, bytes)) or not isinstance(value, Sequence):
        raise SignalSchemaError("shape must be a sequence of positive integers")
    shape: list[int] = []
    for dimension in value:
        if isinstance(dimension, bool) or not isinstance(dimension, int) or dimension <= 0:
            raise SignalSchemaError("shape dimensions must be positive integers")
        shape.append(dimension)
    return tuple(shape)


def _freeze_json(value: object, field_name: str) -> JsonValue:
    active: set[int] = set()

    def visit(item: object, depth: int) -> JsonValue:
        if depth > 64:
            raise SignalSchemaError(f"{field_name} exceeds 64 levels of nesting")
        if item is None or isinstance(item, (bool, int, float, str)):
            if isinstance(item, float) and not isfinite(item):
                raise SignalSchemaError(f"{field_name} cannot contain non-finite numbers")
            return item
        if id(item) in active:
            raise SignalSchemaError(f"{field_name} contains a cyclic value")
        active.add(id(item))
        try:
            if isinstance(item, Mapping):
                if any(not isinstance(key, str) for key in item):
                    raise SignalSchemaError(f"{field_name} keys must be strings")
                # Keys are opaque metadata. Normalizing them can silently lose values.
                return MappingProxyType(
                    {key: visit(child, depth + 1) for key, child in item.items()}
                )
            if isinstance(item, Sequence) and not isinstance(item, (str, bytes, bytearray)):
                return tuple(visit(child, depth + 1) for child in item)
            raise SignalSchemaError(
                f"{field_name} contains unsupported value {type(item).__name__}"
            )
        finally:
            active.remove(id(item))

    return visit(value, 0)


def _thaw_json(value: JsonValue) -> Any:
    if isinstance(value, Mapping):
        return {key: _thaw_json(item) for key, item in sorted(value.items())}
    if isinstance(value, tuple):
        return [_thaw_json(item) for item in value]
    return value


def _freeze_mapping(value: Mapping[str, Any], field_name: str) -> Mapping[str, JsonValue]:
    frozen = _freeze_json(value, field_name)
    if not isinstance(frozen, Mapping):
        raise SignalSchemaError(f"{field_name} must be an object")
    return frozen


def _mapping_to_dict(value: Mapping[str, JsonValue]) -> dict[str, JsonValue]:
    return {key: _thaw_json(item) for key, item in sorted(value.items())}


def _check_keys(value: Mapping, field_name: str) -> None:
    if any(not isinstance(key, str) for key in value):
        raise SignalSchemaError(f"{field_name} keys must be strings")


def _check_extra(value: Mapping[str, JsonValue], reserved: set[str]) -> None:
    collisions = sorted(set(value) & reserved)
    if collisions:
        raise SignalSchemaError(f"extra must not override reserved fields: {collisions}")


def _numeric(value: object) -> bool:
    return isinstance(value, (int, float)) and not isinstance(value, bool)


def _validate_vector(value: object, shape: tuple[int, ...]) -> None:
    if not shape:
        if not _numeric(value):
            raise SignalSchemaError("vector leaves must be numeric")
        return
    if not isinstance(value, tuple) or len(value) != shape[0]:
        raise SignalSchemaError("sample value does not match vector shape")
    for item in value:
        _validate_vector(item, shape[1:])


@dataclass(frozen=True, slots=True)
class SignalAvailability:
    """Explicit availability metadata; missing data is never represented as zero."""

    available: bool = True
    reason: str | None = None
    extra: Mapping[str, JsonValue] = field(default_factory=dict)

    def __post_init__(self) -> None:
        if not isinstance(self.available, bool):
            raise SignalSchemaError("availability.available must be a boolean")
        if self.reason is not None:
            reason = _require_text(self.reason, "availability.reason")
            object.__setattr__(self, "reason", reason)
        if not self.available and self.reason is None:
            raise SignalSchemaError("unavailable signals require availability.reason")
        object.__setattr__(self, "extra", _freeze_mapping(self.extra, "availability.extra"))
        _check_extra(self.extra, {"available", "reason"})

    def to_dict(self) -> dict[str, JsonValue]:
        return {**_mapping_to_dict(self.extra), "available": self.available, "reason": self.reason}

    @classmethod
    def from_dict(cls, value: object, *, allow_unknown: bool = True) -> SignalAvailability:
        if not isinstance(value, Mapping):
            raise SignalSchemaError("availability must be an object")
        _check_keys(value, "availability")
        unknown = set(value) - {"available", "reason"}
        if unknown and not allow_unknown:
            raise SignalSchemaError(f"unknown availability fields: {sorted(unknown)}")
        return cls(
            available=value.get("available", True),
            reason=value.get("reason"),
            extra={key: value[key] for key in unknown},
        )


@dataclass(frozen=True, slots=True)
class SignalDescriptor:
    """Immutable metadata describing one signal independently of its samples."""

    identifier: str
    display_name: str
    path: tuple[str, ...]
    data_type: str
    source_id: str
    shape: tuple[int, ...] = ()
    canonical_unit: str | None = None
    device_id: str | None = None
    nominal_rate_hz: float | None = None
    description: str = ""
    availability: SignalAvailability = field(default_factory=SignalAvailability)
    calibration: Mapping[str, JsonValue] = field(default_factory=dict)
    display: Mapping[str, JsonValue] = field(default_factory=dict)
    extra: Mapping[str, JsonValue] = field(default_factory=dict)
    timestamp_domain: str = "unspecified"

    def __post_init__(self) -> None:
        object.__setattr__(self, "identifier", _validate_identifier(self.identifier, "identifier"))
        object.__setattr__(self, "display_name", _require_text(self.display_name, "display_name"))
        object.__setattr__(self, "path", _validate_path(self.path))
        object.__setattr__(self, "data_type", _validate_identifier(self.data_type, "type"))
        object.__setattr__(self, "source_id", _validate_identifier(self.source_id, "source_id"))
        object.__setattr__(self, "shape", _validate_shape(self.shape))
        _require_text(self.timestamp_domain, "timestamp_domain")
        if self.data_type == "vector" and not self.shape:
            raise SignalSchemaError("vector shape must contain a dimension")
        if self.is_known_type and self.data_type != "vector" and self.shape:
            raise SignalSchemaError(f"{self.data_type} must have scalar shape ()")

        if self.canonical_unit is not None:
            unit = _require_text(self.canonical_unit, "canonical_unit")
            if any(character.isspace() for character in unit):
                raise SignalSchemaError("canonical_unit must not contain whitespace")
            object.__setattr__(self, "canonical_unit", unit)
        if self.device_id is not None:
            object.__setattr__(self, "device_id", _validate_identifier(self.device_id, "device_id"))
        if self.nominal_rate_hz is not None:
            if not isinstance(self.nominal_rate_hz, (int, float)) or isinstance(
                self.nominal_rate_hz, bool
            ):
                raise SignalSchemaError("nominal_rate_hz must be a positive finite number")
            try:
                finite = isfinite(float(self.nominal_rate_hz))
            except OverflowError as error:
                raise SignalSchemaError(
                    "nominal_rate_hz must be a positive finite number"
                ) from error
            if not finite or self.nominal_rate_hz <= 0:
                raise SignalSchemaError("nominal_rate_hz must be a positive finite number")
            object.__setattr__(self, "nominal_rate_hz", float(self.nominal_rate_hz))
        if not isinstance(self.description, str):
            raise SignalSchemaError("description must be a string")
        if not isinstance(self.availability, SignalAvailability):
            raise SignalSchemaError("availability must be SignalAvailability")

        object.__setattr__(self, "calibration", _freeze_mapping(self.calibration, "calibration"))
        object.__setattr__(self, "display", _freeze_mapping(self.display, "display"))
        object.__setattr__(self, "extra", _freeze_mapping(self.extra, "extra"))
        _check_extra(self.extra, _DESCRIPTOR_FIELDS)

    def validate_sample(self, sample: SignalSample) -> None:
        if not isinstance(sample, SignalSample):
            raise SignalSchemaError("sample must be SignalSample")
        if sample.source_id is not None and sample.source_id != self.source_id:
            raise SignalSchemaError("sample source_id differs from descriptor source_id")
        if (
            self.timestamp_domain != "unspecified"
            and sample.timestamp_domain != self.timestamp_domain
        ):
            raise SignalSchemaError(
                "sample timestamp_domain differs from descriptor timestamp_domain"
            )
        if not sample.available:
            return
        value = sample.value
        if self.data_type == "numeric" and not _numeric(value):
            raise SignalSchemaError("numeric samples require a number")
        if self.data_type == "boolean" and not isinstance(value, bool):
            raise SignalSchemaError("boolean samples require a boolean")
        if self.data_type == "string" and not isinstance(value, str):
            raise SignalSchemaError("string samples require a string")
        if self.data_type == "vector":
            _validate_vector(value, self.shape)

    @property
    def is_known_type(self) -> bool:
        """Whether this descriptor uses one of the currently known data types."""

        return self.data_type in KNOWN_SIGNAL_TYPES

    @property
    def search_tokens(self) -> tuple[str, ...]:
        """Derive deterministic search metadata from descriptor fields."""

        text = " ".join(
            (
                self.identifier,
                self.display_name,
                *self.path,
                self.data_type,
                self.source_id,
                self.device_id or "",
                self.canonical_unit or "",
                self.description,
            )
        ).lower()
        return tuple(sorted(set(_TOKEN.findall(text))))

    @property
    def unknown_fields(self) -> Mapping[str, JsonValue]:
        """Return forward-compatible fields that were not understood by this version."""

        return self.extra

    def to_dict(self) -> dict[str, JsonValue]:
        result = _mapping_to_dict(self.extra)
        result.update(
            {
                "availability": self.availability.to_dict(),
                "calibration": _mapping_to_dict(self.calibration),
                "canonical_unit": self.canonical_unit,
                "device_id": self.device_id,
                "display": _mapping_to_dict(self.display),
                "display_name": self.display_name,
                "id": self.identifier,
                "nominal_rate_hz": self.nominal_rate_hz,
                "path": self.path,
                "shape": self.shape,
                "source_id": self.source_id,
                "type": self.data_type,
                "timestamp_domain": self.timestamp_domain,
            }
        )
        if self.description:
            result["description"] = self.description
        return result

    @classmethod
    def from_dict(cls, value: object, *, allow_unknown: bool = True) -> SignalDescriptor:
        if not isinstance(value, Mapping):
            raise SignalSchemaError("signal descriptor must be an object")
        _check_keys(value, "descriptor")
        known = {
            "availability",
            "calibration",
            "canonical_unit",
            "device_id",
            "display",
            "display_name",
            "description",
            "id",
            "nominal_rate_hz",
            "path",
            "shape",
            "source_id",
            "type",
            "timestamp_domain",
        }
        missing = sorted({"id", "display_name", "path", "type", "source_id"} - set(value))
        if missing:
            raise SignalSchemaError(f"signal descriptor is missing fields: {missing}")
        extra = {key: item for key, item in value.items() if key not in known}
        if extra and not allow_unknown:
            raise SignalSchemaError(f"unknown signal fields: {sorted(extra)}")
        calibration = value.get("calibration", {})
        display = value.get("display", {})
        if not isinstance(calibration, Mapping) or not isinstance(display, Mapping):
            raise SignalSchemaError("calibration and display must be objects")
        return cls(
            identifier=value["id"],
            display_name=value["display_name"],
            path=value["path"],
            data_type=value["type"],
            source_id=value["source_id"],
            shape=value.get("shape", ()),
            canonical_unit=value.get("canonical_unit"),
            device_id=value.get("device_id"),
            nominal_rate_hz=value.get("nominal_rate_hz"),
            description=value.get("description", ""),
            availability=SignalAvailability.from_dict(
                value.get("availability", {}), allow_unknown=allow_unknown
            ),
            calibration=calibration,
            display=display,
            extra=extra,
            timestamp_domain=value.get("timestamp_domain", "unspecified"),
        )


@dataclass(frozen=True, slots=True)
class SignalSample:
    """One sparse sample; each signal keeps its own timeline."""

    timestamp_ns: int
    value: Any
    available: bool = True
    quality: str | None = None
    timestamp_domain: str = "unspecified"
    source_id: str | None = None
    unit: str | None = None
    extra: Mapping[str, JsonValue] = field(default_factory=dict)

    def __post_init__(self) -> None:
        if isinstance(self.timestamp_ns, bool) or not isinstance(self.timestamp_ns, int):
            raise SignalSchemaError("sample timestamp_ns must be an integer")
        if not isinstance(self.available, bool):
            raise SignalSchemaError("sample available must be a boolean")
        if self.available and self.value is None:
            raise SignalSchemaError("available samples must contain a value")
        if not self.available and self.value is not None:
            raise SignalSchemaError("unavailable samples must have value None")
        if self.quality is not None:
            object.__setattr__(self, "quality", _require_text(self.quality, "sample quality"))
        _require_text(self.timestamp_domain, "timestamp_domain")
        if self.source_id is not None:
            _validate_identifier(self.source_id, "sample source_id")
        if self.unit is not None:
            _require_text(self.unit, "sample unit")
            if any(character.isspace() for character in self.unit):
                raise SignalSchemaError("sample unit must not contain whitespace")
        object.__setattr__(self, "value", _freeze_json(self.value, "sample value"))
        object.__setattr__(self, "extra", _freeze_mapping(self.extra, "sample extra"))
        _check_extra(
            self.extra,
            {
                "timestamp_ns",
                "value",
                "available",
                "quality",
                "timestamp_domain",
                "source_id",
                "unit",
            },
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            **_mapping_to_dict(self.extra),
            "timestamp_ns": self.timestamp_ns,
            "value": _thaw_json(self.value),
            "available": self.available,
            "quality": self.quality,
            "timestamp_domain": self.timestamp_domain,
            "source_id": self.source_id,
            "unit": self.unit,
        }

    @classmethod
    def from_dict(cls, value: object, *, allow_unknown: bool = True) -> SignalSample:
        if not isinstance(value, Mapping) or "timestamp_ns" not in value or "value" not in value:
            raise SignalSchemaError("sample requires timestamp_ns and value")
        _check_keys(value, "sample")
        known = {
            "timestamp_ns",
            "value",
            "available",
            "quality",
            "timestamp_domain",
            "source_id",
            "unit",
        }
        extra = {key: item for key, item in value.items() if key not in known}
        if extra and not allow_unknown:
            raise SignalSchemaError(f"unknown sample fields: {sorted(extra)}")
        return cls(**{key: item for key, item in value.items() if key in known}, extra=extra)


@dataclass(frozen=True, slots=True)
class SignalSeries:
    """Sparse storage for one signal without synthetic row alignment."""

    signal_id: str
    samples: tuple[SignalSample, ...]
    extra: Mapping[str, JsonValue] = field(default_factory=dict)

    def __post_init__(self) -> None:
        object.__setattr__(self, "signal_id", _validate_identifier(self.signal_id, "signal_id"))
        object.__setattr__(self, "samples", tuple(self.samples))
        if any(not isinstance(sample, SignalSample) for sample in self.samples):
            raise SignalSchemaError("samples must contain SignalSample values")
        object.__setattr__(self, "extra", _freeze_mapping(self.extra, "series extra"))
        _check_extra(self.extra, {"signal_id", "samples"})

    @classmethod
    def from_samples(
        cls,
        signal_id: str,
        samples: Iterable[SignalSample],
        *,
        descriptor: SignalDescriptor | None = None,
    ) -> SignalSeries:
        series = cls(signal_id=signal_id, samples=tuple(samples))
        if descriptor is not None:
            series.validate(descriptor)
        return series

    def validate(self, descriptor: SignalDescriptor) -> None:
        if not isinstance(descriptor, SignalDescriptor) or descriptor.identifier != self.signal_id:
            raise SignalSchemaError("descriptor must identify this signal_id")
        for sample in self.samples:
            descriptor.validate_sample(sample)

    def to_dict(self) -> dict[str, Any]:
        return {
            **_mapping_to_dict(self.extra),
            "signal_id": self.signal_id,
            "samples": [sample.to_dict() for sample in self.samples],
        }

    @classmethod
    def from_dict(
        cls,
        value: object,
        *,
        descriptor: SignalDescriptor | None = None,
        allow_unknown: bool = True,
    ) -> SignalSeries:
        if not isinstance(value, Mapping) or "signal_id" not in value or "samples" not in value:
            raise SignalSchemaError("series requires signal_id and samples")
        _check_keys(value, "series")
        samples = value["samples"]
        if isinstance(samples, (str, bytes)) or not isinstance(samples, Sequence):
            raise SignalSchemaError("samples must be a sequence")
        extra = {key: item for key, item in value.items() if key not in {"signal_id", "samples"}}
        if extra and not allow_unknown:
            raise SignalSchemaError(f"unknown series fields: {sorted(extra)}")
        series = cls(
            value["signal_id"],
            tuple(SignalSample.from_dict(item, allow_unknown=allow_unknown) for item in samples),
            extra,
        )
        if descriptor is not None:
            series.validate(descriptor)
        return series


@dataclass(frozen=True, slots=True)
class SignalSchema:
    """Versioned collection of descriptors with forward-compatible fields."""

    schema_version: SchemaVersion
    signals: tuple[SignalDescriptor, ...]
    metadata: Mapping[str, JsonValue] = field(default_factory=dict)
    extra: Mapping[str, JsonValue] = field(default_factory=dict)

    def __post_init__(self) -> None:
        if isinstance(self.schema_version, bool) or not isinstance(self.schema_version, (int, str)):
            raise SignalSchemaError("schema_version must be a non-empty string or positive integer")
        if isinstance(self.schema_version, str) and not self.schema_version.strip():
            raise SignalSchemaError("schema_version must be a non-empty string or positive integer")
        if isinstance(self.schema_version, int) and self.schema_version <= 0:
            raise SignalSchemaError("schema_version must be a non-empty string or positive integer")
        signals = tuple(self.signals)
        if any(not isinstance(signal, SignalDescriptor) for signal in signals):
            raise SignalSchemaError("signals must contain SignalDescriptor values")
        identifiers = [signal.identifier for signal in signals]
        duplicates = sorted(
            identifier for identifier, count in Counter(identifiers).items() if count > 1
        )
        if duplicates:
            raise SignalSchemaError(f"duplicate signal identifiers: {duplicates}")
        object.__setattr__(
            self, "signals", tuple(sorted(signals, key=lambda signal: signal.identifier))
        )
        object.__setattr__(self, "metadata", _freeze_mapping(self.metadata, "metadata"))
        object.__setattr__(self, "extra", _freeze_mapping(self.extra, "extra"))
        _check_extra(self.extra, {"metadata", "schema_version", "signals"})

    def to_dict(self) -> dict[str, JsonValue]:
        result = _mapping_to_dict(self.extra)
        result.update(
            {
                "metadata": _mapping_to_dict(self.metadata),
                "schema_version": self.schema_version,
                "signals": tuple(signal.to_dict() for signal in self.signals),
            }
        )
        return result

    def to_json(self) -> str:
        try:
            return json.dumps(self.to_dict(), ensure_ascii=False, allow_nan=False, sort_keys=True)
        except (ValueError, RecursionError) as error:
            raise SignalSchemaError("schema cannot be represented as JSON") from error

    @classmethod
    def from_json(cls, value: str | bytes, *, allow_unknown: bool = True) -> SignalSchema:
        if not isinstance(value, (str, bytes)):
            raise SignalSchemaError("schema JSON must be text or bytes")

        def pairs(items):
            result = {}
            for key, item in items:
                if key in result:
                    raise SignalSchemaError(f"duplicate JSON key: {key}")
                result[key] = item
            return result

        try:
            data = json.loads(value, object_pairs_hook=pairs)
        except (ValueError, UnicodeDecodeError, RecursionError) as error:
            raise SignalSchemaError(f"invalid schema JSON: {error}") from error
        return cls.from_dict(data, allow_unknown=allow_unknown)

    @classmethod
    def from_dict(cls, value: object, *, allow_unknown: bool = True) -> SignalSchema:
        if not isinstance(value, Mapping):
            raise SignalSchemaError("signal schema must be an object")
        _check_keys(value, "schema")
        if "schema_version" not in value or "signals" not in value:
            raise SignalSchemaError("signal schema requires schema_version and signals")
        signals = value["signals"]
        if isinstance(signals, (str, bytes)) or not isinstance(signals, Sequence):
            raise SignalSchemaError("signals must be a sequence")
        known = {"metadata", "schema_version", "signals"}
        extra = {key: item for key, item in value.items() if key not in known}
        if extra and not allow_unknown:
            raise SignalSchemaError(f"unknown schema fields: {sorted(extra)}")
        metadata = value.get("metadata", {})
        if not isinstance(metadata, Mapping):
            raise SignalSchemaError("metadata must be an object")
        return cls(
            schema_version=value["schema_version"],
            signals=tuple(
                SignalDescriptor.from_dict(signal, allow_unknown=allow_unknown)
                for signal in signals
            ),
            metadata=metadata,
            extra=extra,
        )


class SignalRegistry:
    """Deterministic hierarchical registry for immutable signal descriptors."""

    def __init__(self, schema: SignalSchema | None = None) -> None:
        self._signals: dict[str, SignalDescriptor] = {}
        if schema is not None:
            self.register_schema(schema)

    def register(self, descriptor: SignalDescriptor) -> None:
        if not isinstance(descriptor, SignalDescriptor):
            raise SignalSchemaError("registry requires SignalDescriptor")
        if descriptor.identifier in self._signals:
            raise RegistryError(
                f"Identifier '{descriptor.identifier}' is already registered in registry 'signals'"
            )
        self._signals[descriptor.identifier] = descriptor

    def register_schema(self, schema: SignalSchema) -> None:
        if not isinstance(schema, SignalSchema):
            raise SignalSchemaError("registry requires SignalSchema")
        pending = {descriptor.identifier: descriptor for descriptor in schema.signals}
        conflicts = sorted(set(pending) & self._signals.keys())
        if conflicts:
            raise RegistryError(
                f"Identifiers already registered in registry 'signals': {conflicts}"
            )
        self._signals.update(pending)

    def get(self, identifier: str) -> SignalDescriptor:
        try:
            return self._signals[identifier]
        except KeyError as exc:
            raise KeyError(f"Unknown signal identifier '{identifier}'") from exc

    def descriptors(self) -> tuple[SignalDescriptor, ...]:
        return tuple(self._signals[identifier] for identifier in sorted(self._signals))

    def search(self, query: str) -> tuple[SignalDescriptor, ...]:
        if not isinstance(query, str):
            raise SignalSchemaError("query must be a string")
        tokens = set(_TOKEN.findall(query.lower()))
        if not tokens:
            return self.descriptors()
        return tuple(
            descriptor
            for descriptor in self.descriptors()
            if tokens.issubset(descriptor.search_tokens)
        )

    def under_path(self, path: Sequence[str]) -> tuple[SignalDescriptor, ...]:
        if not isinstance(path, (str, bytes)) and isinstance(path, Sequence) and not path:
            return self.descriptors()
        normalized = _validate_path(path)
        return tuple(
            descriptor
            for descriptor in self.descriptors()
            if descriptor.path[: len(normalized)] == normalized
        )

    def __contains__(self, identifier: str) -> bool:
        return identifier in self._signals

    def __len__(self) -> int:
        return len(self._signals)
