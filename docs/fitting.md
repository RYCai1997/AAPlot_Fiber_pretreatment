# Bleaching correction and fitting

Every wavelength is fitted and corrected independently; one wavelength is never used as the
regression basis for another. The reference only enters afterwards, in the channel
combination (see [channels.md](channels.md)).

## Bleaching correction

```
corrected(t) = raw(t) − F(t) + (fitting baseline − offset)
             = F0 + [(raw(t) − offset) − F(t)],        F0 = fitting baseline − offset
```

Both lines are the same expression: the first is how the correction reads, the second is how it
is evaluated. The fitted bleaching curve is subtracted in counts and the result is put back on
F0, so the corrected trace sits at the fluorescence the user declared for the minimum of the
signal.

- `raw(t)` is the recorded fluorescence of that wavelength.
- `offset` is the instrument reading with no sample attached — fluorescence that carries no
  biological information. It is subtracted **before** the fit, so it can never be re-absorbed
  by the fitted constant.
- `F(t)` is the independently fitted bleaching curve, evaluated over the whole trace. It is
  **subtracted**, in counts, so the corrected trace keeps the units of the recording.
- `F0` is the fluorescence the user declared for the trace. It sets the level of the corrected
  trace and is the denominator of dF/F0.

Subtracting rather than dividing is what lets F0 stay a free scale: the numerator carries no
factor of F0, so dF/F0 = `100 × (corrected − level) / F0` really does scale as `1 / F0`. If the
decay were divided out and the result multiplied by F0, F0 would cancel against itself and the
field would have no effect — which is exactly what it used to do.

Note what is **not** in the formula: the fitted level at t₀. Anchoring on the first sample
makes the scale depend on where the recording happened to start, on the fastest decay component
and on the noise of a single point, so it is not used anywhere. The scale is the declared F0
instead, which is reproducible and the same for every trace of a recording.

Properties of this form:

| Question | Behaviour |
| --- | --- |
| Where does the corrected trace sit? | On `F0`, the fluorescence that was entered for that channel |
| Is the bleaching removed? | Yes — divided out at every sample, so the transient itself is un-bleached, not merely shifted |
| Does the offset matter? | Yes — it is removed from the trace *and* from F0, so it has to be right |
| Does the transient amplitude depend on when it occurred? | No — the same relative response at 1 min and at 10 min |
| Does the user baseline set the output scale? | Yes — it *is* the scale, and the dF/F0 denominator |

Use the plotted **Fit** curve together with the reported `correction_constant` and
`correction_scale` in `parameters_and_marker_edits.json` to verify what was subtracted and
where the trace was anchored.

### Fitting Baseline, and the level it sets

The **Fitting Baseline** is the **lowest point of the trace after bleaching** — the plateau the
signal decays to — read off the **raw** plot, so it still contains the instrument offset. The
offset is subtracted from it, and the difference is where the corrected trace is placed:

```
level of the corrected trace = fitting baseline − offset
corrected(t)              = raw(t) − F(t) + (fitting baseline − offset)
```

Downstream nothing reads the typed values again: dF/F0 is normalised by the mean of the
**corrected** trace inside the chosen baseline window, so the two fields act by moving that
level:

| Edited | Effect |
| --- | --- |
| Offset | Subtracted from the trace *and* from the level, so the corrected trace and the measured F0 drop by the same amount — the percentages rise as the offset rises (a too-small offset under-reports the response) |
| Fitting baseline | Raises or lowers the corrected level one-for-one; F0 is measured there, so dF/F0 scales as `1 / F0` |

The same value is the asymptote estimate the fit uses: `fit_constant` starts there and lets the
optimizer refine it, while `fixed` pins the asymptote to it. Because the field holds the minimum
of the trace, both readings agree — this is the one number in the fit that the user can state
ahead of time, and `fixed` mode now means exactly "the baseline is the minimum I measured".
The fit window warns when the entered value disagrees with the end of the trace by more than
5 %, which is the check that the value belongs to this recording. Each channel's parameters
record both numbers as `entered_baseline` and `effective_baseline`.

A baseline that is **not larger than the offset** cannot describe fluorescence — the offset
would have to be removed from it and nothing would be left — so the program refuses it with an
explicit error naming the channel, the entered value and the offset it has to exceed. It is not
reinterpreted as an already-subtracted level: that guess would silently produce a wrong F0.

## Fitting models

- The fitting window supports **none**, linear, single-exponential, double-exponential and PLS
  (`penalized`) models, with one or more regions that can be dragged or resized; use
  **Add Region** and **Delete Selected Region** to edit the set. **Preview Fit** draws the
  candidate curve and reports its parameters, RMSE and BIC; **Use These Regions** confirms the
  fit for that wavelength.
- Only samples inside the selected regions enter parameter estimation, refinement or scoring,
  but the fitted curve is evaluated over the whole trace. Regions that skip the initial decay
  therefore extrapolate and distort the early corrected values.
- The baseline initializes the freely fitted constant (`fit_constant`) and is used as the
  fixed constant in `fixed` mode. In `fixed` mode a baseline that disagrees with the plateau
  of the data is reported in `fit_warnings`.
