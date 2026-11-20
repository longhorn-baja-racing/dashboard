"""Attitude of a quaternion signal at one session time, without guessing.

A signal is shown as an orientation only when its descriptor says it is one
(see docs/orientation.md):

- ``data_type == "vector"`` with ``shape == (4,)``;
- ``quaternion_order``: ``"wxyz"`` (scalar first) or ``"xyzw"`` (scalar last);
- ``quaternion_convention``: ``"hamilton"`` (right-handed Hamilton product; the
  quaternion rotates vectors from the signal's ``frame`` into its reference).

Optional metadata, shown as labels: ``frame`` (the body frame, the same keys
as :data:`~lbr_dashboard.core.export.FRAME_KEYS`), ``attitude_reference`` (the
reference frame) and ``yaw_reference`` (what yaw zero means, e.g. "relative to
start, drifts" or "magnetic north"). Keys are looked up in the descriptor's
``extra``, ``display`` and ``calibration`` metadata, in that order.

:func:`attitude_at` returns the last reading at or before a time and says
whether it is a current valid pose. It never interpolates: a reading older than
``max_age_ns`` is stale, an unavailable reading is missing, and a quaternion
whose norm is not within :data:`NORM_TOLERANCE` of one is invalid.
"""

from __future__ import annotations

import math
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from enum import Enum
from numbers import Real

from .export import signal_frame
from .frames import Rotation, RotationError
from .session import SessionSnapshot
from .signals import SignalDescriptor

ORDER_KEY = "quaternion_order"
CONVENTION_KEY = "quaternion_convention"
REFERENCE_KEY = "attitude_reference"
YAW_KEY = "yaw_reference"
# Position of w, x, y and z in the signal's four components.
ORDERS = {"wxyz": (0, 1, 2, 3), "xyzw": (3, 0, 1, 2)}
CONVENTIONS = ("hamilton",)
NORM_TOLERANCE = 0.01
# A reading is stale after this many nominal sample periods without a newer one.
STALE_PERIODS = 3
DEFAULT_MAX_AGE_NS = 500_000_000
_CALIBRATION_WORDS = ("uncalibrated", "not_calibrated", "not calibrated", "not-calibrated")


class OrientationError(ValueError):
    """Raised when a signal is not a declared real unit quaternion."""


def metadata_text(descriptor: SignalDescriptor, key: str) -> str | None:
    """Return a non-empty text value of ``key`` from extra, display or calibration."""

    for mapping in (descriptor.extra, descriptor.display, descriptor.calibration):
        value = mapping.get(key)
        if isinstance(value, str) and value.strip():
            return value.strip()
    return None


@dataclass(frozen=True, slots=True)
class QuaternionLayout:
    """How to read one quaternion signal and what its attitude means."""

    order: str
    convention: str
    frame: str
    reference: str | None
    yaw_reference: str | None

    def rotation(self, value: object) -> Rotation:
        """Return the rotation of one sample value; raise RotationError if invalid."""

        if not isinstance(value, Sequence) or isinstance(value, (str, bytes)) or len(value) != 4:
            raise RotationError("value is not four quaternion components")
        numbers: list[float] = []
        for item in value:
            if isinstance(item, bool) or not isinstance(item, Real):
                raise RotationError("value has a component that is not a number")
            number = float(item)
            if not math.isfinite(number):
                raise RotationError("value has a component that is not finite")
            numbers.append(number)
        w, x, y, z = (numbers[index] for index in ORDERS[self.order])
        norm = math.sqrt(w * w + x * x + y * y + z * z)
        if abs(norm - 1.0) > NORM_TOLERANCE:
            raise RotationError(f"quaternion norm is {norm:.4g}, not 1")
        return Rotation((w / norm, x / norm, y / norm, z / norm))


