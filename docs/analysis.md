# Display, dF/F0 and event analysis

These steps all start from an applied correction. Nothing here changes the corrected traces
in the files that are written; display smoothing and downsampling are recorded in the JSON.

The correction is their only input, so a recomputed correction carries through on its own: if
dF/F0 and the event analysis were already on screen, they are recalculated from the new traces
whenever **Apply Correction** runs — including when it runs because a pending offset or baseline
edit was picked up by **Calculate dF/F0 & Z-score** or **Calculate Event dF/F0 & Z-score**. The
status line then names the F0 each channel is divided by
(`F0: 410=3105, 470=3105; 470/410 F0=1`), so an edit can be followed from the field to the
percentage.

## Display processing

The **Trace Display** options select which curves are drawn (**Raw + Corrected**, **Raw
Only**, or **Corrected Only**) and whether the fitted curves are shown.

- **Enable Smoothing** applies a centered rolling mean over the given window in seconds.
- **Enable Downsampling** reduces the plotted and exported rows by points or seconds.

Both take effect only after **Apply Display Processing** is clicked. The applied settings are
used on screen and in PNG, CSV and AAPlot-compatible output. Event analysis uses the applied
smoothing but keeps its own full marker-aligned sampling grid. Panning and zooming draw from a
reduced copy for speed; the full-resolution curves come back when the interaction ends. Exports
are not affected by that display-only reduction: they contain every row that survives the
applied smoothing and downsampling.

## Full trace analysis (dF/F0 and Z-score)

Applying correction does not reuse the fitting regions as the normalization baseline. On **Full
trace analysis**, select a **Manual** or **Pre-Marker Interval** baseline in the **dF/F0/Z-score
Baseline** section, an optional baseline marker, and optional relative time (**Use Marker as
Time 0** or **Use Specified Time as 0**), then click **Calculate dF/F0 & Z-score**. Smoothing
and downsampling are off by default; enable them only when needed.

- `F0` is the **mean of the corrected trace inside the baseline interval** — for the analysis
  trace and for every wavelength separately, after the channel combination has been applied.
- dF/F0 is `100 × (F − F0) / F0`, which is zero across the baseline by construction.
- The typed offset and fitting baseline never enter this step directly: they set the level of
  the corrected trace, and F0 is measured there. A larger offset or a smaller baseline therefore
  lowers F0 and raises every percentage.
- Z-score uses the dF/F0 mean and sample SD in the same baseline. It is therefore **scale
  invariant**: changing the baseline rescales dF/F0 but leaves the Z-score curve unchanged,
  because the same factor cancels in `(dF/F0 − mean) / SD`. The dF/F0 panel label carries the
  F0 it was divided by (`470 dF/F0 (%) | F0 = 8.91`) so the two can be told apart.

Every corrected wavelength is also normalized separately and retained as
`dff_<wavelength>_percent` and `zscore_<wavelength>` (for example `dff_560_percent` and
`zscore_560`) whenever that wavelength exists.

## Marker-aligned event analysis

The **Event Analysis** tab operates on corrected data and has its own multi-select **Signals to
Analyze** list. Every available corrected wavelength (410, 470 and/or 560) can be analyzed
separately, together with the channel combination defined by the current **Analysis Wavelength /
Reference Wavelength** pair — the mode **Combine Channels** selects, labelled with its slope or
weight. The combination is read from the processed trace, so changing the combination or the
reference weight invalidates the event results and they have to be recalculated. Choose one or
several signals, or use **Select All Signals** / **Clear Signals**. When several signals are
selected, their trial traces and mean ± SEM curves are distinguished by signal in the same
four-panel event figure.

