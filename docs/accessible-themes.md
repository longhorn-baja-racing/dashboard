# Dashboard themes

Select **System**, **Light** or **Dark** from the theme menu. The preference is
saved through the local preferences store. System uses the application palette.
The two explicit themes define their own colors.

In Light and Dark, selected rows have distinct foreground and background
colors, controls get a visible keyboard focus border, and disabled controls
remain readable. Plot labels and tick marks use the theme foreground. The
trace and cursor colors stay stable when switching themes in the primary and
docked plots. The signal browser still labels availability in text.

The integration includes the preferences (#126) and shared cursor (#167)
branches. Run
`uv run --locked pytest tests/ui/test_accessible_theme.py` to verify color
contrast, trace stability and restoring a saved theme.
