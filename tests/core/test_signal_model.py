"""Generic signal-model acceptance, validation, and serialization boundaries."""

import json
from dataclasses import FrozenInstanceError, replace
from pathlib import Path
from typing import Any, cast

import pytest

from lbr_dashboard.core import (
    SignalAvailability,
    SignalDescriptor,
    SignalRegistry,
    SignalSample,
    SignalSchema,
    SignalSchemaError,
    SignalSeries,
)
from lbr_dashboard.core.registry import RegistryError

FIXTURE = Path(__file__).parents[1] / "fixtures/generic_signals.json"


def descriptor(**overrides: Any) -> SignalDescriptor:
    data: dict[str, Any] = dict(
        identifier="lab.pressure",
        display_name="Pressure",
        path=("lab", "pressure"),
        data_type="numeric",
        source_id="adapter",
    )
    data.update(overrides)
    return SignalDescriptor(**data)


def test_unrelated_families_sources_and_unknown_fields_round_trip() -> None:
    schema = SignalSchema.from_json(FIXTURE.read_bytes())
    again = SignalSchema.from_json(schema.to_json())
    assert again.to_dict() == schema.to_dict()
    assert {item.source_id for item in schema.signals} == {
        "csv-replay",
        "simulator",
        "live-adapter",
        "future-adapter",
    }
    registry = SignalRegistry(schema)
    assert len(registry.search("pressure")) == 2
    future = registry.get("future.spectrum")
    assert not future.is_known_type
    assert future.unknown_fields["future_metadata"] == {" k ": (1, 2)}
    assert registry.get("replay.status").availability.extra["future_reason_code"] == 17
    assert schema.extra["future_schema_option"] == {"preserve": True}
    assert [item.identifier for item in registry.under_path(("lab", "pressure"))] == [
        "live.pressure",
        "replay.pressure",
    ]


def test_100hz_and_1hz_keep_independent_timelines_and_missingness() -> None:
    registry = SignalRegistry(SignalSchema.from_json(FIXTURE.read_text(encoding="utf-8")))
    fast_samples = [
        SignalSample(
            i * 10_000_000,
            None if i == 50 else i,
            available=i != 50,
            quality="not-recorded" if i == 50 else "valid",
            timestamp_domain="relative",
            source_id="csv-replay",
            unit="psi",
        )
        for i in range(101)
    ]
    fast = SignalSeries.from_samples(
        "replay.pressure", fast_samples, descriptor=registry.get("replay.pressure")
    )
    slow = SignalSeries.from_samples(
        "sim.temperature",
        [
            SignalSample(0, 20, timestamp_domain="relative", source_id="simulator", unit="°C"),
            SignalSample(
                1_000_000_000, 21, timestamp_domain="relative", source_id="simulator", unit="°C"
            ),
        ],
        descriptor=registry.get("sim.temperature"),
    )
    assert len(fast.samples) == 101 and len(slow.samples) == 2
    assert [sample.timestamp_ns for sample in slow.samples] == [0, 1_000_000_000]
    assert not fast.samples[50].available and fast.samples[50].value is None
    assert all(sample.unit == "psi" for sample in fast.samples)  # No #21 unit conversion here.
    encoded = json.loads(json.dumps(fast.to_dict()))
    assert SignalSeries.from_dict(encoded, descriptor=registry.get(fast.signal_id)) == fast
    fast_samples.clear()
    assert len(fast.samples) == 101


@pytest.mark.parametrize(
    ("kind", "shape", "value"),
    [
        ("numeric", (), 17),
        ("numeric", (), -1.5),
        ("boolean", (), False),
        ("string", (), "ready"),
        ("event", (), {"code": "started", "details": [1, 2]}),
        ("status", (), {"healthy": True}),
        ("vector", (3,), [1, 2.5, -3]),
        ("vector", (2, 2), [[1, 2], [3, 4]]),
        ("future_type", (2,), {"opaque": "data"}),
    ],
)
def test_known_and_opaque_sample_types(kind, shape, value) -> None:
    model = descriptor(data_type=kind, shape=shape)
    series = SignalSeries.from_samples(model.identifier, [SignalSample(0, value)], descriptor=model)
    series.validate(model)


