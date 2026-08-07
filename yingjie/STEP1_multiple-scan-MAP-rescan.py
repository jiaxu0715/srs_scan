import tqdm
import xlwings as xw
import socket
import os
import xmlrpc.client
import sys
import time
import numpy as np
from datetime import datetime
import pandas as pd
from pathlib import Path
import importlib.util

module_path = Path(__file__).parent / "dialogue_box-multiple-scan-MAP-errorhandle.py"

spec = importlib.util.spec_from_file_location(
    "dialogue_box_multiple_scan_MAP",
    module_path
)

dialogue_box_multiple_scan_MAP = importlib.util.module_from_spec(spec)
spec.loader.exec_module(dialogue_box_multiple_scan_MAP)

ParameterDialog = dialogue_box_multiple_scan_MAP.ParameterDialog

def run_excel_macro(excel, sheet, query_cell, query, macro_name):

    # Input the query into the specified cell
    sheet.range(query_cell).value = query

    sheet.range(query_cell).select()

    # Run the macro
    excel.macro(macro_name)()
    excel.save()
    response = sheet.range('B10').value
    return response


def wait_for_status_ok(excel, sheet, status_cell, macro_name, target_status="OK", poll_interval=3, max_attempts=1000):
    for attempt in range(max_attempts):
        status = run_excel_macro(excel, sheet, status_cell, "STATUS?", macro_name)
        if status == target_status:
            return True
        time.sleep(poll_interval)
    return False

def rename_with_retry(src, dst, max_attempts=10, poll_interval=1):
    for attempt in range(max_attempts):
        try:
            os.rename(src, dst)
            return True
        except PermissionError:
            time.sleep(poll_interval)
    return False

def is_numeric(value):
    try:
        float(value)
        return True
    except (TypeError, ValueError):
        return False

def build_area_filenames(area_dir, extension, sample_name, wavelength, opo_power, ir_power, suffix=""):
    fnames = sorted(f for f in os.listdir(area_dir) if f.endswith('.' + extension))

    if is_numeric(wavelength):
        wavelength_tag = wavelength / 10
    else:
        wavelength_tag = wavelength

    renamed_pairs = []
    for idx, fname in enumerate(fnames, start=1):
        old_path = os.path.join(area_dir, fname)
        new_name = "{}_roi{}_{}_{}_{}{}.{}".format(
            sample_name, idx, wavelength_tag, opo_power, ir_power, suffix, extension)
        new_path = os.path.join(area_dir, new_name)
        renamed_pairs.append((old_path, new_path))
    return renamed_pairs


# handle error when laser is in bad status during scanning
def wait_for_matl_completion_with_status_check(excel, sheet, status_cell, macro_name, proxy, target_status, poll_interval=2):
    while True:
        inputVal = {"executionType": "EXECUTION_TYPE_MATL"}
        result = proxy.Protocol.getProtocolProgress(inputVal)
        if result['state'] != "SCANNING":
            return False
        status = run_excel_macro(excel, sheet, status_cell, "STATUS?", macro_name)
        if status != target_status:
            run_excel_macro(excel, sheet, 'B28', "System Shutter=1", macro_name)
            return True
        time.sleep(poll_interval)

def wait_for_status_retry(excel, sheet, status_cell, macro_name,
                          target_status="OK", retries=5, interval=10):

    for attempt in range(retries):
        status = run_excel_macro(
            excel,
            sheet,
            status_cell,
            "STATUS?",
            macro_name
        )

        if status == target_status:
            return True

        print(f"Laser status = {status}, retry {attempt+1}/{retries}")
        time.sleep(interval)

    return False

