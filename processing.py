"""Read-only data loading and wavelength-independent photometry fitting."""
from __future__ import annotations

import copy
import hashlib
import json
import os
import uuid
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import numpy as np
import pandas as pd
from scipy import sparse
from scipy.optimize import least_squares
from scipy.sparse.linalg import spsolve


@dataclass
class ProcessingConfig:
    range_start_min: float
    range_end_min: float
    offsets: dict[str, float] = field(default_factory=dict)
    baselines: dict[str, float] = field(default_factory=dict)
    wavelengths: list[str] = field(default_factory=list)
    analysis_wavelength: str = "470"
    reference_wavelength: str | None = "410"
    # "difference": corrected_primary - weight * (corrected_reference - its level).
    # "ratio": corrected_primary / corrected_reference.
    combine: str = "difference"
    # Weight of the reference channel in difference mode. None keeps the matched
    # scale (primary level / reference level), which reproduces the ratio to first
    # order but without dividing by a noisy reference.
    reference_weight: float | None = None
    fit_model: str = "double_exponential"
    smooth_seconds: float = 10.0
    fit_regions: dict[str, list[tuple[float, float]]] = field(default_factory=dict)
    fit_models: dict[str, str | None] = field(default_factory=dict)
    fit_baseline_modes: dict[str, str | None] = field(default_factory=dict)
    # Penalised-least-squares settings, per wavelength (used when the model is "penalized").
    fit_betas: dict[str, float | None] = field(default_factory=dict)
    fit_lambdas: dict[str, float | None] = field(default_factory=dict)
    # PLS smoothing window in seconds, per wavelength. None keeps the automatic choice, which is
    # never finer than PLS_MIN_CUTOFF_SECONDS.
    fit_smoothing: dict[str, float | None] = field(default_factory=dict)
    # Windows (minutes, absolute) that never enter a bleaching fit: the marker windows, so a
    # response is not absorbed by the baseline it is measured against.
    exclusion_windows: tuple[tuple[float, float], ...] = ()
    source_channel: str = "CH1"
    # "additive": raw = offset + B(t) + s(t), the bleach is subtracted.
    # "multiplicative": raw = offset + B(t)·(1 + s(t)), the bleach is divided out, which keeps
    # the relative size of a response constant as the probe fades. Probe bleaching itself is
    # multiplicative; a drifting lamp or cable is additive, so the choice follows what the
    # decay in these recordings actually is.
    bleach_correction: str = "additive"


SUPPORTED_WAVELENGTHS = ("410", "470", "560")

# Multi-start budgeting. Every start is still evaluated; cheap starts are
# screened on a coarse subsample and only the most promising ones are refined
# on the full sample, then the winner is refined on every selected sample.
# The model, bounds, loss and the complete start set are unchanged.
MULTISTART_SCREEN_POINTS = 600
MULTISTART_SCREEN_MAX_NFEV = 600
MULTISTART_SELECT_TOP = 3
MULTISTART_REFINE_MAX_NFEV = 5000
FINAL_REFINE_MAX_NFEV = 8000
OPTIMIZER_TOLERANCE = 1e-6

# AAPLOT_FIT_EXACT=1 restores the pre-optimization behaviour: every start runs
# to convergence on the full sample and the default tolerances are used. It is
# slower, but it reproduces the historical curves bit for bit when a narrow fit
# region leaves the two-exponential solution under-determined.
FIT_EXACT = os.environ.get("AAPLOT_FIT_EXACT", "").strip().lower() in {"1", "true", "yes", "on"}

# Penalised least squares ("PLS fit"). The weight sharpness and the smoothing
# strength are capped at the values that stay stable on a steep initial decay.
# RWD exposes a single correction coefficient beta, documented as 5-12 with a default
# of 8; the smoothing strength is internal and not user-selectable.
PLS_MIN_BETA = 5.0
PLS_DEFAULT_BETA = 8.0
PLS_MAX_BETA = 12.0
PLS_DEFAULT_LAMBDA = 10.0
PLS_MAX_LAMBDA = 10.0
PLS_MAX_POINTS = 1200
PLS_KNOT_COUNT = 200
# The reweighting can oscillate for a few rounds before it settles, so the cap is
# generous and the tolerance is stated relative to the span of the log signal.
PLS_MAX_ITERATIONS = 100
PLS_TOLERANCE = 1e-5
# The baseline may not be smoother than this, whatever the legacy penalty works out to: below it
# the fit starts following responses instead of the decay, and the correction removes them.
PLS_MIN_CUTOFF_SECONDS = 15.0

# Per-start and final budgets of the historical implementation, kept identical
# so that a converged solution does not change (only the screening stage is new).
_SINGLE_MAX_NFEV = 3000
_SINGLE_FINAL_MAX_NFEV = 5000


class ProcessingCancelled(RuntimeError):
    """Raised when the user cancels a background processing job."""


def available_recording_channels(data: pd.DataFrame) -> dict[str, list[str]]:
    """Return acquisition channels and their available supported wavelengths."""
    found: dict[str, list[str]] = {}
    for column in data.columns:
        match = re.fullmatch(r"(CH\d+)-(410|470|560)", str(column))
        if match:
            found.setdefault(match.group(1), []).append(match.group(2))
    order = {wavelength: index for index, wavelength in enumerate(SUPPORTED_WAVELENGTHS)}
    return {
        channel: sorted(set(wavelengths), key=lambda value: order[value])
        for channel, wavelengths in sorted(found.items(), key=lambda item: int(item[0][2:]))
    }


# A run of missing samples shorter than this many samples (about 0.1 s at 30 Hz) is
# bridged by linear interpolation; longer runs are left as gaps so a real dropout is
# never hidden.
GAP_FILL_MAX_SAMPLES = 3

# An event window may not be built across a longer hole than this. Recording pauses,
# lamp failures and dropped frames show up as holes; a hole a few samples wide is a
# dropped frame and is interpolated, but anything approaching a second is a real
# absence of signal and interpolating it would invent a trial.
MAX_EVENT_GAP_SECONDS = 1.0


def fill_short_gaps(
    values: np.ndarray,
    max_run: int = GAP_FILL_MAX_SAMPLES,
) -> tuple[np.ndarray, dict[str, Any]]:
    """Bridge short runs of missing samples, keeping everything else a gap.

    Linear interpolation is used between the neighbouring valid samples, with the nearest
    valid value held for a run that touches either end. Runs longer than ``max_run`` are
    left untouched, so a real dropout (a lamp off, a disconnected channel) still shows up
    instead of being averaged over.
    """
    original = np.asarray(values, float)
    filled = original.copy()
    missing = ~np.isfinite(original)
    info: dict[str, Any] = {"filled": 0, "filled_runs": 0, "left": 0,
                            "longest_filled_run": 0, "longest_left_run": 0}
    if not missing.any():
        return filled, info
    good = ~missing
    if good.sum() < 2:
        info["left"] = int(missing.sum())
        return filled, info
    index = np.arange(filled.size)
    bridge = np.interp(index, index[good], original[good])
    run_starts = np.flatnonzero(missing & np.r_[True, ~missing[:-1]])
    run_ends = np.flatnonzero(missing & np.r_[~missing[1:], True])
    for start, end in zip(run_starts, run_ends):
        length = int(end - start + 1)
        if length <= max_run:
            filled[start:end + 1] = bridge[start:end + 1]
            info["filled"] += length
            info["filled_runs"] += 1
            info["longest_filled_run"] = max(info["longest_filled_run"], length)
        else:
            info["left"] += length
            info["longest_left_run"] = max(info["longest_left_run"], length)
    return filled, info


def recording_fingerprint(path: Path, sample_bytes: int = 1 << 20) -> str:
    """Identity of a recording file, cheap enough for a multi-hundred-MB CSV.

    The size plus the hash of the first and last megabyte is enough to tell two
    recordings apart (they differ in length or in their head/tail numbers) while
    staying fast on the 46 MB-to-GB files this tool sees. It exists so a saved
    fit can never be replayed onto a different recording.
    """
    path = Path(path)
    size = path.stat().st_size
    digest = hashlib.sha256()
    digest.update(str(size).encode())
    with path.open("rb") as handle:
        digest.update(handle.read(sample_bytes))
        if size > 2 * sample_bytes:
            handle.seek(size - sample_bytes)
            digest.update(handle.read(sample_bytes))
    return f"{size}:{digest.hexdigest()[:32]}"


def load_recording(folder: Path) -> tuple[pd.DataFrame, list[dict[str, Any]], str]:
    path = folder / "Fluorescence.csv"
    if not path.exists():
        raise FileNotFoundError(f"File not found: {path}")
    fingerprint = recording_fingerprint(path)
    with path.open("r", encoding="utf-8-sig", errors="replace") as handle:
        metadata = handle.readline().strip()
    data = pd.read_csv(path, skiprows=1, low_memory=False)
    data = data.loc[:, ~data.columns.astype(str).str.startswith("Unnamed")]
    if "TimeStamp" not in data:
        raise ValueError("Fluorescence.csv is missing the required TimeStamp column.")
    channels = available_recording_channels(data)
    if not channels:
        raise ValueError(
            "Fluorescence.csv does not contain any supported fluorescence columns "
            "(expected CH<n>-410, CH<n>-470, or CH<n>-560)."
        )
    numeric_columns = ["TimeStamp", *(f"{channel}-{wavelength}"
                                      for channel, wavelengths in channels.items()
                                      for wavelength in wavelengths)]
    for column in numeric_columns:
        data[column] = pd.to_numeric(data[column], errors="coerce")
    data = data.dropna(subset=["TimeStamp"]).sort_values("TimeStamp").reset_index(drop=True)
    if len(data) < 20:
        raise ValueError("Fewer than 20 valid data points are available.")
    # One empty cell should not turn into a gap (or a shape mismatch) downstream, so short
    # runs are bridged here, once, before anything fits or plots the trace.
    gap_fills: dict[str, dict[str, Any]] = {}
    time_min = data["TimeStamp"].to_numpy(float) / 60000
    for column in numeric_columns[1:]:
        values = data[column].to_numpy(float)
        if np.isfinite(values).all():
            continue
        filled, info = fill_short_gaps(values)
        if info["filled"]:
            data[column] = filled
        if info["filled"] or info["left"]:
            first_missing = np.flatnonzero(~np.isfinite(values))
            info["first_time_min"] = float(time_min[first_missing[0]])
            info["column"] = column
            gap_fills[column] = info
    data.attrs["gap_fills"] = gap_fills
    data.attrs["source_fingerprint"] = fingerprint
    data.attrs["source_path"] = str(path)

    markers: list[dict[str, Any]] = []
    events_path = folder / "Events.csv"
    if events_path.exists() and events_path.stat().st_size:
        events = pd.read_csv(events_path)
        if "TimeStamp" in events:
            events["TimeStamp"] = pd.to_numeric(events["TimeStamp"], errors="coerce")
            if "State" in events:
                events["State"] = pd.to_numeric(events["State"], errors="coerce")
                selected = events.loc[events["State"] == 1].copy()
                if selected.empty:
                    selected = events.copy()
            else:
                selected = events.copy()
            for _, row in selected.dropna(subset=["TimeStamp"]).iterrows():
                markers.append({
                    "id": str(uuid.uuid4()),
                    "time_min": float(row["TimeStamp"]) / 60000,
                    "name": str(row.get("Name", "event")),
                    "source": "original",
                })
    return data, markers, metadata


def sample_indices(length: int, maximum: int) -> np.ndarray:
    if length <= maximum:
        return np.arange(length)
    return np.unique(np.linspace(0, length - 1, maximum).astype(int))


def region_mask(time_min: np.ndarray, regions: list[tuple[float, float]]) -> np.ndarray:
    mask = np.zeros(len(time_min), dtype=bool)
    for start, end in regions:
        low, high = sorted((float(start), float(end)))
        mask |= (time_min >= low) & (time_min <= high)
    return mask


def _tolerances() -> dict[str, float]:
    if FIT_EXACT:
        return {}
    return {"ftol": OPTIMIZER_TOLERANCE, "xtol": OPTIMIZER_TOLERANCE, "gtol": OPTIMIZER_TOLERANCE}

def _final_refine_nfev(model: str) -> int:
    """Budget of the last all-selected-samples refinement (historical values)."""
    if model == "single_exponential":
        return _SINGLE_FINAL_MAX_NFEV
    return FINAL_REFINE_MAX_NFEV


def _start_nfev(model: str) -> int:
    """Per-start budget of the historical multi-start loop."""
    if model == "single_exponential":
        return _SINGLE_MAX_NFEV
    return MULTISTART_REFINE_MAX_NFEV


def _best_result(results: list[Any]) -> Any:
    """Pick the lowest-cost result, ranking non-finite costs last."""
    def sort_key(item: Any) -> float:
        cost = float(item.cost)
        return cost if np.isfinite(cost) else np.inf
    return min(results, key=sort_key)


