# Raw telemetry and plot image export

Exports give reproducible extracts for analysis in other tools. They copy raw
samples; they never align, resample, interpolate or derive values, and they do
not change the source log.

- `lbr_dashboard.core.export.export_csv()` writes selected raw samples to a CSV
  file plus a JSON metadata sidecar (standard library only).
- `lbr_dashboard.ui.plot_export.export_plot_image()` writes a plot, as shown,
  to a PNG file.

## Input

`export_csv()` takes a list of `ExportSignal(signal_id, unit, times_ns, values)`
records. `times_ns` are integer nanoseconds in non-decreasing order and
`values` holds one value per timestamp. This is a plain export input, not a
signal model; it maps directly to the arrays an importer produces and will be
filled from the session query (#21) once that exists.

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
| `value` | Shortest text that reads back as the exact same 64-bit float (`repr`). Missing values are an empty cell. |
| `unit` | Unit text as given (may be empty). |

- Rows are ordered by signal, then by time. Samples with equal timestamps keep
  their source order.
- **Mixed rates and gaps:** every signal keeps its own timestamps. A 100 Hz and
  a 10 Hz signal produce 100 and 10 rows per second; no rows are added to put
  them on a common time base, and gaps stay gaps. Pivot by `time_ns` in the
  analysis tool if a wide table is wanted, and choose the alignment there
  explicitly.
- **Missing values:** `None` and NaN are written as an empty `value` cell,
  never as `0`. Infinite values are real data and are written as `inf` and
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
| `format`, `format_version` | `"lbr-dashboard-raw-csv"`, `1`. |
| `csv_file`, `csv_sha256` | CSV file name and SHA-256 of its bytes, so a mismatched pair is detectable. |
| `encoding`, `columns`, `row_count`, `row_order` | As described above. |
| `time` | Time column, unit (`ns`) and reference: the source log clock, exported unchanged. |
| `interval` | `start_ns`, `end_ns` (`null` = unbounded) and `"boundaries": "closed"`. |
| `missing_value` | `"empty value cell"`. |
| `alignment`, `resampling`, `derived_operations` | `"none"`, `"none"`, `[]`. |
| `source` | Optional free-text provenance, for example the source log name. |
| `signals` | Per signal in export order: `signal_id`, `unit`, `sample_count`, `missing_count`, `first_time_ns`, `last_time_ns` (`null` when no samples). |

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

`export_plot_image(plot, path, width=None, overwrite=False)` renders a
`PlotWidget` or `PlotItem` with pyqtgraph's image exporter and returns the
written `(width, height)`. Axes, axis labels, title and legend come from the
plot itself, so the image matches what is on screen. `width` is in pixels and
the height keeps the plot's aspect ratio; `None` uses the current plot size.
The PNG is written atomically with the same overwrite rule as the CSV export.
