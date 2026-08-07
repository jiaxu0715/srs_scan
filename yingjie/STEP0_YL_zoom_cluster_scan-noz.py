import xlwings as xw
import xmlrpc.client
import sys
import time
import numpy as np
from scipy import ndimage
from zoom_cluster_dialog_noz import ParameterDialog
from concurrent.futures import ProcessPoolExecutor

def run_excel_macro(excel, sheet, query_cell, query, macro_name):
    sheet.range(query_cell).value = query
    sheet.range(query_cell).select()
    excel.macro(macro_name)()
    excel.save()
    response = sheet.range('B10').value
    return response


def wait_for_status_ok(excel, sheet, status_cell, macro_name, poll_interval=3, max_attempts=1000):
    for attempt in range(max_attempts):
        status = run_excel_macro(excel, sheet, status_cell, "STATUS?", macro_name)
        if status == "OK":
            return True
        time.sleep(poll_interval)
    return False


def wait_for_matl_completion(proxy, poll_interval=2):
    while True:
        inputVal = {"executionType": "EXECUTION_TYPE_MATL"}
        result = proxy.Protocol.getProtocolProgress(inputVal)
        if result['state'] != "SCANNING":
            break
        time.sleep(poll_interval)


def wait_for_manual_completion(proxy, poll_interval=1):
    while True:
        inputVal = {"executionType": "EXECUTION_TYPE_MANUAL_MAIN"}
        result = proxy.Protocol.getProtocolProgress(inputVal)
        if result['state'] != "SCANNING":
            break
        time.sleep(poll_interval)


def read_tile_frame(proxy, group_id_ida, area_index):
    inputVal = {"groupId": group_id_ida, "level": 0, "area": area_index}
    result = proxy.IDA.getArea(inputVal)
    if 'areaId' not in result:
        print(f"getArea failed for area_index={area_index}, full result: {result}")
        raise RuntimeError(f"getArea failed for area_index={area_index}, result code: {result.get('result')}")
    area_id = result['areaId']

    inputVal = {"areaId": area_id, "key": "EnabledChannelIdList"}
    result = proxy.IDA.getProperty(inputVal)
    if 'value' not in result:
        print(f"getProperty (EnabledChannelIdList) failed for area_id={area_id}, full result: {result}")
        raise RuntimeError(f"getProperty failed, result code: {result.get('result')}")
    channel_id = result['value'][0]

    inputVal = {"areaId": area_id, "channelId": channel_id,
                "axisInfo": [{"axisName": "TIMELAPSE", "index": 0}]}
    result = proxy.IDA.getImage(inputVal)
    if 'imageId' not in result:
        print(f"getImage failed for area_id={area_id}, channel_id={channel_id}, full result: {result}")
        raise RuntimeError(f"getImage failed, result code: {result.get('result')}")
    image_id = result['imageId']

    inputVal = {"imageId": image_id}
    result = proxy.IDA.getImageSize(inputVal)
    if 'width' not in result:
        print(f"getImageSize failed for image_id={image_id}, full result: {result}")
        raise RuntimeError(f"getImageSize failed, result code: {result.get('result')}")
    width = result['width']
    height = result['height']

    inputVal = {"imageId": image_id, "compressType": "NONE",
                "rect": {"x": 0, "y": 0, "width": width, "height": height}}
    result = proxy.IDA.getImageBody(inputVal)
    if 'data' not in result:
        print(f"getImageBody failed for image_id={image_id}, full result: {result}")
        raise RuntimeError(f"getImageBody failed, result code: {result.get('result')}")
    image_data = result['data'].data
    while result['continue']:
        inputVal = {"imageId": image_id}
        result = proxy.IDA.getNextImageBody(inputVal)
        if 'data' not in result:
            print(f"getNextImageBody failed for image_id={image_id}, full result: {result}")
            raise RuntimeError(f"getNextImageBody failed, result code: {result.get('result')}")
        image_data = image_data + result['data'].data

    frame = np.frombuffer(image_data, dtype=np.uint16)
    frame = frame.reshape(height, width)

    proxy.IDA.releaseImage({"imageId": image_id})
    proxy.IDA.releaseArea({"areaId": area_id})
    return frame, width, height