def _rank_candidates(results: list[Any]) -> list[Any]:
    """Order screened candidates by cost, breaking near-ties by start order.

    窄选区下双指数存在多个代价等价解，此时按原起点顺序决胜，使筛选结果与
    「逐起点全量精炼」的旧实现一致。
    """
    costs = [float(item.cost) for item in results]
    finite = [cost for cost in costs if np.isfinite(cost)]
    base_cost = min(finite) if finite else 0.0
    scale = abs(base_cost) if base_cost > 0.0 else 1.0

    def sort_key(entry: tuple[int, float]) -> tuple[float, int]:
        index, cost = entry
        if not np.isfinite(cost):
            return (np.inf, index)
        return (round(cost / scale, 2), index)

    order = sorted(enumerate(costs), key=sort_key)
    return [results[index] for index, _cost in order]


def _multi_start_curve(
    starts: list[np.ndarray],
    curve,
    model: str,
    xf: np.ndarray,
    yf: np.ndarray,
    xs: np.ndarray,
    ys: np.ndarray,
    lower: np.ndarray,
    upper: np.ndarray,
    noise: float,
) -> Any:
    """Screen every start on a coarse subsample, then refine only the best few.

    Every start in ``starts`` is still evaluated; the model, bounds, loss and
    the full start set are unchanged. ``xs/ys`` is the cheap screening sample,
    ``xf/yf`` the full fitting sample used for the refined candidates. When the
    screening sample already is the full sample (fewer valid points than
    ``MULTISTART_SCREEN_POINTS``) a 600-nfev screen would rank unconverged
    starts, so every start is fitted on the full sample with its historical
    budget instead.
    """
    tol = _tolerances()
    start_budget = _start_nfev(model)
    same_sample = len(xs) == len(xf) and np.array_equal(xs, xf)
    if FIT_EXACT or same_sample:
        results = [
            least_squares(lambda p: curve(p, xf) - yf, start, bounds=(lower, upper),
                          loss="soft_l1", f_scale=noise, max_nfev=start_budget, **tol)
            for start in starts
        ]
        return _best_result(results)
    screened = [
        least_squares(lambda p: curve(p, xs) - ys, start, bounds=(lower, upper),
                      loss="soft_l1", f_scale=noise, max_nfev=MULTISTART_SCREEN_MAX_NFEV, **tol)
        for start in starts
    ]
    screened = _rank_candidates(screened)
    refined = [
        least_squares(lambda p: curve(p, xf) - yf, candidate.x, bounds=(lower, upper),
                      loss="soft_l1", f_scale=noise, max_nfev=start_budget, **tol)
        for candidate in screened[: max(1, MULTISTART_SELECT_TOP)]
    ]
    return _best_result(refined)


