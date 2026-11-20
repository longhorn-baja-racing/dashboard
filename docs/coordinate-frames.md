# Coordinate frames

Vector readings such as accelerations are measured in the frame of the sensor
that produced them. The firmware protocol (`docs/protocol.md` in the firmware
repository) states that IMU values are sensor-frame and that per-node mounting
transforms are session metadata applied by the dashboard. This page describes
how the dashboard applies such a transform. No sensor, IMU or vehicle frame is
assumed: a transform only ever touches signals that declare a frame.

The math and the session integration are in `lbr_dashboard.core.frames`
(numpy, no Qt). The editor is **Analysis > Coordinate frames**
(`lbr_dashboard.ui.tools.frames`).

## Declaring a frame

A signal declares its frame with a text value under `frame`,
`coordinate_frame` or `reference_frame` in its descriptor's `extra`, `display`
or `calibration` metadata. The first non-empty value in that order wins. This
is the same lookup the raw export uses (`core.export.signal_frame`), so the
frame in an export and the frame used here always agree. A signal without such
a value has frame `"unspecified"` and is never transformed.

Example descriptor (JSON form):

```json
{"id": "imu.front.accel", "type": "vector", "shape": [3], "canonical_unit": "m/s^2",
 "frame": "imu_front", "...": "..."}
```

Frame names are compared exactly after trimming spaces.

## Conventions

- Right-handed frames.
- Quaternions are Hamilton quaternions written scalar first, `(w, x, y, z)`.
  `q` and `-q` are the same rotation.
- Rotations are active: `R @ v` rotates `v`.
- A transform from frame A to frame B uses `v_B = R @ v_A`. The columns of `R`
  are A's x, y and z axes written in B coordinates. For a sensor mounted on a
  vehicle, that is where the sensor's axes point on the vehicle.
- Composition follows matrix order: `(a * b).apply(v) == a.apply(b.apply(v))`.
- Roll, pitch and yaw are intrinsic Z-Y-X angles in degrees:
  `R = Rz(yaw) @ Ry(pitch) @ Rx(roll)`. At pitch +/-90 degrees (gimbal lock)
  roll is reported as 0 and the whole heading as yaw.

Example: a sensor mounted turned 90 degrees to the left about the vertical
axis (its x axis points along the vehicle's y axis) uses roll 0, pitch 0, yaw
90. A sensor reading of `(1, 0, 9.81)` becomes `(0, 1, 9.81)` in the vehicle
frame.

## Entering a rotation

The tool accepts three formats:

| Format | Text | Example (yaw 90 deg) |
| --- | --- | --- |
| Roll, pitch, yaw | three angles in degrees | `0, 0, 90` |
| Quaternion | `w, x, y, z` | `0.7071068, 0, 0, 0.7071068` |
| Matrix rows | three rows separated by `;` | `0, -1, 0; 1, 0, 0; 0, 0, 1` |

Invalid rotations are rejected with a reason and cannot be applied:
non-finite numbers, a quaternion whose norm is not within 1e-3 of one (for
example `1, 1, 0, 0`), a matrix that is not orthonormal within 1e-3, and a
matrix with determinant -1 (a reflection changes handedness and is not a
rotation). Values within the tolerance, such as hand-typed `0.7071`, are
normalized.

## Applying a transform

A transform has a name (lowercase letters, digits, `-` and `_`), a source
frame, a target frame and a rotation. It applies to every signal that:

1. is a `vector` with shape `(3,)`, and
2. declares a frame equal to the transform's source frame, and
3. is not itself a transformed signal.

Everything else is left alone, and the tool's preview says why for every
vector signal: no declared frame, a different frame, or not a 3-vector.
Scalars, booleans, events and status signals are never considered.

Each applicable signal gets a new derived signal next to it:

| Field | Value |
| --- | --- |
| id | input id + `.` + transform name, e.g. `imu.front.accel.vehicle` |
| path | `transformed` / input path (a group in the signal browser) |
| name | input name + ` (<target frame>)` |
| frame | the target frame (`display["frame"]`; frame keys copied from the input are replaced) |
| unit, source, clock domain, rate, calibration | copied from the input |
| provenance | `display["frame_transform"]` holds the input id, transform name, frames and quaternion; the description (shown in the signal browser details) says the same in one line |

Each output record has the input record's timestamp, sequence, availability and
quality. Unavailable readings stay unavailable; nothing is filled in.
Rounding residue below 1e-12 of a vector's largest input component (such as
`cos(90 deg)`) is written as an exact zero.

Raw signals are never changed: the transformed signals are added to the
snapshot the views show, while the session itself keeps only the recorded
records. If an output id would clash with a recorded signal the transform is
refused for that signal.

While a source streams, only new readings are rotated; already rotated
records are reused.

## The Coordinate frames tool

**Analysis > Coordinate frames** has:

- fields for name, source frame (offering the frames the session's vectors
  declare), target frame, rotation format and rotation;
- a preview with the rotation matrix, the signals it would rotate with their
  value at the replay cursor before and after, and the signals it would not
  rotate with the reason;
- **Apply transform** (or **Replace transform** for an existing name) and a
  list of applied transforms with **Remove selected**. Selecting an applied
  transform loads it back into the editor.

Applied transforms belong to the app run, not to one session: they apply to
every session loaded afterwards. `FramesTool.configuration()` returns
`{"version": 1, "transforms": [...]}` with each rotation as
`{"quaternion_wxyz": [w, x, y, z]}`; `restore_configuration()` validates the
whole configuration first and changes nothing when it is invalid.

## Demo

**Simulator - attitude (finite)** in the source picker emits fictional
signals: `attitude.acceleration` (3-vector, frame `sensor`), `attitude.rate`
(3-vector without a frame) and `attitude.quaternion` (see
[orientation.md](orientation.md)). Apply source `sensor`, target `vehicle`,
rotation `0, 0, 90`: `attitude.acceleration.vehicle` appears under
`transformed`, and the preview explains why `attitude.rate` and
`attitude.quaternion` are not rotated.

## Limits

- Only 3-vectors are rotated. Quaternion (orientation) signals are not
  re-expressed in another frame yet.
- Transforms are not chained: a transformed signal is never transformed again.
- No automatic mounting detection, sensor fusion or heading correction.

## Tests

`tests/core/test_frames.py` (known 90-degree rotations, Euler angles,
composition, inverse, sign equivalence, tolerances, invalid input, frame
matching, raw records unchanged, configuration round trip) and
`tests/ui/test_frames_tool.py` (the tool in the real window with the attitude
simulator, restart with a saved configuration, a CSV session without vectors).
