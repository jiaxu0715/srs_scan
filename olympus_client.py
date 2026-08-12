"""General Olympus / Fluoview XML-RPC client, plus acquisition helpers.

Pipeline role
-------------
Microscope-side API. ``Olympus`` talks to FV over XML-RPC for parameters and
protocol control. Single-FOV methods use ``EXECUTION_TYPE_MANUAL_MAIN``;
MATL helpers (``*_matl``) remain available but are unused by the current
pipeline on the ``matl-retry`` branch.

``acquire_single_fov()`` runs one manual single-field acquisition after the
laser is tuned:

  1. waits for the laser to reach a stable status
  2. opens the laser shutter (before Olympus starts — late shutter cuts off
     the top of the FOV)
  3. starts MANUAL_MAIN (one .oir at the current FOV)
  4. records power/λ readouts; watches for faults mid-scan and optionally rescans
  5. closes shutter, stops protocol, renames the .oir with sample/λ/power tags

Imaging geometry (Z, dwell, etc.) lives in the active Olympus settings —
Python does not configure MATL multi-area layouts.
"""

from __future__ import annotations

import os
import time
import xmlrpc.client

from laser_client import power_within_tol

# Local Fluoview XML-RPC endpoint (Olympus must be running with the server enabled).
DEFAULT_OLYMPUS_URL = "http://127.0.0.1:8080/xmlrpc"
_MATL = {"executionType": "EXECUTION_TYPE_MATL"}
_MANUAL = {"executionType": "EXECUTION_TYPE_MANUAL_MAIN"}


def _numeric(value) -> bool:
    """True if *value* can be parsed as a float (used when building filenames)."""
    try:
        float(value)
        return True
    except (TypeError, ValueError):
        return False


class Olympus:
    """General Fluoview XML-RPC client (parameters + protocol control)."""

    def __init__(self, url: str = DEFAULT_OLYMPUS_URL, *, dry_run: bool = False):
        self.dry_run = dry_run
        self.proxy = None if dry_run else xmlrpc.client.ServerProxy(url)

    def get_param(self, setting_id: str) -> dict:
        """Return Parameter.getParameter payload without the ``result`` key."""
        if self.dry_run:
            print(f"[dry-run] getParameter {setting_id}")
            return {}

        input_val = {"settingId": setting_id}
        try:
            response = self.proxy.Parameter.getParameter(input_val)
        except Exception as e:
            raise RuntimeError(f"microscope API error getting {setting_id}") from e

        return_code = response.get("result")
        if return_code != "OK":
            raise RuntimeError(
                f"microscope API error getting {setting_id}, return code {return_code!r}"
            )
        return {key: val for key, val in response.items() if key != "result"}

    def set_param(self, setting_id: str, **kwargs) -> None:
        """Call Parameter.setParameter; raise if Olympus does not return OK.

        Pass extra fields as kwargs using Olympus camelCase names
        (e.g. ``enable=True`` → ``{"enable": True}`` in the RPC payload).
        """
        if self.dry_run:
            print(f"[dry-run] setParameter {setting_id} {kwargs}")
            return

        input_val = {"settingId": setting_id, **kwargs}
        try:
            response = self.proxy.Parameter.setParameter(input_val)
        except Exception as e:
            raise RuntimeError(f"microscope API error setting {setting_id}") from e

        return_code = response.get("result")
        if return_code != "OK":
            raise RuntimeError(
                f"microscope API error setting {setting_id}, return code {return_code!r}"
            )

    # --- HARDWARE MOVEMENT METHODS ---
    def get_stage_xy(self) -> tuple[int, int]:
        """Return absolute stage (x, y) in nanometers."""
        if self.dry_run:
            print("[dry-run] get_stage_xy → (0, 0)")
            return 0, 0
        xy = self.get_param("XY_STAGE_POSITION_SETTING")
        return int(xy["x"]), int(xy["y"])

    def move_stage(self, x_nm: int, y_nm: int, escape_objective: bool = False) -> None:
        """Moves stage to absolute X/Y nanometer coordinates."""
        self.set_param(
            "XY_STAGE_POSITION_SETTING",
            x=int(x_nm),
            y=int(y_nm),
            escapeEnabled=escape_objective,
        )

    def configure_z_sweep(self, start_offset_nm: int, end_offset_nm: int, step_size_nm: int):
        """Enables and configures a relative Z-stack sweep around the current focus origin."""
        self.set_param("LSM_Z_COORDINATE_ENABLE_SETTING", enable=True)
        self.set_param("LSM_Z_COORDINATE_TYPE_SETTING", zType="MOTOR", zType2="START_END")
        self.set_param("LSM_Z_COORDINATE_STEP_SIZE_SETTING", stepSize=step_size_nm)
        self.set_param("LSM_Z_COORDINATE_START_SETTING", startPosition=start_offset_nm)
        self.set_param("LSM_Z_COORDINATE_END_SETTING", endPosition=end_offset_nm)

    # --- PROTOCOL CONTROLS (single FOV / MANUAL_MAIN) ---
    def start_single(self) -> str:
        """Start MANUAL_MAIN; returns path of the .oir the microscope will write."""
        if self.dry_run:
            path = os.path.join(os.getcwd(), "dryrun_area", "dryrun.oir")
            print(f"[dry-run] Olympus start_single → {path}")
            return path
        return self.proxy.Protocol.startProtocol(_MANUAL)["targetName"][0]["name"]

    def stop_single(self) -> None:
        """Stop the running MANUAL_MAIN protocol."""
        if self.dry_run:
            print("[dry-run] Olympus stop_single")
            return
        self.proxy.Protocol.stopProtocol(_MANUAL)

    def scanning_single(self) -> bool:
        """True while Olympus reports MANUAL_MAIN is actively scanning."""
        if self.dry_run:
            return False
        return self.proxy.Protocol.getProtocolProgress(_MANUAL)["state"] == "SCANNING"

    def is_idling(self) -> bool:
        """True if the microscope has finished scanning and returned to IDLING."""
        if self.dry_run:
            return True
        progress = self.proxy.Protocol.getProtocolProgress(_MANUAL)
        return progress.get("state") == "IDLING"

    # --- PROTOCOL CONTROLS (MATL; unused by current pipeline) ---
    def start_matl(self) -> str:
        """Start MATL; returns path of the first .oir the microscope will write."""
        if self.dry_run:
            path = os.path.join(os.getcwd(), "dryrun_area", "dryrun.oir")
            print(f"[dry-run] Olympus start_matl → {path}")
            return path
        return self.proxy.Protocol.startProtocol(_MATL)["targetName"][0]["name"]

    def stop_matl(self) -> None:
        """Stop the running MATL protocol."""
        if self.dry_run:
            print("[dry-run] Olympus stop_matl")
            return
        self.proxy.Protocol.stopProtocol(_MATL)

    def scanning_matl(self) -> bool:
        """True while Olympus reports the MATL protocol is actively scanning."""
        if self.dry_run:
            return False
        return self.proxy.Protocol.getProtocolProgress(_MATL)["state"] == "SCANNING"


