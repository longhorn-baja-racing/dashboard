# Issue 26 local verification

Recorded 2026-11-01. Independent branch from current `main`, commit
`cb172141c06183bdc84fdc70738a756502ff4fec`; no dependency on the open product PRs.
The smaller test count below is main's suite, not the 688-test diagnostics stack.

## Completed locally on Windows

Environment: Python 3.12.14, uv 0.12.23, PySide6/Qt 6.11.2, Ruff 0.16.5,
Pyright 1.1.411. Python selection was explicitly overridden to the available
3.12 runtime; the workflow retains Python 3.13 for Windows/macOS/Linux.

Executed the actual documented shared runner, including every stage:

```sh
uv run --locked python scripts/check_quality.py
```

| Check | Observed result |
| --- | --- |
| Locked sync and dependency consistency | Passed |
| Ruff lint and formatting | Passed |
| Pyright | Zero errors/warnings |
| Full current-main regression suite | 24 passed; 97.87% branch-aware coverage |
| Clean test control / deliberately failing assertion | Exit 0 / exit 1; assertion marker found |
| Clean format control / malformed formatting | Exit 0 / exit 1; reformat diagnostic found |
| Clean typing control / incompatible annotated assignment | Exit 0 / exit 1; reportAssignmentType found |
| Clean lock check / unsynchronized direct dependency | Exit 0 / exit 1; specific stale-lock diagnostic found |
| Wheel and source distribution build | Passed |
| Isolated wheel install/import/CSV/plot/entry point | Passed |
| Isolated sdist install/import/CSV/plot/entry point | Passed |

The installed-package smoke uses isolated mode outside the checkout, and launches
the real application event loop offscreen. It requires no hardware or interactive
desktop. No new application buttons or visible feature window belong to issue 26.

The four clean/broken checker outputs and machine-readable results are generated
under `test-results/gates`. They are included in the existing CI artifact upload.
All generated disposable projects were removed after the successful run.

## Verification of the verifier

A separate disposable copy changed the formatter command to `python -c pass`,
making the formatter incorrectly accept every file. The gate verifier rejected
this false green: its format case saw broken exit 0 and recorded `passed: false`.
The verifier returned exit 1. Running that same broken copy through the shared
`check_quality.py --stage gates` also returned exit 1. The working source was
unchanged by this experiment.

During development the verifier also correctly rejected an offline dependency
resolution failure, rather than counting any nonzero exit as a stale-lock success.
The final lock case allows normal resolver metadata access and requires uv's
specific stale-lock diagnostic. Its observed failure code with pinned uv is 1.

## Server enforcement and remaining remote verification

Read-only GitHub API inspection confirmed ruleset 25523260 already enforces
`CI required` with strict up-to-date checks on main, two approvals, approval after
the latest push, stale-review dismissal, resolved threads, and no force pushes or
deletion. No protection or review settings were changed. Account admin access is
not available, and no settings change was needed.

The current main baseline has six successful platform/Python jobs plus the
aggregate in [run 40357833466](https://github.com/longhorn-baja-racing/dashboard/actions/runs/40357833466).
This is historical baseline evidence. The new negative controls have only been
run locally on Windows so far. Fresh Windows 3.13/macOS/Linux results and the
updated aggregate must be checked after the user approves publication of this
branch. The issue should not be closed on local evidence alone.

See [quality.md](quality.md) for matching commands, scope and merge rules.
