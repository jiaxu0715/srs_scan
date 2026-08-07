# APE + Olympus scan pipeline (Python)

Excel-free replacement for `ape_client_S10531.xlsm` + STEP1.

Uses APE’s original `ape_device.py` TCP client. Python drives the laser with the
same ASCII commands Excel used, and Olympus MATL over XML-RPC.

## Files

| File | Role |
|------|------|
| `ape_device.py` | Official APE TCP client (**do not rewrite**) |
| `laser_client.py` | Thin wrapper: timeout, STATUS/shutter/λ/sweep helpers |
| `olympus_client.py` | Fluoview XML-RPC client + MATL acquire/rescan/rename |
| `scan_pipeline.py` | Sweep / discrete loops |
| `parameter_dialog.py` | JSON + optional GUI |
| `run_scan.py` | CLI |

Defaults match the workbook: host `10.84.172.229`, port `51100`.

## Usage

```bash
pip install -r requirements.txt

# Edit config.json (discrete λ list; Z-stack lives in the Olympus MATL protocol), then:
python run_scan.py
python run_scan.py --dry-run
python run_scan.py --config example_sweep.json
python run_scan.py --gui   # optional dialog
```

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
