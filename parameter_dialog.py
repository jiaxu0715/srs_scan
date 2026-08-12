"""Load and validate scan parameters from JSON (or an optional tkinter dialog).

Pipeline role
-------------
Defines the experiment recipe consumed by ``run_scan`` → ``scan_pipeline``.
Use ``mode: "sweep"`` or ``"discrete"`` for the laser strategy, and
``acquisition: "single_fov"`` or ``"mosaic"``.

- **single_fov**: one MANUAL_MAIN capture at the current stage position (no
  columns/rows prompts).
- **mosaic**: columns×rows grid centered on optional ``stage_x_um`` /
  ``stage_y_um``, or on the current Fluoview stage FOV if those are omitted.
  FOV size is ``509.117 µm / zoom`` (lab zoom‑1 calibration). Each tile is
  acquired with its own retry/power_tol logic and stitched in pure Python
  (no MATL).

JSON files may include ``"_..."`` keys for human-readable notes; they are
ignored during validation.
"""

from __future__ import annotations

import json
from pathlib import Path

_SUPPORTED_ACQUISITION = {"single_fov", "mosaic"}


def validate(params: dict) -> dict:
    """Normalize and type-check a parameter dict from JSON or the GUI."""
    # Allow documentation keys like "_about" in config files.
    params = {k: v for k, v in params.items() if not str(k).startswith("_")}

    mode = str(params.get("mode", "")).strip().lower()
    if mode not in {"sweep", "discrete"}:
        raise ValueError("mode must be 'sweep' or 'discrete'")
    acquisition = str(params.get("acquisition", "single_fov")).strip().lower()
    if acquisition not in _SUPPORTED_ACQUISITION:
        raise ValueError(
            f"acquisition must be one of {sorted(_SUPPORTED_ACQUISITION)}; "
            f"got {acquisition!r}"
        )
    sample = str(params.get("sample_name", "")).strip()
    if not sample:
        raise ValueError("sample_name is required")

    out = {
        "mode": mode,
        "acquisition": acquisition,
        "sample_name": sample,
        "comment": str(params.get("comment", "") or ""),
        "power_tol": float(params["power_tol"]) if "power_tol" in params else 0.10,
    }
    if out["power_tol"] < 0:
        raise ValueError("power_tol must be >= 0")

    if acquisition == "mosaic":
        columns = int(params["columns"])
        rows = int(params["rows"])
        overlap = float(params.get("overlap", 0.05))
        zoom = float(params.get("zoom", 1.0))
        if columns < 1 or rows < 1:
            raise ValueError("columns and rows must be >= 1")
        if not 0.0 <= overlap < 1.0:
            raise ValueError("overlap must be in [0, 1)")
        if zoom <= 0:
            raise ValueError("zoom must be > 0")
        out.update(
            columns=columns,
            rows=rows,
            overlap=overlap,
            zoom=zoom,
            stage_x_sign=int(params.get("stage_x_sign", 1)),
            stage_y_sign=int(params.get("stage_y_sign", 1)),
        )
        has_x = "stage_x_um" in params and params["stage_x_um"] is not None and str(params["stage_x_um"]).strip() != ""
        has_y = "stage_y_um" in params and params["stage_y_um"] is not None and str(params["stage_y_um"]).strip() != ""
        if has_x ^ has_y:
            raise ValueError("provide both stage_x_um and stage_y_um, or neither")
        if has_x and has_y:
            out["stage_x_um"] = float(params["stage_x_um"])
            out["stage_y_um"] = float(params["stage_y_um"])

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
        self.comment = tk.StringVar(value="")
        self.acquisition = tk.StringVar(value="single_fov")
        self.mode = tk.StringVar(value="sweep")
        self.start = tk.StringVar(value="787.0")
        self.end = tk.StringVar(value="800.0")
        self.step = tk.StringVar(value="0.5")
        self.power_tol = tk.StringVar(value="0.10")
        self.opo_power = tk.StringVar(value="150")
        self.ir_power = tk.StringVar(value="200")
        self.columns = tk.StringVar(value="3")
        self.rows = tk.StringVar(value="3")
        self.overlap = tk.StringVar(value="0.05")
        self.zoom = tk.StringVar(value="1.0")
        self.stage_x = tk.StringVar(value="")
        self.stage_y = tk.StringVar(value="")

        for i, (label, var) in enumerate(
            (
                ("Sample name", self.sample),
                ("Comment", self.comment),
                ("Power tol (frac)", self.power_tol),
            )
        ):
            ttk.Label(frm, text=label).grid(row=i, column=0, sticky="w", **pad)
            ttk.Entry(frm, textvariable=var, width=28).grid(row=i, column=1, **pad)

        ttk.Label(frm, text="Acquisition").grid(row=3, column=0, sticky="w", **pad)
        acq = ttk.Combobox(
            frm,
            textvariable=self.acquisition,
            values=("single_fov", "mosaic"),
            state="readonly",
        )
        acq.grid(row=3, column=1, sticky="ew", **pad)
        acq.bind("<<ComboboxSelected>>", lambda _e: self._toggle())

        ttk.Label(frm, text="Mode").grid(row=4, column=0, sticky="w", **pad)
        box = ttk.Combobox(frm, textvariable=self.mode, values=("sweep", "discrete"), state="readonly")
        box.grid(row=4, column=1, sticky="ew", **pad)
        box.bind("<<ComboboxSelected>>", lambda _e: self._toggle())

        self.mosaic = ttk.LabelFrame(
            frm,
            text="Mosaic (blank stage = use current FOV)",
            padding=8,
        )
        self.mosaic.grid(row=5, column=0, columnspan=2, sticky="ew", **pad)
        for i, (label, var) in enumerate(
            (
                ("Stage X µm (opt)", self.stage_x),
                ("Stage Y µm (opt)", self.stage_y),
                ("Columns", self.columns),
                ("Rows", self.rows),
                ("Overlap (frac)", self.overlap),
                ("Zoom", self.zoom),
            )
        ):
            ttk.Label(self.mosaic, text=label).grid(row=i, column=0, sticky="w", **pad)
            ttk.Entry(self.mosaic, textvariable=var, width=20).grid(row=i, column=1, **pad)

        self.sweep = ttk.LabelFrame(frm, text="Sweep", padding=8)
        self.sweep.grid(row=6, column=0, columnspan=2, sticky="ew", **pad)
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
        self.discrete.grid(row=7, column=0, columnspan=2, sticky="ew", **pad)
        self.scans = tk.Text(self.discrete, width=40, height=6)
        self.scans.grid(**pad)
        self.scans.insert("1.0", "787.0, 150, 200\n794.0, 150, 200\n")

        btns = ttk.Frame(frm)
        btns.grid(row=8, column=0, columnspan=2, **pad)
        ttk.Button(btns, text="Cancel", command=self._cancel).grid(row=0, column=0, **pad)
        ttk.Button(btns, text="Start", command=lambda: self._ok(messagebox)).grid(row=0, column=1, **pad)

        self._toggle()
        root.protocol("WM_DELETE_WINDOW", self._cancel)
        root.mainloop()

    def _toggle(self) -> None:
        if self.acquisition.get() == "mosaic":
            self.mosaic.grid()
        else:
            self.mosaic.grid_remove()
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
                "acquisition": self.acquisition.get(),
                "sample_name": self.sample.get(),
                "comment": self.comment.get(),
                "power_tol": float(self.power_tol.get()),
            }
            if raw["acquisition"] == "mosaic":
                raw.update(
                    columns=int(self.columns.get()),
                    rows=int(self.rows.get()),
                    overlap=float(self.overlap.get()),
                    zoom=float(self.zoom.get()),
                )
                sx = self.stage_x.get().strip()
                sy = self.stage_y.get().strip()
                if sx or sy:
                    raw["stage_x_um"] = float(sx)
                    raw["stage_y_um"] = float(sy)
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
