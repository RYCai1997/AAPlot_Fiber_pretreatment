# RWD Fiber Pretreatment

A standalone, interactive pretreatment tool for RWD fiber photometry recordings. Open
one recording folder, fit and remove the bleaching decay of every available wavelength,
compute dF/F0 and Z-score traces, run marker-aligned event analysis, and export figures,
tables, and AAPlot-compatible text files. Input files are opened read-only and are never
modified.

## Requirements and setup

- A Python 3 installation, or Anaconda on Windows.
- `pip install -r requirements.txt` (`matplotlib`, `numpy`, `pandas`, `scipy`).
- Start the program with `run_RWD_fiber_pretreatment.bat`, or run `python app.py` directly
  with a prepared environment.
- On Windows the launcher self-checks the environment: it locates Anaconda, creates the
  `aaplot` conda environment (Python 3.12) when missing, verifies every package listed in
  `requirements.txt` through `env_check.py`, installs whatever is missing into that env with
  `pip`, and then starts `app.py` with the env's own interpreter. `--no-install` only reports
  the check result, `--recreate` deletes and rebuilds the environment, and
  `AAPLOT_NO_PAUSE=1` skips the final pause for automation.

## Input folder

`Fluorescence.csv` is required. Its first line is kept as free-form metadata and
skipped; the data table starts on the second line.

- `TimeStamp` (milliseconds) is required, and at least one fluorescence column must be
  named `CH<n>-410`, `CH<n>-470`, or `CH<n>-560`, where `<n>` is the acquisition channel.
- `Events.csv` is optional. When present, rows with `State` equal to `1` become markers;
  if `State` is missing or selects nothing, every row is used. The marker name comes from
  the `Name` column.
- Rows without a valid `TimeStamp` are dropped and the table is sorted by time. At least
  20 valid samples are required.

Markers can be added, deleted, or restored in **Markers (Working Copy)** on the **Data &
Fitting** tab. Edits are kept in memory and are written to `markers_working_copy.csv` on
export; the input files are never changed.

## Dynamic 410 / 470 / 560 support

The program detects supported wavelengths separately for each recording channel, so one
folder may contain any combination of 410, 470, and 560 traces for CH1, CH2, CH3, and
later channels. Missing 410 or 470 data are valid and their controls and plots are
hidden. 560 is a complete first-class channel with the same independent offset, bleaching
fit, correction, dF/F0, Z-score, plotting, and export support as 410 and 470.

## Workflow

1. Click **Select Recording Folder**, choose the **Recording Channel**, and set the
   **Valid Data Range (min)** used for fitting and correction.
2. For every visible wavelength, enter its **Offset** and **Fitting Baseline**, then open
   **Configure <wavelength> Fit...** to select one or more fitting regions and the model.
3. Choose the **Analysis Wavelength** and, optionally, a **Reference Wavelength**. With a
   reference, pick **Combine Channels** = `ratio` or `difference` and apply; with `None`,
   the independently corrected analysis wavelength is used directly.
4. Click **Apply Correction**. Every available wavelength is corrected independently.

Whole-recording normalization and event analysis are then configured on the **Full trace
analysis** and **Event Analysis** tabs, and files are written from the **Export Results**
tab.

## Reusing a previous analysis

A recording that was analysed before can be restored instead of typed in again, and only
the parts that change are recomputed. The file is chosen explicitly: one mouse folder may
hold several `output_*` folders, so nothing is read automatically.

**Choosing the file.** After a recording is loaded, **Restore From Saved JSON...** becomes
available. It opens a normal file dialog whose start directory is the recording folder you
loaded, or the folder of the last file you restored. The dialog lists every `.json` file in
that directory, so navigate into `output_<recording>_<channel>[_suffix]` and pick the
`parameters_and_marker_edits.json` you want. Cancel leaves everything untouched. A file that
is not an exported parameters file — invalid JSON, or no `processing` block — is rejected
with a message naming the reason, and no settings change.