def acquire_matl_and_rename(
        excel, sheet, proxy, macro_name, sample_name, target_status,
        OPO_power, IR_power, OPO_WAVELENGTH,
        Original_Filename, New_Filename, Rescan,
        requested_wavelength=None,
        max_rescans=3):

    inputVal = {"executionType": "EXECUTION_TYPE_MATL"}

    if not wait_for_status_retry(
            excel,
            sheet,
            'B39',
            macro_name,
            target_status,
            retries=5,
            interval=10):
        print(
            f"Laser never became ready after retries "
            f"(requested wavelength = {requested_wavelength / 10:.1f} nm)."
        )
        return False

    result = proxy.Protocol.startProtocol(inputVal)
    targetName = result['targetName']
    fileName = targetName[0]
    fileName = fileName['name']

    OPO_power.append(run_excel_macro(excel, sheet, 'B40', "OPO POWER?", macro_name))
    IR_power.append(run_excel_macro(excel, sheet, 'B42', "LASER IR POWER?", macro_name))
    OPO_WAVELENGTH.append(run_excel_macro(excel, sheet, 'B41', "OPO WAVELENGTH?", macro_name))

    opo_valid = is_numeric(OPO_power[-1]) and is_numeric(OPO_WAVELENGTH[-1])

    run_excel_macro(excel, sheet, 'B28', "System Shutter=1", macro_name)

    interrupted = wait_for_matl_completion_with_status_check(
        excel, sheet, 'B39', macro_name, proxy, target_status)

    rescan_count = 0

    while interrupted and rescan_count < max_rescans:

        proxy.Protocol.stopProtocol(inputVal)
        run_excel_macro(excel, sheet, 'B29', "System Shutter=0", macro_name)

        wait_for_status_ok(
            excel,
            sheet,
            'B39',
            macro_name,
            target_status=target_status
        )

        if not wait_for_status_retry(
                excel,
                sheet,
                'B39',
                macro_name,
                target_status,
                retries=5,
                interval=10):
            print(
                f"Laser never became ready after retries "
                f"(requested wavelength = {requested_wavelength / 10:.1f} nm)."
            )
            return False

        result = proxy.Protocol.startProtocol(inputVal)
        targetName = result['targetName']
        fileName = targetName[0]
        fileName = fileName['name']

        OPO_power.append(run_excel_macro(excel, sheet, 'B40', "OPO POWER?", macro_name))
        IR_power.append(run_excel_macro(excel, sheet, 'B42', "LASER IR POWER?", macro_name))
        OPO_WAVELENGTH.append(run_excel_macro(excel, sheet, 'B41', "OPO WAVELENGTH?", macro_name))

        opo_valid = is_numeric(OPO_power[-1]) and is_numeric(OPO_WAVELENGTH[-1])

        run_excel_macro(excel, sheet, 'B28', "System Shutter=1", macro_name)

        interrupted = wait_for_matl_completion_with_status_check(
            excel, sheet, 'B39', macro_name, proxy, target_status)

        rescan_count += 1

    run_excel_macro(excel, sheet, 'B29', "System Shutter=0", macro_name)
    proxy.Protocol.stopProtocol(inputVal)

    if interrupted:
        suffix = "_failed"
    elif rescan_count > 0:
        suffix = "_rescan"
    else:
        suffix = ""

    if not opo_valid:
        suffix += "_invalid"

    area_dir = os.path.dirname(fileName)

    renamed_pairs = build_area_filenames(
        area_dir,
        'oir',
        sample_name,
        OPO_WAVELENGTH[-1],
        OPO_power[-1],
        IR_power[-1],
        suffix
    )

    for old_path, new_path in renamed_pairs:
        renamed = rename_with_retry(old_path, new_path)

        if renamed:
            Original_Filename.append(old_path)
            New_Filename.append(new_path)
            Rescan.append(rescan_count > 0)
        else:
            print(f"Could not rename {old_path}, file still locked after retries")

    return True