@pytest.mark.parametrize(
    ("kind", "shape", "value"),
    [
        ("numeric", (), True),
        ("numeric", (), "17"),
        ("boolean", (), 0),
        ("string", (), {}),
        ("vector", (3,), [1, 2]),
        ("vector", (3,), [1, True, 3]),
        ("vector", (2, 2), [[1, 2], [3]]),
        ("vector", (2,), "12"),
    ],
)
def test_sample_value_must_match_known_type_and_shape(kind, shape, value) -> None:
    model = descriptor(data_type=kind, shape=shape)
    with pytest.raises(SignalSchemaError):
        SignalSeries.from_samples(model.identifier, [SignalSample(0, value)], descriptor=model)


@pytest.mark.parametrize(
    "overrides",
    [
        {"identifier": "Bad.id"},
        {"identifier": " a.x"},
        {"identifier": "a..x"},
        {"identifier": ""},
        {"source_id": "bad source"},
        {"device_id": ""},
        {"display_name": " "},
        {"path": ()},
        {"path": "lab"},
        {"path": ("Upper",)},
        {"path": (1,)},
        {"data_type": "Bad"},
        {"data_type": "vector"},
        {"shape": (1,)},
        {"data_type": "vector", "shape": (0,)},
        {"data_type": "vector", "shape": (True,)},
        {"data_type": "vector", "shape": (1.5,)},
        {"shape": "3"},
        {"canonical_unit": "m per s"},
        {"canonical_unit": ""},
        {"canonical_unit": 1},
        {"nominal_rate_hz": 0},
        {"nominal_rate_hz": -1},
        {"nominal_rate_hz": True},
        {"nominal_rate_hz": "100"},
        {"nominal_rate_hz": float("nan")},
        {"nominal_rate_hz": float("inf")},
        {"nominal_rate_hz": 10**1000},
        {"description": 1},
        {"availability": {}},
        {"calibration": []},
        {"timestamp_domain": " "},
        {"timestamp_domain": 1},
        {"extra": {"id": "override"}},
        {"extra": {"path": ["override"]}},
        {"extra": {"source_id": "override"}},
    ],
)
def test_invalid_descriptor_fields_are_rejected(overrides) -> None:
    with pytest.raises(SignalSchemaError):
        descriptor(**overrides)


@pytest.mark.parametrize("timestamp", [True, 1.5, float("nan"), float("inf"), "1", None])
def test_invalid_sample_timestamps(timestamp) -> None:
    with pytest.raises(SignalSchemaError, match="integer"):
        SignalSample(timestamp, 1)


def test_clock_domains_preserve_signed_precise_times_without_wire_width_assumptions() -> None:
    samples = (
        SignalSample(-20_000_001, 1, timestamp_domain="relative"),
        SignalSample(2**80 + 1, 2, timestamp_domain="external_clock"),
        SignalSample(0, 3, timestamp_domain="device_monotonic"),
    )
    for sample in samples:
        assert SignalSample.from_dict(json.loads(json.dumps(sample.to_dict()))) == sample
    series = SignalSeries("lab.pressure", samples + (samples[0],))
    assert series.samples == samples + (samples[0],)  # No sorting, deduplication, or time shifting.


@pytest.mark.parametrize(
    "overrides",
    [
        {"available": 1},
        {"available": True, "value": None},
        {"quality": ""},
        {"quality": 1},
        {"unit": ""},
        {"unit": "Pa unit"},
        {"unit": 3},
        {"timestamp_domain": " "},
        {"source_id": "BAD"},
        {"extra": {"value": 0}},
    ],
)
def test_invalid_sample_metadata(overrides) -> None:
    data: dict[str, Any] = {"timestamp_ns": 0, "value": 1}
    data.update(overrides)
    with pytest.raises(SignalSchemaError):
        SignalSample(**data)


def test_descriptor_binding_checks_identity_and_clock_domain() -> None:
    model = descriptor(timestamp_domain="device_monotonic")
    for sample in (
        SignalSample(0, 1, source_id="other", timestamp_domain="device_monotonic"),
        SignalSample(0, 1, timestamp_domain="relative"),
    ):
        with pytest.raises(SignalSchemaError):
            model.validate_sample(sample)
    with pytest.raises(SignalSchemaError):
        SignalSeries("other.id", ()).validate(model)
    with pytest.raises(SignalSchemaError):
        model.validate_sample(cast(Any, {}))
    model.validate_sample(
        SignalSample(0, None, available=False, timestamp_domain="device_monotonic")
    )


