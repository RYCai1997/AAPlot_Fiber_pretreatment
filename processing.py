"""Read-only data loading and wavelength-independent photometry fitting."""
from __future__ import annotations

import uuid
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import numpy as np
import pandas as pd
from scipy.optimize import least_squares


@dataclass
class ProcessingConfig:
    range_start_min: float
    range_end_min: float
    offsets: dict[str, float] = field(default_factory=dict)
    baselines: dict[str, float] = field(default_factory=dict)
    wavelengths: list[str] = field(default_factory=list)
    analysis_wavelength: str = "470"
    reference_wavelength: str | None = "410"
    combine: str = "ratio"  # ratio | subtraction
    fit_model: str = "double_exponential"
    smooth_seconds: float = 10.0
    fit_regions: dict[str, list[tuple[float, float]]] = field(default_factory=dict)
    fit_models: dict[str, str | None] = field(default_factory=dict)
    fit_baseline_modes: dict[str, str | None] = field(default_factory=dict)
    source_channel: str = "CH1"


SUPPORTED_WAVELENGTHS = ("410", "470", "560")


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


def load_recording(folder: Path) -> tuple[pd.DataFrame, list[dict[str, Any]], str]:
    path = folder / "Fluorescence.csv"
    if not path.exists():
        raise FileNotFoundError(f"File not found: {path}")
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