**Comparison.** The chosen file opens a review window showing its full path, modification
time and saved source channel, followed by one row per difference between the saved
configuration and the current settings, with the saved value beside the current one. Rows
whose scope is a wavelength decide whether that channel is fitted again. The summary above
the table names the channels that still have a usable saved fit and the channels that would
be refitted. The data range is compared numerically, so the sampled end of a recording
(`9.99944 min`) is not reported as different from `10 min`.

**Choose Another File...** reopens the file dialog from the review window, which is the
quickest way to compare two outputs of the same recording before committing to one.

**Initialise.** **Initialise From This File** writes the saved values into the interface:

| Restored | Notes |
| --- | --- |
| Data range, offsets, fitting baselines | Per visible wavelength |
| Fitting regions, fit model, baseline mode | Marked `(restored)` in each channel's status |
| Analysis and reference wavelength, combination, reference weight | Combination and weight are applied immediately |
| Smoothing | Enabled only when the saved analysis used it |
| dF/F0/Z-score baseline interval | Set to the saved interval in manual mode |
| Markers | Original markers minus the deleted ones, plus the manual ones |
| Output folder name | The folder that held the saved file |

**Incremental recompute.** Each channel's fit is identified by a signature
`(data version, channel, model, baseline mode, offset, baseline, fitting regions, data
range)`. A saved fit whose signature matches is rebuilt from its stored parameters and put
straight into the fit cache, so **Apply Correction** reuses it without fitting anything.
Only channels whose signature changed are fitted again.

| Change | Refit needed |
| --- | --- |
| Offset, fitting baseline, fit model, baseline mode, fitting regions, data range | That channel only (data range: every channel) |
| Combination (`ratio` / `difference`), reference weight, analysis or reference wavelength | None |
| Smoothing, output name, markers, dF/F0 baseline interval | None |

The status line reports the split, for example `Reused the saved fit for 410; refitted
470.` Each channel's parameters in `parameters_and_marker_edits.json` record
`fit_reused_from_cache`, so a re-export documents which fits were reused.

A channel falls back to a fresh fit when its saved model differs from the model now
selected, when the saved parameters are missing, or when the saved fitted curve does not
match the current data range. Those channels appear in the "refit needed" list before the
initialisation is applied.

## Correction and fitting

### Bleaching correction

Every wavelength is fitted and corrected independently; one wavelength is never used as
the regression basis for another.

```
corrected(t) = (raw(t) − offset) × F(t₀) / F(t)
```

- `raw(t)` is the recorded fluorescence of that wavelength.
- `offset` is the instrument reading with no sample attached — fluorescence that carries
  no biological information. It is subtracted **before** the fit, so it can never be
  re-absorbed by the fitted constant.
- `F(t)` is the independently fitted bleaching curve, evaluated over the whole trace.
- `F(t₀)` normalises the bleaching factor to 1 at the start of the trace, so the corrected
  trace keeps the fluorescence scale of `raw − offset`.

Properties of this form:

| Question | Behaviour |
| --- | --- |
| Where does the corrected trace sit? | On the `raw − offset` level at t₀, i.e. the un-bleached fluorescence |
| Is the bleaching removed? | Yes — divided out at every sample, so the transient itself is un-bleached, not merely shifted |
| Does the offset matter? | Yes — it sets F0. Skipping it inflates F0 and scales every dF/F0 by `F_true / (F_true + residual offset)` |
| Does the transient amplitude depend on when it occurred? | No — the same relative response at 1 min and at 10 min |
| Does the user baseline set the output scale? | No — it only seeds the free constant (`fit_constant`) or pins it (`fixed`) |

Use the plotted **Fit** curve together with the reported `correction_constant` and
`correction_scale` in `parameters_and_marker_edits.json` to verify what was subtracted and
where the trace was anchored.

### Channel combination

Two combinations are available; both produce the analysis trace that dF/F0 and Z-score are
computed from.

**Ratio** (`combine = ratio`)

```
analysis(t) = C_analysis(t) / C_reference(t)
dF/F0(t)    = 100 · (analysis(t) − analysis₀) / analysis₀ ,   analysis₀ = F_analysis(t₀) / F_reference(t₀)
```

Equivalent to `100 · [(1+a)/(1+b) − 1]` with `a` and `b` the relative deviations of the two
channels, so it removes a fluctuation that is proportional to each channel's own level.

