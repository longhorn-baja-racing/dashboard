# Orientation view

**Analysis > Orientation** shows the attitude given by a quaternion signal at
the replay cursor: roll, pitch and yaw as text and a small drawn box with its
axes. It is optional and generic: it works only for signals that declare
themselves as quaternions, and it never assumes that any sensor supplies
orientation.

Core logic: `lbr_dashboard.core.orientation` (no Qt), reusing the rotation
math of `lbr_dashboard.core.frames` (see [coordinate-frames.md](coordinate-frames.md)).
Widgets: `lbr_dashboard.ui.tools.orientation` and `lbr_dashboard.ui.attitude_view`.

## Which signals qualify

A signal is shown as an orientation only when its descriptor has:

| Requirement | Value |
| --- | --- |
| `type` | `vector` |
| `shape` | `[4]` |
| `quaternion_order` | `"wxyz"` (scalar first) or `"xyzw"` (scalar last) |
| `quaternion_convention` | `"hamilton"`: right-handed Hamilton product; the quaternion rotates vectors from the signal's frame into its reference frame |

Optional metadata, shown as labels:

| Key | Meaning |
| --- | --- |
| `frame` (or `coordinate_frame`, `reference_frame`) | the body frame the quaternion rotates from; same lookup as the export |
| `attitude_reference` | the reference frame the quaternion rotates into, e.g. `level` |
| `yaw_reference` | what yaw zero means, e.g. `relative to start, drifts` or `magnetic north` |

All keys are looked up in the descriptor's `extra`, `display` and
`calibration` metadata, in that order. Any other signal is refused with the
reason, for example "does not declare its component order; add
quaternion_order = 'wxyz' or 'xyzw' to its metadata". Ordering and handedness
are never guessed.

## What is shown

The tool lists the session's vector signals and picks the first declared
quaternion. Selecting a four-component vector in the signal browser switches
to it. For the reading at or before the cursor it shows one of these states:

| State | When | Pose drawn |
| --- | --- | --- |
| valid | the reading is available, at most the stale limit old, and its norm is within 0.01 of one | solid box and colored axes |
| uncalibrated | as valid, but the reading's quality contains `uncalibrated`/`not_calibrated` or its `extra` has `calibrated: false` | dashed grey box, angles marked "NOT a valid current pose" |
| missing | the latest reading is unavailable (its quality, e.g. `NOT_CALIBRATED`, is shown) | none |
| stale | the latest reading is older than the stale limit | none |
| invalid | the norm is not within 0.01 of one, or a component is not a finite number | none |
| no data | no cursor, no reading at or before the cursor, or the signal is not in the session | none |

The previous pose is never carried into a missing, stale or invalid state, and
nothing is interpolated between readings: between two readings the earlier one
is shown until it becomes stale. The state is written over the drawing so a
reference frame without a pose cannot be mistaken for a level attitude.

The stale limit defaults to three nominal sample periods (the firmware's own
staleness rule), or 500 ms when the signal has no nominal rate. **Stale after**
overrides it.

Angles are Z-Y-X: `R = Rz(yaw) @ Ry(pitch) @ Rx(roll)`, in degrees. Yaw is
labelled with the declared `yaw_reference`; without one the label says to
treat yaw as relative and that it may drift. The view makes no claim of
drift-free or magnetic heading.

The drawing is a QPainter projection (no OpenGL): a fixed camera at azimuth 35
and elevation 25 degrees, dashed reference axes labelled `X ref`, `Y ref`,
`Z ref`, and the body box with its x (red), y (green) and z (blue) axes.
Cursor changes are redrawn at most every 33 ms, independently of how fast data
arrives; each redraw is one binary search and a few dozen lines.

`OrientationTool.configuration()` returns `{"signal_id", "max_age_ns"}`
(`max_age_ns` is `None` for the automatic limit); `restore_configuration()`
rejects malformed values and changes nothing then.

## Demo signal

**Simulator - attitude (finite)** emits the fictional `attitude.quaternion`
(20 Hz, `wxyz`, `hamilton`, frame `sensor`, reference `level`, yaw reference
"relative to start (fictional, drifts)"). Its yaw turns once every two
seconds while roll and pitch rock by up to 10 and 5 degrees. Readings 30-33
(1.50-1.65 s) are explicitly unavailable and readings 45-49 (2.25-2.45 s) are
missing, so the view shows the missing and stale states. It is a demo, not a
model of any sensor.

## Tests

`tests/core/test_orientation.py` (identity and 90-degree rotations, both
component orders, sign-equivalent quaternions, invalid norm and NaN, gaps,
calibration loss, backwards seek, the simulator family) and
`tests/ui/test_orientation_tool.py` (the tool in the real window following the
replay cursor through valid, missing and stale readings and back, refusal of a
3-vector, configuration, a CSV session without quaternions, drawn axes for
known rotations, and a redraw time budget).
