# Firmware lbr-csv fixture

`session.csv` is copied byte for byte from the firmware repository, so the
dashboard reads exactly what the firmware's reference decoder writes.

- Source: `longhorn-baja-racing/firmware`, `dev/tests/fixtures/session.csv`
- Commit: `c747d3dc2e4cba154a9781702ce4b6c7eca6d1d6` (branch `esp32-development`,
  "tools: export session logs to CSV")
- Format: lbr-csv version 1, protocol version 4 (firmware `docs/protocol.md`,
  "CSV export")
- sha256: `3123ec520eee382524ab2218910aa79e2705e00f3864268639f96b8087faea4e`

It is the CSV export of the firmware's `session.lbr` golden log and covers
synced and unsynced samples, an unavailable (`NOT_FITTED`) sample, a fault,
heartbeats with error flags, health fields, a loss record, a text record and
the integrity rows, including a truncated tail.

To update it after the firmware fixtures change:

```sh
cp ../firmware/dev/tests/fixtures/session.csv tests/fixtures/lbr/session.csv
sha256sum tests/fixtures/lbr/session.csv
```

Then update the commit and checksum above and the expectations in
`tests/importers/test_lbr_csv.py`. `.gitattributes` keeps the file's line
endings unchanged on every platform.