def _rename(src: str, dst: str, attempts: int = 10) -> bool:
    """Rename with retries — Olympus may still have the file open briefly."""
    for _ in range(attempts):
        try:
            os.rename(src, dst)
            return True
        except PermissionError:
            time.sleep(1)  # file may still be locked by Olympus
        except FileNotFoundError:
            return False
    return False


def _rename_area(
    area_dir: str,
    sample: str,
    wavelength,
    opo,
    ir,
    suffix: str,
    log: dict,
    rescanned: bool,
) -> None:
    """Rename every .oir in the acquisition folder to a descriptive filename.

    Multi-Z: one MATL run can produce several .oir files (one per slice or ROI).
    Each becomes ``{sample}_roi{i}_{λ}_{opo}_{ir}{suffix}.oir``.
    """
    if not os.path.isdir(area_dir):
        return
    # Laser reports λ in tenths of nm; filenames use nm.
    tag = float(wavelength) / 10.0 if _numeric(wavelength) else wavelength
    files = sorted(f for f in os.listdir(area_dir) if f.endswith(".oir"))
    for i, name in enumerate(files, start=1):
        old = os.path.join(area_dir, name)
        new = os.path.join(area_dir, f"{sample}_roi{i}_{tag}_{opo}_{ir}{suffix}.oir")
        if _rename(old, new):
            log["Original_Filename"].append(old)
            log["New_Filename"].append(new)
            log["Rescan"].append(rescanned)
        else:
            print(f"Could not rename {old}")


