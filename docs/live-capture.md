# Recording a live source for later replay

While the [USB serial source](serial-source.md) is connected, **Record** (in the
source picker, next to **Retry / Restart**) saves what the dashboard receives to an `.lbrlog` file.
Open it later with **Firmware log (.lbrlog)**: it replays through the same
reader as an SD card log, with the same signal IDs, device times, values and
unavailable reasons the live view showed.

A recording holds what the live link delivered. It is not the SD card log and
does not replace it: the link can lose packets the SD card kept, and the file
says so (below).

## Use it

1. Connect **USB serial (firmware live)**. **Record** becomes available.
2. Click **Record** and choose a file name (the suggestion is
   `live-YYYYMMDD-HHMMSS.lbrlog` in your home folder; `.lbrlog` is added if
   missing). An existing file is never overwritten: choose another name.
3. A line under the buttons shows the file name, its size, the elapsed
   time, the span of device time recorded, the number of records and whether
   any loss was marked, for example
   `Recording to live-20261120-141502.lbrlog: 1.4 MB, 2:31 elapsed, 2:30 of device time, 48,210 records; no loss.`
4. Click **Stop recording**. The line then says where the file was saved and
   how to open it.

Recording also stops, and the file is finalized, when the source disconnects
(cable unplugged, Disconnect, Retry, choosing another source) and when the
window closes.

## File format

The file is a firmware log (format version 1, protocol version 4,
`docs/protocol.md` "Log format") written by the dashboard:

| Record | Contents |
| --- | --- |
| `SESSION` | Format and protocol version, the catalog hash the stream was decoded with, the Central as logger, the device time of the first recorded packet as start time. Firmware version, reset reason and boot count are not on the link and are 0. The configuration text has `host_*` lines: `host_capture=usb_serial_live_stream`, `host_port`, `host_started_utc`, `host_app` and `host_note=received over the live link; not the SD card log`. |
| carried records | When recording starts mid-stream: the latest heartbeat frame of each node and the latest `KEY` record of each car logic output received before, so the replay knows which nodes are synchronized and what the outputs are called. They keep their original bytes and times. |
| received records | Every record of every received packet, byte for byte and in arrival order (`CAN_FRAME`, `TEXT`, `KEY`, `VALUE`, and any other type; the reader skips types it does not know and counts them). |
| `LOSS` + `TEXT` | Written right before the next record after a loss: `dropped_records` counts lost packets (each held at least one record) plus records the recorder dropped, `dropped_bytes` the rejected frame bytes plus dropped record bytes. The following `TEXT` record starts with `host:` and says which, e.g. `host: 1 packet(s) lost on the live link; 63 byte(s) of undecodable frames rejected.` |
| `END` | On a clean stop: the latest device time and the number of records including `END`. |

Packet headers (link sequence, packet time) are not records and are not
written; the `usb_link.*` signals are live-only. Losses are never filled in.

## Safety and bounds

- **Never blocks reception.** The source worker only queues each packet's
  records; a separate writer thread writes them. The queue holds at most 8 MiB.
  If the disk cannot keep up, packets are dropped from the recording, counted,
  and marked with a `LOSS` record; the live view is unaffected.
- **Incremental and crash-tolerant.** Records are appended and flushed after
  every batch, with an fsync at most once a second and on stop. If the app or
  the computer stops mid-recording, the file keeps everything up to the last
  flush; the reader replays it and reports "no END record (the log was not
  closed cleanly)" and, if the last record was cut short, the truncated tail.
- **Write failures.** If writing fails (disk full, removed drive), the
  recording stops with the reason in the recording line (for example
  `Recording failed: Writing x.lbrlog failed: No space left on device. Live view
  continues; ...`). Reception continues. The file keeps what was written.
- **Destination problems** (no such folder, no permission, file exists) are
  reported when you start recording; nothing is written.
- A recording stopped before any packet arrived leaves no file.

## Replay

Choose **Firmware log (.lbrlog)** and open the file. The replay status lists
anything lost (from the `LOSS` records) and the schema metadata shows the
`host_*` lines. The firmware's reference decoder reads the file too:
`python3 dev/tools/lbr_decode.py csv FILE.lbrlog` writes the lbr-csv export,
with the `host_*` lines as `config` rows and the losses as `loss` and `text`
rows.

The replay source keeps at most 500,000 readings in memory (about two minutes
of a busy bus, see [lbrlog.md](lbrlog.md)). For a longer recording, export it
with `lbr_decode.py csv` and open the CSV with **File > Open**.

## Tests

`tests/source_contract/test_serial_recording.py` records from the in-memory
serial source and checks:

- replay through the `.lbrlog` reader gives identical signal IDs, times, values,
  availability, quality, units and clock domains (and the same descriptors) as
  the live view, with a clean integrity report;
- a recording started mid-stream keeps synchronized clocks and output names;
- lost and corrupt packets become `LOSS` and `host:` records at the right place;
- a full disk stops the recording with the reason while reception continues;
- existing files and missing folders are refused; an interrupted recording
  (no `END`, cut last record) stays readable;
- a slow disk drops records from the bounded buffer with the exact count marked
  in the file; stopping before any packet writes nothing.

`tests/ui/test_serial_source_ui.py` records through the real window, unplugs
the fake cable (the recording is finalized and the port released), reopens
the file with **Firmware log (.lbrlog)** and compares the readings, and checks
that closing the window finalizes a running recording.

## Manual check (not done yet)

With the Central connected on Windows: record for a few minutes while driving
the pedals, stop, open the file with **Firmware log (.lbrlog)** and compare a
few signals with the live plot; then record again and unplug the cable
mid-recording and check the file opens and ends where the cable was pulled;
then record to a nearly full USB stick and check the failure message while the
live view keeps updating. Compare one recording with the SD card log of the
same run using the firmware's `lbr_decode.py` to see the link loss, if any.
