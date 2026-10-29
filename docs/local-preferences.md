# Local preferences

Issue #24 adds versioned local JSON preferences for theme, recent sources and the
last source kind. They belong to this machine/user. Portable workspace layout,
widget bindings and import/export are #49, not part of this file. The file has
no raw samples, signal units/timestamps/quality, dock geometry or executable
configuration. Choosing a theme or saving recent paths does not modify telemetry.

## Location and app behavior

The supported entry point composes `PreferencesStore` with the application:

- Windows: `%LOCALAPPDATA%/LonghornBaja/dashboard/preferences.json`, falling back
  to the user's `AppData/Local` folder if that variable is absent.
- macOS development: `~/Library/Application Support/LonghornBaja/dashboard/`.
- Linux development: absolute `$XDG_CONFIG_HOME/LonghornBaja/dashboard/`, otherwise
  `~/.config/LonghornBaja/dashboard/`.

The working directory and repository are never the default storage location.
`PreferencesStore(path)` supports an explicit isolated path for tests or embedding.
`create_main_window(preferences_store=store)` enables persistence for that window;
calling the composition helper without a store uses memory-only preferences and
does not read/write the user's settings. `main()` enables the platform store.

Preferences → Theme offers System, Light and Dark. A successful save applies the
new window-scoped palette and plot colors while retaining data/selection. System
uses Qt's standard palette at selection/startup. File → Recent CSV files lists up
to ten successful imports; selecting one invokes the normal load/error flow.
Failed or cancelled imports do not add entries. Missing recent paths remain
listed and show an ordinary load error rather than silently disappearing.
Clear recent files removes only local history, not files or loaded telemetry.
Settings are never used to automatically open a file or connect to a source.

Changes save immediately. Save failure leaves active preference values, checked
theme choice and visible palette unchanged, and displays a status-bar error. A
successful CSV import can still be displayed if saving its history fails; the
save error remains visible. The source kind is a generic identifier; only the
existing CSV provider is wired to the current recent-file menu. No adapter is
selected or installed because its name appears in settings.

## Schema, bounds and compatibility

Current schema version 1:

```json
{
  "schema_version": 1,
  "theme": "system",
  "default_source": "csv",
  "recent_sources": [
    {"kind": "csv", "location": "C:/example/pressure.csv"}
  ]
}
```

Themes are exactly system/light/dark. Source kinds use the generic lowercase
identifier rules. Locations are opaque nonempty strings, retaining Unicode,
spaces and path spelling. They may identify local paths/ports for explicit future
adapters; they are not portable paths or signal identities. Credentials should
not be supplied as locations or extension metadata. There is no cloud upload or
workspace export of preferences.

Recent entries are unique by `(kind, location)`; remembering an existing entry
moves it to the front and retains at most ten. Unknown extension fields are
preserved at root and source-entry levels, including exact metadata keys.
Extensions cannot replace known fields. Unknown themes and future/invalid schema
versions are rejected. In particular, save refuses to overwrite an unsupported
primary version even if an older backup could be loaded.

An explicitly supported minimal version 0 uses `schema_version: 0`, optional
`theme` and `recent_files: [path, ...]`. This is the documented migration input,
not a claim that a previous released dashboard wrote it. Migration maps paths to
CSV sources, deduplicates in input order and preserves unknown metadata. It is
deterministic and in-memory until the next explicit successful save writes v1.
No version is inferred from an unrelated JSON document.

Documents are UTF-8 JSON limited to 64 KiB, depth 16 and 4,096 values; object
keys are at most 128 characters and strings at most 8,192. Duplicate JSON keys,
non-finite values, cycles, unsupported Python objects, invalid versions and
malformed known fields are rejected before active state changes. Nested model
values are immutable; serialization returns detached containers. No pickle,
`eval`, arbitrary-object deserialization or imports are performed.

## Atomic saves and recovery

`PreferencesStore.save(candidate)` validates and serializes the complete candidate
before writing. It creates a temporary file in the destination directory,
writes/flushes/fsyncs it, closes it, and atomically replaces the destination.
The previous validated primary is written atomically to `preferences.json.bak`
first. If backup creation or primary replacement fails, the previous primary
and active preference object remain intact. Temporary files are removed where
the filesystem permits cleanup; a crash can leave an ignored orphan `.tmp`.
This uses local filesystem replacement semantics; it does not promise arbitrary
network-filesystem or power-loss guarantees, and it is not a multi-writer merge
mechanism. The application owner serializes writes; multiple app instances can
overwrite one another's last committed preferences.

Reload tries primary, then backup. A valid backup is used with a visible recovery
warning, without automatically rewriting the broken file. A corrupt primary is
never copied over a valid backup on the next save. If both documents are invalid,
the current active preferences remain unchanged (startup retains defaults) and
a warning is returned. Missing files are a normal first-run condition. Read or
permission errors are recoverable diagnostics; an unreadable primary is not
silently overwritten without a validated recovery copy.

## Example

```python
from pathlib import Path
from tempfile import TemporaryDirectory
from dataclasses import replace
from lbr_dashboard.core import PreferencesStore, RecentSource

with TemporaryDirectory() as directory:
    path = Path(directory) / "preferences.json"
    store = PreferencesStore(path)
    current = store.reload().preferences
    candidate = replace(current, theme="dark").remember(RecentSource("csv", "C:/example.csv"))
    store.save(candidate)
    assert PreferencesStore(path).reload().preferences == candidate
```

Tests cover real subprocess restart, theme/menu/window restart, corruption and
backup recovery, invalid/oversized inputs, migration, unknown fields and injected
write/fsync/replace failures. They use isolated settings paths.
