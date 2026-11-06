"""Make Lighter Copy: cull faint smoke and halve the resolution of a bake.

Writes a derived cache slot next to the source one (FUTURE "Bake
post-processing", default chosen 2026-11-05): every frame's grids are
averaged 2x2x2 into voxels twice as large, and voxels whose smoke falls below
the cull threshold are dropped. Grids already coarser than the density grid
(the baked glow) are copied unchanged.
"""
import os
import shutil

import numpy as np

try:
    import openvdb
except ImportError:  # outside Blender
    openvdb = None


def _dense(grid, vector):
    low, high = grid.evalActiveVoxelBoundingBox()
    if any(h < l for l, h in zip(low, high)):
        return None, None
    # Align to even indices so pairs of voxels map to one coarse voxel.
    low = tuple((value // 2) * 2 for value in low)
    shape = [h - l + 1 for l, h in zip(low, high)]
    shape = [s + (s % 2) for s in shape]
    array = np.zeros(tuple(shape) + ((3,) if vector else ()), np.float32)
    grid.copyToArray(array, ijk=low)
    return array, low


def _downsample(array):
    sx, sy, sz = array.shape[:3]
    rest = array.shape[3:]
    return array.reshape((sx // 2, 2, sy // 2, 2, sz // 2, 2) + rest).mean(axis=(1, 3, 5))


def _coarse_transform(grid):
    """Twice the voxel size; coarse voxel J is the mean of fine voxels 2J and 2J+1."""
    fine = grid.transform.voxelSize()[0]
    origin = np.asarray(grid.transform.indexToWorld((0, 0, 0))) + 0.5 * fine
    size = 2.0 * fine
    matrix = [[size, 0.0, 0.0, 0.0], [0.0, size, 0.0, 0.0], [0.0, 0.0, size, 0.0],
              [float(origin[0]), float(origin[1]), float(origin[2]), 1.0]]
    return openvdb.createLinearTransform(matrix)


def lighten_file(source, target, cull=0.01):
    """Write a culled, half-resolution copy of one VDB frame. Returns bytes written."""
    grids, metadata = openvdb.readAll(source)
    density = next((g for g in grids if g.name == "density"), None)
    fine_size = density.transform.voxelSize()[0] if density is not None else None
    mask = None
    if density is not None:
        values, low = _dense(density, False)
        if values is not None:
            coarse = _downsample(values)
            mask = (coarse >= cull, tuple(v // 2 for v in low))
    out = []
    for grid in grids:
        vector = "Vec3" in type(grid).__name__
        size = grid.transform.voxelSize()[0]
        if fine_size is not None and size > fine_size * 1.5 and grid.name != "velocity":
            out.append(grid)  # already coarse (glow)
            continue
        values, low = _dense(grid, vector)
        if values is None:
            out.append(grid)
            continue
        coarse = _downsample(values)
        coarse_low = tuple(v // 2 for v in low)
        if mask is not None and abs(size - fine_size) < 1e-6 * fine_size:
            keep, mask_low = mask
            if keep.shape == coarse.shape[:3] and mask_low == coarse_low:
                if vector:
                    coarse[~keep] = 0.0
                else:
                    coarse = np.where(keep, coarse, 0.0).astype(np.float32)
        elif not vector and grid.name in ("density", "fuel", "flame"):
            coarse = np.where(coarse >= cull, coarse, 0.0).astype(np.float32)
        result = type(grid)()
        result.name = grid.name
        result.gridClass = grid.gridClass
        result.transform = _coarse_transform(grid)
        result.copyFromArray(np.ascontiguousarray(coarse), ijk=coarse_low,
                             tolerance=(0.0, 0.0, 0.0) if vector else 0.0)
        result.saveFloatAsHalf = True
        out.append(result)
    temporary = target + ".tmp"
    openvdb.write(temporary, grids=out, metadata=metadata)
    os.replace(temporary, target)
    return os.path.getsize(target)


def make_lighter_copy(source_dir, target_dir, files, cull=0.01, progress=None):
    """Lighten every frame file (names relative to source_dir) into target_dir."""
    os.makedirs(target_dir, exist_ok=True)
    before = after = 0
    for index, name in enumerate(files):
        source = os.path.join(source_dir, name)
        before += os.path.getsize(source)
        after += lighten_file(source, os.path.join(target_dir, name), cull)
        if progress:
            progress(index + 1, len(files))
    for extra in (".fumaris_settings.json",):
        path = os.path.join(source_dir, extra)
        if os.path.isfile(path):
            shutil.copy2(path, os.path.join(target_dir, extra))
    return before, after
