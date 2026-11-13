# Signal value display

`lbr_dashboard.core.value_display` turns one signal descriptor and its selected
sample into display text (issue #109). It is standard library only and has no
Qt dependency. `lbr_dashboard.ui.value_widget.SignalValueWidget` shows the
result for one signal. The widget is not wired into the main window yet.

```python
display = describe_value(descriptor, sample, now_ns=cursor_ns, max_age_ns=500_000_000)
widget = SignalValueWidget(descriptor.identifier)
widget.show_display(display)
```

## Inputs

`describe_value` reads its inputs through two read-only protocols, so it does
not define a competing signal or source model:

| `DisplayDescriptor` | Meaning |
| --- | --- |
| `identifier` | generic signal ID, the same ID used for signal selection (#36) |
| `display_name` | human-readable name; any Unicode text; the ID is used when empty |
| `source_id` | source that defines the signal |
| `data_type` | `numeric`, `integer`, `boolean`, `text`, `status`, `vector`, or anything else |
| `canonical_unit` | unit text, or `None` / empty for no unit |
| `component_labels` | optional labels for vector components |
| `decimal_places` | optional fixed decimals (0 to 9); default is 6 significant digits |

| `DisplaySample` | Meaning |
| --- | --- |
| `timestamp_ns` | integer nanoseconds on the signal's time axis |
| `value` | the reading |
| `available` | False when the source reports no value |
| `quality` | optional source quality text or unavailability reason |
| `source_id` | optional source of the sample |

`StaticDescriptor` and `StaticSample` are minimal frozen stand-ins for tests and
mock data. The real types come from the signal model (#18). Its descriptor and
sample already use the same attribute names for the shared fields; vector
labels and decimal places can come from its display metadata through a thin
adapter. The aliases `string`, `float`, `int`, and `bool` are accepted for
`text`, `numeric`, `integer`, and `boolean`.

## States

The first matching rule wins:

| State | Shown as | When |
| --- | --- | --- |
| `UNSUPPORTED_TYPE` | `[UNSUPPORTED TYPE]`, `--` | `data_type` is not a supported type; the value is never guessed |
| `NO_DATA` | `[NO DATA]`, `--` | no sample yet |
| `INVALID` | `[INVALID]`, `--` | timestamp is not an integer, or sample `source_id` differs from the descriptor |
| `UNAVAILABLE` | `[UNAVAILABLE]`, `--` | `available` is False; the reason comes from `quality` |
| `INVALID` | `[INVALID]`, `--` | sample time is after `now_ns`, or the value does not match the declared type |
| `STALE` | `[STALE]`, value in italics | age is greater than `max_age_ns` (an age equal to the limit is not stale) |
| `OK` | `[OK]`, value | otherwise |

Age is `now_ns - timestamp_ns`. Without `now_ns` no age is computed and nothing
is stale. Every state is written out as text in the widget and in
`quality_text`, so it never depends on color.

## Value formatting

| Type | Accepted values | Example output |
| --- | --- | --- |
| `numeric` | finite `int` or `float` (not `bool`) | `101.325 kPa`, `0 kPa`, `1.5e+20 kPa` |
| `integer` | `int`, or a whole finite `float` | `0`, `-12`, `1.00000e+20` |
| `boolean` | `bool` only (`0` and `"false"` are invalid) | `true`, `false` |
| `text` | `str`, shown in double quotes | `"0"`, `""` |
| `status` | non-blank `str` label or `int` code, in brackets | `[RUNNING]`, `[code 0]` |
| `vector` | non-empty sequence of finite numbers | `(x=0.10, y=-9.81, z=0.00) m/s^2` |

So numeric zero (`0`), boolean false (`false`), text zero (`"0"`), status code
zero (`[code 0]`), no data, unavailable, and invalid (`--` with a state label)
all look different.

- NaN, infinity, `None`, and wrong Python types are invalid, never zero.
- Units are added to numeric, integer, and vector values only.
- Numbers of magnitude 1e15 and above use scientific notation even when
  `decimal_places` is set.
- Text, status labels, and component labels are cut to 80 characters with
  `...`; control characters are escaped (`\x0a`). Vectors show at most 16
  components followed by `... (+N more)`.
- Times are exact decimal seconds from integer nanoseconds with at least three
  decimals: `1.000 s (age 0.250 s)`.

## Widget

`SignalValueWidget(signal_id)` shows the name, state, value, signal identity,
type, unit (`(none)` when absent), time, and quality. `show_display(record)`
ignores records for other signal IDs and returns False, so one update can be
offered to every widget. `set_signal_id` reassigns the widget and clears it.
The current state is also available as the `valueState` Qt property for
optional styling.

## Not in this slice

- Choosing the sample: nearest-sample query (#21) and the selected replay time
  (#22) provide `sample` and `now_ns`.
- Signal selection UI (#36) and placing the widget in the main window.
- Statistics over an interval (#43).
