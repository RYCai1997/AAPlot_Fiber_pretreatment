# RWD Fiber Pretreatment

A standalone, interactive pretreatment tool for RWD fiber photometry recordings. Open one
recording folder, fit and remove the bleaching decay of every available wavelength, combine the
analysis and reference channels the way the RWD software does, compute dF/F0 and Z-score traces,
run marker-aligned event analysis, and export figures, tables and AAPlot-compatible text files.
Input files are opened read-only and are never modified.

## Install and start

- A Python 3 installation, or Anaconda on Windows.
- `pip install -r requirements.txt` (`matplotlib`, `numpy`, `pandas`, `scipy`).
- Start with `run_RWD_fiber_pretreatment.bat`, or `python app.py` in a prepared environment.

On Windows the launcher self-checks the environment: it locates Anaconda, creates the `aaplot`
conda environment (Python 3.12) when missing, verifies every package in `requirements.txt`
through `env_check.py`, installs what is missing with `pip`, and starts `app.py` with that
environment's interpreter. `--no-install` only reports the check result, `--recreate` rebuilds
the environment, and `AAPLOT_NO_PAUSE=1` skips the final pause for automation.

## Input folder

`Fluorescence.csv` is required. Its first line is free-form metadata and is skipped; the table
starts on the second line.

- `TimeStamp` (milliseconds) is required, plus at least one column named `CH<n>-410`,
  `CH<n>-470` or `CH<n>-560`, where `<n>` is the acquisition channel. All three wavelengths are
  first-class: 410, 470 and 560 each get their own offset, bleaching fit, correction, dF/F0,
  Z-score, plot and export. Any subset per channel is valid, and missing controls are simply
  hidden.
- `Events.csv` is optional. Rows with `State` equal to `1` become markers; if `State` is missing
  or selects nothing, every row is used. The marker name comes from the `Name` column.
- Rows without a valid `TimeStamp` are dropped and the table is sorted by time. At least 20
  valid samples are required.
- A short run of empty or non-numeric cells in a fluorescence column — up to 3 samples, about
  0.1 s at 30 Hz — is **bridged on load** by linear interpolation between its neighbours, so the
  sample count, the timestamps and every derived trace stay unchanged and the fit never sees a
  hole. Longer runs are **not** bridged: they stay NaN, are reported as a gap, and are left out
  of the fit. Both cases are stated in the status line and recorded under
  `fit_details.gap_fills` in the saved JSON.

Markers can be added, deleted or restored in **Markers (Working Copy)** on the **Data & Fitting**
tab. Edits are kept in memory and written to `markers_working_copy.csv` on export.

## Workflow

1. **Data & Fitting** — click **Select Recording Folder**, choose the **Recording Channel**, set
   the **Valid Data Range**, then enter each visible **Offset** and **Fitting Baseline**. The
   baseline is the **lowest point of the trace after bleaching** — the plateau the signal
   decays to — read off the **raw** plot, so it still contains the offset: the program subtracts
   the offset from it and uses the result as F0, the level the corrected trace sits on and the
   denominator of dF/F0. A baseline
   that is not larger than the offset is rejected with an error naming the channel, because
   nothing would be left of it once the offset is removed.
2. Open **Configure <wavelength> Fit...** for every wavelength, select one or more regions and a
   model, preview, and confirm.
3. Choose the **Analysis Wavelength** and, optionally, a **Reference Wavelength** with a
   **Combine Channels** mode. With a reference the default `rwd` mode fits and subtracts the
   motion contribution; with `None` the independently corrected analysis channel is used.
4. Click **Apply Correction**. Every available wavelength is corrected independently.

Then use **Full trace analysis** for dF/F0 and Z-score, **Event Analysis** for marker-aligned
trials, and **Export Results** to write the folder.

## Defaults at a glance

| Setting | Default | Where |
| --- | --- | --- |
| Channel combination | `rwd` (RWD motion fit) | Data & Fitting |
| F0 for dF/F0 | Fitting Baseline − Offset (per wavelength) | Data & Fitting |
| Fit model | double exponential | Configure Fit |
| PLS `beta` | 8, allowed 5–12 | Configure Fit |
| Smoothing / downsampling | off | Full trace analysis |
| Event window | 5 s before, 30 s after, 5 s baseline | Event Analysis |
| Estimate exclusion window | 5 s before, 60 s after each marker | Data & Fitting |
| Time units | per field (`s` for windows, `min` for ranges), switchable | every time field |

## Documentation

| Page | Contents |
| --- | --- |
| [docs/fitting.md](docs/fitting.md) | Bleaching correction formula, fitting models, PLS fit, fit-window behaviour |
| [docs/channels.md](docs/channels.md) | The three channel combinations, the RWD motion fit, choosing and estimating the reference weight |
| [docs/analysis.md](docs/analysis.md) | Display processing, dF/F0/Z-score baselines, event analysis, exported files |
| [docs/restore.md](docs/restore.md) | Restoring a previous analysis and what gets refitted |
| [docs/updates.md](docs/updates.md) | Changelog, newest first |

## Output

Each export creates a new folder named by **Output Folder Name** (an existing name gets `_2`,
`_3`, ...). Depending on the selected boxes it contains corrected, dF/F0 and Z-score traces as
CSV/PNG/SVG, per-marker event trial and average tables, event overview figures and heatmaps,
`markers_working_copy.csv`, `parameters_and_marker_edits.json` (full settings log), and
AAPlot-compatible `Time,Signal,Marker` text files. See [docs/analysis.md](docs/analysis.md) for
the exact file list.

## Troubleshooting

| Symptom | Cause and fix |
| --- | --- |
| A dialog appears during apply or plotting | The full traceback is also written to `aaplot_errors.log` beside `app.py`, appended, never overwritten — send that file. |
| `Series of different length were trimmed ...` in the status line | Two series reached a plot with different lengths; they are trimmed and reported instead of aborting the redraw. Usually an empty cell in the recording. |
| `6 cells in CH1-410 left as a gap` | More than 3 consecutive samples are missing. The trace keeps a gap there; fill or remove those rows in `Fluorescence.csv` if they are not a real dropout. |
| Apply does nothing / asks to confirm regions | A data-range, model or baseline-mode change invalidates that channel's confirmed fit. Offset and baseline edits do not. |
| Event results vanish after changing the combination | Trials are computed from the stored combination trace, so they are cleared and must be recalculated. |
| A single-wavelength recording | Set **Reference Wavelength** to `None`; the combination is skipped and the corrected channel is analysed on its own. |
| `The fitting baseline for 470 (...) must be larger than the offset` | The baseline is the **raw** level, offset included; F0 = baseline − offset has to stay positive. Enter the level you read off the raw trace, or correct the offset. |
| dF/F0 comes out about twice too large or too small | Check that the **Fitting Baseline** is the raw level of the trace minimum and not an already-subtracted one: F0 is the denominator, so a wrong baseline scales every dF/F0 by the same factor. |

## Author and development

Developed by **Ruyi Cai**, **Sophielothia** and **nexsrust** at **Peking University**, with
development assistance from **OpenAI Codex** and **DeepSeek Harness**.
