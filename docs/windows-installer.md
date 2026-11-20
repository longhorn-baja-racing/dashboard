# Windows installer

Issue #53. The installer is a normal Windows setup program around the same
files as the [portable ZIP](portable-build.md): it installs them, adds a Start
menu shortcut and an uninstall entry, and removes them cleanly again. It is
built with [Inno Setup](https://jrsoftware.org/isinfo.php) 6.

## Build it

On Windows, with Inno Setup 6.3 or newer installed (`winget install
JRSoftware.InnoSetup` or `choco install innosetup`):

```sh
uv sync --locked --extra package
uv run --locked python scripts/build_portable.py
uv run --locked python scripts/smoke_portable.py dist/portable
uv run --locked python scripts/build_installer.py
uv run --locked python scripts/smoke_installer.py dist/installer
```

The result is `dist/installer/LBRDashboard-<version>-windows-x64-setup.exe`
and a `.sha256` file next to it. `build_installer.py` finds `ISCC.exe` on
`PATH` or in the usual Inno Setup 6 folders; pass `--iscc <path>` otherwise.

- **One set of files:** the script extracts the portable ZIP into
  `build/installer/` and compiles that, so the installer contains exactly
  what the ZIP smoke test ran. Only `README.txt` is swapped for a version
  that talks about the Start menu and uninstalling.
- **One version:** the version comes from the ZIP name, which comes from
  `src/lbr_dashboard/version.py`. `packaging/installer.iss` has no version
  or path of its own; the script passes them with `/D`, and compiling the
  `.iss` file directly stops with an error. `--version` exists only to build
  an older-looking installer for the upgrade test.
- **Metadata:** the setup exe's Properties > Details tab and the entry in
  Settings > Apps show "LBR Dashboard", the version and "Longhorn Baja
  Racing".

The `Portable build` workflow builds the installer on `windows-latest` after
the ZIP, runs the smoke test below and uploads it as the
`installer-windows-x64` artifact (kept 14 days). The runner image ships Inno
Setup 6; the workflow installs 6.7.1 with Chocolatey if it is missing.

## What the installer does

| | Default (just me) | All users (optional) |
| --- | --- | --- |
| Rights needed | none (standard user) | administrator (UAC prompt) |
| Program folder | `%LOCALAPPDATA%\Programs\LBR Dashboard` | `C:\Program Files\LBR Dashboard` |
| Start menu shortcut | your Start menu | everyone's Start menu |
| Uninstall entry | Settings > Apps, your account | Settings > Apps, every account |

The first page asks "Install for me only" or "Install for all users"; the
first is the default and never asks for elevation. A desktop shortcut is an
unchecked option. Windows 10 or 11, 64-bit, is required.

The program folder only holds program files. The dashboard writes nothing
there; per-user data lives under `%LOCALAPPDATA%`:

| Data | Folder |
| --- | --- |
| Logs | `%LOCALAPPDATA%\LBR Dashboard` |
| Preferences | `%LOCALAPPDATA%\LonghornBaja\dashboard` |
| Telemetry, workspaces, exports | wherever you saved them |

### Upgrade

Running a newer installer finds the installed copy (by its fixed `AppId`) and
upgrades it in place, in the same folder and for the same scope. It first
deletes the old `_internal`, `notices` and `samples` folders, so no library
from the old version stays behind. Logs, preferences and telemetry are not
touched. If the dashboard is running, setup offers to close it. Installing an
older version over a newer one works the same way, which is how to roll back
(see [releasing](releasing.md)).

### Uninstall

Settings > Apps > Installed apps > LBR Dashboard > Uninstall (or the
uninstall entry in Control Panel) removes the program folder, the Start menu
and desktop shortcuts and the uninstall entry. It then asks whether to also
delete your logs and preferences; the default answer is **No**. A silent
uninstall (`unins000.exe /VERYSILENT`) always keeps them. Telemetry files are
never touched.

### Silent install for labs and scripts

```bat
LBRDashboard-0.1.0-windows-x64-setup.exe /VERYSILENT /SUPPRESSMSGBOXES /CURRENTUSER
"%LOCALAPPDATA%\Programs\LBR Dashboard\unins000.exe" /VERYSILENT /SUPPRESSMSGBOXES
```

Use `/ALLUSERS` from an elevated prompt instead of `/CURRENTUSER` for a
machine-wide install, `/DIR="D:\Apps\LBR Dashboard"` for another folder and
`/LOG=setup.log` for a log.

## The smoke test

`scripts/smoke_installer.py` (Windows only) refuses to run if the dashboard is
already installed for the current user, then:

1. **Upgrade** (with `--older <installer>`; CI builds one as version 0.0.0):
   installs the older one per-user into the default folder, plants a stale
   file in its `_internal` folder, installs the new one without choosing a
   folder, and checks it reused the folder, shows the new version in the
   uninstall entry and removed the stale file.
2. **Fresh install** into a folder whose path has a space, an accented
   letter and a CJK character (`/DIR`).

For each it checks the uninstall entry (name, version, publisher, folder), the
Start menu shortcut and the layout, runs `LBRDashboard.exe --self-test` and a
normal launch with Python removed from the environment, checks nothing was
written in the program folder, writes a marker file into both user data
folders, uninstalls with `/VERYSILENT` and checks that the program folder,
shortcut and uninstall entry are gone while the markers are still there. If a
step fails it uninstalls what it installed, so it can be run again.

## Manual check before handing the installer out

CI runs as an administrator on a machine with Python, offscreen. Before a
release, on a clean Windows 10 or 11 machine (or VM) without Python, signed in
as a **standard user**:

- [ ] Double-click the installer. SmartScreen warns because it is unsigned;
      "More info" > "Run anyway". No UAC prompt appears for "Install for me
      only".
- [ ] Start LBR Dashboard from the Start menu, open
      `samples\sample_baja_log.csv` and plot a column.
- [ ] Check Settings > Apps shows LBR Dashboard with the right version and
      publisher, and the setup exe's Properties > Details shows the version.
- [ ] Install the next version over it; logs and preferences remain, the
      Start menu shortcut still works.
- [ ] Uninstall from Settings > Apps, answer No to deleting data: the program
      folder and shortcut are gone, `%LOCALAPPDATA%\LBR Dashboard` remains.
- [ ] Repeat with a Windows user name that has a space or non-ASCII letter.

## Not done yet

- **Unsigned.** Windows SmartScreen and some antivirus tools warn about new
  unsigned programs. Signing needs approved credentials (#56 keeps it out of
  scope until then).
- **No automatic updates.** Users install a newer setup over the old one.
- **x64 only.** Windows on ARM can run it through emulation, but it is not
  tested.
