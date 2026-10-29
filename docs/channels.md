# Channel combination

The analysis trace — the series dF/F0 and Z-score are computed from — is built from the
corrected channels by one of three combinations. Everything on this page assumes each
channel has already been corrected on its own (`corrected = F0 + [(raw − offset) − F(t)]`),
so `C_analysis` and `C_reference` below are corrected traces, not raw ones.

| Mode | Combination | Weight | Mirrors |
| --- | --- | --- | --- |
| `rwd` (**default**) | `C_analysis − (slope·C_reference + intercept)` | fitted from the recording | RWD OFRS motion correction |
| `difference` | `C_analysis − w·(C_reference − F_reference(t₀))` | Reference Weight field | — |
| `ratio` | `C_analysis / C_reference` | none | classic isosbestic ratio |

With **Reference Wavelength** set to `None` the combination is skipped whatever the mode
says, and the analysis trace is the corrected analysis channel. A recording that contains
only one wavelength can therefore be analysed on its own.

## RWD motion fit (`rwd`, default)

Reproduces what the RWD OFRS software does to 470 against 410, as described under
`Pre-processing algorithm description` → `Motion Correction` in its manual:

```
slope, intercept = robust linear fit of C_analysis on C_reference   ("robust linear fit")
motion(t)        = C_analysis(t) − (slope · C_reference(t) + intercept)
analysis(t)      = motion(t) + F0₁
dF/F0(t)         = 100 · (analysis(t) − level) / F0₁ ,   level = mean of the baseline interval
```

`F0₁` is the analysis channel's F0, its fitting baseline minus the offset, and it is the same
denominator every other trace uses, so the whole recording is expressed on one declared scale.

RWD divides the same motion-corrected counts by `median(Ffull)`, *the median value of the
full-time raw fluorescence of interest*. The numerator is identical; only the divisor differs.
Entering `median(Ffull)` plus the offset as the fitting baseline reproduces RWD's scale exactly
(`median(Ffull)` is shown in the run details), and any other F0 gives the same curve multiplied
by `F0 / median(Ffull)`.

The fit is a soft-L1 (`soft_l1`) least-squares line, i.e. RWD's `robust linear fit`; no
weight has to be supplied, because the slope and the intercept both come from the data.

Validation: with the divisor set to `median(Ffull)`, the trace agrees with a literal
re-implementation of the manual's formula to 0.018 % dF/F0 over a synthetic recording
(300-count event, motion artifact 2.3 × stronger on 410) — 0.12 % of the event peak — with the
same artifact residual and the same noise standard deviation. The robust loss matters: an
ordinary least-squares slope leaves 0.64 % of the artifact where the robust fit leaves −0.43 %.

The axis label and the event-analysis signal list show the fitted slope
(`RWD motion-corrected 470 (slope=0.5377)`), and the slope, intercept and sample count are
written to `parameters_and_marker_edits.json` under `fit_details.rwd_motion_fit`.

## Difference (`difference`)

```
analysis(t) = C_analysis(t) − w · (C_reference(t) − F_reference(t₀))
dF/F0(t)    = 100 · (analysis(t) − analysis₀) / analysis₀ ,   analysis₀ = F_analysis(t₀)
```

Only the deviation of the reference is removed, so the trace keeps the level of the analysis
channel and dF/F0 divides by `F_analysis(t₀)` as usual. `w` is the **Reference Weight** field:

- blank → `w = F_analysis(t₀) / F_reference(t₀)`, the matched scale. To first order this
  reproduces the ratio while never dividing by the reference.
- a number → that weight. Use it when the reference reacts to artifacts more or less strongly
  than its own level implies.
- `w = 0` → the reference is ignored (equivalent to no reference).

Only this mode uses the weight field; `rwd` fits its own slope and `ratio` needs none.

## Ratio (`ratio`)

```
analysis(t) = C_analysis(t) / C_reference(t)
dF/F0(t)    = 100 · (analysis(t) − level) / F0 ,   F0 = F0₁ / F0₂
```

Both corrected traces are already on the `raw − offset` scale, so the division happens after
each channel has had its own background removed. The ratio's own F0 is the ratio of the two
F0s, which is the level that trace sits on when both baselines are right.

Equivalent to `100 · [(1+a)/(1+b) − 1]` with `a` and `b` the relative deviations of the two
channels, so it removes a fluctuation proportional to each channel's own level.

