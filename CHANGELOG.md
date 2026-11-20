# Changelog

Release notes for the LBR Dashboard. Each release has a section named after
the version in `src/lbr_dashboard/version.py`; the Release workflow copies
that section into the GitHub Release, so write it for people downloading the
dashboard. Date the section (`## [x.y.z] - YYYY-MM-DD`) when you tag it. See
[docs/releasing.md](docs/releasing.md).

## [0.1.0] - Unreleased

First Windows preview. Unsigned; see the download notes below.

### Added

- Open and plot CSV logs, including firmware `lbr-csv` session exports, with
  clear empty states and import errors.
- A finite process and motion simulator for trying the dashboard without a
  vehicle.
- Session replay controls, a signal browser and type-aware value displays.
- Local logs and a diagnostics report for troubleshooting.
- A portable Windows ZIP and a per-user Windows installer with a Start menu
  shortcut and a clean uninstall that keeps logs and preferences.
