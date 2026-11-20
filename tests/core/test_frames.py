"""Rigid rotations and frame transforms applied only to vectors with a declared frame."""

from __future__ import annotations

import json
import math
from random import Random

import numpy as np
import pytest

from lbr_dashboard.core import SignalDescriptor, SignalSample, SignalSchema, TelemetrySession
from lbr_dashboard.core.export import signal_frame
from lbr_dashboard.core.frames import (
    FrameTransform,
    FrameTransformSet,
    Rotation,
    RotationError,
    TransformError,
    is_transformed,
    parse_rotation,
    plan,
    refusal,
)

X, Y, Z = (1.0, 0.0, 0.0), (0.0, 1.0, 0.0), (0.0, 0.0, 1.0)
TOL = 1e-12


def close(actual, expected, tol=TOL) -> bool:
    return bool(np.allclose(np.asarray(actual, dtype=float), expected, atol=tol, rtol=0))


def vector(identifier: str, frame_key: str | None = "frame", frame: str = "sensor", **kwargs):
    metadata = {frame_key: frame} if frame_key else {}
    where = kwargs.pop("where", "extra")
    return SignalDescriptor(
        identifier,
        identifier.rsplit(".", 1)[-1].title(),
        ("bench",),
        "vector",
        "bench",
        shape=kwargs.pop("shape", (3,)),
        canonical_unit="m/s^2",
        timestamp_domain="relative",
        **{where: metadata},
        **kwargs,
    )


def session_with(*readings: tuple[SignalDescriptor, list[tuple[int, object]]]) -> TelemetrySession:
    session = TelemetrySession(SignalSchema(1, tuple(item for item, _ in readings)))
    for descriptor, samples in readings:
        session.extend(
            descriptor.identifier,
            (
                SignalSample(
                    t,
                    value,
                    available=value is not None,
                    quality=None if value is not None else "SENSOR_FAULT",
                    timestamp_domain="relative",
                )
                for t, value in samples
            ),
        )
    return session


YAW_90 = FrameTransform("vehicle", "sensor", "vehicle", Rotation.from_axis_angle(Z, 90))


# Rotations -------------------------------------------------------------------


def test_identity_leaves_vectors_unchanged() -> None:
    identity = Rotation.identity()
    vectors = [[1.5, -2.0, 3.25], [0.0, 0.0, 0.0], [1e6, -1e-6, 7.0]]
    assert close(identity.apply(vectors), vectors)
    assert close(identity.matrix, np.eye(3))
    assert identity.euler() == (0.0, 0.0, 0.0)


@pytest.mark.parametrize(
    ("axis", "mapping"),
    [
        (X, {Y: Z, Z: (0, -1, 0), X: X}),
        (Y, {Z: X, X: (0, 0, -1), Y: Y}),
        (Z, {X: Y, Y: (-1, 0, 0), Z: Z}),
    ],
)
def test_ninety_degree_rotations_map_known_axes(axis, mapping) -> None:
    rotation = Rotation.from_axis_angle(axis, 90)
    for source, expected in mapping.items():
        assert close(rotation.apply(source), expected)
    assert rotation.angle_degrees() == pytest.approx(90)
    # The same rotation from a matrix whose columns are the rotated axes.
    columns = np.column_stack([mapping[X], mapping[Y], mapping[Z]])
    assert Rotation.from_matrix(columns).is_close(rotation)
    assert close(rotation.matrix, columns)


def test_euler_angles_are_z_y_x_and_round_trip() -> None:
    assert Rotation.from_euler(0, 0, 90).is_close(Rotation.from_axis_angle(Z, 90))
    assert Rotation.from_euler(90, 0, 0).is_close(Rotation.from_axis_angle(X, 90))
    assert Rotation.from_euler(0, 90, 0).is_close(Rotation.from_axis_angle(Y, 90))
    composed = (
        Rotation.from_axis_angle(Z, 30)
        * Rotation.from_axis_angle(Y, 20)
        * Rotation.from_axis_angle(X, 10)
    )
    assert Rotation.from_euler(10, 20, 30).is_close(composed)
    assert Rotation.from_euler(10, 20, 30).euler() == pytest.approx((10, 20, 30))
    assert Rotation.from_euler(-170, -45, 179).euler() == pytest.approx((-170, -45, 179))
    # At gimbal lock roll is reported as 0 and the heading goes to yaw.
    assert Rotation.from_euler(0, 90, 30).euler() == pytest.approx((0, 90, 30))
    assert Rotation.from_euler(20, -90, 50).euler() == pytest.approx((0, -90, 70))
    assert Rotation.from_euler(20, 90, 50).euler() == pytest.approx((0, 90, 30))