After choosing a **Marker Name**, the matching markers appear in **Markers to Analyze** and are
all selected by default. Use Ctrl/Shift-click, **Select All**, or **Clear Selection** to analyze
any subset of those markers, then click **Calculate Event dF/F0 & Z-score**. The default window
is 5 s before and 30 s after the marker, with a 5 s pre-marker baseline. Each selected complete
trial is interpolated to a common time axis and normalized the same way as the full trace: the
F0 is measured inside the pre-marker window of that trial, on the corrected trace, so a baseline
or offset edit moves the event results through the level it sets.
Outputs include trial-level and mean ± SEM dF/F0 and Z-score tables and figures, while the JSON
log records the exact selected marker IDs and times.

The Z-score of a trial divides its dF/F0 by the sample SD (ddof = 1) of the same pre-marker
window, and the mean of that window is subtracted first — which is zero by construction, because
F0 is the mean of that window. With **One baseline SD for all trials (pooled)** ticked, one value
replaces every trial's own:

```
sd_pooled = sqrt( Σ (nᵢ − 1) sᵢ² / Σ (nᵢ − 1) )
```

so a trial with a noisier pre-marker window no longer dilutes its own z-score, at the price of
assuming the noise level is the same in every trial. Each trial row still records both numbers:
`baseline_dff_sd` is the value actually divided by, `baseline_dff_sd_own` is what that trial's
window gave, and `baseline_dff_n` its sample count. Toggling the box recalculates the event
analysis immediately and the status line names the pooled value.

### Per-trial Z and average Z

Two summary z curves are computed and both are exported; **Average Z from** decides which one the
figure draws. Neither choice changes any trial's own z.

| Setting | Column | Meaning |
|---|---|---|
| `mean of trials` (default) | `zscore_mean` ± `zscore_sem` | every trial's own z, averaged point by point; same scale as a single trial |
| `average trace` | `zscore_of_average` | the trial-averaged dF/F0 standardised against that average's own baseline SD; roughly √n_trials larger |

A single trial's z is bounded by that trial's own signal-to-noise ratio, so smoothing the trace
cannot raise it — a centred smoother shrinks the response along with the noise, and when its
window reaches into the pre-marker baseline it also biases F0. The average trace is the honest
route to a larger number: its noise falls as √n. Report which basis a figure uses; the two are not
interchangeable and must not be pooled in one statistic.

Event CSV files are grouped by marker name: one file per analyzed marker, with the shared
relative time axis in the first column, one `dF/F0_trialNN` column per trial, one empty
separator column, then the matching `Z-score_trialNN` columns. When several signals are
selected, column names carry the signal prefix (`410_dF/F0_trial01`, `rwd_470_410_dF/F0_trial01`
or `difference_470_410_dF/F0_trial01`) and columns are grouped signal by signal.

## Result output

The **Export Results** tab selects what is written. Each export creates a new output folder
named by **Output Folder Name**; an existing name is never overwritten and receives a `_2`,
`_3`, ... suffix instead. dF/F0 and event outputs require the corresponding calculation to be
up to date; the program asks you to recalculate when its settings have changed.

| File | Contents |
| --- | --- |
| `corrected_fluorescence_trace.csv` / `.png` | Every available corrected wavelength |
| `dFF0_trace.csv` / `.png` / `.svg` | Per-wavelength dF/F0 |
| `zscore_trace.csv` / `.png` / `.svg` | Per-wavelength Z-score |
| `event_trials_<marker>.csv` | One file per analyzed marker name |
| `event_aligned_average.csv` | Mean ± SEM across trials |
| `event_aligned_dFF_zscore.png` / `.svg` | Four-panel trial and average overview |
| `event_trials_<marker>.png` | Every trial of that marker overlaid in two panels |
| `event_heatmap_<marker>.png` | dF/F0 trials above, Z-score trials below |
| `markers_working_copy.csv`, `parameters_and_marker_edits.json` | Markers and the full settings log |
| `AAPlot_corrected_trace/`, `AAPlot_dFF0/`, `AAPlot_zscore/` | AAPlot-compatible `Time,Signal,Marker` text files |

Original input files are read-only and are never modified.