**Difference** (`combine = difference`)

```
analysis(t) = C_analysis(t) − w · (C_reference(t) − F_reference(t₀))
dF/F0(t)    = 100 · (analysis(t) − analysis₀) / analysis₀ ,   analysis₀ = F_analysis(t₀)
```

Only the deviation of the reference is removed, so the trace keeps the level of the
analysis channel and dF/F0 divides by `F_analysis(t₀)` as usual. `w` is the **Reference
Weight** field:

- blank → `w = F_analysis(t₀) / F_reference(t₀)`, the matched scale. To first order this
  reproduces the ratio while never dividing by the reference.
- a number → that weight. Use it when the reference reacts to artifacts more or less
  strongly than its own level implies.
- `w = 0` → the reference is ignored (equivalent to no reference).

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
scattering and blood-volume changes — the ratio subtracts too much and can report a
negative deflection where the analysis channel actually rose. The difference combination
with a fitted weight removes that failure mode without touching the signal amplitude.

#### Choosing the reference weight

Leave **Reference Weight** blank to start: the matched scale is exactly the ratio's
first-order behaviour, so any deviation from it has to be justified.

Then decide whether the reference over- or under-reacts:

| Symptom | Reading | Action |
| --- | --- | --- |
| Artifacts appear as deflections **opposite** to the analysis channel | Reference over-reacts, too much is subtracted | Lower `w` |
| Artifacts still show up with the **same sign** as the analysis channel | Reference under-reacts | Raise `w` |
| Artifacts are gone and event amplitudes are unchanged | Current `w` is right | Keep it |

**Estimate** fits the weight from the recording itself. It needs an applied correction,
because it works on the corrected traces and on the two channels' `correction_scale`
values. Clicking it runs the following steps.

**1. Build the fitting sample.** Start from every sample where both corrected traces are
finite, then remove each marker window: from `marker − event_pre_seconds` to
`marker + event_post_seconds`. Artifacts stay in the sample; expected transients do not. The
status line reports how many samples survive. With no markers the whole trace is used and
the status line warns that expected transients may bias the weight low; if fewer than
20 samples remain, nothing is estimated and an error is shown.

**2. Convert both channels to relative deviations**, so a bright and a dim channel become
comparable and the DC levels drop out:

```
a(t) = C_analysis(t) / F_analysis(t0) - 1
b(t) = C_reference(t) / F_reference(t0) - 1
```

`F(t0)` is the per-channel `correction_scale`, the level the corrected trace is anchored on.

**3. Fit one slope through the origin** with a soft-L1 loss:

```
minimise  sum( rho( k*b(t) - a(t) ) ),   rho = soft_l1,  f_scale = median(|a - median(a)|)
```

The slope starts from the closed-form least-squares value `sum(a*b) / sum(b*b)`. There is no
intercept because both deviations are already referenced to their own level; a soft-L1 loss
rather than a squared loss so a few remaining deflections — an unmarked transient, a
movement spike — cannot drag the slope. `f_scale` is the median absolute deviation of `a`,
so the loss scales with the noise of the analysis channel.

**4. Convert the slope to the count scale of the field.**

```
w = k * F_analysis(t0) / F_reference(t0) = k * matched scale
```

The conversion is necessary because the fit and the field live in different spaces: the fit
returns a slope between *relative* deviations, while the analysis trace subtracts *counts*.

```
analysis(t) = C_analysis(t) - w * (C_reference(t) - F_reference(t0))
dF/F0(t) ~= a(t) - (w / matched) * b(t)
```

The residual artifact vanishes exactly when `w / matched = k`, which is the conversion
above. Validation example: `matched = 1.524`, fitted `k = 0.333`, reported `w = 0.508`; with
`w = 1.524` the artifact produced a 2.74 % deflection in dF/F0, with `w = 0.508` it was
0.28 %, and the event amplitude moved only from 8.84 % to 8.74 %.

