# Sample data

All files here are synthetic. Column names, units and values are placeholders chosen so
nothing depends on a particular vehicle, sensor or hardware choice. They are not
requirements for any real or planned hardware.

## Time columns

The app treats a column named `timestamp_ms` (milliseconds since start) as time and
normalizes it to start at zero. Files without that column plot against sample index. The
fixtures cover both cases: `motion_accel_gyro.csv` uses `timestamp_ms`, while
`env_temp_pressure.csv` has a `t_s` column (seconds) that is just an ordinary column name
to the app. Timestamps in the valid fixtures are strictly increasing and unevenly spaced.

## Valid fixtures (`fixtures/`)

| File | Time column | Channels |
|---|---|---|
| `env_temp_pressure.csv` | `t_s` (s), not treated as time by the app | `coolant_temp_C` (°C), `line_pressure_kPa` (kPa) |
| `motion_accel_gyro.csv` | `timestamp_ms` (ms) | `ax` (m/s²), `gz` (deg/s) |

### Expected values (as parsed by `CsvImporter`; blank cells are `None`)

| File | Column | Values |
|---|---|---|
| env_temp_pressure | `t_s` | 0.0, 0.1, 0.35, 0.6, 1.2, 2.0 |
| env_temp_pressure | `coolant_temp_C` | 25.0, 25.5, None, 26.5, 27.0, 28.0 |
| env_temp_pressure | `line_pressure_kPa` | 101.3, 101.4, 101.9, 102.2, None, 103.0 |
| motion_accel_gyro | `timestamp_ms` | 0, 5, 12, 30, 41 |
| motion_accel_gyro | `ax` | 0.0, 0.1, 0.25, -0.05, 0.0 |
| motion_accel_gyro | `gz` | 0.0, 1.5, 3.0, None, 4.5 |

Intentional blanks: `coolant_temp_C` at `t_s` = 0.35, `line_pressure_kPa` at `t_s` = 1.2,
and `gz` at `timestamp_ms` = 30. The model keeps them as `None`. The current plot connects
across them rather than drawing a gap.

## Malformed fixtures (`fixtures/malformed/`)

These are inputs for import-error handling (#103). Tests pin the importer's current
behavior and should be updated when that changes.

| File | Content | Current behavior |
|---|---|---|
| `error_empty.csv` | zero bytes | empty session; `LogSessionStore.load` returns `False` |
| `error_bad_encoding.csv` | invalid UTF-8 bytes | `CsvImporter` raises `UnicodeDecodeError` |
| `blank_timestamp_cell.csv` | blank `timestamp_ms` in row 2 | importer loads it with the blank as `None`; time validation happens in the controller (#129) |

## Legacy (`legacy/`)

See `legacy/README.md`.