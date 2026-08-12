# APE + Olympus scan pipeline (Python)

Excel-free replacement for `ape_client_S10531.xlsm` + STEP1.

Uses APE’s original `ape_device.py` TCP client. Python drives the laser with the
same ASCII commands Excel used, and Olympus acquisition over XML-RPC.

## Acquisition modes

| `acquisition` | Behavior |
|---------------|----------|
| `single_fov` | One `MANUAL_MAIN` capture at the current stage FOV. |
| `matl` | Run the loaded Fluoview **MATL** map (multi-area / Z-stack). Default if omitted. |

Laser `mode` is separate: `discrete` (λ list) or `sweep` (APE sweep table).

Shutter opens **before** Olympus starts (avoids cutting off the top of the FOV).

## Files

| File | Role |
|------|------|
| `ape_device.py` | Official APE TCP client (**do not rewrite**) |
| `laser_client.py` | Thin wrapper: timeout, STATUS/shutter/λ/sweep helpers |
| `olympus_client.py` | Fluoview XML-RPC + single-FOV / MATL acquire/rescan/rename |
| `scan_pipeline.py` | Sweep / discrete loops |
| `parameter_dialog.py` | JSON + optional GUI |
| `run_scan.py` | CLI |

Defaults match the workbook: host `10.84.172.229`, port `51100`.

## Usage

```bash
pip install -r requirements.txt

# Edit config.json (discrete λ list + acquisition), then:
python run_scan.py
python run_scan.py --dry-run
python run_scan.py --config config/example_discrete.json   # single_fov
python run_scan.py --config config/example_matl.json       # MATL map
python run_scan.py --config config/example_sweep.json
python run_scan.py --gui   # optional dialog
```

For `matl`, load/save a MATL map with ≥1 area in Fluoview and leave it IDLING
before starting. Z-stack geometry lives in that protocol, not in JSON.

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
