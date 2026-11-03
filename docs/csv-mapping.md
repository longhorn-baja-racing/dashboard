# Importing arbitrary CSV files with a column mapping

Issue #31. A CSV file has no schema: nothing says which column is time, what
unit that time is in, or what the other columns mean. The dashboard therefore
never guesses silently. It proposes a mapping, lists everything the user
should confirm, and applies only a mapping the user has accepted.

This is the Qt-free part: the mapping model (`core/csv_mapping.py`) and the
reader (`importers/mapped_csv.py`). The mapping dialog and the hand-off to the
generic session model (#18/#21) come next, after #129's import states.

## The mapping

```python
CsvMapping(
    time_column=0,  # index of the time column
    time_unit="ms",  # s, ms, us, ns or iso8601; never guessed
    signals=(
        ColumnMapping(column=1, name="signal_a", unit="V", kind=SignalKind.NUMBER),
        ColumnMapping(column=4, name="flag", kind=SignalKind.BOOLEAN),
    ),
    encoding="utf-8",  # e.g. "cp1252" for files saved by older Windows tools
    delimiter=",",  # one character, e.g. ";" for European spreadsheets
)
```

`mapping.problems(headers)` lists, in words a user can act on, why a mapping
cannot be applied: no time column, no or unknown time unit, a column used as
both time and signal, an empty or repeated signal name, a column that does not
exist, or a bad delimiter. The importer refuses such a mapping with the same
messages.

Only mapped columns become signals. Signal names and units are whatever the
mapping says; nothing depends on particular column names.

## Suggestions

`suggest_for_file(path)` (or `suggest_mapping(headers, rows)`) reads the header
and the first 200 rows and returns a `Suggestion` with a mapping and a list of
`ambiguities` to show the user.

- **Time column:** a header named like time (`t`, `time`, `timestamp`,
  `elapsed`, `gps_time`, `time_utc`, ...), or a column whose values are all
  ISO 8601 date-times. When several match, the first one with a known unit is
  used and the others are named in an ambiguity. When none match, the time
  column is left unset.
- **Time unit:** only taken from the header, either in brackets (`time (s)`,
  `t [us]`, `elapsed [milliseconds]`) or as a suffix (`timestamp_ms`,
  `time_ns`), or from ISO 8601 values, which describe themselves. Otherwise it
  is left empty with "The unit of 'time' is not in its header. Choose s, ms, us
  or ns." `timestamp_ms` and `Timestamp_ms` are treated the same.
- **Signal units:** only read from brackets, so `speed (km/h)` becomes signal
  `speed` in `km/h`. Suffixes such as `_rpm` stay part of the name, because
  they are not reliably units.
- **Types:** a column is a number when at least 80% of its non-empty cells are
  numbers. The other cells are named in an ambiguity and will import as
  missing. A column is boolean when every cell is `true`/`false`, `yes`/`no`
  or `on`/`off` (`0` and `1` alone stay numeric). Anything else is text.
- **Names:** repeated or empty headers get a unique name (`v_3`,
  `column_4`), and each rename is listed.

## Applying a mapping

`MappedCsvImporter().import_session(path, mapping)` returns a
`MappedSession`. The file is only read, never modified.

- **Times** become exact integer nanoseconds. Decimal times are converted
  without floating-point rounding (`1.000000001` s is 1,000,000,001 ns).
  ISO 8601 times keep their fraction (up to nanoseconds) and time zone. Times
  without a zone are read as UTC, and the session notes say so.
- **Missing stays missing.** Empty cells and `NA`, `N/A`, `NaN`, `null`,
  `none` and `-` are missing. A missing cell produces no sample, never a zero.
  Each signal keeps only the rows where it has a value, so in a mixed-rate file
  a slow signal gets its own sparse timeline. `MappedSignal.missing` counts
  the missing cells.
- **Invalid cells** (`oops` in a number column, `maybe` in a boolean column,
  `inf`) are kept as missing, counted in `MappedSignal.invalid` and reported.
- **Rows without a valid time** are skipped and reported.
- **Rows with too few or too many cells** are reported. Missing trailing cells
  are missing values. Extra cells are not imported.
- **Out-of-order rows** are counted and each signal is sorted by time (stable,
  so equal times keep their file order). **Repeated times** are counted and
  kept.
- **Problems** carry the line where the row starts (a quoted cell can span
  lines) and the column header. The first 100 are listed; the rest are
  counted in `problems_not_listed`. `summary()` gives one line such as "5
  signals from 10 rows, 1 row skipped, 4 problems, 1 row out of time order, 1
  repeated time".
- **Text handling:** standard CSV quoting (delimiters, quotes and line breaks
  inside quoted cells), a UTF-8 byte order mark, CRLF or LF line endings, and
  any delimiter. A file that is not valid in the chosen encoding fails with
  the line and how to fix it.
- **Preview and cancellation:** `max_rows=N` reads only the first N rows and
  marks the result `truncated`. `cancel=CancellationToken()` is checked every
  1,000 rows and raises `CancellationRequested`, so the import can run in a
  `BackgroundService` worker.

Reading 100,000 rows x 16 columns takes about 0.8 s on a development laptop.

## Tests

`tests/importers/test_mapped_csv.py` covers the legacy example
`sample_data/sample_baja_log.csv` (time in ms from `timestamp_ms`, known times
and values), and `tests/fixtures/csv_mapping/mixed_rate.csv`, whose README
lists what each row exercises: quoted cells, an invalid number, `NaN`, a row
without time, an out-of-order row, a repeated time, short and long rows, a
sparse slow signal, booleans and text. Further tests cover a BOM with CRLF,
cp1252, semicolons, ISO times, previews, cancellation, the problem cap, every
mapping error and unreadable files. They also check that the file's hash is
unchanged after import.
