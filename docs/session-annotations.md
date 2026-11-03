# Session annotations

`lbr_dashboard.core.annotations` stores timestamped notes that a person adds to
a recorded session ("brake fade here", "driver change"). It is the storage
backend for issue #61; there is no UI yet. The module is standard library only.

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
files, invalid UTF-8 or JSON, duplicate JSON keys, `NaN`/`Infinity`, a wrong
`format` or `version`, missing or unknown keys, wrong types, values out of
range, duplicate IDs, and oversized files. Problems inside one annotation name
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
