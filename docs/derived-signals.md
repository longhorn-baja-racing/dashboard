# Derived signals

A derived signal is computed from other signals with a short arithmetic
expression, for example the magnitude of an acceleration vector or a pressure
ratio. The engine is `lbr_dashboard.core.derived` (standard library only). It
works on plain timestamp/value sequences, so it does not depend on any source
type. In the app, **Analysis > Derived signals** defines, previews and applies
derived signals (see [Using derived signals in the app](#using-derived-signals-in-the-app)).

## Safety

Expressions are data, never code. The text is parsed with Python's `ast`
module (`mode="eval"`) only to get a syntax tree. Every node is checked against
the allowlist below and converted into a few plain objects that the module
evaluates itself. `eval`, `exec` and compiling to a code object are never used,
so names such as `__import__`, attribute access, lambdas and comprehensions are
rejected before anything runs. The tests prove this with traps on `eval`,
`exec`, non-AST `compile`, `os.system`, imports and file creation.

Limits: at most 1000 characters, a nesting depth of 32 and 200 syntax nodes.
The expression must be a single line of printable ASCII.

## Grammar

```
expression := term (("+" | "-") term)*
term       := factor (("*" | "/") factor)*
factor     := "-" factor | number | signal | call | "(" expression ")"
signal     := sig("<signal id>")
call       := abs(e) | sqrt(e) | min(e, e, ...) | max(e, e, ...) | hypot(e, e, ...)
```

- Numbers are Python int/float literals (`2`, `0.5`, `1e-3`). Booleans,
  complex numbers, `None` and values that overflow to infinity are rejected.
- `sig("<signal id>")` references an input by its generic signal id. The id
  is a quoted string, so dotted or punctuated ids work as is:
  `sig("process.temperature")`, `sig("can:0x18F/hydraulics-pressure")`.
  The id must not be empty or padded with spaces. Text is allowed nowhere else.
- Functions take positional arguments only. `min`, `max` and `hypot` take two
  or more arguments.
- An expression must reference at least one signal (the output needs
  timestamps).

Everything else is rejected, including `**`, `%`, `//`, unary `+`, comparisons,
`and`/`or`/`not`, conditional expressions, attribute access, indexing, other
function names, keyword or starred arguments, lambdas, comprehensions,
f-strings, tuples, lists and dicts.

## Errors

`ExpressionError` (a `ValueError`) names the problem and points at the
offending text with a 1-based column, for example:

```
attribute access is not allowed at column 1: sig("a").real
unknown name 'x'; reference signals as sig("<id>") at column 12: x
unknown function '__import__'; allowed: sig, abs, sqrt, min, max, hypot at column 1: __import__
```

The error also has `start`, `end` and `segment` for an editor to highlight.
Other errors:

- `UnitError` (subclass of `ExpressionError`): malformed or incompatible
  units, or a declared unit that does not match.
- `DefinitionError`: invalid definition fields or dicts, duplicate output ids,
  output ids that clash with raw signal ids.
- `CycleError` (subclass of `DefinitionError`): definitions that use each
  other in a loop; `cycle` lists the ids, for example `x -> y -> x`.
- `UnresolvedInputError`: evaluation without data for an input; `missing`
  lists the ids.

## Units

A unit is a string of factors joined by `*` and `/`, read left to right, with
optional integer powers: `m/s^2`, `m/s/s`, `m*s^-2`, `kg*m^2/s^2`, `N*m`,
`deg C`, `%`, `1/s`. Units are compared after normalization (sorted factors,
combined powers), so `m/s/s` and `m*s^-2` are the same unit `m/s^2`.

- `"1"` is dimensionless.
- `None`, `""` and `"unknown"` mean the unit is unknown.
- Names are not converted or related to each other: `km` and `m`, or `K` and
  `degC`, are different units. There is no scaling between them.

Rules:

| Operation | Rule |
| --- | --- |
| `a + b`, `a - b` | identical units, else `UnitError`; result has that unit |
| `a * b`, `a / b` | powers add or subtract: `N` * `m` -> `N*m`, `m` / `s` -> `m/s` |
| `-a`, `abs(a)` | unit of `a` |
| `sqrt(a)` | halves every power; odd powers (`sqrt` of `m`) are a `UnitError` |
| `min`, `max`, `hypot` | all arguments identical units; result has that unit |
| number | dimensionless |

Constants adopt the other side's unit in `+`, `-`, `min`, `max` and `hypot`,
so `sig("t") - 273.15` keeps the unit of `t`. A constant expression (only
numbers) counts as a constant.

Unknown units propagate: any operation with an unknown operand gives an
unknown result, and no compatibility check is possible for it.

Each definition may declare its output unit. `None` accepts the inferred unit.
A declared unit must normalize to the inferred unit, and it cannot be confirmed
(so it is rejected) when the inferred unit is unknown. Unit checks run before
any sample is computed, so incompatible units never produce partial output.

## Alignment

Inputs usually have different rates. The policy is sample-and-hold at the
timestamps of one reference input:

- The output has one sample per reference sample (in the evaluation window),
  at the same timestamp. The reference is `reference_id`, by default the first
  input.
- Every other input contributes its latest sample at or before that time, if
  that sample is at most `max_age_ns` old. The default `max_age_ns=0` means an
  exact timestamp match is required. Inputs never look ahead.
- When several samples share a timestamp, the last one in input order is held.
  The reference itself uses each of its own samples.
- There is no interpolation and no resampling.

Example, reference `a` at 0, 10, 20, 30 and `b` at 0 (2.0), 25 (3.0), with
`max_age_ns=10`:

| t | `b` used | Output of `sig("a") * sig("b")` |
| --- | --- | --- |
| 0 | 2.0 | computed |
| 10 | 2.0 (10 ns old) | computed |
| 20 | none (20 ns old) | missing: `stale_input`, `b` |
| 30 | 3.0 (5 ns old) | computed |

`evaluate(..., start_ns=, end_ns=)` limits output to reference samples with
`start_ns <= t <= end_ns`; other inputs may still be held from before
`start_ns`.

## Missing outputs

A sample that cannot be computed stays in the output with value `None` and a
`Missing` record (`reason`, `signal_id`, `where`). Values are never invented,
and evaluation does not raise for per-sample problems.

| Reason | When |
| --- | --- |
| `missing_input` | the selected input sample is `None`, NaN, +/-inf or a number too large for a float (such as `10**400`); there is no fallback to an older sample |
| `stale_input` | an input has no sample at or within `max_age_ns` before `t` |
| `division_by_zero` | the right side of `/` is zero |
| `domain_error` | `sqrt` of a negative number |
| `non_finite` | an intermediate result overflowed to +/-inf |

For input problems the first failing input in `inputs` order is reported. For
arithmetic problems `where` is the failing part of the expression, for example
`1 / (sig("a") - 2)`.

Input sequences are read, never modified or copied. Each evaluation returns a
new `DerivedSeries` with the output id, unit, times, values, missing reasons,
reference id and `max_age_ns`.

## Definitions

`DerivedSignalDefinition` is frozen:

| Field | Meaning |
| --- | --- |
| `output_id` | id of the derived signal |
| `name` | display name |
| `expression` | expression text |
| `unit` | declared output unit or `None` |
| `inputs` | signal ids used, by default in order of first use; if given it must list exactly the ids in the expression |
| `reference_id` | input that supplies timestamps (default: first input) |
| `max_age_ns` | hold limit for the other inputs, integer >= 0 |

Definitions may use each other's outputs. `order_definitions` returns them in
dependency order and raises `CycleError` for loops, including a definition that
uses its own output. `evaluate_all` evaluates such a set over raw inputs; a
derived input carries the output unit and its missing samples become
`missing_input` downstream. All outputs share the window, so derived inputs
have no samples before `start_ns`.

`to_dict()` returns a JSON-compatible dict with `version: 1` and every field.
`from_dict()` requires exactly those keys with the right types (no booleans
for numbers) and validates the expression again.

## Using derived signals in the app

Open **Analysis > Derived signals**. The dock has a form for one definition,
a live preview and the list of applied derived signals.

1. Enter a name. The id is filled in as `derived.<name>` (lowercase, `_`
   between words) until you edit it; it must be a valid signal id and must not
   be the id of a recorded signal.
2. Write the expression. Pick a numeric signal in the list and press
   **Insert signal** to add `sig("<id>")` at the cursor, for example
   `sig("csv.column.2") * 2` or `sig("csv.column.1") - 20` with the CSV demo.
3. Optionally declare the unit (blank uses the unit the expression gives).
   Units are written without spaces, as everywhere in the app (`degC`).
4. Choose the input whose timestamps the output uses (**Timestamps from**) and
   how long the other inputs may be held (**Hold other inputs**, in ms; 0 means
   exact timestamp match). This is the alignment policy described above.

The preview updates as you type. It shows the sample and missing counts, the
unit, the minimum and maximum, each input with its unit and sample count, and
the first rows with the reason for every missing sample. It evaluates at most
the first 20000 reference samples. Problems are shown instead and **Apply**
stays disabled: unknown signals, text, status or vector inputs, inputs from
different sources or clock domains, unit mismatches, cycles between derived
signals and anything outside the grammar, such as `__import__("os")`. Input
units the unit notation cannot read (for example the simulator's degree
Celsius symbol) are treated as unknown and listed as a note.

**Apply** adds the signal (or replaces the one being edited). Applied signals:

- appear in the signal browser under `derived`, with the expression, inputs,
  unit and alignment in the details below the tree;
- plot on the main time plot, work with the replay cursor and value docks,
  and can be used by the other analysis tools and as inputs of further derived
  signals;
- use the reference input's source and clock domain and keep missing samples
  missing (unavailable readings with quality `missing: <reason>`).

Recorded signals are never changed: the source controller evaluates the
definitions into each snapshot it shows and adds the derived descriptors and
series beside the recorded ones. A definition is evaluated again only when the
records of one of its inputs changed. While a live source streams, evaluation
runs at most every 250 ms, and while a replay source is still loading it waits
until loading finishes.

**Edit** loads the selected definition into the form; **Remove** deletes it.
Definitions belong to the app run, not to the source, so they are evaluated
again after reloading or switching sources. A definition whose inputs are not
in the loaded session stays listed and shows in the browser as unavailable with
the reason (for example `unknown signal 'csv.column.9'`).

### Saving definitions

`AnalysisContext.derived` (a `DerivedSignalSet` from
`lbr_dashboard.core.derived_session`) holds the applied definitions.
`configuration()` returns a JSON-compatible dict:

```
{"version": 1, "definitions": [<DerivedSignalDefinition.to_dict()>, ...]}
```

`restore_configuration(config)` replaces every definition from such a dict,
all or nothing: a wrong version, malformed definitions, invalid ids, duplicate
ids and cycles raise `DefinitionError` and keep the current definitions.
Missing inputs are not an error; those signals are shown as unavailable until
a session provides the inputs. Workspace files can store this dict as is.

## Running the tests

On Windows (PowerShell) or any other platform, from the repository root:

```
uv run --locked --extra dev pytest tests/core/test_derived.py tests/core/test_derived_session.py tests/ui/test_derived_signals_tool.py
```

To try it by hand, run `uv run --locked lbr-dashboard`, open
`sample_data\timestamped_demo.csv` with the source picker and use
**Analysis > Derived signals**.
