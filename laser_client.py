"""Lab-specific wrapper around ``ape_device`` for picoEmerald scan control.

Pipeline role
-------------
Middle layer between raw TCP (``ape_device``) and scan orchestration
(``scan_pipeline``). Translates human units (nm, mW) into the exact ASCII
commands the old Excel ``ape_execute`` macro sent, and handles laser quirks:

- wavelength in tenths of nm (787.0 nm → ``7870``)
- delay calibration (``DELAY ABS = -1.6336 * λ_nm + 9527.2``)
- socket timeout so set-commands without a reply do not hang forever
- ``wait_status()`` polling instead of fixed sleeps after tuning

For multi-Z acquisition, callers set λ / power / delay here; Olympus MATL
runs the Z-stack defined in the saved microscope protocol.
"""

from __future__ import annotations

import socket
import time

from ape_device import ape_device

# Network defaults copied from ape_client_S10531.xlsm cells B6 / B7.
DEFAULT_HOST = "10.84.172.229"
DEFAULT_PORT = 51100

# Drop power to 0 this long before restoring setpoints on a power-tol retry.
_POWER_RETUNE_S = 3.0
# Delay stage calibration used in STEP1 (same linear fit as the Excel scripts).
DELAY_SLOPE = -10.181
#DELAY_SLOPE = -1.6336
DELAY_INTERCEPT = 16563
#DELAY_INTERCEPT = 9527.2


def nm_to_tenths(wavelength_nm: float) -> int:
    """Convert nm to the integer tenths-of-nm the APE command expects."""
    return int(round(float(wavelength_nm) * 10))


def delay_abs(wavelength_nm: float) -> int:
    """Compute ``DELAY ABS`` setpoint for a given OPO wavelength (nm)."""
    return int(DELAY_SLOPE * float(wavelength_nm) + DELAY_INTERCEPT)


def power_within_tol(measured, setpoint, tol: float) -> bool:
    """True if *measured* is within ±*tol* (relative) of *setpoint*."""
    try:
        m, s = float(measured), float(setpoint)
    except (TypeError, ValueError):
        return False
    if s == 0:
        return m == 0
    return abs(m - s) / abs(s) <= float(tol)


