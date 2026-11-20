# Workspace docks

Use **Workspace > Add plot widget** or **Add value widget** to create a native
dock. Move it by its title bar, drop it on another dock to make tabs, or drag
it beside another dock to split the space. Closing it hides it. Its entry in
the Workspace menu shows it again with the same signal binding. **Remove
widget** deletes it.

Drag one available signal from the signal browser into a dock. To bind with the
keyboard, select the signal in the browser, tab to the dock's **Bind selected
signal** button and press Space. Plot docks accept scalar numeric and boolean
signals. Value docks show the latest recorded value, including unavailable
readings as such. Plot docks show up to the latest 1,000 retained readings.

The binding uses both the signal and source IDs. An unavailable source is
reported in the dock. Dropping a missing, incompatible or malformed payload
leaves the prior binding intact. Shared replay cursor behavior belongs to
issue #40.

## Saving and opening workspaces

The Workspace menu manages whole workspaces (#49). Its first entry shows the
current workspace name and file.

| Entry | What it does |
| --- | --- |
| New workspace | Removes every workspace dock and closes the analysis tools. |
| Open workspace... | Opens a file and makes it the current workspace. |
| Open demo workspace | Opens the bundled demo overview preset (#45) as an unsaved workspace. |
| Save workspace | Saves to the current file, or asks for one if it has none. |
| Save workspace as... | Saves to a new file, which becomes the current file. |
| Rename workspace... | Changes the name stored in the file (save to keep it). |
| Duplicate workspace | Continues with an unsaved copy named "... copy", so the original file is never touched. |
| Import workspace... | Previews a shared file, then replaces the current docks if you choose **Replace current workspace**. You can rename it in the preview; **Cancel** changes nothing. |
| Export workspace... | Writes a portable copy without changing the current file. |

A workspace stores the plot and value docks with their signal IDs, the trace
colors, axis sides and hidden traces of each plot, the analysis tools that are
open (with the settings of tools that provide `configuration()`), and the dock
arrangement: window edge, splits with relative sizes, tab groups and the shown
tab, and hidden or floating docks. It does not store telemetry, file paths,
source locations, pixel positions or the theme. The file format is described
in `docs/workspace-format.md`.

Relative sizes are applied again after the window is shown and its layout has
settled, so a workspace restored at startup keeps its proportions. Dock
contents scroll when a dock is small instead of forcing a large minimum size,
so saved proportions also fit on smaller screens.

The import preview lists every widget and every signal binding, and says which
bindings resolve against the open session and which will be placeholders.

Every file is validated completely before anything changes. A damaged file, a
file from a newer dashboard version, or one with machine-specific paths is
refused with the reason, and the current workspace stays as it was. Saving
writes atomically and keeps the previous version as `<name>.bak`. If a file
is damaged but its backup is good, opening it opens the backup and says so;
saving then replaces the damaged file. A file written by a newer dashboard
version is never overwritten.

## Missing signals

A saved binding whose signal ID is not in the open session (or when no session
is open) stays in the dock as a placeholder: the dock says
"Missing signal ID - bind an available signal", and a plot lists it as
"Missing: ID" in its trace list. Placeholders resolve by themselves as soon as
a session provides that signal ID, keeping the saved color, axis and
visibility. A dock that finds its signals comes to the front of its tab group
unless another tab there already shows data.

To repair one yourself, select an available signal in the browser and:

- in a value dock, press **Bind selected signal**;
- in a plot dock, choose the trace in the trace list and press **Replace trace
  with selected signal**. The new trace keeps the old color, axis and
  visibility. **Remove selected trace** drops a placeholder.

Saving keeps unresolved placeholders, so a workspace made for one log still
works with the next log that has the same signal IDs. Widgets this version
cannot show (event timelines, raw tables, unknown analysis tools) open as
placeholder docks and are saved back unchanged.

## Restart

The last workspace you opened or saved is remembered in the local preferences
(`last_workspace`, see `docs/local-preferences.md`) and reopened at startup.
If it is missing or cannot be read, the dashboard starts with an empty
workspace and explains why in the status bar and a non-blocking message.
**New workspace**, **Duplicate workspace**, an import or the demo preset leave
nothing to reopen until you save.
