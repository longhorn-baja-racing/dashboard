"""Rigid rotations and coordinate-frame transforms for vector signals with a declared frame.

Conventions (see docs/coordinate-frames.md):

- Quaternions are Hamilton quaternions written scalar first, ``(w, x, y, z)``,
  in right-handed frames. They describe active rotations: ``rotation.apply(v)``
  returns ``R @ v``.
- A :class:`FrameTransform` maps vector components measured in its source frame
  to the same physical vector expressed in its target frame:
  ``v_target = R @ v_source``. The columns of ``R`` are the source frame's axes
  written in target-frame coordinates (for a sensor-to-vehicle mounting, where
  the sensor's x, y and z axes point on the vehicle).
- Composition follows matrix order: ``(a * b).apply(v) == a.apply(b.apply(v))``.
- Roll, pitch and yaw are intrinsic Z-Y-X angles in degrees:
  ``R = Rz(yaw) @ Ry(pitch) @ Rx(roll)``.

A signal declares its frame with a text value under one of
:data:`~lbr_dashboard.core.export.FRAME_KEYS` (``frame``, ``coordinate_frame``,
``reference_frame``) in its descriptor's ``extra``, ``display`` or
``calibration`` metadata, the same lookup the raw export uses. Only
three-component vector signals whose declared frame equals a transform's source
frame are transformed. Signals without a frame are never transformed.

:class:`FrameTransformSet` keeps the applied transforms of one app run and adds
their results to a session snapshot as new signals. Raw records are shared with
the input snapshot and never changed.
"""

from __future__ import annotations

import math
import re
from collections.abc import Callable, Iterable, Mapping, Sequence
from dataclasses import dataclass, replace
from numbers import Real
from typing import Any

import numpy as np

from .export import FRAME_KEYS, UNSPECIFIED_FRAME, signal_frame
from .session import SessionRecord, SessionSnapshot
from .signals import SignalDescriptor, SignalSample

CONFIGURATION_VERSION = 1
# Largest accepted deviation of a quaternion norm from 1, or of R^T R from I.
UNIT_TOLERANCE = 1e-3
TRANSFORM_KEY = "frame_transform"
TRANSFORMED_PATH = "transformed"
_NAME = re.compile(r"^[a-z][a-z0-9]*(?:[_-][a-z0-9]+)*$")

Quaternion = tuple[float, float, float, float]


class RotationError(ValueError):
    """Raised for a rotation that is not finite, not unit length or not proper."""


class TransformError(ValueError):
    """Raised for an invalid transform definition or configuration."""


def _finite(values: Iterable[object], what: str) -> list[float]:
    numbers: list[float] = []
    for value in values:
        if isinstance(value, bool) or not isinstance(value, Real):
            raise RotationError(f"{what} must contain only numbers")
        number = float(value)
        if not math.isfinite(number):
            raise RotationError(f"{what} must contain only finite numbers")
        numbers.append(number)
    return numbers


def _multiply(a: Quaternion, b: Quaternion) -> Quaternion:
    aw, ax, ay, az = a
    bw, bx, by, bz = b
    return (
        aw * bw - ax * bx - ay * by - az * bz,
        aw * bx + ax * bw + ay * bz - az * by,
        aw * by - ax * bz + ay * bw + az * bx,
        aw * bz + ax * by - ay * bx + az * bw,
    )


