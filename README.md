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
   reference, the corrected channels are combined as `ratio`; with `None`, the
   independently corrected analysis wavelength is used directly.
4. Click **Apply Correction**. Every available wavelength is corrected independently.

Whole-recording normalization and event analysis are then configured on the **Full trace
analysis** and **Event Analysis** tabs, and files are written from the **Export Results**
tab.

## Correction and fitting

- Each wavelength is fitted independently; one wavelength is never used as the regression
  basis for another.
- Corrected channel: `(raw - offset) / independently fitted bleaching × user baseline`.
- The fitting window supports linear, single-exponential, and double-exponential models
  and one or more regions that can be dragged or resized; use **Add Region** and
  **Delete Selected Region** to edit the set. **Preview Fit** draws the candidate curve
  and reports its parameters, RMSE, and BIC; **Use These Regions** confirms the fit for
  that wavelength.
- The baseline initializes the freely fitted constant (`fit_constant`).
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
can be analyzed separately, together with the ratio defined by the current **Analysis
Wavelength / Reference Wavelength** pair. Choose one or several signals, or use
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
`ratio_470_410_dF/F0_trial01`) and columns are grouped signal by signal.

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

## Author and development

Developed by **Ruyi Cai**, **Sophielothia** and **nexsrust** at **Peking University**, with
development assistance from **OpenAI Codex** and **DeepSeek Harness**.
