# Frequency spectrum

`lbr_dashboard.core.spectrum` computes the one-sided amplitude spectrum and the
one-sided power spectral density (PSD) of one uniformly sampled numeric signal
with `numpy.fft.rfft`. `lbr_dashboard.ui.spectrum_widget` draws the result, and
**Analysis > Spectrum** (`lbr_dashboard.ui.tools.spectrum`) shows it for the
signal selected in the signal browser.

## Inputs

```python
from lbr_dashboard.core.spectrum import SamplingError, spectrum

try:
    result = spectrum(
        times_ns,  # integer nanoseconds, strictly increasing, uniform
        values,  # floats; None or NaN is a gap and is rejected
        unit="m/s^2",
        window="hann",  # or "none"
        detrend="mean",  # or "none" or "linear"
    )
except SamplingError as error:
    ...  # tell the user why; explicit resampling is required
```

The inputs are the same parallel arrays an importer produces (`array("q")`
times and `array("d")` values), already cut to the interval of interest.

## Processing order

1. Validate the sampling (see below). Nothing is computed for invalid input.
2. Detrend: `none`; `mean` (subtract the mean, the default); or `linear`
   (subtract a least-squares straight line over the sample index).
3. Window: `none` (rectangular) or `hann` (the default). Hann is the periodic
   form `0.5 - 0.5 * cos(2 * pi * k / N)`, `k = 0 .. N - 1`, which is what
   spectral analysis uses (not the symmetric `numpy.hanning`).
4. `X = numpy.fft.rfft(window * detrended)`.

## Scaling

Both outputs are one-sided: every bin except 0 Hz and (for an even sample
count) the Nyquist bin is doubled, because those two have no negative-frequency
partner.

| Output      | Formula                                   | Unit             |
| ----------- | ----------------------------------------- | ---------------- |
| `amplitude` | `one_sided * abs(X) / sum(w)`             | signal unit      |
| `psd`       | `one_sided * abs(X)^2 / (fs * sum(w^2))`  | `unit^2/Hz`      |

`amplitude` is peak amplitude: a sine `A * sin(2 * pi * f * t)` reads `A`, not
the RMS value `A / sqrt(2)`. A constant `c` reads `c` at 0 Hz (with
`detrend="none"`).

`psd_unit` is `unit^2/Hz`, with the unit in parentheses when it has a `/`,
`*`, `^` or space (`(m/s^2)^2/Hz`), and `1/Hz` for a unitless signal.

### Window correction

- Amplitude is divided by `sum(w)`, which corrects the window's coherent gain
  (`coherent_gain = sum(w) / N`: 1.0 for `none`, 0.5 for Hann). A sine exactly
  on a bin reads its true amplitude with either window.
- PSD is divided by `sum(w^2)`, which corrects the window's noise power. White
  noise of variance `s^2` reads a level of `2 * s^2 / fs` with either window.
  `noise_bandwidth_bins` (equivalent noise bandwidth, `N * sum(w^2) / sum(w)^2`:
  1.0 for `none`, 1.5 for Hann) says how many bins one tone is spread over in
  the PSD.
- Without a window and without detrend, Parseval holds exactly:
  `sum(psd) * resolution_hz == mean(x^2)`.

Use `amplitude` to read tones and `psd` to compare broadband noise between
recordings of different length or rate. Do not read a tone's amplitude from the
PSD or a noise level from the amplitude spectrum: each is only correct for the
signal type it is scaled for.

## Resolution and documented tolerance

- `sample_rate_hz = 1e9 * (N - 1) / (t_last - t_first)` (mean spacing).
- `resolution_hz = sample_rate_hz / N`; bins sit at `k * resolution_hz`.
- `nyquist_hz = sample_rate_hz / 2`; content above it aliases and cannot be
  detected or undone here.
- `peak` is the largest-amplitude bin outside the DC main lobe (bin 0 without a
  window, bins 0 and 1 with Hann, where an un-removed offset leaks). Its
  frequency is the bin centre; there is no interpolation.

Tolerances the tests check:

| Case                         | Frequency                | Amplitude                     |
| ---------------------------- | ------------------------ | ----------------------------- |
| sine exactly on a bin        | within `resolution / 2`  | relative 1e-9 (any window)    |
| sine half way between bins   | within `resolution`      | low by up to 36.3 % (`none`)  |
|                              |                          | low by up to 15.1 % (`hann`)  |
| white noise PSD level        |                          | within 3 % for 65536 samples  |

The half-bin loss is scalloping (-3.92 dB rectangular, -1.42 dB Hann). It is
why Hann is the default: off-bin tones read closer to their true amplitude and
leak far less into distant bins, at the cost of a 1.5x wider peak.

## Sampling rules

The FFT assumes equally spaced samples with no gaps. Anything else is rejected
with `SamplingError` (a `ValueError`) and a message saying what is wrong and
that explicit resampling is required. Nothing is silently resampled,
interpolated, filled or dropped.

| Rejected input                          | Why                                         |
| --------------------------------------- | ------------------------------------------- |
| fewer than `MIN_SAMPLES` (8) samples    | too few bins to be meaningful               |
| any `None`, NaN or +/-inf value         | a gap; zero-filling would invent data       |
| non-integer or bool timestamp           | never rounded or truncated to whole ns      |
| timestamp outside the int64 range       | not representable as int64 nanoseconds      |
| duplicate or decreasing timestamp       | no defined sample order or spacing          |
| spacing jitter above `max_jitter`       | nonuniform timing or a dropped sample       |