def test_composition_applies_the_right_operand_first() -> None:
    a = Rotation.from_axis_angle(Z, 90)
    b = Rotation.from_axis_angle(X, 90)
    v = (0.3, -1.2, 2.5)
    assert close((a * b).apply(v), a.apply(b.apply(v)))
    assert close((a * b).matrix, a.matrix @ b.matrix)
    # X then Z: y -> z -> z; Z then X: y -> -x -> -x.
    assert close((a * b).apply(Y), Z)
    assert close((b * a).apply(Y), (-1, 0, 0))
    four = a * a * a * a
    assert four.is_close(Rotation.identity())


def test_inverse_round_trips_within_tolerance() -> None:
    random = Random(5)
    for _ in range(200):
        rotation = Rotation.from_axis_angle(
            [random.uniform(-1, 1) for _ in range(3)], random.uniform(-360, 360)
        )
        v = [random.uniform(-100, 100) for _ in range(3)]
        assert close(rotation.inverse().apply(rotation.apply(v)), v, 1e-9)
        assert (rotation * rotation.inverse()).is_close(Rotation.identity())
        assert Rotation.from_matrix(rotation.matrix).is_close(rotation)
        restored = Rotation.from_dict(json.loads(json.dumps(rotation.to_dict())))
        assert restored.is_close(rotation)


def test_sign_equivalent_quaternions_are_the_same_rotation() -> None:
    q = Rotation.from_euler(15, -30, 120)
    negated = Rotation(tuple(-value for value in q.quaternion))  # type: ignore[arg-type]
    assert negated.is_close(q)
    assert close(negated.matrix, q.matrix)
    assert negated.euler() == pytest.approx(q.euler())
    assert negated.canonical() == pytest.approx(q.canonical())
    assert q.canonical()[0] >= 0


def test_near_unit_quaternions_are_normalized_and_others_rejected() -> None:
    rotation = Rotation.from_quaternion(1.0005, 0, 0, 0)
    assert math.fsum(value * value for value in rotation.quaternion) == pytest.approx(1, abs=1e-15)
    half = math.sqrt(0.5)
    assert Rotation.from_quaternion(0.7071, 0, 0, 0.7071).is_close(
        Rotation.from_quaternion(half, 0, 0, half), 1e-8
    )
    for bad in ((2, 0, 0, 0), (0, 0, 0, 0), (0.5, 0.5, 0, 0)):
        with pytest.raises(RotationError, match="norm"):
            Rotation.from_quaternion(*bad)
    for bad in ((math.nan, 0, 0, 0), (1, math.inf, 0, 0)):
        with pytest.raises(RotationError, match="finite"):
            Rotation.from_quaternion(*bad)
    with pytest.raises(RotationError):
        Rotation((1.0, True, 0.0, 0.0))  # type: ignore[arg-type]
    with pytest.raises(RotationError, match="zero"):
        Rotation.from_axis_angle((0, 0, 0), 10)


def test_invalid_matrices_are_rejected() -> None:
    with pytest.raises(RotationError, match="reflection"):
        Rotation.from_matrix([[1, 0, 0], [0, 1, 0], [0, 0, -1]])
    with pytest.raises(RotationError, match="orthonormal"):
        Rotation.from_matrix([[2, 0, 0], [0, 1, 0], [0, 0, 1]])
    with pytest.raises(RotationError, match="3 rows"):
        Rotation.from_matrix([[1, 0], [0, 1]])
    with pytest.raises(RotationError, match="finite"):
        Rotation.from_matrix([[math.nan, 0, 0], [0, 1, 0], [0, 0, 1]])
    # Hand-typed values within the 1e-3 tolerance are accepted and made exact.
    typed = Rotation.from_matrix([[0.7071, -0.7071, 0], [0.7071, 0.7071, 0], [0, 0, 1]])
    assert typed.is_close(Rotation.from_axis_angle(Z, 45), 1e-8)


def test_parse_rotation_reads_each_format() -> None:
    expected = Rotation.from_axis_angle(Z, 90)
    assert parse_rotation("euler", "0, 0, 90").is_close(expected)
    assert parse_rotation("quaternion", "0.70710678 0 0 0.70710678").is_close(expected, 1e-8)
    assert parse_rotation("matrix", "0,-1,0; 1,0,0; 0,0,1").is_close(expected)
    with pytest.raises(RotationError, match="roll, pitch and yaw"):
        parse_rotation("euler", "1, 2")
    with pytest.raises(RotationError, match="not a number"):
        parse_rotation("quaternion", "1, a, 0, 0")
    with pytest.raises(RotationError, match="three rows"):
        parse_rotation("matrix", "1,0,0; 0,1,0")
    with pytest.raises(RotationError, match="unknown"):
        parse_rotation("axis", "1")


