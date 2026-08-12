# APE + Olympus scan pipeline (Python)

Excel-free replacement for `ape_client_S10531.xlsm` + STEP1.

Uses APE’s original `ape_device.py` TCP client. Python drives the laser with the
same ASCII commands Excel used, and Olympus **MANUAL_MAIN** acquisition over
XML-RPC. This branch does **not** depend on Olympus MATL multi-area protocols.

## Acquisition modes

| `acquisition` | Behavior |
|---------------|----------|
| `single_fov` | One capture at the current stage FOV (no columns/rows prompts). |
| `mosaic` | MATL-like grid with columns, rows, Fluoview **zoom**, and overlap. Center is optional `stage_x_um` / `stage_y_um`, or the **current Fluoview stage FOV** if omitted. Each tile is moved to, captured with its own retry + `power_tol` monitoring, then stitched in **pure Python**. |

Mosaic defaults match lab practice: **5% overlap**, snake visit order. Physical FOV is `509.117 µm / zoom` (hardcoded zoom‑1 calibration; scan resolution does not change FOV). Park the stage on the mosaic center before starting (or set both `stage_x_um` / `stage_y_um` to pin the center). Optional `stage_x_sign` / `stage_y_sign` (±1) flip stage axis sense if needed.

## Files

| File | Role |
|------|------|
| `ape_device.py` | Official APE TCP client (**do not rewrite**) |
| `laser_client.py` | Thin wrapper: timeout, STATUS/shutter/λ/sweep helpers |
| `olympus_client.py` | Fluoview XML-RPC + stage + single-FOV acquire/rescan/rename |
| `mosaic.py` | Grid geometry + pure-Python stitch |
| `scan_pipeline.py` | Sweep / discrete loops (single FOV or mosaic) |
| `parameter_dialog.py` | JSON + optional GUI |
| `run_scan.py` | CLI |

Defaults match the workbook: host `10.84.172.229`, port `51100`.

## Usage

```bash
pip install -r requirements.txt

# Edit config.json (discrete λ list; imaging geometry lives in Olympus settings), then:
python run_scan.py
python run_scan.py --dry-run
python run_scan.py --config example_sweep.json
python run_scan.py --config example_mosaic.json --dry-run
python run_scan.py --gui   # optional dialog (mosaic fields appear when acquisition=mosaic)
```

Stitching `.oir` tiles needs `aicsimageio` + bioformats (optional). If stitch fails,
a `{sample}_{λ}nm_tiles.json` manifest is still written for offline stitching.

Set-commands (`EOM=1`, shutter, …) often send no reply. The wrapper sets a 5s
socket timeout on the APE connection so those calls cannot hang forever.

Possible commands, as outlined in the spreadsheet: 
POWER STATE=

POWER STATE?

OPO WAVELENGTH=

OPO WAVELENGTH?

OPO BANDWIDTH?

OPO SPECTRUM?

OPO POWER=

LASER IR POWER=

OPO POWER?

LASER IR POWER?

DELAY REL=

DELAY ABS=

DELAY POS=

CALIBRATE DELAY

DELAY?

DELAY POS?

LASER IR SHUTTER?

OPO SHUTTER?

SYSTEM SHUTTER?

LASER IR SHUTTER=

OPO SHUTTER=

SYSTEM SHUTTER=

AOM=

AOM LOCK=

AOM MODULATION=

AOM DEPTH=

AOM POWER=

AOM RATIO=

AOM?

AOM LOCK?

AOM MODULATION?

AOM DEPTH?

AOM POWER?

AOM RATIO?

EOM=

EOM LOCK=

EOM POWER=

EOM PHASE=

EOM?

EOM LOCK?

EOM POWER?

EOM PHASE?

STATUS?

HUMIDITY?

TEMPERATURE?

ERROR?

MODULATOR?

*IDN?

PARAMETER?

SWEEP=

SWEEP?

SWEEP START

SWEEP NEXT

SWEEP STOP
