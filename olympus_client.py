"""General Olympus / Fluoview XML-RPC client, plus acquisition helpers.

Pipeline role
-------------
Microscope-side API. ``Olympus`` talks to FV over XML-RPC for parameters and
protocol control.

``acquisition`` in the scan config selects the helper:

- ``single_fov`` → ``acquire_single_fov()`` (``EXECUTION_TYPE_MANUAL_MAIN``)
- ``matl`` → ``acquire_matl()`` (Olympus built-in MATL multi-area / Z-stack)

Both open the laser shutter **before** starting the Olympus protocol (late
shutter cuts off the top of the FOV). Optional ``power_monitor`` samples OPO/IR
on each poll for per-wavelength high/low/mean JSON. Z-stack depth, step, and
MATL ROI layout live in the Fluoview protocol — Python does not set them here.
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
# Status strings that mean the laser is ready to image (sweep may report
# either ``hold`` or ``OK`` once a step is reached / if power is adjusted).
_SCAN_READY = ("OK", "hold")
# How often to poll OPO/IR power, shutter, and STATUS while Olympus is scanning.
_SCAN_POLL_S = 0.5
_MAX_RESCANS = 5


def _numeric(value) -> bool:
    """True if *value* can be parsed as a float (used when building filenames)."""
    try:
        float(value)
        return True
    except (TypeError, ValueError):
        return False


def _status_ready(status) -> bool:
    """True if the laser is at a sweep hold or discrete OK (not tuning/error)."""
    return str(status).strip() in _SCAN_READY


def _shutter_still_open(laser, olympus) -> bool:
    """True if the system shutter is still open; logs and returns False if it dropped."""
    if getattr(laser, "dry_run", False) or getattr(olympus, "dry_run", False):
        return True
    if laser.shutter_is_open():
        return True
    print("Shutter closed unexpectedly during acquisition")
    return False


def _log_status_retry(status) -> None:
    print(f"Laser status {status!r} during acquisition; retrying")


def _restore_laser_power(laser, opo_setpoint, ir_setpoint) -> None:
    """On retry: OPO/IR → 0, pause, then restore config setpoints."""
    laser.restore_power(opo_setpoint, ir_setpoint)


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
        response = self.proxy.Protocol.startProtocol(_MANUAL)
        if not isinstance(response, dict) or "targetName" not in response:
            raise RuntimeError(
                f"Olympus refused MANUAL_MAIN start (no targetName): {response!r}"
            )
        return response["targetName"][0]["name"]

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
        """True if MANUAL_MAIN has finished and returned to IDLING."""
        if self.dry_run:
            return True
        progress = self.proxy.Protocol.getProtocolProgress(_MANUAL)
        return progress.get("state") == "IDLING"

    # --- PROTOCOL CONTROLS (MATL) ---
    def start_matl(self) -> str:
        """Start MATL; returns path of the first .oir the microscope will write."""
        if self.dry_run:
            path = os.path.join(os.getcwd(), "dryrun_area", "dryrun.oir")
            print(f"[dry-run] Olympus start_matl → {path}")
            return path
        progress = self.proxy.Protocol.getProtocolProgress(_MATL)
        print(f"Olympus MATL progress before start: {progress!r}", flush=True)
        response = self.proxy.Protocol.startProtocol(_MATL)
        print(f"Olympus startProtocol: {response!r}", flush=True)
        if not isinstance(response, dict) or "targetName" not in response:
            raise RuntimeError(
                "Olympus refused MATL start (no targetName). "
                f"progress={progress!r} response={response!r}. "
                "In Fluoview: load a MATL map with at least one area, "
                "set the save folder, and leave the protocol IDLING."
            )
        return response["targetName"][0]["name"]

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

    Multi-Z / multi-ROI: one MATL run can produce several .oir files.
    Each becomes ``{sample}_roi{i}_{λ}_{opo}_{ir}{suffix}.oir``.
    Fluoview also writes small ``Map_*.oir`` / ``Stitch_*.oir`` overviews;
    those are left as-is so they do not shift the ROI index.
    """
    if not os.path.isdir(area_dir):
        return
    # Laser reports λ in tenths of nm; filenames use nm.
    tag = float(wavelength) / 10.0 if _numeric(wavelength) else wavelength
    files = sorted(
        f
        for f in os.listdir(area_dir)
        if f.endswith(".oir") and "map" not in f.lower() and "stitch" not in f.lower()
    )
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
    tag = float(wavelength) / 10.0 if _numeric(wavelength) else wavelength
    directory = os.path.dirname(path) or os.getcwd()
    ext = os.path.splitext(path)[1] or ".oir"
    new = os.path.join(directory, f"{sample}_{tag}_{opo}_{ir}{suffix}{ext}")
    log["Original_Filename"].append(path)
    log["New_Filename"].append(new)
    log["Rescan"].append(rescanned)
    if not os.path.isfile(path):
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
    max_rescans: int = _MAX_RESCANS,
    power_monitor=None,
) -> bool:
    """Run one MANUAL_MAIN single-FOV acquisition at the current laser setpoint.

    *target_status* is ``"OK"`` in discrete mode and ``"hold"`` during APE
    hardware sweeps. During the Olympus scan, OPO/IR are compared to the
    config setpoints regardless of whether STATUS is ``OK`` or ``hold``
    (changing power in sweep often leaves STATUS at ``OK``). The system
    shutter is polled and must stay open until this function closes it.
    Out-of-tol power or an unexpected shutter close re-applies setpoints
    (power) / re-opens the shutter and rescans. When *power_monitor* is
    set, OPO/IR are sampled on every scan poll for high/low/mean logging.

    Returns False only if the laser never reaches a ready status (caller may
    retry the whole wavelength point).
    """
    def append_readouts() -> bool:
        opo = laser.get_opo_power()
        ir = laser.get_ir_power()
        log["OPO_power"].append(opo)
        log["IR_power"].append(ir)
        log["OPO_WAVELENGTH"].append(laser.get_wavelength())
        if power_monitor is not None:
            power_monitor.sample(opo, ir)
        return _numeric(opo) and _numeric(log["OPO_WAVELENGTH"][-1])

    def powers_ok(opo, ir) -> bool:
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
        if olympus.dry_run:
            return False
        while olympus.scanning_single():
            opo = laser.get_opo_power()
            ir = laser.get_ir_power()
            if power_monitor is not None:
                power_monitor.sample(opo, ir)
            if not powers_ok(opo, ir):
                laser.shutter(True)
                return True
            if not _shutter_still_open(laser, olympus):
                return True
            status = laser.status()
            if not _status_ready(status):
                _log_status_retry(status)
                laser.shutter(True)
                return True
            time.sleep(_SCAN_POLL_S)
        return False

    path = ""
    valid = False
    interrupted = False
    rescans = 0

    while True:
        if not laser.wait_status(_SCAN_READY, retries=5, interval=10):
            why = " after rescan" if rescans else ""
            print(f"Laser never reached {target_status!r}{why}")
            return False

        # Open shutter BEFORE Olympus starts (late shutter cuts off FOV top).
        laser.shutter(True)
        path = olympus.start_single()
        valid = append_readouts()
        status = (
            laser.status()
            if not (getattr(laser, "dry_run", False) or olympus.dry_run)
            else target_status
        )
        if not powers_ok(log["OPO_power"][-1], log["IR_power"][-1]):
            interrupted = True
        elif not _shutter_still_open(laser, olympus):
            interrupted = True
        elif not _status_ready(status):
            _log_status_retry(status)
            interrupted = True
        else:
            interrupted = watch_for_fault()

        if not interrupted or rescans >= max_rescans:
            break

        olympus.stop_single()
        laser.shutter(False)
        _restore_laser_power(laser, opo_setpoint, ir_setpoint)
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
    max_rescans: int = _MAX_RESCANS,
    power_monitor=None,
) -> bool:
    """Run one Olympus MATL acquisition at the current laser setpoint.

    Uses the loaded Fluoview MATL map (multi-area / Z-stack). During the
    Olympus scan, OPO/IR are compared to the config setpoints for both
    discrete (``OK``) and sweep (``hold`` or ``OK``). The system shutter
    is polled and must stay open until this function closes it. Out-of-tol
    readings re-apply those setpoints, then rescan. When *power_monitor*
    is set, OPO/IR are sampled on every scan poll for high/low/mean logging.
    """
    def append_readouts() -> bool:
        opo = laser.get_opo_power()
        ir = laser.get_ir_power()
        log["OPO_power"].append(opo)
        log["IR_power"].append(ir)
        log["OPO_WAVELENGTH"].append(laser.get_wavelength())
        if power_monitor is not None:
            power_monitor.sample(opo, ir)
        return _numeric(opo) and _numeric(log["OPO_WAVELENGTH"][-1])

    def powers_ok(opo, ir) -> bool:
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
        if olympus.dry_run:
            return False
        while olympus.scanning_matl():
            opo = laser.get_opo_power()
            ir = laser.get_ir_power()
            if power_monitor is not None:
                power_monitor.sample(opo, ir)
            if not powers_ok(opo, ir):
                laser.shutter(True)
                return True
            if not _shutter_still_open(laser, olympus):
                return True
            status = laser.status()
            if not _status_ready(status):
                _log_status_retry(status)
                laser.shutter(True)
                return True
            time.sleep(_SCAN_POLL_S)
        return False

    path = ""
    valid = False
    interrupted = False
    rescans = 0

    while True:
        if not laser.wait_status(_SCAN_READY, retries=5, interval=10):
            why = " after rescan" if rescans else ""
            print(f"Laser never reached {target_status!r}{why}")
            return False

        # Open shutter BEFORE Olympus starts.
        laser.shutter(True)
        path = olympus.start_matl()
        valid = append_readouts()
        status = (
            laser.status()
            if not (getattr(laser, "dry_run", False) or olympus.dry_run)
            else target_status
        )
        if not powers_ok(log["OPO_power"][-1], log["IR_power"][-1]):
            interrupted = True
        elif not _shutter_still_open(laser, olympus):
            interrupted = True
        elif not _status_ready(status):
            _log_status_retry(status)
            interrupted = True
        else:
            interrupted = watch_for_fault()

        if not interrupted or rescans >= max_rescans:
            break

        olympus.stop_matl()
        laser.shutter(False)
        _restore_laser_power(laser, opo_setpoint, ir_setpoint)
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