| | Ratio | Difference |
| --- | --- | --- |
| Removes multiplicative common mode (LED, coupling) | Yes | Yes, at matched scale |
| Removes additive artifacts (motion) | Yes, at matched scale | Yes, tunable through `w` |
| Behaviour when the reference dips | Can cross zero and raise an error | Always finite |
| Reference noise injected into dF/F0 | Multiplicative, full weight | Additive, weight `w` |
| Large-response behaviour | Non-linear and asymmetric | Linear |
| Reference stronger than its level implies | Over-corrects, can turn a positive deflection negative | Fixed by lowering `w` |
| Extra parameter | None | `w` |

The ratio assumes the artifact on the reference scales with the reference level. When the
reference is more sensitive than that — common for an isosbestic channel, which picks up
scattering and blood-volume changes — the ratio subtracts too much and can report a negative
deflection where the analysis channel actually rose. The difference combination with a fitted
weight removes that failure mode without touching the signal amplitude; the RWD motion fit
does the same thing with a fitted intercept as well.

## Choosing the reference weight

Leave **Reference Weight** blank to start: the matched scale is exactly the ratio's
first-order behaviour, so any deviation from it has to be justified. Then decide whether the
reference over- or under-reacts:

| Symptom | Reading | Action |
| --- | --- | --- |
| Artifacts appear as deflections **opposite** to the analysis channel | Reference over-reacts, too much is subtracted | Lower `w` |
| Artifacts still show up with the **same sign** as the analysis channel | Reference under-reacts | Raise `w` |
| Artifacts are gone and event amplitudes are unchanged | Current `w` is right | Keep it |

**Estimate** fits the weight from the recording itself. It needs an applied correction,
because it works on the corrected traces and on the two channels' `correction_scale` values.

### 1. Build the fitting sample

Start from every sample where both corrected traces are finite, then remove each marker
window: from `marker − Exclude Before Marker` to `marker + Exclude After Marker`, defaults
5 s and 60 s. These two fields sit under the weight and are **independent of the Event
Analysis window**, so the weight does not change when the event window is retuned later.
Artifacts stay in the sample; expected transients do not. The status line reports how many
samples survive. With no markers the whole trace is used and the status line warns that
expected transients may bias the weight low; if fewer than 20 samples remain, nothing is
estimated and an error is shown.

### 2. Convert both channels to relative deviations

So that a bright and a dim channel become comparable and the DC levels drop out:

```
a(t) = C_analysis(t) / F_analysis(t0) - 1
b(t) = C_reference(t) / F_reference(t0) - 1
```

`F(t0)` is the per-channel `correction_scale`, the level the corrected trace is anchored on.

### 3. Fit one slope through the origin

```
minimise  sum( rho( k*b(t) - a(t) ) ),   rho = soft_l1,  f_scale = median(|a - median(a)|)
```

The slope starts from the closed-form least-squares value `sum(a*b) / sum(b*b)`. There is no
intercept because both deviations are already referenced to their own level, and a soft-L1
loss rather than a squared loss so a few remaining deflections — an unmarked transient, a
movement spike — cannot drag the slope. `f_scale` is the median absolute deviation of `a`, so
the loss scales with the noise of the analysis channel.

### 4. Convert the slope to the count scale of the field

```
w = k * F_analysis(t0) / F_reference(t0) = k * matched scale
```

```
analysis(t) = C_analysis(t) - w * (C_reference(t) - F_reference(t0))
dF/F0(t) ~= a(t) - (w / matched) * b(t)
```

The conversion is necessary because the fit and the field live in different spaces: the fit
returns a slope between *relative* deviations, while the analysis trace subtracts *counts*.
The residual artifact vanishes exactly when `w / matched = k`.

Validation example: `matched = 1.524`, fitted `k = 0.333`, reported `w = 0.508`. With
`w = 1.524` the artifact produced a 2.74 % deflection in dF/F0; with `w = 0.508` it was
0.28 %, and the event amplitude moved only from 8.84 % to 8.74 %.

### 5. Report and verify

The field is filled and the combination is recomputed immediately. The status line prints the
sample count, the exclusion window used and the matched scale for comparison. Then check
three things: the artifact stretch flattens, the event amplitude matches the matched-scale run
(if it drops, the reference is carrying real signal and `w` is eating biology), and `w` stays
positive. A weight far below the matched scale means the reference reacts strongly to whatever
the artifact is; a weight above it means the reference is attenuated relative to the analysis
channel.

The same computation is available from a script and returns the same count-scale value:

```python
from processing import estimate_reference_weight

w = estimate_reference_weight(corrected_470, corrected_410, mask, level_470, level_410)
```

`mask` selects the samples to fit on and both levels are the per-channel `correction_scale`
values.
