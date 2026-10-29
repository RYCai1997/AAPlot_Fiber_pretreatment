# Reusing a previous analysis

A recording that was analysed before can be restored instead of typed in again, and only the
parts that change are recomputed. The file is chosen explicitly: one mouse folder may hold
several `output_*` folders, so nothing is read automatically.

## Choosing the file

After a recording is loaded, **Restore From Saved JSON...** becomes available. It opens a
normal file dialog whose start directory is the recording folder you loaded, or the folder of
the last file you restored. The dialog lists every `.json` file in that directory, so navigate
into `output_<recording>_<channel>[_suffix]` and pick the `parameters_and_marker_edits.json` you
want. Cancel leaves everything untouched. A file that is not an exported parameters file —
invalid JSON, or no `processing` block — is rejected with a message naming the reason, and no
settings change.

## Comparison

The chosen file opens a review window showing its full path, modification time and saved source
channel, followed by one row per difference between the saved configuration and the current
settings, with the saved value beside the current one. Rows whose scope is a wavelength decide
whether that channel is fitted again. The summary above the table names the channels that still
have a usable saved fit and the channels that would be refitted. The data range is compared
numerically, so the sampled end of a recording (`9.99944 min`) is not reported as different from
`10 min`.

**Choose Another File...** reopens the file dialog from the review window, which is the quickest
way to compare two outputs of the same recording before committing to one.

## Initialise

**Initialise From This File** writes the saved values into the interface:

| Restored | Notes |
| --- | --- |
| Data range, offsets, fitting baselines | Per visible wavelength |
| Fitting regions, fit model, baseline mode, PLS beta | Marked `(restored)` in each channel's status |
| Analysis and reference wavelength, combination, reference weight | Combination and weight are applied immediately |
| Smoothing | Enabled only when the saved analysis used it |
| dF/F0/Z-score baseline interval | Set to the saved interval in manual mode |
| Markers | Original markers minus the deleted ones, plus the manual ones |
| Output folder name | The folder that held the saved file |

## Incremental recompute

Each channel's fit is identified by a signature `(data version, channel, model, baseline mode,
offset, baseline, fitting regions, data range)`. A saved fit whose signature matches is rebuilt
from its stored parameters and put straight into the fit cache, so **Apply Correction** reuses
it without fitting anything. Only channels whose signature changed are fitted again.

| Change | Refit needed |
| --- | --- |
| Offset, fitting baseline, fit model, baseline mode, fitting regions, data range | That channel only (data range: every channel) |
| Combination (`rwd` / `difference` / `ratio`), reference weight, analysis or reference wavelength | None |
| Smoothing, output name, markers, dF/F0 baseline interval | None |

The status line reports the split, for example `Reused the saved fit for 410; refitted 470.`
Each channel's parameters in `parameters_and_marker_edits.json` record `fit_reused_from_cache`,
so a re-export documents which fits were reused.

A channel falls back to a fresh fit when its saved model differs from the model now selected,
when the saved parameters are missing, or when the saved fitted curve does not match the current
data range. Those channels appear in the "refit needed" list before the initialisation is
applied.
