#!/usr/bin/env python3
"""CLI entry point for the multi-Z / multi-λ scan pipeline.

Pipeline role
-------------
Thin launcher: load ``config.json`` (or another JSON path), print the recipe,
and call ``scan_pipeline.run_pipeline``. Does not talk to hardware itself.

Typical multi-Z workflow
------------------------
  1. Configure Z-stack + ROIs in the Olympus MATL protocol (Fluoview).
  2. Edit ``config.json``: set ``sample_name`` and the ``scans`` wavelength list.
  3. Run: ``python run_scan.py``  (or ``--dry-run`` to test without hardware).

File stack (bottom → top): ``ape_device`` → ``laser_client`` → ``scan_pipeline``
with ``olympus_client`` for acquisition and ``parameter_dialog`` for config.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from laser_client import DEFAULT_HOST, DEFAULT_PORT
from olympus_client import DEFAULT_OLYMPUS_URL
from parameter_dialog import load_json
from scan_pipeline import run_pipeline

# Default recipe when no --config is passed.
DEFAULT_CONFIG = Path(__file__).resolve().parent / "config.json"


def _load_params(*, config: Path | None, gui: bool) -> dict | None:
    """JSON file by default; optional tkinter dialog with ``--gui``."""
    if gui:
        from parameter_dialog import ParameterDialog

        return ParameterDialog().get_parameters()
    path = config or DEFAULT_CONFIG
    if not path.is_file():
        print(f"Config not found: {path}", file=sys.stderr)
        print("Create config.json or pass --config / --gui", file=sys.stderr)
        return None
    return load_json(path)


def main(argv=None) -> int:
    p = argparse.ArgumentParser(description="APE + Olympus MATL scan (JSON config)")
    p.add_argument(
        "--config",
        type=Path,
        default=None,
        help=f"parameter JSON (default: {DEFAULT_CONFIG.name})",
    )
    p.add_argument("--gui", action="store_true", help="open parameter dialog instead of JSON")
    p.add_argument("--laser-host", default=DEFAULT_HOST)
    p.add_argument("--laser-port", type=int, default=DEFAULT_PORT)
    p.add_argument("--olympus-url", default=DEFAULT_OLYMPUS_URL)
    p.add_argument("--log-xlsx", default=None, help="override daily Excel log path")
    p.add_argument("--dry-run", action="store_true", help="print commands; skip laser and Olympus")
    args = p.parse_args(argv)

    if args.gui and args.config is not None:
        p.error("use either --config or --gui, not both")

    params = _load_params(config=args.config, gui=args.gui)
    if not params:
        print("Cancelled")
        return 1

    print(json.dumps(params, indent=2))
    run_pipeline(
        params,
        laser_host=args.laser_host,
        laser_port=args.laser_port,
        olympus_url=args.olympus_url,
        dry_run=args.dry_run,
        log_xlsx=args.log_xlsx,
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