@dataclass(frozen=True, slots=True)
class Rotation:
    """A proper rigid rotation stored as a unit Hamilton quaternion ``(w, x, y, z)``.

    ``q`` and ``-q`` are the same rotation; equality via :meth:`is_close` and
    :meth:`canonical` treat them alike. Construct with the ``from_*`` methods,
    which validate their input.
    """

    quaternion: Quaternion

    def __post_init__(self) -> None:
        values = _finite(self.quaternion, "quaternion")
        if len(values) != 4:
            raise RotationError("quaternion must have four components (w, x, y, z)")
        norm = math.sqrt(sum(value * value for value in values))
        if abs(norm - 1.0) > UNIT_TOLERANCE:
            raise RotationError(
                f"quaternion norm is {norm:.6g}; a rotation needs a unit quaternion"
            )
        w, x, y, z = (value / norm for value in values)
        object.__setattr__(self, "quaternion", (w, x, y, z))

    # Construction ----------------------------------------------------------

    @classmethod
    def identity(cls) -> Rotation:
        return cls((1.0, 0.0, 0.0, 0.0))

    @classmethod
    def from_quaternion(cls, w: float, x: float, y: float, z: float) -> Rotation:
        """Return the rotation of a unit quaternion (norm within 1e-3 of one)."""

        return cls((w, x, y, z))

    @classmethod
    def from_axis_angle(cls, axis: Sequence[float], degrees: float) -> Rotation:
        """Rotate by ``degrees`` about ``axis`` (right-hand rule)."""

        vector = _finite(axis, "axis")
        if len(vector) != 3:
            raise RotationError("axis must have three components")
        (angle,) = _finite((degrees,), "angle")
        length = math.sqrt(sum(value * value for value in vector))
        if length == 0.0:
            raise RotationError("axis must not be the zero vector")
        half = math.radians(angle) / 2
        scale = math.sin(half) / length
        return cls((math.cos(half), vector[0] * scale, vector[1] * scale, vector[2] * scale))

    @classmethod
    def from_euler(cls, roll: float, pitch: float, yaw: float) -> Rotation:
        """Return ``Rz(yaw) @ Ry(pitch) @ Rx(roll)`` for angles in degrees."""

        return (
            cls.from_axis_angle((0, 0, 1), yaw)
            * cls.from_axis_angle((0, 1, 0), pitch)
            * cls.from_axis_angle((1, 0, 0), roll)
        )

    @classmethod
    def from_matrix(cls, matrix: object) -> Rotation:
        """Return the rotation of a 3x3 proper orthonormal matrix.

        Rejects non-finite values, non-orthonormal matrices (beyond 1e-3) and
        reflections (determinant -1, a change of handedness).
        """

        try:
            array = np.array(matrix, dtype=float)
        except (TypeError, ValueError) as error:
            raise RotationError("matrix must contain only numbers") from error
        if array.shape != (3, 3):
            raise RotationError("matrix must have 3 rows of 3 numbers")
        if not np.all(np.isfinite(array)):
            raise RotationError("matrix must contain only finite numbers")
        error = float(np.max(np.abs(array.T @ array - np.eye(3))))
        if error > UNIT_TOLERANCE:
            raise RotationError(f"matrix is not orthonormal (R^T R differs from I by {error:.3g})")
        if float(np.linalg.det(array)) < 0:
            raise RotationError(
                "matrix has determinant -1: it is a reflection (it changes handedness), "
                "not a rotation"
            )
        m = array
        trace = m[0, 0] + m[1, 1] + m[2, 2]
        if trace > 0:
            s = math.sqrt(trace + 1.0) * 2
            q = (
                0.25 * s,
                (m[2, 1] - m[1, 2]) / s,
                (m[0, 2] - m[2, 0]) / s,
                (m[1, 0] - m[0, 1]) / s,
            )
        elif m[0, 0] > m[1, 1] and m[0, 0] > m[2, 2]:
            s = math.sqrt(1.0 + m[0, 0] - m[1, 1] - m[2, 2]) * 2
            q = (
                (m[2, 1] - m[1, 2]) / s,
                0.25 * s,
                (m[0, 1] + m[1, 0]) / s,
                (m[0, 2] + m[2, 0]) / s,
            )
        elif m[1, 1] > m[2, 2]:
            s = math.sqrt(1.0 + m[1, 1] - m[0, 0] - m[2, 2]) * 2
            q = (
                (m[0, 2] - m[2, 0]) / s,
                (m[0, 1] + m[1, 0]) / s,
                0.25 * s,
                (m[1, 2] + m[2, 1]) / s,
            )
        else:
            s = math.sqrt(1.0 + m[2, 2] - m[0, 0] - m[1, 1]) * 2
            q = (
                (m[1, 0] - m[0, 1]) / s,
                (m[0, 2] + m[2, 0]) / s,
                (m[1, 2] + m[2, 1]) / s,
                0.25 * s,
            )
        w, x, y, z = (float(value) for value in q)
        norm = math.sqrt(w * w + x * x + y * y + z * z)
        return cls((w / norm, x / norm, y / norm, z / norm))

    # Use -------------------------------------------------------------------

    @property
    def matrix(self) -> np.ndarray:
        """Return the 3x3 rotation matrix ``R`` (``R @ v`` rotates ``v``)."""

        w, x, y, z = self.quaternion
        return np.array(
            [
                [1 - 2 * (y * y + z * z), 2 * (x * y - w * z), 2 * (x * z + w * y)],
                [2 * (x * y + w * z), 1 - 2 * (x * x + z * z), 2 * (y * z - w * x)],
                [2 * (x * z - w * y), 2 * (y * z + w * x), 1 - 2 * (x * x + y * y)],
            ]
        )

    def apply(self, vectors: object) -> np.ndarray:
        """Rotate one vector (shape (3,)) or many (shape (n, 3))."""

        array = np.asarray(vectors, dtype=float)
        if array.shape[-1:] != (3,) or array.ndim > 2:
            raise RotationError("vectors must have shape (3,) or (n, 3)")
        return array @ self.matrix.T

    def __mul__(self, other: Rotation) -> Rotation:
        """Compose: ``(self * other)`` applies ``other`` first, then ``self``."""

        if not isinstance(other, Rotation):
            return NotImplemented
        return Rotation(_multiply(self.quaternion, other.quaternion))

    def inverse(self) -> Rotation:
        w, x, y, z = self.quaternion
        return Rotation((w, -x, -y, -z))

    def canonical(self) -> Quaternion:
        """Return the quaternion with a non-negative first non-zero component."""

        for value in self.quaternion:
            if value > 0:
                return self.quaternion
            if value < 0:
                w, x, y, z = self.quaternion
                return (-w, -x, -y, -z)
        return self.quaternion  # pragma: no cover - a unit quaternion is never zero

    def is_close(self, other: Rotation, tolerance: float = 1e-9) -> bool:
        """Return whether both are the same rotation (``q`` and ``-q`` are equal)."""

        dot = abs(sum(a * b for a, b in zip(self.quaternion, other.quaternion, strict=True)))
        return 1.0 - dot <= tolerance

    def angle_degrees(self) -> float:
        """Return the rotation angle in degrees, from 0 to 180."""

        return math.degrees(2 * math.acos(min(1.0, abs(self.quaternion[0]))))

    def euler(self) -> tuple[float, float, float]:
        """Return ``(roll, pitch, yaw)`` in degrees for ``Rz(yaw) Ry(pitch) Rx(roll)``.

        Pitch is in [-90, 90]; roll and yaw are in (-180, 180]. At pitch +/-90
        (gimbal lock) only roll - yaw (or roll + yaw) is defined; roll is then
        reported as 0 and the whole heading as yaw.
        """

        w, x, y, z = self.quaternion
        sine = max(-1.0, min(1.0, 2 * (w * y - z * x)))
        pitch = math.asin(sine)
        if abs(sine) > 1 - 1e-12:
            m = self.matrix
            return 0.0, math.degrees(pitch), math.degrees(math.atan2(-m[0, 1], m[1, 1]))
        roll = math.atan2(2 * (w * x + y * z), 1 - 2 * (x * x + y * y))
        yaw = math.atan2(2 * (w * z + x * y), 1 - 2 * (y * y + z * z))
        return math.degrees(roll), math.degrees(pitch), math.degrees(yaw)

    def to_dict(self) -> dict[str, list[float]]:
        return {"quaternion_wxyz": [round(value, 12) for value in self.canonical()]}

    @classmethod
    def from_dict(cls, value: object) -> Rotation:
        if not isinstance(value, Mapping) or set(value) != {"quaternion_wxyz"}:
            raise RotationError("rotation must be {'quaternion_wxyz': [w, x, y, z]}")
        components = value["quaternion_wxyz"]
        if not isinstance(components, (list, tuple)) or len(components) != 4:
            raise RotationError("quaternion_wxyz must hold four numbers")
        w, x, y, z = _finite(components, "quaternion")
        return cls((w, x, y, z))