def quaternion_layout(descriptor: SignalDescriptor) -> QuaternionLayout:
    """Return the layout of a declared quaternion signal or explain why it is not one."""

    name = descriptor.display_name
    if descriptor.data_type != "vector" or descriptor.shape != (4,):
        shape = "x".join(str(size) for size in descriptor.shape) or "scalar"
        raise OrientationError(
            f"{name} is {descriptor.data_type} ({shape}); an orientation needs a "
            "four-component vector signal"
        )
    order = metadata_text(descriptor, ORDER_KEY)
    if order is None:
        raise OrientationError(
            f"{name} does not declare its component order; add "
            f"{ORDER_KEY} = 'wxyz' or 'xyzw' to its metadata"
        )
    if order.lower() not in ORDERS:
        raise OrientationError(
            f"{name} declares {ORDER_KEY} = {order!r}; only 'wxyz' and 'xyzw' are supported"
        )
    convention = metadata_text(descriptor, CONVENTION_KEY)
    if convention is None:
        raise OrientationError(
            f"{name} does not declare its quaternion convention; add "
            f"{CONVENTION_KEY} = 'hamilton' to its metadata"
        )
    if convention.lower() not in CONVENTIONS:
        raise OrientationError(
            f"{name} declares {CONVENTION_KEY} = {convention!r}; only 'hamilton' is supported"
        )
    return QuaternionLayout(
        order.lower(),
        convention.lower(),
        signal_frame(descriptor),
        metadata_text(descriptor, REFERENCE_KEY),
        metadata_text(descriptor, YAW_KEY),
    )


def default_max_age_ns(descriptor: SignalDescriptor) -> int:
    """Return :data:`STALE_PERIODS` nominal periods, or 500 ms without a nominal rate."""

    rate = descriptor.nominal_rate_hz
    if rate is None or rate <= 0:
        return DEFAULT_MAX_AGE_NS
    return max(1, round(STALE_PERIODS * 1_000_000_000 / rate))


class AttitudeState(Enum):
    VALID = "valid"
    UNCALIBRATED = "uncalibrated"
    NO_DATA = "no data"
    MISSING = "missing"
    STALE = "stale"
    INVALID = "invalid"


@dataclass(frozen=True, slots=True)
class AttitudeReading:
    """The attitude shown at one time. Only VALID is a current, trustworthy pose."""

    state: AttitudeState
    message: str
    rotation: Rotation | None = None
    sample_ns: int | None = None
    age_ns: int | None = None
    quality: str | None = None

    @property
    def is_current(self) -> bool:
        return self.state is AttitudeState.VALID

    @property
    def euler(self) -> tuple[float, float, float] | None:
        """Return ``(roll, pitch, yaw)`` in degrees when a rotation is shown."""

        return self.rotation.euler() if self.rotation is not None else None


def _uncalibrated(quality: str | None, extra: Mapping[str, object]) -> bool:
    """A reading is uncalibrated when its quality or ``extra["calibrated"] is False`` says so."""

    if quality is not None and any(word in quality.lower() for word in _CALIBRATION_WORDS):
        return True
    return extra.get("calibrated") is False


def attitude_at(
    snapshot: SessionSnapshot | None,
    signal_id: str,
    time_ns: int | None,
    layout: QuaternionLayout,
    max_age_ns: int,
) -> AttitudeReading:
    """Return the attitude of ``signal_id`` at ``time_ns`` from the last reading at or before it."""

    if time_ns is None:
        return AttitudeReading(AttitudeState.NO_DATA, "No replay cursor yet.")
    if snapshot is None or signal_id not in snapshot.series:
        return AttitudeReading(AttitudeState.NO_DATA, "The signal is not in this session.")
    record = snapshot.at_or_before(signal_id, time_ns)
    if record is None:
        return AttitudeReading(AttitudeState.NO_DATA, "No reading at or before the cursor.")
    sample = record.sample
    age = time_ns - sample.timestamp_ns
    common = {"sample_ns": sample.timestamp_ns, "age_ns": age, "quality": sample.quality}
    if not sample.available:
        reason = sample.quality or "unavailable"
        return AttitudeReading(
            AttitudeState.MISSING,
            f"Missing: the latest reading is unavailable ({reason}).",
            **common,
        )
    if age > max_age_ns:
        return AttitudeReading(
            AttitudeState.STALE,
            f"Stale: no reading for {age / 1e6:.0f} ms (limit {max_age_ns / 1e6:.0f} ms).",
            **common,
        )
    try:
        rotation = layout.rotation(sample.value)
    except RotationError as error:
        return AttitudeReading(AttitudeState.INVALID, f"Invalid: {error}.", **common)
    if _uncalibrated(sample.quality, sample.extra):
        return AttitudeReading(
            AttitudeState.UNCALIBRATED,
            f"Uncalibrated: the reading is marked {sample.quality or 'not calibrated'}.",
            rotation,
            **common,
        )
    return AttitudeReading(AttitudeState.VALID, "Current reading.", rotation, **common)
