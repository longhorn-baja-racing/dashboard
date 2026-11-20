# Live USB serial source (firmware protocol v4)

**USB serial (firmware live)** in the source picker shows what the Central
Controller streams on its USB cable while it runs. The adapter is read-only:
it opens the port for reading, never writes a byte and sends no commands
(not even the firmware's `STAT`, `LIST` or `DUMP` host commands).

The format is the firmware's `docs/protocol.md` ("Transport packets", protocol
version 4, branch `esp32-development`) and is pinned by its golden capture
`dev/tests/fixtures/stream_usb.bin`, copied to `tests/fixtures/lbr/` with the
firmware's expected decode.

## Use it

1. Connect the Central's USB cable. On Windows a COM port appears once the
   board's USB-UART driver (CP210x or CH340, depending on the board) is
   installed.
2. In **Telemetry source** choose **USB serial (firmware live)**. A port row
   appears with the ports the operating system lists (USB ports first, with
   their description). Click **Refresh ports** after plugging in. With no port
   the row says why and **Open CSV / Connect** stays disabled.
3. Choose the port and click **Open CSV / Connect**. Preparation opens the
   port for about a second to learn the names of the car logic outputs (the
   Central re-sends them every second), closes it, and then the live source
   opens it again. A missing or busy port is reported here
   ("Could not open source: COM3: access denied ...").
4. Readings arrive in the signal browser, plot and table like any other
   source. The status line adds the link counters, for example
   `COM3: 1234 packets, 0 lost, 1 rejected frames`.
5. **Disconnect** releases the port. Readings stay for inspection. **Retry**
   connects again in a new session.

To keep what arrives, use **Record**; see [Recording a live source](live-capture.md).

## Port settings