def fit_bleaching(
    time_s: np.ndarray,
    values: np.ndarray,
    baseline: float,
    model: str,
    fit_mask: np.ndarray,
    baseline_mode: str = "fixed",
    beta: float = PLS_DEFAULT_BETA,
    lam: float = PLS_DEFAULT_LAMBDA,
    smoothing_seconds: float | None = None,
) -> tuple[np.ndarray, dict[str, Any]]:
    """Fit one channel using only selected regions.

    Exponential models use deterministic multi-start optimization to reduce
    local-minimum failures, then refine the best solution on every selected
    sample. ``baseline_mode='fixed'`` fixes the constant term at the user's
    value. ``baseline_mode='fit_constant'`` uses that value only as the
    initial estimate and fits the constant term. Excluded samples never enter
    fitting, refinement, or scoring.
    """
    if baseline_mode not in {"fixed", "fit_constant"}:
        raise ValueError(f"Unknown baseline fitting mode: {baseline_mode}")
    if model == "none":
        # No bleaching correction. The offset is still removed upstream, and the declared level is
        # returned as a flat curve, so the caller's "place the trace on F0" step becomes the
        # identity: the corrected channel is (raw - offset) itself, decay included.
        x_none = np.asarray(time_s, float) - float(time_s[0])
        y_none = np.asarray(values, float)
        valid_none = np.asarray(fit_mask, bool) & np.isfinite(x_none) & np.isfinite(y_none)
        if valid_none.sum() < 20:
            raise ValueError("The fitting regions contain fewer than 20 valid points in total.")
        level = float(baseline)
        return np.full(y_none.shape, level, float), {
            "model": "none",
            "baseline_mode": baseline_mode,
            "fixed_baseline": level,
            "fitted_constant": level,
            "fit_samples": int(valid_none.sum()),
            "final_refinement_samples": int(valid_none.sum()),
            "score_samples": int(valid_none.sum()),
            "cost": 0.0,
            "rss_selected": 0.0,
            "rmse_selected": float("nan"),
            "bic_selected": float("nan"),
            "aicc_selected": float("nan"),
            "optimizer_success": True,
            "optimizer_status": 0,
            "optimizer_message": "no bleaching correction",
            "optimizer_nfev": 0,
            "optimizer_optimality": 0.0,
            "optimizer_active_mask": [0, 0],
            "correction_disabled": True,
            "fit_warnings": [],
        }
    x = np.asarray(time_s, float) - float(time_s[0])
    y = np.asarray(values, float)
    valid = np.asarray(fit_mask, bool) & np.isfinite(x) & np.isfinite(y)
    if valid.sum() < 20:
        raise ValueError("The fitting regions contain fewer than 20 valid points in total.")
    valid_indices = np.flatnonzero(valid)
    keep = sample_indices(len(valid_indices), 4000)
    xf, yf = x[valid_indices[keep]], y[valid_indices[keep]]
    xs: np.ndarray | None = None
    ys: np.ndarray | None = None
    if model == "penalized":
        # A global smoother, so every sample is used rather than only the regions;
        # the regions still decide which samples score the fit.
        fitted, parameters = fit_penalized_baseline(
            x, y, beta=beta, lam=lam, smoothing_seconds=smoothing_seconds, sample_mask=valid)
        score_indices = valid_indices[sample_indices(len(valid_indices), 2000)]
        residuals = y[score_indices] - fitted[score_indices]
        rss = max(float(np.sum(residuals ** 2)), np.finfo(float).tiny)
        score_n = len(score_indices)
        parameter_count = 2
        bic = score_n * np.log(rss / score_n) + parameter_count * np.log(score_n)
        aic = score_n * np.log(rss / score_n) + 2 * parameter_count
        pls_warnings: list[str] = list(parameters.get("smoothing_warnings") or [])
        if not parameters.get("converged", True):
            pls_warnings.append(
                f"The PLS reweighting reached the {PLS_MAX_ITERATIONS}-iteration limit; "
                "the baseline may still be settling"
            )
        parameters.update({
            "baseline_mode": baseline_mode,
            "fit_samples": int(valid.sum()),
            "final_refinement_samples": int(parameters.get("fit_points", valid.sum())),
            "score_samples": int(score_n),
            "cost": float(np.sqrt(rss / score_n)),
            "rss_selected": rss,
            "rmse_selected": float(np.sqrt(rss / score_n)),
            "bic_selected": float(bic),
            "aicc_selected": float(aic),
            "optimizer_success": bool(parameters.get("converged", True)),
            "optimizer_status": 0 if parameters.get("converged", True) else 1,
            "optimizer_message": ("iteratively reweighted penalised least squares"
                                  if parameters.get("converged", True)
                                  else "stopped at the iteration limit"),
            "optimizer_nfev": int(parameters.get("iterations", 0)),
            "optimizer_optimality": 0.0,
            "optimizer_active_mask": [0, 0],
            "fit_warnings": pls_warnings,
        })
        return fitted, parameters

    spread = max(float(np.percentile(yf, 99) - np.percentile(yf, 1)), 1e-6)
    differences = np.diff(yf)
    noise = max(1.4826 * float(np.median(np.abs(differences - np.median(differences)))), spread * 1e-4)
    duration = max(float(x[-1]), 1.0)

    result = None
    lower: np.ndarray | None = None
    upper: np.ndarray | None = None
    if model == "linear":
        if baseline_mode == "fixed":
            denominator = float(np.dot(xf, xf))
            initial_slope = float(np.dot(xf, yf - baseline) / denominator) if denominator else 0.0
            residual = lambda p, xx, yy: baseline + p[0] * xx - yy
            result = least_squares(lambda p: residual(p, xf, yf), [initial_slope], loss="soft_l1",
                                   f_scale=noise, **_tolerances())
            result = least_squares(lambda p: residual(p, x[valid], y[valid]), result.x,
                                   loss="soft_l1", f_scale=noise, **_tolerances())
            fitted = baseline + result.x[0] * x
            parameters: dict[str, Any] = {"fixed_baseline": baseline, "slope_per_s": float(result.x[0])}
            parameter_count = 1
        else:
            slope_guess = float(np.polyfit(xf, yf, 1)[0])
            residual = lambda p, xx, yy: p[0] + p[1] * xx - yy
            result = least_squares(lambda p: residual(p, xf, yf), [baseline, slope_guess],
                                   loss="soft_l1", f_scale=noise, **_tolerances())
            result = least_squares(lambda p: residual(p, x[valid], y[valid]), result.x,
                                   loss="soft_l1", f_scale=noise, **_tolerances())
            fitted = result.x[0] + result.x[1] * x
            parameters = {"baseline_initial": baseline, "fitted_constant": float(result.x[0]),
                          "slope_per_s": float(result.x[1])}
            parameter_count = 2
    else:
        screen_keep = sample_indices(len(valid_indices), MULTISTART_SCREEN_POINTS)
        xs, ys = x[valid_indices[screen_keep]], y[valid_indices[screen_keep]]
        early = yf[np.argsort(xf)[: max(5, len(yf) // 50)]]
        amplitude = float(np.median(early) - baseline)
        tau_min, tau_max = max(0.2, duration / 100000), max(10.0, duration * 20)
        amplitude_bound = max(3 * spread, abs(amplitude) * 5, 1e-5)
        if model == "single_exponential":
            if baseline_mode == "fixed":
                lower = np.array([-amplitude_bound, tau_min])
                upper = np.array([amplitude_bound, tau_max])
            else:
                constant_bound = max(abs(baseline) + 5 * spread, 10 * spread, 1e-4)
                lower = np.array([-amplitude_bound, tau_min, -constant_bound])
                upper = np.array([amplitude_bound, tau_max, constant_bound])

            def curve(parameters: np.ndarray, xx: np.ndarray) -> np.ndarray:
                constant = baseline if baseline_mode == "fixed" else parameters[2]
                return constant + parameters[0] * np.exp(-xx / parameters[1])

            starts = []
            for fraction in (0.005, 0.02, 0.08, 0.3, 1.0, 3.0):
                start = [amplitude, np.clip(duration * fraction, tau_min * 1.01, tau_max * .99)]
                if baseline_mode == "fit_constant":
                    start.append(baseline)
                starts.append(np.asarray(start, float))
            result = _multi_start_curve(starts, curve, model, xf, yf, xs, ys, lower, upper, noise)
            result = least_squares(lambda p: curve(p, x[valid]) - y[valid], result.x,
                                   bounds=(lower, upper), loss="soft_l1", f_scale=noise,
                                   max_nfev=_final_refine_nfev(model), **_tolerances())
            fitted = curve(result.x, x)
            parameters = {"amplitude": float(result.x[0]), "tau_s": float(result.x[1])}
            if baseline_mode == "fixed":
                parameters["fixed_baseline"] = baseline
            else:
                parameters.update({"baseline_initial": baseline, "fitted_constant": float(result.x[2])})
            parameter_count = len(result.x)
        elif model == "double_exponential":
            if baseline_mode == "fixed":
                lower = np.array([-amplitude_bound, -amplitude_bound, tau_min, tau_min])
                upper = np.array([amplitude_bound, amplitude_bound, tau_max, tau_max])
            else:
                constant_bound = max(abs(baseline) + 5 * spread, 10 * spread, 1e-4)
                lower = np.array([-amplitude_bound, -amplitude_bound, tau_min, tau_min, -constant_bound])
                upper = np.array([amplitude_bound, amplitude_bound, tau_max, tau_max, constant_bound])

            def curve(parameters: np.ndarray, xx: np.ndarray) -> np.ndarray:
                constant = baseline if baseline_mode == "fixed" else parameters[4]
                return (constant + parameters[0] * np.exp(-xx / parameters[2])
                        + parameters[1] * np.exp(-xx / parameters[3]))

            tau_pairs = ((0.005, 0.08), (0.005, 0.3), (0.02, 0.3),
                         (0.02, 1.0), (0.08, 1.0), (0.08, 3.0), (0.3, 3.0))
            starts = []
            for fast_fraction, slow_fraction in tau_pairs:
                for split in (0.35, 0.7):
                    start = [
                        amplitude * split, amplitude * (1 - split),
                        np.clip(duration * fast_fraction, tau_min * 1.01, tau_max * .99),
                        np.clip(duration * slow_fraction, tau_min * 1.01, tau_max * .99),
                    ]
                    if baseline_mode == "fit_constant":
                        start.append(baseline)
                    starts.append(np.asarray(start, float))
            result = _multi_start_curve(starts, curve, model, xf, yf, xs, ys, lower, upper, noise)
            result = least_squares(lambda p: curve(p, x[valid]) - y[valid], result.x,
                                   bounds=(lower, upper), loss="soft_l1", f_scale=noise,
                                   max_nfev=_final_refine_nfev(model), **_tolerances())
            fitted = curve(result.x, x)
            parameters = {
                "amplitude_1": float(result.x[0]), "amplitude_2": float(result.x[1]),
                "tau_1_s": float(result.x[2]), "tau_2_s": float(result.x[3]),
            }
            if baseline_mode == "fixed":
                parameters["fixed_baseline"] = baseline
            else:
                parameters.update({"baseline_initial": baseline, "fitted_constant": float(result.x[4])})
            parameter_count = len(result.x)
        else:
            raise ValueError(f"Unknown fitting model: {model}")
    score_indices = valid_indices[sample_indices(len(valid_indices), 2000)]
    residuals = y[score_indices] - fitted[score_indices]
    rss = max(float(np.sum(residuals ** 2)), np.finfo(float).tiny)
    score_n = len(score_indices)
    bic = score_n * np.log(rss / score_n) + parameter_count * np.log(score_n)
    aic = score_n * np.log(rss / score_n) + 2 * parameter_count
    aicc = aic + (2 * parameter_count * (parameter_count + 1) / (score_n - parameter_count - 1)
                  if score_n > parameter_count + 1 else np.inf)
    warnings: list[str] = []
    if not result.success:
        warnings.append("The optimizer did not report convergence")
    if lower is not None and upper is not None:
        scale = np.maximum(np.abs(upper - lower), np.finfo(float).eps)
        near_bound = np.minimum(np.abs(result.x - lower), np.abs(upper - result.x)) / scale < 1e-4
        if np.any(near_bound):
            warnings.append("One or more parameters are close to the search bounds")
    if model == "double_exponential":
        tau_1, tau_2 = float(result.x[2]), float(result.x[3])
        if max(tau_1, tau_2) / max(min(tau_1, tau_2), np.finfo(float).eps) < 1.25:
            warnings.append("The two time constants are too similar; the double-exponential parameters may not be identifiable")
    parameters.update({
        "model": model,
        "baseline_mode": baseline_mode,
        "fit_samples": int(valid.sum()),
        "multistart_samples": int(len(xf)),
        "multistart_screen_samples": int(len(xs)) if xs is not None else 0,
        "multistart_screened": bool(xs is not None and not np.array_equal(xs, xf)),
        "multistart_start_count": int(len(starts)) if "starts" in locals() else 0,
        "multistart_refined_starts": (
            int(len(starts)) if (xs is None or np.array_equal(xs, xf))
            else int(min(len(starts), MULTISTART_SELECT_TOP))
        ) if "starts" in locals() else 0,
        "final_refinement_samples": int(valid.sum()),
        "score_samples": int(score_n),
        "cost": float(result.cost),
        "rss_selected": rss,
        "rmse_selected": float(np.sqrt(rss / score_n)),
        "bic_selected": float(bic),
        "aicc_selected": float(aicc),
        "optimizer_success": bool(result.success),
        "optimizer_status": int(result.status),
        "optimizer_message": str(result.message),
        "optimizer_nfev": int(result.nfev),
        "optimizer_optimality": float(result.optimality),
        "optimizer_active_mask": np.asarray(result.active_mask, int).tolist(),
        "fit_warnings": warnings,
    })
    return fitted, parameters


def select_best_bleaching_model(
    time_s: np.ndarray,
    values: np.ndarray,
    baseline: float,
    fit_mask: np.ndarray,
    baseline_mode: str = "fixed",
    models: tuple[str, ...] = ("linear", "single_exponential", "double_exponential"),
) -> tuple[str, np.ndarray, dict[str, Any], list[dict[str, Any]]]:
    """Choose the lowest-BIC model, using selected-region samples only."""
    results: list[tuple[str, np.ndarray, dict[str, Any]]] = []
    failures: list[dict[str, Any]] = []
    for model in models:
        try:
            fitted, parameters = fit_bleaching(time_s, values, baseline, model, fit_mask, baseline_mode)
            results.append((model, fitted, parameters))
        except Exception as exc:
            failures.append({"model": model, "error": str(exc)})
    if not results:
        raise ValueError(f"All candidate models failed to fit: {failures}")
    results.sort(key=lambda item: item[2]["bic_selected"])
    best_model, best_fitted, best_parameters = results[0]
    comparison = [
        {"model": model, "bic_selected": parameters["bic_selected"],
         "aicc_selected": parameters["aicc_selected"], "rmse_selected": parameters["rmse_selected"],
         "fit_samples": parameters["fit_samples"]}
        for model, _, parameters in results
    ] + failures
    best_parameters["model_comparison"] = comparison
    best_parameters["selection_rule"] = "minimum BIC on user-selected-region samples only"
    return best_model, best_fitted, best_parameters, comparison


def robust_motion_fit(
    signal: np.ndarray,
    control: np.ndarray,
    mask: np.ndarray | None = None,
) -> tuple[float, float, int]:
    """Robust linear fit of the signal channel on the control channel.

    This reproduces RWD's motion fit: a straight line ``signal ~ slope*control +
    intercept`` estimated with a robust loss, so the prediction can be subtracted
    from the signal. Returns ``(slope, intercept, samples_used)``.
    """
    signal = np.asarray(signal, float)
    control = np.asarray(control, float)
    keep = np.isfinite(signal) & np.isfinite(control)
    if mask is not None:
        keep &= np.asarray(mask, bool)
    used = int(keep.sum())
    if used < 50:
        raise ValueError("Fewer than 50 samples are available for the motion fit.")
    x, y = control[keep], signal[keep]
    if not np.isfinite(x).all() or float(np.ptp(x)) <= 0:
        raise ValueError("The control channel does not vary, so the motion fit cannot be estimated.")
    design = np.column_stack((x, np.ones_like(x)))
    start = np.linalg.lstsq(design, y, rcond=None)[0]
    scale = float(np.median(np.abs(np.diff(y)))) or float(np.std(y)) or 1.0
    result = least_squares(lambda parameters: design @ parameters - y, start,
                           loss="soft_l1", f_scale=scale)
    return float(result.x[0]), float(result.x[1]), used


def rwd_motion_label(primary_label: str, slope: float, intercept: float) -> str:
    return (f"RWD motion-corrected {primary_label} "
            f"(slope={slope:.6g}, intercept={intercept:.6g})")


def rwd_motion_corrected(
    primary: np.ndarray,
    reference: np.ndarray,
    raw_level: float | None = None,
) -> tuple[np.ndarray, float, float, float, int]:
    """RWD's motion correction: robust linear fit, subtract, rescale.

    ``motion = primary - (slope * reference + intercept)`` removes the part of the
    signal that follows the control channel, and the result is placed back on the
    level of the raw trace so that dF/F0 divides by the median of the full-time raw
    fluorescence, exactly as the RWD manual defines it. Returns
    ``(trace, level, slope, intercept, samples)``.
    """
    primary = np.asarray(primary, float)
    reference = np.asarray(reference, float)
    slope, intercept, used = robust_motion_fit(primary, reference)
    motion = primary - (slope * reference + intercept)
    level = float(np.nanmedian(primary)) if raw_level is None else float(raw_level)
    if not np.isfinite(level) or np.isclose(level, 0.0):
        raise ValueError("The median of the raw analysis channel is zero, so dF/F0 cannot be scaled.")
    return motion + level, level, slope, intercept, used


def combine_channels(
    primary: np.ndarray,
    reference: np.ndarray,
    mode: str,
    weight: float | None,
    primary_level: float,
    reference_level: float,
    primary_label: str = "470",
    reference_label: str = "410",
    raw_level: float | None = None,
) -> tuple[np.ndarray, str, float, float]:
    """Combine two corrected channels as a ratio or a weighted difference.

    Returns ``(trace, label, reference_level, weight_used)``.

    ``ratio`` returns ``primary / reference``; the level of that trace is
    ``primary_level / reference_level``.

    ``difference`` returns ``primary - weight * (reference - reference_level)``.
    Only the deviation of the reference is removed, so the trace keeps the level
    of the primary channel and dF/F0 still divides by ``primary_level``. The
    default weight is the matched scale ``primary_level / reference_level``, which
    is the first-order equivalent of the ratio without dividing by the reference.
    """
    if mode not in {"ratio", "difference", "rwd"}:
        raise ValueError(f"Unknown channel combination: {mode}")
    primary = np.asarray(primary, float)
    reference = np.asarray(reference, float)
    if mode == "rwd":
        trace, level, slope, intercept, _used = rwd_motion_corrected(primary, reference, raw_level)
        return trace, rwd_motion_label(primary_label, slope, intercept), level, slope
    if mode == "ratio":
        if np.any(np.isfinite(reference) & np.isclose(reference, 0)):
            raise ValueError(
                f"The corrected {reference_label} trace contains zero, so the ratio cannot be calculated."
            )
        return (primary / reference, f"Corrected {primary_label} / {reference_label} ratio",
                float(primary_level) / float(reference_level), 1.0)
    if weight is None:
        weight = float(primary_level) / float(reference_level)
    weight = float(weight)
    trace = primary - weight * (reference - float(reference_level))
    label = (f"Corrected {primary_label} - {weight:.6g} x ({reference_label} - "
             f"{float(reference_level):.6g})")
    return trace, label, float(primary_level), weight


def estimate_reference_weight(
    primary: np.ndarray,
    reference: np.ndarray,
    mask: np.ndarray,
    primary_level: float,
    reference_level: float,
) -> float:
    """Weight for difference mode, fitted on samples without expected events.

    Both traces are expressed as relative deviations from their own level and a
    single slope through the origin is fitted with a soft-L1 loss. That slope is
    then converted to the count scale used by ``reference_weight``, so the result
    can be entered in the Reference Weight field unchanged.

    Select ``mask`` over a stretch that contains artifacts but no expected
    transients; otherwise the real signal is mistaken for an artifact and the
    weight is biased low.
    """
    a = np.asarray(primary, float) / float(primary_level) - 1.0
    b = np.asarray(reference, float) / float(reference_level) - 1.0
    keep = np.asarray(mask, bool) & np.isfinite(a) & np.isfinite(b)
    if keep.sum() < 20:
        raise ValueError("Fewer than 20 valid samples are available to estimate the reference weight.")
    a, b = a[keep], b[keep]
    if np.dot(b, b) <= np.finfo(float).tiny:
        raise ValueError("The reference channel does not vary; the weight cannot be estimated.")
    result = least_squares(lambda p: p[0] * b - a, [float(np.dot(a, b) / np.dot(b, b))],
                           loss="soft_l1", f_scale=float(np.median(np.abs(a - np.median(a))) or 1.0))
    relative_weight = float(result.x[0])
    matched = float(primary_level) / float(reference_level)
    return relative_weight * matched


def _robust_line(x: np.ndarray, y: np.ndarray, keep: np.ndarray) -> tuple[float, float, int]:
    """Soft-L1 straight-line fit of ``y`` on ``x`` over the samples selected by ``keep``."""
    xx, yy = np.asarray(x, float)[keep], np.asarray(y, float)[keep]
    used = int(keep.sum())
    if used < 50:
        raise ValueError("Fewer than 50 samples are available for the reference fit.")
    if float(np.ptp(xx)) <= 0:
        raise ValueError("The reference channel does not vary, so its weight cannot be fitted.")
    design = np.column_stack((xx, np.ones_like(xx)))
    start = np.linalg.lstsq(design, yy, rcond=None)[0]
    scale = float(np.median(np.abs(np.diff(yy)))) or float(np.std(yy)) or 1.0
    result = least_squares(lambda parameters: design @ parameters - yy, start,
                           loss="soft_l1", f_scale=scale)
    return float(result.x[0]), float(result.x[1]), used


def reference_check(
    primary: np.ndarray,
    reference: np.ndarray,
    time_min: np.ndarray,
    marker_times_min: tuple[float, ...] = (),
    window_seconds: float = 30.0,
    baseline_seconds: float = 10.0,
    primary_label: str = "470",
    reference_label: str = "410",
) -> dict[str, Any]:
    """Describe how the reference channel behaves during the events of one recording.

    A reference only helps the event amplitude when it moves the *same* way as the analysis
    channel there and by an amount that follows it. When it moves against the signal, or by
    an amount that does not track it, no linear combination can separate the two: a positive
    weight mixes that excursion in, a negative one subtracts the response. The numbers here
    say which case a recording is in, before a lot of parameters are tried.

    Everything is returned as plain floats and ints so it can be written to the saved JSON.
    """
    a = np.asarray(primary, float)
    b = np.asarray(reference, float)
    t = np.asarray(time_min, float)
    finite = np.isfinite(a) & np.isfinite(b) & np.isfinite(t)
    if finite.sum() < 50:
        raise ValueError("Fewer than 50 samples are available to check the reference.")
    level_a = float(np.nanmedian(a[finite]))
    level_b = float(np.nanmedian(b[finite]))
    if np.isclose(level_a, 0.0) or np.isclose(level_b, 0.0):
        raise ValueError("One of the channels has a level of zero, so relative deflections "
                         "cannot be formed.")
    markers = sorted({float(value) for value in marker_times_min})
    # A marker written twice (state 0 and state 1 at the same instant) and two markers closer
    # together than the response window describe one event; counting both would measure the
    # same trace twice and drag the ratios towards the noise of a single window.
    spacing = float(window_seconds) / 60.0
    collapsed: list[float] = []
    for marker in markers:
        if collapsed and marker - collapsed[-1] < spacing:
            continue
        collapsed.append(marker)
    markers = collapsed

    quiet = finite.copy()
    pre_window = 5.0 / 60.0
    for marker in markers:
        quiet &= ~((t >= marker - pre_window) & (t <= marker + window_seconds / 60.0))

    def pearson(keep: np.ndarray) -> float | None:
        if keep.sum() < 20:
            return None
        xa = a[keep] - float(np.mean(a[keep]))
        xb = b[keep] - float(np.mean(b[keep]))
        denominator = float(np.sqrt(np.sum(xa * xa) * np.sum(xb * xb)))
        if denominator <= 0:
            return None
        return float(np.sum(xa * xb) / denominator)

    slope_all, intercept_all, samples_all = _robust_line(b, a, finite)
    try:
        slope_quiet, intercept_quiet, samples_quiet = _robust_line(b, a, quiet)
    except ValueError:
        slope_quiet, intercept_quiet, samples_quiet = slope_all, intercept_all, samples_all
    level_matched = level_a / level_b if not np.isclose(level_b, 0.0) else float("nan")

    # Every event is resampled onto one relative-time grid so the preview below is the peak of
    # the trial mean, which is what the event analysis reports, not the mean of the per-event
    # peaks (those are always larger).
    grid = np.linspace(0.0, float(window_seconds), 201)
    primary_curves: list[np.ndarray] = []
    reference_curves: list[np.ndarray] = []
    events: list[dict[str, Any]] = []
    for index, marker in enumerate(markers, start=1):
        base = (t >= marker - baseline_seconds / 60.0) & (t < marker) & finite
        response = (t >= marker) & (t <= marker + window_seconds / 60.0) & finite
        if base.sum() < 5 or response.sum() < 5:
            continue
        a_base = float(np.mean(a[base]))
        b_base = float(np.mean(b[base]))
        if np.isclose(a_base, 0.0) or np.isclose(b_base, 0.0):
            continue
        relative_time = (t[response] - marker) * 60.0
        curve_a = 100.0 * (np.interp(grid, relative_time, a[response]) - a_base) / a_base
        curve_b = 100.0 * (np.interp(grid, relative_time, b[response]) - b_base) / b_base
        primary_curves.append(curve_a)
        reference_curves.append(curve_b)
        d_a = float(curve_a[int(np.argmax(np.abs(curve_a)))])
        d_b = float(curve_b[int(np.argmax(np.abs(curve_b)))])
        events.append({
            "event": int(index),
            "time_min": round(float(marker), 6),
            "primary_percent": round(d_a, 4),
            "reference_percent": round(d_b, 4),
            "in_phase": bool(d_a * d_b > 0),
            "ratio": round(d_b / d_a, 4) if abs(d_a) > 1e-6 else None,
        })

    in_phase_count = sum(1 for event in events if event["in_phase"])
    anti_phase_count = len(events) - in_phase_count
    ratios = [event["ratio"] for event in events if event["ratio"] is not None]
    ratio_mean = float(np.mean(ratios)) if ratios else None
    ratio_sd = float(np.std(ratios)) if len(ratios) > 1 else None
    ratio_cv = (abs(ratio_sd / ratio_mean) if ratio_mean and ratio_sd is not None else None)

    # A reference that mirrors the signal scales with it, so |signal| and |reference| rise and
    # fall together across events. One that carries its own event-locked artifact has a roughly
    # constant excursion while the response varies. The two are told apart here because the
    # advice is opposite: a mirror is a calibration problem, an independent artifact means the
    # reference cannot stand in for the response at all.
    reference_cv = None
    signal_cv = None
    amplitude_correlation = None
    if len(events) >= 3:
        size_a = np.abs([event["primary_percent"] for event in events])
        size_b = np.abs([event["reference_percent"] for event in events])
        if float(np.mean(size_a)) > 0:
            signal_cv = float(np.std(size_a) / np.mean(size_a))
        if float(np.mean(size_b)) > 0:
            reference_cv = float(np.std(size_b) / np.mean(size_b))
        if float(np.std(size_a)) > 0 and float(np.std(size_b)) > 0:
            amplitude_correlation = float(np.corrcoef(size_a, size_b)[0, 1])

    mean_primary = np.mean(np.vstack(primary_curves), axis=0) if primary_curves else None
    mean_reference = np.mean(np.vstack(reference_curves), axis=0) if reference_curves else None

    def predict(weight: float) -> dict[str, float] | None:
        """Peak of the trial mean the difference combination would give at this weight."""
        if mean_primary is None or mean_reference is None:
            return None
        factor = weight * level_b / level_a
        combined = mean_primary - factor * mean_reference
        peak = float(combined[int(np.argmax(np.abs(combined)))])
        return {
            "weight": float(weight),
            "mean_percent": peak,
            "min_percent": float(np.min(combined)),
            "max_percent": float(np.max(combined)),
        }

    candidates = [
        predict(0.0),
        predict(slope_quiet),
        predict(slope_all),
        predict(level_matched),
    ]
    candidates = [value for value in candidates if value is not None]

    if not events:
        verdict = "no_events"
        summary = ("No complete event window is available, so the reference cannot be judged "
                   "against the responses. Load a marker file or widen the window.")
    elif anti_phase_count >= 0.6 * len(events):
        mirrors = (amplitude_correlation is not None and amplitude_correlation >= 0.60
                   and ratio_cv is not None and ratio_cv < 0.50)
        if mirrors:
            verdict = "anti_phase_proportional"
            summary = (
                f"{reference_label} moves against {primary_label} and its size follows the "
                f"response (ratio {ratio_mean:+.2f} ± {ratio_sd:.2f}, amplitude correlation "
                f"{amplitude_correlation:+.2f}). A reference that mirrors the signal like that "
                "usually means bleed-through that was over-subtracted in the acquisition "
                "software: the 'isosbestic' channel already contains the response with the "
                "opposite sign, so it is not an independent measurement. Subtracting it adds the "
                "response back (positive weight) or cancels it (negative one), and no weight can "
                "separate the two. Report the analysis channel on its own and check the "
                f"{reference_label} bleed-through calibration."
            )
        else:
            verdict = "anti_phase_independent"
            size = ""
            if reference_cv is not None and signal_cv is not None:
                size = (f" The {reference_label} excursion varies by {reference_cv * 100:.0f}% of "
                        f"its own mean while the {primary_label} response varies by "
                        f"{signal_cv * 100:.0f}%")
                if amplitude_correlation is not None:
                    size += f", and the two do not grow together (r = {amplitude_correlation:+.2f})"
                size += "."
            summary = (
                f"{reference_label} moves against {primary_label} during {anti_phase_count} of "
                f"{len(events)} events, but by an amount that does not follow the response."
                f"{size} That is the signature of an event-locked artifact on the reference "
                "channel itself - movement, blood volume, a cable - and not of a scaled copy of "
                "the signal. No linear combination can separate the two: a positive weight mixes "
                "the artifact into the response and a negative one removes the response. For "
                "these events report the analysis channel alone (Reference = None) and keep the "
                "reference for drift control."
            )
    elif in_phase_count >= 0.6 * len(events):
        verdict = "in_phase"
        share = (slope_quiet * level_b / level_a * ratio_mean) if ratio_mean is not None else None
        if share is None:
            summary = (
                f"{primary_label} and {reference_label} move the same way during {in_phase_count} "
                f"of {len(events)} events, which is what a shared artifact looks like. The preview "
                "below shows what each weight leaves of the response."
            )
        else:
            summary = (
                f"{primary_label} and {reference_label} move the same way during {in_phase_count} "
                f"of {len(events)} events, which is what a shared artifact looks like. The fitted "
                f"slope turns the reference excursion into {share:.0%} of the response, so "
                "correcting takes that share away: right if it is movement, wrong if the reference "
                "carries a real response. The preview below shows what each weight leaves."
            )
    else:
        verdict = "mixed"
        summary = (
            f"The polarity is inconsistent: {in_phase_count} event(s) in phase, "
            f"{anti_phase_count} against. The mean is dominated by artifacts of both signs; "
            "check the individual events before trusting the average."
        )

    return {
        "primary_label": primary_label,
        "reference_label": reference_label,
        "levels": {"primary": level_a, "reference": level_b},
        "level_matched_weight": float(level_matched),
        "slope_all_samples": slope_all,
        "slope_quiet_samples": slope_quiet,
        "intercept_all_samples": intercept_all,
        "intercept_quiet_samples": intercept_quiet,
        "samples_all": samples_all,
        "samples_quiet": samples_quiet,
        "pearson_all": pearson(finite),
        "pearson_quiet": pearson(quiet),
        "events_checked": len(events),
        "events_in_phase": in_phase_count,
        "events_anti_phase": anti_phase_count,
        "ratio_mean": ratio_mean,
        "ratio_sd": ratio_sd,
        "ratio_cv": ratio_cv,
        "signal_size_cv": signal_cv,
        "reference_size_cv": reference_cv,
        "amplitude_correlation": amplitude_correlation,
        "events": events,
        "amplitude_preview": candidates,
        "verdict": verdict,
        "summary": summary,
        "window_seconds": float(window_seconds),
        "baseline_seconds": float(baseline_seconds),
    }


def reference_response_leak(
    primary: np.ndarray,
    reference: np.ndarray,
    time_min: np.ndarray,
    weight: float,
    primary_level: float,
    reference_level: float,
    marker_times_min: tuple[float, ...] = (),
    primary_label: str = "470",
    reference_label: str = "410",
    window_seconds: float = 30.0,
) -> dict[str, Any] | None:
    """Share of the response that a reference subtraction moves, as a fraction of it.

    Subtracting ``weight × (reference − its level)`` removes whatever of the response the
    reference channel itself carries. If that is a fraction ``k`` of the response in
    relative terms and the two channels sit at ``primary_level`` and ``reference_level``,
    the removed share is ``weight · k · reference_level / primary_level``; the response
    that survives is one minus that. ``k`` is what ``reference_check`` already measures,
    so this is a read-out of the accident, not a correction of it.

    Nothing here changes the trace: an exclusion window does not help, because the same
    weight is applied inside the event window whatever samples estimated it. Only the
    size of ``k`` decides whether the correction is harmless (a clean isosbestic channel)
    or eats the response.
    """
    if not marker_times_min or not np.isfinite(weight):
        return None
    try:
        check = reference_check(
            primary, reference, time_min, tuple(marker_times_min),
            window_seconds=window_seconds,
            primary_label=primary_label, reference_label=reference_label,
        )
    except ValueError:
        return None
    ratio = check.get("ratio_mean")
    if ratio is None or not np.isfinite(ratio) or not np.isfinite(primary_level) \
            or not np.isfinite(reference_level) or np.isclose(primary_level, 0.0):
        return None
    removed = float(weight) * float(ratio) * float(reference_level) / float(primary_level)
    return {
        "reference_ratio_mean": float(ratio),
        "response_fraction_removed": float(removed),
        "response_fraction_kept": float(1.0 - removed),
        "leak_check_events": int(check.get("events_checked") or 0),
        "leak_check_window_seconds": float(window_seconds),
        "leak_check_note": (
            "fraction of the event response that the reference subtraction removes, "
            "from the reference/signal ratio measured over complete event windows"
        ),
    }


def effective_baseline(
    baseline: float | None,
    offset: float | None,
    label: str = "",
) -> float:
    """F0 of a channel: the entered fitting baseline minus the instrument offset.

    The fitting baseline is the **lowest point of the trace after bleaching**, read off the
    raw plot — so it still contains the instrument offset, which has to be removed. The result
    is the level the corrected trace sits on and the denominator of dF/F0.
    A baseline that does not exceed the offset cannot describe fluorescence and is rejected
    instead of being reinterpreted.
    """
    tag = f" for {label}" if label else ""
    if baseline is None:
        raise ValueError(f"Enter the fitting baseline{tag}: it is the lowest point of the "
                         "trace after bleaching, read off the raw plot, offset included.")
    try:
        baseline = float(baseline)
        offset = 0.0 if offset is None else float(offset)
    except (TypeError, ValueError) as exc:
        raise ValueError(f"The fitting baseline{tag} and its offset must be numbers.") from exc
    if not np.isfinite(baseline) or not np.isfinite(offset):
        raise ValueError(f"The fitting baseline{tag} and its offset must be finite numbers.")
    f0 = baseline - offset
    if f0 <= 0:
        raise ValueError(
            f"The fitting baseline{tag} ({baseline:g}) must be larger than the offset "
            f"({offset:g}): it is the lowest point of the raw trace, which still contains the "
            f"offset, so F0 = baseline - offset has to be positive (got {f0:g})."
        )
    return float(f0)


def select_effective_data(data: pd.DataFrame, config: ProcessingConfig) -> pd.DataFrame:
    time_min = data["TimeStamp"].to_numpy(float) / 60000
    use = (time_min >= config.range_start_min) & (time_min <= config.range_end_min)
    if use.sum() < 20:
        raise ValueError("The valid data range contains fewer than 20 points.")
    return data.loc[use].reset_index(drop=True)


def fit_cache_key(
    data_token: Any,
    source: str,
    column: str,
    model: str,
    baseline_mode: str,
    offset: float,
    baseline: float,
    regions: Any,
    range_start_min: float,
    range_end_min: float,
    beta: float | None = None,
    lam: float | None = None,
    exclusions: tuple[tuple[float, float], ...] | None = None,
    smoothing: float | None = None,
) -> tuple[Any, ...]:
    """Signature of one channel fit. Two runs share a fit only when it matches."""
    return (data_token, source, column, model, baseline_mode, offset, baseline,
            tuple(tuple(region) for region in regions),
            float(range_start_min), float(range_end_min), beta, lam,
            tuple(exclusions) if exclusions else None, smoothing)


def process_data(
    data: pd.DataFrame,
    config: ProcessingConfig,
    cache: dict[Any, Any] | None = None,
    data_token: Any = None,
    cancel: Any = None,
    marker_times_min: tuple[float, ...] = (),
) -> tuple[pd.DataFrame, dict[str, Any]]:
    """Fit every wavelength and build the processed table.

    ``cache`` optionally reuses previous fits. A cached entry is only reused when
    the full fitting signature matches, so results are identical to a fresh fit.
    ``cancel`` is an optional ``threading.Event``; it is checked between channels.
    ``marker_times_min`` feeds the reference-leak read-out when the channels are combined.
    """
    gap_fills = dict(data.attrs.get("gap_fills") or {})
    subset = select_effective_data(data, config)
    time_s = subset["TimeStamp"].to_numpy(float) / 1000
    time_min = time_s / 60
    dt = np.diff(time_s)
    sample_rate = float(1 / np.median(dt[dt > 0]))
    source = config.source_channel
    available = available_recording_channels(subset).get(source, [])
    wavelengths = [str(value) for value in config.wavelengths if str(value) in available]
    if not wavelengths:
        raise ValueError(f"{source} has no selected 410, 470, or 560 fluorescence channel.")
    if config.analysis_wavelength not in wavelengths:
        raise ValueError(f"Analysis wavelength {config.analysis_wavelength} is unavailable in {source}.")
    if config.reference_wavelength is not None and config.reference_wavelength not in wavelengths:
        raise ValueError(f"Reference wavelength {config.reference_wavelength} is unavailable in {source}.")
    if config.reference_wavelength == config.analysis_wavelength:
        raise ValueError("The analysis and reference wavelengths must be different.")

    output_columns: dict[str, np.ndarray] = {}
    fit_parameters: dict[str, dict[str, Any]] = {}
    corrected: dict[str, np.ndarray] = {}
    adjusted_traces: dict[str, np.ndarray] = {}
    masks: dict[str, np.ndarray] = {}
    for wavelength in wavelengths:
        if cancel is not None and cancel.is_set():
            raise ProcessingCancelled()
        column = f"{source}-{wavelength}"
        raw = subset[column].to_numpy(float)
        if np.isfinite(raw).sum() < 20:
            raise ValueError(f"{column} contains fewer than 20 valid samples in the selected range.")
        offset = float(config.offsets[wavelength])
        baseline = float(config.baselines[wavelength])
        # The fit works on (raw - offset), so the entered plateau has to lose the offset too.
        fit_baseline = effective_baseline(baseline, offset, wavelength)
        adjusted = raw - offset
        regions = [tuple(region) for region in config.fit_regions.get(wavelength, [])]
        mask = region_mask(time_min, regions)
        excluded = 0
        if config.exclusion_windows:
            window_mask = np.zeros(time_min.shape, bool)
            for start, end in config.exclusion_windows:
                window_mask |= (time_min >= float(start)) & (time_min <= float(end))
            fit_samples = int((mask & np.isfinite(adjusted)).sum())
            excluded = int((mask & window_mask).sum())
            if excluded:
                remaining = fit_samples - excluded
                if remaining < max(200, int(0.25 * fit_samples)):
                    # Silently dropping the exclusion used to be the behaviour here, which
                    # meant the fit kept the marker windows the user had just asked to keep
                    # out. Refuse instead: the user can always widen the regions.
                    raise ValueError(
                        f"The marker exclusion windows remove {excluded:,} of the "
                        f"{fit_samples:,} fitting samples in {wavelength}, leaving "
                        f"{remaining:,}. Widen the fitting regions in the {wavelength} fitting "
                        "window or shorten the exclusion window; the fit will not silently "
                        "ignore an exclusion that removes too much."
                    )
                mask = mask & ~window_mask
        model = config.fit_models.get(wavelength) or config.fit_model
        baseline_mode = config.fit_baseline_modes.get(wavelength) or "fit_constant"
        beta = config.fit_betas.get(wavelength)
        lam = config.fit_lambdas.get(wavelength)
        smoothing = config.fit_smoothing.get(wavelength)
        cache_key = fit_cache_key(data_token, source, column, model, baseline_mode, offset, fit_baseline,
                                  regions, config.range_start_min, config.range_end_min,
                                  None if beta is None else float(beta),
                                  None if lam is None else float(lam),
                                  tuple(config.exclusion_windows) or None,
                                  None if smoothing is None else float(smoothing))
        cached = cache.get(cache_key) if cache is not None else None
        if cached is not None:
            fitted, parameters = cached[0], copy.deepcopy(cached[1])
            parameters["fit_reused_from_cache"] = True
        else:
            fitted, parameters = fit_bleaching(
                time_s, adjusted, fit_baseline, model, mask, baseline_mode,
                beta=PLS_DEFAULT_BETA if beta is None else float(beta),
                lam=PLS_DEFAULT_LAMBDA if lam is None else float(lam),
                smoothing_seconds=None if smoothing is None else float(smoothing),
            )
            parameters["excluded_samples"] = int(excluded)
            parameters["fit_reused_from_cache"] = False
            if cache is not None:
                if len(cache) >= 12:
                    cache.clear()
                cache[cache_key] = (np.array(fitted, float, copy=True), copy.deepcopy(parameters))
        fitted = np.asarray(fitted, float)
        if fitted.shape != adjusted.shape:
            raise ValueError(
                f"The {wavelength} fit returned {fitted.size:,} points for {adjusted.size:,} samples. "
                "This is a bug rather than a data problem; report it with the recording."
            )
        parameters["fit_regions"] = [list(region) for region in regions]
        parameters["entered_baseline"] = float(baseline)
        parameters["effective_baseline"] = float(fit_baseline)
        constant = parameters.get("fitted_constant")
        if constant is None:
            constant = parameters.get("fixed_baseline", 0.0)
        constant = float(constant)
        parameters["correction_constant"] = constant
        if not np.isfinite(fitted).any():
            raise ValueError(f"The fitted {wavelength} curve has no finite value.")
        scale = float(fit_baseline)
        if not np.isfinite(scale) or scale <= 0:
            raise ValueError(
                f"F0 for {wavelength} is not positive (baseline {baseline:g} - offset {offset:g}); "
                "the correction cannot be calculated."
            )
        if np.any(np.isfinite(fitted) & (fitted <= 0)):
            raise ValueError(
                f"The fitted {wavelength} curve is not positive. Adjust its offset or fitting regions."
            )
        form = config.bleach_correction
        if form == "multiplicative":
            # The light path is modelled as raw = offset + B(t)·(1 + s(t)): the response is a
            # fraction of the fluorescence that is still excitable, so it shrinks as the probe
            # bleaches. Dividing by B(t) puts the trace on F0 and leaves F0·s(t), which keeps
            # the relative size of a response the same at every time point.
            #     corrected = F0 · (raw - offset) / F(t)
            channel_corrected = scale * (adjusted / fitted)
        elif form == "additive":
            # raw = offset + B(t) + s(t): the response is added on top of the bleach, so
            # subtracting F(t) leaves F0 + s(t) and the response keeps its size regardless of
            # how much the trace has faded.
            #     corrected = F0 + [(raw - offset) - F(t)]
            channel_corrected = scale + (adjusted - fitted)
        else:
            raise ValueError(f"Unknown bleaching correction form: {form}")
        parameters["correction_form"] = form
        parameters["correction_scale"] = scale
        missing = int((~np.isfinite(raw)).sum())
        if np.isfinite(raw).sum() != raw.shape[0]:
            first_bad = float(time_min[~np.isfinite(raw)][0])
            parameters.setdefault("fit_warnings", []).append(
                f"{missing} sample(s) of {wavelength} are empty or non-numeric (first at "
                f"{first_bad:.3f} min); they are left out of the fit and the corrected trace "
                "is a gap there"
            )
        if model == "none":
            # Nothing is pinned and nothing is subtracted, so the trace keeps its own level.
            parameters["corrected_note"] = ("No bleaching correction: the corrected trace is "
                                            "(raw - offset), so any decay remains in it")
        if model != "none" and baseline_mode == "fixed" and float(fit_baseline) != 0.0:
            tail = adjusted[np.isfinite(adjusted)][-max(20, int(0.05 * np.isfinite(adjusted).sum())):]
            plateau = float(np.median(tail)) if tail.size else float("nan")
            if np.isfinite(plateau) and plateau != 0.0 and not 0.95 <= float(fit_baseline) / plateau <= 1.05:
                parameters.setdefault("fit_warnings", []).append(
                    f"The fitting baseline implies a minimum of {float(fit_baseline):.6g} "
                    f"(baseline {baseline:g} - offset {offset:g}), but the trace ends at "
                    f"{plateau:.6g}. The fixed mode pins the asymptote of the fitted curve to the "
                    "former; check that the value is the lowest point of this recording."
                )
        output_columns[f"raw_{wavelength}"] = raw
        output_columns[f"offset_adjusted_{wavelength}"] = adjusted
        output_columns[f"fit_{wavelength}"] = fitted
        output_columns[f"corrected_{wavelength}"] = channel_corrected
        output_columns[f"fit_used_{wavelength}"] = mask.astype(int)
        corrected[wavelength] = channel_corrected
        adjusted_traces[wavelength] = adjusted
        masks[wavelength] = mask
        fit_parameters[wavelength] = parameters

    primary = config.analysis_wavelength
    reference = config.reference_wavelength
    combined = np.full(len(subset), np.nan)
    combined_label: str | None = None
    motion_fit: dict[str, Any] | None = None
    combine_warnings: list[str] = []
    if reference is not None and config.combine == "rwd":
        # RWD motion correction: robust linear fit of the signal on the control, the
        # fitted prediction is subtracted, and the result is placed back on the level of
        # the raw trace so dF/F0 divides by the median of the full-time raw fluorescence.
        level_analysis = float(fit_parameters[primary]["effective_baseline"])
        analysis_trace, level, slope, intercept, used = rwd_motion_corrected(
            corrected[primary], corrected[reference], level_analysis)
        combined = analysis_trace.copy()
        combined_label = rwd_motion_label(primary, slope, intercept)
        analysis_reference = level
        weight_used = slope
        motion_fit = {
            "slope": slope,
            "intercept": intercept,
            "samples": used,
            "median_signal": level,
            "denominator": "F0 of the analysis channel (fitting baseline minus offset)",
        }
        leak = reference_response_leak(
            corrected[primary], corrected[reference], time_min, float(slope),
            float(fit_parameters[primary]["correction_scale"]),
            float(fit_parameters[reference]["correction_scale"]),
            marker_times_min, primary, reference)
        if leak is not None:
            motion_fit.update(leak)
    elif reference is not None:
        combined, combined_label, analysis_reference, weight_used = combine_channels(
            corrected[primary], corrected[reference], config.combine, config.reference_weight,
            fit_parameters[primary]["correction_scale"], fit_parameters[reference]["correction_scale"],
            primary, reference,
        )
        analysis_trace = combined.copy()
        leak = reference_response_leak(
            corrected[primary], corrected[reference], time_min, float(weight_used),
            float(fit_parameters[primary]["correction_scale"]),
            float(fit_parameters[reference]["correction_scale"]),
            marker_times_min, primary, reference)
        if leak is not None:
            motion_fit = dict(leak)
    else:
        analysis_trace = corrected[primary].copy()
        analysis_reference = fit_parameters[primary]["correction_scale"]
        weight_used = None

    if motion_fit is not None and motion_fit.get("response_fraction_removed") is not None:
        removed = float(motion_fit["response_fraction_removed"])
        if abs(removed) > 0.2:
            combine_warnings.append(
                f"The {reference} subtraction removes about {removed:.0%} of the event "
                f"response (reference/signal ratio {motion_fit['reference_ratio_mean']:+.2f} "
                f"× weight {weight_used:.4g}). That is what a reference carrying its own "
                "response looks like; check the bleed-through calibration, or set Reference "
                "to None to keep the analysis channel alone."
            )

    smooth_points = max(1, int(round(config.smooth_seconds * sample_rate)))
    combined_smoothed = (pd.Series(combined).rolling(smooth_points, center=True, min_periods=1).mean().to_numpy()
                         if reference is not None else combined.copy())
    analysis_smoothed = pd.Series(analysis_trace).rolling(smooth_points, center=True, min_periods=1).mean().to_numpy()
    empty = np.full(len(subset), np.nan)
    output = pd.DataFrame({
        "time_s": time_s, "time_min": time_min,
        "original_time_s": time_s, "original_time_min": time_min,
        "relative_time_s": time_s, "relative_time_min": time_min,
        **output_columns,
        "combined_signal": combined, "combined_signal_smoothed": combined_smoothed,
        "analysis_trace": analysis_trace, "analysis_trace_smoothed": analysis_smoothed,
        "dff_percent": empty.copy(), "dff_percent_smoothed": empty.copy(),
        "zscore": empty.copy(), "zscore_smoothed": empty.copy(),
        "normalization_baseline": np.zeros(len(subset), dtype=int),
    })
    details = {
        "sample_rate_hz": sample_rate,
        "rows": int(len(output)),
        "combined_label": combined_label,
        "wavelengths": wavelengths,
        "analysis_wavelength": primary,
        "reference_wavelength": reference,
        "fit_parameters": fit_parameters,
        **{f"fit_{wavelength}_parameters": fit_parameters[wavelength] for wavelength in wavelengths},
        "missing_samples": {wavelength: int((~np.isfinite(subset[f"{source}-{wavelength}"].to_numpy(float))).sum())
                            for wavelength in wavelengths},
        "gap_fills": gap_fills,
        "analysis_reference_from_user_baselines": float(analysis_reference),
        "combine": config.combine if reference is not None else None,
        "bleach_correction": config.bleach_correction,
        "reference_weight": None if weight_used is None else float(weight_used),
        "rwd_motion_fit": motion_fit,
        "combine_warnings": combine_warnings,
        "source_fingerprint": data.attrs.get("source_fingerprint"),
        "source_path": data.attrs.get("source_path"),
        "normalization": None,
        "source_channel": source,
        "definitions": {
            "baseline": ("raw fluorescence level at the start of the recording; F0 = baseline - offset, "
                         "used as the correction scale and as the dF/F0 denominator"),
            "corrected_channel": (
                "F0 + [(raw - offset) - fitted]; F0 = baseline - offset, never the fitted level at t0"
                if config.bleach_correction == "additive" else
                "F0 x (raw - offset) / fitted; the bleach is divided out, so a response keeps the "
                "same relative size however far the probe has faded"),
            "combined_channel": {
                "ratio": "corrected analysis / corrected reference",
                "difference": "corrected analysis - weight x (corrected reference - its level); only the reference deviation is removed",
                "rwd": ("RWD motion correction: corrected analysis - (slope x corrected reference + intercept), "
                        "then placed on F0 of the analysis channel"),
            }[config.combine],
            "reference_weight": ("fitted robustly from the recording and not user-editable"
                                 if config.combine == "rwd"
                                 else "None means the matched scale (analysis level / reference level)"),
            "fit_regions": "only samples inside the user-selected intervals are used for parameter estimation",
        },
    }
    return output, details


def calculate_normalized_traces(
    processed: pd.DataFrame,
    baseline_start_min: float,
    baseline_end_min: float,
    smooth_seconds: float,
    sample_rate_hz: float,
    zero_time_min: float | None = None,
) -> tuple[pd.DataFrame, dict[str, Any]]:
    """Calculate dF/F0 and z-score from a user-selected baseline interval.

    F0 is the mean of the corrected trace inside the baseline interval, for the analysis
    trace and for every wavelength separately; dF/F0 divides by it and the Z-score divides
    the dF/F0 by its own standard deviation in the same interval.
    """
    if baseline_end_min <= baseline_start_min:
        raise ValueError("The normalization baseline end time must be greater than the start time.")
    result = processed.copy()
    time_min = result["time_min"].to_numpy(float)
    trace = result["analysis_trace"].to_numpy(float)
    baseline_mask = ((time_min >= baseline_start_min) & (time_min <= baseline_end_min)
                     & np.isfinite(trace))
    if baseline_mask.sum() < 20:
        raise ValueError("The normalization baseline interval contains fewer than 20 valid data points.")
    f0 = float(np.mean(trace[baseline_mask]))
    if not np.isfinite(f0) or np.isclose(f0, 0):
        raise ValueError("F0 in the normalization baseline interval is zero or invalid, so "
                         "dF/F0 cannot be calculated.")
    dff = 100.0 * (trace - f0) / f0
    baseline_dff = dff[baseline_mask & np.isfinite(dff)]
    z_center = float(np.mean(baseline_dff))
    z_scale = float(np.std(baseline_dff, ddof=1))
    if not np.isfinite(z_scale) or z_scale <= np.finfo(float).eps:
        raise ValueError("The standard deviation in the normalization baseline interval is zero, so the Z-score cannot be calculated.")
    zscore = (dff - z_center) / z_scale
    smooth_points = max(1, int(round(float(smooth_seconds) * float(sample_rate_hz))))
    result["dff_percent"] = dff
    result["dff_percent_smoothed"] = pd.Series(dff).rolling(
        smooth_points, center=True, min_periods=1
    ).mean().to_numpy()
    result["zscore"] = zscore
    result["zscore_smoothed"] = pd.Series(zscore).rolling(
        smooth_points, center=True, min_periods=1
    ).mean().to_numpy()
    # Retain every independently corrected wavelength as a first-class output.
    # The legacy dff_percent/zscore names continue to mean the selected analysis trace.
    corrected_columns = sorted(
        (str(column).removeprefix("corrected_"), str(column))
        for column in result.columns if re.fullmatch(r"corrected_(410|470|560)", str(column))
    )
    channel_normalization: dict[str, dict[str, float]] = {}
    for label, column in corrected_columns:
        values = result[column].to_numpy(float)
        valid_baseline = baseline_mask & np.isfinite(values)
        if valid_baseline.sum() < 20:
            result[f"dff_{label}_percent"] = np.full(len(result), np.nan)
            result[f"zscore_{label}"] = np.full(len(result), np.nan)
            result[f"dff_{label}_percent_smoothed"] = np.full(len(result), np.nan)
            result[f"zscore_{label}_smoothed"] = np.full(len(result), np.nan)
            continue
        channel_f0 = float(np.mean(values[valid_baseline]))
        if not np.isfinite(channel_f0) or np.isclose(channel_f0, 0):
            raise ValueError(f"F0 for corrected {label} is zero or invalid.")
        channel_dff = 100.0 * (values - channel_f0) / channel_f0
        baseline_channel_dff = channel_dff[valid_baseline]
        channel_sd = float(np.std(baseline_channel_dff, ddof=1))
        if not np.isfinite(channel_sd) or channel_sd <= np.finfo(float).eps:
            raise ValueError(f"The baseline standard deviation for corrected {label} is zero.")
        channel_z = (channel_dff - float(np.mean(baseline_channel_dff))) / channel_sd
        result[f"dff_{label}_percent"] = channel_dff
        result[f"zscore_{label}"] = channel_z
        result[f"dff_{label}_percent_smoothed"] = pd.Series(channel_dff).rolling(
            smooth_points, center=True, min_periods=1
        ).mean().to_numpy()
        result[f"zscore_{label}_smoothed"] = pd.Series(channel_z).rolling(
            smooth_points, center=True, min_periods=1
        ).mean().to_numpy()
        channel_normalization[label] = {
            "f0_mean": channel_f0,
            "dff_baseline_mean": float(np.mean(baseline_channel_dff)),
            "dff_baseline_sd": channel_sd,
        }
    result["normalization_baseline"] = baseline_mask.astype(int)
    if zero_time_min is None:
        result["relative_time_min"] = time_min
        result["relative_time_s"] = result["time_s"].to_numpy(float)
    else:
        result["relative_time_min"] = time_min - float(zero_time_min)
        result["relative_time_s"] = (time_min - float(zero_time_min)) * 60.0
    details = {
        "baseline_start_min": float(baseline_start_min),
        "baseline_end_min": float(baseline_end_min),
        "samples": int(baseline_mask.sum()),
        "f0_mean": f0,
        "f0_source": "mean of the corrected trace in the baseline interval",
        "dff_baseline_mean": z_center,
        "dff_baseline_sd": z_scale,
        "smooth_seconds": float(smooth_seconds),
        "zero_time_min": None if zero_time_min is None else float(zero_time_min),
        "channels": channel_normalization,
    }
    return result, details


def longest_gap_inside(time_s: np.ndarray, start: float, end: float) -> float:
    """Longest stretch without a sample that a ``[start, end]`` window straddles.

    The samples are the ones that survived the finite check, so a hole in the
    recording shows up here as one wide spacing between neighbours. A window that
    sits inside such a hole has a gap at least as long as the hole itself.
    """
    if len(time_s) < 2 or end <= start:
        return 0.0
    # Every spacing that overlaps [start, end]: from the sample just before the window to
    # the sample just after it. A spacing that merely touches an edge does not count.
    low = int(np.searchsorted(time_s, start, side="right")) - 1
    high = int(np.searchsorted(time_s, end, side="left"))
    low = max(0, low)
    high = min(len(time_s) - 1, high)
    if high - low < 1:
        return 0.0
    return float(np.max(np.diff(time_s[low:high + 1])))


def calculate_event_locked_traces(
    processed: pd.DataFrame,
    marker_times_min: list[float],
    marker_name: str,
    pre_seconds: float,
    post_seconds: float,
    baseline_seconds: float,
    signal_column: str = "analysis_trace",
    pool_baseline_sd: bool = False,
) -> tuple[pd.DataFrame, pd.DataFrame, dict[str, Any]]:
    """Split a corrected signal around repeated events and normalize each trial independently.

    ``pool_baseline_sd`` replaces the per-trial noise estimate with one value pooled over the
    pre-marker windows of every included trial, using the within-trial degrees of freedom. A trial
    whose own window is noisy then no longer gets a diluted z-score, at the price of assuming the
    noise is the same in every trial.
    """
    if signal_column not in processed:
        raise ValueError(f"The corrected signal column '{signal_column}' is unavailable.")
    if pre_seconds <= 0 or post_seconds <= 0:
        raise ValueError("The pre-marker and post-marker trace durations must both be greater than 0 seconds.")
    if baseline_seconds <= 0:
        raise ValueError("The event baseline duration must be greater than 0 seconds.")
    if baseline_seconds > pre_seconds:
        raise ValueError("The event baseline duration cannot exceed the pre-marker trace duration.")
    if not marker_times_min:
        raise ValueError(f"No markers named '{marker_name}' are available.")

    time_s = processed["original_time_s"].to_numpy(float)
    signal = processed[signal_column].to_numpy(float)
    valid_source = np.isfinite(time_s) & np.isfinite(signal)
    time_s, signal = time_s[valid_source], signal[valid_source]
    order = np.argsort(time_s)
    time_s, signal = time_s[order], signal[order]
    unique = np.r_[True, np.diff(time_s) > 0]
    time_s, signal = time_s[unique], signal[unique]
    if len(time_s) < 3:
        raise ValueError("Fewer than three finite corrected-signal samples are available for event analysis.")
    positive_dt = np.diff(time_s)
    positive_dt = positive_dt[positive_dt > 0]
    sample_interval = float(np.median(positive_dt))
    if not np.isfinite(sample_interval) or sample_interval <= 0:
        raise ValueError("The signal sampling interval could not be determined.")
    # Sparse recordings legitimately have wide spacing, so the limit follows the sampling
    # interval when that is already coarser than the absolute cap.
    gap_limit = max(MAX_EVENT_GAP_SECONDS, 4.0 * sample_interval)

    pre_point_count = max(2, int(round(pre_seconds / sample_interval)) + 1)
    post_point_count = max(2, int(round(post_seconds / sample_interval)) + 1)
    relative_time = np.concatenate((
        np.linspace(-float(pre_seconds), 0.0, pre_point_count)[:-1],
        np.linspace(0.0, float(post_seconds), post_point_count),
    ))
    baseline_mask = (relative_time >= -float(baseline_seconds)) & (relative_time < 0)
    if baseline_mask.sum() < 2:
        raise ValueError(
            "The event baseline contains fewer than two samples. Increase the baseline duration "
            "or use data with a higher sampling rate."
        )

    trial_rows: list[pd.DataFrame] = []
    excluded: list[dict[str, Any]] = []
    trial_arrays: list[tuple[np.ndarray, np.ndarray, np.ndarray]] = []
    collected: list[dict[str, Any]] = []
    for event_index, marker_time_min in enumerate(marker_times_min, start=1):
        marker_time_s = float(marker_time_min) * 60.0
        absolute_time = marker_time_s + relative_time
        if absolute_time[0] < time_s[0] or absolute_time[-1] > time_s[-1]:
            excluded.append({
                "event_index": event_index,
                "marker_time_min": float(marker_time_min),
                "reason": "the requested trace window extends outside the corrected recording",
            })
            continue
        window_gap = longest_gap_inside(time_s, absolute_time[0], absolute_time[-1])
        if window_gap > gap_limit:
            # np.interp would happily draw a straight line across a paused recording and the
            # trial would look complete. A gap wider than the limit means the window has no
            # data of its own, so it is reported instead of being averaged in.
            excluded.append({
                "event_index": event_index,
                "marker_time_min": float(marker_time_min),
                "reason": (f"the trace window spans a {window_gap:.2f} s hole in the corrected "
                           f"signal (limit {gap_limit:g} s)"),
            })
            continue
        trace = np.interp(absolute_time, time_s, signal)
        baseline_values = trace[baseline_mask]
        # F0 is this trial's own pre-marker mean, measured on the corrected trace.
        f0 = float(np.mean(baseline_values))
        if not np.isfinite(f0) or np.isclose(f0, 0):
            excluded.append({
                "event_index": event_index,
                "marker_time_min": float(marker_time_min),
                "reason": "the trial baseline mean is zero or invalid",
            })
            continue
        dff = 100.0 * (trace - f0) / f0
        baseline_dff = dff[baseline_mask]
        baseline_center = float(np.mean(baseline_dff))
        baseline_sd = float(np.std(baseline_dff, ddof=1))
        if not np.isfinite(baseline_sd):
            excluded.append({
                "event_index": event_index,
                "marker_time_min": float(marker_time_min),
                "reason": "the trial baseline standard deviation is invalid",
            })
            continue
        if not pool_baseline_sd and baseline_sd <= np.finfo(float).eps:
            excluded.append({
                "event_index": event_index,
                "marker_time_min": float(marker_time_min),
                "reason": "the trial baseline standard deviation is zero or invalid",
            })
            continue
        collected.append({
            "event_index": event_index,
            "marker_time_min": float(marker_time_min),
            "trace": trace,
            "dff": dff,
            "center": baseline_center,
            "own_sd": baseline_sd,
            "n_baseline": int(baseline_mask.sum()),
        })

    pooled_sd: float | None = None
    pooled_dof = 0
    if pool_baseline_sd and collected:
        # Textbook pooled within-trial SD: every trial contributes its own sum of squares, so a
        # trial with a longer window counts for more and no trial is weighted by its variance.
        pooled_dof = sum(max(0, item["n_baseline"] - 1) for item in collected)
        if pooled_dof > 0:
            total = sum(item["own_sd"] ** 2 * max(0, item["n_baseline"] - 1) for item in collected)
            pooled_sd = float(np.sqrt(total / pooled_dof))
        if pooled_sd is None or not np.isfinite(pooled_sd):
            excluded.append({"event_index": 0, "marker_time_min": float("nan"),
                             "reason": "the pooled baseline standard deviation is invalid"})
            pooled_sd = None

    for item in collected:
        sd_used = pooled_sd if (pool_baseline_sd and pooled_sd is not None) else item["own_sd"]
        if not np.isfinite(sd_used) or sd_used <= np.finfo(float).eps:
            excluded.append({
                "event_index": item["event_index"],
                "marker_time_min": item["marker_time_min"],
                "reason": "the baseline standard deviation used for the z-score is zero or invalid",
            })
            continue
        zscore = (item["dff"] - item["center"]) / sd_used
        trial_number = len(trial_rows) + 1
        trial_rows.append(pd.DataFrame({
            "trial": trial_number,
            "source_event_index": item["event_index"],
            "marker_name": marker_name,
            "marker_time_min": item["marker_time_min"],
            "relative_time_s": relative_time,
            "corrected_signal": item["trace"],
            "dff_percent": item["dff"],
            "zscore": zscore,
            "event_baseline": baseline_mask.astype(int),
            "f0": item["trace"][baseline_mask].mean(),
            "baseline_dff_sd": sd_used,
            "baseline_dff_sd_own": item["own_sd"],
            "baseline_dff_n": item["n_baseline"],
        }))
        trial_arrays.append((item["trace"], item["dff"], zscore))

    if not trial_rows:
        reasons = "; ".join(item["reason"] for item in excluded[:3])
        raise ValueError(f"No complete event trials could be analyzed. {reasons}")

    trials = pd.concat(trial_rows, ignore_index=True)
    corrected_stack = np.vstack([item[0] for item in trial_arrays])
    dff_stack = np.vstack([item[1] for item in trial_arrays])
    zscore_stack = np.vstack([item[2] for item in trial_arrays])
    trial_count = len(trial_arrays)

    def mean_and_sem(values: np.ndarray) -> tuple[np.ndarray, np.ndarray]:
        mean = np.mean(values, axis=0)
        if values.shape[0] < 2:
            return mean, np.full(values.shape[1], np.nan)
        return mean, np.std(values, axis=0, ddof=1) / np.sqrt(values.shape[0])

    corrected_mean, corrected_sem = mean_and_sem(corrected_stack)
    dff_mean, dff_sem = mean_and_sem(dff_stack)
    zscore_mean, zscore_sem = mean_and_sem(zscore_stack)
    # One trial's z is limited by that trial's own noise, and no smoother can change that
    # without also shrinking the response. The average trace is the honest way to a larger
    # number: its noise falls as sqrt(n), so it is standardised against its own baseline.
    baseline_mean_dff = dff_mean[baseline_mask]
    average_center = float(np.mean(baseline_mean_dff))
    average_sd = float(np.std(baseline_mean_dff, ddof=1)) if baseline_mean_dff.size > 1 else float("nan")
    if np.isfinite(average_sd) and average_sd > np.finfo(float).eps:
        zscore_of_average = (dff_mean - average_center) / average_sd
    else:
        zscore_of_average = np.full(dff_mean.shape, np.nan)
    average = pd.DataFrame({
        "relative_time_s": relative_time,
        "n_trials": trial_count,
        "corrected_signal_mean": corrected_mean,
        "corrected_signal_sem": corrected_sem,
        "dff_percent_mean": dff_mean,
        "dff_percent_sem": dff_sem,
        "zscore_mean": zscore_mean,
        "zscore_sem": zscore_sem,
        "zscore_of_average": zscore_of_average,
        "event_baseline": baseline_mask.astype(int),
    })
    details = {
        "marker_name": marker_name,
        "requested_events": len(marker_times_min),
        "included_trials": trial_count,
        "excluded_events": excluded,
        "pre_seconds": float(pre_seconds),
        "post_seconds": float(post_seconds),
        "baseline_seconds": float(baseline_seconds),
        "sample_interval_seconds": sample_interval,
        "signal_column": signal_column,
        "baseline_sd_mode": ("one value pooled across trials" if pool_baseline_sd
                             else "one value per trial"),
        "pooled_baseline_sd": pooled_sd,
        "pooled_baseline_dof": int(pooled_dof),
        "average_baseline_sd": average_sd,
        "zscore_of_average_note": (
            "zscore_of_average standardises the trial-averaged dF/F0 against that average's "
            "own baseline SD, so it grows roughly as sqrt(n_trials) against a single trial's z. "
            "Use it for the shape and significance of the average response, not as a per-trial z."
        ),
        "normalization": ("each trial uses its own pre-marker baseline; the z-score divides by one "
                          "standard deviation pooled across trials" if pool_baseline_sd
                          else "each trial uses its own pre-marker baseline"),
    }
    return trials, average, details


# --- Reusing a previous analysis -------------------------------------------

SAVED_ANALYSIS_FILE = "parameters_and_marker_edits.json"
# Sampling jitter between two exports must not be reported as a parameter change.
RANGE_TOLERANCE_MIN = 1e-3


def _as_float(value: Any) -> float | None:
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    return number if np.isfinite(number) else None


def _with_default(value: Any, default: float) -> float:
    number = _as_float(value)
    return default if number is None else number


def _regions_from_saved(raw: Any) -> list[tuple[float, float]]:
    regions: list[tuple[float, float]] = []
    for region in raw or []:
        try:
            start, end = float(region[0]), float(region[1])
        except (TypeError, ValueError, IndexError):
            continue
        regions.append((start, end))
    return regions


def _windows_from_saved(raw: Any) -> tuple[tuple[float, float], ...]:
    windows: list[tuple[float, float]] = []
    for window in raw or []:
        try:
            start, end = float(window[0]), float(window[1])
        except (TypeError, ValueError, IndexError):
            continue
        if end > start:
            windows.append((start, end))
    return tuple(windows)


def config_from_saved(payload: dict[str, Any]) -> ProcessingConfig:
    """Rebuild a ProcessingConfig from an exported parameters file.

    Missing keys fall back to the dataclass defaults, so files written by older
    versions still initialise.
    """
    processing = payload.get("processing") or {}
    if not isinstance(processing, dict):
        raise ValueError("The saved parameters file has no processing block.")
    default = ProcessingConfig(range_start_min=0.0, range_end_min=1.0)
    wavelengths = [str(value) for value in processing.get("wavelengths") or [] if str(value) in SUPPORTED_WAVELENGTHS]
    offsets = {value: _as_float(processing.get("offsets", {}).get(value)) for value in SUPPORTED_WAVELENGTHS}
    baselines = {value: _as_float(processing.get("baselines", {}).get(value)) for value in SUPPORTED_WAVELENGTHS}
    reference = processing.get("reference_wavelength", default.reference_wavelength)
    weight = processing.get("reference_weight")
    return ProcessingConfig(
        range_start_min=_with_default(processing.get("range_start_min"), 0.0),
        range_end_min=_with_default(processing.get("range_end_min"), 1.0),
        offsets={key: (value if value is not None else 0.0) for key, value in offsets.items()},
        baselines={key: (value if value is not None else 0.0) for key, value in baselines.items()},
        wavelengths=wavelengths,
        analysis_wavelength=str(processing.get("analysis_wavelength") or default.analysis_wavelength),
        reference_wavelength=None if reference in (None, "", "None") else str(reference),
        combine=str(processing.get("combine") or default.combine),
        reference_weight=_as_float(weight),
        fit_model=str(processing.get("fit_model") or default.fit_model),
        smooth_seconds=_with_default(processing.get("smooth_seconds"), 0.0),
        fit_regions={key: _regions_from_saved(value)
                     for key, value in (processing.get("fit_regions") or {}).items()},
        fit_models={key: value for key, value in (processing.get("fit_models") or {}).items()},
        fit_baseline_modes={key: value for key, value in (processing.get("fit_baseline_modes") or {}).items()},
        fit_betas={key: _as_float(value) for key, value in (processing.get("fit_betas") or {}).items()},
        fit_lambdas={key: _as_float(value) for key, value in (processing.get("fit_lambdas") or {}).items()},
        fit_smoothing={key: _as_float(value) for key, value in (processing.get("fit_smoothing") or {}).items()},
        exclusion_windows=_windows_from_saved(processing.get("exclusion_windows")),
        source_channel=str(processing.get("source_channel") or default.source_channel),
        bleach_correction=str(processing.get("bleach_correction") or default.bleach_correction),
    )


def fit_penalized_baseline(
    time_s: np.ndarray,
    values: np.ndarray,
    beta: float = PLS_DEFAULT_BETA,
    lam: float = PLS_DEFAULT_LAMBDA,
    max_points: int = PLS_MAX_POINTS,
    smoothing_seconds: float | None = None,
    sample_mask: np.ndarray | None = None,
) -> tuple[np.ndarray, dict[str, Any]]:
    """Lower-envelope bleaching baseline by iteratively reweighted penalised LS.

    The trace is fitted in log space with a second-difference (Whittaker)
    penalty, and samples that sit above the current fit are progressively
    down-weighted, so upward transients end up excluded from the baseline while
    the decay is still followed. Working in log space keeps a steep initial decay
    representable by a single smoothing strength.

    ``beta`` sharpens the asymmetric weighting (higher = closer to the lower
    envelope). The smoothing strength follows ``smoothing_seconds`` when it is given: the
    penalty is set so that fluctuations shorter than that period are smoothed away, which is
    what keeps a slow drift in the fit and a fast response out of it. Without it the legacy
    ``lam`` is used, whose cutoff depends on the recording length. Long recordings are
    decimated before fitting and the result is interpolated back, which is the down-sampling
    variant of the same algorithm.
    """
    x_full = np.asarray(time_s, float)
    x = x_full - float(x_full[0])
    y_full = np.asarray(values, float)
    keep = np.isfinite(x) & np.isfinite(y_full)
    total = x.shape[0]
    dropped = int(total - keep.sum())
    x, y = x[keep], y_full[keep]
    if x.size < 50:
        raise ValueError("Fewer than 50 valid samples are available for the PLS fit.")
    beta = float(np.clip(PLS_DEFAULT_BETA if beta is None else beta, PLS_MIN_BETA, PLS_MAX_BETA))
    lam = float(np.clip(PLS_DEFAULT_LAMBDA if lam is None else lam, 1e-2, PLS_MAX_LAMBDA))

    index = sample_indices(x.size, max_points)
    grid_x, grid_y = x[index], y[index]
    # Samples the caller excluded keep their place on the grid - the penalty still shapes the
    # curve across them, so the baseline continues smoothly instead of being bridged by a
    # straight chord - but they lose their say in the fit, so a response cannot drag the baseline.
    excluded_grid = np.zeros(index.size, bool)
    if sample_mask is not None:
        exclude = ~np.asarray(sample_mask, bool)[keep]
        excluded_grid = exclude[index]
        if int((~excluded_grid).sum()) < 50:
            raise ValueError("Fewer than 50 samples are left for the PLS fit after the "
                             "excluded windows.")
    floor = max(float(np.percentile(grid_y, 99)) * 1e-3, 1e-6)
    logged = np.log(np.maximum(grid_y, floor))
    n = logged.size
    sigma = float(np.median(np.abs(np.diff(logged)))) / 0.6745 / np.sqrt(2.0)
    sigma = max(sigma, 1e-9)
    # The Whittaker penalty attenuates a frequency f by 1 / (1 + lambda * (2 sin(pi f dt))^4), so
    # half power sits at lambda = (T / (2 pi dt))^4 for a period T. Inverting that lets the user
    # say "the baseline may follow drift slower than 120 s" instead of choosing a penalty.
    grid_dt = float(np.median(np.diff(grid_x))) if n > 1 else 1.0
    grid_dt = grid_dt if np.isfinite(grid_dt) and grid_dt > 0 else 1.0
    legacy_cutoff = 2.0 * np.pi * grid_dt * max(lam * (n / 1000.0) ** 2, 1e-12) ** 0.25
    if smoothing_seconds is not None and float(smoothing_seconds) > 0:
        window = float(np.clip(float(smoothing_seconds), 2.0 * grid_dt, 1e6))
        cutoff_period = window
    else:
        # Automatic: the legacy penalty, but never finer than the band a response lives in. On a
        # short recording the legacy value collapses to a few seconds, and a smoother that fine
        # follows the response itself, so the correction removes it.
        window = None
        cutoff_period = max(PLS_MIN_CUTOFF_SECONDS, legacy_cutoff)
    lam_scaled = float(np.clip((cutoff_period / (2.0 * np.pi * grid_dt)) ** 4, 1e-6, 1e14))

    difference = sparse.diags([1.0, -2.0, 1.0], [0, 1, 2], shape=(n - 2, n), format="csr")
    penalty = lam_scaled * (difference.T @ difference)

    def solve(weights: np.ndarray) -> np.ndarray:
        system = sparse.diags(weights, 0, shape=(n, n), format="csr") + penalty
        return spsolve(system.tocsr(), weights * logged)

    weights = np.ones(n)
    fitted_log = solve(weights)
    span = max(float(np.ptp(logged)), 1e-9)
    iterations = 0
    converged = False
    for iterations in range(1, PLS_MAX_ITERATIONS + 1):
        residual = logged - fitted_log
        weights = np.maximum(np.exp(-beta * np.maximum(residual, 0.0) / sigma), 1e-8)
        if excluded_grid.any():
            weights[excluded_grid] = 1e-8
        updated = solve(weights)
        step = float(np.max(np.abs(updated - fitted_log)))
        fitted_log = updated
        if step < PLS_TOLERANCE * span:
            converged = True
            break

    fitted_grid = np.exp(fitted_log)
    smoothing_warnings: list[str] = []
    if window is not None:
        # A window longer than the decay cannot follow it; the asymmetric reweighting then pulls
        # the curve below the data and the correction lifts the whole trace. Say so instead of
        # handing over a quietly broken baseline.
        tail = slice(max(0, int(0.8 * n)), n)
        drift = float(np.mean(grid_y[tail] - fitted_grid[tail])) / max(float(np.mean(grid_y[tail])), 1e-9)
        if abs(drift) > 0.02:
            smoothing_warnings.append(
                f"The smoothing window of {window:.0f} s is too long for this recording: the "
                f"baseline ends up {drift * 100:+.1f}% away from the data, so it cannot follow the "
                "decay. Shorten it (a third of the decay time is a good start) or leave it blank."
            )
    # The fit only sees finite samples, but the returned curve has to line up with the
    # caller's arrays: a single empty cell must not change the length of the result.
    fitted = np.full(total, np.nan)
    fitted[keep] = np.interp(x, grid_x, fitted_grid)
    knot_index = sample_indices(n, PLS_KNOT_COUNT)
    parameters: dict[str, Any] = {
        "model": "penalized",
        "beta": beta,
        "lambda": lam,
        "lambda_scaled": float(lam_scaled),
        "noise_sigma_log": sigma,
        "iterations": int(iterations),
        "converged": bool(converged),
        "tolerance": float(PLS_TOLERANCE),
        "fit_points": int(n),
        "smoothing_seconds": None if window is None else float(window),
        "cutoff_period_s": float(cutoff_period),
        "legacy_cutoff_s": float(legacy_cutoff),
        "smoothing_warnings": smoothing_warnings,
        "dropped_samples": dropped,
        "downsampled": bool(n < x.size),
        "curve_knots_t": [float(value) for value in grid_x[knot_index]],
        "curve_knots_z": [float(value) for value in fitted_grid[knot_index]],
    }
    return fitted, parameters


def fitted_curve_from_parameters(parameters: dict[str, Any], time_s: np.ndarray) -> np.ndarray:
    """Evaluate a saved fit on the current time axis, so a fit can be reused as is."""
    x = np.asarray(time_s, float)
    x = x - x[0]
    model = parameters.get("model")
    if model == "penalized":
        knots_t = parameters.get("curve_knots_t")
        knots_z = parameters.get("curve_knots_z")
        if not knots_t or not knots_z:
            raise ValueError("The saved PLS fit has no stored curve knots.")
        return np.interp(x, np.asarray(knots_t, float), np.asarray(knots_z, float))
    constant = parameters.get("fitted_constant")
    if constant is None:
        constant = parameters.get("fixed_baseline", 0.0)
    constant = float(constant)
    if model == "none":
        return np.full(x.shape, constant, float)
    if model == "linear":
        return constant + float(parameters["slope_per_s"]) * x
    if model == "single_exponential":
        return constant + float(parameters["amplitude"]) * np.exp(-x / float(parameters["tau_s"]))
    if model == "double_exponential":
        return (constant
                + float(parameters["amplitude_1"]) * np.exp(-x / float(parameters["tau_1_s"]))
                + float(parameters["amplitude_2"]) * np.exp(-x / float(parameters["tau_2_s"])))
    raise ValueError(f"The saved fit uses an unknown model: {model}")


def load_saved_analysis(path: Path) -> dict[str, Any]:
    """Read one exported parameters file and add the values used for comparison."""
    path = Path(path)
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        raise ValueError(f"{path.name} is not valid JSON: {exc}") from exc
    if not isinstance(payload, dict):
        raise ValueError(f"{path.name} does not contain a JSON object.")
    if not isinstance(payload.get("processing"), dict):
        raise ValueError(
            f"{path.name} has no 'processing' block, so it is not an exported "
            f"{SAVED_ANALYSIS_FILE}."
        )
    payload["_path"] = str(Path(path))
    payload["_config"] = config_from_saved(payload)
    return payload


def _describe_regions(regions: Any) -> str:
    pairs = [tuple(float(value) for value in region) for region in regions or []]
    return "; ".join(f"{start:g}-{end:g}" for start, end in pairs) if pairs else "none"


def compare_configs(
    saved: ProcessingConfig,
    current: ProcessingConfig,
) -> list[dict[str, Any]]:
    """Differences between a saved configuration and the current one.

    Every entry is ``{"scope", "parameter", "saved", "current"}``; ``scope`` is
    ``"global"`` or a wavelength. Per-channel rows are the ones that decide
    whether that channel has to be fitted again.
    """
    rows: list[dict[str, Any]] = []

    # The data range is compared numerically: the recorded end is a sampled
    # timestamp, so "0-10 min" and "0-9.99944 min" are the same interval.
    if (abs(saved.range_start_min - current.range_start_min) > RANGE_TOLERANCE_MIN
            or abs(saved.range_end_min - current.range_end_min) > RANGE_TOLERANCE_MIN):
        rows.append({
            "scope": "global", "parameter": "data range",
            "saved": f"{saved.range_start_min:g}-{saved.range_end_min:g} min",
            "current": f"{current.range_start_min:g}-{current.range_end_min:g} min",
        })

    def add(scope: str, parameter: str, saved_value: Any, current_value: Any) -> None:
        if saved_value == current_value:
            return
        if isinstance(saved_value, float) and isinstance(current_value, float):
            if abs(saved_value - current_value) <= 1e-9 * max(1.0, abs(saved_value)):
                return
        rows.append({"scope": scope, "parameter": parameter,
                     "saved": saved_value, "current": current_value})

    add("global", "wavelengths", ", ".join(saved.wavelengths), ", ".join(current.wavelengths))
    add("global", "analysis wavelength", saved.analysis_wavelength, current.analysis_wavelength)
    add("global", "reference wavelength", saved.reference_wavelength or "None",
        current.reference_wavelength or "None")
    add("global", "combine", saved.combine, current.combine)
    add("global", "reference weight", saved.reference_weight, current.reference_weight)
    add("global", "default fit model", saved.fit_model, current.fit_model)
    add("global", "smoothing (s)", float(saved.smooth_seconds), float(current.smooth_seconds))
    add("global", "bleaching correction", saved.bleach_correction, current.bleach_correction)
    add("global", "source channel", saved.source_channel, current.source_channel)

    for channel in sorted(set(saved.wavelengths) | set(current.wavelengths)):
        add(channel, "offset", float(saved.offsets.get(channel, 0.0)), float(current.offsets.get(channel, 0.0)))
        add(channel, "fitting baseline", float(saved.baselines.get(channel, 0.0)),
            float(current.baselines.get(channel, 0.0)))
        add(channel, "fit model", saved.fit_models.get(channel) or saved.fit_model,
            current.fit_models.get(channel) or current.fit_model)
        add(channel, "baseline mode", saved.fit_baseline_modes.get(channel) or "fit_constant",
            current.fit_baseline_modes.get(channel) or "fit_constant")
        add(channel, "fitting regions", _describe_regions(saved.fit_regions.get(channel)),
            _describe_regions(current.fit_regions.get(channel)))
        if (saved.fit_models.get(channel) or saved.fit_model) == "penalized":
            add(channel, "PLS beta", saved.fit_betas.get(channel), current.fit_betas.get(channel))
            add(channel, "PLS smoothing window", saved.fit_smoothing.get(channel),
                current.fit_smoothing.get(channel))
        add(channel, "excluded windows",
            f"{len(saved.exclusion_windows)} window(s)" if saved.exclusion_windows else "none",
            f"{len(current.exclusion_windows)} window(s)" if current.exclusion_windows else "none")
    return rows


def changed_fit_channels(saved: ProcessingConfig, current: ProcessingConfig) -> set[str]:
    """Channels whose fit inputs differ, i.e. the channels that must be fitted again."""
    scope_keys = {"offset", "fitting baseline", "fit model", "baseline mode", "fitting regions",
                  "excluded windows", "PLS smoothing window"}
    changed = {row["scope"] for row in compare_configs(saved, current)
               if row["scope"] != "global" and row["parameter"] in scope_keys}
    if (abs(saved.range_start_min - current.range_start_min) > RANGE_TOLERANCE_MIN
            or abs(saved.range_end_min - current.range_end_min) > RANGE_TOLERANCE_MIN):
        changed |= set(current.wavelengths)
    return changed