#optimized for human blood smear immune cells
def find_candidate_clusters(frame, pixel_size_nm):
    threshold_factor = 1.4
    bbox_size_um = 10.0
    min_bright_fraction = 0.70
    min_seed_area_um2 = 7.0
    debris_bbox_size_um = 22.0
    debris_bright_fraction_max = 0.8

    frame_float = frame.astype(np.float64)
    image_mean = np.mean(frame_float)
    threshold = threshold_factor * image_mean

    bright_mask = frame_float > threshold
    labeled_array, num_features = ndimage.label(bright_mask)

    pixel_size_um = pixel_size_nm / 1000.0
    min_seed_pixels = int(round(min_seed_area_um2 / (pixel_size_um * pixel_size_um)))
    duplicate_distance_px = int(round(bbox_size_um / pixel_size_um))

    candidates = []
    accepted_centers = []

    for label_id in range(1, num_features + 1):
        ys, xs = np.where(labeled_array == label_id)

        if len(ys) < min_seed_pixels:
            continue

        weights = frame_float[ys, xs] - threshold
        weights[weights < 0] = 0

        if np.sum(weights) == 0:
            continue

        center_row = int(round(np.sum(ys * weights) / np.sum(weights)))
        center_col = int(round(np.sum(xs * weights) / np.sum(weights)))

#avoid duplication
        duplicate = False
        for accepted_row, accepted_col in accepted_centers:
            distance_px = np.sqrt((center_row - accepted_row) ** 2 + (center_col - accepted_col) ** 2)
            if distance_px < duplicate_distance_px:
                duplicate = True
                break

        if duplicate:
            continue
#fixed-size bounding box
        box_size_px = int(round(bbox_size_um / pixel_size_um))
        half_size_px = box_size_px // 2

        y_min = center_row - half_size_px
        y_max = y_min + box_size_px
        x_min = center_col - half_size_px
        x_max = x_min + box_size_px

        if y_min < 0 or x_min < 0 or y_max > frame.shape[0] or x_max > frame.shape[1]:
            continue
# filter out thin non-immune cell speckles and noise
        bbox_pixels = frame_float[y_min:y_max, x_min:x_max]
        bright_fraction = np.mean(bbox_pixels > threshold)

        if bright_fraction <= min_bright_fraction:
            continue
# large debris filter
        debris_box_size_px = int(round(debris_bbox_size_um / pixel_size_um))
        debris_half_size_px = debris_box_size_px // 2

        debris_y_min = max(0, center_row - debris_half_size_px)
        debris_y_max = min(frame.shape[0], debris_y_min + debris_box_size_px)
        debris_x_min = max(0, center_col - debris_half_size_px)
        debris_x_max = min(frame.shape[1], debris_x_min + debris_box_size_px)

        debris_pixels = frame_float[debris_y_min:debris_y_max, debris_x_min:debris_x_max]
        if debris_pixels.size > 0:
            debris_bright_fraction = np.mean(debris_pixels > threshold)
            if debris_bright_fraction > debris_bright_fraction_max:
                continue

        candidate = {
            'centroid_row_px': (y_min + y_max - 1) / 2,
            'centroid_col_px': (x_min + x_max - 1) / 2,
            'bbox': {
                'x_min': int(x_min),
                'x_max': int(x_max - 1),
                'y_min': int(y_min),
                'y_max': int(y_max - 1)
            },
            'bbox_size_um': bbox_size_um,
            'bright_fraction': float(bright_fraction)
        }
        candidates.append(candidate)
        accepted_centers.append((candidate['centroid_row_px'], candidate['centroid_col_px']))

    candidates.sort(key=lambda c: c['bright_fraction'], reverse=True)
    return candidates