# Transforms --------------------------------------------------------------------


def test_transform_definitions_are_validated() -> None:
    rotation = Rotation.identity()
    for name in ("", "Vehicle", "1st", "a b", "x."):
        with pytest.raises(TransformError, match="name"):
            FrameTransform(name, "sensor", "vehicle", rotation)
    with pytest.raises(TransformError, match="differ"):
        FrameTransform("same", "sensor", " sensor ", rotation)
    with pytest.raises(TransformError, match="empty"):
        FrameTransform("t", "", "vehicle", rotation)
    with pytest.raises(TransformError, match="unspecified"):
        FrameTransform("t", "unspecified", "vehicle", rotation)
    with pytest.raises(TransformError, match="invalid rotation"):
        FrameTransform.from_dict(
            {
                "name": "t",
                "source_frame": "a",
                "target_frame": "b",
                "rotation": {"quaternion_wxyz": [2, 0, 0, 0]},
            }
        )


def test_only_three_vectors_with_the_matching_declared_frame_apply() -> None:
    framed = vector("bench.accel")
    unframed = vector("bench.raw", frame_key=None)
    other = vector("bench.mag", frame="chassis")
    via_display = vector("bench.gyro", frame_key="coordinate_frame", where="display")
    via_calibration = vector("bench.field", frame_key="reference_frame", where="calibration")
    quaternion = vector("bench.q", shape=(4,))
    scalar = SignalDescriptor("bench.speed", "Speed", ("bench",), "numeric", "bench")
    assert refusal(framed, YAW_90) is None
    assert refusal(via_display, YAW_90) is None
    assert refusal(via_calibration, YAW_90) is None
    assert "declares no coordinate frame" in (refusal(unframed, YAW_90) or "")
    assert refusal(other, YAW_90) == "is in frame 'chassis', not 'sensor'"
    assert "only three-component vectors" in (refusal(quaternion, YAW_90) or "")
    assert "only three-component vectors" in (refusal(scalar, YAW_90) or "")
    items = plan((framed, unframed, other, scalar), (YAW_90,))
    # Scalars are never considered; every vector gets an answer.
    assert [(item.descriptor.identifier, item.applies) for item in items] == [
        ("bench.accel", True),
        ("bench.raw", False),
        ("bench.mag", False),
    ]


def test_augment_adds_rotated_signals_and_keeps_raw_records() -> None:
    framed = vector("bench.accel")
    unframed = vector("bench.raw", frame_key=None)
    mismatched = vector("bench.mag", frame="chassis")
    session = session_with(
        (framed, [(0, [1.0, 0.0, 9.81]), (10, None), (20, [0.0, 2.0, 0.0])]),
        (unframed, [(0, [1.0, 0.0, 0.0])]),
        (mismatched, [(5, [1.0, 0.0, 0.0])]),
    )
    snapshot = session.snapshot()
    transforms = FrameTransformSet()
    assert transforms.augment(session, snapshot) is snapshot
    transforms.apply(YAW_90)
    view = transforms.augment(session, snapshot)

    ids = [item.identifier for item in view.schema.signals]
    assert ids == ["bench.accel", "bench.accel.vehicle", "bench.mag", "bench.raw"]
    for raw in ("bench.accel", "bench.raw", "bench.mag"):
        assert view.series[raw] is snapshot.series[raw]
    output = view.series["bench.accel.vehicle"]
    assert [record.sample.timestamp_ns for record in output] == [0, 10, 20]
    assert [record.sequence for record in output] == [
        record.sequence for record in snapshot.series["bench.accel"]
    ]
    # Rounding residue such as cos(90 deg) is written as an exact zero.
    assert output[0].sample.value == (0.0, 1.0, 9.81)
    assert not output[1].sample.available and output[1].sample.value is None
    assert output[1].sample.quality == "SENSOR_FAULT"
    assert close(output[2].sample.value, (-2.0, 0.0, 0.0))
    assert snapshot.series["bench.accel"][0].sample.value == (1.0, 0.0, 9.81)

    descriptor = next(item for item in view.schema.signals if is_transformed(item))
    assert signal_frame(descriptor) == "vehicle"
    assert descriptor.path == ("transformed", "bench")
    assert descriptor.canonical_unit == "m/s^2" and descriptor.shape == (3,)
    assert descriptor.timestamp_domain == "relative" and descriptor.source_id == "bench"
    assert "bench.accel rotated from frame 'sensor' to 'vehicle'" in descriptor.description
    provenance = descriptor.display["frame_transform"]
    assert isinstance(provenance, dict | type(descriptor.display))
    assert provenance["input"] == "bench.accel"
    assert provenance["source_frame"] == "sensor"
    assert view.domains["bench.accel.vehicle"] == "relative"


