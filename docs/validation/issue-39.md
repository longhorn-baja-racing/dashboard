# Multiple traces and labeled axes

This change extends the native plot docks from #37 and the shared replay cursor from #40.
The PR is stacked on #167. The main selection plot keeps its existing single-signal behavior.

## Windows walkthrough

```powershell
uv sync --locked --extra dev
uv run --locked lbr-dashboard
```

1. Select the fictional process simulator and connect.
2. Choose **Workspace > Add plot widget**.
3. Select pressure in the browser and press **Add selected signal** in the dock.
4. Select temperature and add it to the same dock. The legend identifies each signal,
   original unit and left/right assignment. Each axis retains its independent scale.
5. Move the replay slider and drag the dock's yellow cursor. Every cursor and selected
   reading follows the shared session clock. Missing readings remain unavailable.
6. Select a trace in the dock's trace list. Choose a side and press **Assign trace axis**.
   Matching known units can share a side. Different units require separate sides.
   Unspecified units receive separate sides. A plot supports two Y axes.
7. Pan or zoom the plot and press **Fit traces** to return to the complete recording.
8. Toggle visibility using a legend sample. Remove a trace with **Remove selected trace**.
   Removing the final right-axis trace removes its axis and linked view.
9. Switch sources. Unresolved bindings show a missing-signal message and clear their
   curves. Returning to matching descriptors repairs the plot without recreating its bindings.

## Regression checks

```powershell
uv run --locked ruff check .
uv run --locked ruff format --check .
uv run --locked pyright
uv run --locked pytest --cov
$env:QT_QPA_PLATFORM = 'windows'
uv run --locked pytest tests/ui/test_multi_trace_plot.py tests/ui/test_workspace_docks.py tests/ui/test_time_plot.py tests/ui/test_replay_controls.py
```

The plot reuses the #38 renderer's extrema/gap preservation, bounded point budget,
coalesced pan updates and measurement-time window queries. Mixed-rate traces use one
integer origin before conversion to seconds. Source/domain incompatibility and mixed
sample units produce explicit feedback. Raw samples remain in the shared session.

`MultiTracePlot.configuration()` returns JSON-compatible signal/source identities,
units, axis assignments, stable colors and visibility. `restore_configuration()` validates
the complete list before activation and retains unresolved bindings. This is the plot
configuration boundary for #49. Workspace file import/export and restart persistence
remain in #49.
