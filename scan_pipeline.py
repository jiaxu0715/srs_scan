"""Scan orchestration: loop over wavelengths and write run logs.

Pipeline role
-------------
Coordinates ``laser_client.Laser`` and ``olympus_client.Olympus`` for one full
experiment. This is where multi-Z + multi-λ logic lives at the Python level:

- **discrete mode** (typical for multi-Z): for each (λ, OPO, IR) in config,
  tune the laser → wait for ``OK`` → call ``acquire_matl()`` → Olympus runs the
  saved Z-stack MATL protocol once per wavelength.
- **sweep mode**: use the APE internal sweep table; wait for ``hold`` at each
  step instead of setting λ explicitly.

Replaces the Excel + STEP1 VBA workflow. Logging appends one row per run to
``YYYYMMDD.xlsx`` (legacy workbook layout).
"""

from __future__ import annotations

import json
import os
import time
from datetime import datetime

from laser_client import DEFAULT_HOST, DEFAULT_PORT, Laser, nm_to_tenths
from olympus_client import DEFAULT_OLYMPUS_URL, Olympus, acquire_matl

try:
    from tqdm import tqdm
except ImportError:
    def tqdm(it, **_):
        return it


def _new_log() -> dict:
    """Empty in-memory log; filled by ``acquire_matl()`` and saved at end of run."""
    return {
        "OPO_power": [],
        "IR_power": [],
        "OPO_WAVELENGTH": [],
        "Original_Filename": [],
        "New_Filename": [],
        "Rescan": [],
    }


def _save_log(log: dict, comment: str = "", path: str | None = None) -> str:
    """Append one run as a single Excel/JSONL row (list cells, STEP1-style)."""
    path = path or f"{datetime.now():%Y%m%d}.xlsx"
    columns = {
        "Original Filename": log["Original_Filename"],
        "New Filename": log["New_Filename"],
        "OPO WAVELENGTH": log["OPO_WAVELENGTH"],
        "OPO POWER": log["OPO_power"],
        "IR POWER": log["IR_power"],
        "Rescan": log["Rescan"],
        "Comment": comment,
    }
    try:
        import pandas as pd

        # One row whose cells are the lists above (matches legacy workbook logs).
        row = {name: [values] for name, values in columns.items()}
        df = pd.DataFrame([row])
        if os.path.isfile(path):
            df = pd.concat([pd.read_excel(path), df], ignore_index=True)
        df.to_excel(path, index=False)
        return path
    except ImportError:
        out = path[:-5] + ".jsonl" if path.lower().endswith(".xlsx") else path + ".jsonl"
        with open(out, "a", encoding="utf-8") as f:
            f.write(json.dumps(columns) + "\n")
        print("pandas/openpyxl missing; wrote JSONL instead")
        return out


def _acquire_with_retry(laser, olympus, sample: str, status: str, log: dict, nm: float, on_retry=None) -> None:
    """Keep calling ``acquire_matl()`` until the laser was ready enough to start."""
    while not acquire_matl(laser, olympus, sample, status, log):
        print(f"Retrying {nm:.1f} nm…")
        if on_retry:
            on_retry()


def run_sweep(laser: Laser, olympus: Olympus, params: dict, log: dict) -> None:
    """Continuous APE sweep: SWEEP START → for each λ: hold → delay → acquire → NEXT."""
    start = params["start_wavelength"]
    end = params["end_wavelength"]
    step = params["step_size"]
    sample = params["sample_name"]

    # n intervals ⇒ n+1 visit points (same convention as the APE SWEEP= command).
    n = int((nm_to_tenths(end) - nm_to_tenths(start)) / (step * 10))
    delta = (end - start) / n if n else 0.0
    print(f"Sweep {start:g}–{end:g} nm, step {step:g} nm ({n + 1} points)")

    laser.sweep_config(start, end, n)
    laser.sweep_start()

    for i in tqdm(range(n + 1)):
        nm = start + i * delta
        # After START / NEXT the OPO retunes; wait until stable before DELAY.
        if not laser.wait_status("hold"):
            print(f"Skip step {i} ({nm:.1f} nm): status never hold")
            continue
        laser.set_delay_nm(nm)
        _acquire_with_retry(laser, olympus, sample, "hold", log, nm)
        laser.sweep_next()


def run_discrete(laser: Laser, olympus: Olympus, params: dict, log: dict) -> None:
    """Multi-λ (and multi-Z via MATL) mode: one acquisition per config row.

    Each row in ``params["scans"]`` sets λ, OPO power, and IR power. Olympus
    executes the same Z-stack MATL protocol at every wavelength.
    """
    sample = params["sample_name"]
    for scan in tqdm(params["scans"]):
        nm = float(scan["wavelength"])
        laser.set_wavelength_nm(nm)
        laser.set_opo_power(scan["opo_power"])
        laser.set_ir_power(scan["ir_power"])
        laser.set_delay_nm(nm)
        if not laser.wait_status("OK"):
            print(f"Skip {nm} nm: status never OK")
            continue

        def reassert() -> None:
            # Re-assert λ/delay in case a fault left the laser off-target.
            laser.set_wavelength_nm(nm)
            laser.set_delay_nm(nm)

        _acquire_with_retry(laser, olympus, sample, "OK", log, nm, on_retry=reassert)


def run_pipeline(
    params: dict,
    *,
    laser_host: str = DEFAULT_HOST,
    laser_port: int = DEFAULT_PORT,
    olympus_url: str = DEFAULT_OLYMPUS_URL,
    dry_run: bool = False,
    log_xlsx: str | None = None,
) -> dict:
    """Connect hardware, run discrete or sweep loop, save log. Main entry from ``run_scan``."""
    olympus = Olympus(url=olympus_url, dry_run=dry_run)
    log = _new_log()
    with Laser(host=laser_host, port=laser_port, dry_run=dry_run) as laser:
        laser.enable_eom()
        time.sleep(1)  # brief hardware settle after enabling the EOM
        if params["mode"] == "sweep":
            run_sweep(laser, olympus, params, log)
        else:
            run_discrete(laser, olympus, params, log)
    out = _save_log(log, comment=str(params.get("comment", "")), path=log_xlsx)
    print(f"Wrote run log → {out}")
    return {"log": log, "log_xlsx": out}
