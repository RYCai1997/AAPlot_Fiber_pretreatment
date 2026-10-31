#!/usr/bin/env python
"""Tkinter front-end for the Inper data.db -> InperView CSV converter.

Launch with:  python inper_transfer_gui.py
"""
from __future__ import annotations

import os
import subprocess
import sys
import queue
import threading
import tkinter as tk
from pathlib import Path
from tkinter import filedialog, messagebox, ttk

from inper_transfer import (  # noqa: E402
    EVENTS,
    FLUORESCENCE,
    OUTPUTS,
    UNALIGNED,
    convert_session,
)


def open_in_file_manager(path: str) -> None:
    """Reveal a folder in Explorer, Finder or the desktop's file manager.

    os.startfile exists only on Windows; on macOS it raises AttributeError,
    which is not an OSError and used to escape the caller's handler.
    """
    if sys.platform == "darwin":
        subprocess.Popen(["open", path])
    elif os.name == "nt":
        os.startfile(path)  # type: ignore[attr-defined]
    else:
        subprocess.Popen(["xdg-open", path])

PALETTE = {
    "window": "#eef2f5",
    "panel": "#f7f9fb",
    "text": "#1f2d38",
    "muted": "#66757f",
    "accent": "#2f6f9f",
    "accent_active": "#255a80",
    "border": "#cfd8de",
    "log_bg": "#fbfcfd",
}


