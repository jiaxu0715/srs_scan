"""Scan orchestration: loop over wavelengths and write run logs.

Pipeline role
-------------
Coordinates ``laser_client.Laser`` and ``olympus_client.Olympus`` for one full
experiment. Laser strategy (sweep vs discrete) is configured in JSON; each
wavelength point acquires via ``acquire_single_fov()`` (Olympus MANUAL_MAIN):

- **acquisition single_fov**: one capture at the current stage position.
- **acquisition mosaic**: move stage across a columns×rows grid centered on the
  current FOV, capture each tile with its own retry/power_tol logic, then
  stitch with pure Python (no MATL).

- **discrete mode**: for each (λ, OPO, IR) in config, tune the laser → wait
  for ``OK`` → acquire (single FOV or mosaic).
- **sweep mode**: use the APE internal sweep table; wait for ``hold`` at each
  step instead of setting λ explicitly.

Logging appends one row per run to ``YYYYMMDD.xlsx`` (legacy workbook layout).
"""

from __future__ import annotations

import json
import os
import time
from datetime import datetime

from laser_client import DEFAULT_HOST, DEFAULT_PORT, Laser, nm_to_tenths
from mosaic import (
    build_mosaic_tiles,
    collect_tiles_from_dir,
    collect_tiles_from_paths,
    field_um_from_zoom,
    oir_stitch_deps_ok,
    stitch_tiles,
    tile_sample_name,
    write_tile_manifest,
)
from olympus_client import DEFAULT_OLYMPUS_URL, Olympus, acquire_single_fov

try:
    from tqdm import tqdm
except ImportError:
    def tqdm(it, **_):
        return it

# Brief pause after XY move so the stage can settle before MANUAL_MAIN.
_STAGE_SETTLE_S = 2.0


def _new_log() -> dict:
    """Empty in-memory log; filled by ``acquire_single_fov()`` and saved at end of run."""
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
    on_retry=None,
) -> None:
    """Keep calling ``acquire_single_fov()`` until the laser was ready enough to start."""
    while not acquire_single_fov(
        laser,
        olympus,
        sample,
        status,
        log,
        opo_setpoint,
        ir_setpoint,
        power_tol,
    ):
        print(f"Retrying {nm:.1f} nm…")
        if on_retry:
            on_retry()


def _acquire_mosaic(
    laser: Laser,
    olympus: Olympus,
    params: dict,
    status: str,
    log: dict,
    nm: float,
    opo_setpoint,
    ir_setpoint,
    on_retry=None,
) -> None:
    """Capture every mosaic tile at the current laser setpoint, then stitch."""
    sample = params["sample_name"]
    power_tol = params["power_tol"]
    field_um = field_um_from_zoom(params["zoom"])
    # Mosaic center = current stage FOV (read from Fluoview; not a config field).
    center_x_nm, center_y_nm = olympus.get_stage_xy()
    stage_x_um = center_x_nm / 1000.0
    stage_y_um = center_y_nm / 1000.0
    tiles = build_mosaic_tiles(
        stage_x_um,
        stage_y_um,
        params["columns"],
        params["rows"],
        field_um,
        field_um,
        params["overlap"],
        x_sign=params.get("stage_x_sign", 1),
        y_sign=params.get("stage_y_sign", 1),
    )
    print(
        f"Mosaic {params['columns']}×{params['rows']} centered on "
        f"({stage_x_um:g}, {stage_y_um:g}) µm; "
        f"zoom={params['zoom']:g} → field={field_um:.3f} µm; "
        f"{len(tiles)} tiles; overlap={params['overlap']:.0%}"
    )

    n_before = len(log["New_Filename"])
    for tile in tqdm(tiles, desc=f"mosaic@{nm:.1f}nm"):
        row, col = tile["row"], tile["col"]
        print(
            f"Tile r{row} c{col} → stage "
            f"({tile['x_um']:.2f}, {tile['y_um']:.2f}) µm"
        )
        olympus.move_stage(tile["x_nm"], tile["y_nm"], escape_objective=False)
        if not olympus.dry_run:
            time.sleep(_STAGE_SETTLE_S)

        def reassert_tile() -> None:
            olympus.move_stage(tile["x_nm"], tile["y_nm"], escape_objective=False)
            if not olympus.dry_run:
                time.sleep(_STAGE_SETTLE_S)
            if on_retry:
                on_retry()

        _acquire_with_retry(
            laser,
            olympus,
            tile_sample_name(sample, row, col),
            status,
            log,
            nm,
            opo_setpoint,
            ir_setpoint,
            power_tol,
            on_retry=reassert_tile,
        )

    # Return to mosaic center (current FOV).
    olympus.move_stage(center_x_nm, center_y_nm, escape_objective=False)

    new_paths = log["New_Filename"][n_before:]
    acquired = collect_tiles_from_paths(new_paths)
    out_dir = os.path.dirname(new_paths[-1]) if new_paths else os.getcwd()
    tag = f"{sample}_{nm:.1f}nm"
    manifest = os.path.join(out_dir, f"{tag}_tiles.json")
    write_tile_manifest(
        manifest,
        tiles,
        acquired,
        meta={
            "sample": sample,
            "wavelength_nm": nm,
            "overlap": params["overlap"],
            "columns": params["columns"],
            "rows": params["rows"],
            "stage_x_um": stage_x_um,
            "stage_y_um": stage_y_um,
            "zoom": params["zoom"],
            "field_um": field_um,
        },
    )
    if olympus.dry_run:
        print("[dry-run] skip stitch (no real .oir tiles)", flush=True)
        return

    # Log paths may miss _r/_c_ if rename failed; fall back to directory scan.
    existing = {rc: p for rc, p in acquired.items() if os.path.isfile(p)}
    if len(existing) < len(tiles):
        scanned = collect_tiles_from_dir(out_dir)
        for rc, path in scanned.items():
            if rc not in existing and os.path.isfile(path):
                existing[rc] = path
        if scanned:
            print(
                f"Stitch: log had {len(acquired)} named tiles, "
                f"{len(existing)} readable after dir scan of {out_dir!r}",
                flush=True,
            )

    if not existing:
        print(
            f"Stitch skipped: no mosaic-named tile files found under {out_dir!r}. "
            f"Expected names containing _r{{row}}_c{{col}}_ (see {manifest}).",
            flush=True,
        )
        return
    if len(existing) < len(tiles):
        print(
            f"Warning: only {len(existing)}/{len(tiles)} tiles on disk; "
            "stitching available tiles only",
            flush=True,
        )

    needs_oir = any(p.lower().endswith(".oir") for p in existing.values())
    if needs_oir:
        ok, detail = oir_stitch_deps_ok()
        if not ok:
            print(
                f"Stitch skipped: cannot read .oir ({detail}). "
                f"Tile paths are in {manifest}. "
                "Install aicsimageio + bioformats_jar (Java required), or convert tiles to .tif.",
                flush=True,
            )
            return

    stitch_path = os.path.join(out_dir, f"{tag}_mosaic.tif")
    print(f"Stitching {len(existing)} tiles → {stitch_path}", flush=True)
    try:
        stitch_tiles(
            existing,
            overlap_fraction=params["overlap"],
            out_path=stitch_path,
        )
    except Exception as exc:
        import traceback

        print(f"Stitch failed ({exc}); tile paths are in {manifest}", flush=True)
        traceback.print_exc()


