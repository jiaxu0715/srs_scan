"""Scan orchestration: loop over wavelengths and write run logs.

Pipeline role
-------------
Coordinates ``laser_client.Laser`` and ``olympus_client.Olympus`` for one full
experiment.

Laser strategy (``mode``):

- **discrete**: for each (λ, OPO, IR) in config, tune → wait ``OK`` → acquire
- **sweep**: APE sweep table; wait ``hold`` at each step → acquire

Olympus strategy (``acquisition``):

- **single_fov**: ``acquire_single_fov()`` / MANUAL_MAIN at the current stage
- **matl**: ``acquire_matl()`` / Fluoview built-in MATL map (multi-area / Z)

Logging appends one row per run to ``YYYYMMDD.xlsx`` (legacy workbook layout).
Per-wavelength OPO/IR high/low/mean from scan polls go to ``YYYYMMDD_power.json``.
"""

from __future__ import annotations

import json
import os
import time
from datetime import datetime

from laser_client import DEFAULT_HOST, DEFAULT_PORT, Laser, nm_to_tenths
from olympus_client import (
    DEFAULT_OLYMPUS_URL,
    Olympus,
    acquire_matl,
    acquire_single_fov,
)
from power_monitor import PowerMonitor

try:
    from tqdm import tqdm
except ImportError:
    def tqdm(it, **_):
        return it


def _new_log() -> dict:
    """Empty in-memory log; filled by acquire helpers and saved at end of run."""
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


def _acquire_fn(params: dict):
    """Return the Olympus acquire helper for ``params['acquisition']``."""
    if params.get("acquisition", "matl") == "single_fov":
        return acquire_single_fov
    return acquire_matl


def _power_json_path(log_xlsx: str | None) -> str:
    """Sibling ``*_power.json`` next to the run log, or dated default."""
    if not log_xlsx:
        return f"{datetime.now():%Y%m%d}_power.json"
    base = log_xlsx
    if base.lower().endswith(".xlsx"):
        base = base[:-5]
    elif base.lower().endswith(".jsonl"):
        base = base[:-6]
    return f"{base}_power.json"


def _acquire_with_retry(
    laser,
    olympus,
    sample: str,
    status: str,
    log: dict,
    nm: float,
    opo_setpoint,
    ir_setpoint,
    power_tol,
    acquire,
    on_retry=None,
    power_monitor=None,
) -> None:
    """Keep calling *acquire* until the laser was ready enough to start."""
    while not acquire(
        laser,
        olympus,
        sample,
        status,
        log,
        opo_setpoint,
        ir_setpoint,
        power_tol,
        power_monitor=power_monitor,
    ):
        print(f"Retrying {nm:.1f} nm…")
        if on_retry:
            on_retry()


def run_sweep(
    laser: Laser,
    olympus: Olympus,
    params: dict,
    log: dict,
    power_monitor: PowerMonitor | None = None,
) -> None:
    """Continuous APE sweep: SWEEP START → for each λ: hold → delay → acquire → NEXT."""
    start = params["start_wavelength"]
    end = params["end_wavelength"]
    step = params["step_size"]
    sample = params["sample_name"]
    opo_setpoint = params["opo_power"]
    ir_setpoint = params["ir_power"]
    power_tol = params["power_tol"]
    acquire = _acquire_fn(params)

    n = int((nm_to_tenths(end) - nm_to_tenths(start)) / (step * 10))
    delta = (end - start) / n if n else 0.0
    print(
        f"Sweep {start:g}–{end:g} nm, step {step:g} nm ({n + 1} points); "
        f"acquisition={params.get('acquisition', 'matl')}"
    )

    laser.set_opo_power(opo_setpoint)
    laser.set_ir_power(ir_setpoint)
    laser.sweep_config(start, end, n)
    laser.sweep_start()

    for i in tqdm(range(n + 1)):
        nm = start + i * delta
        if not laser.wait_status("hold"):
            print(f"Skip step {i} ({nm:.1f} nm): status never hold")
            continue
        laser.set_delay_nm(nm)
        if power_monitor is not None:
            power_monitor.begin(nm, opo_setpoint, ir_setpoint)
        _acquire_with_retry(
            laser,
            olympus,
            sample,
            "hold",
            log,
            nm,
            opo_setpoint,
            ir_setpoint,
            power_tol,
            acquire,
            power_monitor=power_monitor,
        )
        if power_monitor is not None:
            power_monitor.end()
        laser.sweep_next()


def run_discrete(
    laser: Laser,
    olympus: Olympus,
    params: dict,
    log: dict,
    power_monitor: PowerMonitor | None = None,
) -> None:
    """Multi-λ mode: one acquisition per config row (single FOV or MATL)."""
    sample = params["sample_name"]
    power_tol = params["power_tol"]
    acquire = _acquire_fn(params)
    print(f"Discrete scan; acquisition={params.get('acquisition', 'matl')}")

    for scan in tqdm(params["scans"]):
        nm = float(scan["wavelength"])
        opo_setpoint = scan["opo_power"]
        ir_setpoint = scan["ir_power"]
        laser.set_wavelength_nm(nm)
        laser.set_opo_power(opo_setpoint)
        laser.set_ir_power(ir_setpoint)
        laser.set_delay_nm(nm)
        if not laser.wait_status("OK"):
            print(f"Skip {nm} nm: status never OK")
            continue

        def reassert() -> None:
            laser.set_wavelength_nm(nm)
            laser.set_opo_power(opo_setpoint)
            laser.set_ir_power(ir_setpoint)
            laser.set_delay_nm(nm)

        if power_monitor is not None:
            power_monitor.begin(nm, opo_setpoint, ir_setpoint)
        _acquire_with_retry(
            laser,
            olympus,
            sample,
            "OK",
            log,
            nm,
            opo_setpoint,
            ir_setpoint,
            power_tol,
            acquire,
            on_retry=reassert,
            power_monitor=power_monitor,
        )
        if power_monitor is not None:
            power_monitor.end()


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
    power_monitor = PowerMonitor(
        sample_name=str(params.get("sample_name", "")),
        comment=str(params.get("comment", "")),
    )
    with Laser(host=laser_host, port=laser_port, dry_run=dry_run) as laser:
        laser.enable_eom()
        time.sleep(1)
        if params["mode"] == "sweep":
            run_sweep(laser, olympus, params, log, power_monitor=power_monitor)
        else:
            run_discrete(laser, olympus, params, log, power_monitor=power_monitor)
    out = _save_log(log, comment=str(params.get("comment", "")), path=log_xlsx)
    print(f"Wrote run log → {out}")
    power_path = power_monitor.save_json(_power_json_path(log_xlsx or out))
    return {"log": log, "log_xlsx": out, "power_json": power_path}
