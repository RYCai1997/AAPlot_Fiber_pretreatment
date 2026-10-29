# Updates

Newest first. Superseded behaviour is marked, so an old entry does not contradict the current
manual.

## 2026-10-29

- The bleaching correction is now additive and the **Fitting Baseline** defines F0:
  `corrected = F0 + [(raw − offset) − F(t)]`, with `F0 = baseline − offset`. The fitted decay is
  subtracted in counts, the corrected trace sits on the fluorescence the user declared, and F0 is
  the denominator of dF/F0 both for the whole trace and for every event trial. Editing the
  baseline now moves every number (`1 / F0`), and editing the offset moves it too, instead of the
  two cancelling inside the fit.
- The correction no longer uses the fitted level at t₀ anywhere. Anchoring on the first sample
  made the scale depend on where the recording started, on its fastest decay component and on
  one noisy point; the scale is the declared F0 now, and each channel records
  `entered_baseline` and `effective_baseline`.
- A baseline that is not larger than the offset is refused with an error naming the channel,
  instead of being reinterpreted as an already subtracted level.
- A ratio trace is divided after each channel has had its offset removed, and its F0 is
  `F0₁ / F0₂`, so a baseline edit moves the ratio dF/F0 as well.
- The numerator of dF/F0 is still referenced to the mean of the baseline interval, so the trace
  keeps starting at zero; only the denominator comes from the declared baseline.
- Reproduced RWD's 470/410 motion correction as a third channel combination, `rwd`, and made it
  the default. The corrected analysis channel is regressed on the corrected reference with a
  robust (`soft_l1`) straight line, the fitted prediction is subtracted, and the result is put
  back on `median(Ffull)` — the median of the full-time raw analysis trace, the denominator the
  RWD manual specifies. Against a literal implementation of the manual's formula the two traces
  agree to 0.018 % dF/F0 across the whole trace (0.12 % of the event peak). `difference` and
  `ratio` remain selectable, with the weight field used only by `difference`.
- Reduced the PLS fit to the single knob RWD exposes: `beta`, range 5–12, default 8. `lambda` is
  internal and always falls back to its default.
- Short runs of missing fluorescence samples are now bridged while loading: a run of up to 3
  samples is interpolated between its neighbours, so the sample count never changes and the fit
  never sees a hole. Longer runs stay NaN and are reported, and the counts go into
  `fit_details.gap_fills`.
- Fixed `operands could not be broadcast together with shapes (14062,) (14061,)`: the PLS fit
  returned a curve one sample shorter than the trace whenever the recording held an empty cell,
  which also caused the earlier `x and y must have same first dimension`. The fitted curve now
  keeps the input length, the correction normalises on the first finite fitted value, and the
  applied result reports skipped cells.
- The combined-presentation axis label and the event-analysis signal list name the RWD fit and
  its slope instead of calling everything a ratio.

## 2026-10-28

- Optimised the bleaching correction: `corrected = (raw − offset) × F(t₀) / F(t)` *(the
  multiplicative form was replaced by the additive one on 2026-10-29, below)*. Dividing by
  the bleaching factor normalised at t₀ un-bleaches the transient itself and keeps the
  fluorescence scale of `raw − offset`, so the user baseline no longer controls the output level.
- The offset is subtracted before the fit and therefore reaches F0 directly. Omitting it no
  longer hides inside the fitted constant: it inflates F0 and compresses every dF/F0 by
  `F_true / (F_true + residual offset)`.
- Corrected traces and the ratio are anchored on `F(t₀)` per channel, so the expected ratio level
  equals `F(t₀)_analysis / F(t₀)_reference` and is written to the output details *(the anchor was
  replaced by the declared F0 on 2026-10-29)*.
- Division by a near-zero fitted value is rejected with an explicit message instead of producing
  spikes, and `fixed`-mode baselines that disagree with the plateau are reported in
  `fit_warnings`.
- Added the `difference` channel combination next to `ratio` (it was the default until
  2026-10-29). Only the reference deviation is removed, the analysis level and dF/F0 scaling are
  unchanged, and the reference-noise and zero-crossing failure modes of the ratio are avoided. A
  blank weight uses the matched scale, which reproduces the ratio to first order.
- Added **Restore From Saved JSON**, with a parameter-by-parameter comparison before anything is
  applied and saved fits seeded into the cache so only changed channels are refitted.
- Added an **s / min** selector to every time field; switching the unit converts the value in
  place, so the duration is unchanged.
- The **Estimate** exclusion window is now set by its own two fields under the weight (default
  5 s before and 60 s after each marker) instead of borrowing the Event Analysis window.