class Laser:
    """High-level picoEmerald API used by ``scan_pipeline`` and ``olympus_client``."""

    def __init__(
        self,
        host: str = DEFAULT_HOST,
        port: int = DEFAULT_PORT,
        *,
        timeout_s: float = 5.0,
        dry_run: bool = False,
    ):
        self.dry_run = dry_run
        self.dev = None
        self._timeout_s = float(timeout_s)
        if dry_run:
            print(f"[dry-run] would connect to {host}:{port}")
            return
        self.dev = ape_device(host=host, port=port, name="picoEmerald")
        if not self.dev.connected or self.dev.dev is None:
            raise RuntimeError(f"Failed to connect to APE laser at {host}:{port}")
        # Set-commands often send no reply; without a timeout receive() hangs forever.
        self.dev.dev.settimeout(self._timeout_s)

    def close(self) -> None:
        if self.dev is not None and self.dev.connected:
            self.dev.disconnect()
            self.dev = None

    def __enter__(self) -> "Laser":
        return self

    def __exit__(self, *_exc) -> None:
        self.close()

    def _drain_rx(self) -> None:
        """Discard leftover bytes so a late set-command reply cannot poison a query."""
        if self.dry_run or self.dev is None or self.dev.dev is None:
            return
        sock = self.dev.dev
        old = sock.gettimeout()
        try:
            sock.settimeout(0.0)
            while True:
                chunk = sock.recv(4096)
                if not chunk:
                    break
        except (BlockingIOError, ConnectionResetError, OSError, socket.timeout):
            pass
        finally:
            try:
                sock.settimeout(old if old is not None else self._timeout_s)
            except OSError:
                pass

    def cmd(self, command: str) -> str:
        """Send one raw APE command; normalize empty set-command replies.

        Routine command/response traffic is silent. Callers print measured
        values only on retry or out-of-tolerance faults.
        """
        command = str(command).strip()
        if self.dry_run:
            print(f"[dry-run] {command}")
            return "OK" if command.upper() == "STATUS?" or not command.endswith("?") else "0"
        if command.endswith("?"):
            self._drain_rx()
        try:
            response = self.dev.query(command)
        except Exception:
            # Set-commands frequently time out with an empty reply (same as Excel VBA).
            if not command.endswith("?"):
                return "OK"
            raise
        if response == "" and not command.endswith("?"):
            response = "OK"
        return response

    def status(self) -> str:
        return self.cmd("STATUS?")

    def restore_power(self, opo_setpoint, ir_setpoint, *, settle_s: float = _POWER_RETUNE_S) -> None:
        """Zero OPO/IR, wait, then re-apply config setpoints so the laser retunes.

        STATUS can stay ``OK`` while measured power is out of tol; a direct
        re-set is ignored. Dropping to 0 forces a retune, then we restore
        the config targets.
        """
        print(
            f"Power retune: OPO/IR → 0 for {float(settle_s):.0f}s, "
            f"then OPO={opo_setpoint}, IR={ir_setpoint}"
        )
        self.set_opo_power(0)
        self.set_ir_power(0)
        if not self.dry_run:
            time.sleep(float(settle_s))
        self.set_opo_power(opo_setpoint)
        self.set_ir_power(ir_setpoint)

    def wait_status(
        self,
        target: str | tuple[str, ...] = "OK",
        *,
        retries: int = 1000,
        interval: float = 3.0,
    ) -> bool:
        """Poll ``STATUS?`` until it matches *target* (``OK`` for discrete, ``hold`` for sweep).

        *target* may be one status string or a tuple of acceptable statuses.
        Prefer this over a fixed sleep after wavelength changes — returns as soon
        as tuning finishes instead of waiting a worst-case settle time.
        """
        if self.dry_run:
            return True
        targets = {target} if isinstance(target, str) else set(target)
        for _attempt in range(retries):
            current = str(self.status()).strip()
            if current in targets:
                return True
            time.sleep(interval)
        print(f"Laser status never reached {sorted(targets)!r} after {retries} polls")
        return False

    def shutter(self, open_: bool) -> str:
        """Open/close the system shutter that gates excitation during acquisition.

        Sends the set-command and returns immediately. APE often sends no ACK;
        waiting for one parks the beam until the 5 s socket timeout, then
        Fluoview start is delayed by that same gap.
        """
        command = f"System Shutter={1 if open_ else 0}"
        if self.dry_run:
            print(f"[dry-run] {command}")
            return "OK"
        if self.dev is None:
            raise RuntimeError("Laser is not connected")
        self.dev.send(command)
        return "OK"

    def shutter_is_open(self) -> bool:
        """True if ``System Shutter?`` reports open (1). Dry-run always True."""
        if self.dry_run:
            return True
        raw = str(self.cmd("System Shutter?")).strip()
        try:
            return float(raw) != 0
        except (TypeError, ValueError):
            lowered = raw.lower()
            if lowered in {"on", "open", "true"}:
                return True
            if lowered in {"off", "closed", "close", "false", ""}:
                return False
            return False

    def enable_eom(self) -> str:
        """Enable the electro-optic modulator (done once at the start of a run)."""
        return self.cmd("EOM=1")

    def set_wavelength_nm(self, nm: float) -> str:
        return self.cmd(f"OPO WAVELENGTH={nm_to_tenths(nm)}")

    def set_opo_power(self, power) -> str:
        return self.cmd(f"OPO POWER={power}")

    def set_ir_power(self, power) -> str:
        return self.cmd(f"LASER IR POWER={power}")

    def set_delay_nm(self, nm: float) -> str:
        """Set pulse delay for CARS/SHG phase matching at this wavelength."""
        return self.cmd(f"DELAY ABS={delay_abs(nm)}")

    def get_opo_power(self):
        return self.cmd("OPO POWER?")

    def get_ir_power(self):
        return self.cmd("LASER IR POWER?")

    def get_wavelength(self):
        return self.cmd("OPO WAVELENGTH?")

    def sweep_config(self, start_nm: float, end_nm: float, n_intervals: int, dwell: int = 300) -> str:
        return self.cmd(
            f"SWEEP={nm_to_tenths(start_nm)};{nm_to_tenths(end_nm)};{n_intervals};{dwell}"
        )

    def sweep_start(self) -> str:
        return self.cmd("SWEEP START")

    def sweep_next(self) -> str:
        return self.cmd("SWEEP NEXT")