class ConverterApp:
    def __init__(self, root: tk.Tk) -> None:
        self.root = root
        self.messages: queue.Queue[str] = queue.Queue()
        self.busy = False
        self.last_out: Path | None = None

        self.source_var = tk.StringVar()
        self.output_var = tk.StringVar()
        self.skip_var = tk.BooleanVar(value=False)
        self.replace_var = tk.BooleanVar(value=False)
        self.status_var = tk.StringVar(value="Select a session folder or a data.db file.")

        self._build()
        self._pump()

    # ------------------------------------------------------------------ UI --
    def _build(self) -> None:
        root = self.root
        root.title("Inper Data Converter — data.db to Fluorescence.csv")
        root.geometry("940x660")
        root.minsize(780, 540)
        root.configure(background=PALETTE["window"])

        style = ttk.Style(root)
        try:
            style.theme_use("clam")
        except tk.TclError:
            pass
        style.configure(".", background=PALETTE["window"], foreground=PALETTE["text"],
                        font=("Segoe UI", 9))
        style.configure("TFrame", background=PALETTE["window"])
        style.configure("Panel.TFrame", background=PALETTE["panel"])
        style.configure("TLabel", background=PALETTE["window"], foreground=PALETTE["text"],
                        font=("Segoe UI", 9))
        style.configure("Panel.TLabel", background=PALETTE["panel"], foreground=PALETTE["text"],
                        font=("Segoe UI", 9))
        style.configure("TCheckbutton", background=PALETTE["panel"], foreground=PALETTE["text"],
                        font=("Segoe UI", 9))
        style.configure("Hint.TLabel", background=PALETTE["panel"], foreground=PALETTE["muted"],
                        font=("Segoe UI", 8))
        style.configure("Status.TLabel", background=PALETTE["window"], foreground=PALETTE["accent"],
                        font=("Segoe UI", 9, "bold"))
        style.configure("HeaderTitle.TLabel", background=PALETTE["window"], foreground=PALETTE["text"],
                        font=("Segoe UI", 15, "bold"))
        style.configure("HeaderSubtitle.TLabel", background=PALETTE["window"], foreground=PALETTE["muted"],
                        font=("Segoe UI", 9))
        style.configure("TButton", font=("Segoe UI", 9), padding=(7, 4))
        style.configure("Accent.TButton", font=("Segoe UI", 10, "bold"), padding=(18, 8),
                        borderwidth=0, background=PALETTE["accent"], foreground="#ffffff")
        style.map("Accent.TButton",
                  background=[("active", PALETTE["accent_active"]), ("disabled", "#a9bcc8")],
                  foreground=[("disabled", "#f0f4f6")])
        style.configure("TLabelframe", background=PALETTE["panel"],
                        bordercolor=PALETTE["border"], borderwidth=1)
        style.configure("TLabelframe.Label", background=PALETTE["panel"],
                        foreground=PALETTE["text"], font=("Segoe UI", 10, "bold"))
        style.configure("TEntry", padding=4)
        style.configure("Horizontal.TProgressbar", background=PALETTE["accent"],
                        troughcolor=PALETTE["panel"], borderwidth=0)

        # ---- header ----
        header = ttk.Frame(root)
        header.pack(fill=tk.X, padx=14, pady=(12, 4))
        ttk.Label(header, text="Inper Data Converter", style="HeaderTitle.TLabel").pack(anchor="w")
        ttk.Label(header,
                  text="Convert InperView data.db into Fluorescence.csv / Fluorescence-unaligned.csv / "
                       "Events.csv / Outputs.csv",
                  style="HeaderSubtitle.TLabel").pack(anchor="w")

        body = ttk.Frame(root)
        body.pack(fill=tk.BOTH, expand=True, padx=14, pady=(4, 8))

        # ---- source ----
        src = ttk.Labelframe(body, text="Source", padding=(10, 8))
        src.pack(fill=tk.X, pady=(0, 8))
        row = ttk.Frame(src, style="Panel.TFrame")
        row.pack(fill=tk.X)
        ttk.Label(row, text="Session folder", style="Panel.TLabel", width=15).pack(side=tk.LEFT)
        ttk.Entry(row, textvariable=self.source_var).pack(side=tk.LEFT, fill=tk.X, expand=True, padx=(0, 6))
        ttk.Button(row, text="Browse…", width=10, command=self._pick_folder).pack(side=tk.LEFT)
        ttk.Button(row, text="data.db…", width=10, command=self._pick_db).pack(side=tk.LEFT, padx=(4, 0))
        ttk.Label(src, text="Pick the folder that contains data.db (the parent folder is used if you pick "
                            "data.db itself).",
                  style="Hint.TLabel").pack(anchor="w", pady=(6, 0))

        # ---- output ----
        out = ttk.Labelframe(body, text="Output", padding=(10, 8))
        out.pack(fill=tk.X, pady=(0, 8))
        orow = ttk.Frame(out, style="Panel.TFrame")
        orow.pack(fill=tk.X)
        ttk.Label(orow, text="Output folder", style="Panel.TLabel", width=15).pack(side=tk.LEFT)
        ttk.Entry(orow, textvariable=self.output_var).pack(side=tk.LEFT, fill=tk.X, expand=True, padx=(0, 6))
        ttk.Button(orow, text="Browse…", width=10, command=self._pick_output).pack(side=tk.LEFT)
        ttk.Label(out, text="Leave empty to write the CSV files into the session folder next to data.db.",
                  style="Hint.TLabel").pack(anchor="w", pady=(6, 0))

        opts = ttk.Frame(out, style="Panel.TFrame")
        opts.pack(fill=tk.X, pady=(8, 0))
        ttk.Checkbutton(opts, text="Skip Fluorescence-unaligned.csv (saves ~32 MB per session)",
                        variable=self.skip_var).pack(anchor="w")
        ttk.Checkbutton(opts, text="Replace existing output files without asking",
                        variable=self.replace_var).pack(anchor="w", pady=(2, 0))

        # ---- action ----
        action = ttk.Frame(body)
        action.pack(fill=tk.X, pady=(0, 6))
        self.convert_btn = ttk.Button(action, text="Convert", style="Accent.TButton",
                                      command=self._start)
        self.convert_btn.pack(side=tk.LEFT)
        self.open_btn = ttk.Button(action, text="Open output folder", width=20,
                                   command=self._open_output, state=tk.DISABLED)
        self.open_btn.pack(side=tk.LEFT, padx=(8, 0))
        self.progress = ttk.Progressbar(action, mode="indeterminate", length=180)
        ttk.Label(body, textvariable=self.status_var, style="Status.TLabel",
                  wraplength=880, justify="left").pack(fill=tk.X)

        # ---- log ----
        logframe = ttk.Labelframe(body, text="Log", padding=(8, 6))
        logframe.pack(fill=tk.BOTH, expand=True, pady=(8, 0))
        self.log = tk.Text(logframe, height=10, wrap="word", relief=tk.FLAT, borderwidth=1,
                           background=PALETTE["log_bg"], foreground=PALETTE["text"],
                           font=("Consolas", 9), state=tk.DISABLED)
        scroll = ttk.Scrollbar(logframe, orient=tk.VERTICAL, command=self.log.yview)
        self.log.configure(yscrollcommand=scroll.set)
        scroll.pack(side=tk.RIGHT, fill=tk.Y)
        self.log.pack(side=tk.LEFT, fill=tk.BOTH, expand=True)

    # ------------------------------------------------------------- actions --
    def _pick_folder(self) -> None:
        chosen = filedialog.askdirectory(title="Select the session folder containing data.db")
        if chosen:
            self.source_var.set(chosen)

    def _pick_db(self) -> None:
        chosen = filedialog.askopenfilename(
            title="Select data.db",
            filetypes=[("Inper database", "data.db"), ("SQLite database", "*.db"), ("All files", "*.*")],
        )
        if chosen:
            self.source_var.set(chosen)

    def _pick_output(self) -> None:
        chosen = filedialog.askdirectory(title="Select the output folder")
        if chosen:
            self.output_var.set(chosen)

    def _open_output(self) -> None:
        target = self.last_out
        if target and Path(target).is_dir():
            try:
                open_in_file_manager(str(target))
            except Exception as exc:  # noqa: BLE001 - report, never crash the window
                messagebox.showerror("Cannot open folder", str(exc), parent=self.root)

    def _resolve_session(self) -> Path:
        raw = self.source_var.get().strip().strip('"')
        if not raw:
            raise ValueError("Select a session folder or a data.db file first.")
        path = Path(raw)
        if path.suffix.lower() == ".db" or path.is_file():
            path = path.parent
        if not (path / "data.db").is_file():
            raise FileNotFoundError(f"No data.db found in {path}")
        return path

    def _start(self) -> None:
        if self.busy:
            return
        try:
            session = self._resolve_session()
        except Exception as exc:  # noqa: BLE001 - 直接反馈给用户
            messagebox.showerror("Invalid source", str(exc), parent=self.root)
            return

        out_text = self.output_var.get().strip().strip('"')
        out_dir = Path(out_text) if out_text else session
        skip = self.skip_var.get()
        targets = [FLUORESCENCE, EVENTS, OUTPUTS] + ([] if skip else [UNALIGNED])
        existing = [name for name in targets if (out_dir / name).is_file()]
        overwrite = False
        if existing:
            if self.replace_var.get():
                overwrite = True
            else:
                overwrite = messagebox.askyesno(
                    "Output exists",
                    f"These files already exist in:\n{out_dir}\n\n"
                    f"{', '.join(existing)}\n\nReplace them?",
                    parent=self.root,
                )
                if not overwrite:
                    self.status_var.set("Cancelled: existing files were kept.")
                    return

        self._set_busy(True)
        self.log.configure(state=tk.NORMAL)
        self.log.delete("1.0", tk.END)
        self.log.configure(state=tk.DISABLED)
        self._append_log(f"session: {session}")
        self._append_log(f"output : {out_dir}   (skip unaligned={skip}, replace={overwrite})")
        self.status_var.set("Converting … this can take a few minutes for a 14 h recording.")

        def worker() -> None:
            try:
                stats = convert_session(session, out_dir=out_dir, overwrite=overwrite,
                                        skip_unaligned=skip, log=self.messages.put)
                self.root.after(0, self._done, stats, None)
            except Exception as exc:  # noqa: BLE001
                self.root.after(0, self._done, None, exc)

        threading.Thread(target=worker, daemon=True).start()

    def _done(self, stats: dict | None, error: Exception | None) -> None:
        self._set_busy(False)
        if error is not None:
            self._append_log(f"ERROR: {type(error).__name__}: {error}")
            self.status_var.set("Conversion failed. See the log below.")
            messagebox.showerror("Conversion failed", f"{type(error).__name__}: {error}", parent=self.root)
            return

        info = stats["rec"]
        out_dir = Path(stats["out_dir"])
        self.last_out = out_dir
        self.open_btn.configure(state=tk.NORMAL)
        self._append_log("")
        self._append_log(f"frames      : {info['frames']:,}")
        self._append_log(f"duration    : {info['duration_h']:.2f} h")
        self._append_log(f"wavelengths : {info['wavelengths']}")
        self._append_log(f"channels    : {info['channels']}   fps={info['fps']}")
        self._append_log(f"aligned rows: {stats['aligned']['rows']:,}")
        if stats.get("unaligned"):
            self._append_log(f"unaligned   : {stats['unaligned']['rows']:,}")
        self._append_log(f"markers     : {stats['events']['rows']}")
        if stats["problems"]:
            for problem in stats["problems"]:
                self._append_log(f"WARN: {problem}")
            self.status_var.set("Done with warnings — see the log below.")
        else:
            self._append_log("validation  : columns, timestamps, row counts OK")
            self.status_var.set(f"Done. Wrote {out_dir}")
        self._append_log("")

    # ------------------------------------------------------------- helpers --
    def _set_busy(self, busy: bool) -> None:
        self.busy = busy
        self.convert_btn.configure(state=tk.DISABLED if busy else tk.NORMAL)
        if busy:
            self.progress.pack(side=tk.RIGHT)
            self.progress.start(12)
        else:
            self.progress.stop()
            self.progress.pack_forget()

    def _append_log(self, message: str) -> None:
        self.log.configure(state=tk.NORMAL)
        self.log.insert(tk.END, message + "\n")
        self.log.see(tk.END)
        self.log.configure(state=tk.DISABLED)

    def _pump(self) -> None:
        drained = 0
        while drained < 200:
            try:
                message = self.messages.get_nowait()
            except queue.Empty:
                break
            self._append_log(message)
            drained += 1
        self.root.after(120, self._pump)


def main() -> None:
    root = tk.Tk()
    ConverterApp(root)
    root.mainloop()


if __name__ == "__main__":
    main()
