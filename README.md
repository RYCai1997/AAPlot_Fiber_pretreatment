# RWD fiber pretreatment

This folder is a standalone interactive pretreatment program. Double-click `run_RWD_fiber_pretreatment.bat` to start it.

## Processing rules

- 410 is never used as a regression basis for fitting 470.
- The user enters offset and baseline values directly. There is no automatic first-60-second baseline estimate.
- Fitting windows use `fit_constant`: the user's baseline is the initial estimate for a freely fitted constant term. The main window no longer has a redundant model selector; each channel's model is chosen in its own fitting window.
- Corrected channel: `(raw - offset) / independently fitted bleaching × user baseline`.
- Method `fit_both` independently fits 470 and 410, then calculates corrected `470/410` ratio or corrected `470-410` subtraction.
- Method `fit_470_only` fits and displays 470 only; the 410 panel is hidden.
- The program does not automatically display “470 change from baseline.”

## Channel fitting windows

Open the 470 and, when required, 410 fitting windows separately.

1. One shaded interval block is created by default.
2. Drag inside the block to move it; drag its left or right edge to resize it.
3. Click **Add Region** to add more separated fitting regions. Select a block and click **Delete Selected Region** to remove it. Only shaded regions enter parameter estimation.
4. Click **Preview Fit** to inspect the manually selected model.
5. Click **Use These Regions**.

Selected samples are shown as black points over shaded intervals. The raw fitting trace and selected points are drawn at full resolution, and working-copy markers are overlaid. Multi-start search uses a deterministic subset for speed, then the chosen solution is refined using every selected sample. The fitting window reports the parameters, selected sample count, RMSE, BIC, convergence state, iteration count, optimality, and warnings for boundary contact or nearly identical double-exponential time constants. Excluded samples do not enter optimization, final refinement, or model scoring.

Changing effective range, channel offset, channel baseline, or fit model invalidates the corresponding saved fit and requires confirmation again.

## Main trace layout and interaction

- 470 and 410 always use separate plots.
- In 470-only mode, the 410 plot is absent.
- Ratio mode adds a dedicated corrected-ratio plot; subtraction mode adds a corrected-difference plot.
- 470 fluorescence is green, 410 fluorescence is blue, and fitted curves are red dashed lines.
- After fitting, original offset-adjusted fluorescence and corrected fluorescence can be overlaid or displayed individually.
- Every raw, fitted, corrected, combined, dF/F0, and Z-score curve has an independent thickness setting below the plot area. The fitting window separately controls signal and fit line widths.
- Horizontal grids are always off.
- Each plot has Y-lower-endpoint, Y-range, and Y-upper-endpoint inputs on the right. The bottom has X-left-endpoint, time-range, and X-right-endpoint inputs. Entering or leaving any field calculates the third value from the edited field and one other available value, then immediately updates the axes; there is no separate Apply button.
- The right-side view controls include **Restore Initial View**, **Auto-Scale All Y**, and per-plot `Y−` / `Y+` buttons for reducing or enlarging that panel's Y range around its center.
- The bottom controls include **Narrow X Range** and **Widen X Range**. X scaling stays centered on the current window and is clamped to the full trace range.
- Drag the bottom X axis horizontally to pan every plot together. Drag the left Y axis of any individual plot vertically to move only that plot's Y window. Axis dragging works outside the plot area, while drag-box zoom works inside it.
- Moving the mouse over any plot displays a blitted crosshair; the Y label is drawn inside the left edge so it is not clipped.
- Mouse-wheel zoom is disabled. Drag-box zoom is always enabled—drag a rectangle inside one plot to apply its X range to all plots and its Y range to the active plot. The crosshair remains available whenever a rectangle is not actively being dragged and returns immediately after selection. Use toolbar **Home** or **Restore Initial View** to restore the full view.
- **Auto-Scale All Y** rescales each visible plot using trace values inside the current X window.

## Smoothing and downsampling

The **dF/F0 & Z-score** tab provides independent controls for:

- optional rolling smoothing, with a window in seconds;
- optional downsampling by sample-point interval or time interval in seconds.

The controls default to 10-second smoothing and 1-second downsampling, but these are pending settings: loading data and applying a fit still show the unprocessed full-resolution trace. They take effect only after clicking **Apply Display Processing**. Once applied, the same smoothing and row downsampling are used for the screen, PNG, CSV, and AAPlot-compatible outputs. CSVs retain both original and relative time columns; when smoothing is active, the canonical signal column contains the smoothed result and an `_unsmoothed` companion column preserves the corresponding unsmoothed values at the exported rows.

## dF/F0 and Z-score

Applying the fit creates corrected fluorescence traces only. It does not silently reuse fitting regions as a normalization baseline.

1. Open **dF/F0 & Z-score**.
2. Select the baseline mode:
   - manually enter start/end in original recording minutes; or
   - select a marker and use the specified duration immediately before that marker.
3. Relative time is enabled by default. Select a marker or a specific original recording time as zero, or disable relative time when it is not wanted.
4. Click **Calculate dF/F0 & Z-score**.

The program calculates `F0` as the mean corrected analysis trace inside that interval. dF/F0 is `100 × (F - F0) / F0`. Z-score center and SD are calculated from dF/F0 samples inside the same interval. Dedicated dF/F0 and Z-score panels are then added to the right plot area, with the baseline interval shaded. When time zero is enabled, plots use negative/positive relative time, while `original_time_s` and `original_time_min` remain in the underlying data and CSV exports.

## Marker and outputs

Marker controls are located on **Data & Fitting**. Original markers are loaded into a working copy. Adding, deleting, and restoring markers never modifies `Events.csv`.

The export tab independently selects CSV and PNG output for corrected fluorescence, dF/F0, and Z-score. Corrected-fluorescence CSV is unchecked by default. The editable result-folder name defaults to `output_<input folder name>`; each save creates that folder directly inside the input recording folder, and appends `_2`, `_3`, etc. instead of overwriting an existing result. Depending on the selected boxes it can contain:

- `corrected_fluorescence_trace.csv` / `.png`
- `dFF0_trace.csv` / `.png`
- `zscore_trace.csv` / `.png`
- `markers_working_copy.csv`
- `parameters_and_marker_edits.json`

dF/F0 or Z-score export is blocked until normalization has been calculated. Changing the baseline definition or time-zero definition requires recalculation before export. Applying a new smoothing setting regenerates the displayed/exported smoothed traces; changing downsampling changes the exported row selection without altering the underlying full-resolution calculation.

Selecting a different input recording resets all analysis, display, line-width, normalization, marker-entry, zoom, axis, and export options to their defaults before the new data are shown.

## AAPlot-compatible outputs

When the corresponding CSV output is selected, the save folder also contains the matching AAPlot-compatible subfolder:

- `AAPlot_corrected_trace`
- `AAPlot_dFF0`
- `AAPlot_zscore`

Each contains one comma-separated `.txt` file whose first three columns are `Time` in seconds, `Signal`, and `Marker`. When time zero is enabled, this `Time` column is relative time; full CSV exports still retain original and relative time columns. This is the format read by `D:\Coding\AAPlot\GUI\fiber_trace_spike2_analysis_batch_gui.py`. Signal types are separated into folders so AAPlot does not interpret them as separate animals in one batch.

For ratio/subtraction processing, the corrected analysis trace is the corrected ratio/difference. For 470-only processing, it is corrected 470. Normalization always uses the independently specified normalization baseline interval, not the exponential fitting regions.
