# Telemetry source conformance

Run the same source contract checks from the repository root, without devices or
a running dashboard:

```sh
uv sync --locked --extra dev
uv run --locked pytest tests/source_contract -v
```

The suite uses the [signal model](signal-model.md) and [source lifecycle
contract](telemetry-sources.md). It exercises an iterator fake, a CSV-backed
fixture, and the real `SimulatorSource`. The fictional process and motion
families combine fast and slow signals, duplicate display names, true zeros or
`False`, and unavailable values. They use signed integer nanoseconds, including
timestamps beyond exact floating-point integer precision.

The shared cases cover descriptors and records, repeat open/close, independent
runs with the same source ID, cancellation, lossless replay when the consumer
stalls, bounded live overflow with reported loss, and failure after start. The
reader cases also cover malformed input, cancellation before the first record,
and preservation of late and duplicate records. The simulator has a generated,
ordered schedule and no input reader, so those reader cases do not apply.

## Add an adapter

1. Define two or more signals in an explicit mapping. Describe the source and
   signal IDs, input fields, data types, units, timestamp domain and conversion
   to integer nanoseconds. Explain the input's missing-value and invalid-data
   rules. Build a `SignalSchema` and emit typed `SourceEvent` records with
   `SignalSample` payloads. Keep a valid zero or `False` distinct from an
   unavailable sample. Select the mapping explicitly; do not infer a device
   from unknown input.
2. Create deterministic input and its expected schema and record events. Write
   the expectations from the input format's rules; do not obtain them by
   decoding the fixture with the adapter under test.
3. Add the adapter as a parameter in
   [test_conformance.py](../tests/source_contract/test_conformance.py) and build
   its scenarios in [examples.py](../tests/source_contract/examples.py). Or
   reuse the assertions in [checks.py](../tests/source_contract/checks.py)
   directly in an adapter test. Pass an `AdapterRun` with a resource probe if
   the adapter owns a reader, handle, or other input resource. Use the
   capabilities and input type to decide which scenarios apply.
4. Run `assert_controls` for seek and rate reporting. An unsupported control must
   raise `UnsupportedSourceOperation`. A supported one needs a probe that checks
   its effect: for example, seek changes which records are delivered and rate
   changes their delivery schedule without rewriting the original timestamps.

The finite-run helper bounds collection by both wall time and event count.
Cancellation and close checks give the worker a deadline to release resources.
Negative controls corrupt delivered IDs, timestamps and missingness, make a
finite reader stream forever, misreport its queue capacity, and disable
cooperative stop. Each runs the same assertions as the passing adapters and
must fail for the expected reason.

## CSV fixture mapping

The suite's UTF-8 CSV is a test format with the header
`signal,time_ns,value,available,quality,unit,source`. Each row maps to one
record. `time_ns` is a base-10 integer in the relative time domain, `value` is a
JSON scalar (`null` for unavailable), and `available` is `0` or `1`. IDs, quality
and unit are read verbatim; descriptors come from the supplied fixture schema.
The CSV fixture uses an actual file reader. Its live case uses fixture values
to exercise streaming behavior. This is a test input mapping, separate from
the production CSV adapter in #31. That adapter can register its own mapping
and fixture when integrated.
