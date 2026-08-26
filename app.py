#!/usr/bin/env python
"""Interactive GUI for independent 410/470 fiber-photometry pretreatment."""
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
    calculate_normalized_traces,
    fit_bleaching,
    load_recording,
    process_data,
    region_mask,
    select_effective_data,
)


DEFAULT_STRINGS = {
    "folder": "", "range_start": "0", "range_end": "0", "offset410": "0", "offset470": "0",
    "baseline410": "", "baseline470": "", "method": "fit_both", "combine": "ratio",
    "fit_model": "double_exponential", "smooth": "10", "fit410_status": "Not set",
    "fit470_status": "Not set", "norm_start": "0", "norm_end": "1",
    "norm_pre_duration": "5", "zero_time": "0", "downsample_value": "1",
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
    "combined": "Ratio / subtraction",
    "dff": "dF/F0",
    "zscore": "Z-score",
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
        self.window.title(f"{channel} fitting regions")
        self.window.geometry("1200x760")
        subset = select_effective_data(data, config)
        self.time_s = subset["TimeStamp"].to_numpy(float) / 1000
        self.time_min = self.time_s / 60
        raw = subset[f"CH1-{channel}"].to_numpy(float)
        offset = config.offset_470 if channel == "470" else config.offset_410
        self.baseline = config.baseline_470 if channel == "470" else config.baseline_410
        self.values = raw - offset
        self.regions = [tuple(sorted(region)) for region in existing_regions]
        start, end = float(self.time_min[0]), float(self.time_min[-1])
        if not self.regions:
            self.regions = [(start + 0.15 * (end - start), start + 0.35 * (end - start))]
        initial_model = config.fit_model_470 if channel == "470" else config.fit_model_410
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
        ttk.Label(left, text=f"User baseline: {self.baseline:g}", justify="left").pack(anchor="w", pady=(2, 5))
        ttk.Label(left, text="Fitting model").pack(anchor="w")
        self.model_box = ttk.Combobox(
            left, textvariable=self.model_var,
            values=["linear", "single_exponential", "double_exponential"], state="readonly",
        )
        self.model_box.pack(fill=tk.X, pady=(0, 7))
        self.model_box.bind("<<ComboboxSelected>>", self.invalidate_preview)
        ttk.Label(left, text="Baseline: initial value for a freely fitted constant (fit_constant)",
                  wraplength=250).pack(anchor="w", pady=(0, 5))
        ttk.Button(left, text="Add region", command=self.add_region).pack(fill=tk.X, pady=2)
        ttk.Button(left, text="Delete selected region", command=self.delete_region).pack(fill=tk.X, pady=2)
        self.region_list = tk.Listbox(left, height=12, exportselection=False)
        self.region_list.pack(fill=tk.BOTH, expand=True, pady=6)
        self.region_list.bind("<<ListboxSelect>>", self.list_selection_changed)
        ttk.Button(left, text="Preview fit", command=self.preview_fit).pack(fill=tk.X, pady=2)
        ttk.Button(left, text="Confirm and save regions", command=self.accept).pack(fill=tk.X, pady=2)
        ttk.Label(left, textvariable=self.readout, wraplength=250).pack(anchor="w", pady=(8, 0))

        self.figure = Figure(figsize=(9, 7), dpi=100, constrained_layout=True)
        self.ax = self.figure.subplots(1, 1)
        self.canvas = FigureCanvasTkAgg(self.figure, master=right)
        toolbar = NavigationToolbar2Tk(self.canvas, right, pack_toolbar=False)
        toolbar.update(); toolbar.pack(fill=tk.X)
        self.canvas.get_tk_widget().pack(fill=tk.BOTH, expand=True)
        fit_footer = ttk.Frame(right, padding=(8, 4)); fit_footer.pack(fill=tk.X)
        ttk.Label(fit_footer, text="Signal width").pack(side=tk.LEFT)
        ttk.Scale(fit_footer, from_=0.4, to=3.0, variable=self.signal_width_var,
                  command=lambda _value: self.fit_line_width_changed(), length=150).pack(side=tk.LEFT, padx=6)
        ttk.Label(fit_footer, textvariable=self.signal_width_text, width=4).pack(side=tk.LEFT)
        ttk.Label(fit_footer, text="Fit width").pack(side=tk.LEFT, padx=(16, 0))
        ttk.Scale(fit_footer, from_=0.4, to=3.0, variable=self.fit_width_var,
                  command=lambda _value: self.fit_line_width_changed(), length=150).pack(side=tk.LEFT, padx=6)
        ttk.Label(fit_footer, textvariable=self.fit_width_text, width=4).pack(side=tk.LEFT)
        self.canvas.mpl_connect("button_press_event", self.press)
        self.canvas.mpl_connect("motion_notify_event", self.motion)
        self.canvas.mpl_connect("button_release_event", self.release)
        self.refresh_list(); self.draw()

    def signature(self) -> tuple[Any, ...]:
        offset = self.config.offset_470 if self.channel == "470" else self.config.offset_410
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
        color = "#2E8B57" if self.channel == "470" else "#2F6FB0"
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
        root.title("RWD Fiber Pretreatment")
        root.geometry("1650x950")
        root.minsize(1120, 720)
        self.folder: Path | None = None
        self.data: pd.DataFrame | None = None
        self.metadata = ""
        self.original_markers: list[dict[str, Any]] = []
        self.markers: list[dict[str, Any]] = []
        self.processed: pd.DataFrame | None = None
        self.details: dict[str, Any] | None = None
        self.fit_regions = {"410": [], "470": []}
        self.fit_signatures: dict[str, tuple[Any, ...] | None] = {"410": None, "470": None}
        self.fit_models: dict[str, str | None] = {"410": None, "470": None}
        self.fit_baseline_modes: dict[str, str | None] = {"410": "fit_constant", "470": "fit_constant"}
        self.axes: list[Any] = []
        self.axis_keys: list[str] = []
        self.normalized = False
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
        self.export_vars = {
            "corrected_csv": tk.BooleanVar(value=False), "corrected_png": tk.BooleanVar(value=True),
            "dff_csv": tk.BooleanVar(value=True), "dff_png": tk.BooleanVar(value=True),
            "zscore_csv": tk.BooleanVar(value=True), "zscore_png": tk.BooleanVar(value=True),
        }

        style = ttk.Style(root)
        if "clam" in style.theme_names():
            style.theme_use("clam")
        style.configure("TNotebook.Tab", padding=(14, 7))
        style.configure("TLabelframe", padding=8)
        style.configure("TLabelframe.Label", font=("Segoe UI", 10, "bold"))
        style.configure("Accent.TButton", font=("Segoe UI", 10, "bold"), padding=(8, 6))

        pane = ttk.Panedwindow(root, orient=tk.HORIZONTAL); pane.pack(fill=tk.BOTH, expand=True, padx=8, pady=8)
        controls, plot_frame = ttk.Frame(pane, width=390), ttk.Frame(pane)
        pane.add(controls, weight=0); pane.add(plot_frame, weight=1)
        notebook = ttk.Notebook(controls)
        notebook.pack(fill=tk.BOTH, expand=True)
        data_tab, display_tab, export_tab = ttk.Frame(notebook, padding=10), ttk.Frame(notebook, padding=10), ttk.Frame(notebook, padding=10)
        notebook.add(data_tab, text="Data & Fitting")
        notebook.add(display_tab, text="Display & Normalization")
        notebook.add(export_tab, text="Export")

        row = 0
        ttk.Button(data_tab, text="Select Recording Folder", command=self.open_folder, style="Accent.TButton").grid(row=row, column=0, columnspan=2, sticky="ew"); row += 1
        ttk.Label(data_tab, textvariable=self.vars["folder"], wraplength=350).grid(row=row, column=0, columnspan=2, sticky="w", pady=(3, 9)); row += 1
        row = self.section(data_tab, row, "Valid Data Range (min)")
        row = self.entry(data_tab, row, "Start", "range_start"); row = self.entry(data_tab, row, "End", "range_end")
        row = self.section(data_tab, row, "User-Defined Parameters")
        for label, key in [("410 offset", "offset410"), ("470 offset", "offset470"),
                           ("410 baseline", "baseline410"), ("470 baseline", "baseline470")]:
            row = self.entry(data_tab, row, label, key)
        row = self.section(data_tab, row, "Correction Method")
        ttk.Radiobutton(data_tab, text="Fit 410 and 470 separately", variable=self.vars["method"], value="fit_both", command=self.method_changed).grid(row=row, column=0, columnspan=2, sticky="w"); row += 1
        ttk.Radiobutton(data_tab, text="Fit 470 only", variable=self.vars["method"], value="fit_470_only", command=self.method_changed).grid(row=row, column=0, columnspan=2, sticky="w"); row += 1
        ttk.Label(data_tab, text="Combination").grid(row=row, column=0, sticky="w")
        self.combine_box = ttk.Combobox(data_tab, textvariable=self.vars["combine"], values=["ratio", "subtraction"], state="readonly", width=19)
        self.combine_box.grid(row=row, column=1, sticky="ew"); row += 1
        ttk.Button(data_tab, text="Open 470 Fitting Window", command=lambda: self.open_fit("470")).grid(row=row, column=0, sticky="ew", pady=2)
        ttk.Label(data_tab, textvariable=self.vars["fit470_status"], wraplength=180).grid(row=row, column=1, sticky="w"); row += 1
        self.fit410_button = ttk.Button(data_tab, text="Open 410 Fitting Window", command=lambda: self.open_fit("410"))
        self.fit410_button.grid(row=row, column=0, sticky="ew", pady=2)
        ttk.Label(data_tab, textvariable=self.vars["fit410_status"], wraplength=180).grid(row=row, column=1, sticky="w"); row += 1
        ttk.Button(data_tab, text="Apply Fit and Update Traces", command=self.apply_processing, style="Accent.TButton").grid(row=row, column=0, columnspan=2, sticky="ew", pady=(7, 2)); row += 1
        row = self.section(data_tab, row, "Marker Working Copy")
        self.marker_list = tk.Listbox(data_tab, height=5, exportselection=False, relief="flat", highlightthickness=1)
        self.marker_list.grid(row=row, column=0, columnspan=2, sticky="nsew", pady=(2, 5)); row += 1
        row = self.entry(data_tab, row, "Time (min)", "marker_time"); row = self.entry(data_tab, row, "Name", "marker_name")
        marker_buttons = ttk.Frame(data_tab); marker_buttons.grid(row=row, column=0, columnspan=2, sticky="ew")
        for text, command in [("Add", self.add_marker), ("Delete", self.delete_marker), ("Restore Original", self.reset_markers)]:
            ttk.Button(marker_buttons, text=text, command=command).pack(side=tk.LEFT, fill=tk.X, expand=True, padx=1)
        data_tab.columnconfigure(1, weight=1)

        row = 0
        row = self.section(display_tab, row, "Fluorescence Trace Layers")
        layer_row = ttk.Frame(display_tab); layer_row.grid(row=row, column=0, columnspan=2, sticky="w"); row += 1
        for text, value in [("Raw + Corrected", "overlay"), ("Raw Only", "raw_only"), ("Corrected Only", "corrected_only")]:
            ttk.Radiobutton(layer_row, text=text, variable=self.trace_view_var, value=value,
                            command=self.redraw_current).pack(side=tk.LEFT)
        ttk.Checkbutton(display_tab, text="Show fitted curve (red dashed line)", variable=self.show_fit_var,
                        command=self.redraw_current).grid(row=row, column=0, columnspan=2, sticky="w"); row += 1
        row = self.section(display_tab, row, "Smooth / Downsample (Applied on Demand)")
        ttk.Checkbutton(display_tab, text="Enable Smooth", variable=self.smooth_enabled_var).grid(row=row, column=0, sticky="w")
        ttk.Entry(display_tab, textvariable=self.vars["smooth"], width=10).grid(row=row, column=1, sticky="ew"); row += 1
        ttk.Label(display_tab, text="Smooth Window (s)").grid(row=row, column=0, columnspan=2, sticky="w"); row += 1
        ttk.Checkbutton(display_tab, text="Enable Downsample", variable=self.downsample_enabled_var).grid(row=row, column=0, sticky="w")
        ttk.Combobox(display_tab, textvariable=self.downsample_mode_var, values=["points", "seconds"],
                     state="readonly", width=10).grid(row=row, column=1, sticky="ew"); row += 1
        row = self.entry(display_tab, row, "Downsample Interval", "downsample_value")
        ttk.Button(display_tab, text="Apply Smooth / Downsample", command=self.display_settings_changed).grid(
            row=row, column=0, columnspan=2, sticky="ew", pady=(2, 4)); row += 1
        row = self.section(display_tab, row, "dF/F0 and Z-score Baseline")
        baseline_modes = ttk.Frame(display_tab); baseline_modes.grid(row=row, column=0, columnspan=2, sticky="w"); row += 1
        ttk.Radiobutton(baseline_modes, text="Manual", variable=self.norm_mode_var,
                        value="manual").pack(side=tk.LEFT)
        ttk.Radiobutton(baseline_modes, text="Pre-Marker Interval", variable=self.norm_mode_var,
                        value="marker_before").pack(side=tk.LEFT)
        row = self.entry(display_tab, row, "Manual Start (original min)", "norm_start")
        row = self.entry(display_tab, row, "Manual End (original min)", "norm_end")
        ttk.Label(display_tab, text="Baseline Marker").grid(row=row, column=0, sticky="w")
        self.norm_marker_box = ttk.Combobox(display_tab, textvariable=self.norm_marker_var,
                                            state="readonly", width=20)
        self.norm_marker_box.grid(row=row, column=1, sticky="ew"); row += 1
        row = self.entry(display_tab, row, "Pre-Marker Duration (min)", "norm_pre_duration")
        row = self.section(display_tab, row, "Optional Time Zero")
        ttk.Checkbutton(display_tab, text="Enable Relative Time", variable=self.zero_enabled_var).grid(
            row=row, column=0, columnspan=2, sticky="w"); row += 1
        zero_modes = ttk.Frame(display_tab); zero_modes.grid(row=row, column=0, columnspan=2, sticky="w"); row += 1
        ttk.Radiobutton(zero_modes, text="Marker as Time 0", variable=self.zero_mode_var, value="marker").pack(side=tk.LEFT)
        ttk.Radiobutton(zero_modes, text="Specified Time as 0", variable=self.zero_mode_var, value="time").pack(side=tk.LEFT)
        ttk.Label(display_tab, text="Zero Marker").grid(row=row, column=0, sticky="w")
        self.zero_marker_box = ttk.Combobox(display_tab, textvariable=self.zero_marker_var,
                                            state="readonly", width=20)
        self.zero_marker_box.grid(row=row, column=1, sticky="ew"); row += 1
        row = self.entry(display_tab, row, "Specified Time (original min)", "zero_time")
        ttk.Button(display_tab, text="Calculate dF/F0 and Z-score", command=self.calculate_normalization,
                   style="Accent.TButton").grid(row=row, column=0, columnspan=2, sticky="ew", pady=(5, 2)); row += 1
        ttk.Label(display_tab, text="Drag the bottom X-axis to pan horizontally; drag a plot's left Y-axis to pan it vertically. Box zoom works only inside a plot.",
                   wraplength=350).grid(row=row, column=0, columnspan=2, sticky="w", pady=(8, 0))
        display_tab.columnconfigure(1, weight=1)

        row = 0
        row = self.section(export_tab, row, "Select Outputs")
        export_labels = [("corrected_csv", "Corrected Fluorescence CSV"), ("corrected_png", "Corrected Fluorescence PNG"),
                         ("dff_csv", "dF/F0 CSV"), ("dff_png", "dF/F0 PNG"),
                         ("zscore_csv", "Z-score CSV"), ("zscore_png", "Z-score PNG")]
        for index, (key, label) in enumerate(export_labels):
            ttk.Checkbutton(export_tab, text=label, variable=self.export_vars[key]).grid(
                row=row + index // 2, column=index % 2, sticky="w", pady=2
            )
        row += 3
        row = self.entry(export_tab, row, "Output Folder Name", "output_name")
        ttk.Label(export_tab, text="The output folder is created inside the input folder; a number is appended if the name already exists.",
                  wraplength=350).grid(row=row, column=0, columnspan=2, sticky="w", pady=(0, 5)); row += 1
        ttk.Button(export_tab, text="Save Selected Results", command=self.save_results, style="Accent.TButton").grid(row=row, column=0, columnspan=2, sticky="ew", pady=(8, 4)); row += 1
        ttk.Label(export_tab, textvariable=self.vars["status"], wraplength=350).grid(row=row, column=0, columnspan=2, sticky="w")
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
        ttk.Label(axis_row, text="X Left (min)").pack(side=tk.LEFT)
        x_start_entry = ttk.Entry(axis_row, textvariable=self.vars["x_start"], width=9)
        x_start_entry.pack(side=tk.LEFT, padx=5)
        ttk.Label(axis_row, text="X Range (min)").pack(side=tk.LEFT)
        x_span_entry = ttk.Entry(axis_row, textvariable=self.vars["x_span"], width=9)
        x_span_entry.pack(side=tk.LEFT, padx=5)
        ttk.Label(axis_row, text="X Right (min)").pack(side=tk.LEFT)
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
        self.rebuild_axes(); self.method_changed()

    def section(self, parent: Any, row: int, text: str) -> int:
        self.ttk.Separator(parent).grid(row=row, column=0, columnspan=2, sticky="ew", pady=(5, 3)); row += 1
        self.ttk.Label(parent, text=text).grid(row=row, column=0, columnspan=2, sticky="w")
        return row + 1

    def entry(self, parent: Any, row: int, label: str, key: str) -> int:
        self.ttk.Label(parent, text=label).grid(row=row, column=0, sticky="w", padx=(0, 5))
        self.ttk.Entry(parent, textvariable=self.vars[key], width=17).grid(row=row, column=1, sticky="ew")
        return row + 1

    def number(self, key: str) -> float:
        try:
            value = float(self.vars[key].get())
        except ValueError as exc:
            raise ValueError(f"Enter a value for {key}.") from exc
        if not math.isfinite(value):
            raise ValueError(f"{key} must be a finite number.")
        return value

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
        export_defaults = {
            "corrected_csv": False, "corrected_png": True,
            "dff_csv": True, "dff_png": True, "zscore_csv": True, "zscore_png": True,
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
        self.fit_regions = {"410": [], "470": []}
        self.fit_signatures = {"410": None, "470": None}
        self.fit_models = {"410": None, "470": None}
        self.fit_baseline_modes = {"410": "fit_constant", "470": "fit_constant"}
        self.processed = None
        self.details = None
        self.normalized = False
        self.display_cache.clear()

    def current_config(self) -> ProcessingConfig:
        method = self.vars["method"].get()
        offset410 = self.number("offset410") if method == "fit_both" else 0.0
        baseline410 = self.number("baseline410") if method == "fit_both" else 1.0
        smooth_seconds = self.smoothing_seconds()
        config = ProcessingConfig(
            self.number("range_start"), self.number("range_end"), offset410, self.number("offset470"),
            baseline410, self.number("baseline470"), method,
            self.vars["combine"].get(), self.vars["fit_model"].get(), smooth_seconds,
            list(self.fit_regions["410"]), list(self.fit_regions["470"]),
            self.fit_models["410"], self.fit_models["470"],
            self.fit_baseline_modes["410"], self.fit_baseline_modes["470"],
        )
        if config.range_end_min <= config.range_start_min:
            raise ValueError("The valid range end must be greater than the start.")
        if config.smooth_seconds < 0:
            raise ValueError("The smoothing duration cannot be negative.")
        return config

    def pending_smoothing_seconds(self) -> float:
        value = self.number("smooth") if self.smooth_enabled_var.get() else 0.0
        if value < 0:
            raise ValueError("The Smooth duration cannot be negative.")
        return value

    def smoothing_seconds(self) -> float:
        return float(self.applied_smooth_seconds)

    def pending_downsample_settings(self) -> tuple[bool, str, float]:
        enabled = bool(self.downsample_enabled_var.get())
        mode = self.downsample_mode_var.get()
        if mode not in {"points", "seconds"}:
            raise ValueError("Invalid Downsample mode.")
        value = self.number("downsample_value") if enabled else 1.0
        if value <= 0:
            raise ValueError("The Downsample interval must be greater than 0.")
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
                for raw, smoothed in (("combined_signal", "combined_signal_smoothed"),
                                      ("analysis_trace", "analysis_trace_smoothed"),
                                      ("dff_percent", "dff_percent_smoothed"),
                                      ("zscore", "zscore_smoothed")):
                    if raw in self.processed:
                        self.processed[smoothed] = self.smooth_array(self.processed[raw].to_numpy(float))
                if self.normalized and self.details and self.details.get("normalization"):
                    self.details["normalization"]["smooth_seconds"] = self.smoothing_seconds()
            self.redraw_current()
            downsample_text = (f"{downsample_value:g} {downsample_mode}"
                               if downsample_enabled else "Off")
            self.vars["status"].set(
                f"Display processing applied: Smooth={smooth_seconds:g}s, Downsample={downsample_text}."
            )
        except Exception as exc:
            messagebox.showerror("Invalid Display Processing Settings", str(exc))

    def expected_signature(self, channel: str, config: ProcessingConfig) -> tuple[Any, ...]:
        offset = config.offset_470 if channel == "470" else config.offset_410
        baseline = config.baseline_470 if channel == "470" else config.baseline_410
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
            self.original_markers = [dict(marker) for marker in markers]
            self.markers = [dict(marker) for marker in markers]
            self.vars["folder"].set(str(self.folder))
            self.vars["range_start"].set("0")
            recording_end = float(self.data["TimeStamp"].iloc[-1]) / 60000
            self.vars["range_end"].set(f"{recording_end:.5f}")
            self.vars["norm_start"].set("0")
            self.vars["norm_end"].set(f"{min(1.0, recording_end):.5f}")
            self.vars["output_name"].set(f"output_{self.folder.name}")
            self.refresh_markers(); self.method_changed()
            self.vars["status"].set(
                "Data loaded. The current display is not smoothed or downsampled. Enter the baseline and set the fitting regions."
            )
        except Exception as exc:
            messagebox.showerror("Load Failed", str(exc))

    def method_changed(self) -> None:
        fit_both = self.vars["method"].get() == "fit_both"
        self.fit410_button.configure(state="normal" if fit_both else "disabled")
        self.combine_box.configure(state="readonly" if fit_both else "disabled")
        self.rebuild_axes();
        if self.processed is not None:
            self.draw_processed()
        elif self.data is not None:
            self.draw_raw_preview()

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
            required = ["470"] + (["410"] if config.method == "fit_both" else [])
            for channel in required:
                if not self.fit_regions[channel]:
                    raise ValueError(f"Select and confirm fitting regions in the {channel} fitting window first.")
                if self.fit_signatures[channel] != self.expected_signature(channel, config):
                    raise ValueError(f"The {channel} range, offset, baseline, or fitting model has changed. Reconfirm this channel's fit.")
            self.vars["status"].set("Fitting..."); self.root.update_idletasks()
            self.processed, self.details = process_data(self.data, config)
            self.normalized = False
            self.display_cache.clear()
            self.rebuild_axes(); self.draw_processed()
            self.vars["status"].set(
                f"Correction complete: {len(self.processed):,} points. The current display remains unsmoothed and not downsampled. "
                "Apply display processing from the Display & Normalization tab if needed."
            )
        except Exception as exc:
            messagebox.showerror("Processing Failed", str(exc)); self.vars["status"].set(f"Processing failed: {exc}")

    def rebuild_axes(self) -> None:
        method = self.vars["method"].get()
        keys = ["470"]
        if method == "fit_both":
            keys.append("410")
            keys.append(self.vars["combine"].get())
        if self.normalized:
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

    def draw_raw_preview(self) -> None:
        if self.data is None:
            return
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
        for ax, key in zip(self.axes, self.axis_keys):
            ax.clear()
            if key == "470":
                values = subset["CH1-470"].to_numpy(float)
                ax.plot(x, values, color="#2E8B57", lw=self.line_width("raw470"),
                        gid="raw470"); ax.set_ylabel("Raw 470")
            elif key == "410":
                values = subset["CH1-410"].to_numpy(float)
                ax.plot(x, values, color="#2F6FB0", lw=self.line_width("raw410"),
                        gid="raw410"); ax.set_ylabel("Raw 410")
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
        d = self.processed; indices = self.display_indices(len(d))
        time_column = "relative_time_min" if self.normalized else "time_min"
        x = d[time_column].to_numpy(float)[indices]
        view = self.trace_view_var.get()
        for ax, key in zip(self.axes, self.axis_keys):
            ax.clear()
            if key == "470":
                if view != "corrected_only":
                    ax.plot(x, self.display_values("offset_adjusted_470")[indices], color="#4BAF72",
                            lw=self.line_width("raw470"), alpha=0.72,
                            label="Original 470 after offset", gid="raw470")
                if self.show_fit_var.get():
                    ax.plot(x, d["fit_470"].to_numpy()[indices], color="#D62728", ls="--",
                            lw=self.line_width("fit470"), label="470 fit", gid="fit470")
                if view != "raw_only":
                    ax.plot(x, self.display_values("corrected_470")[indices], color="#006D3C",
                            lw=self.line_width("corrected470"), label="Corrected 470", gid="corrected470")
                ax.set_ylabel("470 fluorescence")
                ax.legend(frameon=False, ncol=3, loc="lower right", bbox_to_anchor=(1, 1.01),
                          borderaxespad=0, fontsize=8)
            elif key == "410":
                if view != "corrected_only":
                    ax.plot(x, self.display_values("offset_adjusted_410")[indices], color="#4D8FCC",
                            lw=self.line_width("raw410"), alpha=0.72,
                            label="Original 410 after offset", gid="raw410")
                if self.show_fit_var.get():
                    ax.plot(x, d["fit_410"].to_numpy()[indices], color="#D62728", ls="--",
                            lw=self.line_width("fit410"), label="410 fit", gid="fit410")
                if view != "raw_only":
                    ax.plot(x, self.display_values("corrected_410")[indices], color="#15558D",
                            lw=self.line_width("corrected410"), label="Corrected 410", gid="corrected410")
                ax.set_ylabel("410 fluorescence")
                ax.legend(frameon=False, ncol=3, loc="lower right", bbox_to_anchor=(1, 1.01),
                          borderaxespad=0, fontsize=8)
            elif key == "ratio":
                ax.plot(x, self.display_values("combined_signal")[indices], color="#7B4B94",
                        lw=self.line_width("combined"), gid="combined")
                ax.set_ylabel("470 / 410 ratio")
            elif key == "subtraction":
                ax.plot(x, self.display_values("combined_signal")[indices], color="#7B4B94",
                        lw=self.line_width("combined"), gid="combined")
                ax.set_ylabel("470 - 410")
            elif key == "dff":
                ax.plot(x, self.display_values("dff_percent")[indices], color="#7B4B94",
                        lw=self.line_width("dff"), gid="dff")
                ax.axhline(0, color="#777777", lw=0.7, alpha=0.7)
                ax.set_ylabel("dF/F0 (%)")
                self.shade_normalization_baseline(ax)
            elif key == "zscore":
                ax.plot(x, self.display_values("zscore")[indices], color="#C05A2A",
                        lw=self.line_width("zscore"), gid="zscore")
                ax.axhline(0, color="#777777", lw=0.7, alpha=0.7)
                ax.set_ylabel("Z-score")
                self.shade_normalization_baseline(ax)
            self.style_axis(ax)
        model_title = f"470={self.fit_models['470']}"
        if "410" in self.axis_keys:
            model_title += f" | 410={self.fit_models['410']}"
        self.figure.suptitle(f"RWD fiber pretreatment | {model_title}", fontsize=11)
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
                              (getattr(self, "zero_marker_box", None), self.zero_marker_var)):
            if box is not None:
                box.configure(values=choices)
            if choices and variable.get() not in choices:
                variable.set(choices[0])
            elif not choices:
                variable.set("")
        if not choices and self.zero_enabled_var.get() and self.zero_mode_var.get() == "marker":
            self.zero_mode_var.set("time")

    def add_marker(self) -> None:
        try:
            self.markers.append({"id": str(uuid.uuid4()), "time_min": self.number("marker_time"),
                                 "name": self.vars["marker_name"].get().strip() or "marker", "source": "manual"})
            self.refresh_markers(); self.redraw_current()
        except Exception as exc:
            self.vars["status"].set(f"Failed to add marker: {exc}")

    def delete_marker(self) -> None:
        selected = self.marker_list.curselection()
        if selected:
            target = self.sorted_markers()[selected[0]]
            self.markers = [marker for marker in self.markers if marker["id"] != target["id"]]
            self.refresh_markers(); self.redraw_current()

    def reset_markers(self) -> None:
        self.markers = [dict(marker) for marker in self.original_markers]
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
        if self.processed is not None:
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
        figure.savefig(path, dpi=220, facecolor="white")

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
        signal_columns = [
            "corrected_470", "corrected_410", "combined_signal", "analysis_trace",
            "dff_percent", "zscore",
        ]
        for column in signal_columns:
            if column not in self.processed:
                continue
            original = self.processed[column].to_numpy(float)[indices]
            if self.applied_smooth_seconds > 0:
                frame[f"{column}_unsmoothed"] = original
            frame[column] = self.display_values(column)[indices]
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
        normalization_requested = any(selections[key] for key in ("dff_csv", "dff_png", "zscore_csv", "zscore_png"))
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
                messagebox.showinfo("Normalization Settings Changed", "The baseline, Smooth settings, or time zero has changed. Recalculate dF/F0 and Z-score.")
                return
        try:
            self.display_cache.clear()
            for raw, smoothed in (("combined_signal", "combined_signal_smoothed"),
                                  ("analysis_trace", "analysis_trace_smoothed"),
                                  ("dff_percent", "dff_percent_smoothed"),
                                  ("zscore", "zscore_smoothed")):
                self.processed[smoothed] = self.smooth_array(self.processed[raw].to_numpy(float))
            export_frame, _export_indices = self.prepared_export_frame()
            output = self.next_output_directory()
            output.mkdir(parents=True, exist_ok=True)
            pd.DataFrame(self.sorted_markers()).to_csv(output / "markers_working_copy.csv", index=False)
            time_columns = ["original_time_s", "original_time_min", "relative_time_s", "relative_time_min"]
            if selections["corrected_csv"]:
                corrected_columns = time_columns + ["corrected_470"]
                if "corrected_470_unsmoothed" in export_frame:
                    corrected_columns.append("corrected_470_unsmoothed")
                if self.vars["method"].get() == "fit_both":
                    corrected_columns.append("corrected_410")
                    if "corrected_410_unsmoothed" in export_frame:
                        corrected_columns.append("corrected_410_unsmoothed")
                corrected_columns.append("analysis_trace")
                if "analysis_trace_unsmoothed" in export_frame:
                    corrected_columns.append("analysis_trace_unsmoothed")
                export_frame[corrected_columns].to_csv(
                    output / "corrected_fluorescence_trace.csv", index=False, float_format="%.9g"
                )
            if selections["dff_csv"]:
                dff_columns = time_columns + ["dff_percent"]
                if "dff_percent_unsmoothed" in export_frame:
                    dff_columns.append("dff_percent_unsmoothed")
                dff_columns.append("normalization_baseline")
                export_frame[dff_columns].to_csv(
                    output / "dFF0_trace.csv", index=False, float_format="%.9g"
                )
            if selections["zscore_csv"]:
                zscore_columns = time_columns + ["zscore"]
                if "zscore_unsmoothed" in export_frame:
                    zscore_columns.append("zscore_unsmoothed")
                zscore_columns.append("normalization_baseline")
                export_frame[zscore_columns].to_csv(
                    output / "zscore_trace.csv", index=False, float_format="%.9g"
                )

            if selections["corrected_png"]:
                corrected_panels = [
                    ("Corrected 470", self.display_values("corrected_470"), "#006D3C", "corrected470")
                ]
                if self.vars["method"].get() == "fit_both":
                    corrected_panels.append(
                        ("Corrected 410", self.display_values("corrected_410"), "#15558D", "corrected410")
                    )
                    corrected_panels.append((self.details.get("combined_label") or "Analysis trace",
                                             self.display_values("analysis_trace"), "#7B4B94", "combined"))
                self.save_trace_png(output / "corrected_fluorescence_trace.png", corrected_panels,
                                    "Corrected fluorescence traces")
            normalization = self.details.get("normalization") if self.details else None
            if normalization:
                zero_time = normalization.get("zero_time_min")
                shift = float(zero_time) if zero_time is not None else 0.0
                baseline_limits = (normalization["baseline_start_min"] - shift,
                                   normalization["baseline_end_min"] - shift)
            else:
                baseline_limits = None
            if selections["dff_png"]:
                self.save_trace_png(
                    output / "dFF0_trace.png",
                    [("dF/F0 (%)", self.display_values("dff_percent"), "#7B4B94", "dff")],
                    "dF/F0", baseline_limits,
                )
            if selections["zscore_png"]:
                self.save_trace_png(
                    output / "zscore_trace.png",
                    [("Z-score", self.display_values("zscore"), "#C05A2A", "zscore")],
                    "Z-score", baseline_limits,
                )

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
                "selected_outputs": selections,
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
                    "note": "Applied Smooth and Downsample affect screen, PNG, CSV, and AAPlot outputs.",
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
