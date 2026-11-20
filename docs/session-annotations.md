# Session annotations

`lbr_dashboard.core.annotations` stores timestamped notes that a person adds to
a recorded session ("brake fade here", "driver change"). It is the storage
backend for issue #61 and is used by the **Analysis > Annotations** tool
described below. The module is standard library only.

## In the app: Analysis > Annotations

Open a timestamped CSV with the source picker, then choose
**Analysis > Annotations**. The dock lists the markers of the loaded session
with their time (exact session seconds; the tooltip shows nanoseconds), label
(the `category`) and note (the `text`).

- **Add at cursor** adds a marker at the replay cursor's exact session time.
  Type a label (optional) and a note first; an empty note becomes "Marker".
- **Add for interval** adds two markers, at the start and at the end of the
  shared time interval (Analysis > Select visible time range), with the notes
  "<note> (start)" and "<note> (end)". Interval annotations themselves are
  left for a later change.
- Select a row to load its label and note into the fields, then **Save edit**
  to change them, **Move to cursor** to move the marker to the replay cursor,
  or **Remove** to delete it.
- **Go to marker** (or double-click a row) pauses replay and moves the replay
  cursor to the marker's exact session time. This works whenever the replay
  slider does; a live source, a loading session or a time outside the
  recording shows an explanation instead.
- Every marker is drawn on the main time plot as a dashed vertical line
  (hover for its note). The lines come back whenever the plot is redrawn,
  for example after selecting another signal.

For a file-backed session every change is saved at once to the sidecar file
next to the log (`<log name>.annotations.json`). Opening the file again, also
after restarting the app, restores the markers. Opening a session never
creates a file; the first change does. The log is only read.

- If a save fails (read-only folder, full disk), the previous sidecar file and
  the markers shown stay as they were, and the dock shows the reason in red.
- If the sidecar cannot be read (invalid JSON, wrong format), the dock shows
  the reason and adding markers is disabled, so the file is never overwritten.
  Fix or move the file and open the log again.
- If the log's content changed since the markers were saved, they are still
  shown, with a note; the next change attaches them to the current content.

Simulator sessions have no file. Their markers are kept in memory only and are
lost when another source is opened or the app closes; the dock says so.

## What an annotation is

| Field | Type | Rule |
| --- | --- | --- |
| `annotation_id` | `str` | Non-empty, not padded, at most 64 characters. `new_annotation()` uses a random UUID in hex. |
| `time_ns` | `int` | Integer nanoseconds on the session's own time axis; any signed 64-bit value, including negative. `bool` and `float` are rejected. |
| `text` | `str` | Must contain something other than whitespace; at most 4000 characters; may span lines. |
| `category` | `str` | Free-form label, may be empty, at most 64 characters. |
| `source_id` | `str` | The signal or source the note is about, may be empty for a whole-session note, at most 256 characters. |

`time_ns` uses the same integer nanosecond values as the session's sample
times, with no unit conversion or rounding, so a saved marker selects exactly
the session time it was placed at. Annotations are keyed by time, source, and
session (the log fingerprint below); they do not depend on any particular
sensor or signal name.

`Annotation`, `AnnotationSet`, and the other types are immutable. Editing
methods (`add`, `update`, `remove`, `with_fingerprint`, `merge`) return a new
set. An unknown ID raises `KeyError` with the ID; a duplicate ID raises
`ValueError`. Sets are always sorted by `(time_ns, annotation_id)`.

## Annotations are not log events

Annotations are user notes kept in a separate file. They never modify the
raw log or any firmware/log events in it, and they are never mixed into the
imported session data. The log is opened only for reading, to fingerprint it.
`save_annotations` also refuses to overwrite a file whose content matches the
log's fingerprint, so passing the log path by mistake cannot destroy it.

## Navigation

- `next_after(t)` returns the first annotation strictly after `t`.
- `previous_before(t)` returns the last annotation strictly before `t`.
- `in_range(start, end)` uses a CLOSED interval: an annotation at exactly
  `start` or `end` is included.
- `at_or_near(t, max_distance)` returns the closest annotation with
  `abs(time_ns - t) <= max_distance`; on a tie the earlier one wins.

Annotations that share one time are a single navigation stop;
`in_range(t, t)` returns all of them.

## Sidecar file

The default file sits next to the log: `sidecar_path(log)` is
`<log file name>.annotations.json`, for example `run 1.csv.annotations.json`.

```json
{
  "format": "lbr-dashboard-annotations",
  "version": 1,
  "log": {
    "name": "run 1.csv",
    "size": 52,
    "sha256": "<64 lowercase hex digits>"
  },
  "annotations": [
    {
      "id": "3f2a...",
      "time_ns": 1000000,
      "category": "driver",
      "source_id": "front_left_rpm",
      "text": "Brake fade"
    }
  ]
}
```

- UTF-8, written with LF line endings and a fixed key order, so saving the same
  set twice gives identical bytes and diffs stay small. Non-ASCII user text is
  stored as UTF-8 rather than escaped, so the file stays readable. A UTF-8
  byte order mark and CRLF line endings (from editing on Windows) are accepted
  when reading.
- `time_ns` is a JSON integer. Tools that read JSON numbers as doubles lose
  precision above 2^53 ns (about 104 days); this application does not.
- Files larger than 8 MiB are rejected on read and never written.

## Matching the log

The `log` block is a fingerprint: file name, size, and SHA-256 of the content.
Only size and SHA-256 are compared; the name is informational. A log moved or
renamed together with its sidecar therefore still matches.

`load_annotations(path, log_path=log)` returns the set and a status:

| Status | Meaning |
| --- | --- |
| `OK` | The log exists and its content matches the fingerprint. |
| `LOG_CHANGED` | The log exists but its content differs. The annotations are still returned, with `current_fingerprint`, so the caller can ask the user and then use `with_fingerprint()` to attach them to the new content. |
| `LOG_MISSING` | The log was not found. The annotations are still returned. |
| `NOT_CHECKED` | No `log_path` was given, for example when reading an exported file to merge. |

A missing sidecar is not an error: an empty set for the current log is
returned and nothing is written. Because a set always belongs to a log, a
missing sidecar with no readable log raises `AnnotationFileError`. Errors
other than "not found" while reading the log propagate as `OSError`.

## Errors

`AnnotationFileError` carries `path` and `reason`. It is raised for unreadable
files, invalid UTF-8 or JSON, strings that cannot be written as UTF-8 (such
as an escaped lone surrogate like `\udcff` in any text field or the log name),
duplicate JSON keys, `NaN`/`Infinity`, a wrong `format` or `version`, missing
or unknown keys, wrong types, values out of range, duplicate IDs, and
oversized files. Problems inside one annotation name
its index, for example `annotation 2: duplicate id 'dup'`.

## Atomic saves

`save_annotations(path, annotation_set)` writes to a new temporary file in the
same directory, flushes it and calls `os.fsync`, then replaces `path` with
`os.replace` (atomic on Windows and POSIX). If any step fails, the previous file
is left as it was, the temporary file is removed, and `AnnotationFileError`
reports the path and reason. Temporary files are named
`.<file name>.<random hex>.tmp` and are not left behind after a successful
save. The directory itself is not fsynced, so after a power loss the previous
version may still be the one on disk, but the file is never half written.

## Import and merge

`merge(other)` adds annotations from `other` whose IDs are new. An ID in both
sets with identical content is skipped. An ID in both sets with different
content keeps the existing annotation and is listed in `MergeResult.conflicts`;
nothing is overwritten silently. Merging sets recorded for different log
content raises `ValueError`. Export is a normal `save_annotations` to another
path.
