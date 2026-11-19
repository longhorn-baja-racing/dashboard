# Session alignment

Comparing two runs (for example lap 3 of one practice and lap 7 of another)
needs both runs on one time axis. `lbr_dashboard.core.session_alignment`
(standard library only) defines that axis with one manual offset, reports
whether the runs overlap and whether the matched signals are comparable, and
returns each run's samples for a comparison-time window. Plotting the aligned
runs is separate work (#64).

## Inputs

Sessions are plain values built by the caller. The module does not load or own
sessions; it only reads these values.

- `SessionIdentity(session_id, label="")`: `session_id` is the stable identity
  that is saved and compared exactly; `label` is for display.
- `SignalTrace(signal_id, times_ns, values, unit=None, frame=None)`:
  `times_ns` are integer nanoseconds in that session's own time base and must
  be non-decreasing; `values` are returned unchanged (`None` and NaN stay as
  they are). `unit` and `frame` are free text.
- `SessionData(identity, signals)`: one session. Signal ids must be unique.
- `SignalMatch(reference_signal_id, compared_signal_id)`: a pair of signals to
  compare. The ids may differ, so a signal from one source type can be matched
  with a differently named signal from another.

Mismatched lengths, decreasing or non-integer timestamps, empty or
whitespace-padded ids and duplicate signal ids raise `ValueError`.

## Offset and time mapping

`SessionAlignment(reference, compared, offset_ns=0, matches=())` holds the two
identities (which must differ), one integer offset and the matched pairs.

The comparison timeline is the reference session's own timeline:

```
comparison_ns = original_ns                (reference)
comparison_ns = original_ns + offset_ns    (compared)
```

A positive offset moves the compared run later. `to_comparison_ns(side, t)` and
`to_original_ns(side, t)` convert in either direction for `Side.REFERENCE` or
`Side.COMPARED`. Original timestamps are never changed; queries return them
next to the mapped times. Integers are exact at any size, so epoch-based and
uptime-based sessions can be aligned without rounding.

Example: run B was logged with logger uptime and starts at 5 s, run A starts
at 0. `offset_ns = -5_000_000_000` puts run B's first sample at comparison
time 0. `with_offset(new_offset)` returns a copy for nudging the offset.

## Overlap

`overlap(alignment, reference, compared, match=None)` returns an `Overlap` with
the closed comparison-time interval `[start_ns, end_ns]` covered by both runs,
plus each side's span in comparison time.

A side's span runs from its earliest to its latest sample over the selected
signals: `match` alone when given, otherwise every matched signal, otherwise
every signal in the session. Missing signals are skipped. Spans that only touch
overlap at one instant (`duration_ns == 0`).

When there is no overlap, `start_ns` and `end_ns` are `None` and `reason`
explains why, for example:

```
compared session 'run-b' starts at comparison time 5000000000 ns, 4900000000 ns
after reference session 'run-a' ends at 100000000 ns; decrease offset_ns by at
least 4900000000 to overlap
```

A side with no samples for the selected signals is reported the same way.

## Matched-signal checks

`check_matches(alignment, reference, compared)` returns a tuple of
`MatchIssue(kind, match, message, side)`; an empty tuple means every pair is
present and consistent.

- `MISSING_SIGNAL`: a matched signal id is not in that session (`side` says
  which). Both sides can be reported for one pair.
- `UNIT_MISMATCH`: the unit strings differ.
- `FRAME_MISMATCH`: the frame strings differ.

Units and frames are compared as exact strings and are never converted. A pair
is reported when at least one side states a unit (or frame) and the two differ,
so `"km/h"` against an unknown unit is a mismatch; two unknown values are not.
The checks report problems; they do not block queries.

## Window query

`window(alignment, reference, compared, match, start_ns, end_ns)` returns an
`AlignedWindow` with an `AlignedSamples` for each side: the samples with
`start_ns <= comparison time <= end_ns`, as `comparison_times_ns`,
`original_times_ns` and `values`, plus the session identity, side, signal id,
unit and frame. `samples_in_window(...)` does the same for one side.

Each side keeps its own sample times and rate. Nothing is resampled,
interpolated or paired; pairing for deltas is a later step.

Passing a session whose id is not the one in the alignment, an unknown signal,
non-integer bounds or `start_ns > end_ns` raises `ValueError`.

## Save and restore

`SessionAlignment.to_dict()` returns a JSON-serializable document and
`alignment_from_dict(document)` rebuilds it:

```json
{
  "format": "lbr-session-alignment",
  "version": 1,
  "reference": {"session_id": "run-a", "label": "Endurance practice, lap 3"},
  "compared": {"session_id": "run-b", "label": "Endurance practice, lap 7"},
  "offset_ns": -5000000000,
  "matches": [{"reference": "wheel_speed", "compared": "rear_wheel_speed"}]
}
```

Loading is strict. A wrong `format`, an unsupported `version`, a missing or
unknown field, or a value of the wrong type (including a boolean or float
where an integer is required) raises `ValueError` naming the field. The
document stores identities, not data: restoring it with the same sessions
reproduces the same mapping and windows.

## Not included yet

- Finding sessions by identity and the integrated clock (#21, #22).
- Anchors, drift correction and more than two runs.
- Overlay plots, legends and cursor deltas (#64).

## Running the tests

On Windows (PowerShell) or any other platform, from the repository root:

```
uv run --locked --extra dev pytest tests/core/test_session_alignment.py
```