def force_stage_z(proxy, z_nm):
    inputVal = {"settingId": "LSM_Z_COORDINATE_ENABLE_SETTING", "enable": False}
    proxy.Parameter.setParameter(inputVal)

    inputVal = {"settingId": "Z_STAGE_POSITION_SETTING", "stagePosition": int(z_nm)}
    result = proxy.Parameter.setParameter(inputVal)

    print(f"Forced live Z to {z_nm / 1000.0:.2f} um, result={result}")


def register_cluster_roi(proxy, x_nm, y_nm, z_nm):
    force_stage_z(proxy, z_nm)

    inputVal = {"settingId": "LSM_Z_COORDINATE_ENABLE_SETTING", "enable": False}
    proxy.Parameter.setParameter(inputVal)

    inputVal = {"settingId": "Z_STAGE_POSITION_SETTING", "stagePosition": int(z_nm)}
    proxy.Parameter.setParameter(inputVal)

    inputVal = {"settingId": "MATL_ROI_CREATE", "acquisitionType": "LSM_IMAGING",
                "roiType": "DEFINE_MATRIX", "column": 1, "row": 1}
    result = proxy.Parameter.setParameter(inputVal)
    cluster_group_id = result['groupId']

    inputVal = {"settingId": "MATL_ROI_XY_POSITION_SETTING", "groupId": cluster_group_id,
                "x": int(x_nm), "y": int(y_nm)}
    proxy.Parameter.setParameter(inputVal)

    set_roi_z(proxy, cluster_group_id, z_nm)

    return cluster_group_id


def set_roi_z(proxy, group_id, z_nm):
    inputVal = {"settingId": "MATL_ROI_Z_POSITION_SETTING", "groupId": group_id, "z": int(z_nm)}
    result = proxy.Parameter.setParameter(inputVal)
    print(f"Set ROI group {group_id} Z to {z_nm / 1000.0:.2f} um, result={result}")


def nearest_neighbor_order_from_first_tile(clusters):
    if not clusters:
        return []

    remaining = clusters.copy()

    first_index = min(
        range(len(remaining)),
        key=lambda i: (remaining[i]['tile_row'], remaining[i]['tile_col'], remaining[i]['candidate_order'])
    )
    first_cluster = remaining.pop(first_index)

    ordered = [first_cluster]
    current_x_um = first_cluster['x_um']
    current_y_um = first_cluster['y_um']

    while remaining:
        distances = [
            (cluster['x_um'] - current_x_um) ** 2 + (cluster['y_um'] - current_y_um) ** 2
            for cluster in remaining
        ]
        nearest_index = int(np.argmin(distances))
        nearest_cluster = remaining.pop(nearest_index)
        ordered.append(nearest_cluster)
        current_x_um = nearest_cluster['x_um']
        current_y_um = nearest_cluster['y_um']

    return ordered


def compute_bottom_right_stage_position(cell_x_nm, cell_y_nm, scan_size_px,
                                        bbox_size_um, pixel_size_nm,
                                        row_direction, col_direction, margin_fraction):
    pixel_size_um = pixel_size_nm / 1000.0
    half_bbox_px = (bbox_size_um / pixel_size_um) / 2.0
    target_center_col_px = scan_size_px * (1.0 - margin_fraction) - half_bbox_px
    target_center_row_px = scan_size_px * (1.0 - margin_fraction) - half_bbox_px

    stage_x_nm = cell_x_nm - col_direction * (target_center_col_px - scan_size_px / 2.0) * pixel_size_nm
    stage_y_nm = cell_y_nm - row_direction * (target_center_row_px - scan_size_px / 2.0) * pixel_size_nm
    return stage_x_nm, stage_y_nm


