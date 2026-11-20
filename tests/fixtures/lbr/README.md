# Firmware session fixtures

These files are copied byte for byte from the firmware repository, so the
dashboard reads exactly what the firmware writes and what its reference
decoder (`dev/tools/lbr_decode.py`) says about it.

- Source: `longhorn-baja-racing/firmware`, `dev/tests/fixtures/`
- Commit: `2715e6c0a0cf6df444ae0b51dd46a56d2d9db15b` (branch `esp32-development`,
  "replay: re-run the car logic on a recorded log, bit for bit"), the last
  commit that changed these fixtures
- Format: protocol version 4, `.lbrlog` log format version 1 and lbr-csv
  version 1 (firmware `docs/protocol.md`, "Log format" and "CSV export")

| File | What it is | sha256 |
| --- | --- | --- |
| `session.lbr.hex` | golden session log (`.lbrlog` format), as hex | `29933a8e14328389054d2cb72f7e0237ce9c87e58dce9ca04ac114e62d694a27` |
| `session.json` | the firmware's expected decode of `session.lbr`, record by record | `ba1712dec690feecf0de7307c7600e102bde884a2227ac8d72f86078c19a77b5` |
| `session.csv` | the reference decoder's lbr-csv export of `session.lbr` | `555eca81fe69fc92a006e23787a702cebd628fefcb0b88fa199b7874831f1202` |
| `can_capture.bin.hex` | 27 raw CAN frame records (22 bytes each), as hex | `d3f7f88ff452a93054e9064ae539d9d5c5a290c2c5feca8c3d12abe144a78fff` |
| `can_capture.json` | the firmware's expected decode of `can_capture.bin` | `ba0e0e18cb7a8e1a173616500fd7d17fe716ba51b40084555acfd073c950f236` |
| `catalog.json` | the signal catalog these were written with (hash 580578432) | `dc9c368ae09e555669412f6ab97dafbf082b3ad789cdefae77029172add83356` |
| `stream_usb.bin.hex` | a captured USB live stream: boot ROM text, then two COBS-framed transport packets, as hex | `ffccd937c65006dd22863f256228f17bde595ad0528ed7d2cf2b7adb25bb5473` |
| `stream_usb.json` | the firmware's expected decode of `stream_usb.bin` (packets and `bad_frames`) | `cdbb22ae9521e4fd8f3cbff4929e9432f566aafbc84ce818f938be0907afdafb` |

The USB stream fixture was last changed in firmware commit
`f6f5c9b05ce071a0a25c49bbfeed2127f651dc4f` ("daq: put the four wheel hub nodes on
CAN") and matches the same catalog; the live serial source
(`importers/serial_source.py`, `docs/serial-source.md`) is tested against it.

The binary files are stored as lowercase hex text, 32 bytes per line, so
every file here is plain text; `bytes.fromhex(path.read_text())` gives back the
firmware's bytes, and the checksums above are of those bytes (tests check
them). To get a real `session.lbr` to open in the dashboard:

```sh
python -c "import pathlib as p; p.Path('session.lbr').write_bytes(bytes.fromhex(p.Path('tests/fixtures/lbr/session.lbr.hex').read_text()))"
```

The session covers synced and unsynced samples, an unavailable (`NOT_FITTED`)
sample, a fault, heartbeats with error flags, a health record, a loss record, a
text record and a truncated tail. The CAN capture adds every frame kind the
Central logs: data frames with and without counters, unavailable fields,
events, time sync, commands, health pages, firmware update frames and an
extended frame.

`src/lbr_dashboard/importers/lbr_catalog_data.py` embeds `catalog.json` so the
packaged app needs no data files; a test keeps the two equal.

To update after the firmware fixtures change (from the dashboard checkout,
with the firmware checked out next to it):

```sh
for f in session.json session.csv can_capture.json catalog.json stream_usb.json; do
    cp ../firmware/dev/tests/fixtures/$f tests/fixtures/lbr/$f
done
for f in session.lbr can_capture.bin stream_usb.bin; do
    python -c "import sys; h = open(sys.argv[1], 'rb').read().hex(); print(*(h[i:i + 64] for i in range(0, len(h), 64)), sep='\n')" \
        ../firmware/dev/tests/fixtures/$f > tests/fixtures/lbr/$f.hex
done
sha256sum tests/fixtures/lbr/*.json tests/fixtures/lbr/*.csv
```

Then regenerate `lbr_catalog_data.py` from `catalog.json` (paste it between the
quotes of `CATALOG_JSON`), update the commit and checksums above and the
expectations in `tests/importers/test_lbr_csv.py` and
`tests/importers/test_lbrlog.py`. `.gitattributes` keeps every byte unchanged
on every platform.