**5. Report and verify.** The field is filled and the combination is recomputed
immediately. The status line prints the sample count, the number of marker windows and the
matched scale for comparison. Then check three things: the artifact stretch flattens, the
event amplitude matches the matched-scale run (if it drops, the reference is carrying real
signal and `w` is eating biology), and `w` stays positive. A weight far below the matched
scale means the reference reacts strongly to whatever the artifact is; a weight above it
means the reference is attenuated relative to the analysis channel.

The same computation is available from a script and returns the same count-scale value:

```python
from processing import estimate_reference_weight

w = estimate_reference_weight(corrected_470, corrected_410, mask, level_470, level_410)
```

`mask` selects the samples to fit on and both levels are the per-channel `correction_scale`
values.

### Fitting

- The fitting window supports linear, single-exponential, and double-exponential models
  and one or more regions that can be dragged or resized; use **Add Region** and
  **Delete Selected Region** to edit the set. **Preview Fit** draws the candidate curve
  and reports its parameters, RMSE, and BIC; **Use These Regions** confirms the fit for
  that wavelength.
- Only samples inside the selected regions enter parameter estimation, refinement, or
  scoring, but the fitted curve is evaluated over the whole trace. Regions that skip the
  initial decay therefore extrapolate and distort the early corrected values.
- The baseline initializes the freely fitted constant (`fit_constant`) and is used as the
  fixed constant in `fixed` mode. In `fixed` mode a baseline that disagrees with the
  plateau of the data is reported in `fit_warnings`.
- The fitted curve must stay positive and away from zero; a correction that would divide
  by a near-zero value is rejected instead of producing spikes.
- Fitting runs in the background: a progress bar and a **Cancel Fit** button appear below
  the plot while it works. Results whose data or settings changed in the meantime are
  discarded instead of applied.
- Changing the valid data range, offset, baseline, or model invalidates that wavelength's
  confirmed fit and requires confirmation again before **Apply Correction** succeeds.

## Display processing

The **Trace Display** options select which curves are drawn (**Raw + Corrected**, **Raw
Only**, or **Corrected Only**) and whether the fitted curves are shown.

- **Enable Smoothing** applies a centered rolling mean over the given window in seconds.
- **Enable Downsampling** reduces the plotted and exported rows by points or seconds.

Both take effect only after **Apply Display Processing** is clicked. The applied settings
are used on screen and in PNG, CSV, and AAPlot-compatible output. Event analysis uses the
applied smoothing but keeps its own full marker-aligned sampling grid. Panning and
zooming draw from a reduced copy for speed; the full-resolution curves come back when the
interaction ends. Exports are not affected by that display-only reduction: they contain
every row that survives the applied smoothing and downsampling.

## Full trace analysis (dF/F0 and Z-score)

Applying correction does not reuse the fitting regions as the normalization baseline. On
**Full trace analysis**, select a **Manual** or **Pre-Marker Interval** baseline in the
**dF/F0/Z-score Baseline** section, an optional baseline marker, and optional relative time
(**Use Marker as Time 0** or **Use Specified Time as 0**), then click **Calculate dF/F0 &
Z-score**. Smoothing and downsampling are off by default; enable them only when needed.

- `F0` is the mean corrected analysis trace in the baseline.
- dF/F0 is `100 × (F - F0) / F0`.
- Z-score uses the dF/F0 mean and sample SD in the same baseline.

Every corrected wavelength is also normalized separately and retained as
`dff_<wavelength>_percent` and `zscore_<wavelength>` (for example `dff_560_percent` and
`zscore_560`) whenever that wavelength exists.

## Marker-aligned event analysis

The **Event Analysis** tab operates on corrected data and has its own multi-select
**Signals to Analyze** list. Every available corrected wavelength (410, 470, and/or 560)
can be analyzed separately, together with the channel combination defined by the current
**Analysis Wavelength / Reference Wavelength** pair — a ratio or a weighted difference,
whichever **Combine Channels** selects, labelled with its weight. The combination is read
from the processed trace, so changing the combination or the reference weight invalidates
the event results and they have to be recalculated. Choose one or several signals, or use
**Select All Signals** / **Clear Signals**. When several signals are selected, their trial
traces and mean ± SEM curves are distinguished by signal in the same four-panel event
figure.

