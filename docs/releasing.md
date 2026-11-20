# Releasing

Issue #56. A release is a Git tag `vX.Y.Z` plus a GitHub Release with the
Windows installer, the portable ZIP, the license notices and their SHA-256
checksums. The `Release` workflow (`.github/workflows/release.yml`) builds and
tests everything from the tag and publishes only if every step passed.

## What a release contains

| File | What it is |
| --- | --- |
| `LBRDashboard-X.Y.Z-windows-x64-setup.exe` | Per-user installer ([windows-installer.md](windows-installer.md)) |
| `LBRDashboard-X.Y.Z-windows-x64.zip` | Portable ZIP ([portable-build.md](portable-build.md)) |
| `LBRDashboard-X.Y.Z-THIRD-PARTY-NOTICES.txt` | Licenses of the bundled Python, Qt and libraries ([third-party-notices.md](third-party-notices.md)); also inside both downloads |
| `SHA256SUMS.txt` | SHA-256 of the three files above |

The release notes are the version's section of [CHANGELOG.md](../CHANGELOG.md)
followed by the download table, the checksums, the supported target (Windows
10 and 11, x64) and the unsigned-build warning. Versions with `a`, `b`, `rc`
or `dev` in them are marked as pre-releases.

Supported targets: Windows 10 and 11, 64-bit. macOS and Linux builds are not
published. Builds are **not code signed**; the notes say so and explain the
SmartScreen prompt.

## Cut a release

1. On `main`, open a PR that:
   - sets `__version__` in `src/lbr_dashboard/version.py` to `X.Y.Z`
     (the only place the version lives);
   - adds or completes `## [X.Y.Z] - YYYY-MM-DD` in `CHANGELOG.md`, written
     for people downloading the dashboard. Note any change to where user data
     is stored or how old preferences are read.
2. Merge it after the Quality and Portable build checks pass. The portable
   build check also builds and smoke-tests the installer.
3. Optional dry run: Actions > Release > Run workflow on `main`. It runs the
   whole pipeline and uploads the would-be release as the
   `release-windows-x64` artifact, but never publishes. It accepts an
   undated changelog section.
4. Tag the merge commit and push the tag:

   ```sh
   git switch main && git pull
   git tag -a vX.Y.Z -m "LBR Dashboard X.Y.Z"
   git push origin vX.Y.Z
   ```

5. Watch Actions > Release. The `windows` job:
   - stops at once if the tag is not `v` plus the package version, or the
     changelog section is missing, empty or undated;
   - runs the regression tests;
   - builds the portable ZIP and runs its smoke test;
   - builds the installer and an older test installer, and runs the silent
     install, upgrade, self-test, launch and uninstall smoke test;
   - checks each file's name, version and `.sha256`, checks the ZIP's
     `build-info.json` matches the tag, and writes `SHA256SUMS.txt` and the
     notes.

   Only then does the `publish` job run. It is the only job with permission to
   write (`contents: write`, using the workflow's `GITHUB_TOKEN`); it verifies
   the checksums again, creates the release as a **draft**, uploads every
   file, checks they all arrived and only then makes the release public.
6. Do the manual checks in [windows-installer.md](windows-installer.md) and
   [portable-build.md](portable-build.md) on a clean Windows machine with the
   published files.

## When something fails

- **Any build or test step fails:** nothing is published. Fix the problem on
  `main`, then move the tag only if no release was published from it:
  `git push origin :refs/tags/vX.Y.Z`, retag the fixed commit and push again.
  If you would rather not move a tag, bump to the next patch version.
- **Publishing fails half way:** the release stays a draft that only
  maintainers can see. Delete it (Releases > the draft > Delete, or
  `gh release delete vX.Y.Z`) and re-run the failed `publish` job.
- **A published release is broken:** do not replace its files. Mark it as a
  pre-release or delete it, and release a fixed `X.Y.Z+1`.

## Verify a download

PowerShell:

```powershell
Get-FileHash .\LBRDashboard-X.Y.Z-windows-x64-setup.exe -Algorithm SHA256
```

The hash must match the line in `SHA256SUMS.txt` (also shown in the release
notes). On Linux or macOS: `sha256sum --check --ignore-missing SHA256SUMS.txt`.

## Upgrade

- **Installer:** run the newer `-setup.exe`. It upgrades the installed copy
  in place (same folder, same "just me" or "all users" choice), replaces the
  program files and keeps logs, preferences and telemetry. There is no need
  to uninstall first. Close the dashboard first, or let setup close it.
- **Portable ZIP:** extract the new ZIP to a new folder and delete the old
  folder when you are happy. Both versions read the same per-user data.

User data is not stored with the program:

| Data | Folder |
| --- | --- |
| Logs | `%LOCALAPPDATA%\LBR Dashboard` |
| Preferences | `%LOCALAPPDATA%\LonghornBaja\dashboard\preferences.json` |
| Telemetry, workspaces, exports | wherever you saved them |

Preferences carry a schema version (currently 1) and unknown fields are kept
when they are saved (see [local-preferences.md](local-preferences.md)). A
dashboard that finds a schema version it does not support uses its backup
copy or default settings and reports a warning instead of failing. Each
release's notes call out any change to these formats.

## Roll back

- **Installer:** download the earlier release's `-setup.exe` and run it. It
  installs over the newer version in place (old program files are removed
  first) and keeps your data. Uninstalling first also works.
- **Portable ZIP:** run the earlier version's folder.

Preferences written by a newer version may contain settings an older version
does not know; it keeps them as they are. If a newer version moved to a schema
the older one does not support, the older one starts with default settings.
To start from defaults on purpose, close the dashboard and rename
`%LOCALAPPDATA%\LonghornBaja\dashboard\preferences.json`. Telemetry files are never changed by installing, upgrading or
rolling back.

## Not done yet

- Code signing (needs approved credentials) and automatic updates are out of
  scope for now.
- Builds are not bit-for-bit reproducible: PyInstaller output differs between
  runs. `build-info.json` in each download records the commit, Python,
  PyInstaller and PySide6 versions and the `uv.lock` hash, so a build can be
  repeated from the same inputs.
