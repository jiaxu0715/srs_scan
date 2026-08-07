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
from dialogue_box_sweep_single import ParameterDialog

def run_excel_macro(excel, sheet, query_cell, query, macro_name):

    # Input the query into the specified cell
    sheet.range(query_cell).value = query

    sheet.range(query_cell).select()

    # Run the macro
    excel.macro(macro_name)()
    excel.save()
    response = sheet.range('B10').value
    return response

# def rename_with_retry(src, dst, max_attempts=10, poll_interval=1):
#     for attempt in range(max_attempts):
#         try:
#             os.rename(src, dst)
#             return True
#         except PermissionError:
#             time.sleep(poll_interval)
#     return False

def rename_with_retry(src, dst, max_attempts=10, poll_interval=1):
    base, ext = os.path.splitext(dst)

    candidate = dst
    counter = 1

    while os.path.exists(candidate):
        candidate = f"{base}_{counter:02d}{ext}"
        counter += 1

    for attempt in range(max_attempts):
        try:
            os.rename(src, candidate)
            return candidate
        except PermissionError:
            time.sleep(poll_interval)

    return None

def wait_for_status_ok(excel, sheet, status_cell, macro_name, target_status="hold", poll_interval=3, max_attempts=1000):
    for attempt in range(max_attempts):
        status = run_excel_macro(excel, sheet, status_cell, "STATUS?", macro_name)
        if status == target_status:
            return True
        time.sleep(poll_interval)
    return False

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

        print(f"Laser status = {status}, retry {attempt + 1}/{retries}")
        time.sleep(interval)

    return False
def wait_for_scan_completion_with_tuning_check(excel, sheet, status_cell, macro_name, proxy, execution_type, target_status, poll_interval=2):
    while True:
        inputVal = {"executionType": execution_type}
        result = proxy.Protocol.getProtocolProgress(inputVal)
        if result['state'] != "SCANNING":
            return False
        status = run_excel_macro(excel, sheet, status_cell, "STATUS?", macro_name)
        if status != target_status:
            run_excel_macro(excel, sheet, 'B28', "System Shutter=1", macro_name)
            return True
        time.sleep(poll_interval)

def is_numeric(value):
    try:
        float(value)
        return True
    except (TypeError, ValueError):
        return False


#laser error handle
def acquire_and_rename(
        excel, sheet, proxy, macro_name, sample_name, target_status,
        OPO_power, IR_power, OPO_WAVELENGTH,
        Original_Filename, New_Filename, Rescan,
        requested_wavelength=None,
        max_rescans=3):
    OPO_power.append(run_excel_macro(excel, sheet, 'B40', "OPO POWER?", macro_name))
    IR_power.append(run_excel_macro(excel, sheet, 'B42', "LASER IR POWER?", macro_name))
    OPO_WAVELENGTH.append(run_excel_macro(excel, sheet, 'B41', "OPO WAVELENGTH?", macro_name))

    opo_valid = is_numeric(OPO_power[-1]) and is_numeric(OPO_WAVELENGTH[-1])

    inputVal = {"executionType": "EXECUTION_TYPE_MANUAL_MAIN"}

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

    run_excel_macro(excel, sheet, 'B28', "System Shutter=1", macro_name)
    try:
        result = proxy.Protocol.startProtocol(inputVal)
        targetName = result['targetName']
        fileName = targetName[0]
        fileName = fileName['name']
    except:
        print(result.keys())
        result = proxy.Protocol.startProtocol(inputVal)
        targetName = result['targetName']
        fileName = targetName[0]
        fileName = fileName['name']

    interrupted = wait_for_scan_completion_with_tuning_check(
        excel, sheet, 'B39', macro_name, proxy, "EXECUTION_TYPE_MANUAL_MAIN", target_status)

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

        run_excel_macro(excel, sheet, 'B28', "System Shutter=1", macro_name)
        try:
            result = proxy.Protocol.startProtocol(inputVal)
            targetName = result['targetName']
            fileName = targetName[0]
            fileName = fileName['name']
        except:
            print(result.keys())
            result = proxy.Protocol.startProtocol(inputVal)
            targetName = result['targetName']
            fileName = targetName[0]
            fileName = fileName['name']

        OPO_power.append(run_excel_macro(excel, sheet, 'B40', "OPO POWER?", macro_name))
        IR_power.append(run_excel_macro(excel, sheet, 'B42', "LASER IR POWER?", macro_name))
        OPO_WAVELENGTH.append(run_excel_macro(excel, sheet, 'B41', "OPO WAVELENGTH?", macro_name))

        opo_valid = is_numeric(OPO_power[-1]) and is_numeric(OPO_WAVELENGTH[-1])

        interrupted = wait_for_scan_completion_with_tuning_check(
            excel, sheet, 'B39', macro_name, proxy, "EXECUTION_TYPE_MANUAL_MAIN", target_status)
        rescan_count += 1

    Original_Filename.append(fileName)
    Rescan.append(rescan_count > 0)

    run_excel_macro(excel, sheet, 'B29', "System Shutter=0", macro_name)
    proxy.Protocol.stopProtocol(inputVal)

    if interrupted:
        suffix = "_failed"
    elif rescan_count > 0:
        suffix = "_rescan"
    else:
        suffix = ""

    if not opo_valid:
        suffix = suffix + "_invalid"

    if opo_valid:
        wavelength_tag = OPO_WAVELENGTH[-1] / 10
    else:
        wavelength_tag = OPO_WAVELENGTH[-1]

    new_filename = "{}_{}_{}_{}{}{}".format(
        sample_name,
        wavelength_tag,
        OPO_power[-1],
        IR_power[-1],
        suffix,
        os.path.splitext(fileName)[1]
    )

    # new_path = os.path.join(os.path.dirname(fileName), new_filename)
    # renamed = rename_with_retry(fileName, new_path)
    # if not renamed:
    #     print(f"Could not rename {fileName}, file still locked after retries")
    #
    # New_Filename.append(new_path)
    new_path = os.path.join(os.path.dirname(fileName), new_filename)

    renamed_path = rename_with_retry(fileName, new_path)

    if renamed_path is None:
        print(f"Could not rename {fileName}, file still locked after retries")
        New_Filename.append(fileName)
    else:
        New_Filename.append(renamed_path)
    return True