@pytest.mark.parametrize(
    "value",
    [
        float("nan"),
        float("inf"),
        {"nested": [float("nan")]},
        b"bytes",
        bytearray(b"bytes"),
        {1: "not-string"},
        {1, 2},
        object(),
    ],
)
def test_samples_reject_non_json_values(value) -> None:
    with pytest.raises(SignalSchemaError):
        SignalSample(0, value)


def test_nested_snapshots_and_serialized_copies_are_independent() -> None:
    raw = {"levels": [1, {"healthy": True}]}
    model = descriptor(calibration=raw)
    raw["levels"].append(99)
    assert model.calibration["levels"] == (1, {"healthy": True})
    with pytest.raises(TypeError):
        cast(Any, model.calibration)["levels"][1]["healthy"] = False
    with pytest.raises(FrozenInstanceError):
        cast(Any, model).display_name = "Changed"
    encoded: Any = model.to_dict()
    encoded["calibration"]["levels"][1]["healthy"] = False
    assert cast(Any, model.calibration)["levels"][1]["healthy"] is True


def test_cyclic_and_excessively_nested_metadata_are_rejected() -> None:
    cycle: Any = {}
    cycle["self"] = cycle
    with pytest.raises(SignalSchemaError, match="cyclic"):
        descriptor(calibration=cycle)
    deep: Any = 1
    for _ in range(66):
        deep = [deep]
    with pytest.raises(SignalSchemaError, match="nesting"):
        SignalSample(0, deep)
    shared = {"values": [1, 2]}
    assert SignalSample(0, [shared, shared]).value == ({"values": (1, 2)}, {"values": (1, 2)})


@pytest.mark.parametrize("version", [None, False, 0, -1, 1.5, " "])
def test_invalid_schema_version(version) -> None:
    with pytest.raises(SignalSchemaError):
        SignalSchema(version, ())


def test_empty_and_future_versioned_schemas_are_lossless() -> None:
    for version in (1, "1", "future-v2"):
        model = SignalSchema(version, (), extra={" future ": {"": "preserved"}})
        assert SignalSchema.from_json(model.to_json()).to_dict() == model.to_dict()


@pytest.mark.parametrize(
    "payload",
    [
        None,
        {},
        {"signals": []},
        {"schema_version": 1},
        {"schema_version": 1, "signals": "wrong"},
        {"schema_version": 1, "signals": [{}]},
        {"schema_version": 1, "signals": [], "metadata": []},
    ],
)
def test_invalid_schema_objects(payload) -> None:
    with pytest.raises(SignalSchemaError):
        SignalSchema.from_dict(payload)


@pytest.mark.parametrize(
    "payload",
    [
        b"\xff",
        "{",
        "[]",
        '{"schema_version":1,"schema_version":2,"signals":[]}',
        '{"schema_version":1,"signals":[],"metadata":{"x":NaN}}',
        None,
    ],
)
def test_invalid_or_duplicate_json_is_rejected(payload) -> None:
    with pytest.raises(SignalSchemaError):
        SignalSchema.from_json(payload)


def test_strict_unknown_fields_recurse_and_permissive_fields_round_trip() -> None:
    payload: Any = descriptor().to_dict()
    payload["availability"]["future"] = {"v": 1}
    with pytest.raises(SignalSchemaError, match="unknown"):
        SignalDescriptor.from_dict(payload, allow_unknown=False)
    payload["future"] = 17
    with pytest.raises(SignalSchemaError, match="unknown"):
        SignalDescriptor.from_dict(payload, allow_unknown=False)
    schema = {"schema_version": 1, "signals": [payload], "extra": True}
    with pytest.raises(SignalSchemaError, match="unknown"):
        SignalSchema.from_dict(schema, allow_unknown=False)
    schema.pop("extra")
    with pytest.raises(SignalSchemaError, match="unknown"):
        SignalSchema.from_dict(schema, allow_unknown=False)
    sample = SignalSample.from_dict({"timestamp_ns": 0, "value": 1, "future": {"x": [1, 2]}})
    assert sample.to_dict()["future"] == {"x": [1, 2]}
    with pytest.raises(SignalSchemaError, match="unknown"):
        SignalSample.from_dict(sample.to_dict(), allow_unknown=False)
    series = SignalSeries.from_dict(
        {"signal_id": "a.x", "samples": [sample.to_dict()], "future": 1}
    )
    assert SignalSeries.from_dict(series.to_dict()).to_dict() == series.to_dict()
    with pytest.raises(SignalSchemaError, match="unknown"):
        SignalSeries.from_dict(series.to_dict(), allow_unknown=False)