def _rename_single(
    path: str,
    sample: str,
    wavelength,
    opo,
    ir,
    suffix: str,
    log: dict,
    rescanned: bool,
) -> None:
    """Rename one MANUAL_MAIN .oir to ``{sample}_{λ}_{opo}_{ir}{suffix}.oir``."""
    # Laser reports λ in tenths of nm; filenames use nm.
    tag = float(wavelength) / 10.0 if _numeric(wavelength) else wavelength
    directory = os.path.dirname(path) or os.getcwd()
    ext = os.path.splitext(path)[1] or ".oir"
    new = os.path.join(directory, f"{sample}_{tag}_{opo}_{ir}{suffix}{ext}")
    # Always record the intended name (mosaic stitch keys off ``_rR_cC_`` in it).
    log["Original_Filename"].append(path)
    log["New_Filename"].append(new)
    log["Rescan"].append(rescanned)
    if not os.path.isfile(path):
        # Dry-run / missing file: keep logical destination only.
        return
    if not _rename(path, new):
        print(f"Could not rename {path} → {new}")
        log["New_Filename"][-1] = path


def acquire_single_fov(
    laser,
    olympus: Olympus,
    sample: str,
    target_status: str,
    log: dict,
    opo_setpoint,
    ir_setpoint,
    power_tol,
    *,
    max_rescans: int = 3,
) -> bool:
    """Run one MANUAL_MAIN single-FOV acquisition at the current laser setpoint.

    *target_status* is ``"OK"`` in discrete mode and ``"hold"`` during APE
    hardware sweeps.

    *opo_setpoint* / *ir_setpoint* / *power_tol* come from the loaded scan
    config. Power tolerance is enforced only in discrete mode (``target_status``
    ``"OK"``), and only while status remains ``"OK"`` — sweep/hold acquisitions
    skip it, and a non-OK status already triggers the rescan path.

    Returns False only if the laser never reaches *target_status* (caller may
    retry the whole wavelength point). Otherwise returns True and tags the .oir
    name with ``_rescan`` / ``_failed`` / ``_invalid`` when appropriate.
    """
    # Power monitoring is for settled discrete (STATUS=OK) runs only.
    check_power = target_status == "OK"

    def append_readouts() -> bool:
        """Record actual laser readouts immediately after acquisition starts."""
        log["OPO_power"].append(laser.get_opo_power())
        log["IR_power"].append(laser.get_ir_power())
        log["OPO_WAVELENGTH"].append(laser.get_wavelength())
        return _numeric(log["OPO_power"][-1]) and _numeric(log["OPO_WAVELENGTH"][-1])

    def powers_ok(opo, ir) -> bool:
        """True if OPO and IR readouts are within tolerance of config setpoints."""
        if getattr(laser, "dry_run", False) or olympus.dry_run:
            return True
        opo_ok = power_within_tol(opo, opo_setpoint, power_tol)
        ir_ok = power_within_tol(ir, ir_setpoint, power_tol)
        if opo_ok and ir_ok:
            return True
        print(
            f"Power out of tol (±{float(power_tol):.0%}): "
            f"OPO {opo!r} vs {opo_setpoint}, IR {ir!r} vs {ir_setpoint}"
        )
        return False

    def watch_for_fault() -> bool:
        """True if status (or power, in discrete mode) faults while scanning."""
        if olympus.dry_run:
            return False
        while olympus.scanning_single():
            status = laser.status()
            if status != target_status:
                # Open shutter so the laser can retune (same as legacy STEP1).
                laser.shutter(True)
                return True
            # Power only when status is OK (discrete); skip during sweep/hold.
            if check_power and status == "OK":
                opo = laser.get_opo_power()
                ir = laser.get_ir_power()
                if not powers_ok(opo, ir):
                    laser.shutter(True)
                    return True
            time.sleep(2)
        return False

    path = ""
    valid = False
    interrupted = False
    rescans = 0

    # First pass + up to max_rescans retries share the same body.
    while True:
        if not laser.wait_status(target_status, retries=5, interval=10):
            why = " after rescan" if rescans else ""
            print(f"Laser never reached {target_status!r}{why}")
            return False

        # Open shutter BEFORE Olympus starts (legacy STEP1.1). Late shutter
        # blacks out the top of the FOV; early shutter is preferred.
        laser.shutter(True)
        path = olympus.start_single()
        valid = append_readouts()
        status = (
            laser.status()
            if not (getattr(laser, "dry_run", False) or olympus.dry_run)
            else target_status
        )
        if status != target_status:
            interrupted = True
        elif check_power and status == "OK" and not powers_ok(
            log["OPO_power"][-1], log["IR_power"][-1]
        ):
            interrupted = True
        else:
            interrupted = watch_for_fault()

        if not interrupted or rescans >= max_rescans:
            break

        olympus.stop_single()
        laser.shutter(False)
        rescans += 1

    laser.shutter(False)
    olympus.stop_single()

    suffix = "_failed" if interrupted else ("_rescan" if rescans else "")
    if not valid:
        suffix += "_invalid"
    _rename_single(
        path,
        sample,
        log["OPO_WAVELENGTH"][-1],
        log["OPO_power"][-1],
        log["IR_power"][-1],
        suffix,
        log,
        rescans > 0,
    )
    return True


