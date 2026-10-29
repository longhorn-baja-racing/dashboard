# Dashboard architecture

This document describes the boundaries that current dashboard code follows.
It adds no runtime layers for providers that do not exist yet.

## Layers and allowed dependencies

| Layer | Responsibility | May depend on |
| --- | --- | --- |
| `core` | Framework-neutral data contracts, registries, and worker lifecycle | Python standard library |
| `importers` | File decoding into normalized models | `core` |
| `ui` | Qt widgets and presentation | `core`, Qt, and PyQtGraph |
| `app` | Composition, application-owned model state, and process lifecycle | `core`, `importers`, and `ui` |

The dependency direction is enforced by `tests/architecture/test_boundaries.py`.
Core and importers never import the UI or the composition root. UI widgets do
not import concrete importer implementations.
The check resolves relative and package-root imports, every imported alias, and
literal `importlib.import_module` / `__import__` calls (including importlib aliases,
literal relative packages, and literal `fromlist` entries).
It is a static guard; computed module names are not resolved.
Top-level Python modules belong to the application layer. Core, importers,
and widgets cannot import that layer, including `dashboard_controller`.

## Ownership

- `app` creates and connects the window, importer, and `DashboardController`.
  Provider selection belongs in this composition layer.
- `DashboardController` owns `LogSessionStore`, handles UI requests, and passes
  decoded table rows or numeric series to the window for rendering.
- `MainWindow.load_csv` calls the controller synchronously through a UI-defined
  protocol, so filesystem and decoding exceptions reach programmatic callers.
  File-dialog requests use `csv_requested`; the controller catches failures at
  that Qt slot boundary, displays a status-bar error, and emits
  `csv_load_failed(path, message)`. Failed imports retain the prior session;
  a successful load clears the error message.
- Widgets render the rows they receive. They do not open files, parse CSV, or
  retain a second copy of the imported model or numeric columns.
- A `Registry` is a per-application selection mechanism. The current app uses
  one for its concrete CSV importer. Source or widget registries should be
  added only when multiple real implementations need selection.
- Registry identifiers are exact: empty or whitespace-padded identifiers are
  rejected. Duplicate IDs fail at registration, unknown IDs raise a clear
  `KeyError`, and factory failures include registry and ID context.
- `core` has no Qt dependency. Workers publish immutable `ServiceEvent`
  snapshots; the owner drains them on its own thread. Worker failures are
  exposed as immutable `ServiceFailure` values to that owner.
- Cancellation is cooperative. The owner requests cancellation and joins the
  worker before releasing resources; worker cleanup belongs in `finally`.

## Worker lifecycle

`BackgroundService` owns one worker thread, event queue, and cancellation
token. Worker code can only enqueue immutable events; it cannot call a UI
callback. After the worker finishes, the owner calls `join`, reads `failure`
when needed, and drains queued events at its normal update boundary.

## Legacy CSV loading

Columns are selected by physical index, so repeated display names retain their
own values. Name lookup returns the first occurrence (including `timestamp_ms`).
Non-finite numeric cells become unavailable values and are omitted from plots.
Successful empty loads clear columns, table cells, and plots; missing cells in
a replacement file cannot retain values from the previous file.
