# Sample data

All files here are synthetic. Column names, units and values are placeholders chosen so
nothing depends on a particular vehicle, sensor or hardware choice. They are not
requirements for any real or planned hardware.

## Valid fixtures (`fixtures/`)

| File | Time column | Channels |
|---|---|---|
| `env_temp_pressure.csv` | `t_s` (seconds since start) | `coolant_temp_C` (°C), `line_pressure_kPa` (kPa) |
| `motion_accel_gyro.csv` | `ts_ms` (milliseconds since start) | `ax` (m/s²), `gz` (deg/s) |

Timestamps are strictly increasing and deliberately unevenly spaced. The importer does not
identify a time column; "time column" is a convention of these fixtures.

`motion_accel_gyro.mapping.json` is a sample of unit/label metadata for the motion fixture.
No importer reads it yet; a test keeps it consistent with the CSV.

### Expected values (as parsed by `CsvImporter`; blanks are `None`)

| File | Column | Values |
|---|---|---|
| env_temp_pressure | `t_s` | 0.0, 0.1, 0.35, 0.6, 1.2, 2.0 |
| env_temp_pressure | `coolant_temp_C` | 25.0, 25.5, None, 26.5, 27.0, 28.0 |
| env_temp_pressure | `line_pressure_kPa` | 101.3, 101.4, 101.9, 102.2, None, 103.0 |
| motion_accel_gyro | `ts_ms` | 0, 5, 12, 30, 41 |
| motion_accel_gyro | `ax` | 0.0, 0.1, 0.25, -0.05, 0.0 |
| motion_accel_gyro | `gz` | 0.0, 1.5, 3.0, None, 4.5 |

Intentional blanks: `coolant_temp_C` at t=0.35 s, `line_pressure_kPa` at t=1.2 s,
`gz` at ts_ms=30. A plot should show gaps there, not zeros.

## Malformed fixtures (`fixtures/malformed/`)

| File | Content | Current importer behavior |
|---|---|---|
| `error_empty.csv` | zero bytes | empty session; `LogSessionStore.load` returns `False` |
| `error_bad_encoding.csv` | invalid UTF-8 bytes | raises `UnicodeDecodeError` |
| `blank_timestamp_cell.csv` | blank timestamp in row 2 | loads; the blank becomes `None` |

User-facing handling of these is tracked in #103.

## Legacy (`legacy/`)

See `legacy/README.md`.

# Legacy sample

`sample_baja_log.csv` is a fictional sample kept only for backward compatibility with
existing tests and demos. It does not describe the data format of any real or planned
hardware. Don't build new code or fixtures on its column names; use `../fixtures/`.