def test_registry_hierarchy_order_duplicates_and_atomic_batches() -> None:
    values = [
        descriptor(identifier="a.z"),
        descriptor(identifier="a.x"),
        descriptor(identifier="b.y"),
    ]
    registry = SignalRegistry(SignalSchema(1, tuple(reversed(values))))
    assert [item.identifier for item in registry.descriptors()] == ["a.x", "a.z", "b.y"]
    assert registry.search("") == registry.descriptors()
    assert registry.under_path(()) == registry.descriptors()
    assert registry.under_path(("unknown",)) == ()
    assert "a.x" in registry and len(registry) == 3
    assert registry.search("PRESSURE csv") == ()
    with pytest.raises(RegistryError):
        registry.register(values[0])
    with pytest.raises(KeyError, match="Unknown"):
        registry.get("missing")
    for value in (None, "wrong"):
        with pytest.raises(SignalSchemaError):
            registry.register(cast(Any, value))
        with pytest.raises(SignalSchemaError):
            registry.register_schema(cast(Any, value))
    with pytest.raises(SignalSchemaError):
        registry.search(cast(Any, None))
    with pytest.raises(SignalSchemaError):
        registry.under_path("lab")
    with pytest.raises(SignalSchemaError):
        SignalSchema(1, (values[0], replace(values[0], source_id="another")))


def test_large_schema_registration_is_deterministic_without_quadratic_duplicate_scan() -> None:
    signals = tuple(descriptor(identifier=f"custom.s{i}") for i in range(10000))
    schema = SignalSchema(1, signals)
    registry = SignalRegistry(schema)
    assert len(registry) == 10000
    assert registry.descriptors()[0].identifier == "custom.s0"
    assert registry.descriptors()[-1].identifier == "custom.s9999"


@pytest.mark.parametrize(
    "factory,payload",
    [
        (SignalAvailability.from_dict, {1: 0, "future": 1}),
        (SignalDescriptor.from_dict, {**descriptor().to_dict(), 1: 0, "future": 1}),
        (SignalSample.from_dict, {"timestamp_ns": 0, "value": 1, 1: 0, "future": 1}),
        (SignalSeries.from_dict, {"signal_id": "a.x", "samples": [], 1: 0, "future": 1}),
        (SignalSchema.from_dict, {"schema_version": 1, "signals": [], 1: 0, "future": 1}),
    ],
)
@pytest.mark.parametrize("strict", [True, False])
def test_serialization_rejects_non_string_keys(factory, payload, strict) -> None:
    with pytest.raises(SignalSchemaError, match="keys must be strings"):
        factory(payload, allow_unknown=not strict)


@pytest.mark.parametrize(
    "factory,payload",
    [
        (SignalAvailability.from_dict, []),
        (SignalAvailability.from_dict, {"available": 0}),
        (SignalAvailability.from_dict, {"available": False}),
        (SignalAvailability.from_dict, {"reason": " "}),
        (SignalDescriptor.from_dict, []),
        (SignalDescriptor.from_dict, {}),
        (SignalDescriptor.from_dict, {**descriptor().to_dict(), "display": []}),
        (SignalSample.from_dict, []),
        (SignalSample.from_dict, {"timestamp_ns": 0}),
        (SignalSeries.from_dict, {}),
        (SignalSeries.from_dict, {"signal_id": "a.x", "samples": "bad"}),
        (SignalSeries.from_dict, {"signal_id": "a.x", "samples": [0]}),
    ],
)
def test_serialization_rejects_malformed_objects(factory, payload) -> None:
    with pytest.raises(SignalSchemaError):
        factory(payload)


def test_empty_series_generator_and_metadata_search() -> None:
    model = descriptor(device_id="external-device", description="Hydraulic pressure")
    registry = SignalRegistry(SignalSchema(1, (model,)))
    assert registry.search("HYDRAULIC external device") == (model,)
    series = SignalSeries.from_samples(model.identifier, iter(()))
    assert series.samples == ()
    assert SignalSeries.from_dict(series.to_dict(), descriptor=model) == series
    with pytest.raises(SignalSchemaError):
        SignalSeries(model.identifier, cast(Any, (1,)))
    with pytest.raises(SignalSchemaError):
        SignalSchema(1, cast(Any, (1,)))