- Added the **PLS fit** (`penalized`) bleaching model: decimated log-space Whittaker smoothing
  with iteratively reweighted least squares. The fitted curve is stored as 200 knots, so the reuse
  feature works for this model too. `beta` and `lambda` were both editable at the time; since
  2026-10-29 only `beta` is.
- Fixed a crash when previewing the PLS fit (`KeyError: 'final_refinement_samples'`): the preview
  asked for parameters a penalized fit does not have. It now falls back through the keys the
  model actually provides.
- The PLS reweighting states its convergence honestly: tolerance `1e-5` of the log span with a
  cap of 100 iterations and a warning when the cap is hit, instead of stopping at 40 rounds and
  still reporting "Converged: Yes".
- A plotting call that receives series of different lengths trims them to the shorter one and says
  so in the status line, instead of raising
  `ValueError: x and y must have same first dimension`.
- Callback tracebacks are written to `aaplot_errors.log` beside `app.py` in addition to the
  console.
- An offset or baseline edit that has not been applied is reported instead of leaving stale
  results on screen, and **Calculate Event dF/F0 & Z-score**, **Calculate dF/F0 & Z-score** and
  **Estimate** re-apply it automatically. The status line after a successful apply lists the
  offsets used.
- Changing an **offset** or a **fitting baseline** no longer blocks **Apply Correction** with
  "reconfirm this channel's fit": those two fields are excluded from the confirmation signature
  and the channel is refitted on apply. A data-range, model or baseline-mode change still requires
  confirming the regions again.
- A recording with a single wavelength can be applied: with no reference wavelength the
  combination is skipped instead of blocking the run, and `combine` is reported as `null`.
- Event analysis uses the stored combination trace instead of recomputing a ratio of its own, so
  switching the combination and changing the reference weight flow through to the trial traces,
  the mean ± SEM curves and the exported columns.
- The **Reference Weight** field gained an **Estimate** button. In a synthetic recording whose
  reference reacted 2.5× more strongly than its level implied, the estimate landed within 2 % of
  the grid-search optimum and cut the artifact deflection from 2.76 % to 0.29 % while leaving the
  event amplitude unchanged.
- The window opens at a size derived from the screen (up to 1860×1180) with a smaller minimum, and
  every notebook tab scrolls vertically. The fitting window and the restore dialog are resizable.

## 2026-10-27

- Reworked the multi-start fitting path: every candidate start is screened on a ~600-point
  subsample and only the three best are refined on the full fitting data, while each model keeps
  its iteration budget (single-exponential 3000/5000, double-exponential 5000/8000). Fitting is
  2–4× faster and the fitted curve stays within noise of the previous implementation.
- Added a fit cache keyed by data version and channel settings, so re-applying the same correction,
  or correcting a single changed wavelength, no longer refits every channel.
- Fitting runs on a worker thread with a progress bar and a **Cancel Fit** button; results whose
  data version or fit settings changed in the meantime are discarded instead of applied.
- Panning and zooming no longer redraw the full traces: curves are drawn with a min/max envelope,
  resampled when the X range changes, and switched to a coarse copy while dragging.
- Markers are drawn as a single `vlines` artist and the fitting-window scatter is decimated.
- Set `AAPLOT_FIT_EXACT=1` before launching to reproduce the previous fitting path bit for bit.
- Renamed the normalization tab to **Full trace analysis**, left smoothing and downsampling off by
  default, and renamed its baseline section to **dF/F0/Z-score Baseline**.
- Event defaults are 5 s before and 30 s after the marker with a 5 s baseline.
- Split **Export Results** into a **Full trace plot** column and an **Event trials** column; event
  defaults are the two CSV outputs.
- `event_trials_<marker>.csv` replaces `event_aligned_trials.csv`: a shared `Time_s` column,
  per-trial dF/F0 columns, one empty separator column, then the per-trial Z-score columns.
- Added per-marker overlaid trial figures and per-marker heatmaps.
- Added a Windows launcher self-check: `run_RWD_fiber_pretreatment.bat` locates Anaconda, creates
  or reuses the `aaplot` conda environment, validates `requirements.txt` with `env_check.py`,
  installs missing packages with `pip`, and starts `app.py` with that environment's interpreter.
  `--no-install` only checks; `--recreate` rebuilds the environment.

## 2026-09-25

- Added trial-level marker selection in **Event Analysis**. Matching markers are selected by
  default, but any contiguous or non-contiguous subset can be analyzed.
- Added independent event-signal selection for corrected 410, 470 and 560 traces plus the combined
  trace. Signals can be selected individually, in any combination, or all at once.
- Changed `event_aligned_trials.csv` and `event_aligned_average.csv` from stacked long-form signal
  rows to wide, signal-grouped columns.
- Event figures, CSV output and `parameters_and_marker_edits.json` retain the exact marker subset
  and signal selection used for each calculation.
