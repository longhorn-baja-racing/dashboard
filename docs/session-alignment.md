# Session alignment

Comparing two runs (for example lap 3 of one practice and lap 7 of another)
needs both runs on one time axis. `lbr_dashboard.core.session_alignment`
(standard library only) defines that axis with one manual offset, reports
whether the runs overlap and whether the matched signals are comparable, and
returns each run's samples for a comparison-time window. In the app,
**Analysis > Compare sessions** uses it to overlay a second run on the main
session (see [Compare sessions in the app](#compare-sessions-in-the-app)).

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

## Compare sessions in the app

**Analysis > Compare sessions** (`lbr_dashboard/ui/tools/session_overlay.py`)
opens a dock that overlays a second run on the main session. The main session
loaded with the source picker is the reference; it is never replaced or
modified.

1. Open a main session with the source picker (for example
   `sample_data/timestamped_demo.csv`).
2. In the dock, choose **Comparison file...** (a `timestamp_ms` CSV) or a
   finite fictional simulator, then **Load comparison**. The comparison run is
   read completely on a background worker into its own snapshot. A file that
   cannot be read is reported in the dock; the main session stays as it was.
3. Signals with the same id in both runs are matched automatically. The
   **Matched signals** tab lists every numeric main-run signal with a chooser
   for its comparison signal (or `(not compared)`), and a **Plot** check box
   that hides or shows that pair.
4. Set **Offset** in milliseconds (added to comparison-run times, positive
   moves the comparison run later), or place the replay cursor and press
   **Align at cursor** to start the comparison run's first matched reading at
   the cursor.

The plot uses the main run's time axis (seconds since its first reading).
Each matched pair has one color: the main run is solid with round markers,
the comparison run is dashed with square markers. Legend entries read
`Main run (main session): chamber_temperature [unit unspecified] - csv.column.1`
and `Comparison run (<file name>): ...`, so the run, signal, unit and id are
always visible. **Show: Main run / Comparison run** hide a whole run. The
shaded band is the overlap; outside it only one run has data.

Above the plot the dock lists missing signals, unit and frame mismatches and
the overlap, or why there is none, for example `No overlap: the comparison
run starts 4.200 s after the main run ends. ... Decrease the offset by at
least 4200 ms.`

The **At cursor** tab follows the replay cursor. For each pair it shows the
last main-run reading at or before the cursor, the last comparison-run reading
at or before the mapped comparison time, and their difference (comparison
minus main). A difference is only shown when both readings exist and the
units are the same; otherwise it reads `--` or `-- (units differ)`. Missing
readings stay missing.

`SessionOverlayTool.configuration(relative_to=None)` returns the comparison as
plain data and `restore_configuration(config, base_dir=None)` reloads it:

```json
{
  "comparison": {"path": "runs/lap7.csv"},
  "alignment": {"format": "lbr-session-alignment", "version": 1, "...": "..."},
  "show_main": true,
  "show_comparison": true,
  "hidden": []
}
```

`comparison` is `{"path": ...}` (stored as chosen, or relative to
`relative_to` when the file lies inside it; relative paths are resolved
against `base_dir` on restore), `{"preset": "process"}` for a simulator run,
or `null`. `alignment` is the `SessionAlignment.to_dict()` document with the
matched ids and the offset; it is applied once the comparison run has been
read. An invalid document raises `ValueError` and changes nothing.

The UI never imports a file reader: the source controller hands the dock a
`ComparisonSources` with the CSV reader and the finite simulators.

## Not included yet

- Finding sessions by identity and the integrated clock (#21, #22).
- Anchors, drift correction and more than two runs.
- Saving the comparison in a workspace file.

## Running the tests

On Windows (PowerShell) or any other platform, from the repository root:

```
uv run --locked --extra dev pytest tests/core/test_session_alignment.py tests/ui/test_session_overlay.py
```

To try it by hand, start the app with `uv run --locked lbr-dashboard`, open
`sample_data/timestamped_demo.csv` with the source picker, then open
**Analysis > Compare sessions** and load a copy of the same file whose
`timestamp_ms` values are shifted.
