# Opening firmware session logs (.lbrlog)

The Central Controller writes one log per boot to its SD card as
`/sdcard/lbr/NNNNNNNN.lbrlog` (`.LBR` from older firmware, same format). The
dashboard reads these files directly; no export step is needed.

1. Copy the log from the SD card to the laptop. Keep the original card copy.
2. In the **Telemetry source** panel choose **Firmware log (.lbrlog)** and
   press **Open CSV / Connect**.
3. Select the file. The status line shows how much has been read
   (`Preparing ... - 40% read - Disconnect cancels.`). **Disconnect** stops
   the read and closes the file; the previous readings stay on screen.
4. When the replay starts, every channel appears in the signal browser grouped
   by node. Pick one to plot it or see its readings in the table.

A recording the dashboard made of the live USB stream
([live-capture.md](live-capture.md)) is an `.lbrlog` too and opens the same way;
its configuration lines start with `host_` and say it is not the SD card log.

To try it without a car, open the firmware's golden log
`dev/tests/fixtures/session.lbr` (the dashboard keeps it as hex text in
`tests/fixtures/lbr/session.lbr.hex`; its README shows how to write it out).

The format is `.lbrlog` log format version 1 of protocol version 4, defined
in the firmware's `docs/protocol.md` ("Log format") and pinned by its
`dev/tests/fixtures/session.lbr`. The reader is
`src/lbr_dashboard/importers/lbrlog.py` (stdlib and numpy only); the mapping
onto generic signals is `src/lbr_dashboard/importers/lbrlog_source.py`.

## What the reader does

- **Checks every record.** A record is `0xA5 0x5A`, type, flags (0), length,
  payload and a CRC-32 over type, flags, length and payload. Only records whose
  CRC matches are used.
- **Recovers from damage.** Bytes that are not a valid record (bit flips,
  garbage, a half-written record in the middle) are skipped up to the next
  valid record and reported as a corrupt region with its file offset and
  length. An incomplete record at the end of the file (power lost mid-write) is
  reported as a truncated tail with its offset. A record with a valid CRC but an
  impossible payload (wrong length, reserved bits set) is rejected and listed
  with its offset and reason. Record types this reader does not know are
  skipped by length and counted, as the spec requires.
- **Never writes the file.** The log is opened read-only and read once in 1 MiB
  chunks. Its SHA-256 is computed while reading and kept with the session
  metadata, so you can confirm later that it is the same file.
- **Decodes like the firmware's reference decoder.** Names, value types,
  units, events and every CAN frame layout come from the signal catalog. The
  Central's log holds the raw CAN frames; they are decoded with the frame
  table and the bus sync-tracking rules exactly as `dev/tools/lbr_decode.py`
  does, so every value and timestamp equals that tool's CSV export.
- **Refuses what it cannot read, with a reason.** An empty file, a file with no
  valid record, a log of another log format version or protocol version, or a
  log written with a catalog the dashboard does not have is refused with a
  message that says what to do. The previous source and readings stay intact.

### Signal catalog

The dashboard ships the catalog of the firmware it was tested with
(`catalog_hash` 580578432). A log names its catalog in its SESSION record. If
the log was written by firmware with a different catalog, put that firmware's
`catalog.json` (from `dev/tests/fixtures/` or the build) next to the log file
and open it again; a catalog whose content does not match its own hash is
ignored. A log whose SESSION record is damaged is decoded with the bundled
catalog and the status says the catalog was not checked.

## How log records become signals

Each channel becomes one generic signal (`SignalDescriptor`). IDs match the
lbr-csv view's channel IDs.

| Log content | Signal ID | Type | Value |
| --- | --- | --- | --- |
| sample (SAMPLE record or CAN data frame field) | `<node>.<signal>`, e.g. `driver_input.brake_pedal` | `numeric` (`f32`, `i32`, `u32`, unknown signals), `boolean`, `status` | the decoded value; `f32` as the exact single-precision value |
| sample taken before the node was synchronized | `<node>.<signal>.node-clock` | as above | as above, on the node's own clock |
| event / fault | `<node>.event`, `<node>.fault` | `event` | `{"code": "NODE_LOST", "argument": 5}` |
| heartbeat | `<node>.heartbeat.state`, `<node>.heartbeat.err_flags` | `status` | the state or flag bits; the flag names are the sample quality |
| health field (HEALTH record or CAN health page) | `<node>.health.<field>`, e.g. `central.health.uptime_ms` | `numeric` | the field |
| LOSS record | `<logger>.loss.dropped_records`, `.dropped_bytes` | `numeric` | the counts |
| TEXT record | `<logger>.text` | `string` | the firmware log line |
| car logic output (KEY/VALUE) | `<logger>.output.<group>.<name>`, e.g. `central.output.cvt.ratio` | `numeric` or `boolean` | the output value |
| command frame | `<node>.command` | `string` | command name and argument bytes |
| unknown or malformed CAN frame | `<node>.can_raw` | `string` | id and data bytes (hex) |

