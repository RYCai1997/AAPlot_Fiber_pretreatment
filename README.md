# RWD fiber pretreatment

This folder is a standalone interactive pretreatment program. Double-click
`run_RWD_fiber_pretreatment.bat` to start it.

## Dynamic 410 / 470 / 560 support

`Fluorescence.csv` must contain `TimeStamp` and at least one supported fluorescence
column named `CH<n>-410`, `CH<n>-470`, or `CH<n>-560`. The file may contain any
combination of these wavelengths for CH1, CH2, CH3, and later acquisition channels.

The program detects the wavelengths separately for each recording channel. Missing
410 or 470 data are valid: their controls and plots are hidden. 560 is a complete
first-class channel with the same independent offset, bleaching fit, correction,
dF/F0, Z-score, plotting, and export support as 410 and 470.

For every visible wavelength:

1. Enter its offset and fitting baseline.
2. Open its fitting window, select one or more fitting regions, and choose the model.
3. Click **Apply Correction**. Every available wavelength is corrected independently.

Use **Analysis Wavelength** to choose the signal for whole-recording normalization
and event analysis. **Reference Wavelength** may be another available wavelength or
`None`. With a reference, the analysis trace is the selected ratio or subtraction;
with `None`, the independently corrected analysis wavelength is used directly. This
makes a corrected 560-only analysis available even when 410 is present.

## Correction and fitting

- Each wavelength is fitted independently; one wavelength is never used as the
  regression basis for another.
- Corrected channel: `(raw - offset) / independently fitted bleaching × user baseline`.
- Each fitting window supports linear, single-exponential, and double-exponential
  models and one or more draggable fitting regions.
- The user baseline initializes the freely fitted constant (`fit_constant`).
- Changing the effective range, offset, baseline, or model invalidates that
  wavelength's saved fit and requires confirmation again.

## dF/F0 and Z-score

Applying correction does not reuse fitting regions as the normalization baseline.
On **dF/F0 & Z-score**, select a manual or pre-marker baseline and optional relative
time, then calculate normalization.

`F0` is the mean corrected analysis trace in the baseline. dF/F0 is
`100 × (F - F0) / F0`; Z-score uses the dF/F0 mean and sample SD in the same baseline.
The program also calculates and retains separate values for every corrected wavelength,
including `dff_560_percent` and `zscore_560` whenever 560 exists.

Smoothing and downsampling are applied only after **Apply Display Processing**. The
same applied settings are used on screen and in PNG, CSV, and AAPlot-compatible output.

## Marker-aligned event analysis

The **Event Analysis** tab operates on the current analysis trace. To analyze 560 by
itself, choose 560 as **Analysis Wavelength** and `None` as **Reference Wavelength**,
apply correction, then calculate event dF/F0 and Z-score. To analyze 560/410 or
560−410, choose 560 and 410 with the corresponding combination.

Every marker with the selected name is treated as one trial. Each complete trial is
interpolated to a common time axis and normalized using its own pre-marker baseline.
Outputs include trial-level and mean ± SEM dF/F0 and Z-score tables and figures.

## Result output

Depending on the selected boxes, a new non-overwriting result folder contains:

- `corrected_fluorescence_trace.csv` / `.png`, including every available corrected wavelength;
- `dFF0_trace.csv` / `.png` / `.svg`, including per-wavelength dF/F0;
- `zscore_trace.csv` / `.png` / `.svg`, including per-wavelength Z-score;
- `event_aligned_trials.csv`, `event_aligned_average.csv`, and event figures;
- `markers_working_copy.csv` and `parameters_and_marker_edits.json`;
- selected AAPlot-compatible `Time,Signal,Marker` text output subfolders.

Original input files are read-only and are never modified.

## Author and development

Developed by **Ruyi Cai** at **Peking University**, with development assistance from
**OpenAI Codex**.

Contributor: **nexsrust**.