def acquire_matl(
    laser,
    olympus: Olympus,
    sample: str,
    target_status: str,
    log: dict,
    opo_setpoint,
    ir_setpoint,
    power_tol,
    *,
    max_rescans: int = 3,
) -> bool:
    """Run one MATL acquisition at the current laser setpoint.

    *target_status* is ``"OK"`` in discrete (multi-λ / multi-Z) mode and
    ``"hold"`` during APE hardware sweeps.

    *opo_setpoint* / *ir_setpoint* / *power_tol* come from the loaded scan
    config. Power tolerance is enforced only in discrete mode (``target_status``
    ``"OK"``), and only while status remains ``"OK"`` — sweep/hold acquisitions
    skip it, and a non-OK status already triggers the rescan path.

    Returns False only if the laser never reaches *target_status* (caller may
    retry the whole wavelength point). Otherwise returns True and tags .oir
    names with ``_rescan`` / ``_failed`` / ``_invalid`` when appropriate.
    """
    # Power monitoring is for settled discrete (STATUS=OK) runs only.
    check_power = target_status == "OK"

    def append_readouts() -> bool:
        """Record actual laser readouts immediately after MATL starts."""
        log["OPO_power"].append(laser.get_opo_power())
        log["IR_power"].append(laser.get_ir_power())
        log["OPO_WAVELENGTH"].append(laser.get_wavelength())
        return _numeric(log["OPO_power"][-1]) and _numeric(log["OPO_WAVELENGTH"][-1])

    def powers_ok(opo, ir) -> bool:
        """True if OPO and IR readouts are within tolerance of config setpoints."""
        if getattr(laser, "dry_run", False) or olympus.dry_run:
            return True
        opo_ok = power_within_tol(opo, opo_setpoint, power_tol)
        ir_ok = power_within_tol(ir, ir_setpoint, power_tol)
        if opo_ok and ir_ok:
            return True
        print(
            f"Power out of tol (±{float(power_tol):.0%}): "
            f"OPO {opo!r} vs {opo_setpoint}, IR {ir!r} vs {ir_setpoint}"
        )
        return False

    def watch_for_fault() -> bool:
        """True if status (or power, in discrete mode) faults while MATL scans."""
        if olympus.dry_run:
            return False
        while olympus.scanning_matl():
            status = laser.status()
            if status != target_status:
                # Open shutter so the laser can retune (same as legacy STEP1).
                laser.shutter(True)
                return True
            # Power only when status is OK (discrete); skip during sweep/hold.
            if check_power and status == "OK":
                opo = laser.get_opo_power()
                ir = laser.get_ir_power()
                if not powers_ok(opo, ir):
                    laser.shutter(True)
                    return True
            time.sleep(2)
        return False

    path = ""
    valid = False
    interrupted = False
    rescans = 0

    # First pass + up to max_rescans retries share the same body.
    while True:
        if not laser.wait_status(target_status, retries=5, interval=10):
            why = " after rescan" if rescans else ""
            print(f"Laser never reached {target_status!r}{why}")
            return False

        # Open shutter BEFORE Olympus starts. Prefer early shutter over late
        # (late blacks out the start of the scan / top of FOV).
        laser.shutter(True)
        path = olympus.start_matl()
        valid = append_readouts()
        status = laser.status() if not (getattr(laser, "dry_run", False) or olympus.dry_run) else target_status
        if status != target_status:
            interrupted = True
        elif check_power and status == "OK" and not powers_ok(
            log["OPO_power"][-1], log["IR_power"][-1]
        ):
            interrupted = True
        else:
            interrupted = watch_for_fault()

        if not interrupted or rescans >= max_rescans:
            break

        olympus.stop_matl()
        laser.shutter(False)
        rescans += 1

    laser.shutter(False)
    olympus.stop_matl()

    suffix = "_failed" if interrupted else ("_rescan" if rescans else "")
    if not valid:
        suffix += "_invalid"
    _rename_area(
        os.path.dirname(path),
        sample,
        log["OPO_WAVELENGTH"][-1],
        log["OPO_power"][-1],
        log["IR_power"][-1],
        suffix,
        log,
        rescans > 0,
    )
    return True
