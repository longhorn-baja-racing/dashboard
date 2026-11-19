# CSV column mapping fixtures

Generic files for `tests/importers/test_mapped_csv.py` (issue #31). Column
names are placeholders, not real sensors.

`mixed_rate.csv` covers, row by row (CRLF and BOM variants are made by the test):

| Line | Time (s) | What it tests |
| --- | --- | --- |
| 2-4 | 0.000-0.020 | normal rows; `slow_signal` only every few rows (mixed rate), `NaN` as missing |
| 5-6 | 0.030 | invalid number `oops` kept as missing; quoted note with a line break |
| 7 | (empty) | row without time, skipped and reported |
| 8 | 0.025 | out of time order, sorted into place |
| 9-10 | 0.040 | the same time twice (reported, both kept) |
| 11 | 0.050 | too few cells (`flag`, `note` missing) |
| 12 | 0.060 | too many cells (`extra,cells` beyond the header) |

Expected results are asserted in the test, so a change in behavior shows up
as a failing test rather than a silent difference.