ROTATION_FORMATS = {
    "euler": "Roll, pitch, yaw (deg, Z-Y-X)",
    "quaternion": "Quaternion w, x, y, z",
    "matrix": "Matrix rows (r1; r2; r3)",
}


def parse_rotation(form: str, text: str) -> Rotation:
    """Parse rotation text in one of :data:`ROTATION_FORMATS`.

    ``euler``: ``"roll, pitch, yaw"`` in degrees. ``quaternion``: ``"w, x, y, z"``.
    ``matrix``: three rows separated by ``;``, each of three numbers.
    Raises :class:`RotationError` with a readable message.
    """

    def numbers(part: str) -> list[float]:
        items = [item for item in re.split(r"[,\s]+", part.strip()) if item]
        try:
            return [float(item) for item in items]
        except ValueError:
            raise RotationError(f"not a number in {part.strip()!r}") from None

    if form == "matrix":
        rows = [numbers(row) for row in text.split(";") if row.strip()]
        if len(rows) != 3 or any(len(row) != 3 for row in rows):
            raise RotationError("enter three rows of three numbers separated by ';'")
        return Rotation.from_matrix(rows)
    values = _finite(numbers(text), "rotation")
    if form == "euler":
        if len(values) != 3:
            raise RotationError("enter roll, pitch and yaw in degrees")
        return Rotation.from_euler(*values)
    if form == "quaternion":
        if len(values) != 4:
            raise RotationError("enter four quaternion components w, x, y, z")
        return Rotation.from_quaternion(*values)
    raise RotationError(f"unknown rotation format {form!r}")


