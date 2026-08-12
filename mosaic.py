"""Mosaic geometry and pure-Python tile stitching (MATL-free).

``stage_x_um`` / ``stage_y_um`` are the absolute stage coordinates of the
**current FOV center** (also the mosaic center), typically read from Fluoview
at runtime rather than from config. Tile pitch uses FOV size and overlap
(default 5%, matching lab MATL maps).

FOV edge length is ``ZOOM1_FIELD_UM / zoom`` (509.117 µm at zoom 1×); scan
pixel count does not change the physical field.

Visit order is snake (row-major, alternate rows reverse), matching Yingjie/MATL
area indexing.
"""

from __future__ import annotations

import json
import os
import re
from pathlib import Path
from typing import Iterable

# Physical FOV at Fluoview optical zoom 1× (lab calibration; independent of scan pixels).
ZOOM1_FIELD_UM = 509.117


def field_um_from_zoom(zoom: float) -> float:
    """Square FOV edge length in µm for a Fluoview optical zoom factor."""
    z = float(zoom)
    if z <= 0:
        raise ValueError(f"zoom must be > 0; got {zoom}")
    return ZOOM1_FIELD_UM / z


def tile_pitch_um(field_um: float, overlap: float) -> float:
    """Center-to-center spacing for one axis."""
    if not 0.0 <= float(overlap) < 1.0:
        raise ValueError(f"overlap must be in [0, 1); got {overlap}")
    return float(field_um) * (1.0 - float(overlap))


def build_mosaic_tiles(
    stage_x_um: float,
    stage_y_um: float,
    columns: int,
    rows: int,
    field_x_um: float,
    field_y_um: float,
    overlap: float = 0.05,
    *,
    x_sign: int = 1,
    y_sign: int = 1,
) -> list[dict]:
    """Return snake-ordered tiles centered on the current FOV.

    Each tile dict: ``row``, ``col``, ``x_um``, ``y_um``, ``x_nm``, ``y_nm``.
    ``row``/``col`` are geometric indices (0-based); list order is visit order.
    """
    columns = int(columns)
    rows = int(rows)
    if columns < 1 or rows < 1:
        raise ValueError("columns and rows must be >= 1")

    pitch_x = tile_pitch_um(field_x_um, overlap)
    pitch_y = tile_pitch_um(field_y_um, overlap)
    # Current FOV center == mosaic center.
    c0 = (columns - 1) / 2.0
    r0 = (rows - 1) / 2.0
    x_sign = 1 if int(x_sign) >= 0 else -1
    y_sign = 1 if int(y_sign) >= 0 else -1

    tiles: list[dict] = []
    for row in range(rows):
        cols = range(columns) if row % 2 == 0 else range(columns - 1, -1, -1)
        for col in cols:
            x_um = float(stage_x_um) + x_sign * (col - c0) * pitch_x
            y_um = float(stage_y_um) + y_sign * (row - r0) * pitch_y
            tiles.append(
                {
                    "row": row,
                    "col": col,
                    "x_um": x_um,
                    "y_um": y_um,
                    "x_nm": int(round(x_um * 1000.0)),
                    "y_nm": int(round(y_um * 1000.0)),
                }
            )
    return tiles


def tile_sample_name(sample: str, row: int, col: int) -> str:
    """Filename stem prefix so tiles sort as ``{sample}_r{R}_c{C}_…``."""
    return f"{sample}_r{int(row)}_c{int(col)}"


_TILE_RE = re.compile(r"_r(\d+)_c(\d+)_")


def parse_tile_rc(path: str | Path) -> tuple[int, int] | None:
    """Extract (row, col) from a mosaic tile filename, if present."""
    m = _TILE_RE.search(Path(path).name)
    if not m:
        return None
    return int(m.group(1)), int(m.group(2))


def _load_image(path: str, z_index: int = 0):
    """Load a 2D plane from .oir/.tif/.png/.npy for stitching."""
    p = Path(path)
    suf = p.suffix.lower()
    if suf == ".npy":
        import numpy as np

        arr = np.load(p)
        return arr[z_index] if arr.ndim >= 3 else arr
    if suf in {".tif", ".tiff", ".png"}:
        import numpy as np

        try:
            import tifffile

            arr = tifffile.imread(p)
        except Exception:
            from PIL import Image

            arr = np.asarray(Image.open(p))
        return arr[z_index] if getattr(arr, "ndim", 0) >= 3 else arr
    if suf == ".oir":
        try:
            from aicsimageio import AICSImage
            from aicsimageio.readers import BioformatsReader
        except ImportError as exc:
            raise ImportError(
                "Reading .oir for stitch needs aicsimageio + bioformats_jar "
                "(and Java). Install those or convert tiles to .tif first."
            ) from exc
        print(f"  loading .oir via bioformats: {p.name}", flush=True)
        img = AICSImage(str(p), reader=BioformatsReader)
        data = img.get_image_data("ZYX", T=0, C=0)
        if data.ndim == 3:
            z = min(max(int(z_index), 0), data.shape[0] - 1)
            return data[z]
        return data
    raise ValueError(f"Unsupported image type for stitch: {path}")


