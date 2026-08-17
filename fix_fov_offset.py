#!/usr/bin/env python3
"""Fix MATL FOV index offsets caused by Map/Stitch .oir files.

Usage: ``python fix_fov_offset.py /path/to/directory``

Among ``.oir`` files that are not already Map/Stitch, find the most common
file size. Larger outliers become ``Stitch_A01.oir`` (then A02, …); smaller
outliers become ``Map_A01.oir``. Remaining FOV files are reindexed in place
so ``_roiN_`` runs 1, 2, 3, … with Map/Stitch excluded. Prints nothing on
success.
"""

from __future__ import annotations

import argparse
import re
import sys
from collections import Counter
from pathlib import Path

_ROI_RE = re.compile(r"_roi(\d+)_", re.IGNORECASE)


def _is_overview(name: str) -> bool:
    lower = name.lower()
    return "map" in lower or "stitch" in lower


def _oir_files(directory: Path) -> list[Path]:
    return sorted(
        p for p in directory.iterdir() if p.is_file() and p.suffix.lower() == ".oir"
    )


def _free_name(directory: Path, stem: str) -> Path:
    """Return ``stem_A01.oir``, then A02, …, skipping names that already exist."""
    i = 1
    while True:
        dest = directory / f"{stem}_A{i:02d}.oir"
        if not dest.exists():
            return dest
        i += 1


def _rename(src: Path, dest: Path) -> None:
    dest.parent.mkdir(parents=True, exist_ok=True)
    src.rename(dest)


def _reindex_fovs(fovs: list[Path]) -> None:
    """Rewrite ``_roiN_`` to a dense 1-based sequence, using temps to avoid clashes."""
    if not fovs:
        return

    def sort_key(path: Path) -> tuple[int, str]:
        match = _ROI_RE.search(path.name)
        idx = int(match.group(1)) if match else 10**9
        return (idx, path.name)

    ordered = sorted(fovs, key=sort_key)
    staged: list[tuple[Path, int, str]] = []
    for new_i, path in enumerate(ordered, start=1):
        tmp = path.with_name(f".__fov_reindex_{new_i}__{path.name}")
        _rename(path, tmp)
        staged.append((tmp, new_i, path.name))

    for tmp, new_i, orig_name in staged:
        if _ROI_RE.search(orig_name):
            new_name = _ROI_RE.sub(f"_roi{new_i}_", orig_name, count=1)
        else:
            new_name = orig_name
        _rename(tmp, tmp.with_name(new_name))


def fix_directory(directory: Path) -> None:
    candidates = [p for p in _oir_files(directory) if not _is_overview(p.name)]
    if len(candidates) < 2:
        return

    counts = Counter(p.stat().st_size for p in candidates)
    common_size = counts.most_common(1)[0][0]

    maps: list[Path] = []
    stitches: list[Path] = []
    fovs: list[Path] = []
    for path in candidates:
        size = path.stat().st_size
        if size == common_size:
            fovs.append(path)
        elif size > common_size:
            stitches.append(path)
        else:
            maps.append(path)

    for path in sorted(maps, key=lambda p: p.name):
        _rename(path, _free_name(directory, "Map"))
    for path in sorted(stitches, key=lambda p: p.name):
        _rename(path, _free_name(directory, "Stitch"))

    _reindex_fovs(fovs)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(add_help=True)
    parser.add_argument("directory", type=Path, help="folder of .oir files to fix in place")
    args = parser.parse_args(argv)
    directory = args.directory.expanduser().resolve()
    if not directory.is_dir():
        print(f"Not a directory: {directory}", file=sys.stderr)
        return 1
    fix_directory(directory)
    return 0


if __name__ == "__main__":
    sys.exit(main())
