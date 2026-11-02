# Third-party notices checklist

The dashboard's own code is under the [MIT License](../LICENSE). A packaged
Windows build (portable ZIP #52, installer #53) also contains the Python runtime
and the libraries below, each under its own license. This page lists what ships
and where the notices come from, so packaging can include them. It is a
checklist for the team, not legal advice; review it again whenever a dependency
or the packaging tool changes.

## What ships

| Component | License (from package metadata) | Notice files |
| --- | --- | --- |
| lbr-dashboard | MIT | `LICENSE` |
| PySide6 and its split wheels (PySide6-Essentials, PySide6-Addons, and from 6.12 PySide6-Pdf and PySide6-WebEngine), shiboken6 (Qt for Python and the Qt libraries it bundles) | LGPL-3.0-only OR GPL-2.0-only OR GPL-3.0-only; we use it under **LGPL-3.0** | None in the wheels; `licenses/LGPL-3.0.txt` and `licenses/GPL-3.0.txt` from this repository |
| pyqtgraph | MIT | package `LICENSE.txt` (plus CC-BY notice for bundled color maps) |
| numpy | BSD-3-Clause AND 0BSD AND MIT AND Zlib AND CC0-1.0 | package license files, including bundled libraries |
| colorama | BSD-3-Clause | package `LICENSE.txt` |
| Python runtime | PSF-2.0 | the interpreter's `LICENSE.txt` |

Versions are whatever `uv.lock` pins; the collector below reports them.

## Collecting the notices

```sh
uv run --locked python scripts/collect_notices.py --check
uv run --locked python scripts/collect_notices.py --out build/notices
```

`--check` lists every runtime component (development tools are excluded) and
fails if any has no license file. `--out` copies each component's files into
its own folder and writes `THIRD-PARTY-NOTICES.txt` with names, versions,
licenses and, for Qt for Python, where the corresponding source is published.
`tests/test_license_notices.py` runs the same check, so adding a dependency
without notices fails CI.

## Items for the packaging work (#52, #53, #56)

- [ ] Copy the `--out` folder and the dashboard `LICENSE` into every ZIP and
      installer, and fail the build if `THIRD-PARTY-NOTICES.txt` is missing.
- [ ] Keep the Qt and PySide6 libraries as separate, replaceable shared
      libraries (no static linking or merging into one executable), as LGPL-3.0
      use expects.
- [ ] Point to the exact Qt for Python and Qt source versions used (the
      collector writes the links).
- [ ] When the freezing tool is chosen (for example PyInstaller), add its own
      runtime or bootloader notices if it ships any.
- [ ] Re-run `--check` after any dependency update.
