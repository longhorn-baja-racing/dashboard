"""Quaternion attitude at a time: declared layouts only, no interpolation, explicit states."""

from __future__ import annotations

import math

import pytest

from lbr_dashboard.core import (
    SignalDescriptor,
    SignalSample,
    SignalSchema,
    SimulatorSource,
    TelemetrySession,
    example_signals,
)
from lbr_dashboard.core.frames import Rotation, RotationError
from lbr_dashboard.core.orientation import (
    DEFAULT_MAX_AGE_NS,
    AttitudeState,
    OrientationError,
    attitude_at,
    default_max_age_ns,
    quaternion_layout,
)

MS = 1_000_000
HALF = math.sqrt(0.5)


def quaternion(
    identifier: str = "bench.q",
    order: str | None = "wxyz",
    convention: str | None = "hamilton",
    **extra: str,
):
    metadata = dict(extra)
    if order is not None:
        metadata["quaternion_order"] = order
    if convention is not None:
        metadata["quaternion_convention"] = convention
    return SignalDescriptor(
        identifier,
        "Attitude",
        ("bench",),
        "vector",
        "bench",
        shape=(4,),
        nominal_rate_hz=100,
        timestamp_domain="relative",
        extra=metadata,
    )


def snapshot_of(descriptor, samples):
    session = TelemetrySession(SignalSchema(1, (descriptor,)))
    session.extend(descriptor.identifier, samples)
    return session.snapshot()


def sample(t_ms, value, quality=None, **extra):
    return SignalSample(
        t_ms * MS,
        value,
        available=value is not None,
        quality=quality,
        timestamp_domain="relative",
        extra=extra,
    )


def test_only_declared_four_vectors_are_quaternions() -> None:
    layout = quaternion_layout(quaternion(frame="imu", yaw_reference="magnetic north"))
    assert (layout.order, layout.convention, layout.frame) == ("wxyz", "hamilton", "imu")
    assert layout.yaw_reference == "magnetic north" and layout.reference is None
    three = SignalDescriptor(
        "bench.v", "Vec", ("bench",), "vector", "bench", shape=(3,), extra={"frame": "a"}
    )
    with pytest.raises(OrientationError, match="four-component vector"):
        quaternion_layout(three)
    with pytest.raises(OrientationError, match="component order"):
        quaternion_layout(quaternion(order=None))
    with pytest.raises(OrientationError, match="only 'wxyz' and 'xyzw'"):
        quaternion_layout(quaternion(order="wzyx"))
    with pytest.raises(OrientationError, match="convention"):
        quaternion_layout(quaternion(convention=None))
    with pytest.raises(OrientationError, match="only 'hamilton'"):
        quaternion_layout(quaternion(convention="jpl"))
    scalar = SignalDescriptor("bench.s", "S", ("bench",), "numeric", "bench")
    with pytest.raises(OrientationError, match="numeric"):
        quaternion_layout(scalar)


@pytest.mark.parametrize(
    ("value", "order", "expected"),
    [
        ((1, 0, 0, 0), "wxyz", (0, 0, 0)),
        ((HALF, 0, 0, HALF), "wxyz", (0, 0, 90)),
        ((0, 0, HALF, HALF), "xyzw", (0, 0, 90)),
        ((HALF, HALF, 0, 0), "wxyz", (90, 0, 0)),
        ((HALF, 0, -HALF, 0), "wxyz", (0, -90, 0)),
        ((-HALF, 0, 0, -HALF), "wxyz", (0, 0, 90)),
        ((0, 0, 0, 1), "wxyz", (0, 0, 180)),
    ],
)
def test_known_rotations_give_known_angles(value, order, expected) -> None:
    layout = quaternion_layout(quaternion(order=order))
    rotation = layout.rotation(value)
    roll, pitch, yaw = rotation.euler()
    assert (roll, pitch, abs(yaw) if expected[2] == 180 else yaw) == pytest.approx(
        expected, abs=1e-9
    )


def test_sign_equivalent_quaternions_show_the_same_attitude() -> None:
    layout = quaternion_layout(quaternion())
    q = Rotation.from_euler(20, -10, 135).quaternion
    a = layout.rotation(q)
    b = layout.rotation(tuple(-item for item in q))
    assert a.is_close(b) and a.euler() == pytest.approx(b.euler())