# Frames and transforms -------------------------------------------------------


def declared_frame(descriptor: SignalDescriptor) -> str | None:
    """Return the frame a descriptor declares, or None when it declares none."""

    frame = signal_frame(descriptor)
    return None if frame == UNSPECIFIED_FRAME else frame


def is_transformed(descriptor: SignalDescriptor | None) -> bool:
    """Return whether ``descriptor`` was produced by :meth:`FrameTransformSet.augment`."""

    return descriptor is not None and TRANSFORM_KEY in descriptor.display


@dataclass(frozen=True, slots=True)
class FrameTransform:
    """Rotate vectors measured in ``source_frame`` into ``target_frame``.

    ``name`` is a short lowercase id (letters, digits, ``-`` and ``_``); it is
    appended to each input id to name the output signal.
    """

    name: str
    source_frame: str
    target_frame: str
    rotation: Rotation

    def __post_init__(self) -> None:
        if not isinstance(self.name, str) or _NAME.fullmatch(self.name) is None:
            raise TransformError(
                "name must start with a lowercase letter and use only lowercase letters, "
                "digits, '-' and '_'"
            )
        for field in ("source_frame", "target_frame"):
            value = getattr(self, field)
            if not isinstance(value, str) or not value.strip():
                raise TransformError(f"{field.replace('_', ' ')} must not be empty")
            if value.strip() == UNSPECIFIED_FRAME:
                raise TransformError(f"{field.replace('_', ' ')} cannot be '{UNSPECIFIED_FRAME}'")
            object.__setattr__(self, field, value.strip())
        if self.source_frame == self.target_frame:
            raise TransformError("source and target frames must differ")
        if not isinstance(self.rotation, Rotation):
            raise TransformError("rotation must be a Rotation")

    def output_id(self, signal_id: str) -> str:
        return f"{signal_id}.{self.name}"

    def provenance(self, signal_id: str) -> str:
        w, x, y, z = self.rotation.canonical()
        return (
            f"Transformed: {signal_id} rotated from frame '{self.source_frame}' to "
            f"'{self.target_frame}' by transform '{self.name}' "
            f"(quaternion w, x, y, z = {w:.6g}, {x:.6g}, {y:.6g}, {z:.6g}); "
            "raw values unchanged in the source signal"
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "source_frame": self.source_frame,
            "target_frame": self.target_frame,
            "rotation": self.rotation.to_dict(),
        }

    @classmethod
    def from_dict(cls, value: object) -> FrameTransform:
        keys = {"name", "source_frame", "target_frame", "rotation"}
        if not isinstance(value, Mapping) or set(value) != keys:
            raise TransformError(f"a transform must have exactly the keys {sorted(keys)}")
        try:
            rotation = Rotation.from_dict(value["rotation"])
        except RotationError as error:
            raise TransformError(f"invalid rotation: {error}") from None
        return cls(value["name"], value["source_frame"], value["target_frame"], rotation)


def refusal(descriptor: SignalDescriptor, transform: FrameTransform) -> str | None:
    """Return why ``transform`` does not apply to ``descriptor``, or None when it does."""

    if is_transformed(descriptor):
        return "is already a transformed signal"
    if descriptor.data_type != "vector" or descriptor.shape != (3,):
        shape = "x".join(str(size) for size in descriptor.shape) or "scalar"
        return f"is {descriptor.data_type} ({shape}); only three-component vectors are rotated"
    frame = declared_frame(descriptor)
    if frame is None:
        return (
            "declares no coordinate frame (no 'frame', 'coordinate_frame' or "
            "'reference_frame' metadata), so it is never transformed"
        )
    if frame != transform.source_frame:
        return f"is in frame '{frame}', not '{transform.source_frame}'"
    return None