def test_mismatched_and_unframed_vectors_are_never_transformed() -> None:
    unframed = vector("bench.raw", frame_key=None)
    session = session_with((unframed, [(0, [1.0, 2.0, 3.0])]))
    transforms = FrameTransformSet()
    transforms.apply(YAW_90)
    transforms.apply(FrameTransform("chassis", "chassis", "vehicle", Rotation.identity()))
    snapshot = session.snapshot()
    view = transforms.augment(session, snapshot)
    assert [item.identifier for item in view.schema.signals] == ["bench.raw"]
    assert view.series["bench.raw"] is snapshot.series["bench.raw"]


def test_output_id_clash_with_a_recorded_signal_is_refused() -> None:
    framed = vector("bench.accel")
    clash = vector("bench.accel.vehicle", frame_key=None)
    session = session_with((framed, [(0, [1.0, 0.0, 0.0])]), (clash, [(0, [5.0, 5.0, 5.0])]))
    transforms = FrameTransformSet()
    transforms.apply(YAW_90)
    view = transforms.augment(session, session.snapshot())
    assert view.series["bench.accel.vehicle"][0].sample.value == (5.0, 5.0, 5.0)
    reasons = {item.descriptor.identifier: item.reason for item in transforms.plan(view)}
    assert "already used" in (reasons["bench.accel"] or "")


def test_new_readings_are_rotated_incrementally() -> None:
    framed = vector("bench.accel")
    session = session_with((framed, [(0, [1.0, 0.0, 0.0])]))
    transforms = FrameTransformSet()
    transforms.apply(YAW_90)
    first = transforms.augment(session, session.snapshot()).series["bench.accel.vehicle"]
    session.append("bench.accel", SignalSample(10, [0.0, 1.0, 0.0], timestamp_domain="relative"))
    second = transforms.augment(session, session.snapshot()).series["bench.accel.vehicle"]
    assert second[0] is first[0]
    assert close(second[1].sample.value, (-1.0, 0.0, 0.0))
    # A changed transform recomputes everything.
    transforms.apply(FrameTransform("vehicle", "sensor", "vehicle", Rotation.identity()))
    third = transforms.augment(session, session.snapshot()).series["bench.accel.vehicle"]
    assert close(third[0].sample.value, (1.0, 0.0, 0.0))


def test_configuration_round_trips_through_json_and_rejects_invalid() -> None:
    transforms = FrameTransformSet()
    calls: list[int] = []
    transforms.subscribe(lambda: calls.append(1))
    transforms.apply(YAW_90)
    transforms.apply(FrameTransform("level", "vehicle", "level", Rotation.from_euler(0, 5, 0)))
    saved = json.loads(json.dumps(transforms.configuration()))
    restored = FrameTransformSet()
    restored.restore(saved)
    assert [item.name for item in restored.transforms] == ["vehicle", "level"]
    for before, after in zip(transforms.transforms, restored.transforms, strict=True):
        assert after.rotation.is_close(before.rotation)
        assert (after.source_frame, after.target_frame) == (
            before.source_frame,
            before.target_frame,
        )
    invalid = [
        {"version": 2, "transforms": []},
        {"version": 1},
        {"version": 1, "transforms": {}},
        {"version": 1, "transforms": [saved["transforms"][0], saved["transforms"][0]]},
        {"version": 1, "transforms": [{**saved["transforms"][0], "rotation": {"x": 1}}]},
        {
            "version": 1,
            "transforms": [
                {**saved["transforms"][0], "rotation": {"quaternion_wxyz": [0, 0, 0, 0]}}
            ],
        },
    ]
    for config in invalid:
        with pytest.raises(TransformError):
            restored.restore(config)
        assert [item.name for item in restored.transforms] == ["vehicle", "level"]
    assert len(calls) == 2
    transforms.remove("level")
    transforms.remove("missing")
    transforms.clear()
    assert transforms.transforms == () and len(calls) == 4