def fit_bleaching(
    time_s: np.ndarray,
    values: np.ndarray,
    baseline: float,
    model: str,
    fit_mask: np.ndarray,
    baseline_mode: str = "fixed",
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
    x = np.asarray(time_s, float) - float(time_s[0])
    y = np.asarray(values, float)
    valid = np.asarray(fit_mask, bool) & np.isfinite(x) & np.isfinite(y)
    if valid.sum() < 20:
        raise ValueError("The fitting regions contain fewer than 20 valid points in total.")
    valid_indices = np.flatnonzero(valid)
    keep = sample_indices(len(valid_indices), 4000)
    xf, yf = x[valid_indices[keep]], y[valid_indices[keep]]
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
            result = least_squares(lambda p: residual(p, xf, yf), [initial_slope], loss="soft_l1", f_scale=noise)
            result = least_squares(lambda p: residual(p, x[valid], y[valid]), result.x,
                                   loss="soft_l1", f_scale=noise)
            fitted = baseline + result.x[0] * x
            parameters: dict[str, Any] = {"fixed_baseline": baseline, "slope_per_s": float(result.x[0])}
            parameter_count = 1
        else:
            slope_guess = float(np.polyfit(xf, yf, 1)[0])
            residual = lambda p, xx, yy: p[0] + p[1] * xx - yy
            result = least_squares(lambda p: residual(p, xf, yf), [baseline, slope_guess], loss="soft_l1", f_scale=noise)
            result = least_squares(lambda p: residual(p, x[valid], y[valid]), result.x,
                                   loss="soft_l1", f_scale=noise)
            fitted = result.x[0] + result.x[1] * x
            parameters = {"baseline_initial": baseline, "fitted_constant": float(result.x[0]),
                          "slope_per_s": float(result.x[1])}
            parameter_count = 2
    else:
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
            candidates = [least_squares(lambda p: curve(p, xf) - yf, start, bounds=(lower, upper),
                                        loss="soft_l1", f_scale=noise, max_nfev=3000) for start in starts]
            result = min(candidates, key=lambda item: item.cost)
            result = least_squares(lambda p: curve(p, x[valid]) - y[valid], result.x,
                                   bounds=(lower, upper), loss="soft_l1", f_scale=noise, max_nfev=5000)
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
            candidates = [least_squares(lambda p: curve(p, xf) - yf, start, bounds=(lower, upper),
                                        loss="soft_l1", f_scale=noise, max_nfev=5000) for start in starts]
            result = min(candidates, key=lambda item: item.cost)
            result = least_squares(lambda p: curve(p, x[valid]) - y[valid], result.x,
                                   bounds=(lower, upper), loss="soft_l1", f_scale=noise, max_nfev=8000)
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


def select_effective_data(data: pd.DataFrame, config: ProcessingConfig) -> pd.DataFrame:
    time_min = data["TimeStamp"].to_numpy(float) / 60000
    use = (time_min >= config.range_start_min) & (time_min <= config.range_end_min)
    if use.sum() < 20:
        raise ValueError("The valid data range contains fewer than 20 points.")
    return data.loc[use].reset_index(drop=True)


def process_data(data: pd.DataFrame, config: ProcessingConfig) -> tuple[pd.DataFrame, dict[str, Any]]:
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
    masks: dict[str, np.ndarray] = {}
    for wavelength in wavelengths:
        column = f"{source}-{wavelength}"
        raw = subset[column].to_numpy(float)
        if np.isfinite(raw).sum() < 20:
            raise ValueError(f"{column} contains fewer than 20 valid samples in the selected range.")
        offset = float(config.offsets[wavelength])
        baseline = float(config.baselines[wavelength])
        adjusted = raw - offset
        mask = region_mask(time_min, config.fit_regions.get(wavelength, []))
        model = config.fit_models.get(wavelength) or config.fit_model
        baseline_mode = config.fit_baseline_modes.get(wavelength) or "fit_constant"
        fitted, parameters = fit_bleaching(time_s, adjusted, baseline, model, mask, baseline_mode)
        parameters["fit_regions"] = [list(region) for region in config.fit_regions.get(wavelength, [])]
        if np.any(np.isfinite(fitted) & np.isclose(fitted, 0)):
            raise ValueError(
                f"The fitted {wavelength} curve contains zero. Adjust its baseline, offset, or fitting regions."
            )
        channel_corrected = adjusted / fitted * baseline
        output_columns[f"raw_{wavelength}"] = raw
        output_columns[f"offset_adjusted_{wavelength}"] = adjusted
        output_columns[f"fit_{wavelength}"] = fitted
        output_columns[f"corrected_{wavelength}"] = channel_corrected
        output_columns[f"fit_used_{wavelength}"] = mask.astype(int)
        corrected[wavelength] = channel_corrected
        masks[wavelength] = mask
        fit_parameters[wavelength] = parameters

    primary = config.analysis_wavelength
    reference = config.reference_wavelength
    combined = np.full(len(subset), np.nan)
    combined_label: str | None = None
    if reference is not None:
        if config.combine == "ratio":
            if np.any(np.isfinite(corrected[reference]) & np.isclose(corrected[reference], 0)):
                raise ValueError(
                    f"The corrected {reference} trace contains zero, so the ratio cannot be calculated."
                )
            combined = corrected[primary] / corrected[reference]
            combined_label = f"Corrected {primary} / {reference} ratio"
            analysis_reference = config.baselines[primary] / config.baselines[reference]
        elif config.combine == "subtraction":
            combined = corrected[primary] - corrected[reference]
            combined_label = f"Corrected {primary} - {reference}"
            analysis_reference = config.baselines[primary] - config.baselines[reference]
        else:
            raise ValueError(f"Unknown combination method: {config.combine}")
        analysis_trace = combined.copy()
    else:
        analysis_trace = corrected[primary].copy()
        analysis_reference = config.baselines[primary]

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
        "analysis_reference_from_user_baselines": float(analysis_reference),
        "normalization": None,
        "source_channel": source,
        "definitions": {
            "baseline": "user value is either a fixed model constant or the initial estimate for a fitted constant, as recorded per channel",
            "corrected_channel": "(raw - offset) / independently fitted bleaching * user baseline",
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
    """Calculate dF/F0 and z-score from a user-selected baseline interval."""
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
        raise ValueError("F0 in the normalization baseline interval is zero or invalid, so dF/F0 cannot be calculated.")
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
        "dff_baseline_mean": z_center,
        "dff_baseline_sd": z_scale,
        "smooth_seconds": float(smooth_seconds),
        "zero_time_min": None if zero_time_min is None else float(zero_time_min),
        "channels": channel_normalization,
    }
    return result, details


def calculate_event_locked_traces(
    processed: pd.DataFrame,
    marker_times_min: list[float],
    marker_name: str,
    pre_seconds: float,
    post_seconds: float,
    baseline_seconds: float,
    signal_column: str = "analysis_trace",
) -> tuple[pd.DataFrame, pd.DataFrame, dict[str, Any]]:
    """Split a corrected signal around repeated events and normalize each trial independently."""
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
        trace = np.interp(absolute_time, time_s, signal)
        baseline_values = trace[baseline_mask]
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
        if not np.isfinite(baseline_sd) or baseline_sd <= np.finfo(float).eps:
            excluded.append({
                "event_index": event_index,
                "marker_time_min": float(marker_time_min),
                "reason": "the trial baseline standard deviation is zero or invalid",
            })
            continue
        zscore = (dff - baseline_center) / baseline_sd
        trial_number = len(trial_rows) + 1
        trial_rows.append(pd.DataFrame({
            "trial": trial_number,
            "source_event_index": event_index,
            "marker_name": marker_name,
            "marker_time_min": float(marker_time_min),
            "relative_time_s": relative_time,
            "corrected_signal": trace,
            "dff_percent": dff,
            "zscore": zscore,
            "event_baseline": baseline_mask.astype(int),
            "f0": f0,
            "baseline_dff_sd": baseline_sd,
        }))
        trial_arrays.append((trace, dff, zscore))

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
    average = pd.DataFrame({
        "relative_time_s": relative_time,
        "n_trials": trial_count,
        "corrected_signal_mean": corrected_mean,
        "corrected_signal_sem": corrected_sem,
        "dff_percent_mean": dff_mean,
        "dff_percent_sem": dff_sem,
        "zscore_mean": zscore_mean,
        "zscore_sem": zscore_sem,
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
        "normalization": "each trial uses its own pre-marker baseline",
    }
    return trials, average, details
