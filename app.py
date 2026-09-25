#!/usr/bin/env python
"""Interactive GUI for independent 410/470/560 fiber-photometry pretreatment."""
from __future__ import annotations

import json
import math
import re
import sys
import uuid
from dataclasses import asdict
from pathlib import Path
from typing import Any, Callable

import numpy as np
import pandas as pd

from processing import (
    ProcessingConfig,
    available_recording_channels,
    calculate_event_locked_traces,
    calculate_normalized_traces,
    fit_bleaching,
    load_recording,
    process_data,
    region_mask,
    select_effective_data,
)


DEFAULT_STRINGS = {
    "folder": "", "range_start": "0", "range_end": "0",
    "offset410": "0", "offset470": "0", "offset560": "0",
    "baseline410": "", "baseline470": "", "baseline560": "",
    "combine": "ratio", "fit_model": "double_exponential", "smooth": "10",
    "fit410_status": "Not set", "fit470_status": "Not set", "fit560_status": "Not set",
    "norm_start": "0", "norm_end": "1",
    "norm_pre_duration": "5", "zero_time": "0", "downsample_value": "1",
    "export_window_duration": "5",
    "event_pre_seconds": "10", "event_post_seconds": "40", "event_baseline_seconds": "2",
    "event_status": "Select a marker name after loading data.",
    "x_start": "", "x_span": "", "x_end": "", "output_name": "",
    "marker_time": "0", "marker_name": "marker", "status": "Select a recording folder.",
}

LINE_WIDTH_LABELS = {
    "raw470": "Raw 470",
    "fit470": "470 fit",
    "corrected470": "Corrected 470",
    "raw410": "Raw 410",
    "fit410": "410 fit",
    "corrected410": "Corrected 410",
    "raw560": "Raw 560",
    "fit560": "560 fit",
    "corrected560": "Corrected 560",
    "combined": "Ratio / subtraction",
    "dff": "dF/F0",
    "zscore": "Z-score",
}

WAVELENGTHS = ("410", "470", "560")
WAVELENGTH_COLORS = {
    "410": ("#4D8FCC", "#15558D"),
    "470": ("#4BAF72", "#006D3C"),
    "560": ("#E6A33A", "#B86F00"),
}


class FitWindow:
    """Channel-specific fitting window with draggable interval cursors."""

    def __init__(
        self,
        parent: Any,
        channel: str,
        data: pd.DataFrame,
        markers: list[dict[str, Any]],
        config: ProcessingConfig,
        existing_regions: list[tuple[float, float]],
        on_accept: Callable[[str, list[tuple[float, float]], tuple[Any, ...], str, str], None],
    ) -> None:
        import tkinter as tk
        from tkinter import ttk
        from matplotlib.backends.backend_tkagg import FigureCanvasTkAgg, NavigationToolbar2Tk
        from matplotlib.figure import Figure

        self.tk, self.ttk = tk, ttk
        self.channel, self.config, self.on_accept = channel, config, on_accept
        self.markers = [dict(marker) for marker in markers]
        self.window = tk.Toplevel(parent)
        self.window.title(f"{channel} Fitting Regions")
        self.window.geometry("1200x760")
        subset = select_effective_data(data, config)
        self.time_s = subset["TimeStamp"].to_numpy(float) / 1000
        self.time_min = self.time_s / 60
        raw = subset[f"{config.source_channel}-{channel}"].to_numpy(float)
        offset = config.offsets[channel]
        self.baseline = config.baselines[channel]
        self.values = raw - offset
        self.regions = [tuple(sorted(region)) for region in existing_regions]
        start, end = float(self.time_min[0]), float(self.time_min[-1])
        if not self.regions:
            self.regions = [(start + 0.15 * (end - start), start + 0.35 * (end - start))]
        initial_model = config.fit_models.get(channel)
        self.model_var = tk.StringVar(value=initial_model or config.fit_model)
        self.baseline_mode_var = tk.StringVar(value="fit_constant")
        self.signal_width_var = tk.DoubleVar(value=1.0)
        self.fit_width_var = tk.DoubleVar(value=1.2)
        self.signal_width_text = tk.StringVar(value="1.0")
        self.fit_width_text = tk.StringVar(value="1.2")
        self.fitted_preview: np.ndarray | None = None
        self.selected_model = self.model_var.get()
        self.selected_baseline_mode = self.baseline_mode_var.get()
        self.selected_region_index = 0
        self.drag_action: str | None = None
        self.drag_start_x = 0.0
        self.drag_original_region = (0.0, 0.0)
        self.region_patches: list[Any] = []
        self.readout = tk.StringVar(value="Drag a region to move it; drag either edge to resize it.")

        pane = ttk.Panedwindow(self.window, orient=tk.HORIZONTAL)
        pane.pack(fill=tk.BOTH, expand=True)
        left, right = ttk.Frame(pane, padding=8, width=270), ttk.Frame(pane)
        pane.add(left, weight=0); pane.add(right, weight=1)
        ttk.Label(left, text=f"{channel} nm", font=("Segoe UI", 12, "bold")).pack(anchor="w")
        ttk.Label(left, text=f"Fitting baseline: {self.baseline:g}", justify="left").pack(anchor="w", pady=(2, 5))
        ttk.Label(left, text="Bleaching model").pack(anchor="w")
        self.model_box = ttk.Combobox(
            left, textvariable=self.model_var,
            values=["linear", "single_exponential", "double_exponential"], state="readonly",
        )
        self.model_box.pack(fill=tk.X, pady=(0, 7))
        self.model_box.bind("<<ComboboxSelected>>", self.invalidate_preview)
        ttk.Label(left, text="The baseline initializes the freely fitted constant (fit_constant).",
                  wraplength=250).pack(anchor="w", pady=(0, 5))
        ttk.Button(left, text="Add Region", command=self.add_region).pack(fill=tk.X, pady=2)
        ttk.Button(left, text="Delete Selected Region", command=self.delete_region).pack(fill=tk.X, pady=2)
        self.region_list = tk.Listbox(left, height=12, exportselection=False)
        self.region_list.pack(fill=tk.BOTH, expand=True, pady=6)
        self.region_list.bind("<<ListboxSelect>>", self.list_selection_changed)
        ttk.Button(left, text="Preview Fit", command=self.preview_fit).pack(fill=tk.X, pady=2)
        ttk.Button(left, text="Use These Regions", command=self.accept, style="Accent.TButton").pack(fill=tk.X, pady=2)
        ttk.Label(left, textvariable=self.readout, wraplength=250).pack(anchor="w", pady=(8, 0))

        self.figure = Figure(figsize=(9, 7), dpi=100, constrained_layout=True)
        self.ax = self.figure.subplots(1, 1)
        self.canvas = FigureCanvasTkAgg(self.figure, master=right)
        toolbar = NavigationToolbar2Tk(self.canvas, right, pack_toolbar=False)
        toolbar.update(); toolbar.pack(fill=tk.X)
        self.canvas.get_tk_widget().pack(fill=tk.BOTH, expand=True)
        fit_footer = ttk.Frame(right, padding=(8, 4)); fit_footer.pack(fill=tk.X)
        ttk.Label(fit_footer, text="Signal Width").pack(side=tk.LEFT)
        ttk.Scale(fit_footer, from_=0.4, to=3.0, variable=self.signal_width_var,
                  command=lambda _value: self.fit_line_width_changed(), length=150).pack(side=tk.LEFT, padx=6)
        ttk.Label(fit_footer, textvariable=self.signal_width_text, width=4).pack(side=tk.LEFT)
        ttk.Label(fit_footer, text="Fit Width").pack(side=tk.LEFT, padx=(16, 0))
        ttk.Scale(fit_footer, from_=0.4, to=3.0, variable=self.fit_width_var,
                  command=lambda _value: self.fit_line_width_changed(), length=150).pack(side=tk.LEFT, padx=6)
        ttk.Label(fit_footer, textvariable=self.fit_width_text, width=4).pack(side=tk.LEFT)
        self.canvas.mpl_connect("button_press_event", self.press)
        self.canvas.mpl_connect("motion_notify_event", self.motion)
        self.canvas.mpl_connect("button_release_event", self.release)
        self.refresh_list(); self.draw()

    def signature(self) -> tuple[Any, ...]:
        offset = self.config.offsets[self.channel]
        return (self.config.range_start_min, self.config.range_end_min, offset, self.baseline,
                self.selected_model, self.selected_baseline_mode)

    def refresh_list(self) -> None:
        self.region_list.delete(0, self.tk.END)
        for index, (start, end) in enumerate(self.regions, 1):
            self.region_list.insert(self.tk.END, f"{index}. {start:.4f} – {end:.4f} min")
        if self.regions:
            self.selected_region_index = int(np.clip(self.selected_region_index, 0, len(self.regions) - 1))
            self.region_list.selection_set(self.selected_region_index)
            self.region_list.see(self.selected_region_index)

    def add_region(self) -> None:
        view_low, view_high = self.ax.get_xlim()
        data_low, data_high = float(self.time_min[0]), float(self.time_min[-1])
        view_low, view_high = max(view_low, data_low), min(view_high, data_high)
        width = max((view_high - view_low) * 0.2, (data_high - data_low) * 0.005)
        center = (view_low + view_high) / 2
        low = max(data_low, center - width / 2)
        high = min(data_high, low + width)
        low = max(data_low, high - width)
        self.regions.append((low, high))
        self.fitted_preview = None
        self.selected_region_index = len(self.regions) - 1
        self.refresh_list(); self.draw()

    def delete_region(self) -> None:
        selected = self.region_list.curselection()
        if selected:
            del self.regions[selected[0]]
            self.fitted_preview = None
            self.selected_region_index = min(selected[0], len(self.regions) - 1)
            self.refresh_list(); self.draw()

    def list_selection_changed(self, _event: Any = None) -> None:
        selected = self.region_list.curselection()
        if selected:
            self.selected_region_index = selected[0]
            self.update_patch_styles()
            self.canvas.draw_idle()

    def invalidate_preview(self, _event: Any = None) -> None:
        self.fitted_preview = None
        self.draw()

    def draw(self, fitted: np.ndarray | None = None) -> None:
        xlim = self.ax.get_xlim() if self.ax.has_data() else None
        if fitted is not None:
            self.fitted_preview = np.asarray(fitted, dtype=float)
        self.ax.clear()
        color = WAVELENGTH_COLORS.get(self.channel, ("#666666", "#333333"))[0]
        self.ax.plot(self.time_min, self.values, color=color, lw=float(self.signal_width_var.get()),
                     label=f"{self.channel} after offset", gid="signal")
        if self.fitted_preview is not None:
            self.ax.plot(self.time_min, self.fitted_preview, color="#D62728", ls="--",
                         lw=float(self.fit_width_var.get()), label="fit", gid="fit")
        self.region_patches = []
        for index, (start, end) in enumerate(self.regions):
            patch = self.ax.axvspan(start, end, facecolor="#B9C9D8", alpha=0.38,
                                   edgecolor="#4B6F8A", linewidth=1.2, zorder=0.5)
            self.region_patches.append(patch)
        selected_mask = region_mask(self.time_min, self.regions)
        selected_indices = np.flatnonzero(selected_mask)
        if selected_indices.size:
            self.ax.scatter(self.time_min[selected_indices], self.values[selected_indices], s=2.0,
                            color="#222222", alpha=0.35, label="samples used in fit")
        for marker in self.markers:
            marker_time = float(marker["time_min"])
            if self.time_min[0] <= marker_time <= self.time_min[-1]:
                marker_color = "#777777" if marker.get("source") == "original" else "#C65D3B"
                marker_style = ":" if marker.get("source") == "original" else "-"
                self.ax.axvline(marker_time, color=marker_color, ls=marker_style, lw=0.9, alpha=0.8)
                self.ax.annotate(str(marker.get("name", "marker")), xy=(marker_time, 1),
                                 xycoords=("data", "axes fraction"), xytext=(2, -2),
                                 textcoords="offset points", rotation=90, va="top",
                                 fontsize=8, color=marker_color)
        self.update_patch_styles()
        self.ax.set_title(f"{self.channel} fitting — drag blocks/edges; only shaded black samples enter fit")
        self.ax.set_xlabel("Recording time (min)"); self.ax.set_ylabel("Signal after offset")
        self.ax.grid(False)
        self.ax.spines[["top", "right"]].set_visible(False)
        self.ax.legend(frameon=False, loc="upper right")
        if xlim and np.isfinite(xlim).all():
            self.ax.set_xlim(xlim)
        else:
            self.ax.set_xlim(self.time_min[0], self.time_min[-1])
        self.canvas.draw_idle()

    def fit_line_width_changed(self) -> None:
        self.signal_width_text.set(f"{float(self.signal_width_var.get()):.1f}")
        self.fit_width_text.set(f"{float(self.fit_width_var.get()):.1f}")
        for line in self.ax.get_lines():
            if line.get_gid() == "signal":
                line.set_linewidth(float(self.signal_width_var.get()))
            elif line.get_gid() == "fit":
                line.set_linewidth(float(self.fit_width_var.get()))
        self.canvas.draw_idle()

    def update_patch_styles(self) -> None:
        for index, patch in enumerate(self.region_patches):
            selected = index == self.selected_region_index
            patch.set_edgecolor("#174A6E" if selected else "#7290A6")
            patch.set_linewidth(2.2 if selected else 1.0)
            patch.set_alpha(0.48 if selected else 0.28)

    @staticmethod
    def parameter_text(parameters: dict[str, Any]) -> str:
        model = parameters["model"]
        if model == "double_exponential":
            text = (f"A1={parameters['amplitude_1']:.6g}, tau1={parameters['tau_1_s']:.6g}s\n"
                    f"A2={parameters['amplitude_2']:.6g}, tau2={parameters['tau_2_s']:.6g}s")
        elif model == "single_exponential":
            text = f"A={parameters['amplitude']:.6g}, tau={parameters['tau_s']:.6g}s"
        else:
            text = f"slope={parameters['slope_per_s']:.6g}/s"
        if "fitted_constant" in parameters:
            text += f"\nC(fitted)={parameters['fitted_constant']:.6g}"
        elif "fixed_baseline" in parameters:
            text += f"\nC(fixed)={parameters['fixed_baseline']:.6g}"
        return text

    def preview_fit(self) -> None:
        from tkinter import messagebox
        try:
            mask = region_mask(self.time_min, self.regions)
            self.selected_model = self.model_var.get()
            self.selected_baseline_mode = self.baseline_mode_var.get()
            fitted, parameters = fit_bleaching(
                self.time_s, self.values, self.baseline, self.selected_model, mask,
                self.selected_baseline_mode,
            )
            warning_text = ("\nWarnings: " + "; ".join(parameters["fit_warnings"])) if parameters["fit_warnings"] else ""
            self.readout.set(
                f"Model: {self.selected_model} | baseline: {self.selected_baseline_mode}\n"
                f"Selected samples used in final refinement: {parameters['final_refinement_samples']:,}\n"
                f"RMSE: {parameters['rmse_selected']:.6g} | BIC: {parameters['bic_selected']:.3f}\n"
                f"{self.parameter_text(parameters)}\n"
                f"Converged: {'Yes' if parameters['optimizer_success'] else 'No'} | "
                f"nfev={parameters['optimizer_nfev']} | optimality={parameters['optimizer_optimality']:.3g}"
                f"{warning_text}"
            )
            self.draw(fitted)
        except Exception as exc:
            messagebox.showerror("Fit failed", str(exc), parent=self.window)

    def accept(self) -> None:
        from tkinter import messagebox
        try:
            mask = region_mask(self.time_min, self.regions)
            self.selected_model = self.model_var.get()
            self.selected_baseline_mode = self.baseline_mode_var.get()
            fit_bleaching(self.time_s, self.values, self.baseline, self.selected_model, mask,
                           self.selected_baseline_mode)
            self.on_accept(self.channel, list(self.regions), self.signature(), self.selected_model,
                           self.selected_baseline_mode)
            self.window.destroy()
        except Exception as exc:
            messagebox.showerror("Invalid fitting regions", str(exc), parent=self.window)

    def nearest_value(self, x: float) -> tuple[float, float]:
        index = int(np.clip(np.searchsorted(self.time_min, x), 0, len(self.time_min) - 1))
        return float(self.time_min[index]), float(self.values[index])

    def press(self, event: Any) -> None:
        if event.inaxes is not self.ax or event.xdata is None or event.button != 1:
            return
        x = float(event.xdata)
        visible_width = max(self.ax.get_xlim()[1] - self.ax.get_xlim()[0], 1e-9)
        edge_tolerance = visible_width * 0.018
        candidates: list[tuple[float, int, str]] = []
        for index, (low, high) in enumerate(self.regions):
            if abs(x - low) <= edge_tolerance:
                candidates.append((abs(x - low), index, "left"))
            if abs(x - high) <= edge_tolerance:
                candidates.append((abs(x - high), index, "right"))
            if low < x < high:
                candidates.append((edge_tolerance * 1.5, index, "move"))
        if not candidates:
            return
        _, index, action = min(candidates, key=lambda item: item[0])
        self.selected_region_index = index
        self.drag_action = action
        self.drag_start_x = x
        self.drag_original_region = self.regions[index]
        self.refresh_list()
        self.update_patch_styles()
        self.canvas.draw_idle()

    def motion(self, event: Any) -> None:
        if self.drag_action and event.inaxes is self.ax and event.xdata is not None:
            self.move_region(float(event.xdata))

    def release(self, _event: Any) -> None:
        if self.drag_action:
            self.drag_action = None
            self.fitted_preview = None
            self.refresh_list()
            self.draw()

    def move_region(self, x: float) -> None:
        data_low, data_high = float(self.time_min[0]), float(self.time_min[-1])
        x = float(np.clip(x, data_low, data_high))
        low0, high0 = self.drag_original_region
        minimum_width = max((data_high - data_low) * 1e-5, np.finfo(float).eps)
        if self.drag_action == "left":
            low, high = min(x, high0 - minimum_width), high0
        elif self.drag_action == "right":
            low, high = low0, max(x, low0 + minimum_width)
        else:
            width = high0 - low0
            delta = x - self.drag_start_x
            low, high = low0 + delta, high0 + delta
            if low < data_low:
                low, high = data_low, data_low + width
            if high > data_high:
                low, high = data_high - width, data_high
        low, high = max(data_low, low), min(data_high, high)
        self.regions[self.selected_region_index] = (low, high)
        patch = self.region_patches[self.selected_region_index]
        patch.set_x(low)
        patch.set_width(high - low)
        nearest_x, y = self.nearest_value(x)
        self.readout.set(
            f"Region {self.selected_region_index + 1}: {low:.5f}–{high:.5f} min\n"
            f"Current X={nearest_x:.5f} min, Y={y:.6g}"
        )
        self.canvas.draw_idle()