def map_area_index_to_row_col(area_index, grid_size):
    row = area_index // grid_size
    col_in_row = area_index % grid_size

    if row % 2 == 0:
        col = col_in_row
    else:
        col = grid_size - 1 - col_in_row

    return row, col

if __name__ == "__main__":
    dialog = ParameterDialog()
    parameters = dialog.get_parameters()

    if not parameters:
        print("Dialog was cancelled")
        sys.exit()

    file_path = r'C:\Users\yilai\Downloads\ape_client_S10531.xlsm'
    url = "http://127.0.0.1:8080/xmlrpc"
    proxy = xmlrpc.client.ServerProxy(url)

    try:
        app = xw.App(visible=False)
        excel = app.books.open(file_path)
        sheet = excel.sheets[0]
    except Exception as e:
        print(f"Failed to open the workbook: {e}")
        sys.exit()

    macro_name = 'ape_execute'
    run_excel_macro(excel, sheet, 'B8', '', macro_name)

    wavelength_value = int(parameters['opo_wavelength'] * 10)
    run_excel_macro(excel, sheet, 'B41', "OPO WAVELENGTH={}".format(wavelength_value), macro_name)
    run_excel_macro(excel, sheet, 'B40', "OPO POWER={}".format(parameters['opo_power']), macro_name)
    run_excel_macro(excel, sheet, 'B42', "LASER IR POWER={}".format(parameters['ir_power']), macro_name)

    status_ok = wait_for_status_ok(excel, sheet, 'B39', macro_name)
    if not status_ok:
        print("Status did not reach OK, stopping")
        excel.close()
        sys.exit()

    # run_excel_macro(excel, sheet, 'B28', "System Shutter=1", macro_name)

    inputVal = {"settingId": "LSM_Z_COORDINATE_ENABLE_SETTING", "enable": parameters['enable_z_stack']}
    proxy.Parameter.setParameter(inputVal)

    inputVal = {"targetName": parameters['zoom1_reference_path']}
    proxy.Parameter.loadParameter(inputVal)

    GRID_SIZE = 5
    ZOOM1_SCAN_SIZE_PX = 640
    ZOOM12_SCAN_SIZE_PX = 256
    MARGIN_FRACTION = 0.15
    Z_ORIGIN_UM = 200

    inputVal = {"settingId": "MATL_ROI_GET_REGISTERD_LIST"}
    result = proxy.Parameter.getParameter(inputVal)
    registered_list = result['groupList']
    survey_group_id = registered_list[0]['groupId']

    map_center_x_nm = parameters['map_center_x_um'] * 1000.0
    map_center_y_nm = parameters['map_center_y_um'] * 1000.0

    ZOOM1_FIELD_X_UM = 509.117
    ZOOM1_FIELD_Y_UM = 509.117

    # Change this if MATL overlap is different.
    # 0.05 means 5 percent overlap, so tile pitch is 95 percent of full field size.
    MAP_STITCH_OVERLAP_FRACTION = 0.05

    tile_pitch_x_nm = ZOOM1_FIELD_X_UM * (1.0 - MAP_STITCH_OVERLAP_FRACTION) * 1000.0
    tile_pitch_y_nm = ZOOM1_FIELD_Y_UM * (1.0 - MAP_STITCH_OVERLAP_FRACTION) * 1000.0

    print(
        f"Using manual map center: x={parameters['map_center_x_um']:.1f} um, y={parameters['map_center_y_um']:.1f} um")
    print(f"Using map tile pitch: x={tile_pitch_x_nm / 1000.0:.3f} um, y={tile_pitch_y_nm / 1000.0:.3f} um")