Jitter is `max(abs(dt_i - mean_dt)) / mean_dt` over all spacings. The default
tolerance is `DEFAULT_MAX_JITTER = 0.01` (1 % of the spacing). That accepts
integer-nanosecond rounding of any realistic rate (for example 3 kHz is
333333.33 ns, rounded to whole ns) and small clock wobble, and rejects any
dropped sample (that spacing is about 100 % too long). A caller can pass a looser `max_jitter` (below 0.5); the
measured value is always returned as `Spectrum.max_jitter`.

`SamplingError.index` is the sample the problem was found at (the first
missing value, the first out-of-order timestamp, or the sample after the worst
spacing), or None for too few samples or a bad timestamp type.

Option errors (unknown window or detrend, bad `max_jitter`, mismatched lengths)
raise a plain `ValueError`, not `SamplingError`.

## Result

`Spectrum` is frozen. `frequencies_hz`, `amplitude` and `psd` are read-only
numpy arrays of length `N // 2 + 1`. It also carries `unit`, `sample_rate_hz`,
`sample_count`, `window`, `detrend`, `coherent_gain`, `noise_bandwidth_bins`,
`max_jitter`, `start_ns`, `end_ns` and `scaling` (the rule above as text),
plus the derived `resolution_hz`, `nyquist_hz`, `amplitude_unit`, `psd_unit`
and `peak` (`frequency_hz`, `amplitude`, `psd`).

## Widget

`SpectrumWidget.show_spectrum(result, name, mode="amplitude")` plots amplitude
(or, with `mode="psd"`, the PSD) against frequency in Hz, labels the y axis
with the unit, marks the peak, and prints the scaling (what the y values are
and which window correction they carry), sample rate, resolution, Nyquist,
sample count, window, detrend, measured jitter and the peak under the plot.
`show_error(name, message)` hides the plot and shows the `SamplingError`
message in its place, so a rejected selection never looks like a result.
`clear()` resets the view. The plot title follows the axis text color, so a
themed plot keeps a readable title.

## Analysis > Spectrum

The tool opens as a dock and follows the shared analysis state:

- Signal: the one selected in the signal browser. Non-numeric signals (boolean,
  string, event, vector, status) show a note instead of a plot.
- Range: the shared time interval (Analysis > Select visible time range, then
  drag its edges). With no interval it uses the whole session. The line above
  the options names the signal, the range and the number of samples in it.
- Options: **Window** (Hann or none), **Detrend** (remove mean, remove linear
  trend, or none) and **Scale** (amplitude or PSD).
- Invalid sampling (too few samples, missing readings, gaps or irregular
  spacing) hides the plot and shows the reason, with the session time of the
  offending sample where there is one. Nothing is resampled.
- The plot uses the application theme and restyles when the theme changes.
- It recomputes at most every 100 ms while a source streams. The replay cursor
  does not affect it.

`SpectrumTool.configuration()` returns `{"window", "detrend", "scale"}` and
`restore_configuration(config)` applies one. Missing keys take the defaults
(`hann`, `mean`, `amplitude`); unknown keys are ignored; an unknown value or a
non-dict raises `ValueError` and changes nothing.

## Try it (Windows)

From the repository root in PowerShell, with the development environment set
up as in the README:

```powershell
.venv\Scripts\python -m lbr_dashboard
```

1. In the source picker choose **Simulator - process (finite)** and press
   Connect. It produces 3 s of fictional data.
2. In the signal browser select `process.pressure` (100 Hz, Pa, a repeating
   0, 5, 8, 3 Pa cycle).
3. Open **Analysis > Spectrum**. With no interval it covers the whole session,
   which has null readings at 0.5 s and dropped readings at 0.2 s and 0.7 s,
   so the dock shows "no spectrum" with the reason (missing values at
   0.500 s) instead of a plot.
4. Zoom the main plot to roughly 1 s to 3 s and press **Ctrl+I** (Analysis >
   Select visible time range). The dock now plots a single peak near 25 Hz
   (a quarter of the 100 Hz rate) of about 4.1 Pa (`sqrt(17)`; slightly lower
   when the interval is not a whole number of cycles), with sample rate
   100 Hz, Nyquist 50 Hz, resolution about 0.5 Hz and jitter 0.000 %.
5. Switch **Scale** to PSD to see the same peak in `Pa^2/Hz`, and **Window**
   to none to compare leakage. Drag the interval over 0.7 s to see the gap
   rejected.

`process.temperature` (1 Hz, 4 samples) shows the too-few-samples message.

## Limitations

- One FFT over the whole selection. No Welch averaging, overlap or
  spectrogram, so a single PSD estimate is noisy (each bin is roughly
  chi-squared with 2 degrees of freedom).
- No peak interpolation and only `none`/`hann` windows. Flat-top windows for
  more exact off-bin amplitude can be added when needed.
- No resampling. A signal with gaps or irregular timing needs an explicit
  resampling step that the user chooses and sees.
- The tool computes one signal at a time, over at most one interval.