if __name__=="__main__":
    dialog = ParameterDialog()
    parameters = dialog.get_parameters()

    if not parameters:
        print("Dialog was cancelled")
        sys.exit()

    mode = parameters['mode']
    imaging_time = parameters['imaging_time']
    sample_name = parameters['sample_name']

    file_path = r'C:\Users\yilai\Downloads\ape_client_S10531.xlsm'
    url = "http://127.0.0.1:8080/xmlrpc"
    proxy = xmlrpc.client.ServerProxy(url)

    try:
        app=xw.App(visible=False)
        excel = app.books.open(file_path)

        sheet = excel.sheets[0]

    except Exception as e:
        print(f"Failed to open the workbook: {e}")
    else:
        macro_name = 'ape_execute'
        run_excel_macro(excel, sheet, 'B8', '', macro_name)
        date_str = datetime.now().strftime('%Y%m%d')
        filename = f'{date_str}.xlsx'

        run_excel_macro(excel, sheet, 'B25', "EOM=1", macro_name)
        time.sleep(1)

        OPO_power = []
        IR_power = []
        OPO_WAVELENGTH = []
        # Filename = []
        Original_Filename = []
        New_Filename = []
        Rescan = []

        if mode == 'sweep':
            start_wavelength = int(parameters['start_wavelength']*10)
            end_wavelength = int(parameters['end_wavelength']*10)
            step_size = parameters['step_size']
            sample = int((end_wavelength-start_wavelength)/(step_size*10))

            print(f"Starting measurement from {start_wavelength} Angstrom to {end_wavelength} Angstrom")
            print(f"Step size: {step_size} nm")
            print(f"Imaging time: {imaging_time} s")

            run_excel_macro(excel, sheet, 'B48', "SWEEP={};{};{};300".format(start_wavelength,end_wavelength,sample), macro_name)
            print("Sweep in Tuning, start in 100 second!")
            run_excel_macro(excel, sheet, 'B27', "SWEEP START", macro_name)
            time.sleep(100)

            for i in tqdm.tqdm(range(sample + 1)):
                y = -1.6336 * (
                            start_wavelength / 10 + i * (((end_wavelength - start_wavelength) / sample) / 10)) + 9527.2
                run_excel_macro(excel, sheet, 'B70', "DELAY ABS={}".format(int(y)), macro_name)

                status_ok = wait_for_status_ok(excel, sheet, 'B39', macro_name, target_status="hold")
                if not status_ok:
                    print(f"Status did not reach hold for step {i}, skipping")
                    continue

                current_wavelength = start_wavelength + int(
                    i * (end_wavelength - start_wavelength) / sample
                )

                while True:

                    success = acquire_matl_and_rename(
                        excel, sheet, proxy, macro_name, sample_name, "hold",
                        OPO_power, IR_power, OPO_WAVELENGTH,
                        Original_Filename, New_Filename, Rescan,
                        requested_wavelength=current_wavelength
                    )

                    if success:
                        break

                    print(f"Retrying {current_wavelength / 10:.1f} nm...")

                run_excel_macro(excel, sheet, 'B30', "SWEEP NEXT", macro_name)

        else:
            for scan in parameters['scans']:
                wavelength_value = int(scan['wavelength']*10)
                opo_power_value = scan['opo_power']
                ir_power_value = scan['ir_power']

                run_excel_macro(excel, sheet, 'B41', "OPO WAVELENGTH={}".format(wavelength_value), macro_name)
                run_excel_macro(excel, sheet, 'B40', "OPO POWER={}".format(opo_power_value), macro_name)
                run_excel_macro(excel, sheet, 'B42', "LASER IR POWER={}".format(ir_power_value), macro_name)
                y = -1.6336 * (wavelength_value / 10) + 9527.2
                run_excel_macro(excel, sheet, 'B70', "DELAY ABS={}".format(int(y)), macro_name)

                status_ok = wait_for_status_ok(excel, sheet, 'B39', macro_name)
                if not status_ok:
                    print(f"Status did not reach OK for wavelength {scan['wavelength']} nm, skipping")
                    continue

                while True:

                    success = acquire_matl_and_rename(
                        excel, sheet, proxy, macro_name, sample_name, "OK",
                        OPO_power, IR_power, OPO_WAVELENGTH,
                        Original_Filename, New_Filename, Rescan,
                        requested_wavelength=wavelength_value
                    )

                    if success:
                        break

                    print(f"Retrying {wavelength_value / 10:.1f} nm...")

                    run_excel_macro(
                        excel, sheet, 'B41',
                        f"OPO WAVELENGTH={wavelength_value}",
                        macro_name
                    )

                    y = -1.6336 * (wavelength_value / 10) + 9527.2
                    run_excel_macro(
                        excel, sheet, 'B70',
                        f"DELAY ABS={int(y)}",
                        macro_name
                    )

        excel.close()
        comment = ''  # change every time
        columns = ['Original Filename', 'New Filename', 'OPO WAVELENGTH', 'OPO POWER', 'IR POWER', 'Rescan', 'Comment']
        data = {'Original Filename': [Original_Filename],
                'New Filename': [New_Filename],
                'OPO WAVELENGTH': [OPO_WAVELENGTH],
                'OPO POWER': [OPO_power],
                'IR POWER': [IR_power],
                'Rescan': [Rescan],
                'Comment': [comment]
                }
        df = pd.DataFrame(data, columns=columns)
        if not os.path.isfile(filename):
            updated_df = df
        else:
            existing_df = pd.read_excel(filename, sheet_name='Sheet1')
            if isinstance(existing_df, pd.DataFrame):
                updated_df = pd.concat([existing_df, df], ignore_index=True)
            else:
                raise TypeError("Loaded object is not a DataFrame.")
        with pd.ExcelWriter(filename, engine='openpyxl') as writer:
            updated_df.to_excel(writer, index=False, sheet_name='Sheet1')