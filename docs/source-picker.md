# Choose a telemetry source

Start the dashboard on Windows with `uv run --locked lbr-dashboard`. The
**Telemetry source** panel is part of the main dashboard.

1. Choose **Simulator — process (finite)** and press **Open CSV / Connect**.
   The seeded three-second recording loads without waiting three seconds.
   Select pressure, temperature, or status on the left. Numeric values plot
   against measurement time; structured status values appear in the table.
2. Choose **Simulator — motion (finite)** and connect. The previous source
   closes before the new one starts. Signal names, units, types and values
   come from the selected adapter's schema.
3. **Retry / Restart** creates a fresh run of the last requested source. The
   finite simulators use seed 7, so retry gives the same readings.
4. Choose **Simulator — process (live)** to watch new readings arrive. Press
   **Disconnect** to cancel and release its worker. Readings remain available
   for inspection, as stated in the panel. Closing the window also cancels and
   waits for cleanup without blocking the GUI thread.
5. Choose **CSV replay — timestamp_ms preset**, then connect and select
   `sample_data/sample_baja_log.csv` or your own file in that explicit preset.
   Loading happens on a worker. Disconnect cancels preparation. A failed open
   reports a reason and leaves the previous source and readings intact.

The active identity, lifecycle state, loss count, error and adapter capabilities
come from the #20 contract. Source seek and source rate buttons are disabled
with explanatory tooltips when unsupported. They invoke only capabilities an
adapter actually advertises. They are distinct from the session playback UI
in #41. No USB device, serial port or physical sensor is assumed.

## CSV preset and integration boundary

This bridge supports the existing wide CSV shape with exactly one
`timestamp_ms` column. All other physical columns have separate IDs, so repeated
header names cannot select the wrong readings. Times convert exactly to signed
64-bit integer nanoseconds; non-finite, excessive or sub-nanosecond values are
rejected. Headers provide display names, not inferred units or device identity.
A column is numeric if all populated cells are finite numbers; otherwise it is
text. Empty cells remain unavailable, including in text columns. Non-finite
numeric spellings are errors rather than zeros.

Limits are 16 MiB, 20,000 data rows, 64 signals and 200,000 samples. Full finite
history is retained in the session; the selected plot displays the latest 1,000
samples and its table displays the latest 100. Live history is bounded to
1,000 samples per signal. Times in the table remain original integer nanoseconds;
the plotted seconds are relative to that signal's first retained reading.
The raw table is read-only; changing recorded measurements is not a source action.

The arbitrary mapping workflow in #31 remains separate. This preset does not
auto-detect unknown formats or devices. File > Open retains the legacy table
loader for compatibility; a successful legacy import releases the active source.
Use the source picker for the common CSV/simulator session path.

## Internal adapter extension and verification

Register a `SourceOption` with the application-owned `SourceController`. Its
preparation callable receives cancellation on a worker and returns a nonblocking
source factory. The resulting `TelemetrySource` supplies its schema, bounded
events, capabilities and health. Widgets do not own another transport state
machine or know sensor-specific fields.

Preparation must cooperate with cancellation; source open/start/stop/close must
obey the #20 contract. A source that cannot close immediately is polled while
the event loop remains responsive. After two seconds the panel explains that
cleanup is pending. It never starts the replacement or claims release until
close succeeds. Python cannot forcibly release an uncooperative adapter.

Run `uv run --locked pytest tests/ui/test_source_picker.py
tests/importers/test_csv_source.py`. Tests exercise the real picker with CSV,
both finite simulator families, live simulation, failures, cancellation, retry,
unsupported/supported controls, and delayed cleanup. A negative control disables
the production render binding and verifies that the ordinary workflow test fails
because source samples did not reach the plot.