#image starts then shutter on
    inputVal = {"executionType": "EXECUTION_TYPE_MATL"}
    result = proxy.Protocol.startProtocol(inputVal)
    run_excel_macro(excel, sheet, 'B28', "System Shutter=1", macro_name)
    targetName = result['targetName']
    omp2info_path = targetName[0]['name']

    wait_for_matl_completion(proxy)
    run_excel_macro(excel, sheet, 'B29', "System Shutter=0", macro_name)

    inputVal = {"filename": omp2info_path}
    result = proxy.IDA.open(inputVal)
    file_id = result['fileId']

    inputVal = {"fileId": file_id, "group": 0}
    result = proxy.IDA.getGroup(inputVal)
    group_id_ida = result['groupId']

    inputVal = {"groupId": group_id_ida, "level": 0}
    result = proxy.IDA.getNumOfArea(inputVal)
    actual_num_areas = result['numOfArea']
    print(f"Expected {GRID_SIZE * GRID_SIZE} areas based on GRID_SIZE constant, "
          f"actual area count in acquired file: {actual_num_areas}")

    if actual_num_areas != GRID_SIZE * GRID_SIZE:
        print("Area count mismatch. GRID_SIZE constant does not match the grid you actually built. "
              "Update GRID_SIZE to match your manually constructed grid, or check whether any "
              "areas in that grid are disabled, then rerun.")
        proxy.IDA.releaseGroup({"groupId": group_id_ida})
        proxy.IDA.close({"fileId": file_id})
        run_excel_macro(excel, sheet, 'B29', "System Shutter=0", macro_name)
        excel.close()
        sys.exit()

    ROW_DIRECTION = 1
    COL_DIRECTION = 1

    # Sequential imaging pass. All proxy reads stay single-threaded to keep one XMLRPC
    # client safe. At grid sizes below 6x6 with zoom1 frames, holding every frame in
    # memory before the parallel search is fine. Revisit if GRID_SIZE grows much larger,
    # since memory scales with GRID_SIZE * GRID_SIZE.
    tile_frames = []
    for area_index in range(GRID_SIZE * GRID_SIZE):
        row, col = map_area_index_to_row_col(area_index, GRID_SIZE)
        print(f"Reading area_index={area_index} as tile=({row},{col})")

        frame, width, height = read_tile_frame(proxy, group_id_ida, area_index)
        tile_frames.append({
            'area_index': area_index,
            'row': row,
            'col': col,
            'frame': frame,
            'width': width,
            'height': height
        })

    # Parallel candidate search across all tiles. Only the Python search runs in parallel,
    # executor.map preserves input order, so the assembly
    # below still produces a grid-1-first candidate sequence for the downstream ROI loop.
    #currently use 9CPU
    frames_only = [t['frame'] for t in tile_frames]
    pixel_size_list = [parameters['zoom1_pixel_size_nm']] * len(frames_only)
    with ProcessPoolExecutor(max_workers=16) as executor:
        candidates_per_tile = list(executor.map(find_candidate_clusters, frames_only, pixel_size_list))

    # Sequential assembly of global coordinates, from grid 1 to set roi1
    all_candidates = []
    for tile, candidates in zip(tile_frames, candidates_per_tile):
        row = tile['row']
        col = tile['col']
        width = tile['width']
        height = tile['height']

        tile_center_x_nm = map_center_x_nm + COL_DIRECTION * (col - (GRID_SIZE - 1) / 2) * tile_pitch_x_nm
        tile_center_y_nm = map_center_y_nm + ROW_DIRECTION * (row - (GRID_SIZE - 1) / 2) * tile_pitch_y_nm

        for c in candidates:
            global_x_nm = tile_center_x_nm + COL_DIRECTION * (c['centroid_col_px'] - width / 2) * parameters[
                'zoom1_pixel_size_nm']
            global_y_nm = tile_center_y_nm + ROW_DIRECTION * (c['centroid_row_px'] - height / 2) * parameters[
                'zoom1_pixel_size_nm']

            print(f"tile=({row},{col}), pixel=({c['centroid_col_px']:.1f}, {c['centroid_row_px']:.1f}), "
                  f"global=({global_x_nm / 1000.0:.1f}, {global_y_nm / 1000.0:.1f}) um, "
                  f"bbox={c['bbox_size_um']:.1f} um, bright_fraction={c['bright_fraction']:.2f}")

            all_candidates.append({
                'candidate_order': len(all_candidates),
                'tile_row': row,
                'tile_col': col,
                'global_x_um': float(global_x_nm / 1000.0),
                'global_y_um': float(global_y_nm / 1000.0),
                'bbox_size_um': float(c['bbox_size_um']),
                'bright_fraction': float(c['bright_fraction'])
            })

    proxy.IDA.releaseGroup({"groupId": group_id_ida})
    proxy.IDA.close({"fileId": file_id})

    print(f"Found {len(all_candidates)} candidates from zoom1 survey:")
    for c in all_candidates:
        print(c)

    inputVal = {"settingId": "MATL_ROI_ENABLE_SETTING", "groupId": survey_group_id, "enable": False}
    proxy.Parameter.setParameter(inputVal)

    ZOOM12_BBOX_SIZE_UM = 10.0
    roi_z_nm = int(parameters['middle_z_um'] * 1000.0)

    placed_clusters = []
    for candidate in all_candidates:
        cell_x_nm = candidate['global_x_um'] * 1000.0
        cell_y_nm = candidate['global_y_um'] * 1000.0

        roi_x_nm, roi_y_nm = compute_bottom_right_stage_position(
            cell_x_nm, cell_y_nm, ZOOM12_SCAN_SIZE_PX,
            ZOOM12_BBOX_SIZE_UM, parameters['zoom12_pixel_size_nm'],
            ROW_DIRECTION, COL_DIRECTION, MARGIN_FRACTION)

        placed_clusters.append({
            'candidate_order': candidate['candidate_order'],
            'tile_row': candidate['tile_row'],
            'tile_col': candidate['tile_col'],
            'x_um': float(roi_x_nm / 1000.0),
            'y_um': float(roi_y_nm / 1000.0),
            'best_z_nm': roi_z_nm,
            'bright_fraction': candidate['bright_fraction']
        })

    ordered_clusters = nearest_neighbor_order_from_first_tile(placed_clusters)

    for order_index, cluster in enumerate(ordered_clusters, start=1):
        cluster['order'] = order_index
        print(f"Placed cluster order {order_index}, tile=({cluster['tile_row']},{cluster['tile_col']}), "
              f"x={cluster['x_um']:.1f} um, y={cluster['y_um']:.1f} um, "
              f"z={cluster['best_z_nm'] / 1000.0:.2f} um")

    inputVal = {"targetName": parameters['zoom12_reference_path']}
    proxy.Parameter.loadParameter(inputVal)
    print("Loaded zoom12 reference before ROI registration")

    registered_clusters = []
    for cluster in ordered_clusters:
        roi_x_nm = cluster['x_um'] * 1000.0
        roi_y_nm = cluster['y_um'] * 1000.0
        cluster_group_id = register_cluster_roi(proxy, roi_x_nm, roi_y_nm, cluster['best_z_nm'])

        registered_cluster = {
            'group_id': cluster_group_id,
            'order': cluster['order'],
            'x_um': cluster['x_um'],
            'y_um': cluster['y_um'],
            'best_z_um': float(cluster['best_z_nm'] / 1000.0),
            'bright_fraction': cluster['bright_fraction']
        }
        registered_clusters.append(registered_cluster)

        print(f"Registered ROI order {cluster['order']}, group {cluster_group_id}, "
              f"tile=({cluster['tile_row']},{cluster['tile_col']}), "
              f"x={cluster['x_um']:.1f} um, y={cluster['y_um']:.1f} um, "
              f"z={cluster['best_z_nm'] / 1000.0:.2f} um")

    print(f"Total placed clusters: {len(placed_clusters)}")
    print(f"Total registered clusters: {len(registered_clusters)}")

    run_excel_macro(excel, sheet, 'B29', "System Shutter=0", macro_name)
    excel.close()
