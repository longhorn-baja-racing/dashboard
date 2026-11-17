# Workspace docks

Use **Workspace > Add plot widget** or **Add value widget** to create a native
dock. Move it by its title bar. Closing it hides it. Its entry in the Workspace
menu shows it again with the same signal binding. **Remove widget** deletes it.

Drag one available signal from the signal browser into a dock. To bind with the
keyboard, select the signal in the browser, tab to the dock's **Bind selected
signal** button and press Space. Plot docks accept scalar numeric and boolean
signals. Value docks show the latest recorded value, including unavailable
readings as such. Plot docks show up to the latest 1,000 retained readings.

The binding uses both the signal and source IDs. An unavailable source is
reported in the dock. Dropping a missing, incompatible or malformed payload
leaves the prior binding intact. Layout and binding persistence belong to
issue #49. Shared replay cursor behavior belongs to issue #40.
