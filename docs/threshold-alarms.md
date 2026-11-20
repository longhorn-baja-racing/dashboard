# Threshold alarms

`lbr_dashboard.core.thresholds` evaluates user-defined thresholds on any scalar
signal. It is standard-library Python with no Qt, no session model, and no
knowledge of specific sensors: a rule names a signal by its ID and the caller
passes that signal's samples.

Evaluation is read-only. It never sends commands to the vehicle or any device,
never writes files, and keeps no state between calls. The output is a list of
state changes that a display can show. In the app, rules are edited and shown
with **Analysis > Thresholds**; see [Thresholds tool](#thresholds-tool).

## Inputs

`evaluate(rule, times_ns, values, *, end_ns=None)` takes parallel sequences:

- `times_ns`: integer nanoseconds, non-decreasing. Negative times are allowed.
  `array("q")` from an importer works as is.
- `values`: numbers in the signal's own unit. `None`, NaN, and +/- infinity are
  all treated as missing. `array("d")` works as is.
- `end_ns`: where the data ends (the replay end, or "now" when live). It must
  not be before the last sample. It only matters for staleness after the last
  sample.

All intervals are closed: a sample at exactly an interval edge is inside it.

## Rule fields

| Field | Meaning |
| --- | --- |
| `rule_id` | Unique, non-empty ID for the rule (no surrounding whitespace) |
| `signal_id` | ID of the signal the rule applies to |
| `comparison` | `"above"` or `"below"` |
| `level` | Finite threshold, in the signal's unit |
| `hysteresis` | Finite, >= 0, in the signal's unit (default 0) |
| `debounce_ns` | Integer >= 0 (default 0) |
| `severity` | `"info"`, `"warning"` (default), or `"critical"` |
| `stale_after_ns` | Integer > 0, or `None` to disable staleness (default) |
| `label` | Free text (default empty) |
| `unit` | Free text copy of the signal's unit, for display and later checks |

Evaluation does not convert units. `unit` is stored so a saved rule stays
readable and can later be checked against the signal it is applied to.

`describe()` returns a plain sentence, for example
`Above 95 deg C for 0.5 s, clears below 93 deg C for 0.5 s, unknown after 2 s without data`.

## States

Each rule is `NORMAL`, `ACTIVE`, or `UNKNOWN`. Evaluation starts `UNKNOWN`,
and the state is `UNKNOWN` before the first reported transition. Missing or
stale input always makes the state `UNKNOWN`; it can never become `NORMAL`
without a valid value.

The result is a tuple of `AlarmTransition(time_ns, state, value, reason)` with
strictly increasing times. It lists changes only. `value` is the sample that
caused the change (`None` for missing or stale). `reason` is one of
`crossed` (-> `ACTIVE`), `cleared` (-> `NORMAL`), `missing`, or `stale`.

## Crossing and hysteresis

For `"above"` rules, with `clear_level = level - hysteresis`:

- `value > level` means the alarm condition (strict: a value exactly at the
  level does not activate).
- `value < clear_level` means clear (strict: a value exactly at the clear level
  does not clear).
- Anything in between (the hysteresis band, both ends included) keeps the
  current state. With zero hysteresis the band is the level itself.

`"below"` rules mirror this: condition `value < level`, clear
`value > level + hysteresis`.

## Debounce

A change between states happens only after its condition has held without
interruption for `debounce_ns`, measured with sample timestamps. The change is
reported at the first sample where `time - first_sample_of_the_run >=
debounce_ns`, not at the first crossing. With sparse samples this may be later
than `first crossing + debounce_ns`. A value that no longer meets the condition
(including a value in the hysteresis band) restarts the run. Debounce applies
to activating, clearing, and leaving `UNKNOWN`. It never delays entering
`UNKNOWN`.

## Missing and stale input

- A missing value makes the state `UNKNOWN` at that sample's time.
- With `stale_after_ns` set, a sample stays valid through
  `time + stale_after_ns` (closed). If the next sample, or `end_ns`, is later
  than that, the state becomes `UNKNOWN` at `time + stale_after_ns + 1`, the
  first nanosecond without valid data. A gap of exactly `stale_after_ns` is
  still fresh.
- Missing or stale input cancels any debounce run in progress.

## Leaving UNKNOWN

After `UNKNOWN`, a valid value is compared against the level only: past the
level means `ACTIVE`, anything else (including the hysteresis band) means
`NORMAL`. Debounce applies, so after a gap the signal must hold for
`debounce_ns` before any state other than `UNKNOWN` is reported.

## Duplicate timestamps

Samples that share a timestamp are applied in order (so they still affect
hysteresis and debounce), but only the state after the last of them is
reported for that time. A zero-length state is never reported, and two
transitions never share a time.

## Live, replay, and seeking

`evaluate` is pure: the same inputs always give the same transitions. For
replay, evaluate the whole signal once with `end_ns` set to the session end.
For live data, call it again with `end_ns` set to the current time.

`state_at(transitions, t_ns)` looks up the state at any time with a binary
search; a transition exactly at `t_ns` counts. Seeking forward or backward
gives the same answer as evaluating only the samples at or before `t_ns` with
`end_ns=t_ns`. `transition_at` returns the transition in effect.

## Acknowledgement

An acknowledgement is keyed by `(rule_id, activation time_ns)` and stored in a
plain `frozenset`:

- `acknowledge(acks, rule_id, transitions, activation_ns)` returns a new set.
  It rejects a time that is not the start of an `ACTIVE` transition.
- `is_acknowledged(acks, rule_id, transitions, t_ns)` is true only while the
  alarm is `ACTIVE` at `t_ns` and that activation was acknowledged.

Acknowledging one activation never covers a later one. That includes an alarm
that comes back after a brief `UNKNOWN`. Acknowledgement does not change the
evaluated state.

## Saved definitions

`rules_to_json(rules)` writes an ASCII-only, versioned document;
`rules_from_json(text)` reads it back:

```json
{
  "format": "lbr-dashboard-thresholds",
  "version": 1,
  "rules": [
    {
      "rule_id": "pressure-high",
      "signal_id": "line_pressure",
      "comparison": "above",
      "level": 250.0,
      "hysteresis": 10.0,
      "debounce_ns": 250000000,
      "severity": "critical",
      "stale_after_ns": 1000000000,
      "label": "Line pressure",
      "unit": "kPa"
    }
  ]
}
```

Reading is strict and raises `ThresholdFileError` (a `ValueError`) whose
message, `index`, and `field` locate the problem, for example
`rules[2].level: must be a finite number`. It rejects:

- input over 1 MiB of text or that is not valid JSON;
- a wrong `format`, a non-integer `version`, or a version other than 1;
- unknown fields at the top level or in a rule;
- a key that appears twice in one object;
- missing `rule_id`, `signal_id`, `comparison`, or `level` (the other fields
  are optional and use the defaults above);
- wrong types, unknown comparison or severity, negative hysteresis or debounce;
- NaN, infinity, or numbers too large for a float;
- duplicate `rule_id` values.

Unknown fields are rejected rather than ignored, so a misspelled field (for
example `hysterisis`) cannot silently fall back to a default. A new field
needs a new format version.

## Thresholds tool

**Analysis > Thresholds** opens a dock (`lbr_dashboard/ui/tools/thresholds.py`)
that edits rules and evaluates them on the loaded session. It only reads the
shared session snapshot: it never sends anything to a source or vehicle.

### Editing rules

The editor on the right of the dock has one field per rule setting:

| Field | Rule field |
| --- | --- |
| Signal | `signal_id`; only scalar numeric signals of the session are offered |
| Comparison | `comparison` (`above`: alarm when value > level; `below`: value < level) |
| Level | `level`, in the signal's unit |
| Hysteresis | `hysteresis`; the computed clear level is shown below it |
| Debounce | `debounce_ns`, entered in seconds |
| Validity window | `stale_after_ns`, entered in seconds; `off` (0) disables staleness |
| Severity | `severity` |
| Name | `label`, shown in the list |

**Add rule** creates a rule from the editor. Selecting a rule in the list loads
it into the editor; **Update rule** replaces it and **Remove rule** deletes it.
The rule's `unit` is copied from the signal's unit. While no rule is selected
the editor follows the signal selected in the signal browser.

### States

Each rule is shown with one of four states. The state is spelled out in the
first column with a text marker and a standard icon; the cell color only
repeats it:

| Marker | State | Meaning |
| --- | --- | --- |
| `[ok]` | Normal | the last valid value is inside the limit |
| `[!]` | ACTIVE | the alarm condition holds; the text also says whether it is acknowledged |
| `[?]` | Unknown | missing value, no data yet, signal absent or not numeric, or unit mismatch |
| `[~]` | Stale | no sample within the validity window |

**Since** gives the session time of the transition that started the state, and
**Details** explains it (the value that crossed, `missing value`, or the time
of the last sample before the data went stale). A rule whose signal is missing
from the session, is not numeric, or whose saved unit differs from the
signal's unit is Unknown, never Normal.

### Replay and live

Rules are evaluated over the whole snapshot with `end_ns` set to the newest
sample in the session, then looked up with `state_at`:

- **Replay:** at the replay cursor. Seeking forward or backward gives the same
  state as playing to that time.
- **Live:** while the replay bar follows live data, at the newest sample in the
  session; a signal that stops reporting while others continue goes Stale. A
  paused live stream is evaluated at the cursor, like replay.

The line above the list says which time is used.

### Acknowledgement

Select an ACTIVE rule and click **Acknowledge**. The acknowledgement belongs to
that activation (the rule ID and its start time): seeking back into it shows it
acknowledged again, and a later activation, including one after a brief
Unknown, needs its own acknowledgement. Editing or removing a rule, importing
rules, or loading another session clears acknowledgements; they are not saved.

### Lines on the main plot

When the main time plot shows a signal that has rules, each rule's level is
drawn as a horizontal dashed line in the severity color (info blue, warning
amber, critical red), and its clear level as a dotted line when it has
hysteresis. Hovering a line names the rule. The lines are drawn again whenever
the plot is redrawn, for example after selecting another signal and coming
back.

### Import, export and configuration

**Export...** writes all rules to a JSON file in the format above;
**Import...** reads such a file and replaces the current rules. An invalid file
is rejected with its error message (for example `rules[0].levle: unknown
field`) and the current rules are kept.

`ThresholdsTool.configuration()` returns the same document as a JSON object,
and `restore_configuration(config)` restores it (raising `ValueError` when it
is invalid), so a workspace can store the rules.

### Run it on Windows

From the repository folder in PowerShell:

```powershell
uv sync --locked --extra dev
uv run --locked lbr-dashboard
```

1. In **Telemetry source**, choose **Simulator - process (finite)** and click
   **Open CSV / Connect**. Select `process.temperature` in the signal browser.
2. Open **Analysis > Thresholds**. With Signal set to
   `temperature (process.temperature)`, choose **Above**, level `20.5`,
   hysteresis `1`, and click **Add rule**. The clear level reads
   `clears below 19.5` followed by the signal's unit.
3. The main plot shows a dashed line at 20.5 and a dotted line at 19.5.
4. Move the replay slider. The temperature is about 20.07, 21.00, 18.93 and
   19.99 at 0, 1, 2 and 3 s, so the rule is Normal since 0.000 s before 1 s,
   ACTIVE since 1.000 s from 1 s, and Normal since 2.000 s from 2 s.
5. Seek to about 1.5 s, select the rule and click **Acknowledge**. The state
   reads `[!] ACTIVE - acknowledged`. Seek past 2 s and back: it is still
   acknowledged.
6. Add a rule on `process.pressure`, **Above** `6` with a validity window of
   `0.015` s. Around 0.21 s it is Stale (records 200 to 240 ms are lost), and
   from 0.5 s to 0.54 s it is Unknown (unavailable readings).
7. Click **Export...**, save `thresholds.json`, remove the rules, and use
   **Import...** to load them again.
8. Choose **Simulator - process (live)** and connect: with the replay bar
   following live, the line above the list reads `Following live data`.

## Not yet included

- Storing rule files and acknowledgements with a saved session or workspace.
- Source-aware freshness from source health.
- Latching and audio.
