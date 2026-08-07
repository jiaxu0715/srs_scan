"""Load and validate scan parameters from JSON (or an optional tkinter dialog).

Pipeline role
-------------
Defines the experiment recipe consumed by ``run_scan`` → ``scan_pipeline``.
For multi-Z acquisition, use ``mode: "discrete"`` and list each wavelength
with OPO/IR power. Z-stack geometry is **not** configured here — set that in
the Olympus MATL protocol before running.

JSON files may include ``"_..."`` keys for human-readable notes; they are
ignored during validation.
"""

from __future__ import annotations

import json
from pathlib import Path


def validate(params: dict) -> dict:
    """Normalize and type-check a parameter dict from JSON or the GUI."""
    # Allow documentation keys like "_about" in config files.
    params = {k: v for k, v in params.items() if not str(k).startswith("_")}

    mode = str(params.get("mode", "")).strip().lower()
    if mode not in {"sweep", "discrete"}:
        raise ValueError("mode must be 'sweep' or 'discrete'")
    sample = str(params.get("sample_name", "")).strip()
    if not sample:
        raise ValueError("sample_name is required")

    out = {
        "mode": mode,
        "sample_name": sample,
        "imaging_time": float(params.get("imaging_time", 0) or 0),
        "comment": str(params.get("comment", "") or ""),
        "power_tol": float(params["power_tol"]) if "power_tol" in params else 0.10,
    }
    if out["power_tol"] < 0:
        raise ValueError("power_tol must be >= 0")
    if mode == "sweep":
        start = float(params["start_wavelength"])
        end = float(params["end_wavelength"])
        step = float(params["step_size"])
        if step <= 0 or end < start:
            raise ValueError("invalid sweep range/step")
        out.update(
            start_wavelength=start,
            end_wavelength=end,
            step_size=step,
            opo_power=float(params["opo_power"]),
            ir_power=float(params["ir_power"]),
        )
    else:
        scans = params.get("scans") or []
        if not scans:
            raise ValueError("discrete mode needs scans")
        out["scans"] = [
            {
                "wavelength": float(s["wavelength"]),
                "opo_power": float(s["opo_power"]),
                "ir_power": float(s["ir_power"]),
            }
            for s in scans
        ]
    return out


def load_json(path: str | Path) -> dict:
    """Read and validate a JSON config file (e.g. ``config.json``)."""
    return validate(json.loads(Path(path).read_text()))


def parse_scan_lines(text: str) -> list[dict]:
    """Parse discrete-mode lines: ``wavelength, opo_power, ir_power`` per row."""
    scans = []
    for line in text.splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        parts = [p.strip() for p in line.replace(";", ",").split(",")]
        if len(parts) != 3:
            raise ValueError(f"bad scan line: {line!r}")
        scans.append(
            {
                "wavelength": float(parts[0]),
                "opo_power": float(parts[1]),
                "ir_power": float(parts[2]),
            }
        )
    return scans


class ParameterDialog:
    """Optional GUI fallback when ``run_scan.py --gui`` is used instead of JSON."""

    def __init__(self):
        self._params = None
        try:
            import tkinter as tk
            from tkinter import messagebox, ttk
        except ImportError as exc:
            raise RuntimeError("tkinter unavailable; pass --config") from exc

        root = tk.Tk()
        root.title("APE / Olympus scan")
        self.root = root
        pad = {"padx": 6, "pady": 4}
        frm = ttk.Frame(root, padding=10)
        frm.grid()

        self.sample = tk.StringVar(value="sample")
        self.imaging = tk.StringVar(value="0")
        self.comment = tk.StringVar(value="")
        self.mode = tk.StringVar(value="sweep")
        self.start = tk.StringVar(value="787.0")
        self.end = tk.StringVar(value="800.0")
        self.step = tk.StringVar(value="0.5")
        self.power_tol = tk.StringVar(value="0.10")
        self.opo_power = tk.StringVar(value="150")
        self.ir_power = tk.StringVar(value="200")

        for i, (label, var) in enumerate(
            (
                ("Sample name", self.sample),
                ("Imaging time (s)", self.imaging),
                ("Comment", self.comment),
                ("Power tol (frac)", self.power_tol),
            )
        ):
            ttk.Label(frm, text=label).grid(row=i, column=0, sticky="w", **pad)
            ttk.Entry(frm, textvariable=var, width=28).grid(row=i, column=1, **pad)

        ttk.Label(frm, text="Mode").grid(row=4, column=0, sticky="w", **pad)
        box = ttk.Combobox(frm, textvariable=self.mode, values=("sweep", "discrete"), state="readonly")
        box.grid(row=4, column=1, sticky="ew", **pad)
        box.bind("<<ComboboxSelected>>", lambda _e: self._toggle())

        self.sweep = ttk.LabelFrame(frm, text="Sweep", padding=8)
        self.sweep.grid(row=5, column=0, columnspan=2, sticky="ew", **pad)
        for i, (label, var) in enumerate(
            (
                ("Start λ (nm)", self.start),
                ("End λ (nm)", self.end),
                ("Step (nm)", self.step),
                ("OPO power", self.opo_power),
                ("IR power", self.ir_power),
            )
        ):
            ttk.Label(self.sweep, text=label).grid(row=i, column=0, sticky="w", **pad)
            ttk.Entry(self.sweep, textvariable=var, width=20).grid(row=i, column=1, **pad)

        self.discrete = ttk.LabelFrame(frm, text="Discrete (λ, opo, ir per line)", padding=8)
        self.discrete.grid(row=6, column=0, columnspan=2, sticky="ew", **pad)
        self.scans = tk.Text(self.discrete, width=40, height=6)
        self.scans.grid(**pad)
        self.scans.insert("1.0", "787.0, 150, 200\n794.0, 150, 200\n")

        btns = ttk.Frame(frm)
        btns.grid(row=7, column=0, columnspan=2, **pad)
        ttk.Button(btns, text="Cancel", command=self._cancel).grid(row=0, column=0, **pad)
        ttk.Button(btns, text="Start", command=lambda: self._ok(messagebox)).grid(row=0, column=1, **pad)

        self._toggle()
        root.protocol("WM_DELETE_WINDOW", self._cancel)
        root.mainloop()

    def _toggle(self) -> None:
        if self.mode.get() == "sweep":
            self.sweep.grid()
            self.discrete.grid_remove()
        else:
            self.sweep.grid_remove()
            self.discrete.grid()

    def _ok(self, messagebox) -> None:
        try:
            raw = {
                "mode": self.mode.get(),
                "sample_name": self.sample.get(),
                "imaging_time": float(self.imaging.get() or 0),
                "comment": self.comment.get(),
                "power_tol": float(self.power_tol.get()),
            }
            if raw["mode"] == "sweep":
                raw.update(
                    start_wavelength=float(self.start.get()),
                    end_wavelength=float(self.end.get()),
                    step_size=float(self.step.get()),
                    opo_power=float(self.opo_power.get()),
                    ir_power=float(self.ir_power.get()),
                )
            else:
                raw["scans"] = parse_scan_lines(self.scans.get("1.0", "end"))
            self._params = validate(raw)
        except Exception as exc:
            messagebox.showerror("Invalid parameters", str(exc))
            return
        self.root.destroy()

    def _cancel(self) -> None:
        self._params = None
        self.root.destroy()

    def get_parameters(self):
        return self._params