- The fitted curve must stay positive and away from zero; a correction that would divide by a
  near-zero value is rejected instead of producing spikes.
- Fitting runs in the background: a progress bar and a **Cancel Fit** button appear below the
  plot while it works. Results whose data or settings changed in the meantime are discarded
  instead of applied.
- Changing the **data range**, the **fit model** or the **baseline mode** invalidates that
  wavelength's confirmed fit and requires confirming the regions again before **Apply
  Correction** succeeds.
- Changing the **offset** or the **fitting baseline** does not need a confirmation: they
  change the fit input, not the regions or the model, so **Apply Correction** refits that
  channel immediately. The corrected trace moves one-for-one with the offset, because the
  correction is anchored on `raw − offset`.
- Editing an offset or a fitting baseline does **not** silently change anything on screen: the
  trace, dF/F0 and every event result keep coming from the last applied settings. The status
  line and the Event Analysis status then say which field is not applied yet, and the status
  line after the next **Apply Correction** lists the offsets that were used
  (`Offset used: 410=5000, 470=5000`). **Calculate Event dF/F0 & Z-score**, **Calculate dF/F0
  & Z-score** and **Estimate** apply pending edits themselves before they run, so a changed
  offset always reaches these results.

## No correction (`none`)

`none` removes the offset and nothing else: `corrected = raw − offset`. The trace keeps its
decay, so dF/F0 drifts with the bleaching and every percentage depends on where the baseline
window sits. It exists for recordings without measurable bleaching, and as a reference point
for how much a fitted baseline is actually doing: switching between `none` and a model on the
same data shows the size of the correction. The model needs no regions and no confirmation —
**Apply Correction** accepts it directly — and the fitting window prints
`no bleaching correction (offset only)`.

## PLS fit

Alongside the parametric models, `penalized` fits the bleaching baseline as a lower envelope
without assuming a functional form, in the spirit of the iterative weighted least squares
baseline used by RWD recorders. It suits recordings with rising signals, where a parametric
curve is pulled upwards by the transients themselves.

How it works:

1. The trace is decimated to at most 1200 points (the down-sampling variant of the algorithm)
   and taken to log space, where a steep exponential decay becomes close to linear.
2. A Whittaker smoother (second-difference penalty) is fitted with **iteratively reweighted
   least squares**: every round down-weights the samples that sit above the current fit, so
   upward transients drop out of the baseline while the decay is followed. The reweighting can
   oscillate for a few rounds before it settles, so it runs until the largest change falls
   below `1e-5` of the log span, with a cap of 100 iterations; if the cap is reached the fit is
   marked as not converged and a warning is recorded instead of claiming success.
3. The result is interpolated back onto the full time axis and used exactly like the other
   models: `corrected = F0 + [(raw − offset) − F(t)]`.

Like RWD there is **one user-facing knob**:

| Control | Range | Default | Effect |
| --- | --- | --- | --- |
| `beta` | 5 – 12 | 8 | Sharpness of the asymmetric weighting. Higher clings closer to the lower envelope and ignores transients more aggressively; lower keeps the fitted curve closer to the data trend |

The range and the default are the ones RWD documents for its correction coefficient (`The
correction coefficient β ranges from 5~12, with a default value of 8`), and the effect of
moving it matches that description. The smoothing strength is internal: `lambda` stays at 10
and scales with the number of fit points, so it is not exposed in the window and always falls
back to its default when a fit is run. `beta` is stored per wavelength in `fit_betas`, written
to `parameters_and_marker_edits.json`, and restored with the rest of the configuration. The
200 knots of the fitted curve are stored as well, so a saved PLS fit can be reused without
refitting, like the parametric models.

RWD splits this algorithm into `PLS Fit` and `PLS Fit (Down Sampling)`, the second one for
long recordings. Here a single `penalized` model covers both: recordings shorter than 1200
device samples are fitted on every point, and longer ones are decimated before the fit and
interpolated back — the down-sampling variant, chosen automatically. The preview reports which
of the two was used (`fit points = 1,200 (downsampled)`).

Notes:

- The fit uses the **whole trace**, not just the fitting regions, because it is a global
  smoother by nature. The regions only decide which samples score the fit (RMSE, BIC), so keep
  them over a quiet stretch.
- `beta` and `lambda` above the stable range are what break this family of algorithms: a steep
  initial decay starts to look like an upward signal and the first minute is dragged below the
  data. That is why the caps exist. With the defaults the baseline stays within about 0.15 % of
  a known decay while a 3 s transient leaks only 4 % into the fit.
- The fitting window preview reports `beta`, the iteration count, the number of fit points (and
  whether they were down-sampled) and how many knots were stored, next to the usual RMSE, BIC
  and convergence line.
- `penalized` is not part of the automatic BIC model comparison, because it is not a parametric
  model and its effective degrees of freedom are not a single number. Select it explicitly in
  the fitting window.
- An empty or non-numeric sample is bridged before the fit (see the README); if a gap is too
  long to bridge it is skipped and the fitted curve is NaN there, so the corrected trace keeps
  the same length as the recording.
