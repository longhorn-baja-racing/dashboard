# Generic signal model

Issue #18 adds framework-neutral descriptors and per-signal samples in
`lbr_dashboard.core`. It does not choose hardware, a wire protocol, unit
conversion, resampling, or a replacement for the existing CSV table UI.

## Descriptors and identity

`SignalDescriptor` contains a stable `identifier`, human `display_name`, hierarchy
`path`, `data_type`, `shape`, `source_id`, optional `device_id`, canonical unit,
nominal rate, description, availability, calibration and display metadata, and
timestamp domain. Adapters supply these values; there are no built-in vehicle
channels. IDs use lowercase letters/digits separated by dots, dashes or
underscores and start with a letter. Path segments start with a letter and allow
lowercase letters, digits, dashes and underscores. Display names need not be unique.

IDs are unique across a schema/registry, including across sources. Adapters must
namespace otherwise identical local IDs by source. `SignalRegistry` returns
descriptors sorted by ID; `under_path(())` returns all descriptors and other path
queries match segment prefixes. Search requires all query tokens. Batch
registration checks every collision before modifying the registry, including on
retry after failure.

Known types are `numeric` (finite integer/float, excluding boolean), `boolean`,
`string`, `vector` (numeric nested arrays matching every positive shape dimension),
and `event`/`status` (JSON payloads). Known non-vector types have scalar shape `()`;
events/status can contain structured JSON. Unknown type names retain their shape
and JSON values without being interpreted. Type/shape checking occurs at the
descriptor boundary: call `descriptor.validate_sample(sample)`,
`series.validate(descriptor)`, or pass `descriptor=` to `SignalSeries.from_samples`
or `from_dict`. A standalone sample cannot infer its descriptor's type.

Units are opaque nonempty tokens without whitespace (for example `Pa`, `psi`,
`m/s²`, `°C`); no unit vocabulary or conversion is imposed. A sample can retain
its original unit even when it differs from the descriptor's canonical unit.
Rates must be finite and positive when supplied; they describe expectations and
never generate samples.

## Sparse samples, clocks and missingness

`SignalSeries` owns samples for one signal. A 100 Hz source and a 1 Hz source
retain their independent counts/times; no wide rows, interpolation, padding,
sorting or deduplication occurs. Empty series and repeated/reset timestamps are
valid. When a sample supplies `source_id`, descriptor validation rejects a
different source; an omitted source resolves through the bound descriptor.

`timestamp_ns` is an exact signed integer in nanoseconds, excluding boolean.
Negative relative/pre-trigger times and integers beyond transport-specific
uint64 limits are preserved. Transport range checks belong to the adapter.
`timestamp_domain` is an explicit nonempty opaque string, for example
`device_monotonic`, `relative`, or `unix_utc`. A descriptor with an explicit domain
rejects samples with a different domain. The compatibility default `unspecified`
does not assert a clock origin or synchronize clocks. Adapters should prefer the
device monotonic measurement time when available and describe its origin;
reception time must not silently replace it. Clock normalization belongs to #21.

The legacy CSV UI remains a table importer and does not implicitly create these
descriptors. A future CSV adapter must identify its source, map physical columns
to unique IDs (even when headings repeat), and map finite `timestamp_ms` values
to nanoseconds with a documented rounding policy and origin. Relative milliseconds
must remain relative, without subtracting the first row or claiming UTC. If clock
meaning is absent, use `unspecified`; missing/non-finite timestamps cannot become
valid samples. The existing UI's elapsed-time plot is a presentation operation.

An available sample requires a non-null value; zero and false remain real values.
An unavailable sample requires `value=None`, `available=False`; `quality` can
preserve a reason/status. Both `available=True,value=None` and
`available=False,value=0` are rejected, including during deserialization. Adapters
must map wire missingness to this contract explicitly. Descriptor availability
describes a source/channel, requires a reason when false, and does not rewrite
historical samples. No protocol codec is imported by this model.

## Serialization and compatibility

`SignalSchema.to_json/from_json` serialize a versioned envelope containing
`schema_version`, `signals`, and optional `metadata`. Versions are positive
integers or nonempty strings; labels are preserved rather than coerced. Future
labels are accepted only through this same envelope and validation rules; this
does not claim to interpret incompatible future formats. Descriptors, samples,
availability and series have `to_dict/from_dict` methods. Encode sample/series
dictionaries with standard `json.dumps` when needed.

Unknown fields are preserved at every envelope level by default;
`allow_unknown=False` rejects them recursively. Unknown types remain opaque and
are marked by `is_known_type=False`. Consumers must avoid rendering or calculating
with unsupported types. Metadata keys are preserved verbatim, including spaces
and empty strings. Explicit `extra` mappings cannot overwrite known fields.
Schema JSON rejects duplicate object keys; all model payloads reject non-string
keys, non-finite numbers, unsupported Python objects, cycles, and nesting deeper
than 64 levels. The serializer returns detached mutable containers; the model
snapshots nested dictionaries/arrays into immutable mappings/tuples.

`tests/fixtures/generic_signals.json` demonstrates multiple independent sources,
unrelated scalar/vector/event/status signals, duplicate display names, unavailable
channels and an unknown future type. It requires no hardware or framing contract.