def _acquire_point(
    laser: Laser,
    olympus: Olympus,
    params: dict,
    status: str,
    log: dict,
    nm: float,
    opo_setpoint,
    ir_setpoint,
    on_retry=None,
) -> None:
    """Single FOV or mosaic capture at one laser setpoint."""
    if params.get("acquisition", "single_fov") == "mosaic":
        _acquire_mosaic(
            laser,
            olympus,
            params,
            status,
            log,
            nm,
            opo_setpoint,
            ir_setpoint,
            on_retry=on_retry,
        )
    else:
        _acquire_with_retry(
            laser,
            olympus,
            params["sample_name"],
            status,
            log,
            nm,
            opo_setpoint,
            ir_setpoint,
            params["power_tol"],
            on_retry=on_retry,
        )


def run_sweep(laser: Laser, olympus: Olympus, params: dict, log: dict) -> None:
    """Continuous APE sweep: SWEEP START → for each λ: hold → delay → acquire → NEXT."""
    start = params["start_wavelength"]
    end = params["end_wavelength"]
    step = params["step_size"]
    opo_setpoint = params["opo_power"]
    ir_setpoint = params["ir_power"]

    # n intervals ⇒ n+1 visit points (same convention as the APE SWEEP= command).
    n = int((nm_to_tenths(end) - nm_to_tenths(start)) / (step * 10))
    delta = (end - start) / n if n else 0.0
    print(f"Sweep {start:g}–{end:g} nm, step {step:g} nm ({n + 1} points)")

    laser.set_opo_power(opo_setpoint)
    laser.set_ir_power(ir_setpoint)
    laser.sweep_config(start, end, n)
    laser.sweep_start()

    for i in tqdm(range(n + 1)):
        nm = start + i * delta
        # After START / NEXT the OPO retunes; wait until stable before DELAY.
        if not laser.wait_status("hold"):
            print(f"Skip step {i} ({nm:.1f} nm): status never hold")
            continue
        laser.set_delay_nm(nm)
        _acquire_point(
            laser,
            olympus,
            params,
            "hold",
            log,
            nm,
            opo_setpoint,
            ir_setpoint,
        )
        laser.sweep_next()


def run_discrete(laser: Laser, olympus: Olympus, params: dict, log: dict) -> None:
    """Multi-λ mode: one acquisition (FOV or mosaic) per config row."""
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
            # Re-assert λ/power/delay in case a fault left the laser off-target.
            laser.set_wavelength_nm(nm)
            laser.set_opo_power(opo_setpoint)
            laser.set_ir_power(ir_setpoint)
            laser.set_delay_nm(nm)

        _acquire_point(
            laser,
            olympus,
            params,
            "OK",
            log,
            nm,
            opo_setpoint,
            ir_setpoint,
            on_retry=reassert,
        )


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
