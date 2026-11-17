# Dashboard themes

Select **System**, **Light** or **Dark** from the theme menu. The preference is
saved through the local preferences store. System uses the application palette.
The two explicit themes define their own colors.

In Light and Dark, selected rows have distinct foreground and background
colors, controls get a visible keyboard focus border, and disabled controls
remain readable. Plot labels and tick marks use the theme foreground. The
trace color stays stable when switching themes. Selection and status still have
text labels.

The current change is based on the preferences PR (#126). Plot styling on the
newer time-plot and signal-browser stack still needs integration. Run
`uv run --locked pytest tests/ui/test_accessible_theme.py` to verify color
contrast, trace stability and restoring a saved theme.