# Components smaller than this fraction of a vector's largest input component are
# rounding residue (e.g. cos(90 deg) = 6e-17) and are written as exactly zero.
RESIDUE = 1e-12


def rotate_vectors(rotation: Rotation, vectors: object) -> np.ndarray:
    """Rotate vectors of shape (n, 3) and clear rounding residue to exact zeros."""

    array = np.asarray(vectors, dtype=float).reshape(-1, 3)
    rotated = rotation.apply(array)
    scale = np.max(np.abs(array), axis=1, keepdims=True)
    rotated[np.abs(rotated) <= RESIDUE * scale] = 0.0
    return rotated


def rotate_vector(rotation: Rotation, value: object) -> tuple[float, float, float]:
    x, y, z = rotate_vectors(rotation, value)[0]
    return float(x), float(y), float(z)


def output_descriptor(descriptor: SignalDescriptor, transform: FrameTransform) -> SignalDescriptor:
    """Describe the transformed copy of ``descriptor``: same unit, clock and source."""

    def reframed(mapping: Mapping[str, Any]) -> dict[str, Any]:
        return {
            key: (transform.target_frame if key in FRAME_KEYS else value)
            for key, value in mapping.items()
        }

    display = reframed(descriptor.display)
    display["frame"] = transform.target_frame
    display[TRANSFORM_KEY] = {"input": descriptor.identifier, **transform.to_dict()}
    provenance = transform.provenance(descriptor.identifier)
    return SignalDescriptor(
        transform.output_id(descriptor.identifier),
        f"{descriptor.display_name} ({transform.target_frame})",
        (TRANSFORMED_PATH, *descriptor.path),
        "vector",
        descriptor.source_id,
        shape=(3,),
        canonical_unit=descriptor.canonical_unit,
        device_id=descriptor.device_id,
        nominal_rate_hz=descriptor.nominal_rate_hz,
        description=provenance,
        availability=descriptor.availability,
        calibration=reframed(descriptor.calibration),
        display=display,
        extra=reframed(descriptor.extra),
        timestamp_domain=descriptor.timestamp_domain,
    )


@dataclass(frozen=True, slots=True)
class PlanItem:
    """One signal considered by one transform: its output id or why it was refused."""

    descriptor: SignalDescriptor
    transform: FrameTransform
    reason: str | None

    @property
    def applies(self) -> bool:
        return self.reason is None

    @property
    def output_id(self) -> str:
        return self.transform.output_id(self.descriptor.identifier)


def plan(
    descriptors: Iterable[SignalDescriptor], transforms: Iterable[FrameTransform]
) -> tuple[PlanItem, ...]:
    """Return, for every transform and every raw vector signal, whether it applies.

    Non-vector signals are left out: they are never transformed.
    """

    descriptors = tuple(descriptors)
    raw = [item for item in descriptors if item.data_type == "vector" and not is_transformed(item)]
    taken = {item.identifier for item in descriptors if not is_transformed(item)}
    items: list[PlanItem] = []
    for transform in transforms:
        for descriptor in raw:
            reason = refusal(descriptor, transform)
            output = transform.output_id(descriptor.identifier)
            if reason is None and output in taken:
                reason = f"its output id {output} is already used by a recorded signal"
            items.append(PlanItem(descriptor, transform, reason))
    return tuple(items)