- **Time.** Each reading carries the measurement time the log gives for it,
  in integer nanoseconds (microseconds x 1000, no float rounding): the
  sample's own time, the grid time of a CAN data frame (not its receive
  time), the unwrapped event time. Heartbeats, health pages, commands and raw
  frames use the frame's receive time; HEALTH and TEXT records, which carry
  no time, use the time of the latest record before them.
- **Clocks.** Readings on the Central's shared clock use the timestamp
  domain `lbr_central_us` (microseconds since Central boot). Readings taken
  before a node was synchronized use `lbr_node_clock.<node>` and are separate
  signals, so they are never drawn on the same axis.
- **Missing stays missing.** An unavailable reading (`NOT_FITTED`,
  `SENSOR_FAULT`, `NO_DATA`, an output `STALE`, ...) is an unavailable sample
  with its reason as quality, never zero. A valid NaN or infinite reading
  cannot be held by the generic sample model; it becomes unavailable with
  quality `non_finite` and the reading (`nan`, `inf`) in the sample's extra
  fields.
- **Units and metadata.** The descriptor has the catalog unit (`deg*1e-7`,
  `m/s^2`, ...), description, node as device and the frame rate as nominal
  rate. The schema metadata holds the SESSION record (versions, boot count,
  reset reason, configuration) and the full integrity report.
- **Problems are reported once.** If anything was skipped, rejected or lost
  (corrupt regions, truncated tail, rejected records, LOSS records, no END
  record, unknown record types, unchecked catalog), the replay starts with a
  recoverable source error that lists it, with offsets. It stays in the source
  status line (for example `Log read with problems: ignored a 9-byte
  incomplete record at the end (offset 688); the logger dropped 5 record(s)
  (150 bytes) while writing.`).

Firmware update frames, time sync frames and car logic replay ticks are
counted in the integrity report, not turned into signals.

## Limits

- The reader decodes about 100,000 CAN frames per second on a development
  laptop (a 6 MB log of 200,000 frames in about 2 s) and keeps decoded values in
  compact arrays.
- The replay source holds one object per reading. It accepts logs with up to
  500,000 readings (about two minutes of a full Central log) and refuses longer
  ones with a message. For those, export the log with the firmware's
  `python3 dev/tools/lbr_decode.py csv FILE.lbrlog` and open the CSV with
  **File > Open** ([lbr-csv session view](lbr-csv.md)).
- Sequence numbers are kept by the reader but not shown in the generic table.

## Tests

- `tests/importers/test_lbrlog.py`: the golden log equals the firmware's
  expected decode (`session.json`) bit for bit and its CSV export
  (`session.csv`) value for value; the CAN capture equals
  `can_capture.json`; bit flips, garbage, truncation at every kind of position
  and 200 randomly damaged copies are recovered with every byte accounted
  for; version, catalog and non-log refusals; output, tick, unknown and
  rejected records; progress, cancellation (the file is closed) and a large
  log; the file is unchanged after reading.
- `tests/source_contract/test_lbrlog_conformance.py`: the replay source passes
  the shared source contract (descriptor and record fidelity written from
  `session.json`, repeat open/close, lossless backpressure, stop and
  cancellation, unsupported controls) and reports damage before replaying
  every good record.
- `tests/ui/test_lbrlog_source.py`: the picker entry opens a log through the
  file dialog, plots it and shows the damage report; an unsupported version is
  refused without losing the previous readings; progress is shown and
  Disconnect cancels the read.

The fixtures and where they came from are listed in
`tests/fixtures/lbr/README.md`.

## Windows check

On a Windows laptop, open a real SD card log and the golden `session.lbr`
with the steps above, check the file's modified time is unchanged afterwards,
and press Disconnect during a large read to confirm the file can be deleted
right away (the reader released it).