def test_invalid_components_are_rejected() -> None:
    layout = quaternion_layout(quaternion())
    for bad, match in (
        ((2, 0, 0, 0), "norm is 2"),
        ((0, 0, 0, 0), "norm is 0"),
        ((math.nan, 0, 0, 0), "not finite"),
        ((1, 0, 0), "four"),
        ((True, 0, 0, 0), "not a number"),
    ):
        with pytest.raises(RotationError, match=match):
            layout.rotation(bad)
    # Within the 0.01 tolerance the reading is normalized.
    assert layout.rotation((1.005, 0, 0, 0)).is_close(Rotation.identity())


def test_states_follow_gaps_unavailability_and_calibration_without_interpolating() -> None:
    descriptor = quaternion()
    layout = quaternion_layout(descriptor)
    view = snapshot_of(
        descriptor,
        [
            sample(0, (1, 0, 0, 0)),
            sample(10, (HALF, 0, 0, HALF)),
            sample(20, None, "NOT_CALIBRATED"),
            sample(30, (2, 0, 0, 0)),
            sample(40, (1, 0, 0, 0), "uncalibrated"),
            sample(50, (1, 0, 0, 0), calibrated=False),
            sample(60, (1, 0, 0, 0)),
        ],
    )
    limit = default_max_age_ns(descriptor)
    assert limit == 30 * MS

    def at(t_ms, max_age=limit):
        return attitude_at(view, "bench.q", None if t_ms is None else t_ms * MS, layout, max_age)

    assert at(None).state is AttitudeState.NO_DATA
    assert at(-1).state is AttitudeState.NO_DATA
    first = at(5)
    assert first.state is AttitudeState.VALID and first.is_current
    # Halfway between identity and yaw 90 the last reading is shown, never a blend.
    assert first.euler == pytest.approx((0, 0, 0))
    assert (first.sample_ns, first.age_ns) == (0, 5 * MS)
    assert at(15).euler == pytest.approx((0, 0, 90))
    missing = at(25)
    assert missing.state is AttitudeState.MISSING and missing.rotation is None
    assert "NOT_CALIBRATED" in missing.message and not missing.is_current
    invalid = at(30)
    assert invalid.state is AttitudeState.INVALID and invalid.rotation is None
    for t in (40, 50):
        reading = at(t)
        assert reading.state is AttitudeState.UNCALIBRATED and not reading.is_current
        assert reading.rotation is not None
    assert at(60).state is AttitudeState.VALID
    stale = at(91)
    assert stale.state is AttitudeState.STALE and stale.rotation is None
    assert stale.euler is None and "Stale" in stale.message
    assert at(90).state is AttitudeState.VALID
    assert at(200, max_age=500 * MS).state is AttitudeState.VALID
    # Seeking backwards gives the same answer as a fresh query.
    assert at(15) == at(15)
    assert attitude_at(view, "other", 0, layout, limit).state is AttitudeState.NO_DATA


def test_default_stale_limit_without_a_rate() -> None:
    descriptor = SignalDescriptor(
        "bench.q", "Q", ("bench",), "vector", "bench", shape=(4,), timestamp_domain="relative"
    )
    assert default_max_age_ns(descriptor) == DEFAULT_MAX_AGE_NS


def test_fictional_attitude_family_declares_its_quaternion() -> None:
    source = SimulatorSource(example_signals("attitude"), seed=3, duration_ns=3_000_000_000)
    session = TelemetrySession(source.schema)
    source.open()
    source.start()
    try:
        assert source.join(2)
        session.ingest_source(source)
    finally:
        assert source.close(1)
    view = session.snapshot()
    descriptors = {item.identifier: item for item in view.schema.signals}
    assert set(descriptors) == {"attitude.quaternion", "attitude.acceleration", "attitude.rate"}
    assert "Fictional" in descriptors["attitude.quaternion"].description
    layout = quaternion_layout(descriptors["attitude.quaternion"])
    assert layout.yaw_reference is not None and "fictional" in layout.yaw_reference
    records = view.series["attitude.quaternion"]
    # 61 attempts at 20 Hz over 3 s: 5 stale gaps, 4 explicit unavailable readings.
    assert len(records) == 56
    assert sum(not record.sample.available for record in records) == 4
    for record in records:
        if record.sample.available:
            layout.rotation(record.sample.value)
    assert layout.rotation(records[10].sample.value).euler()[2] == pytest.approx(90, abs=1e-6)
    with pytest.raises(ValueError, match="attitude"):
        example_signals("unknown")