def stitch_tiles(
    tile_paths: dict[tuple[int, int], str],
    *,
    overlap_fraction: float = 0.05,
    z_index: int = 0,
    out_path: str | None = None,
):
    """Stitch a {(row,col): path} map into one 2D array; optionally save .tif/.npy.

    Overlap is applied as a fraction of tile width/height (same as acquisition
    pitch). Overlapping pixels are averaged where both tiles contribute.
    """
    import numpy as np

    if not tile_paths:
        raise ValueError("no tiles to stitch")

    rows = sorted({r for r, _ in tile_paths})
    cols = sorted({c for _, c in tile_paths})
    n_rows = max(rows) + 1
    n_cols = max(cols) + 1

    loaded: dict[tuple[int, int], object] = {}
    for rc, path in sorted(tile_paths.items()):
        loaded[rc] = np.asarray(_load_image(path, z_index=z_index))

    first = next(iter(loaded.values()))
    img_h, img_w = int(first.shape[0]), int(first.shape[1])
    overlap_x = int(round(img_w * float(overlap_fraction)))
    overlap_y = int(round(img_h * float(overlap_fraction)))
    eff_w = max(img_w - overlap_x, 1)
    eff_h = max(img_h - overlap_y, 1)
    out_h = n_rows * eff_h + overlap_y
    out_w = n_cols * eff_w + overlap_x
    stitched = np.zeros((out_h, out_w), dtype=np.result_type(first.dtype, np.float64))
    weight = np.zeros((out_h, out_w), dtype=np.float64)

    for (row, col), img in loaded.items():
        img = np.asarray(img, dtype=np.float64)
        if img.shape[0] != img_h or img.shape[1] != img_w:
            raise ValueError(f"tile size mismatch at r{row}_c{col}: {img.shape}")
        r0 = row * eff_h
        c0 = col * eff_w
        stitched[r0 : r0 + img_h, c0 : c0 + img_w] += img
        weight[r0 : r0 + img_h, c0 : c0 + img_w] += 1.0

    mask = weight > 0
    stitched[mask] /= weight[mask]
    if np.issubdtype(first.dtype, np.integer):
        stitched = np.clip(stitched, 0, np.iinfo(first.dtype).max).astype(first.dtype)
    else:
        stitched = stitched.astype(first.dtype)

    if out_path:
        out = Path(out_path)
        out.parent.mkdir(parents=True, exist_ok=True)
        if out.suffix.lower() == ".npy":
            np.save(out, stitched)
        else:
            try:
                import tifffile

                tifffile.imwrite(out, stitched)
            except ImportError:
                npy = out.with_suffix(".npy")
                np.save(npy, stitched)
                print(f"tifffile missing; wrote {npy}")
                return stitched, str(npy)
        print(f"Wrote stitched mosaic → {out}", flush=True)
        return stitched, str(out)
    return stitched, None


def collect_tiles_from_paths(paths: Iterable[str]) -> dict[tuple[int, int], str]:
    """Build {(row,col): path} from mosaic-named files (last path wins)."""
    out: dict[tuple[int, int], str] = {}
    for path in paths:
        rc = parse_tile_rc(path)
        if rc is not None and path:
            out[rc] = path
    return out


def collect_tiles_from_dir(directory: str) -> dict[tuple[int, int], str]:
    """Scan *directory* for mosaic-named ``.oir``/``.tif`` tiles."""
    if not directory or not os.path.isdir(directory):
        return {}
    paths = []
    for name in os.listdir(directory):
        low = name.lower()
        if low.endswith((".oir", ".tif", ".tiff", ".npy", ".png")):
            paths.append(os.path.join(directory, name))
    return collect_tiles_from_paths(paths)


def oir_stitch_deps_ok() -> tuple[bool, str]:
    """Return (ok, detail) for optional .oir stitch dependencies."""
    try:
        import aicsimageio  # noqa: F401
        from aicsimageio.readers import BioformatsReader  # noqa: F401
    except ImportError as exc:
        return False, f"missing aicsimageio/bioformats ({exc})"
    return True, "aicsimageio + BioformatsReader importable"


def write_tile_manifest(
    path: str,
    tiles: list[dict],
    acquired: dict[tuple[int, int], str],
    meta: dict,
) -> str:
    """Write JSON manifest for offline stitch if .oir readers are unavailable."""
    payload = {
        "meta": meta,
        "tiles": [
            {
                **t,
                "path": acquired.get((t["row"], t["col"])),
            }
            for t in tiles
        ],
    }
    os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        json.dump(payload, f, indent=2)
    print(f"Wrote tile manifest → {path}")
    return path