After choosing a **Marker Name**, the matching markers appear in **Markers to Analyze** and
are all selected by default. Use Ctrl/Shift-click, **Select All**, or **Clear Selection**
to analyze any subset of those markers, then click **Calculate Event dF/F0 & Z-score**.
The default window is 5 s before and 30 s after the marker, with a 5 s pre-marker
baseline. Each selected complete trial is interpolated to a common time axis and
normalized using its own pre-marker baseline. Outputs include trial-level and mean ± SEM
dF/F0 and Z-score tables and figures, while the JSON log records the exact selected marker
IDs and times.

Event CSV files are grouped by marker name: one file per analyzed marker, with the shared
relative time axis in the first column, one `dF/F0_trialNN` column per trial, one empty
separator column, then the matching `Z-score_trialNN` columns. When several signals are
selected, column names carry the signal prefix (`410_dF/F0_trial01`,
`ratio_470_410_dF/F0_trial01` or `difference_470_410_dF/F0_trial01`) and columns are
grouped signal by signal.

## Result output

The **Export Results** tab selects what is written. Each export creates a new output folder
named by **Output Folder Name**; an existing name is never overwritten and receives a
`_2`, `_3`, ... suffix instead. dF/F0 and event outputs require the corresponding
calculation to be up to date; the program asks you to recalculate when its settings have
changed. Depending on the selected boxes, the folder contains:

- `corrected_fluorescence_trace.csv` / `.png`, including every available corrected
  wavelength;
- `dFF0_trace.csv` / `.png` / `.svg`, including per-wavelength dF/F0;
- `zscore_trace.csv` / `.png` / `.svg`, including per-wavelength Z-score;
- `event_trials_<marker>.csv` (one file per analyzed marker name) and
  `event_aligned_average.csv`;
- `event_aligned_dFF_zscore.png` / `.svg`, the four-panel trial and average overview;
- `event_trials_<marker>.png`, every trial of that marker overlaid in two panels;
- `event_heatmap_<marker>.png`, with the dF/F0 trials above and the Z-score trials below;
- `markers_working_copy.csv` and `parameters_and_marker_edits.json`;
- AAPlot-compatible `Time,Signal,Marker` text files in the `AAPlot_corrected_trace`,
  `AAPlot_dFF0`, and `AAPlot_zscore` subfolders.

Original input files are read-only and are never modified.

## Updates

### 2026-09-25

- Added trial-level marker selection in **Event Analysis**. Matching markers are selected
  by default, but any contiguous or non-contiguous subset can be analyzed.
- Added independent event-signal selection for corrected 410, 470, and 560 traces plus the
  ratio defined by the current analysis/reference wavelength pair. Signals can be selected
  individually, in any combination, or all at once.
- Changed `event_aligned_trials.csv` and `event_aligned_average.csv` from stacked long-form
  signal rows to wide, signal-grouped columns. Columns are ordered as common event
  information, 410 results, 470 results, optional 560 results, then ratio results.
- Event figures, CSV output, and `parameters_and_marker_edits.json` now retain the exact
  marker subset and signal selection used for each calculation.

### 2026-10-27

- Reworked the multi-start fitting path: every candidate start is screened on a ~600-point
  subsample and only the three best are refined on the full fitting data, while each model
  keeps its previous iteration budget (single-exponential 3000/5000, double-exponential
  5000/8000). Fitting a selected trace is 2–4× faster and the fitted curve stays within
  noise of the previous implementation.
- Added a fit cache keyed by data version and channel settings, so re-applying the same
  correction, or correcting a single changed wavelength, no longer refits every channel.
- Fitting now runs on a worker thread with a progress bar and a **Cancel Fit** button,
  keeping the window responsive; results whose data version or fit settings changed in the
  meantime are discarded instead of applied.
- Panning and zooming no longer redraw the full traces. Curves are drawn with a min/max
  envelope, resampled from the full series whenever the X range changes, and switched to a
  coarse copy while dragging.
- Markers are drawn as a single `vlines` artist and the fitting-window scatter is
  decimated, reducing the per-frame redraw cost.
- Set `AAPLOT_FIT_EXACT=1` before launching to reproduce the previous fitting path bit for
  bit.