class FrameTransformSet:
    """The applied transforms of one app run and their cached results.

    Results are computed incrementally: an output record is reused while its
    input record (same sequence number) is still in the snapshot, so a live
    session only rotates new readings.
    """

    def __init__(self) -> None:
        self._transforms: dict[str, FrameTransform] = {}
        self._listeners: list[Callable[[], None]] = []
        self._session: object | None = None
        self._outputs: dict[tuple[str, str], tuple[SignalDescriptor, dict[int, SessionRecord]]] = {}

    @property
    def transforms(self) -> tuple[FrameTransform, ...]:
        return tuple(self._transforms.values())

    def subscribe(self, callback: Callable[[], None]) -> Callable[[], None]:
        self._listeners.append(callback)
        return lambda: self._listeners.remove(callback) if callback in self._listeners else None

    def _changed(self) -> None:
        self._outputs.clear()
        for callback in tuple(self._listeners):
            callback()

    def apply(self, transform: FrameTransform) -> None:
        """Add ``transform``, replacing one with the same name."""

        if not isinstance(transform, FrameTransform):
            raise TransformError("transform must be a FrameTransform")
        self._transforms[transform.name] = transform
        self._changed()

    def remove(self, name: str) -> None:
        if self._transforms.pop(name, None) is not None:
            self._changed()

    def clear(self) -> None:
        if self._transforms:
            self._transforms.clear()
            self._changed()

    def configuration(self) -> dict[str, Any]:
        return {
            "version": CONFIGURATION_VERSION,
            "transforms": [item.to_dict() for item in self._transforms.values()],
        }

    def restore(self, config: object) -> None:
        """Replace all transforms from :meth:`configuration` output.

        Raises :class:`TransformError` for an invalid configuration and changes
        nothing then.
        """

        if not isinstance(config, Mapping) or set(config) != {"version", "transforms"}:
            raise TransformError("configuration must have the keys 'version' and 'transforms'")
        if config["version"] != CONFIGURATION_VERSION:
            raise TransformError(f"unsupported configuration version {config['version']!r}")
        items = config["transforms"]
        if not isinstance(items, (list, tuple)):
            raise TransformError("transforms must be a list")
        transforms = [FrameTransform.from_dict(item) for item in items]
        names = [item.name for item in transforms]
        if len(set(names)) != len(names):
            raise TransformError("transform names must be unique")
        self._transforms = {item.name: item for item in transforms}
        self._changed()

    def plan(self, snapshot: SessionSnapshot | None) -> tuple[PlanItem, ...]:
        if snapshot is None:
            return ()
        return plan(snapshot.schema.signals, self._transforms.values())

    def augment(self, session: object, snapshot: SessionSnapshot) -> SessionSnapshot:
        """Return ``snapshot`` plus one transformed signal per applicable pair.

        Without transforms the same snapshot object is returned.
        """

        if not self._transforms:
            return snapshot
        if session is not self._session:
            self._session = session
            self._outputs.clear()
        descriptors = list(snapshot.schema.signals)
        series = dict(snapshot.series)
        domains = dict(snapshot.domains)
        evicted = dict(snapshot.evicted)
        used: set[tuple[str, str]] = set()
        for item in plan(descriptors, self._transforms.values()):
            if not item.applies:
                continue
            key = (item.transform.name, item.descriptor.identifier)
            used.add(key)
            cached = self._outputs.get(key)
            if cached is None or cached[0] != output_descriptor(item.descriptor, item.transform):
                cached = (output_descriptor(item.descriptor, item.transform), {})
            descriptor, previous = cached
            records = self._rotate(
                snapshot.series.get(item.descriptor.identifier, ()),
                descriptor,
                item.transform.rotation,
                previous,
            )
            self._outputs[key] = (descriptor, {record.sequence: record for record in records})
            descriptors.append(descriptor)
            series[descriptor.identifier] = records
            if item.descriptor.identifier in domains:
                domains[descriptor.identifier] = domains[item.descriptor.identifier]
            evicted[descriptor.identifier] = 0
        for key in set(self._outputs) - used:
            del self._outputs[key]
        return replace(
            snapshot,
            schema=replace(snapshot.schema, signals=tuple(descriptors)),
            series=series,
            domains=domains,
            evicted=evicted,
        )

    @staticmethod
    def _rotate(
        records: Sequence[SessionRecord],
        descriptor: SignalDescriptor,
        rotation: Rotation,
        previous: Mapping[int, SessionRecord],
    ) -> tuple[SessionRecord, ...]:
        new = [record for record in records if record.sequence not in previous]
        values = [record.sample.value for record in new if record.sample.available]
        rotated = iter(rotate_vectors(rotation, values).tolist() if values else ())
        made: dict[int, SessionRecord] = {}
        for record in new:
            sample = record.sample
            value = tuple(next(rotated)) if sample.available else None
            made[record.sequence] = SessionRecord(
                descriptor.identifier,
                record.source_id,
                SignalSample(
                    sample.timestamp_ns,
                    value,
                    available=sample.available,
                    quality=sample.quality,
                    timestamp_domain=sample.timestamp_domain,
                    source_id=sample.source_id,
                    unit=sample.unit,
                    extra=sample.extra,
                ),
                record.sequence,
            )
        return tuple(previous.get(record.sequence) or made[record.sequence] for record in records)
