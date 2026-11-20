# Raw telemetry and plot image export

Exports give reproducible extracts for analysis in other tools. They copy raw
samples; they never align, resample, interpolate or derive values, and they do
not change the source log.

- `lbr_dashboard.core.export.export_csv()` writes selected raw samples to a CSV
  file plus a JSON metadata sidecar (standard library only).
- `lbr_dashboard.core.export.snapshot_signals()` reads the raw records of
  chosen signals from a session snapshot (#21) as export input.
- `lbr_dashboard.ui.plot_export.export_plot_image()` writes a plot, as shown,
  to a PNG file.

## In the app

**Analysis > Export** opens the export tool as a dock:

1. The list shows every signal of the loaded session with its type and unit.
   The signal selected in the signal browser is checked by default and the
   check follows the browser until you change the checks yourself. **Check
   all** and **Check none** change every check at once.
2. The line under the list states what will be exported: with a shared time
   interval (Analysis > Select visible time range, then drag its edges) only
   the records inside it, edges included; without one the **whole session**.
   It also shows how many records that is.
3. **Export CSV...** asks for a file name (`.csv` is added if missing). If the
   CSV or its `.meta.json` file already exists you are asked before either is
   replaced. Large exports show a progress dialog with **Cancel**.
4. The result line names the written file. A failure (for example a full disk
   or a missing folder) is shown in a message and in the result line; a
   cancelled or failed export leaves no new file and keeps existing ones.

The tool reads the latest session snapshot, never the arrays a plot draws, so
the file holds exactly the recorded samples: no decimation, no alignment and
no filled-in values.

**Analysis > Save main plot as PNG...** writes the main plot as shown, with its
title and axis labels. The image gets a legend naming the trace even though the
plot on screen has none. Existing images need confirmation before they are
replaced, and a failure is reported in a message.

## Input

`export_csv()` takes a list of `ExportSignal` records:

| Field | Content |
| --- | --- |
| `signal_id`, `unit` | Id and unit text (may be empty). |
| `times_ns`, `values` | Integer nanosecond times in non-decreasing order and one value per time. `None` and NaN mean missing. |
| `available` | Optional per-sample flags; `False` marks a reading the source reported as unavailable. Its value is never written. |
| `quality` | Optional per-sample quality label from the source (for example `measured`, `missing`). |
| `frame` | Coordinate or reference frame of the values; default `"unspecified"`. |
| `display_name`, `data_type`, `timestamp_domain` | Optional descriptor details copied into the metadata. |

This is a plain export input, not a signal model. `snapshot_signals(snapshot,
signal_ids, start_ns=None, end_ns=None)` fills it from a `SessionSnapshot`:
values, availability and quality are copied from each sample unchanged and the
unit (`canonical_unit`, empty when none), frame, name, type and time domain
come from the signal descriptor. The frame is the first non-empty text under
`frame`, `coordinate_frame` or `reference_frame` in the descriptor's `extra`,
`display` or `calibration` metadata, else `"unspecified"`. No current importer
declares a frame, so their exports say `"unspecified"`.

Only the signals passed in are exported, in the order given. Signal ids must be
unique.

## Interval boundary rule

The interval is **closed**: a sample at exactly `start_ns` or exactly `end_ns`
is exported. `start_ns=None` or `end_ns=None` leaves that side unbounded, so
both `None` exports every sample. An interval with no samples gives a CSV with
only the header row. `start_ns > end_ns` is rejected.

## CSV format

One row per raw sample ("long" or "tidy" format):

| Column | Content |
| --- | --- |
| `signal_id` | Signal id as given. |
| `time_ns` | Sample time, integer nanoseconds, unchanged from the source. |
| `time_s` | The same time in seconds as exact decimal text with 9 fraction digits (`-1500000001` -> `-1.500000001`). It is never rounded through a float. |
| `value` | Numbers: shortest text that reads back as the exact same 64-bit float (`repr`). Booleans: `true`/`false`. Text: unchanged. Vectors and other JSON values: compact JSON (`[0.5,-1.0,9.81]`). Missing values are an empty cell. |
| `unit` | Unit text as given (may be empty). |
| `available` | `true` when the sample has a value, `false` when it is missing (reported unavailable, `None` or NaN). |
| `quality` | Quality label reported by the source, empty when none. |

- Rows are ordered by signal, then by time. Samples with equal timestamps keep
  their source order.
- **Mixed rates and gaps:** every signal keeps its own timestamps. A 100 Hz and
  a 10 Hz signal produce 100 and 10 rows per second; no rows are added to put
  them on a common time base, and gaps stay gaps. Pivot by `time_ns` in the
  analysis tool if a wide table is wanted, and choose the alignment there
  explicitly.
- **Missing values:** unavailable readings, `None` and NaN are written as an
  empty `value` cell with `available` set to `false`, never as `0`. An
  available empty text value has an empty cell with `available` `true`. Infinite values are real data and are written as `inf` and
  `-inf` (Python and pandas read these; Excel shows them as text).
- Encoding is UTF-8 **without** a byte order mark, with `\r\n` line endings and
  standard CSV quoting (ids or units containing commas or quotes are quoted).
  Excel on Windows opens a double-clicked UTF-8 file without a BOM as the local
  code page, so non-ASCII ids or units (for example a degree sign) look wrong;
  use Data -> From Text/CSV and pick UTF-8 instead. Other tools read the file
  directly.

## Metadata sidecar

`<file>.csv` is accompanied by `<file>.csv.meta.json` (UTF-8 JSON):

| Key | Content |
| --- | --- |
| `format`, `format_version` | `"lbr-dashboard-raw-csv"`, `2` (version 1 had no `available` and `quality` columns and no per-signal frame). |
| `csv_file`, `csv_sha256` | CSV file name and SHA-256 of its bytes, so a mismatched pair is detectable. |
| `encoding`, `columns`, `row_count`, `row_order` | As described above. |
| `time` | Time column, unit (`ns`) and reference: the source log clock, exported unchanged. |
| `interval` | `start_ns`, `end_ns` (`null` = unbounded), `"boundaries": "closed"` and `scope`: `"all samples"` (whole session) or `"samples inside the interval"`. |
| `missing_value`, `available`, `quality`, `frame` | How missing values, the two extra columns and the per-signal frame are represented. |
| `alignment`, `resampling`, `derived_operations` | `"none"`, `"none"`, `[]`. |
| `source` | Optional free-text provenance; the app records the source ids of the exported signals. |
| `signals` | Per signal in export order: `signal_id`, `display_name`, `data_type` (when known), `unit`, `frame`, `timestamp_domain` (when known), `sample_count`, `missing_count`, `first_time_ns`, `last_time_ns` (`null` when no samples). |

The export time is deliberately not recorded: exporting the same selection
twice gives byte-identical files, which makes extracts easy to compare.

## Safe finalization

- If the CSV or the metadata file already exists and `overwrite` is false,
  `ExportError` is raised before anything is written.
- Both files are first written to hidden temporary files
  (`.<name>.<random>.tmp`) in the target folder and synced to disk.
- Only then are they moved into place with `os.replace`. Existing targets are
  first renamed to backups; if any move fails (for example because the old CSV
  is open in Excel on Windows), the new files are removed and the backups are
  restored, so the old pair is left as it was. Backups are deleted on success;
  if a restore itself fails, the original stays in its backup file.
- Any write failure (such as a full disk) raises `ExportError` with the
  original `OSError` as its cause. Cancellation raises `CancellationRequested`.
  In both cases the temporary files are removed and no new output is left.

## Progress and cancellation

`export_csv()` accepts a `CancellationToken` (`lbr_dashboard.core.lifecycle`)
and a `progress(done_rows, total_rows)` callback. Both are checked or called
every 10,000 rows and once at the end (the last call has `done == total`).
Once all rows are written the token is checked one final time; moving the
files into place is not interruptible. The export can run inside a
`BackgroundService`, which treats `CancellationRequested` as a clean stop.

## Plot images

`export_plot_image(plot, path, width=None, overwrite=False, legend=False)` renders a
`PlotWidget` or `PlotItem` with pyqtgraph's image exporter and returns the
written `(width, height)`. Axes, axis labels, title and legend come from the
plot itself, so the image matches what is on screen. `width` is in pixels and
the height keeps the plot's aspect ratio; `None` uses the current plot size.
With `legend=True` a plot without a legend gets one in the image only, listing
its named data items; the plot on screen does not change.
The PNG is written atomically with the same overwrite rule as the CSV export.