- Removed the `subtraction` channel combination; a reference wavelength is always combined
  as `ratio`. The **Combine Channels** field keeps only that option.
- Renamed the normalization tab to **Full trace analysis**, left smoothing and downsampling
  off by default, and renamed its baseline section to **dF/F0/Z-score Baseline**.
- Event defaults are now 5 s before and 30 s after the marker with a 5 s baseline.
- Split **Export Results** into a **Full trace plot** column and an **Event trials** column;
  event defaults are the two CSV outputs.
- `event_trials_<marker>.csv` replaces `event_aligned_trials.csv`: a shared `Time_s` column,
  per-trial dF/F0 columns, one empty separator column, then the per-trial Z-score columns.
- Added per-marker overlayed trial figures (`event_trials_<marker>.png`) and per-marker
  heatmaps (`event_heatmap_<marker>.png`).
- Added a Windows launcher self-check. `run_RWD_fiber_pretreatment.bat` now locates Anaconda,
  creates or reuses the `aaplot` conda environment, validates `requirements.txt` with the new
  `env_check.py`, installs missing packages with `pip`, and starts `app.py` with that
  environment's interpreter; `--no-install` only checks and `--recreate` rebuilds the env.

### 2026-10-28

- Optimised the bleaching correction algorithm. The corrected trace is
  `(raw − offset) × F(t₀) / F(t)`, where `F(t)` is the independently fitted bleaching
  curve. Dividing by the bleaching factor normalised at t₀ un-bleaches the transient
  itself and keeps the fluorescence scale of `raw − offset`, so the user baseline no longer
  controls the output level.
- The offset is subtracted before the fit and therefore reaches F0 directly. Omitting it
  no longer hides inside the fitted constant: it inflates F0 and compresses every dF/F0 by
  `F_true / (F_true + residual offset)`.
- The corrected level is no longer pinned to a user-entered value, so a large baseline can
  no longer push the corrected trace above the raw data.
- Division by a near-zero fitted value is now rejected with an explicit message instead of
  producing spikes, and `fixed`-mode baselines that disagree with the plateau of the data
  are reported in `fit_warnings`.
- Corrected traces and the ratio are anchored on `F(t₀)` per channel, so the expected ratio
  level equals `F(t₀)_analysis / F(t₀)_reference` and is written to the output details.
- Added the `difference` channel combination next to `ratio`, so the reference can be
  subtracted with an explicit weight instead of divided. Only the reference deviation is
  removed, the analysis level and dF/F0 scaling are unchanged, and the reference-noise and
  zero-crossing failure modes of the ratio are avoided. A blank weight uses the matched
  scale, which reproduces the ratio to first order.
- Added **Restore From Saved JSON**. After loading a recording, the button opens a file
  dialog anchored at that recording folder so the user picks the exact
  `parameters_and_marker_edits.json` to restore — one mouse folder can hold several
  outputs. The file is compared parameter by parameter with the current settings before
  anything is applied, and saved fitted curves are rebuilt from their parameters and seeded
  into the fit cache, so applying the restored configuration refits only the channels whose
  fit inputs changed; changing the channel combination, the reference weight or the
  smoothing refits nothing.
- Event analysis now uses the stored combination trace instead of recomputing a ratio of
  its own, so switching between `ratio` and `difference` and changing the reference weight
  flow through to the trial traces, the mean ± SEM curves and the exported columns. Those
  results are cleared when the combination changes, because they were computed from the
  previous trace.
- The **Reference Weight** field now has an **Estimate** button. It fits the weight on all
  samples outside the marker windows, converts the result to the count scale of the field,
  and reports the matched scale beside it for comparison. The algorithm, its unit
  conversion and the acceptance checks are written out in
  **Choosing the reference weight**. In a synthetic recording whose
  reference reacted 2.5x more strongly than its level implied, the estimate landed within
  2 % of the grid-search optimum and cut the artifact deflection from 2.76 % to 0.29 %
  while leaving the event amplitude unchanged.

## Author and development

Developed by **Ruyi Cai**, **Sophielothia** and **nexsrust** at **Peking University**, with
development assistance from **OpenAI Codex** and **DeepSeek Harness**.
