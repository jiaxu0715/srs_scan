"""Per-wavelength OPO/IR power statistics collected during acquisition polling.

Pipeline role
-------------
While Olympus is scanning, ``olympus_client`` polls laser power. This module
accumulates those readings per wavelength and writes a JSON summary with
high / low / mean (and sample count) for OPO and IR.

Typical flow (from ``scan_pipeline``)::

    mon = PowerMonitor(sample_name=..., comment=...)
    mon.begin(wavelength_nm, opo_setpoint, ir_setpoint)
    # acquire_* calls mon.sample(opo, ir) on each poll
    mon.end()
    mon.save_json("YYYYMMDD_power.json")
"""

from __future__ import annotations

import json
import os
from datetime import datetime
from typing import Any


def _as_float(value) -> float | None:
    """Parse a laser readout as float; return None if non-numeric."""
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


class _ChannelStats:
    """Running high / low / sum / count for one power channel."""

    __slots__ = ("high", "low", "sum", "n", "samples")

    def __init__(self) -> None:
        self.high: float | None = None
        self.low: float | None = None
        self.sum = 0.0
        self.n = 0
        self.samples: list[float] = []

    def add(self, value) -> None:
        v = _as_float(value)
        if v is None:
            return
        if self.high is None or v > self.high:
            self.high = v
        if self.low is None or v < self.low:
            self.low = v
        self.sum += v
        self.n += 1
        self.samples.append(v)

    def summary(self) -> dict[str, Any]:
        if self.n == 0:
            return {"high": None, "low": None, "mean": None, "n": 0}
        return {
            "high": self.high,
            "low": self.low,
            "mean": self.sum / self.n,
            "n": self.n,
        }


class PowerMonitor:
    """Collect OPO/IR power stats for each wavelength in a run."""

    def __init__(self, *, sample_name: str = "", comment: str = "") -> None:
        self.sample_name = sample_name
        self.comment = comment
        self.records: list[dict[str, Any]] = []
        self._opo: _ChannelStats | None = None
        self._ir: _ChannelStats | None = None
        self._meta: dict[str, Any] | None = None

    def begin(
        self,
        wavelength_nm: float,
        opo_setpoint=None,
        ir_setpoint=None,
    ) -> None:
        """Start a new wavelength session (call before acquire for that λ)."""
        if self._meta is not None:
            self.end()
        self._opo = _ChannelStats()
        self._ir = _ChannelStats()
        self._meta = {
            "wavelength_nm": float(wavelength_nm),
            "opo_setpoint": opo_setpoint,
            "ir_setpoint": ir_setpoint,
        }

    def sample(self, opo, ir) -> None:
        """Record one polled OPO/IR pair (no-op if begin() was not called)."""
        if self._opo is None or self._ir is None:
            return
        self._opo.add(opo)
        self._ir.add(ir)

    def end(self) -> dict[str, Any] | None:
        """Finish the current wavelength and append its summary. Returns the record."""
        if self._meta is None or self._opo is None or self._ir is None:
            return None
        record = {
            **self._meta,
            "opo": self._opo.summary(),
            "ir": self._ir.summary(),
            "opo_samples": list(self._opo.samples),
            "ir_samples": list(self._ir.samples),
        }
        self.records.append(record)
        self._meta = None
        self._opo = None
        self._ir = None
        return record

    def to_dict(self) -> dict[str, Any]:
        """Full run payload for JSON serialization."""
        if self._meta is not None:
            self.end()
        return {
            "sample_name": self.sample_name,
            "comment": self.comment,
            "created": datetime.now().isoformat(timespec="seconds"),
            "wavelengths": list(self.records),
        }

    def save_json(self, path: str | None = None) -> str:
        """Write power stats JSON; default ``YYYYMMDD_power.json``."""
        if self._meta is not None:
            self.end()
        path = path or f"{datetime.now():%Y%m%d}_power.json"
        parent = os.path.dirname(path)
        if parent:
            os.makedirs(parent, exist_ok=True)
        with open(path, "w", encoding="utf-8") as f:
            json.dump(self.to_dict(), f, indent=2)
        print(f"Wrote power log → {path}")
        return path