if __name__=="__main__":
    dialog = ParameterDialog()
    parameters = dialog.get_parameters()

    if parameters:
        mode = parameters['mode']
        sample_name = parameters['sample_name']

        if mode == 'sweep':
            opo_power = parameters['opo_power']
            ir_power = parameters['ir_power']
            start_wavelength = int(parameters['start_wavelength'] * 10)
            end_wavelength = int(parameters['end_wavelength'] * 10)
            step_size = parameters['step_size']
            print(f"Starting measurement from {start_wavelength} Angstrom to {end_wavelength} Angstrom")
            print(f"Step size: {step_size} nm")
            print(f"OPO power: {opo_power}")
            print(f"IR power: {ir_power}")
        else:
            print(f"Single mode with {len(parameters['scans'])} wavelength rows")
    else:
        print("Dialog was cancelled")
        sys.exit()

    file_path = r'C:\Users\yilai\Downloads\ape_client_S10531.xlsm'
    #picoEmerald = ape_device.ape_device("172.21.31.161", 51100)
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
        run_excel_macro(excel, sheet, 'B29', "System Shutter=0", macro_name)

        OPO_power = []
        IR_power = []
        OPO_WAVELENGTH = []
        Original_Filename = []
        New_Filename = []
        Rescan = []

        if mode == 'sweep':
            run_excel_macro(excel, sheet, 'B40', "OPO POWER={}".format(opo_power), macro_name)
            run_excel_macro(excel, sheet, 'B42', "LASER IR POWER={}".format(ir_power), macro_name)

            sample = int((end_wavelength - start_wavelength) / (step_size * 10)) + 1
            print("number:", sample)
            run_excel_macro(excel, sheet, 'B48',
                            "SWEEP={};{};{};300".format(start_wavelength, end_wavelength, sample - 1), macro_name)
            run_excel_macro(excel, sheet, 'B27', "SWEEP START", macro_name)
            print("Sweep in Tuning, waiting for status hold")
            wait_for_status_ok(excel, sheet, 'B39', macro_name)

            for i in tqdm.tqdm(range(sample)):
                wait_for_status_ok(excel, sheet, 'B39', macro_name)

                y = -1.6336 * (
                        start_wavelength / 10 + i * (((end_wavelength - start_wavelength) / sample) / 10)) + 9527.2
                run_excel_macro(excel, sheet, 'B70', "DELAY ABS={}".format(int(y)), macro_name)

# 260704 modified to handle laser error
#                 acquire_and_rename(excel, sheet, proxy, macro_name, sample_name, "hold",
#                                    OPO_power, IR_power, OPO_WAVELENGTH,
#                                    Original_Filename, New_Filename, Rescan)
                current_wavelength = start_wavelength + int(
                    i * (end_wavelength - start_wavelength) / sample
                )

                while True:

                    success = acquire_and_rename(
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
                wavelength_value = int(scan['wavelength'] * 10)
                opo_power_value = scan['opo_power']
                ir_power_value = scan['ir_power']

                run_excel_macro(excel, sheet, 'B41', "OPO WAVELENGTH={}".format(wavelength_value), macro_name)
                run_excel_macro(excel, sheet, 'B40', "OPO POWER={}".format(opo_power_value), macro_name)
                run_excel_macro(excel, sheet, 'B42', "LASER IR POWER={}".format(ir_power_value), macro_name)
                y = -1.6336 * (wavelength_value / 10) + 9527.2
                run_excel_macro(excel, sheet, 'B70', "DELAY ABS={}".format(int(y)), macro_name)
                wait_for_status_ok(excel, sheet, 'B39', macro_name, target_status="OK")

# 260704 modified to handle laser error
#                 acquire_and_rename(excel, sheet, proxy, macro_name, sample_name, "OK",
#                                    OPO_power, IR_power, OPO_WAVELENGTH,
#                                    Original_Filename, New_Filename, Rescan)
        while True:

            success = acquire_and_rename(
                excel, sheet, proxy, macro_name, sample_name, "OK",
                OPO_power, IR_power, OPO_WAVELENGTH,
                Original_Filename, New_Filename, Rescan,
                requested_wavelength=wavelength_value
            )

            if success:
                break

            print(f"Retrying {wavelength_value / 10:.1f} nm...")

            run_excel_macro(
                excel,
                sheet,
                'B41',
                f"OPO WAVELENGTH={wavelength_value}",
                macro_name
            )

            y = -1.6336 * (wavelength_value / 10) + 9527.2
            run_excel_macro(
                excel,
                sheet,
                'B70',
                f"DELAY ABS={int(y)}",
                macro_name
            )

        excel.close()
        comment=''#change every time

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
        with pd.ExcelWriter(filename) as writer:
            updated_df.to_excel(writer, index=False, sheet_name='Sheet1')