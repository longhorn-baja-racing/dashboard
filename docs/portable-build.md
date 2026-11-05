# Portable Windows build

Issue #52. The first way to hand the dashboard to someone without Python is a
ZIP they extract and run. The installer (#53) and releases (#56) wrap this
same build.

## Build it

On Windows, from a checkout:

```sh
uv sync --locked --extra package
uv run --locked python scripts/build_portable.py
uv run --locked python scripts/smoke_portable.py dist/portable
```

The result is `dist/portable/LBRDashboard-<version>-windows-x64.zip` and a
`.sha256` file next to it. The `Portable build` workflow does the same on
`windows-latest` for every pull request and push to `main`, and uploads the
ZIP as the `portable-windows-x64` artifact (kept 14 days). That workflow is
not a required check yet.

The same commands work on Linux and macOS for development (the ZIP is then
named for that platform), but only Windows is a target for now.

## What is in the ZIP

```
LBRDashboard-<version>/
  LBRDashboard.exe        the dashboard (windowed, no console)
  _internal/              Python, Qt, PySide6, pyqtgraph, numpy as separate files
  LICENSE                 the dashboard's MIT license
  notices/                every bundled component's license files
    THIRD-PARTY-NOTICES.txt
  samples/                the CSV files from sample_data/
  README.txt              how to run it, where data goes, how to check a copy
  build-info.json         version, commit, platform, Python, PyInstaller, PySide6, uv.lock hash
```

The exe carries a Windows version resource (product name, company, version,
copyright) generated from `src/lbr_dashboard/version.py`, so Explorer's
Properties > Details tab shows the right version.

## How it is built

- **Packager:** PyInstaller, in one-folder mode. It is maintained, has
  first-party hooks for PySide6 and keeps every Qt library as its own
  replaceable DLL, which is what LGPL-3.0 use of Qt expects. One-file mode is
  not used: it extracts to a temporary folder on every start, which is slower
  and trips antivirus tools more often.
- **One configuration:** `scripts/build_portable.py` holds every PyInstaller
  option; there is no hand-edited `.spec` file. The frozen entry point is
  `packaging/entry.py`, which runs the normal `lbr_dashboard.app.main`, so the
  portable app is not a separate fork of the code.
- **Unused Qt:** the build excludes Qt modules the dashboard never imports
  (WebEngine, PDF, QML/Quick, 3D, Multimedia, Charts). PySide6's hook still
  collects two plugins that link to them, the virtual keyboard input method
  and the PDF image format plugin. The script deletes those plugins and the
  libraries they need, and the smoke test proves the app runs without them.
  This keeps a Chromium engine out of the ZIP, which matters since PySide6
  6.12 also installs QtWebEngine (see #119).
- **Windows ICU:** Qt uses Windows' system `icuuc.dll`. An unrelated copy from
  a PATH entry (for example Poppler) can have incompatible exports, even though
  the filename matches. The build removes bundled copies of that unversioned
  DLL, and the smoke layout check rejects them before launching the executable.
- **Notices:** `scripts/collect_notices.py` (#106) copies the license files of
  every runtime dependency and the Python runtime into `notices/`. The build
  adds the PyInstaller bootloader's license, since the bootloader is part of
  the exe. The build fails if any component has no license file.
- **Reproducible inputs:** the versions come from `uv.lock` (PyInstaller is
  locked too, as the `package` extra), Python is pinned in the workflow, and
  `build-info.json` records all of them and the commit. The ZIP itself is
  deterministic: sorted entries, one timestamp (`SOURCE_DATE_EPOCH`, else the
  commit time) and fixed permissions, so packing the same folder twice gives
  the same bytes. PyInstaller's own output is not guaranteed to be
  byte-identical between runs, so compare `build-info.json`, not ZIP hashes,
  across separate builds.

## Where the app writes

Nothing is written inside the extracted folder; the smoke test fails if a run
adds or changes any file there. That lets the folder live on a read-only or
shared drive, and deleting it never deletes telemetry. Per-user data goes
under `%LOCALAPPDATA%`:

| Data | Location | Added by |
| --- | --- | --- |
| Logs | `%LOCALAPPDATA%\LBR Dashboard\logs` | #25 / #131 |
| Preferences | `%LOCALAPPDATA%\LonghornBaja\dashboard\preferences.json` | #24 / #126 |

Those two PRs currently use different folder names. It would be simpler for
users (and for the installer's "keep my data" rule in #53) if they shared one
root, for example `%LOCALAPPDATA%\LBR Dashboard\`.

## The smoke test

`scripts/smoke_portable.py` checks a ZIP the way a user would run it:

1. Extract it into a folder whose path has a space, an accented letter and a
   CJK character.
2. Check the layout: the exe, `LICENSE`, the notices, at least one sample,
   `build-info.json` matching the ZIP's version and platform, and no unused
   Qt libraries.
3. Remove every Python folder from `PATH` and every `PYTHON*`, `VIRTUAL_ENV`
   and `UV_*` variable, then run `LBRDashboard.exe --self-test --report
   <file>`. The self-test (`lbr_dashboard/self_test.py`) starts the main
   window, opens the bundled sample CSV, plots a column, renders the window
   and checks the bundle files. The exe has no console, so results go to the
   report file and the exit code.
4. Start the dashboard normally and check that it is still running after five
   seconds.
5. Check that nothing was written inside the extracted folder.

CI runs this offscreen (`QT_QPA_PLATFORM=offscreen`). A real display and a
clean machine still need a person:

## Manual check before handing a ZIP out

On a Windows 10 or 11 machine (or VM) without Python, signed in as a standard
user:

- [ ] Extract the ZIP to a folder with a space in its name and double-click
      `LBRDashboard.exe`. SmartScreen warns because the build is unsigned;
      "More info" > "Run anyway" starts it.
- [ ] File > Open `samples\sample_baja_log.csv`, pick a few columns and check
      the plot and table.
- [ ] Close the app and confirm the folder has no new files.
- [ ] Run `LBRDashboard.exe --self-test --report report.txt` from a terminal
      and check `report.txt` ends with `self-test passed`.
- [ ] Check Properties > Details on the exe shows the expected version.

Signing, an installer and automatic updates are out of scope here (#53, #56).
