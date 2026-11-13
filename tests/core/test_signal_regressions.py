"""Reproductions of issue #18's reported data-loss failures."""

from dataclasses import replace

import pytest

from lbr_dashboard.core.registry import RegistryError
from lbr_dashboard.core.signals import (
    SignalDescriptor,
    SignalRegistry,
    SignalSample,
    SignalSchema,
    SignalSchemaError,
)


def descriptor(identifier="a.y", **kwargs):
    return SignalDescriptor(identifier, "Pressure", ("lab", "pressure"), "numeric", "csv", **kwargs)


def test_schema_registration_is_atomic_on_collision_and_retry() -> None:
    existing = descriptor()
    registry = SignalRegistry(SignalSchema(1, (existing,)))
    pending = SignalSchema(1, (replace(existing, identifier="a.x"), existing))
    for _ in range(2):
        with pytest.raises(RegistryError, match="a.y"):
            registry.register_schema(pending)
        assert registry.descriptors() == (existing,)


def test_metadata_keys_are_preserved_without_stripping_collisions() -> None:
    value = descriptor(calibration={"k": 1, " k": 2}, extra={" key ": {"x": 3, " x": 4}})
    assert dict(value.calibration) == {"k": 1, " k": 2}
    encoded = value.to_dict()
    assert encoded[" key "] == {"x": 3, " x": 4}
    assert SignalDescriptor.from_dict(encoded).to_dict() == encoded


@pytest.mark.parametrize("value", [0, False, "missing", [], {}])
def test_unavailable_sample_cannot_carry_a_value(value) -> None:
    with pytest.raises(SignalSchemaError, match="unavailable"):
        SignalSample(0, value, available=False)


def test_sample_value_is_an_immutable_snapshot() -> None:
    original = {"status": [1, {"healthy": True}]}
    sample = SignalSample(0, original)
    original["status"].append(99)
    assert sample.value == {"status": (1, {"healthy": True})}
    with pytest.raises(TypeError):
        sample.value["status"][1]["healthy"] = False
