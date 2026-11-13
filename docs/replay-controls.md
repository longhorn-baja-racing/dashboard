# Replay controls on the dashboard

The **Session replay** row binds the app's #22 `SessionClock`. Source loading
and the clock are separate: finite CSV/simulator data first enters the shared
session, then its cached measurements can be replayed even when the adapter
does not implement seek or rate control.

## Try it

1. Connect **Simulator - process (finite)** in the source picker.
2. Press **Play**. The selected time advances, and a yellow time marker moves
   across the numeric plot. The time display and marker use the same clock frame.
3. Press **Pause**, change speed (0.25x, 0.5x, 1x, 2x or 4x), then play again.
   Changing speed does not rewrite the original measurements or timestamps.
4. Drag or click the seek slider. Seeking pauses at the requested position.
   Slider arithmetic uses integer nanoseconds; the display includes the exact
   selected source timestamp. A finite slider has 10,000 intervals, so very
   long recordings have coarser slider increments.
5. At the end, playback stops at the recorded bound. **Replay** starts at the
   beginning. Seeking directly to the end and pressing Play also restarts in
   one click. Source Retry creates a new session and resets replay to paused,
   speed 1x.
6. Select a different signal. Selection keeps the same session clock. A cursor
   appears only for numeric/boolean scalar data with a declared matching source
   and resolved clock domain; it never implies cross-device synchronization.
7. Connect **Simulator - process (live)**. Live-follow tracks the latest recorded
   timestamp. Pause detaches from the live head while ingestion continues.
   **Follow live** rejoins it. Speed and seek are disabled for live input.

Empty recordings and pending source preparation/cleanup disable replay controls.
A single timestamp has no playable interval. Disconnect retains finite data for
offline replay, while disconnected live input cannot be followed. Live retention
remains bounded by the session; if paused history is evicted, the clock clamps
to the retained interval.

The time display identifies its reference signal. The first signal with recorded
data becomes that session's reference timeline. Mixed-rate signals keep their
independent samples; playback does not resample or interpolate them.

## Keyboard

When focus is on the dashboard background or plot:

| Key | Action |
| --- | --- |
| Space | Play/pause; resume live-follow for paused live input |
| Left / Right | Seek backward/forward one second and pause |
| Home / End | Seek to the recorded start/end and pause |

Text editors, the signal search box, spin boxes, combo boxes, tree/table
navigation, buttons and the seek slider retain their normal keyboard behavior.
After using a transport button, move focus back to the plot to use the replay
shortcuts. The keypad flag on macOS arrow keys is accepted. Other modified key
combinations are left to the focused widget.

## Verification

`uv run --locked pytest tests/ui/test_replay_controls.py` exercises production
controls with an injected monotonic clock. Tests cover paused-time stability,
speed changes, bounds, replay-end/restart, large/negative timestamps, empty and
single-sample sessions, live-follow transitions, source changes, cursor/domain
rules, text-entry protection and disabled states. The ordinary seek test must
fail when its actual signal binding is disconnected.
