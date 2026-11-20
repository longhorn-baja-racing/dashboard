# Adding a telemetry source adapter

An adapter reads an explicitly selected file or documented stream. It decodes
that format into `SignalDescriptor`, `SignalSample`, and `SourceEvent` objects.
The application controller drains these events into the shared telemetry
session, which supplies the plot and other widgets. Keep file parsing, frame
validation, and hardware-specific fields inside the adapter.

## Run the example on Windows

From the repository root with [uv installed](../README.md#development-setup):

```powershell
uv sync --locked --extra dev
uv run --locked python tests/source_contract/tutorial.py tests/fixtures/tutorial-bench-log.txt
uv run --locked pytest tests/source_contract/test_tutorial.py tests/ui/test_adapter_tutorial.py -v
```

Choose **Example bench log** in the source picker and click **Open CSV / Connect**.
The numeric plot and table receive pressure readings of 0, 7, and 11 Pa. The
table also has an unavailable reading at 10 ms. The plot leaves that reading
out instead of drawing it as zero.

The [example adapter](../tests/source_contract/tutorial.py) reads a local
ASCII file with one `microseconds,pascals` record per line and no header.
Each timestamp is a nonnegative integer in one device-monotonic clock domain.
Each pressure value is an integer or a blank field. The adapter converts
microseconds to integer nanoseconds exactly, with a signed 64-bit upper bound.
It accepts up to 64 bytes per record excluding LF or CRLF and up to 20,000
records. An empty log fails. A malformed or excess record reports its line
number. The file closes when the iterator exits.

## Map the input into the source contract

1. Assign a stable signal ID and an opaque source ID. A descriptor also defines
   the display name, hierarchy, value type and shape, and canonical unit where
   applicable. Put the source's actual sample unit on the sample. Report
   availability explicitly and use `None` for missing values.
2. Declare the timestamp domain and preserve measurement time separately from
   arrival time. Do not round through a float, sort away duplicate times, or
   silently compare unrelated clocks. If a device resets its clock, start a
   new session for the new epoch.
3. Pass the fixed schema and a cancellable generator factory to
   `IteratorSource`. The factory yields records and can report health or
   adapter errors. The source emits descriptors and terminal events itself.
4. Use finite reads and cancellation checks where a worker might block.
   Release handles with a context manager or `finally`. Cap individual payloads
   as well as the number of queued events. A finite source waits for space
   while a live source can drop events after its publication deadline. Read
   health and loss counters even if an end or error event never arrives.

The owner calls `open()` to prepare a cycle and `start()` to start its worker,
then drains the queue while it runs. The worker reports `complete`,
`cancelled`, or `failed` as its terminal reason. A raised read error becomes an
error event and a failed terminal state, with details retained in `health()`.
`stop()` requests cancellation and wakes blocked publishers. A false result
from `close(timeout)` means cleanup is still pending. `IteratorSource` does
not advertise seek or rate control; replaying cached data is the application's
job. See the [lifecycle and backpressure contract](telemetry-sources.md).

## Register the adapter where the app is composed

Register a `SourceOption` on the existing `SourceController` as shown in the
example's `create_demo_window()`. Its preparation callback runs in the background and
receives a cancellation token. Return a factory that creates a fresh source
for each attempt. The example defers opening the chosen file to the source
worker. If preparation itself reads input, check the token there as well and
close those handles before returning. The picker, session, and plot use the
same path as the built-in sources.

## Verify a new adapter

Use the [shared conformance assertions](source-conformance.md). Write expected
descriptors and events directly from the format specification. The
[example tests](../tests/source_contract/test_tutorial.py) check valid replay,
reject a fabricated zero, report a malformed line, and stop with a full queue
to verify that the file closes. The [picker test](../tests/ui/test_adapter_tutorial.py)
uses the tutorial's registration code and checks the real plot and table.
The tutorial branch is stacked on the conformance suite (PR #161), which
includes the picker (PR #148). The source contracts from issues #18 and #20
remain under review.

Before adding a format or transport:

- **CSV or text log** — Document encoding, columns, timestamp scale, blank
  fields, malformed-record behavior, and input limits.
- **SD or binary log** — Specify layout version, byte order, record lengths,
  integrity checks, and how a truncated final record is handled.
- **Serial or CAN** — Require an explicit port or channel and a documented
  protocol. Specify frame boundaries, timeouts, disconnect behavior and loss
  reporting. The telemetry path is read-only and sends no vehicle commands.
  The [USB serial source](serial-source.md) is a worked example: port access
  behind a read-only interface, a fake serial line in the tests, and the
  firmware's golden capture as the oracle.
- **IMU or other sensor** — Document units, axes, coordinate frame and
  calibration in the mapping. Add fixtures for real zeros and unavailable
  readings. An undocumented device needs its own confirmed mapping before it
  can be supported.