- 921600 baud (the firmware's `CONFIG_LBR_USB_BAUD` default), 8 data bits, no
  parity, one stop bit, no flow control.
- Opened read-only through QtSerialPort (part of PySide6, so no extra
  dependency). DTR and RTS are deasserted right after opening so the ESP32's
  auto-reset circuit is not triggered. On some drivers the lines can still
  glitch while the port opens; the manual check below covers this.
- Port access sits behind a three-method interface (`open`, `read`, `close`,
  `importers/serial_source.py`); `serial_ports.py` is the Qt implementation.
  Tests use an in-memory serial line instead.

## What the adapter does with the bytes

- **Framing.** The stream is split at `0x00`. Each frame must be valid COBS, a
  packet of version 1, with a matching CRC-32 and record framing and the right
  record count. Anything else (boot ROM text, panic output, line noise, a frame
  cut by a reset) is a *rejected frame*: counted, reported as a recoverable
  `decode_error` source event with the reason, and skipped. Decoding resumes at
  the next delimiter. A run of more than 1,030 bytes without a delimiter is
  discarded and counted once, so the buffer stays bounded. Partial frames are
  kept across reads.
- **Sequence.** Every packet carries a 32-bit per-link sequence number. A gap is
  counted as lost packets (a corrupted packet is both a rejected frame and a
  lost packet); duplicates are counted; the counter wraps without loss. If the
  sequence and the packet time both jump backwards, the device restarted: the
  session ends as failed with `device_restarted` (its clock started over), and
  Retry starts a new one.
- **Records.** The records are the `.lbrlog` record types, so they go through the
  same decoder and catalog as a firmware log (`importers/lbrlog.py`, see
  [lbrlog.md](lbrlog.md)). A reading has the same signal ID, device time,
  value, unit and unavailable reason live and in a log, for example
  `driver_input.brake_pedal`, `powertrain.engine_rpm.node-clock`,
  `corner_br.heartbeat.err_flags`, `central.output.vehicle.speedkph` and
  `central.text` (firmware log lines).
- **Time.** Sample times are the device's: microseconds on the Central's clock
  (`lbr_central_us`), or a node's own clock before it synchronized
  (`lbr_node_clock.<node>`, separate `.node-clock` signals). Arrival time is
  used only for link freshness.
- **Link signals.** `usb_link.packet_seq` (every packet's sequence number at its
  build time), `usb_link.lost_packets` and `usb_link.rejected_frames` (running
  totals, published when they change) make the link itself plottable.
- **Health.** Each node heartbeat in the stream publishes a source HEALTH event
  (at most two per second). The source declares
  `health_config = SourceHealthConfig(sequence_bits=32, heartbeat_timeout_ns=3 s,
  reports_decode_errors=True)` and puts the packet sequence number in every
  sample's `extra["sequence"]`, which is what the source health monitor reads.
  The status line says `no packets for N s - data is stale` after two seconds
  without a packet, and `port closed ... data is stale` after a disconnect.
- **Catalog check.** Health page 2 carries each node's catalog hash. A node that
  reports a different catalog than the bundled one is reported once
  (`catalog_mismatch`) because its names and values may be wrong.

### The signal list is fixed when connecting

The session schema is fixed when the source starts, so it is built from the
catalog: every CAN frame field on the shared clock and (for nodes other than
the Central) on the node clock, events and faults, heartbeat state and flags,
every health page field, raw/unknown frames, command frames and text, plus the
car logic outputs heard while preparing. A reading of anything else (an output
that first appears later, an unknown signal ID) is counted, reported once as
`channel_not_shown` ("click Retry to include it") and not shown under a guessed
name. Recordings keep those records, and the replay shows them.

## Bounds and threads

- The port is read on the source worker in reads of at most 4 KiB with a 50 ms
  timeout, so Disconnect and window close take effect within about 50 ms.
- Decoded events go to a bounded queue of 16,384 events (about two seconds of a
  busy bus). If the app falls behind, readings are dropped instead of blocking
  the reader, and the drop count shows in the status line (`lost records`).
- The decoder's per-channel buffers are emptied after every packet. Live
  history in the session is limited to 1,000 readings per signal.
- Decode error events are limited to ten per second; the rest are summarized
  once a second ("N more frames ... could not be decoded") and all are counted
  in the link counters.

## Failure behavior

| Situation | What happens |
| --- | --- |
| Port missing, busy or access denied | Preparation fails with the port name and reason; the previous readings stay. |
| Cable unplugged while connected | The run ends as failed (`serial_disconnected`), the port is released, the status says the data is stale and how to reconnect. Any recording is finalized. |
| Device stops sending | The source stays connected; the status counts the seconds since the last packet. |
| Device restarts | The run ends as failed (`device_restarted`); Retry starts a new session on the new clock. |
| Corrupt bytes | Rejected frames are counted and reported; decoding resumes at the next frame. |
| App falls behind | Readings are dropped at the queue, counted as lost records; the port keeps being read. |

## Tests

- `tests/importers/test_lbr_stream.py`: COBS, packet checks, the golden capture
  against the firmware's expected decode in any chunking (down to one byte),
  corruption and resynchronization, bounded oversize frames, sequence steps.
- `tests/source_contract/test_serial_conformance.py`: the source contract on an
  in-memory serial line: firmware values from the golden capture, partial
  reads with corruption and lost packets, unplug and reconnect, open failure,
  repeated open/close, a stalled consumer, device restart, outputs and the
  hidden-channel report, catalog mismatch, error rate limiting, heartbeat
  health and staleness, every channel of the firmware fixtures declared, and a
  check that the port is only ever opened, read and closed.
- `tests/ui/test_serial_ports.py`: the QtSerialPort implementation reads a POSIX
  pseudo-terminal opened read-only, writes nothing and releases it.
- `tests/ui/test_serial_source_ui.py`: the real window: port chooser, busy port,
  live readings, recording, unplug and replay of the recording.

## Manual check with the car (not done yet)

No Central was available while this was written, so this check is still open.
On Windows, with the Central running its esp32-development firmware:

1. Plug in the USB cable, start the dashboard, choose **USB serial (firmware
   live)**, click **Refresh ports** and check that the Central's COM port is
   listed with its USB description.
2. Connect. Check that the Central did **not** reset: its uptime
   (`central.health.uptime_ms` if the Central sends health pages, or the
   firmware log in `central.text`) keeps counting and no `rst:` boot banner
   appears as a rejected frame.
3. Check that pedal, engine and IMU signals update, that their units and values
   match the firmware's `lbr_decode.py serial COM3` output, and that
   `usb_link.lost_packets` stays at 0 at rest.
4. Unplug the cable: the status must say disconnected and stale within a
   second or two, and the COM port must be free (another program can open it).
   Plug it back in and click **Retry**.
5. Reset the Central with its button while connected: the session must end
   with "restarted" and Retry must start a new one.
6. With another program holding the port, Connect must fail with an access
   message instead of hanging.
