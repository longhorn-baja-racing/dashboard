# Workspace file format (version 1)

A workspace describes which widgets the dashboard shows, how they are arranged,
and which signals they display. It is a small JSON document that can be shared
between team members and machines. The format and its validator live in
`lbr_dashboard.workspace` and do not depend on Qt.

This is the format only (#112). The workspace manager UI, import/export and
layout restore belong to #49, real dock geometry to #37, and resolving signal
IDs against the signal registry to #36.

## What a workspace never contains

- Telemetry samples. Arrays inside `extensions` are limited to 64 entries and
  the whole `extensions` object to 16 KiB.
- Machine-specific details: absolute file paths (`C:\...`, `/home/...`,
  `\\server\...`, `~/...`, `file:`), device nodes (`/dev/...`) and serial ports
  (`COM3`). Any string that looks like one is rejected, and extension keys such
  as `path`, `file`, `dir` or `port` are refused. Keep these in local
  preferences (#24).
- Credentials. Extension keys such as `password`, `token`, `secret`, `auth`,
  `api_key` or `private_key` are refused.
- Code. Files are only ever parsed as JSON. Nothing is imported, evaluated or
  unpickled.

## Example

```json
{
  "format": "lbr-dashboard-workspace",
  "version": 1,
  "name": "Engine temperature and pressure",
  "widgets": [
    {
      "id": "env-plot",
      "type": "time_plot",
      "axes": [{"id": "temperature", "side": "left", "unit": "degC", "range": [0, 120]}],
      "bindings": [{"signal": "coolant_temp_c", "axis": "temperature", "color": "#e8590c"}]
    },
    {"id": "env-values", "type": "value_cards", "bindings": [{"signal": "coolant_temp_c"}]}
  ],
  "layout": {
    "kind": "split",
    "orientation": "vertical",
    "weights": [3, 1],
    "children": [
      {"kind": "widget", "widget": "env-plot"},
      {"kind": "widget", "widget": "env-values"}
    ]
  }
}
```

Complete examples, in the exact form the serializer writes them:

- `sample_data/workspaces/numeric_environment.json`: two numeric signals on
  separate axes plus value cards.
- `sample_data/workspaces/mixed_status.json`: numeric traces, discrete status
  signals on an event timeline, tabs, a raw table and an extension.

Their signal IDs are illustrative. The mixed example uses column names from
`sample_data/sample_baja_log.csv`; the numeric example uses the column names of
the generic environment fixture from #105. The exact IDs a CSV adapter produces
are decided in #31. Until then, any binding that does not match is simply
reported as unresolved.

## Fields

Top level (unknown fields are rejected):

| Field | Required | Meaning |
| --- | --- | --- |
| `format` | yes | Always `lbr-dashboard-workspace`. |
| `version` | yes | Positive integer. This build reads version 1. |
| `name` | yes | Display name, up to 200 characters. |
| `description` | no | Up to 2000 characters; newlines and tabs allowed. |
| `widgets` | yes | 1 to 64 widget objects. |
| `layout` | yes | Layout tree that places every widget exactly once. |
| `extensions` | no | Namespaced keys (`team.notes`) with small JSON values. |

Widget:

| Field | Required | Meaning |
| --- | --- | --- |
| `id` | yes | Stable instance ID: lowercase letters and digits joined by `.`, `-` or `_`, starting with a letter. Unique per workspace. |
| `type` | yes | One of `time_plot`, `value_cards`, `event_timeline`, `raw_table`. |
| `title` | no | Display title. |
| `axes` | no | `time_plot` only. Up to 8 axis preferences. |
| `bindings` | no | Up to 64 signal bindings. |

Axis preference (`time_plot` only): `id` (required, unique within the widget),
`side` (`left` or `right`, default `left`), `label`, `unit` (a display label;
no conversion is implied), and `range` (`[minimum, maximum]` with
minimum < maximum; omit for auto range).

Signal binding: `signal` (required descriptor ID, opaque to the workspace),
`label`, `color` (`#rrggbb`), and `axis` (`time_plot` only; must name one of
the widget's axes).

Layout nodes, nested at most 8 deep, each with at most 16 children:

- `{"kind": "widget", "widget": "<widget id>"}`
- `{"kind": "split", "orientation": "horizontal" | "vertical", "children": [...], "weights": [...]}`;
  `weights` is optional, one positive number per child.
- `{"kind": "tabs", "children": [...], "current": 0}`; `current` is optional.

Every widget must appear in the layout exactly once, and the layout may only
reference defined widgets.

## Python API

```python
from pathlib import Path
from lbr_dashboard.workspace import load_workspace, resolve_bindings, save_workspace

result = load_workspace(Path("my_layout.json"))
if result.recovered:
    print("Primary file was damaged:", result.primary_error)
resolution = resolve_bindings(result.workspace, available_signal_ids)
for missing in resolution.unresolved:
    print(f"{missing.widget_id}: signal {missing.signal} is not available")
save_workspace(Path("my_layout.json"), result.workspace)
```

- `loads_workspace(text)` and `workspace_from_dict(data)` validate the whole
  document and return an immutable `Workspace`, or raise `WorkspaceError`.
- `WorkspaceError.issues` lists every problem at once as `WorkspaceIssue`
  objects with a `location` (for example `$.widgets[1].bindings[0].axis`), a
  stable `code` and a readable `message`, ready for an import preview UI.
- `dumps_workspace(workspace)` returns deterministic, ASCII-only JSON and
  validates first, so an invalid workspace built in code is never written.
- `resolve_bindings(workspace, ids)` splits bindings into resolved and
  unresolved. Missing signals stay in the workspace so the user can rebind
  them; nothing is deleted.

Issue codes: `type`, `required`, `unknown_field`, `invalid_value`, `too_long`,
`too_many`, `too_large`, `too_deep`, `duplicate_id`, `duplicate_placement`,
`unplaced_widget`, `unknown_reference`, `unknown_widget_type`,
`unknown_format`, `future_version`, `unsupported_version`, `machine_local`,
`secret`, `json`, `decode`, `io`.

## Saving and recovery

`save_workspace(path, workspace)` writes a temporary file in the same folder,
flushes it to disk and atomically replaces the target, so a crash or full disk
leaves either the old file or the new file, never a mix. Before replacing, a
valid existing file is copied to `<name>.bak`. A damaged primary never
overwrites a good backup.

`load_workspace(path)` falls back to `<name>.bak` when the primary cannot be
read or fails validation, and reports `recovered=True` with the primary's error.
It never modifies files. If both are unusable, the primary's error is raised.

## Versions and migration

Files declare an integer `version`. A file newer than the running dashboard is
refused with `future_version` instead of being guessed at. Older versions are
upgraded by functions registered in `lbr_dashboard.workspace.MIGRATIONS`, keyed
by the version they upgrade from. Each step receives a private copy, so the
input is never modified, and the result is validated like any other document.
Version 1 is the first published format, so the registry is empty today.

When a change is needed: bump `CURRENT_VERSION`, add a migration from the
previous version, keep a fixture of the old format, and test the upgrade.

## Limits

| Limit | Value |
| --- | --- |
| File size | 1 MiB |
| JSON nesting | 32 levels |
| Widgets | 64 |
| Bindings per widget | 64 |
| Axes per widget | 8 |
| Layout depth / children per node | 8 / 16 |
| IDs | 128 characters |
| Names, titles, labels, units | 200 characters |
| Description | 2000 characters |
| Extensions | 16 KiB, arrays of at most 64 entries |
