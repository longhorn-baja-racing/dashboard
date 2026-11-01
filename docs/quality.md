# Reproducible quality gates (issue 26)

Windows is the Tier 1 target. The existing macOS/Linux development checks remain
in the single `Quality` workflow. No hardware, vehicle connection, graphical
desktop, performance benchmark, or release publication is part of these gates.

## Matching local and CI commands

Use Python 3.10-3.13 and **uv 0.12.23**, the version pinned in the workflow.
`.python-version` selects Python 3.13. From the repository root:

```sh
uv sync --locked --extra dev
uv run --locked python scripts/check_quality.py
```

The shared runner executes locked synchronization, dependency consistency, Ruff
lint, Ruff format verification, Pyright, pytest with coverage/JUnit reports,
negative gate verification, package build, and isolated wheel/sdist installation
and smoke checks. It stops at the first nonzero exit and preserves that exit code.
Missing executables and timeouts fail explicitly. It forces Qt offscreen mode.
The optional `make quality` shortcut calls the same runner.

To reproduce a specific CI step, pass `--stage` with one of `sync`, `dependencies`,
`lint`, `format`, `types`, `tests`, `gates`, `build`, or `package`:

```sh
uv run --locked python scripts/check_quality.py --stage gates
```

CI keeps separate named steps so failures are easy to locate, but executes these
same stage definitions. Its first bootstrap `uv sync --locked --extra dev` installs
the tools needed to run the Python stages. `--stage package` expects distributions
from `--stage build`; `--stage gates` requires the locked development environment.
The runner does not install uv itself. Dependency edits must update `uv.lock`.

## Negative controls

`scripts/check_quality_gates.py` copies the current source, tests, sample CSV and
project configuration into a disposable directory under `test-results/gates`.
It uses the installed locked tools and the same test/format/type command
definitions as the quality runner. The only copied Pyright setting changed is
the environment location, so the isolated source uses the existing dependencies.

Each gate must first accept the clean copy, then reject its deliberate defect
with the expected exit code **and a specific diagnostic**:

| Gate | Deliberate defect | Expected rejection |
| --- | --- | --- |
| Tests | An additional failing assertion with a unique marker | pytest exit 1 and the marker |
| Format | An unformatted Python file under `src` | Ruff exit 1 and a reformat diagnostic |
| Types | A string assigned to a declared integer under `src` | Pyright exit 1 and `reportAssignmentType` |
| Lock | A new direct dependency without updating `uv.lock` | `uv sync --locked` exit 1 and the stale-lock diagnostic |

The clean lock control uses `uv lock --check --offline`, which validates the
unchanged lock without installing into a developer environment. The broken lock
control exercises the actual CI `sync --locked` operation; any environment it
creates is confined to the disposable copy. As with ordinary dependency resolution,
this broken-lock case may contact the package index for resolver metadata.
Tool absence, network resolution errors, crashes,
or timeouts cannot count as the intended defect being caught.

The injected files/configuration are restored between cases; the working source
and lockfile are never mutated. Temporary projects are cleaned up after a run.
Clean/broken output and `summary.json` remain in `test-results/gates`, including
partial evidence when a gate fails. A gate that silently accepts its broken input
makes the verifier fail. CI runs the verifier on every existing matrix target;
failure fails that platform job and therefore the required aggregate check.

## Matrix, reports, and merge enforcement

The retained matrix is Python 3.13 on Windows, macOS and Linux, plus Python
3.10-3.12 on Linux. Actions and uv remain pinned; setup-uv caches dependencies.
Artifact upload retains JUnit, gate evidence, coverage and packages for seven
days, including evidence from failed checks when available. Performance and
hardware tests belong in separate explicitly opt-in work, outside this required
workflow.

The stable **`CI required`** aggregate has `if: always()` and needs the entire
platform matrix. It succeeds only when that matrix's result is `success`; failure,
cancellation or skipping fails the aggregate. Do not rename this context without
coordinating the repository rules.

On 2026-11-01 the repository ruleset
[Main: reviewed changes and passing CI](https://github.com/longhorn-baja-racing/dashboard/rules/25523260)
already enforced `CI required` from GitHub Actions (integration ID 15368) against
`refs/heads/main`, with strict up-to-date checks. It also required two approvals,
stale review dismissal, approval after the last push, resolved review threads,
and blocked deletion/force pushes. This inspection changed no repository settings.
The older issue description saying no status context was selected is outdated.

An admin should preserve those review rules and verify this same aggregate is
still selected after workflow changes. GitHub rules are server settings; this
checkout cannot enforce them by itself. The current connected account has push
access but no admin access, and cannot bypass the inspected ruleset.

The existing positive matrix on main at `cb172141c06183bdc84fdc70738a756502ff4fec`
passed all six platform/Python jobs and the aggregate in
[run 40357833466](https://github.com/longhorn-baja-racing/dashboard/actions/runs/40357833466).
That is evidence for the existing baseline, not a CI result for this unpublished
change. Fresh cross-platform negative-gate results require a reviewed PR/CI run;
local Windows evidence is documented separately.