class PretreatmentApp:
    def __init__(self, root: Any) -> None:
        import tkinter as tk
        from tkinter import ttk
        from matplotlib.backends.backend_tkagg import FigureCanvasTkAgg, NavigationToolbar2Tk
        from matplotlib.figure import Figure
        from matplotlib.widgets import RectangleSelector

        self.tk, self.ttk, self.root = tk, ttk, root
        self.RectangleSelector = RectangleSelector
        root.title("RWD Fiber Pretreatment — Fiber Photometry")
        root.geometry("1650x950")
        root.minsize(1120, 720)
        self.folder: Path | None = None
        self.data: pd.DataFrame | None = None
        self.metadata = ""
        self.original_markers: list[dict[str, Any]] = []
        self.markers: list[dict[str, Any]] = []
        self.processed: pd.DataFrame | None = None
        self.details: dict[str, Any] | None = None
        self.fit_regions = {wavelength: [] for wavelength in WAVELENGTHS}
        self.fit_signatures: dict[str, tuple[Any, ...] | None] = {wavelength: None for wavelength in WAVELENGTHS}
        self.fit_models: dict[str, str | None] = {wavelength: None for wavelength in WAVELENGTHS}
        self.fit_baseline_modes: dict[str, str | None] = {wavelength: "fit_constant" for wavelength in WAVELENGTHS}
        self.recording_channels: dict[str, list[str]] = {}
        self.available_wavelengths: list[str] = []
        self.active_source_channel = "CH1"
        self.channel_ranges: dict[str, tuple[float, float]] = {}
        self.axes: list[Any] = []
        self.axis_keys: list[str] = []
        self.normalized = False
        self.event_trials: pd.DataFrame | None = None
        self.event_average: pd.DataFrame | None = None
        self.event_details: dict[str, Any] | None = None
        self.event_view_active = False
        self.hover_x_lines: list[Any] = []
        self.hover_y_lines: list[Any] = []
        self.hover_y_labels: list[Any] = []
        self.hover_x_label: Any | None = None
        self.hover_y_label: Any | None = None
        self.hover_background: Any | None = None
        self.axis_scale_vars: dict[str, Any] = {}
        self.axis_lower_vars: dict[str, Any] = {}
        self.axis_upper_vars: dict[str, Any] = {}
        self.initial_x_limits: tuple[float, float] | None = None
        self.initial_y_limits: dict[str, tuple[float, float]] = {}
        self.display_cache: dict[tuple[str, float], np.ndarray] = {}
        self.zoom_selectors: list[Any] = []
        self.axis_drag_state: dict[str, Any] | None = None
        self.box_drag_active = False
        self.vars = {key: tk.StringVar(value=value) for key, value in DEFAULT_STRINGS.items()}
        self.trace_view_var = tk.StringVar(value="overlay")
        self.show_fit_var = tk.BooleanVar(value=True)
        self.line_width_vars = {key: tk.DoubleVar(value=1.0) for key in LINE_WIDTH_LABELS}
        self.line_width_selector_var = tk.StringVar(value=LINE_WIDTH_LABELS["raw470"])
        self.line_width_text = tk.StringVar(value="1.0")
        self.x_left_label_var = tk.StringVar(value="X Left (min)")
        self.x_range_label_var = tk.StringVar(value="X Range (min)")
        self.x_right_label_var = tk.StringVar(value="X Right (min)")
        self.smooth_enabled_var = tk.BooleanVar(value=True)
        self.downsample_enabled_var = tk.BooleanVar(value=True)
        self.downsample_mode_var = tk.StringVar(value="seconds")
        self.applied_smooth_seconds = 0.0
        self.applied_downsample_enabled = False
        self.applied_downsample_mode = "seconds"
        self.applied_downsample_value = 1.0
        self.norm_mode_var = tk.StringVar(value="manual")
        self.norm_marker_var = tk.StringVar(value="")
        self.zero_enabled_var = tk.BooleanVar(value=True)
        self.zero_mode_var = tk.StringVar(value="marker")
        self.zero_marker_var = tk.StringVar(value="")
        self.source_channel_var = tk.StringVar(value="CH1")
        self.analysis_wavelength_var = tk.StringVar(value="470")
        self.reference_wavelength_var = tk.StringVar(value="410")
        self.event_marker_var = tk.StringVar(value="")
        self.event_marker_candidates: list[dict[str, Any]] = []
        self.event_marker_list_name: str | None = None
        self.event_signal_candidates: list[dict[str, str]] = []
        self.event_signal_status_var = tk.StringVar(value="Apply correction, then select event signals.")
        self.export_annotation_var = tk.StringVar(value="none")
        self.export_marker_var = tk.StringVar(value="")
        self.export_vars = {
            "corrected_csv": tk.BooleanVar(value=False), "corrected_png": tk.BooleanVar(value=True),
            "dff_csv": tk.BooleanVar(value=True), "dff_png": tk.BooleanVar(value=True), "dff_svg": tk.BooleanVar(value=False),
            "zscore_csv": tk.BooleanVar(value=True), "zscore_png": tk.BooleanVar(value=True), "zscore_svg": tk.BooleanVar(value=False),
            "event_csv": tk.BooleanVar(value=False), "event_png": tk.BooleanVar(value=False),
            "event_svg": tk.BooleanVar(value=False),
        }

        palette = {
            "window": "#eef2f5", "panel": "#f7f9fb", "accent": "#2f6f9f",
            "accent_active": "#255d86", "text": "#263746", "muted": "#657786",
            "tab": "#dfe7ee", "border": "#ccd6df",
        }
        root.configure(background=palette["window"])
        style = ttk.Style(root)
        if "clam" in style.theme_names():
            style.theme_use("clam")
        style.configure("TFrame", background=palette["panel"])
        style.configure("TLabel", background=palette["panel"], foreground=palette["text"], font=("Segoe UI", 9))
        style.configure("TCheckbutton", background=palette["panel"], foreground=palette["text"], font=("Segoe UI", 9))
        style.configure("TRadiobutton", background=palette["panel"], foreground=palette["text"], font=("Segoe UI", 9))
        style.configure("TNotebook", background=palette["window"], borderwidth=0)
        style.configure("TNotebook.Tab", background=palette["tab"], foreground=palette["muted"],
                        font=("Segoe UI", 9, "bold"), padding=(14, 8))
        style.map("TNotebook.Tab",
                  background=[("selected", palette["panel"]), ("active", "#e9eef3")],
                  foreground=[("selected", palette["accent"]), ("active", palette["text"])])
        style.configure("TLabelframe", padding=8)
        style.configure("TLabelframe.Label", font=("Segoe UI", 10, "bold"))
        style.configure("TButton", font=("Segoe UI", 9), padding=(7, 4))
        style.configure("Accent.TButton", background=palette["accent"], foreground="white",
                        font=("Segoe UI", 10, "bold"), padding=(9, 7), borderwidth=0)
        style.map("Accent.TButton", background=[("active", palette["accent_active"]),
                                                ("pressed", palette["accent_active"]),
                                                ("disabled", "#9eb4c5")])
        style.configure("TEntry", padding=4, fieldbackground="white")
        style.configure("TCombobox", padding=3, fieldbackground="white")
        style.configure("Section.TLabel", foreground=palette["accent"],
                        font=("Segoe UI", 10, "bold"), padding=(0, 2))
        style.configure("Hint.TLabel", foreground=palette["muted"], font=("Segoe UI", 8))
        style.configure("Status.TLabel", foreground=palette["accent"], font=("Segoe UI", 9, "bold"))
        style.configure("HeaderTitle.TLabel", foreground=palette["text"], font=("Segoe UI", 15, "bold"))
        style.configure("HeaderSubtitle.TLabel", foreground=palette["muted"], font=("Segoe UI", 9))

        pane = ttk.Panedwindow(root, orient=tk.HORIZONTAL); pane.pack(fill=tk.BOTH, expand=True, padx=10, pady=10)
        controls, plot_frame = ttk.Frame(pane, width=430), ttk.Frame(pane)
        pane.add(controls, weight=0); pane.add(plot_frame, weight=1)
        header = ttk.Frame(controls, padding=(12, 8))
        header.pack(fill=tk.X, pady=(0, 6))
        ttk.Label(header, text="RWD Fiber Pretreatment", style="HeaderTitle.TLabel").pack(anchor="w")
        ttk.Label(header, text="Fiber photometry correction, normalization, and export",
                  style="HeaderSubtitle.TLabel").pack(anchor="w", pady=(1, 0))
        notebook = ttk.Notebook(controls)
        notebook.pack(fill=tk.BOTH, expand=True)
        data_tab = ttk.Frame(notebook, padding=12)
        display_tab = ttk.Frame(notebook, padding=12)
        event_tab = ttk.Frame(notebook, padding=12)
        export_tab = ttk.Frame(notebook, padding=12)
        notebook.add(data_tab, text="Data & Fitting")
        notebook.add(display_tab, text="dF/F0 & Z-score")
        notebook.add(event_tab, text="Event Analysis")
        notebook.add(export_tab, text="Export Results")

        row = 0
        ttk.Button(data_tab, text="Select Recording Folder", command=self.open_folder, style="Accent.TButton").grid(row=row, column=0, columnspan=2, sticky="ew"); row += 1
        ttk.Label(data_tab, textvariable=self.vars["folder"], wraplength=350).grid(row=row, column=0, columnspan=2, sticky="w", pady=(3, 9)); row += 1
        ttk.Label(data_tab, text="Recording Channel").grid(row=row, column=0, sticky="w")
        self.source_channel_box = ttk.Combobox(data_tab, textvariable=self.source_channel_var,
                                               values=["CH1"], state="readonly", width=19)
        self.source_channel_box.grid(row=row, column=1, sticky="ew"); row += 1
        self.source_channel_box.bind("<<ComboboxSelected>>", lambda _event: self.channel_changed())
        row = self.section(data_tab, row, "Valid Data Range (min)")
        row = self.entry(data_tab, row, "Start", "range_start"); row = self.entry(data_tab, row, "End", "range_end")
        row = self.section(data_tab, row, "Offsets & Fitting Baselines")
        self.wavelength_frames: dict[str, Any] = {}
        for wavelength in WAVELENGTHS:
            frame = ttk.Frame(data_tab)
            frame.grid(row=row, column=0, columnspan=2, sticky="ew", pady=2); row += 1
            frame.columnconfigure(1, weight=1)
            ttk.Label(frame, text=f"{wavelength} Offset").grid(row=0, column=0, sticky="w")
            ttk.Entry(frame, textvariable=self.vars[f"offset{wavelength}"], width=12).grid(row=0, column=1, sticky="ew")
            ttk.Label(frame, text=f"{wavelength} Fitting Baseline").grid(row=1, column=0, sticky="w")
            ttk.Entry(frame, textvariable=self.vars[f"baseline{wavelength}"], width=12).grid(row=1, column=1, sticky="ew")
            ttk.Button(frame, text=f"Configure {wavelength} Fit...",
                       command=lambda value=wavelength: self.open_fit(value)).grid(row=2, column=0, sticky="ew", pady=2)
            ttk.Label(frame, textvariable=self.vars[f"fit{wavelength}_status"],
                      wraplength=180).grid(row=2, column=1, sticky="w")
            self.wavelength_frames[wavelength] = frame
            frame.grid_remove()
        row = self.section(data_tab, row, "Analysis Trace")
        ttk.Label(data_tab, text="Analysis Wavelength").grid(row=row, column=0, sticky="w")
        self.analysis_wavelength_box = ttk.Combobox(
            data_tab, textvariable=self.analysis_wavelength_var, values=["470"], state="readonly", width=19,
        )
        self.analysis_wavelength_box.grid(row=row, column=1, sticky="ew"); row += 1
        self.analysis_wavelength_box.bind("<<ComboboxSelected>>", lambda _event: self.analysis_selection_changed())
        ttk.Label(data_tab, text="Reference Wavelength").grid(row=row, column=0, sticky="w")
        self.reference_wavelength_box = ttk.Combobox(
            data_tab, textvariable=self.reference_wavelength_var, values=["None", "410"], state="readonly", width=19,
        )
        self.reference_wavelength_box.grid(row=row, column=1, sticky="ew"); row += 1
        self.reference_wavelength_box.bind("<<ComboboxSelected>>", lambda _event: self.analysis_selection_changed())
        ttk.Label(data_tab, text="Combine Channels").grid(row=row, column=0, sticky="w")
        self.combine_box = ttk.Combobox(data_tab, textvariable=self.vars["combine"], values=["ratio", "subtraction"], state="readonly", width=19)
        self.combine_box.grid(row=row, column=1, sticky="ew"); row += 1
        self.combine_box.bind("<<ComboboxSelected>>", lambda _event: self.combine_changed())
        ttk.Button(data_tab, text="Apply Correction", command=self.apply_processing, style="Accent.TButton").grid(row=row, column=0, columnspan=2, sticky="ew", pady=(8, 3)); row += 1
        row = self.section(data_tab, row, "Markers (Working Copy)")
        self.marker_list = tk.Listbox(data_tab, height=5, exportselection=False, relief="flat", highlightthickness=1)
        self.marker_list.grid(row=row, column=0, columnspan=2, sticky="nsew", pady=(2, 5)); row += 1
        row = self.entry(data_tab, row, "Time (min)", "marker_time"); row = self.entry(data_tab, row, "Name", "marker_name")
        marker_buttons = ttk.Frame(data_tab); marker_buttons.grid(row=row, column=0, columnspan=2, sticky="ew")
        for text, command in [("Add", self.add_marker), ("Delete", self.delete_marker), ("Restore Original", self.reset_markers)]:
            ttk.Button(marker_buttons, text=text, command=command).pack(side=tk.LEFT, fill=tk.X, expand=True, padx=1)
        data_tab.columnconfigure(1, weight=1)

        row = 0
        row = self.section(display_tab, row, "Trace Display")
        layer_row = ttk.Frame(display_tab); layer_row.grid(row=row, column=0, columnspan=2, sticky="w"); row += 1
        for text, value in [("Raw + Corrected", "overlay"), ("Raw Only", "raw_only"), ("Corrected Only", "corrected_only")]:
            ttk.Radiobutton(layer_row, text=text, variable=self.trace_view_var, value=value,
                            command=self.redraw_current).pack(side=tk.LEFT)
        ttk.Checkbutton(display_tab, text="Show Fitted Curves (red dashed)", variable=self.show_fit_var,
                        command=self.redraw_current).grid(row=row, column=0, columnspan=2, sticky="w"); row += 1
        row = self.section(display_tab, row, "Smoothing & Downsampling")
        ttk.Checkbutton(display_tab, text="Enable Smoothing", variable=self.smooth_enabled_var).grid(
            row=row, column=0, columnspan=2, sticky="w"); row += 1
        row = self.entry(display_tab, row, "Smoothing Window (s)", "smooth")
        ttk.Checkbutton(display_tab, text="Enable Downsampling", variable=self.downsample_enabled_var).grid(
            row=row, column=0, columnspan=2, sticky="w"); row += 1
        ttk.Label(display_tab, text="Downsampling Unit").grid(row=row, column=0, sticky="w")
        ttk.Combobox(display_tab, textvariable=self.downsample_mode_var, values=["points", "seconds"],
                     state="readonly", width=10).grid(row=row, column=1, sticky="ew"); row += 1
        row = self.entry(display_tab, row, "Interval / Step", "downsample_value")
        ttk.Button(display_tab, text="Apply Display Processing", command=self.display_settings_changed).grid(
            row=row, column=0, columnspan=2, sticky="ew", pady=(2, 4)); row += 1
        row = self.section(display_tab, row, "Normalization Baseline")
        baseline_modes = ttk.Frame(display_tab); baseline_modes.grid(row=row, column=0, columnspan=2, sticky="w"); row += 1
        ttk.Radiobutton(baseline_modes, text="Manual", variable=self.norm_mode_var,
                        value="manual").pack(side=tk.LEFT)
        ttk.Radiobutton(baseline_modes, text="Pre-Marker Interval", variable=self.norm_mode_var,
                        value="marker_before").pack(side=tk.LEFT)
        row = self.entry(display_tab, row, "Baseline Start (original min)", "norm_start")
        row = self.entry(display_tab, row, "Baseline End (original min)", "norm_end")
        ttk.Label(display_tab, text="Baseline Marker").grid(row=row, column=0, sticky="w")
        self.norm_marker_box = ttk.Combobox(display_tab, textvariable=self.norm_marker_var,
                                            state="readonly", width=20)
        self.norm_marker_box.grid(row=row, column=1, sticky="ew"); row += 1
        row = self.entry(display_tab, row, "Pre-Marker Duration (min)", "norm_pre_duration")
        row = self.section(display_tab, row, "Relative Time (Optional)")
        ttk.Checkbutton(display_tab, text="Enable Relative Time", variable=self.zero_enabled_var).grid(
            row=row, column=0, columnspan=2, sticky="w"); row += 1
        zero_modes = ttk.Frame(display_tab); zero_modes.grid(row=row, column=0, columnspan=2, sticky="w"); row += 1
        ttk.Radiobutton(zero_modes, text="Use Marker as Time 0", variable=self.zero_mode_var, value="marker").pack(side=tk.LEFT)
        ttk.Radiobutton(zero_modes, text="Use Specified Time as 0", variable=self.zero_mode_var, value="time").pack(side=tk.LEFT)
        ttk.Label(display_tab, text="Zero Marker").grid(row=row, column=0, sticky="w")
        self.zero_marker_box = ttk.Combobox(display_tab, textvariable=self.zero_marker_var,
                                            state="readonly", width=20)
        self.zero_marker_box.grid(row=row, column=1, sticky="ew"); row += 1
        row = self.entry(display_tab, row, "Specified Time (original min)", "zero_time")
        ttk.Button(display_tab, text="Calculate dF/F0 & Z-score", command=self.calculate_normalization,
                   style="Accent.TButton").grid(row=row, column=0, columnspan=2, sticky="ew", pady=(5, 2)); row += 1
        ttk.Label(display_tab, text="Drag the bottom X-axis to pan horizontally. Drag a plot's left Y-axis to pan vertically. Box zoom works inside plots.",
                   wraplength=380, style="Hint.TLabel").grid(row=row, column=0, columnspan=2, sticky="w", pady=(8, 0))
        display_tab.columnconfigure(1, weight=1)

        row = 0
        row = self.section(event_tab, row, "Marker-Aligned Trial Analysis")
        ttk.Label(event_tab, text="Signals to Analyze").grid(row=row, column=0, sticky="nw")
        event_signal_frame = ttk.Frame(event_tab)
        event_signal_frame.grid(row=row, column=1, sticky="nsew"); row += 1
        event_signal_frame.columnconfigure(0, weight=1)
        event_signal_frame.rowconfigure(0, weight=1)
        self.event_signal_list = tk.Listbox(
            event_signal_frame, height=4, selectmode=tk.EXTENDED, exportselection=False,
            relief="flat", highlightthickness=1,
        )
        self.event_signal_list.grid(row=0, column=0, sticky="nsew")
        event_signal_scroll = ttk.Scrollbar(
            event_signal_frame, orient=tk.VERTICAL, command=self.event_signal_list.yview,
        )
        event_signal_scroll.grid(row=0, column=1, sticky="ns")
        self.event_signal_list.configure(yscrollcommand=event_signal_scroll.set)
        self.event_signal_list.bind("<<ListboxSelect>>", self.event_signal_selection_changed)
        event_signal_buttons = ttk.Frame(event_tab)
        event_signal_buttons.grid(row=row, column=0, columnspan=2, sticky="ew", pady=(2, 1)); row += 1
        ttk.Button(event_signal_buttons, text="Select All Signals",
                   command=self.select_all_event_signals).pack(
            side=tk.LEFT, fill=tk.X, expand=True, padx=(0, 1),
        )
        ttk.Button(event_signal_buttons, text="Clear Signals",
                   command=self.clear_event_signal_selection).pack(
            side=tk.LEFT, fill=tk.X, expand=True, padx=(1, 0),
        )
        ttk.Label(event_tab, textvariable=self.event_signal_status_var,
                  wraplength=380, style="Hint.TLabel").grid(
            row=row, column=0, columnspan=2, sticky="w", pady=(0, 5),
        ); row += 1
        ttk.Label(event_tab, text="Marker Name").grid(row=row, column=0, sticky="w")
        self.event_marker_box = ttk.Combobox(
            event_tab, textvariable=self.event_marker_var, state="readonly", width=20,
        )
        self.event_marker_box.grid(row=row, column=1, sticky="ew"); row += 1
        self.event_marker_box.bind("<<ComboboxSelected>>", self.event_marker_changed)
        ttk.Label(event_tab, text="Markers to Analyze").grid(row=row, column=0, sticky="nw")
        event_marker_frame = ttk.Frame(event_tab)
        event_marker_frame.grid(row=row, column=1, sticky="nsew"); row += 1
        event_marker_frame.columnconfigure(0, weight=1)
        event_marker_frame.rowconfigure(0, weight=1)
        self.event_marker_list = tk.Listbox(
            event_marker_frame, height=7, selectmode=tk.EXTENDED, exportselection=False,
            relief="flat", highlightthickness=1,
        )
        self.event_marker_list.grid(row=0, column=0, sticky="nsew")
        event_marker_scroll = ttk.Scrollbar(
            event_marker_frame, orient=tk.VERTICAL, command=self.event_marker_list.yview,
        )
        event_marker_scroll.grid(row=0, column=1, sticky="ns")
        self.event_marker_list.configure(yscrollcommand=event_marker_scroll.set)
        self.event_marker_list.bind("<<ListboxSelect>>", self.event_marker_selection_changed)
        event_marker_buttons = ttk.Frame(event_tab)
        event_marker_buttons.grid(row=row, column=0, columnspan=2, sticky="ew", pady=(2, 5)); row += 1
        ttk.Button(event_marker_buttons, text="Select All", command=self.select_all_event_markers).pack(
            side=tk.LEFT, fill=tk.X, expand=True, padx=(0, 1),
        )
        ttk.Button(event_marker_buttons, text="Clear Selection", command=self.clear_event_marker_selection).pack(
            side=tk.LEFT, fill=tk.X, expand=True, padx=(1, 0),
        )
        row = self.entry(event_tab, row, "Trace Before Marker (s)", "event_pre_seconds")
        row = self.entry(event_tab, row, "Trace After Marker (s)", "event_post_seconds")
        row = self.entry(event_tab, row, "Baseline Before Marker (s)", "event_baseline_seconds")
        ttk.Label(
            event_tab,
            text="Select any number of markers with Ctrl/Shift-click (all are selected by default). "
                 "Each selected trial is normalized using its own interval immediately before time 0.",
            wraplength=380, style="Hint.TLabel",
        ).grid(row=row, column=0, columnspan=2, sticky="w", pady=(5, 8)); row += 1
        ttk.Button(
            event_tab, text="Calculate Event dF/F0 & Z-score",
            command=self.calculate_event_analysis, style="Accent.TButton",
        ).grid(row=row, column=0, columnspan=2, sticky="ew", pady=(3, 5)); row += 1
        ttk.Label(
            event_tab, textvariable=self.vars["event_status"], wraplength=380, style="Status.TLabel",
        ).grid(row=row, column=0, columnspan=2, sticky="w", pady=(4, 0))
        event_tab.columnconfigure(1, weight=1)

        row = 0
        row = self.section(export_tab, row, "Output Files")
        export_labels = [("corrected_csv", "Corrected Fluorescence CSV"), ("corrected_png", "Corrected Fluorescence PNG"),
                         ("dff_csv", "dF/F0 CSV"), ("dff_png", "dF/F0 PNG"),
                         ("dff_svg", "dF/F0 SVG (Vector)"), ("zscore_csv", "Z-score CSV"),
                         ("zscore_png", "Z-score PNG"), ("zscore_svg", "Z-score SVG (Vector)"),
                         ("event_csv", "Event Trials + Average CSV"),
                         ("event_png", "Event Trials + Average PNG"),
                         ("event_svg", "Event Trials + Average SVG")]
        for index, (key, label) in enumerate(export_labels):
            ttk.Checkbutton(export_tab, text=label, variable=self.export_vars[key]).grid(
                row=row + index // 2, column=index % 2, sticky="w", pady=2
            )
        row += (len(export_labels) + 1) // 2
        row = self.section(export_tab, row, "Trace Annotation")
        annotation_modes = ttk.Frame(export_tab); annotation_modes.grid(row=row, column=0, columnspan=2, sticky="w"); row += 1
        for text, value in [("None", "none"), ("Normalization Baseline", "baseline"), ("After Marker Window", "marker_after")]:
            ttk.Radiobutton(annotation_modes, text=text, variable=self.export_annotation_var, value=value).pack(side=tk.LEFT)
        ttk.Label(export_tab, text="Annotation Marker").grid(row=row, column=0, sticky="w")
        self.export_marker_box = ttk.Combobox(export_tab, textvariable=self.export_marker_var, state="readonly", width=20)
        self.export_marker_box.grid(row=row, column=1, sticky="ew"); row += 1
        row = self.entry(export_tab, row, "Marker Window (min)", "export_window_duration")
        ttk.Label(export_tab, text="The selected region is shaded in PNG files and recorded as export_window in CSV files.", wraplength=380, style="Hint.TLabel").grid(row=row, column=0, columnspan=2, sticky="w"); row += 1
        row = self.entry(export_tab, row, "Output Folder Name", "output_name")
        ttk.Label(export_tab, text="The output folder is created inside the input folder. A number is appended if the name already exists.",
                  wraplength=380, style="Hint.TLabel").grid(row=row, column=0, columnspan=2, sticky="w", pady=(0, 5)); row += 1
        ttk.Button(export_tab, text="Export Selected Results", command=self.save_results, style="Accent.TButton").grid(row=row, column=0, columnspan=2, sticky="ew", pady=(8, 4)); row += 1
        ttk.Label(export_tab, textvariable=self.vars["status"], wraplength=380, style="Status.TLabel").grid(row=row, column=0, columnspan=2, sticky="w", pady=(4, 0))
        export_tab.columnconfigure(0, weight=1); export_tab.columnconfigure(1, weight=1)

        self.figure = Figure(figsize=(11, 8), dpi=100, constrained_layout=True)
        toolbar_frame = ttk.Frame(plot_frame); toolbar_frame.pack(fill=tk.X)
        plot_body = ttk.Frame(plot_frame); plot_body.pack(fill=tk.BOTH, expand=True)
        self.scale_panel = ttk.Frame(plot_body, width=105, padding=(5, 0))
        self.scale_panel.pack(side=tk.RIGHT, fill=tk.Y)
        self.canvas = FigureCanvasTkAgg(self.figure, master=plot_body)
        self.toolbar = NavigationToolbar2Tk(self.canvas, toolbar_frame, pack_toolbar=False)
        self.toolbar.update(); self.toolbar.pack(fill=tk.X)
        self.canvas.get_tk_widget().pack(side=tk.LEFT, fill=tk.BOTH, expand=True)
        plot_footer = ttk.Frame(plot_frame, padding=(8, 5)); plot_footer.pack(fill=tk.X)
        width_row = ttk.Frame(plot_footer); width_row.pack(fill=tk.X, pady=(0, 3))
        axis_row = ttk.Frame(plot_footer); axis_row.pack(fill=tk.X)
        ttk.Label(width_row, text="Trace").pack(side=tk.LEFT)
        self.line_width_box = ttk.Combobox(
            width_row, textvariable=self.line_width_selector_var,
            values=list(LINE_WIDTH_LABELS.values()), state="readonly", width=18,
        )
        self.line_width_box.pack(side=tk.LEFT, padx=(5, 4))
        self.line_width_box.bind("<<ComboboxSelected>>", self.line_width_selection_changed)
        ttk.Label(width_row, text="Line Width").pack(side=tk.LEFT)
        self.line_width_scale = ttk.Scale(width_row, from_=0.4, to=3.0,
                                          variable=self.line_width_vars["raw470"],
                                          command=self.line_width_changed, length=180)
        self.line_width_scale.pack(side=tk.LEFT, padx=(6, 4))
        ttk.Label(width_row, textvariable=self.line_width_text, width=4).pack(side=tk.LEFT)
        ttk.Label(axis_row, textvariable=self.x_left_label_var).pack(side=tk.LEFT)
        x_start_entry = ttk.Entry(axis_row, textvariable=self.vars["x_start"], width=9)
        x_start_entry.pack(side=tk.LEFT, padx=5)
        ttk.Label(axis_row, textvariable=self.x_range_label_var).pack(side=tk.LEFT)
        x_span_entry = ttk.Entry(axis_row, textvariable=self.vars["x_span"], width=9)
        x_span_entry.pack(side=tk.LEFT, padx=5)
        ttk.Label(axis_row, textvariable=self.x_right_label_var).pack(side=tk.LEFT)
        x_end_entry = ttk.Entry(axis_row, textvariable=self.vars["x_end"], width=9)
        x_end_entry.pack(side=tk.LEFT, padx=5)
        for entry, changed in ((x_start_entry, "start"), (x_span_entry, "span"), (x_end_entry, "end")):
            entry.bind("<Return>", lambda _event, field=changed: self.update_x_from_fields(field, True))
            entry.bind("<FocusOut>", lambda _event, field=changed: self.update_x_from_fields(field, False))
        ttk.Button(axis_row, text="Narrow X Range", command=lambda: self.scale_x_view(0.8)).pack(side=tk.LEFT, padx=(10, 2))
        ttk.Button(axis_row, text="Widen X Range", command=lambda: self.scale_x_view(1.25)).pack(side=tk.LEFT, padx=2)
        self.canvas.mpl_connect("button_press_event", self.axis_drag_press)
        self.canvas.mpl_connect("motion_notify_event", self.axis_drag_motion)
        self.canvas.mpl_connect("button_release_event", self.axis_drag_release)
        self.canvas.mpl_connect("motion_notify_event", self.hover_motion)
        self.canvas.mpl_connect("figure_leave_event", self.axis_drag_release)
        self.canvas.mpl_connect("figure_leave_event", self.hover_leave)
        self.canvas.mpl_connect("draw_event", self.cache_hover_background)
        self.rebuild_axes()

    def section(self, parent: Any, row: int, text: str) -> int:
        self.ttk.Separator(parent).grid(row=row, column=0, columnspan=2, sticky="ew", pady=(8, 4)); row += 1
        self.ttk.Label(parent, text=text, style="Section.TLabel").grid(
            row=row, column=0, columnspan=2, sticky="w", pady=(0, 3)
        )
        return row + 1

    def entry(self, parent: Any, row: int, label: str, key: str) -> int:
        self.ttk.Label(parent, text=label).grid(row=row, column=0, sticky="w", padx=(0, 8), pady=2)
        self.ttk.Entry(parent, textvariable=self.vars[key], width=17).grid(
            row=row, column=1, sticky="ew", pady=2
        )
        return row + 1

    def number(self, key: str) -> float:
        try:
            value = float(self.vars[key].get())
        except ValueError as exc:
            raise ValueError(f"Enter a value for {key}.") from exc
        if not math.isfinite(value):
            raise ValueError(f"{key} must be a finite number.")
        return value

    def set_x_axis_unit(self, unit: str) -> None:
        self.x_left_label_var.set(f"X Left ({unit})")
        self.x_range_label_var.set(f"X Range ({unit})")
        self.x_right_label_var.set(f"X Right ({unit})")

    def reset_parameters_for_new_recording(self) -> None:
        """Restore every analysis, display and export option to its startup default."""
        for key, value in DEFAULT_STRINGS.items():
            self.vars[key].set(value)
        self.trace_view_var.set("overlay")
        self.show_fit_var.set(True)
        self.box_drag_active = False
        self.smooth_enabled_var.set(True)
        self.downsample_enabled_var.set(True)
        self.downsample_mode_var.set("seconds")
        self.applied_smooth_seconds = 0.0
        self.applied_downsample_enabled = False
        self.applied_downsample_mode = "seconds"
        self.applied_downsample_value = 1.0
        self.norm_mode_var.set("manual")
        self.norm_marker_var.set("")
        self.zero_enabled_var.set(True)
        self.zero_mode_var.set("marker")
        self.zero_marker_var.set("")
        self.source_channel_var.set("CH1")
        self.analysis_wavelength_var.set("470")
        self.reference_wavelength_var.set("410")
        self.event_marker_var.set("")
        self.event_marker_candidates = []
        self.event_marker_list_name = None
        if hasattr(self, "event_marker_list"):
            self.event_marker_list.delete(0, self.tk.END)
        self.event_signal_candidates = []
        self.event_signal_status_var.set("Apply correction, then select event signals.")
        if hasattr(self, "event_signal_list"):
            self.event_signal_list.delete(0, self.tk.END)
        self.export_annotation_var.set("none")
        self.export_marker_var.set("")
        export_defaults = {
            "corrected_csv": False, "corrected_png": True,
            "dff_csv": True, "dff_png": True, "dff_svg": False,
            "zscore_csv": True, "zscore_png": True, "zscore_svg": False,
            "event_csv": False, "event_png": False, "event_svg": False,
        }
        for key, value in export_defaults.items():
            self.export_vars[key].set(value)
        for variable in self.line_width_vars.values():
            variable.set(1.0)
        self.line_width_selector_var.set(LINE_WIDTH_LABELS["raw470"])
        self.line_width_scale.configure(variable=self.line_width_vars["raw470"])
        self.line_width_text.set("1.0")
        self.axis_scale_vars.clear()
        self.axis_lower_vars.clear()
        self.axis_upper_vars.clear()
        self.initial_x_limits = None
        self.initial_y_limits = {}
        self.axis_drag_state = None
        self.fit_regions = {wavelength: [] for wavelength in WAVELENGTHS}
        self.fit_signatures = {wavelength: None for wavelength in WAVELENGTHS}
        self.fit_models = {wavelength: None for wavelength in WAVELENGTHS}
        self.fit_baseline_modes = {wavelength: "fit_constant" for wavelength in WAVELENGTHS}
        self.recording_channels = {}
        self.available_wavelengths = []
        self.active_source_channel = "CH1"
        self.channel_ranges = {}
        self.processed = None
        self.details = None
        self.normalized = False
        self.event_trials = None
        self.event_average = None
        self.event_details = None
        self.event_view_active = False
        self.display_cache.clear()

    def clear_event_analysis(self, message: str | None = None) -> None:
        """Discard event results when their corrected source signal or marker set changes."""
        was_active = self.event_view_active
        self.event_trials = None
        self.event_average = None
        self.event_details = None
        self.event_view_active = False
        if was_active and hasattr(self, "figure"):
            self.rebuild_axes()
        if message is not None:
            self.vars["event_status"].set(message)

    def current_config(self) -> ProcessingConfig:
        smooth_seconds = self.smoothing_seconds()
        wavelengths = list(self.available_wavelengths)
        if not wavelengths:
            raise ValueError("Load a recording containing 410, 470, or 560 data first.")
        analysis = self.analysis_wavelength_var.get()
        reference_text = self.reference_wavelength_var.get()
        reference = None if reference_text in {"", "None"} else reference_text
        config = ProcessingConfig(
            range_start_min=self.number("range_start"),
            range_end_min=self.number("range_end"),
            offsets={value: self.number(f"offset{value}") for value in wavelengths},
            baselines={value: self.number(f"baseline{value}") for value in wavelengths},
            wavelengths=wavelengths,
            analysis_wavelength=analysis,
            reference_wavelength=reference,
            combine=self.vars["combine"].get(),
            fit_model=self.vars["fit_model"].get(),
            smooth_seconds=smooth_seconds,
            fit_regions={value: list(self.fit_regions[value]) for value in wavelengths},
            fit_models={value: self.fit_models[value] for value in wavelengths},
            fit_baseline_modes={value: self.fit_baseline_modes[value] for value in wavelengths},
            source_channel=self.source_channel_var.get(),
        )
        if config.range_end_min <= config.range_start_min:
            raise ValueError("The valid range end must be greater than the start.")
        if config.smooth_seconds < 0:
            raise ValueError("The smoothing duration cannot be negative.")
        return config

    def pending_smoothing_seconds(self) -> float:
        value = self.number("smooth") if self.smooth_enabled_var.get() else 0.0
        if value < 0:
            raise ValueError("The smoothing window cannot be negative.")
        return value

    def smoothing_seconds(self) -> float:
        return float(self.applied_smooth_seconds)

    def pending_downsample_settings(self) -> tuple[bool, str, float]:
        enabled = bool(self.downsample_enabled_var.get())
        mode = self.downsample_mode_var.get()
        if mode not in {"points", "seconds"}:
            raise ValueError("Invalid downsampling unit.")
        value = self.number("downsample_value") if enabled else 1.0
        if value <= 0:
            raise ValueError("The downsampling interval must be greater than 0.")
        return enabled, mode, float(value)

    def smooth_array(self, values: np.ndarray) -> np.ndarray:
        array = np.asarray(values, float)
        seconds = self.smoothing_seconds()
        if seconds <= 0:
            return array
        if self.details is not None:
            rate = float(self.details["sample_rate_hz"])
        elif self.data is not None:
            time_s = self.data["TimeStamp"].to_numpy(float) / 1000
            differences = np.diff(time_s)
            rate = float(1 / np.median(differences[differences > 0]))
        else:
            return array
        points = max(1, int(round(seconds * rate)))
        return pd.Series(array).rolling(points, center=True, min_periods=1).mean().to_numpy()

    def display_indices(self, length: int) -> np.ndarray:
        if length <= 0:
            return np.array([], dtype=int)
        stride = 1
        if self.applied_downsample_enabled:
            value = float(self.applied_downsample_value)
            if self.applied_downsample_mode == "points":
                stride = max(1, int(round(value)))
            else:
                if self.details is not None:
                    rate = float(self.details["sample_rate_hz"])
                elif self.data is not None:
                    time_s = self.data["TimeStamp"].to_numpy(float) / 1000
                    differences = np.diff(time_s)
                    rate = float(1 / np.median(differences[differences > 0]))
                else:
                    rate = 1.0
                stride = max(1, int(round(value * rate)))
        indices = np.arange(0, length, stride, dtype=int)
        if indices[-1] != length - 1:
            indices = np.append(indices, length - 1)
        return indices

    def display_values(self, column: str) -> np.ndarray:
        if self.processed is None:
            return np.array([], dtype=float)
        seconds = self.smoothing_seconds()
        key = (column, seconds)
        if key not in self.display_cache:
            self.display_cache[key] = self.smooth_array(self.processed[column].to_numpy(float))
        return self.display_cache[key]

    def display_settings_changed(self) -> None:
        from tkinter import messagebox
        try:
            smooth_seconds = self.pending_smoothing_seconds()
            downsample_enabled, downsample_mode, downsample_value = self.pending_downsample_settings()
            self.applied_smooth_seconds = smooth_seconds
            self.applied_downsample_enabled = downsample_enabled
            self.applied_downsample_mode = downsample_mode
            self.applied_downsample_value = downsample_value
            self.display_indices(len(self.processed) if self.processed is not None else
                                 (len(self.data) if self.data is not None else 1))
            self.display_cache.clear()
            if self.processed is not None:
                pairs = [("combined_signal", "combined_signal_smoothed"),
                         ("analysis_trace", "analysis_trace_smoothed"),
                         ("dff_percent", "dff_percent_smoothed"),
                         ("zscore", "zscore_smoothed")]
                for wavelength in WAVELENGTHS:
                    pairs.extend([
                        (f"corrected_{wavelength}", f"corrected_{wavelength}_smoothed"),
                        (f"dff_{wavelength}_percent", f"dff_{wavelength}_percent_smoothed"),
                        (f"zscore_{wavelength}", f"zscore_{wavelength}_smoothed"),
                    ])
                for raw, smoothed in pairs:
                    if raw in self.processed:
                        self.processed[smoothed] = self.smooth_array(self.processed[raw].to_numpy(float))
                if self.normalized and self.details and self.details.get("normalization"):
                    self.details["normalization"]["smooth_seconds"] = self.smoothing_seconds()
            self.clear_event_analysis(
                "Display processing changed. Recalculate the event analysis."
            )
            self.redraw_current()
            downsample_text = (f"{downsample_value:g} {downsample_mode}"
                               if downsample_enabled else "Off")
            self.vars["status"].set(
                f"Display processing applied: Smoothing={smooth_seconds:g}s, Downsampling={downsample_text}."
            )
        except Exception as exc:
            messagebox.showerror("Invalid Display Processing Settings", str(exc))

    def expected_signature(self, channel: str, config: ProcessingConfig) -> tuple[Any, ...]:
        offset = config.offsets[channel]
        baseline = config.baselines[channel]
        model = self.fit_models[channel] or config.fit_model
        baseline_mode = self.fit_baseline_modes[channel] or "fit_constant"
        return (config.range_start_min, config.range_end_min, offset, baseline, model, baseline_mode)

    def open_folder(self) -> None:
        from tkinter import filedialog, messagebox
        selected = filedialog.askdirectory(title="Select the folder containing Fluorescence.csv")
        if not selected:
            return
        try:
            new_folder = Path(selected)
            data, markers, metadata = load_recording(new_folder)
            self.reset_parameters_for_new_recording()
            self.folder = new_folder
            self.data, self.metadata = data, metadata
            self.recording_channels = available_recording_channels(data)
            channels = list(self.recording_channels)
            self.source_channel_box.configure(values=channels)
            self.source_channel_var.set(channels[0])
            self.active_source_channel = channels[0]
            self.configure_available_wavelengths(channels[0])
            self.original_markers = [dict(marker) for marker in markers]
            self.markers = [dict(marker) for marker in markers]
            self.vars["folder"].set(str(self.folder))
            self.vars["range_start"].set("0")
            recording_end = float(self.data["TimeStamp"].iloc[-1]) / 60000
            self.channel_ranges = {channel: (0.0, recording_end) for channel in channels}
            self.vars["range_end"].set(f"{recording_end:.5f}")
            self.vars["norm_start"].set("0")
            self.vars["norm_end"].set(f"{min(1.0, recording_end):.5f}")
            self.vars["output_name"].set(f"output_{self.folder.name}_{self.source_channel_var.get()}")
            self.refresh_markers(); self.analysis_selection_changed()
            self.vars["status"].set(
                f"Data loaded with wavelengths {', '.join(self.available_wavelengths)}. "
                "Enter each visible baseline and set its fitting regions."
            )
        except Exception as exc:
            messagebox.showerror("Load Failed", str(exc))

    def configure_available_wavelengths(self, source: str) -> None:
        self.available_wavelengths = list(self.recording_channels.get(source, []))
        for wavelength, frame in self.wavelength_frames.items():
            if wavelength in self.available_wavelengths:
                frame.grid()
            else:
                frame.grid_remove()
        preferred = next((value for value in ("560", "470", "410")
                          if value in self.available_wavelengths), self.available_wavelengths[0])
        current_analysis = self.analysis_wavelength_var.get()
        analysis = current_analysis if current_analysis in self.available_wavelengths else preferred
        self.analysis_wavelength_box.configure(values=self.available_wavelengths)
        self.analysis_wavelength_var.set(analysis)
        reference_values = ["None", *(value for value in self.available_wavelengths if value != analysis)]
        current_reference = self.reference_wavelength_var.get()
        if current_reference not in reference_values:
            current_reference = "410" if "410" in reference_values else "None"
        self.reference_wavelength_box.configure(values=reference_values)
        self.reference_wavelength_var.set(current_reference)
        self.combine_box.configure(state="readonly" if current_reference != "None" else "disabled")
        available_width_labels = [
            label for key, label in LINE_WIDTH_LABELS.items()
            if not re.search(r"(410|470|560)$", key) or key[-3:] in self.available_wavelengths
        ]
        self.line_width_box.configure(values=available_width_labels)
        if self.line_width_selector_var.get() not in available_width_labels:
            first = f"Raw {analysis}"
            self.line_width_selector_var.set(first)
            self.line_width_selection_changed()
        self.refresh_event_signal_list(prefer_default=True)

    def analysis_selection_changed(self) -> None:
        analysis = self.analysis_wavelength_var.get()
        if self.available_wavelengths and analysis not in self.available_wavelengths:
            return
        references = ["None", *(value for value in self.available_wavelengths if value != analysis)]
        self.reference_wavelength_box.configure(values=references)
        if self.reference_wavelength_var.get() not in references:
            self.reference_wavelength_var.set("410" if "410" in references else "None")
        has_reference = self.reference_wavelength_var.get() != "None"
        self.combine_box.configure(state="readonly" if has_reference else "disabled")
        self.refresh_event_signal_list(prefer_default=True)
        if self.event_trials is not None:
            self.clear_event_analysis("Analysis wavelength changed. Recalculate the event analysis.")
        if self.processed is not None:
            self.processed = None
            self.details = None
            self.normalized = False
            self.display_cache.clear()
        self.rebuild_axes();
        if self.data is not None:
            self.draw_raw_preview()

    def combine_changed(self) -> None:
        """Refresh the raw preview or corrected combination when its operation changes."""
        primary = self.analysis_wavelength_var.get()
        reference = self.reference_wavelength_var.get()
        if reference in {"", "None"}:
            return
        method = self.vars["combine"].get()
        if self.processed is None:
            self.rebuild_axes()
            if self.data is not None:
                self.draw_raw_preview()
                self.vars["status"].set(
                    f"Showing the direct raw {primary} {'/' if method == 'ratio' else '-'} {reference} result."
                )
            return

        from tkinter import messagebox
        previous_method = (
            "ratio" if "/" in str((self.details or {}).get("combined_label", "")) else "subtraction"
        )
        corrected_primary = self.processed[f"corrected_{primary}"].to_numpy(float)
        corrected_reference = self.processed[f"corrected_{reference}"].to_numpy(float)
        if method == "ratio":
            if np.any(np.isfinite(corrected_reference) & np.isclose(corrected_reference, 0)):
                self.vars["combine"].set(previous_method)
                messagebox.showerror(
                    "Cannot Calculate Ratio",
                    f"The corrected {reference} trace contains zero, so the ratio cannot be calculated.",
                )
                return
            combined = corrected_primary / corrected_reference
            combined_label = f"Corrected {primary} / {reference} ratio"
            analysis_reference = self.number(f"baseline{primary}") / self.number(f"baseline{reference}")
        elif method == "subtraction":
            combined = corrected_primary - corrected_reference
            combined_label = f"Corrected {primary} - {reference}"
            analysis_reference = self.number(f"baseline{primary}") - self.number(f"baseline{reference}")
        else:
            self.vars["combine"].set(previous_method)
            return

        self.processed["combined_signal"] = combined
        self.processed["analysis_trace"] = combined
        self.processed["combined_signal_smoothed"] = self.smooth_array(combined)
        self.processed["analysis_trace_smoothed"] = self.smooth_array(combined)
        self.normalized = False
        self.clear_event_analysis("Channel combination changed. Recalculate the event analysis.")
        self.display_cache.clear()
        if self.details is not None:
            self.details["combined_label"] = combined_label
            self.details["analysis_reference_from_user_baselines"] = float(analysis_reference)
            self.details["normalization"] = None
        self.rebuild_axes()
        self.draw_processed()
        self.vars["status"].set(
            f"Channel combination changed to {method}. Recalculate dF/F0 and Z-score if needed."
        )

    def channel_changed(self) -> None:
        """Switch channel atomically and force a clean raw-data preview."""
        source = self.source_channel_box.get().strip() or self.source_channel_var.get().strip()
        if self.data is None or not re.fullmatch(r"CH\d+", source):
            return
        if source not in self.recording_channels:
            self.vars["status"].set(f"{source} has no supported 410, 470, or 560 data.")
            return
        # Preserve the range for the channel being left.  A channel that has
        # not been visited starts at the full recording range instead of
        # inheriting another channel's analysis interval.
        try:
            prior_start, prior_end = self.number("range_start"), self.number("range_end")
            if prior_end > prior_start:
                self.channel_ranges[self.active_source_channel] = (prior_start, prior_end)
        except ValueError:
            pass
        recording_end = float(self.data["TimeStamp"].iloc[-1]) / 60000
        start, end = self.channel_ranges.get(source, (0.0, recording_end))
        self.channel_ranges[source] = (start, end)
        self.vars["range_start"].set(f"{start:.5f}")
        self.vars["range_end"].set(f"{end:.5f}")
        self.active_source_channel = source
        # Store the selected value before any redraw.  This avoids a queued
        # canvas draw retaining CH1's processed artists during a CH2 switch.
        self.source_channel_var.set(source)
        self.configure_available_wavelengths(source)
        self.processed = None
        self.details = None
        self.normalized = False
        self.clear_event_analysis("Apply correction for this recording channel before event analysis.")
        self.display_cache.clear()
        self.fit_regions = {wavelength: [] for wavelength in WAVELENGTHS}
        self.fit_signatures = {wavelength: None for wavelength in WAVELENGTHS}
        self.fit_models = {wavelength: None for wavelength in WAVELENGTHS}
        self.fit_baseline_modes = {wavelength: "fit_constant" for wavelength in WAVELENGTHS}
        for wavelength in WAVELENGTHS:
            self.vars[f"fit{wavelength}_status"].set("Not set")
        if self.folder is not None:
            self.vars["output_name"].set(f"output_{self.folder.name}_{source}")
        self.rebuild_axes()
        self.draw_raw_preview(source)
        self.vars["status"].set(f"{source} selected. Showing full raw traces; set its baselines and fitting regions.")

    def open_fit(self, channel: str) -> None:
        from tkinter import messagebox
        if self.data is None:
            return
        try:
            config = self.current_config()
            FitWindow(self.root, channel, self.data, self.markers, config,
                      self.fit_regions[channel], self.accept_fit_regions)
        except Exception as exc:
            messagebox.showerror("Cannot Open Fitting Window", str(exc))

    def accept_fit_regions(
        self, channel: str, regions: list[tuple[float, float]], signature: tuple[Any, ...], model: str,
        baseline_mode: str,
    ) -> None:
        self.fit_regions[channel] = regions
        self.fit_signatures[channel] = signature
        self.fit_models[channel] = model
        self.fit_baseline_modes[channel] = baseline_mode
        self.vars[f"fit{channel}_status"].set(f"{len(regions)} region(s) | {model} | {baseline_mode}")
        self.vars["status"].set(f"{channel} fitting regions and model saved.")

    def apply_processing(self) -> None:
        from tkinter import messagebox
        if self.data is None:
            return
        try:
            config = self.current_config()
            for channel in config.wavelengths:
                if not self.fit_regions[channel]:
                    raise ValueError(f"Select and confirm fitting regions in the {channel} fitting window first.")
                if self.fit_signatures[channel] != self.expected_signature(channel, config):
                    raise ValueError(f"The {channel} range, offset, baseline, or fitting model has changed. Reconfirm this channel's fit.")
            self.vars["status"].set("Fitting..."); self.root.update_idletasks()
            self.processed, self.details = process_data(self.data, config)
            self.normalized = False
            self.clear_event_analysis("Correction updated. Select a marker name and calculate event analysis.")
            self.refresh_event_signal_list()
            self.display_cache.clear()
            self.rebuild_axes(); self.draw_processed()
            self.vars["status"].set(
                f"Correction complete: {len(self.processed):,} points. The current display remains unsmoothed and not downsampled. "
                "Apply display processing from the dF/F0 & Z-score tab if needed."
            )
        except Exception as exc:
            messagebox.showerror("Processing Failed", str(exc)); self.vars["status"].set(f"Processing failed: {exc}")

    def rebuild_axes(self) -> None:
        wavelengths = list(self.available_wavelengths) or [self.analysis_wavelength_var.get()]
        keys = list(wavelengths)
        has_reference = self.reference_wavelength_var.get() not in {"", "None"}
        if has_reference:
            keys.append(self.vars["combine"].get())
        if self.normalized:
            if has_reference or len(wavelengths) > 1:
                keys.extend([*(f"dff{value}" for value in wavelengths), "dff",
                             *(f"zscore{value}" for value in wavelengths), "zscore"])
            else:
                keys.extend(["dff", "zscore"])
        for selector in self.zoom_selectors:
            selector.set_active(False)
            selector.disconnect_events()
        self.zoom_selectors = []
        self.figure.clear()
        axes = self.figure.subplots(len(keys), 1, sharex=True, squeeze=False).ravel().tolist()
        self.axes, self.axis_keys = axes, keys
        self.refresh_axis_scale_controls()
        self.initialize_hover_artists()
        self.initialize_box_zoom()
        self.canvas.draw_idle()

    def refresh_axis_scale_controls(self) -> None:
        for child in self.scale_panel.winfo_children():
            child.destroy()
        for row in range(12):
            self.scale_panel.rowconfigure(row, weight=0)
        global_frame = self.ttk.Frame(self.scale_panel)
        global_frame.grid(row=0, column=0, sticky="ew", pady=(0, 5))
        self.ttk.Button(global_frame, text="Restore Initial View", command=self.restore_initial_view).pack(fill=self.tk.X, pady=1)
        self.ttk.Button(global_frame, text="Auto-Scale All Y", command=self.autoscale_current_window).pack(fill=self.tk.X, pady=1)
        for row, key in enumerate(self.axis_keys, start=1):
            self.scale_panel.rowconfigure(row, weight=1)
            frame = self.ttk.Frame(self.scale_panel)
            frame.grid(row=row, column=0, sticky="nsew", pady=3)
            self.ttk.Label(frame, text=key, justify="center").pack()
            lower_variable = self.axis_lower_vars.get(key)
            span_variable = self.axis_scale_vars.get(key)
            upper_variable = self.axis_upper_vars.get(key)
            if lower_variable is None:
                lower_variable = self.tk.StringVar(value="")
                self.axis_lower_vars[key] = lower_variable
            if span_variable is None:
                span_variable = self.tk.StringVar(value="")
                self.axis_scale_vars[key] = span_variable
            if upper_variable is None:
                upper_variable = self.tk.StringVar(value="")
                self.axis_upper_vars[key] = upper_variable
            self.ttk.Label(frame, text="Y Lower").pack()
            lower_entry = self.ttk.Entry(frame, textvariable=lower_variable, width=10, justify="center")
            lower_entry.pack(pady=(0, 2))
            self.ttk.Label(frame, text="Y Range").pack()
            span_entry = self.ttk.Entry(frame, textvariable=span_variable, width=10, justify="center")
            span_entry.pack(pady=(0, 2))
            self.ttk.Label(frame, text="Y Upper").pack()
            upper_entry = self.ttk.Entry(frame, textvariable=upper_variable, width=10, justify="center")
            upper_entry.pack(pady=(0, 2))
            for entry, changed in ((lower_entry, "lower"), (span_entry, "span"), (upper_entry, "upper")):
                entry.bind("<Return>", lambda _event, axis_key=key, field=changed:
                           self.update_y_from_fields(axis_key, field, True))
                entry.bind("<FocusOut>", lambda _event, axis_key=key, field=changed:
                           self.update_y_from_fields(axis_key, field, False))
            button_row = self.ttk.Frame(frame); button_row.pack()
            self.ttk.Button(button_row, text="Y−", width=3,
                            command=lambda axis_key=key: self.scale_y_axis(axis_key, 0.8)).pack(side=self.tk.LEFT, padx=1)
            self.ttk.Button(button_row, text="Y+", width=3,
                            command=lambda axis_key=key: self.scale_y_axis(axis_key, 1.25)).pack(side=self.tk.LEFT, padx=1)

    @staticmethod
    def optional_axis_value(variable: Any) -> float | None:
        text = variable.get().strip()
        if not text:
            return None
        value = float(text)
        if not math.isfinite(value):
            raise ValueError("Axis values must be finite numbers.")
        return value

    def update_y_from_fields(self, key: str, changed: str, show_error: bool = True) -> None:
        from tkinter import messagebox
        try:
            if key not in self.axis_keys:
                return
            lower = self.optional_axis_value(self.axis_lower_vars[key])
            span = self.optional_axis_value(self.axis_scale_vars[key])
            upper = self.optional_axis_value(self.axis_upper_vars[key])
            if changed == "lower" and lower is not None:
                if span is not None:
                    upper = lower + span
                elif upper is not None:
                    span = upper - lower
            elif changed == "span" and span is not None:
                if lower is not None:
                    upper = lower + span
                elif upper is not None:
                    lower = upper - span
            elif changed == "upper" and upper is not None:
                if lower is not None:
                    span = upper - lower
                elif span is not None:
                    lower = upper - span
            if None in (lower, span, upper):
                return
            if span <= 0 or upper <= lower:
                raise ValueError("The Y range must be greater than 0, and the upper limit must exceed the lower limit.")
            self.axis_lower_vars[key].set(f"{lower:.7g}")
            self.axis_scale_vars[key].set(f"{span:.7g}")
            self.axis_upper_vars[key].set(f"{upper:.7g}")
            ax = self.axes[self.axis_keys.index(key)]
            ax.set_ylim(lower, upper)
            self.update_scale_readouts()
            self.canvas.draw_idle()
        except Exception as exc:
            if show_error:
                messagebox.showerror("Invalid Y-Axis Range", str(exc))

    def update_x_from_fields(self, changed: str, show_error: bool = True) -> None:
        from tkinter import messagebox
        try:
            start = self.optional_axis_value(self.vars["x_start"])
            span = self.optional_axis_value(self.vars["x_span"])
            end = self.optional_axis_value(self.vars["x_end"])
            if changed == "start" and start is not None:
                if span is not None:
                    end = start + span
                elif end is not None:
                    span = end - start
            elif changed == "span" and span is not None:
                if start is not None:
                    end = start + span
                elif end is not None:
                    start = end - span
            elif changed == "end" and end is not None:
                if start is not None:
                    span = end - start
                elif span is not None:
                    start = end - span
            if None in (start, span, end):
                return
            if span <= 0 or end <= start:
                raise ValueError("The time range must be greater than 0, and the right limit must exceed the left limit.")
            self.vars["x_start"].set(f"{start:.7g}")
            self.vars["x_span"].set(f"{span:.7g}")
            self.vars["x_end"].set(f"{end:.7g}")
            for ax in self.axes:
                ax.set_xlim(start, end)
            self.update_scale_readouts()
            self.canvas.draw_idle()
        except Exception as exc:
            if show_error:
                messagebox.showerror("Invalid Time-Axis Range", str(exc))

    def scale_y_axis(self, key: str, factor: float) -> None:
        """Scale one Y range around its current center without changing the data."""
        if key not in self.axis_keys or factor <= 0:
            return
        ax = self.axes[self.axis_keys.index(key)]
        low, high = ax.get_ylim()
        center = (low + high) / 2
        half_range = max((high - low) * factor / 2, np.finfo(float).eps)
        ax.set_ylim(center - half_range, center + half_range)
        self.update_scale_readouts()
        self.canvas.draw_idle()

    def scale_x_view(self, factor: float) -> None:
        """Scale the shared X range around its center and stay inside the full trace view."""
        if not self.axes or factor <= 0:
            return
        low, high = self.axes[-1].get_xlim()
        center = (low + high) / 2
        target_span = max((high - low) * factor, np.finfo(float).eps)
        if self.initial_x_limits is not None:
            full_low, full_high = self.initial_x_limits
            full_span = full_high - full_low
            target_span = min(target_span, full_span)
            new_low, new_high = center - target_span / 2, center + target_span / 2
            if new_low < full_low:
                new_low, new_high = full_low, full_low + target_span
            if new_high > full_high:
                new_low, new_high = full_high - target_span, full_high
        else:
            new_low, new_high = center - target_span / 2, center + target_span / 2
        for ax in self.axes:
            ax.set_xlim(new_low, new_high)
        self.update_scale_readouts()
        self.canvas.draw_idle()

    def capture_initial_view(self) -> None:
        if not self.axes:
            self.initial_x_limits = None
            self.initial_y_limits = {}
            return
        self.initial_x_limits = tuple(float(value) for value in self.axes[-1].get_xlim())
        self.initial_y_limits = {
            key: tuple(float(value) for value in ax.get_ylim())
            for ax, key in zip(self.axes, self.axis_keys)
        }

    def restore_initial_view(self) -> None:
        if not self.axes or self.initial_x_limits is None:
            return
        for ax in self.axes:
            ax.set_xlim(*self.initial_x_limits)
        for ax, key in zip(self.axes, self.axis_keys):
            if key in self.initial_y_limits:
                ax.set_ylim(*self.initial_y_limits[key])
        self.update_scale_readouts()
        self.vars["status"].set("Trace view restored to the full initial range of the current data.")
        self.canvas.draw_idle()

    def axis_drag_press(self, event: Any) -> None:
        """Start Spike2-like panning when the user grabs an axis outside the plot area."""
        if not self.axes or event.button != 1 or event.x is None or event.y is None:
            return
        if getattr(self.toolbar, "mode", ""):
            return
        if getattr(event, "inaxes", None) in self.axes:
            self.box_drag_active = True
            self.hover_leave()
            return
        bottom_ax = self.axes[-1]
        bottom_box = bottom_ax.bbox
        if (bottom_box.x0 <= event.x <= bottom_box.x1 and
                max(0.0, bottom_box.y0 - 58) <= event.y <= bottom_box.y0):
            self.hover_leave()
            self.axis_drag_state = {
                "kind": "x", "start_pixel": float(event.x),
                "limits": tuple(float(value) for value in bottom_ax.get_xlim()),
                "pixels": max(float(bottom_box.width), 1.0),
            }
            self.set_canvas_cursor("sb_h_double_arrow")
            return
        for ax, key in zip(self.axes, self.axis_keys):
            box = ax.bbox
            if (max(0.0, box.x0 - 90) <= event.x <= box.x0 and
                    box.y0 <= event.y <= box.y1):
                self.hover_leave()
                self.axis_drag_state = {
                    "kind": "y", "key": key, "axis": ax, "start_pixel": float(event.y),
                    "limits": tuple(float(value) for value in ax.get_ylim()),
                    "pixels": max(float(box.height), 1.0),
                }
                self.set_canvas_cursor("sb_v_double_arrow")
                return

    def axis_drag_motion(self, event: Any) -> None:
        state = self.axis_drag_state
        if state is None:
            return
        if state["kind"] == "x" and event.x is not None:
            low, high = state["limits"]
            span = high - low
            shift = -(float(event.x) - state["start_pixel"]) / state["pixels"] * span
            new_low, new_high = low + shift, high + shift
            if self.initial_x_limits is not None:
                full_low, full_high = self.initial_x_limits
                if span >= full_high - full_low:
                    new_low, new_high = full_low, full_high
                else:
                    if new_low < full_low:
                        new_low, new_high = full_low, full_low + span
                    if new_high > full_high:
                        new_low, new_high = full_high - span, full_high
            for ax in self.axes:
                ax.set_xlim(new_low, new_high)
        elif state["kind"] == "y" and event.y is not None:
            low, high = state["limits"]
            span = high - low
            shift = -(float(event.y) - state["start_pixel"]) / state["pixels"] * span
            state["axis"].set_ylim(low + shift, high + shift)
        self.update_scale_readouts()
        self.canvas.draw_idle()

    def axis_drag_release(self, _event: Any = None) -> None:
        self.box_drag_active = False
        if self.axis_drag_state is None:
            return
        kind = self.axis_drag_state["kind"]
        key = self.axis_drag_state.get("key")
        self.axis_drag_state = None
        self.set_canvas_cursor("")
        self.vars["status"].set(
            "Time window panned using the X-axis." if kind == "x" else f"The {key} plot was panned vertically."
        )

    def set_canvas_cursor(self, cursor: str) -> None:
        try:
            self.canvas.get_tk_widget().configure(cursor=cursor)
        except Exception:
            pass

    def update_scale_readouts(self) -> None:
        for ax, key in zip(self.axes, self.axis_keys):
            low, high = ax.get_ylim()
            self.axis_lower_vars[key].set(f"{low:.7g}")
            self.axis_scale_vars[key].set(f"{high - low:.7g}")
            self.axis_upper_vars[key].set(f"{high:.7g}")
        if self.axes:
            low, high = self.axes[-1].get_xlim()
            self.vars["x_start"].set(f"{low:.7g}")
            self.vars["x_span"].set(f"{high - low:.7g}")
            self.vars["x_end"].set(f"{high:.7g}")

    def initialize_box_zoom(self) -> None:
        for selector in self.zoom_selectors:
            selector.set_active(False)
            selector.disconnect_events()
        self.zoom_selectors = []
        for ax in self.axes:
            selector = self.RectangleSelector(
                ax,
                lambda click, release, target=ax: self.apply_box_zoom(click, release, target),
                useblit=True,
                button=[1],
                minspanx=5,
                minspany=5,
                spancoords="pixels",
                interactive=False,
                props={"facecolor": "#5B7FA3", "edgecolor": "#34526C", "alpha": 0.18},
            )
            selector.set_active(True)
            self.zoom_selectors.append(selector)

    def apply_box_zoom(self, click: Any, release: Any, target_ax: Any) -> None:
        if None in (click.xdata, click.ydata, release.xdata, release.ydata):
            return
        x_low, x_high = sorted((float(click.xdata), float(release.xdata)))
        y_low, y_high = sorted((float(click.ydata), float(release.ydata)))
        if np.isclose(x_low, x_high) or np.isclose(y_low, y_high):
            return
        for ax in self.axes:
            ax.set_xlim(x_low, x_high)
        target_ax.set_ylim(y_low, y_high)
        self.update_scale_readouts()
        self.canvas.draw_idle()

    def draw_raw_preview(self, source: str | None = None) -> None:
        if self.data is None:
            return
        self.set_x_axis_unit("min")
        source = source or self.source_channel_var.get()
        try:
            start, end = self.number("range_start"), self.number("range_end")
        except Exception:
            return
        if end <= start:
            return
        t = self.data["TimeStamp"].to_numpy(float) / 60000
        use = (t >= start) & (t <= end)
        if not use.any():
            return
        subset = self.data.loc[use]; full_x = subset["TimeStamp"].to_numpy(float) / 60000
        x = full_x
        raw = {wavelength: subset[f"{source}-{wavelength}"].to_numpy(float)
               for wavelength in self.available_wavelengths}
        primary = self.analysis_wavelength_var.get()
        reference = self.reference_wavelength_var.get()
        for ax, key in zip(self.axes, self.axis_keys):
            ax.clear()
            if key in raw:
                color = WAVELENGTH_COLORS[key][0]
                ax.plot(x, raw[key], color=color, lw=self.line_width(f"raw{key}"),
                        gid=f"raw{key}"); ax.set_ylabel(f"Raw {source} {key}")
            elif key == "ratio":
                direct = np.full(len(raw[primary]), np.nan, dtype=float)
                valid = (np.isfinite(raw[primary]) & np.isfinite(raw[reference])
                         & ~np.isclose(raw[reference], 0))
                np.divide(raw[primary], raw[reference], out=direct, where=valid)
                ax.plot(x, direct, color="#7B4B94", lw=self.line_width("combined"),
                        gid="combined")
                ax.set_ylabel(f"Raw {primary} / {reference}")
            elif key == "subtraction":
                direct = raw[primary] - raw[reference]
                ax.plot(x, direct, color="#7B4B94", lw=self.line_width("combined"),
                        gid="combined")
                ax.set_ylabel(f"Raw {primary} - {reference}")
            else:
                ax.text(0.5, 0.5, "Apply fitting to display combined trace", ha="center", va="center", transform=ax.transAxes)
                ax.set_ylabel(key)
            self.style_axis(ax)
        self.axes[-1].set_xlabel("Recording time (min)"); self.axes[-1].set_xlim(start, end)
        self.draw_markers(); self.capture_initial_view()
        self.initialize_hover_artists(); self.initialize_box_zoom()
        self.update_scale_readouts(); self.canvas.draw_idle()

    def draw_processed(self) -> None:
        if self.processed is None:
            return
        self.set_x_axis_unit("min")
        d = self.processed; indices = self.display_indices(len(d))
        time_column = "relative_time_min" if self.normalized else "time_min"
        x = d[time_column].to_numpy(float)[indices]
        view = self.trace_view_var.get()
        for ax, key in zip(self.axes, self.axis_keys):
            ax.clear()
            if key in self.available_wavelengths:
                raw_color, corrected_color = WAVELENGTH_COLORS[key]
                if view != "corrected_only":
                    ax.plot(x, self.display_values(f"offset_adjusted_{key}")[indices], color=raw_color,
                            lw=self.line_width(f"raw{key}"), alpha=0.72,
                            label=f"Original {key} after offset", gid=f"raw{key}")
                if self.show_fit_var.get():
                    ax.plot(x, d[f"fit_{key}"].to_numpy()[indices], color="#D62728", ls="--",
                            lw=self.line_width(f"fit{key}"), label=f"{key} fit", gid=f"fit{key}")
                if view != "raw_only":
                    ax.plot(x, self.display_values(f"corrected_{key}")[indices], color=corrected_color,
                            lw=self.line_width(f"corrected{key}"), label=f"Corrected {key}",
                            gid=f"corrected{key}")
                ax.set_ylabel(f"{key} fluorescence")
                ax.legend(frameon=False, ncol=3, loc="lower right", bbox_to_anchor=(1, 1.01),
                          borderaxespad=0, fontsize=8)
            elif key == "ratio":
                ax.plot(x, self.display_values("combined_signal")[indices], color="#7B4B94",
                        lw=self.line_width("combined"), gid="combined")
                ax.set_ylabel(self.details.get("combined_label") or "Corrected ratio")
            elif key == "subtraction":
                ax.plot(x, self.display_values("combined_signal")[indices], color="#7B4B94",
                        lw=self.line_width("combined"), gid="combined")
                ax.set_ylabel(self.details.get("combined_label") or "Corrected subtraction")
            elif key == "dff":
                ax.plot(x, self.display_values("dff_percent")[indices], color="#7B4B94",
                        lw=self.line_width("dff"), gid="dff")
                ax.axhline(0, color="#777777", lw=0.7, alpha=0.7)
                ax.set_ylabel("dF/F0 (%)")
                self.shade_normalization_baseline(ax)
            elif key.startswith("dff") and key[3:] in self.available_wavelengths:
                wavelength = key[3:]
                ax.plot(x, self.display_values(f"dff_{wavelength}_percent")[indices],
                        color=WAVELENGTH_COLORS[wavelength][1], lw=self.line_width("dff"), gid="dff")
                ax.axhline(0, color="#777777", lw=0.7, alpha=0.7)
                ax.set_ylabel(f"{wavelength} dF/F0 (%)")
                self.shade_normalization_baseline(ax)
            elif key == "zscore":
                ax.plot(x, self.display_values("zscore")[indices], color="#7B4B94",
                        lw=self.line_width("zscore"), gid="zscore")
                ax.axhline(0, color="#777777", lw=0.7, alpha=0.7)
                ax.set_ylabel("Z-score")
                self.shade_normalization_baseline(ax)
            elif key.startswith("zscore") and key[6:] in self.available_wavelengths:
                wavelength = key[6:]
                ax.plot(x, self.display_values(f"zscore_{wavelength}")[indices],
                        color=WAVELENGTH_COLORS[wavelength][1], lw=self.line_width("zscore"), gid="zscore")
                ax.axhline(0, color="#777777", lw=0.7, alpha=0.7)
                ax.set_ylabel(f"{wavelength} Z-score")
                self.shade_normalization_baseline(ax)
            self.style_axis(ax)
        model_title = " | ".join(
            f"{wavelength}={self.fit_models[wavelength]}" for wavelength in self.available_wavelengths
        )
        self.figure.suptitle(f"RWD fiber pretreatment | {self.source_channel_var.get()} | {model_title}", fontsize=11)
        zero_time = self.details.get("normalization", {}).get("zero_time_min") if self.details and self.details.get("normalization") else None
        self.axes[-1].set_xlabel("Time relative to zero (min)" if zero_time is not None else "Recording time (min)")
        self.axes[-1].set_xlim(float(d[time_column].iloc[0]), float(d[time_column].iloc[-1]))
        self.draw_markers(); self.capture_initial_view()
        self.initialize_hover_artists(); self.initialize_box_zoom()
        self.update_scale_readouts(); self.canvas.draw_idle()

    def style_axis(self, ax: Any) -> None:
        ax.set_facecolor("#FCFCFC")
        ax.grid(False)
        ax.spines[["top", "right"]].set_visible(False)
        ax.spines[["left", "bottom"]].set_color("#A7A7A7")
        ax.tick_params(colors="#4A4A4A", labelsize=9)

    def shade_normalization_baseline(self, ax: Any) -> None:
        if self.processed is None or not self.normalized:
            return
        mask = self.processed["normalization_baseline"].to_numpy(bool)
        if mask.any():
            time_column = "relative_time_min" if self.normalized else "time_min"
            times = self.processed[time_column].to_numpy(float)
            ax.axvspan(float(times[mask][0]), float(times[mask][-1]), color="#B8B8B8",
                       alpha=0.18, label="Normalization baseline")

    def autoscale_current_window(self) -> None:
        """Fit every visible Y axis to trace data inside the current X window."""
        if not self.axes:
            return
        x_low, x_high = self.axes[-1].get_xlim()
        adjusted = 0
        for ax in self.axes:
            visible_values: list[np.ndarray] = []
            for line in ax.get_lines():
                xdata = np.asarray(line.get_xdata(), dtype=float)
                ydata = np.asarray(line.get_ydata(), dtype=float)
                # Marker/cursor/reference lines have only two points.
                if xdata.size <= 2 or ydata.size != xdata.size:
                    continue
                mask = (xdata >= x_low) & (xdata <= x_high) & np.isfinite(ydata)
                if mask.any():
                    visible_values.append(ydata[mask])
            if visible_values:
                values = np.concatenate(visible_values)
                low, high = float(np.min(values)), float(np.max(values))
                if np.isclose(low, high):
                    padding = max(abs(low) * 0.05, 1.0)
                else:
                    padding = (high - low) * 0.06
                ax.set_ylim(low - padding, high + padding)
                adjusted += 1
        self.vars["status"].set(f"Y-axis auto-scaled for {adjusted} plot(s) in the current X window.")
        self.update_scale_readouts()
        self.canvas.draw_idle()

    def draw_markers(self) -> None:
        zero_time = None
        if self.normalized and self.details and self.details.get("normalization"):
            zero_time = self.details["normalization"].get("zero_time_min")
        for marker in self.markers:
            color, style = ("#777777", ":") if marker["source"] == "original" else ("#C65D3B", "-")
            display_time = float(marker["time_min"]) - (float(zero_time) if zero_time is not None else 0.0)
            for ax in self.axes:
                ax.axvline(display_time, color=color, ls=style, lw=1.0)
            self.axes[0].annotate(marker["name"], xy=(display_time, 1), xycoords=("data", "axes fraction"),
                                  xytext=(2, -2), textcoords="offset points", rotation=90, va="top", fontsize=8, color=color)

    def initialize_hover_artists(self) -> None:
        self.hover_x_lines = [ax.axvline(0, color="#5B5B5B", lw=0.8, ls=":", visible=False,
                                            animated=True, zorder=20) for ax in self.axes]
        self.hover_y_lines = [ax.axhline(0, color="#5B5B5B", lw=0.8, ls=":", visible=False,
                                            animated=True, zorder=20) for ax in self.axes]
        self.hover_y_labels = [
            ax.text(0.006, 0, "", transform=ax.get_yaxis_transform(), ha="left", va="center",
                    fontsize=8, color="white", clip_on=True, visible=False, animated=True,
                    bbox={"boxstyle": "round,pad=0.22", "facecolor": "#444444", "edgecolor": "none"},
                    zorder=21)
            for ax in self.axes
        ]
        bottom = self.axes[-1]
        self.hover_x_label = bottom.text(
            0, 0, "", transform=bottom.get_xaxis_transform(), ha="center", va="top",
            fontsize=8, color="white", clip_on=False, visible=False, animated=True,
            bbox={"boxstyle": "round,pad=0.22", "facecolor": "#444444", "edgecolor": "none"},
            zorder=21,
        )
        for artist in self.hover_x_lines + self.hover_y_lines + self.hover_y_labels + [self.hover_x_label]:
            artist.set_in_layout(False)
        self.hover_background = None

    def cache_hover_background(self, _event: Any = None) -> None:
        try:
            self.hover_background = self.canvas.copy_from_bbox(self.figure.bbox)
        except Exception:
            self.hover_background = None

    def blit_hover(self) -> None:
        if self.hover_background is None:
            self.canvas.draw_idle()
            return
        self.canvas.restore_region(self.hover_background)
        for ax, x_line, y_line, y_label in zip(
            self.axes, self.hover_x_lines, self.hover_y_lines, self.hover_y_labels
        ):
            if x_line.get_visible():
                ax.draw_artist(x_line)
            if y_line.get_visible():
                ax.draw_artist(y_line)
            if y_label.get_visible():
                ax.draw_artist(y_label)
        if self.hover_x_label is not None and self.hover_x_label.get_visible():
            self.axes[-1].draw_artist(self.hover_x_label)
        self.canvas.blit(self.figure.bbox)

    def hover_motion(self, event: Any) -> None:
        if self.box_drag_active or self.axis_drag_state is not None:
            return
        if event.inaxes not in self.axes or event.xdata is None or event.ydata is None:
            self.hover_leave()
            return
        for line in self.hover_x_lines:
            line.set_xdata([event.xdata, event.xdata]); line.set_visible(True)
        active_index = self.axes.index(event.inaxes)
        for index, (line, label) in enumerate(zip(self.hover_y_lines, self.hover_y_labels)):
            visible = index == active_index
            line.set_visible(visible); label.set_visible(visible)
            if visible:
                line.set_ydata([event.ydata, event.ydata])
                label.set_y(event.ydata); label.set_text(f"{event.ydata:.6g}")
        if self.hover_x_label is not None:
            self.hover_x_label.set_x(event.xdata)
            self.hover_x_label.set_text(f"{event.xdata:.5f} min")
            self.hover_x_label.set_visible(True)
        self.blit_hover()

    def hover_leave(self, _event: Any = None) -> None:
        for line in getattr(self, "hover_x_lines", []):
            line.set_visible(False)
        for line in getattr(self, "hover_y_lines", []):
            line.set_visible(False)
        for label in getattr(self, "hover_y_labels", []):
            label.set_visible(False)
        if self.hover_x_label is not None:
            self.hover_x_label.set_visible(False)
        if self.hover_background is not None:
            self.canvas.restore_region(self.hover_background)
            self.canvas.blit(self.figure.bbox)
        else:
            self.canvas.draw_idle()

    def sorted_markers(self) -> list[dict[str, Any]]:
        return sorted(self.markers, key=lambda marker: marker["time_min"])

    def marker_choices(self) -> list[str]:
        return [f"{index + 1}. {marker['name']} @ {marker['time_min']:.5f} min"
                for index, marker in enumerate(self.sorted_markers())]

    def marker_time_from_choice(self, choice: str) -> float:
        choices = self.marker_choices()
        if choice not in choices:
            raise ValueError("Select a valid marker.")
        return float(self.sorted_markers()[choices.index(choice)]["time_min"])

    def refresh_markers(self) -> None:
        self.marker_list.delete(0, self.tk.END)
        for marker in self.sorted_markers():
            source = "Original" if marker["source"] == "original" else "Manual"
            self.marker_list.insert(self.tk.END, f"[{source}] {marker['time_min']:.4f}  {marker['name']}")
        choices = self.marker_choices()
        for box, variable in ((getattr(self, "norm_marker_box", None), self.norm_marker_var),
                              (getattr(self, "zero_marker_box", None), self.zero_marker_var),
                              (getattr(self, "export_marker_box", None), self.export_marker_var)):
            if box is not None:
                box.configure(values=choices)
            if choices and variable.get() not in choices:
                variable.set(choices[0])
            elif not choices:
                variable.set("")
        if not choices and self.zero_enabled_var.get() and self.zero_mode_var.get() == "marker":
            self.zero_mode_var.set("time")
        marker_names = sorted({str(marker["name"]) for marker in self.markers})
        previous_event_name = self.event_marker_var.get()
        if getattr(self, "event_marker_box", None) is not None:
            self.event_marker_box.configure(values=marker_names)
        if marker_names and self.event_marker_var.get() not in marker_names:
            self.event_marker_var.set(marker_names[0])
        elif not marker_names:
            self.event_marker_var.set("")
        self.refresh_event_marker_list(select_all=self.event_marker_var.get() != previous_event_name)

    def available_event_signals(self) -> list[dict[str, str]]:
        """Describe corrected wavelength traces and the currently configured ratio."""
        choices = [
            {
                "key": f"wavelength:{wavelength}",
                "label": f"{wavelength} (corrected)",
                "kind": "wavelength",
                "column": f"corrected_{wavelength}",
            }
            for wavelength in self.available_wavelengths
        ]
        analysis = self.analysis_wavelength_var.get()
        reference = self.reference_wavelength_var.get()
        if (analysis in self.available_wavelengths
                and reference in self.available_wavelengths
                and analysis != reference):
            choices.append({
                "key": f"ratio:{analysis}/{reference}",
                "label": f"Ratio {analysis}/{reference}",
                "kind": "ratio",
                "numerator": analysis,
                "denominator": reference,
            })
        return choices

    def selected_event_signals(self) -> list[dict[str, str]]:
        if not hasattr(self, "event_signal_list"):
            return []
        return [
            self.event_signal_candidates[index]
            for index in self.event_signal_list.curselection()
            if 0 <= index < len(self.event_signal_candidates)
        ]

    def refresh_event_signal_list(self, prefer_default: bool = False) -> None:
        if not hasattr(self, "event_signal_list"):
            return
        selected_keys = {signal["key"] for signal in self.selected_event_signals()}
        had_candidates = bool(self.event_signal_candidates)
        self.event_signal_candidates = self.available_event_signals()
        self.event_signal_list.delete(0, self.tk.END)
        for signal in self.event_signal_candidates:
            self.event_signal_list.insert(self.tk.END, signal["label"])

        if self.event_signal_candidates:
            if prefer_default or not had_candidates:
                analysis = self.analysis_wavelength_var.get()
                reference = self.reference_wavelength_var.get()
                default_key = (f"ratio:{analysis}/{reference}"
                               if reference not in {"", "None"}
                               else f"wavelength:{analysis}")
                default_index = next(
                    (index for index, signal in enumerate(self.event_signal_candidates)
                     if signal["key"] == default_key),
                    0,
                )
                self.event_signal_list.selection_set(default_index)
            else:
                for index, signal in enumerate(self.event_signal_candidates):
                    if signal["key"] in selected_keys:
                        self.event_signal_list.selection_set(index)
        self.update_event_signal_selection_status()

    def update_event_signal_selection_status(self) -> None:
        selected = self.selected_event_signals()
        total = len(self.event_signal_candidates)
        if selected:
            labels = ", ".join(signal["label"] for signal in selected)
            self.event_signal_status_var.set(f"Selected {len(selected)} of {total}: {labels}")
        elif total:
            self.event_signal_status_var.set(f"Selected 0 of {total} signals.")
        else:
            self.event_signal_status_var.set("No corrected wavelength or ratio is available.")

    def event_signal_selection_changed(self, _event: Any = None) -> None:
        event_plot_was_active = self.event_view_active
        if self.event_details is not None:
            self.clear_event_analysis()
        self.update_event_signal_selection_status()
        if event_plot_was_active and self.processed is not None:
            self.draw_processed()

    def select_all_event_signals(self) -> None:
        if self.event_signal_candidates:
            self.event_signal_list.selection_set(0, self.tk.END)
        self.event_signal_selection_changed()

    def clear_event_signal_selection(self) -> None:
        self.event_signal_list.selection_clear(0, self.tk.END)
        self.event_signal_selection_changed()

    def event_signal_values(self, signal: dict[str, str]) -> np.ndarray:
        if self.processed is None:
            raise ValueError("Apply correction before calculating event analysis.")
        if signal["kind"] == "wavelength":
            column = signal["column"]
            if column not in self.processed:
                raise ValueError(f"{signal['label']} is unavailable in the corrected data.")
            return self.display_values(column)
        numerator_column = f"corrected_{signal['numerator']}"
        denominator_column = f"corrected_{signal['denominator']}"
        if numerator_column not in self.processed or denominator_column not in self.processed:
            raise ValueError(f"{signal['label']} is unavailable in the corrected data.")
        numerator = self.processed[numerator_column].to_numpy(float)
        denominator = self.processed[denominator_column].to_numpy(float)
        ratio = np.full(len(self.processed), np.nan, dtype=float)
        valid = np.isfinite(numerator) & np.isfinite(denominator) & ~np.isclose(denominator, 0)
        np.divide(numerator, denominator, out=ratio, where=valid)
        return self.smooth_array(ratio)

    def selected_event_markers(self) -> list[dict[str, Any]]:
        """Return only the event markers explicitly selected in the event-analysis list."""
        if not hasattr(self, "event_marker_list"):
            return []
        return [
            self.event_marker_candidates[index]
            for index in self.event_marker_list.curselection()
            if 0 <= index < len(self.event_marker_candidates)
        ]

    def refresh_event_marker_list(self, select_all: bool = False) -> None:
        if not hasattr(self, "event_marker_list"):
            return
        marker_name = self.event_marker_var.get().strip()
        same_name = marker_name == self.event_marker_list_name
        selected_ids = ({str(marker["id"]) for marker in self.selected_event_markers()}
                        if same_name else set())
        self.event_marker_candidates = [
            marker for marker in self.sorted_markers() if str(marker["name"]) == marker_name
        ]
        self.event_marker_list.delete(0, self.tk.END)
        for index, marker in enumerate(self.event_marker_candidates, start=1):
            source = "Original" if marker["source"] == "original" else "Manual"
            self.event_marker_list.insert(
                self.tk.END, f"{index}. {marker['time_min']:.5f} min  [{source}]",
            )
        self.event_marker_list_name = marker_name
        if self.event_marker_candidates:
            if select_all or not same_name:
                self.event_marker_list.selection_set(0, self.tk.END)
            else:
                for index, marker in enumerate(self.event_marker_candidates):
                    if str(marker["id"]) in selected_ids:
                        self.event_marker_list.selection_set(index)
        self.update_event_marker_selection_status()

    def update_event_marker_selection_status(self) -> None:
        total = len(self.event_marker_candidates)
        selected = (len(self.event_marker_list.curselection())
                    if hasattr(self, "event_marker_list") else 0)
        marker_name = self.event_marker_var.get().strip()
        if total:
            self.vars["event_status"].set(
                f"Selected {selected} of {total} '{marker_name}' marker(s) for event analysis."
            )
        elif marker_name:
            self.vars["event_status"].set(f"No markers named '{marker_name}' are available.")
        else:
            self.vars["event_status"].set("Select a marker name after loading data.")

    def event_marker_changed(self, _event: Any = None) -> None:
        event_plot_was_active = self.event_view_active
        self.clear_event_analysis()
        self.refresh_event_marker_list(select_all=True)
        if event_plot_was_active and self.processed is not None:
            self.draw_processed()

    def event_marker_selection_changed(self, _event: Any = None) -> None:
        event_plot_was_active = self.event_view_active
        if self.event_details is not None:
            self.clear_event_analysis()
        self.update_event_marker_selection_status()
        if event_plot_was_active and self.processed is not None:
            self.draw_processed()

    def select_all_event_markers(self) -> None:
        if self.event_marker_candidates:
            self.event_marker_list.selection_set(0, self.tk.END)
        self.event_marker_selection_changed()

    def clear_event_marker_selection(self) -> None:
        self.event_marker_list.selection_clear(0, self.tk.END)
        self.event_marker_selection_changed()

    def add_marker(self) -> None:
        try:
            self.markers.append({"id": str(uuid.uuid4()), "time_min": self.number("marker_time"),
                                 "name": self.vars["marker_name"].get().strip() or "marker", "source": "manual"})
            self.clear_event_analysis("Marker list changed. Recalculate the event analysis.")
            self.refresh_markers(); self.redraw_current()
        except Exception as exc:
            self.vars["status"].set(f"Failed to add marker: {exc}")

    def delete_marker(self) -> None:
        selected = self.marker_list.curselection()
        if selected:
            target = self.sorted_markers()[selected[0]]
            self.markers = [marker for marker in self.markers if marker["id"] != target["id"]]
            self.clear_event_analysis("Marker list changed. Recalculate the event analysis.")
            self.refresh_markers(); self.redraw_current()

    def reset_markers(self) -> None:
        self.markers = [dict(marker) for marker in self.original_markers]
        self.clear_event_analysis("Marker list restored. Recalculate the event analysis.")
        self.refresh_markers(); self.redraw_current()

    def selected_line_width_key(self) -> str:
        selected = self.line_width_selector_var.get()
        return next((key for key, label in LINE_WIDTH_LABELS.items() if label == selected), "raw470")

    def line_width(self, key: str) -> float:
        return float(self.line_width_vars[key].get())

    def line_width_selection_changed(self, _event: Any = None) -> None:
        key = self.selected_line_width_key()
        self.line_width_scale.configure(variable=self.line_width_vars[key])
        self.line_width_text.set(f"{self.line_width(key):.1f}")

    def line_width_changed(self, value: str) -> None:
        key = self.selected_line_width_key()
        width = float(value)
        self.line_width_vars[key].set(width)
        self.line_width_text.set(f"{width:.1f}")
        for ax in self.axes:
            for line in ax.get_lines():
                if line.get_gid() == key:
                    line.set_linewidth(width)
        self.canvas.draw_idle()

    def resolve_baseline_interval(self) -> tuple[float, float, dict[str, Any]]:
        if self.norm_mode_var.get() == "manual":
            start, end = self.number("norm_start"), self.number("norm_end")
            return start, end, {"mode": "manual"}
        marker_time = self.marker_time_from_choice(self.norm_marker_var.get())
        duration = self.number("norm_pre_duration")
        if duration <= 0:
            raise ValueError("The pre-marker baseline duration must be greater than 0.")
        return marker_time - duration, marker_time, {
            "mode": "marker_before", "marker": self.norm_marker_var.get(),
            "duration_min": duration,
        }

    def resolve_zero_time(self) -> tuple[float | None, dict[str, Any]]:
        if not self.zero_enabled_var.get():
            return None, {"enabled": False}
        if self.zero_mode_var.get() == "marker":
            value = self.marker_time_from_choice(self.zero_marker_var.get())
            return value, {"enabled": True, "mode": "marker", "marker": self.zero_marker_var.get()}
        value = self.number("zero_time")
        return value, {"enabled": True, "mode": "time", "original_time_min": value}

    def current_event_settings(
        self,
    ) -> tuple[str, float, float, float, tuple[str, ...], tuple[str, ...]]:
        marker_name = self.event_marker_var.get().strip()
        if not marker_name:
            raise ValueError("Select a marker name for event analysis.")
        selected_markers = self.selected_event_markers()
        if not selected_markers:
            raise ValueError("Select at least one marker for event analysis.")
        selected_signals = self.selected_event_signals()
        if not selected_signals:
            raise ValueError("Select at least one signal for event analysis.")
        pre_seconds = self.number("event_pre_seconds")
        post_seconds = self.number("event_post_seconds")
        baseline_seconds = self.number("event_baseline_seconds")
        if pre_seconds <= 0 or post_seconds <= 0 or baseline_seconds <= 0:
            raise ValueError("Event trace and baseline durations must be greater than 0 seconds.")
        if baseline_seconds > pre_seconds:
            raise ValueError("The event baseline cannot be longer than the pre-marker trace interval.")
        selected_ids = tuple(str(marker["id"]) for marker in selected_markers)
        selected_signal_keys = tuple(signal["key"] for signal in selected_signals)
        return (marker_name, pre_seconds, post_seconds, baseline_seconds,
                selected_ids, selected_signal_keys)

    def calculate_event_analysis(self) -> None:
        from tkinter import messagebox
        if self.processed is None or self.details is None:
            messagebox.showinfo("Correction Required", "Complete fitting and apply correction first.")
            return
        try:
            (marker_name, pre_seconds, post_seconds, baseline_seconds,
             selected_ids, selected_signal_keys) = self.current_event_settings()
            selected_markers = self.selected_event_markers()
            selected_signals = self.selected_event_signals()
            marker_times = [float(marker["time_min"]) for marker in selected_markers]
            trial_frames: list[pd.DataFrame] = []
            average_frames: list[pd.DataFrame] = []
            signal_details: dict[str, Any] = {}
            excluded_events: list[dict[str, Any]] = []
            included_by_signal: dict[str, int] = {}
            for signal in selected_signals:
                event_source = self.processed[["original_time_s"]].copy()
                event_source["event_signal"] = self.event_signal_values(signal)
                trials, average, details = calculate_event_locked_traces(
                    event_source,
                    marker_times,
                    marker_name,
                    pre_seconds,
                    post_seconds,
                    baseline_seconds,
                    signal_column="event_signal",
                )
                trials.insert(0, "event_signal_label", signal["label"])
                trials.insert(0, "event_signal_key", signal["key"])
                average.insert(0, "event_signal_label", signal["label"])
                average.insert(0, "event_signal_key", signal["key"])
                trial_frames.append(trials)
                average_frames.append(average)
                included_by_signal[signal["key"]] = int(details["included_trials"])
                for excluded in details["excluded_events"]:
                    excluded_events.append({
                        "event_signal_key": signal["key"],
                        "event_signal_label": signal["label"],
                        **excluded,
                    })
                signal_details[signal["key"]] = {
                    "label": signal["label"],
                    "kind": signal["kind"],
                    **{key: value for key, value in signal.items()
                       if key not in {"key", "label", "kind"}},
                    **details,
                }

            event_details = {
                "marker_name": marker_name,
                "requested_events": len(marker_times),
                "included_trials": int(sum(included_by_signal.values())),
                "included_trials_per_signal": included_by_signal,
                "excluded_events": excluded_events,
                "pre_seconds": float(pre_seconds),
                "post_seconds": float(post_seconds),
                "baseline_seconds": float(baseline_seconds),
                "smoothing_seconds": self.smoothing_seconds(),
                "source_channel": self.source_channel_var.get(),
                "available_events_with_marker_name": len(self.event_marker_candidates),
                "selected_marker_ids": list(selected_ids),
                "selected_marker_times_min": marker_times,
                "selected_signal_keys": list(selected_signal_keys),
                "selected_signals": [dict(signal) for signal in selected_signals],
                "signal_details": signal_details,
                "normalization": "each signal trial uses its own pre-marker baseline",
            }
            self.event_trials = pd.concat(trial_frames, ignore_index=True)
            self.event_average = pd.concat(average_frames, ignore_index=True)
            self.event_details = event_details
            self.event_view_active = True
            self.draw_event_analysis()
            excluded_count = len(event_details["excluded_events"])
            self.vars["event_status"].set(
                f"Calculated {len(marker_times)} selected marker(s) × {len(selected_signals)} signal(s) "
                f"for '{marker_name}' ({event_details['included_trials']} included traces)"
                + (f"; excluded {excluded_count} incomplete/invalid trace(s)." if excluded_count else ".")
            )
            self.vars["status"].set(self.vars["event_status"].get())
        except Exception as exc:
            messagebox.showerror("Event Analysis Failed", str(exc))
            self.vars["event_status"].set(f"Event analysis failed: {exc}")

    def plot_event_analysis_axes(self, axes: list[Any], interactive: bool) -> None:
        """Draw one or several event signals on the shared four-panel layout."""
        if self.event_trials is None or self.event_average is None or self.event_details is None:
            return
        trials, average = self.event_trials, self.event_average
        signals = self.event_details.get("selected_signals", [])
        signal_palette = ["#4C78A8", "#F58518", "#54A24B", "#E45756",
                          "#72B7B2", "#B279A2", "#9D755D", "#7B4B94"]
        trial_palette = ["#4C78A8", "#F58518", "#54A24B", "#E45756", "#72B7B2",
                         "#B279A2", "#FF9DA6", "#9D755D", "#BAB0AC", "#7B4B94"]
        multiple_signals = len(signals) > 1

        for signal_index, signal in enumerate(signals):
            signal_key, signal_label = signal["key"], signal["label"]
            color = signal_palette[signal_index % len(signal_palette)]
            signal_trials = trials.loc[trials["event_signal_key"] == signal_key]
            grouped_trials = list(signal_trials.groupby("trial", sort=True))
            for trial_index, (trial_number, trial) in enumerate(grouped_trials):
                trial_time = trial["relative_time_s"].to_numpy(float)
                trial_color = color if multiple_signals else trial_palette[trial_index % len(trial_palette)]
                trial_label = (f"{signal_label} (n={len(grouped_trials)})"
                               if multiple_signals and trial_index == 0
                               else (None if multiple_signals else f"Trial {trial_number}"))
                axes[0].plot(
                    trial_time, trial["dff_percent"].to_numpy(float), color=trial_color,
                    lw=self.line_width("dff"), alpha=0.38 if multiple_signals else 0.78,
                    label=trial_label, gid="dff" if interactive else None,
                )
                axes[2].plot(
                    trial_time, trial["zscore"].to_numpy(float), color=trial_color,
                    lw=self.line_width("zscore"), alpha=0.38 if multiple_signals else 0.78,
                    label=trial_label, gid="zscore" if interactive else None,
                )

            signal_average = average.loc[average["event_signal_key"] == signal_key]
            relative_time = signal_average["relative_time_s"].to_numpy(float)
            dff_mean = signal_average["dff_percent_mean"].to_numpy(float)
            dff_sem = signal_average["dff_percent_sem"].to_numpy(float)
            zscore_mean = signal_average["zscore_mean"].to_numpy(float)
            zscore_sem = signal_average["zscore_sem"].to_numpy(float)
            mean_color = color if multiple_signals else "#5B2C83"
            mean_label = signal_label if multiple_signals else "Mean"
            axes[1].plot(relative_time, dff_mean, color=mean_color, lw=2.0,
                         label=mean_label, gid="dff" if interactive else None)
            axes[3].plot(relative_time, zscore_mean, color=mean_color, lw=2.0,
                         label=mean_label, gid="zscore" if interactive else None)
            if np.any(np.isfinite(dff_sem)):
                axes[1].fill_between(relative_time, dff_mean - dff_sem, dff_mean + dff_sem,
                                     color=mean_color, alpha=0.20,
                                     label="SEM" if not multiple_signals else None)
            if np.any(np.isfinite(zscore_sem)):
                axes[3].fill_between(relative_time, zscore_mean - zscore_sem, zscore_mean + zscore_sem,
                                     color=mean_color, alpha=0.20,
                                     label="SEM" if not multiple_signals else None)

        labels = ["Trial dF/F0 (%)", "Average dF/F0 (%)", "Trial Z-score", "Average Z-score"]
        baseline_start = -float(self.event_details["baseline_seconds"])
        for ax, label in zip(axes, labels):
            ax.axvspan(baseline_start, 0, color="#B8B8B8", alpha=0.18)
            ax.axvline(0, color="#C43C39", ls="--", lw=1.0)
            ax.axhline(0, color="#777777", lw=0.7, alpha=0.6)
            ax.set_ylabel(label)
            if interactive:
                self.style_axis(ax)
            else:
                ax.grid(False)
                ax.spines[["top", "right"]].set_visible(False)
        for axis_index in (0, 1, 3):
            handles, legend_labels = axes[axis_index].get_legend_handles_labels()
            if handles:
                axes[axis_index].legend(frameon=False, ncol=min(4, len(handles)),
                                        fontsize=7 if axis_index == 0 else 8,
                                        loc="upper right")
        axes[-1].set_xlabel("Time from marker (s)")
        axes[-1].set_xlim(-float(self.event_details["pre_seconds"]),
                          float(self.event_details["post_seconds"]))

    def event_analysis_title(self, suffix: str) -> str:
        assert self.event_details is not None
        return (
            f"Event analysis | {self.event_details['marker_name']} | "
            f"markers={self.event_details['requested_events']} | "
            f"signals={len(self.event_details.get('selected_signal_keys', []))} | {suffix}"
        )

    def draw_event_analysis(self) -> None:
        if self.event_trials is None or self.event_average is None or self.event_details is None:
            return
        self.set_x_axis_unit("s")
        for selector in self.zoom_selectors:
            selector.set_active(False)
            selector.disconnect_events()
        self.zoom_selectors = []
        self.figure.clear()
        self.axes = self.figure.subplots(4, 1, sharex=True, squeeze=False).ravel().tolist()
        self.axis_keys = ["event_dff_trials", "event_dff_average", "event_zscore_trials", "event_zscore_average"]
        self.plot_event_analysis_axes(self.axes, interactive=True)
        self.figure.suptitle(self.event_analysis_title("shaded area=baseline"), fontsize=11)
        self.refresh_axis_scale_controls()
        self.capture_initial_view()
        self.initialize_hover_artists()
        self.initialize_box_zoom()
        self.update_scale_readouts()
        self.canvas.draw_idle()

    def calculate_normalization(self) -> None:
        from tkinter import messagebox
        if self.processed is None or self.details is None:
            messagebox.showinfo("Correction Required", "Complete fitting and apply correction first.")
            return
        try:
            baseline_start, baseline_end, baseline_definition = self.resolve_baseline_interval()
            zero_time, zero_definition = self.resolve_zero_time()
            normalized, normalization_details = calculate_normalized_traces(
                self.processed,
                baseline_start,
                baseline_end,
                self.smoothing_seconds(),
                float(self.details["sample_rate_hz"]),
                zero_time,
            )
            normalization_details["baseline_definition"] = baseline_definition
            normalization_details["zero_definition"] = zero_definition
            self.processed = normalized
            self.details["normalization"] = normalization_details
            self.normalized = True
            self.event_view_active = False
            self.display_cache.clear()
            self.rebuild_axes(); self.draw_processed()
            self.vars["status"].set(
                f"Normalization complete: F0={normalization_details['f0_mean']:.7g}, "
                f"baseline samples={normalization_details['samples']:,}."
            )
        except Exception as exc:
            messagebox.showerror("Normalization Failed", str(exc))

    def redraw_current(self) -> None:
        limits = None
        y_limits: dict[str, tuple[float, float]] = {}
        if self.axes and self.axes[-1].has_data():
            limits = self.axes[-1].get_xlim()
            y_limits = {key: ax.get_ylim() for ax, key in zip(self.axes, self.axis_keys)}
        if self.event_view_active and self.event_trials is not None:
            self.draw_event_analysis()
        elif self.processed is not None:
            self.draw_processed()
        elif self.data is not None:
            self.draw_raw_preview()
        if limits is not None and self.axes:
            for ax in self.axes:
                ax.set_xlim(*limits)
            for ax, key in zip(self.axes, self.axis_keys):
                if key in y_limits:
                    ax.set_ylim(*y_limits[key])
            self.update_scale_readouts()
            self.canvas.draw_idle()

    def save_trace_png(
        self,
        path: Path,
        panels: list[tuple[str, np.ndarray, str, str]],
        title: str,
        baseline_limits: tuple[float, float] | None = None,
    ) -> None:
        from matplotlib.figure import Figure
        if self.processed is None:
            return
        figure = Figure(figsize=(12, max(3.2, 2.7 * len(panels))), dpi=100, constrained_layout=True)
        axes = figure.subplots(len(panels), 1, sharex=True, squeeze=False).ravel().tolist()
        time_column = "relative_time_min" if self.normalized else "time_min"
        time_min = self.processed[time_column].to_numpy(float)
        indices = self.display_indices(len(time_min))
        zero_time = (self.details.get("normalization", {}).get("zero_time_min")
                     if self.details and self.details.get("normalization") else None)
        for ax, (label, values, color, width_key) in zip(axes, panels):
            ax.plot(time_min[indices], np.asarray(values)[indices], color=color,
                    lw=self.line_width(width_key))
            if baseline_limits is not None:
                ax.axvspan(*baseline_limits, color="#B8B8B8", alpha=0.18)
            for marker in self.markers:
                marker_time = float(marker["time_min"]) - (float(zero_time) if zero_time is not None else 0.0)
                ax.axvline(marker_time, color="#777777", ls=":", lw=0.8, alpha=0.75)
            ax.set_ylabel(label)
            ax.grid(False)
            ax.spines[["top", "right"]].set_visible(False)
        axes[0].set_title(title)
        axes[-1].set_xlabel("Time relative to zero (min)" if zero_time is not None else "Recording time (min)")
        axes[-1].set_xlim(float(time_min[0]), float(time_min[-1]))
        save_args: dict[str, Any] = {"facecolor": "white", "format": path.suffix.lstrip(".")}
        if path.suffix.lower() == ".png":
            save_args["dpi"] = 220
        # Keep labels as text in SVG instead of converting glyphs to outlines,
        # so Illustrator can edit both the annotation and the vector curves.
        if path.suffix.lower() == ".svg":
            from matplotlib import rc_context
            with rc_context({"svg.fonttype": "none"}):
                figure.savefig(path, **save_args)
        else:
            figure.savefig(path, **save_args)

    def save_event_analysis_figure(self, path: Path) -> None:
        """Export selected event signals, individual trials and mean ± SEM."""
        from matplotlib.figure import Figure
        if self.event_trials is None or self.event_average is None or self.event_details is None:
            raise ValueError("Calculate event analysis before exporting event plots.")
        figure = Figure(figsize=(12, 10), dpi=100, constrained_layout=True)
        axes = figure.subplots(4, 1, sharex=True, squeeze=False).ravel().tolist()
        self.plot_event_analysis_axes(axes, interactive=False)
        figure.suptitle(self.event_analysis_title("mean ± SEM"), fontsize=12)
        save_args: dict[str, Any] = {"facecolor": "white", "format": path.suffix.lstrip(".")}
        if path.suffix.lower() == ".png":
            save_args["dpi"] = 220
        if path.suffix.lower() == ".svg":
            from matplotlib import rc_context
            with rc_context({"svg.fonttype": "none"}):
                figure.savefig(path, **save_args)
        else:
            figure.savefig(path, **save_args)

    def ordered_event_export_signals(self) -> list[dict[str, str]]:
        """Return selected signals in 410, 470, 560, then ratio column order."""
        if self.event_details is None:
            return []
        wavelength_order = {value: index for index, value in enumerate(WAVELENGTHS)}

        def order_key(signal: dict[str, str]) -> tuple[int, int]:
            if signal.get("kind") == "wavelength":
                wavelength = signal.get("key", "").split(":", 1)[-1]
                return 0, wavelength_order.get(wavelength, len(wavelength_order))
            return 1, 0

        return sorted(self.event_details.get("selected_signals", []), key=order_key)

    @staticmethod
    def event_export_prefix(signal: dict[str, str]) -> str:
        if signal.get("kind") == "wavelength":
            return signal.get("key", "wavelength").split(":", 1)[-1]
        numerator = signal.get("numerator", "numerator")
        denominator = signal.get("denominator", "denominator")
        return f"ratio_{numerator}_{denominator}"

    def event_trials_for_export(self) -> pd.DataFrame:
        """Convert internal long-form trials to signal-grouped wide columns."""
        if self.event_trials is None:
            raise ValueError("Calculate event analysis before exporting event trials.")
        if "event_signal_key" not in self.event_trials:
            return self.event_trials.copy()
        merge_keys = ["source_event_index", "relative_time_s"]
        common_columns = [
            "source_event_index", "marker_name", "marker_time_min",
            "relative_time_s", "event_baseline",
        ]
        available_common = [column for column in common_columns if column in self.event_trials]
        wide = (self.event_trials[available_common]
                .drop_duplicates(subset=merge_keys)
                .copy())
        metric_columns = [
            "trial", "corrected_signal", "dff_percent", "zscore", "f0", "baseline_dff_sd",
        ]
        for signal in self.ordered_event_export_signals():
            prefix = self.event_export_prefix(signal)
            signal_frame = self.event_trials.loc[
                self.event_trials["event_signal_key"] == signal["key"],
                [*merge_keys, *metric_columns],
            ].copy()
            signal_frame = signal_frame.rename(
                columns={column: f"{prefix}_{column}" for column in metric_columns}
            )
            wide = wide.merge(signal_frame, on=merge_keys, how="left", validate="one_to_one")
        return wide.sort_values(merge_keys, kind="stable").reset_index(drop=True)

    def event_average_for_export(self) -> pd.DataFrame:
        """Convert internal long-form averages to signal-grouped wide columns."""
        if self.event_average is None:
            raise ValueError("Calculate event analysis before exporting event averages.")
        if "event_signal_key" not in self.event_average:
            return self.event_average.copy()
        merge_keys = ["relative_time_s"]
        common_columns = ["relative_time_s", "event_baseline"]
        wide = (self.event_average[common_columns]
                .drop_duplicates(subset=merge_keys)
                .copy())
        metric_columns = [
            "n_trials", "corrected_signal_mean", "corrected_signal_sem",
            "dff_percent_mean", "dff_percent_sem", "zscore_mean", "zscore_sem",
        ]
        for signal in self.ordered_event_export_signals():
            prefix = self.event_export_prefix(signal)
            signal_frame = self.event_average.loc[
                self.event_average["event_signal_key"] == signal["key"],
                [*merge_keys, *metric_columns],
            ].copy()
            signal_frame = signal_frame.rename(
                columns={column: f"{prefix}_{column}" for column in metric_columns}
            )
            wide = wide.merge(signal_frame, on=merge_keys, how="left", validate="one_to_one")
        return wide.sort_values(merge_keys, kind="stable").reset_index(drop=True)

    def resolve_export_annotation(self) -> tuple[tuple[float, float] | None, str]:
        """Return an export highlight in the plotted time coordinate."""
        mode = self.export_annotation_var.get()
        if mode == "none":
            return None, "none"
        zero_time = (self.details.get("normalization", {}).get("zero_time_min")
                     if self.details and self.details.get("normalization") else None)
        shift = float(zero_time) if zero_time is not None else 0.0
        if mode == "baseline":
            start, end, _definition = self.resolve_baseline_interval()
            return (start - shift, end - shift), "normalization_baseline"
        if mode == "marker_after":
            start = self.marker_time_from_choice(self.export_marker_var.get())
            duration = self.number("export_window_duration")
            if duration <= 0:
                raise ValueError("The marker-window duration must be greater than 0.")
            return (start - shift, start + duration - shift), "marker_after_window"
        raise ValueError("Select a valid export annotation mode.")

    def next_output_directory(self) -> Path:
        if self.folder is None:
            raise ValueError("No input folder has been selected.")
        output_name = self.vars["output_name"].get().strip()
        if not output_name:
            raise ValueError("Enter an output folder name.")
        if re.search(r'[<>:"/\\|?*\x00-\x1f]', output_name) or output_name in {".", ".."}:
            raise ValueError("The output folder name contains characters that are not allowed by Windows.")
        if output_name.endswith((" ", ".")):
            raise ValueError("The output folder name cannot end with a space or period.")
        reserved = {"CON", "PRN", "AUX", "NUL", *(f"COM{i}" for i in range(1, 10)),
                    *(f"LPT{i}" for i in range(1, 10))}
        if output_name.upper() in reserved:
            raise ValueError("This output folder name is reserved by Windows. Choose another name.")
        output = self.folder / output_name
        suffix = 2
        while output.exists():
            output = self.folder / f"{output_name}_{suffix}"
            suffix += 1
        return output

    def prepared_export_frame(self) -> tuple[pd.DataFrame, np.ndarray]:
        """Return rows and signal values after the display processing that was actually applied."""
        if self.processed is None:
            raise ValueError("No data are available for export.")
        indices = self.display_indices(len(self.processed))
        frame = self.processed.iloc[indices].copy().reset_index(drop=True)
        signal_columns = ["combined_signal", "analysis_trace", "dff_percent", "zscore"]
        for wavelength in WAVELENGTHS:
            signal_columns.extend([
                f"corrected_{wavelength}", f"dff_{wavelength}_percent", f"zscore_{wavelength}",
            ])
        for column in signal_columns:
            if column not in self.processed:
                continue
            original = self.processed[column].to_numpy(float)[indices]
            if self.applied_smooth_seconds > 0:
                frame[f"{column}_unsmoothed"] = original
            frame[column] = self.display_values(column)[indices]
        limits, _label = self.resolve_export_annotation()
        frame["export_window"] = 0
        if limits is not None:
            plotted_time = frame["relative_time_min" if self.normalized else "time_min"].to_numpy(float)
            frame.loc[(plotted_time >= limits[0]) & (plotted_time <= limits[1]), "export_window"] = 1
        return frame, indices

    def save_results(self) -> None:
        from tkinter import messagebox
        if self.processed is None or self.details is None or self.folder is None:
            messagebox.showinfo("No Results", "Complete fitting and apply the correction first.")
            return
        selections = {key: var.get() for key, var in self.export_vars.items()}
        if not any(selections.values()):
            messagebox.showinfo("No Output Selected", "Select at least one CSV or PNG output.")
            return
        normalization_requested = any(selections[key] for key in (
            "dff_csv", "dff_png", "dff_svg", "zscore_csv", "zscore_png", "zscore_svg"
        ))
        event_requested = any(selections[key] for key in ("event_csv", "event_png", "event_svg"))
        if event_requested and (self.event_trials is None or self.event_average is None or self.event_details is None):
            messagebox.showinfo(
                "Event Analysis Required",
                "Select the marker and event intervals, then calculate the event analysis first.",
            )
            return
        if event_requested and self.event_details is not None:
            try:
                current_event = (*self.current_event_settings(), self.smoothing_seconds())
            except Exception as exc:
                messagebox.showerror("Invalid Event Settings", str(exc)); return
            saved_event = (
                self.event_details["marker_name"], self.event_details["pre_seconds"],
                self.event_details["post_seconds"], self.event_details["baseline_seconds"],
                tuple(self.event_details.get("selected_marker_ids", [])),
                tuple(self.event_details.get("selected_signal_keys", [])),
                self.event_details.get("smoothing_seconds", 0.0),
            )
            if current_event != saved_event:
                messagebox.showinfo(
                    "Event Settings Changed",
                    "The signal selection, marker selection, event intervals, baseline, or smoothing has changed. "
                    "Recalculate event analysis.",
                )
                return
        if normalization_requested and not self.normalized:
            messagebox.showinfo("Normalization Required", "Set the baseline interval and calculate dF/F0 and Z-score first.")
            return
        if normalization_requested and self.details is not None:
            normalization = self.details.get("normalization")
            try:
                baseline_start, baseline_end, _ = self.resolve_baseline_interval()
                zero_time, _ = self.resolve_zero_time()
                current_normalization = (baseline_start, baseline_end, self.smoothing_seconds(), zero_time)
            except Exception as exc:
                messagebox.showerror("Invalid Normalization Settings", str(exc)); return
            saved_normalization = (normalization["baseline_start_min"], normalization["baseline_end_min"],
                                   normalization["smooth_seconds"], normalization.get("zero_time_min")) if normalization else None
            if saved_normalization != current_normalization:
                messagebox.showinfo("Normalization Settings Changed", "The baseline, smoothing settings, or time zero has changed. Recalculate dF/F0 and Z-score.")
                return
        try:
            self.display_cache.clear()
            pairs = [("combined_signal", "combined_signal_smoothed"),
                     ("analysis_trace", "analysis_trace_smoothed"),
                     ("dff_percent", "dff_percent_smoothed"),
                     ("zscore", "zscore_smoothed")]
            for wavelength in WAVELENGTHS:
                pairs.extend([
                    (f"corrected_{wavelength}", f"corrected_{wavelength}_smoothed"),
                    (f"dff_{wavelength}_percent", f"dff_{wavelength}_percent_smoothed"),
                    (f"zscore_{wavelength}", f"zscore_{wavelength}_smoothed"),
                ])
            for raw, smoothed in pairs:
                if raw in self.processed:
                    self.processed[smoothed] = self.smooth_array(self.processed[raw].to_numpy(float))
            export_frame, _export_indices = self.prepared_export_frame()
            output = self.next_output_directory()
            output.mkdir(parents=True, exist_ok=True)
            pd.DataFrame(self.sorted_markers()).to_csv(output / "markers_working_copy.csv", index=False)
            if selections["event_csv"]:
                assert self.event_trials is not None and self.event_average is not None
                self.event_trials_for_export().to_csv(
                    output / "event_aligned_trials.csv", index=False, float_format="%.9g"
                )
                self.event_average_for_export().to_csv(
                    output / "event_aligned_average.csv", index=False, float_format="%.9g"
                )
            time_columns = ["original_time_s", "original_time_min", "relative_time_s", "relative_time_min"]
            if selections["corrected_csv"]:
                corrected_columns = list(time_columns)
                for wavelength in self.available_wavelengths:
                    corrected_columns.append(f"corrected_{wavelength}")
                    if f"corrected_{wavelength}_unsmoothed" in export_frame:
                        corrected_columns.append(f"corrected_{wavelength}_unsmoothed")
                if self.reference_wavelength_var.get() != "None":
                    corrected_columns.append("combined_signal")
                    if "combined_signal_unsmoothed" in export_frame:
                        corrected_columns.append("combined_signal_unsmoothed")
                corrected_columns.append("analysis_trace")
                if "analysis_trace_unsmoothed" in export_frame:
                    corrected_columns.append("analysis_trace_unsmoothed")
                corrected_columns.append("export_window")
                export_frame[corrected_columns].to_csv(
                    output / "corrected_fluorescence_trace.csv", index=False, float_format="%.9g"
                )
            if selections["dff_csv"]:
                dff_columns = time_columns + ["dff_percent"]
                for wavelength in self.available_wavelengths:
                    dff_columns.append(f"dff_{wavelength}_percent")
                    for column in (f"dff_{wavelength}_percent_unsmoothed",):
                        if column in export_frame:
                            dff_columns.append(column)
                if "dff_percent_unsmoothed" in export_frame:
                    dff_columns.append("dff_percent_unsmoothed")
                dff_columns.extend(["normalization_baseline", "export_window"])
                export_frame[dff_columns].to_csv(
                    output / "dFF0_trace.csv", index=False, float_format="%.9g"
                )
            if selections["zscore_csv"]:
                zscore_columns = time_columns + ["zscore"]
                for wavelength in self.available_wavelengths:
                    zscore_columns.append(f"zscore_{wavelength}")
                    for column in (f"zscore_{wavelength}_unsmoothed",):
                        if column in export_frame:
                            zscore_columns.append(column)
                if "zscore_unsmoothed" in export_frame:
                    zscore_columns.append("zscore_unsmoothed")
                zscore_columns.extend(["normalization_baseline", "export_window"])
                export_frame[zscore_columns].to_csv(
                    output / "zscore_trace.csv", index=False, float_format="%.9g"
                )

            annotation_limits, annotation_label = self.resolve_export_annotation()
            if selections["corrected_png"]:
                corrected_panels = [
                    (f"Corrected {wavelength}", self.display_values(f"corrected_{wavelength}"),
                     WAVELENGTH_COLORS[wavelength][1], f"corrected{wavelength}")
                    for wavelength in self.available_wavelengths
                ]
                if self.reference_wavelength_var.get() != "None":
                    corrected_panels.append((self.details.get("combined_label") or "Analysis trace",
                                             self.display_values("analysis_trace"), "#7B4B94", "combined"))
                self.save_trace_png(output / "corrected_fluorescence_trace.png", corrected_panels,
                                    "Corrected fluorescence traces", annotation_limits)
            if selections["dff_png"] or selections["dff_svg"]:
                dff_panels = [
                    (f"{wavelength} dF/F0 (%)", self.display_values(f"dff_{wavelength}_percent"),
                     WAVELENGTH_COLORS[wavelength][1], "dff")
                    for wavelength in self.available_wavelengths
                ]
                if self.reference_wavelength_var.get() != "None":
                    dff_panels.append(((self.details.get("combined_label") or "Analysis trace") + " dF/F0 (%)",
                                       self.display_values("dff_percent"), "#7B4B94", "dff"))
                if selections["dff_png"]:
                    self.save_trace_png(output / "dFF0_trace.png", dff_panels, "dF/F0", annotation_limits)
                if selections["dff_svg"]:
                    self.save_trace_png(output / "dFF0_trace.svg", dff_panels, "dF/F0", annotation_limits)
            if selections["zscore_png"] or selections["zscore_svg"]:
                z_panels = [
                    (f"{wavelength} Z-score", self.display_values(f"zscore_{wavelength}"),
                     WAVELENGTH_COLORS[wavelength][1], "zscore")
                    for wavelength in self.available_wavelengths
                ]
                if self.reference_wavelength_var.get() != "None":
                    z_panels.append(((self.details.get("combined_label") or "Analysis trace") + " Z-score",
                                     self.display_values("zscore"), "#7B4B94", "zscore"))
                if selections["zscore_png"]:
                    self.save_trace_png(output / "zscore_trace.png", z_panels, "Z-score", annotation_limits)
                if selections["zscore_svg"]:
                    self.save_trace_png(output / "zscore_trace.svg", z_panels, "Z-score", annotation_limits)
            if selections["event_png"]:
                self.save_event_analysis_figure(output / "event_aligned_dFF_zscore.png")
            if selections["event_svg"]:
                self.save_event_analysis_figure(output / "event_aligned_dFF_zscore.svg")

            marker_values = np.zeros(len(export_frame), dtype=int)
            time_values = export_frame["relative_time_s"].to_numpy(float)
            for marker in self.markers:
                marker_index = int(np.argmin(np.abs(export_frame["original_time_min"].to_numpy(float)
                                                    - float(marker["time_min"]))))
                marker_values[marker_index] = 1
            aa_exports: dict[str, np.ndarray] = {}
            if selections["corrected_csv"]:
                aa_exports["AAPlot_corrected_trace"] = export_frame["analysis_trace"].to_numpy(float)
            if selections["dff_csv"]:
                aa_exports["AAPlot_dFF0"] = export_frame["dff_percent"].to_numpy(float)
            if selections["zscore_csv"]:
                aa_exports["AAPlot_zscore"] = export_frame["zscore"].to_numpy(float)
            aa_file_name = f"{self.folder.name}.txt"
            for subfolder, signal in aa_exports.items():
                aa_dir = output / subfolder
                aa_dir.mkdir(parents=True, exist_ok=True)
                pd.DataFrame({"Time": time_values, "Signal": signal, "Marker": marker_values}).to_csv(
                    aa_dir / aa_file_name, index=False
                )
            current_ids = {marker["id"] for marker in self.markers}
            log = {
                "source_folder": str(self.folder), "source_files_modified": False,
                "processing": asdict(self.current_config()), "fit_details": self.details,
                "event_analysis": self.event_details,
                "selected_outputs": selections,
                "export_annotation": {"mode": annotation_label, "limits_in_display_min": annotation_limits},
                "display_processing": {
                    "smooth_enabled": self.applied_smooth_seconds > 0,
                    "smooth_seconds": self.smoothing_seconds(),
                    "downsample_enabled": self.applied_downsample_enabled,
                    "downsample_mode": self.applied_downsample_mode,
                    "downsample_value": (self.applied_downsample_value
                                         if self.applied_downsample_enabled else None),
                    "exported_rows": int(len(export_frame)),
                    "source_rows_before_downsample": int(len(self.processed)),
                    "line_widths": {key: self.line_width(key) for key in LINE_WIDTH_LABELS},
                    "note": (
                        "Applied smoothing and downsampling affect whole-recording screen, PNG, CSV, and AAPlot outputs. "
                        "Event analysis uses the applied smoothing but retains its full marker-aligned sampling grid."
                    ),
                },
                "marker_edits": {
                    "deleted_original_markers": [m for m in self.original_markers if m["id"] not in current_ids],
                    "manual_markers": [m for m in self.markers if m["source"] == "manual"],
                },
                "aaplot_exports": {
                    "reader": r"D:\Coding\AAPlot\GUI\fiber_trace_spike2_analysis_batch_gui.py",
                    "format": "comma-separated .txt with first three columns Time(seconds), Signal, Marker",
                    "folders": list(aa_exports),
                    "note": "Signal types are separated into folders so AAPlot does not treat them as separate animals in one batch.",
                },
                "source_metadata": self.metadata,
            }
            (output / "parameters_and_marker_edits.json").write_text(json.dumps(log, indent=2, ensure_ascii=False), encoding="utf-8")
            self.vars["status"].set(f"Saved: {output}")
            messagebox.showinfo("Save Complete", f"{output}\n\nThe original data were not modified.")
        except Exception as exc:
            messagebox.showerror("Save Failed", str(exc))


def main() -> int:
    try:
        import tkinter as tk
        root = tk.Tk(); PretreatmentApp(root); root.mainloop(); return 0
    except Exception as exc:
        print(f"GUI failed to start: {exc}", file=sys.stderr); return 1


if __name__ == "__main__":
    raise SystemExit(main())
