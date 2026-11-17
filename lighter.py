"""Make Lighter Copy: cull faint smoke and halve the resolution of a bake.

Writes a derived cache slot next to the source one (FUTURE "Bake
post-processing", default chosen 2026-11-05): every frame's grids are
averaged 2x2x2 into voxels twice as large, and voxels whose smoke falls below
the cull threshold are dropped. The Pyro look's grids, already at half
resolution, are halved again: copied as they were, they were most of a fire's
lighter copy (121 -> 115 MB, 2026-11-14). Since 2.0.0 beta 2 they are "pyro"
(heat, glow, glow_depth packed on the heat lattice), density_soft and glow_wide,
all on one lattice, so all three are halved alike. Older bakes' coarse glow
grids (glow, glow_wide, glow_depth) are copied unchanged (their lattice is the
light's spread).
"""

# Copied as they are (older bakes' coarse glow lattices).
_KEEP = ("glow", "glow_wide", "glow_depth")
# On the Pyro lattice in newer bakes (with "pyro"): halved like it.
_ON_PYRO_LATTICE = ("glow_wide",)
# Culled by their own values when no density mask applies.
_CULLED = ("density", "fuel", "flame", "density_soft")
import os
import shutil

import numpy as np

try:
    import openvdb
except ImportError:  # outside Blender
    openvdb = None


# Largest dense array one grid region is copied into. A sparse frame (two
# puffs far apart) has a huge bounding box: it is split into active regions.
BLOCK_BYTES = 256 << 20


def _even_box(low, high):
    """Align to even indices so pairs of voxels map to one coarse voxel."""
    low = tuple((value // 2) * 2 for value in low)
    shape = [h - l + 1 for l, h in zip(low, high)]
    return low, tuple(s + (s % 2) for s in shape)


def _active_box(grid):
    low, high = grid.evalActiveVoxelBoundingBox()
    if any(h < l for l, h in zip(low, high)):
        return None
    return _even_box(low, high)


def _fits(box, vector):
    return box is None or np.prod(box[1], dtype=np.float64) * 4 * (3 if vector else 1) <= BLOCK_BYTES


def _dense(grid, vector):
    box = _active_box(grid)
    if box is None:
        return None, None
    low, shape = box
    array = np.zeros(shape + ((3,) if vector else ()), np.float32)
    grid.copyToArray(array, ijk=low)
    return array, low


def _regions(grid, vector):
    """(grid, low, shape) regions covering the active voxels, each within BLOCK_BYTES.

    Halves are split at even indices, so no coarse voxel spans two regions; each
    half is a copy with the other side deactivated, shrunk to its active box.
    """
    stack = [(grid, _active_box(grid))]
    while stack:
        part, box = stack.pop()
        if box is None:
            continue
        low, shape = box
        if _fits(box, vector) or max(shape) <= 2:
            yield part, low, shape
            continue
        axis = int(np.argmax(shape))
        middle = low[axis] + shape[axis] // 2
        # Cut on the coarsest node boundary inside the box (OpenVDB nodes span
        # 4096, 128 and 8 voxels): a fill face through nodes allocates a plane of them.
        for step in (4096, 128, 8, 2):
            split = (middle // step) * step
            if low[axis] < split < low[axis] + shape[axis]:
                break
        cut_low = [(l // 4096) * 4096 for l in low]
        cut_high = [((l + s + 4095) // 4096) * 4096 - 1 for l, s in zip(low, shape)]
        for keep_low in (True, False):
            half = part.deepCopy()
            other_low, other_high = list(cut_low), list(cut_high)
            if keep_low:
                other_low[axis] = split
            else:
                other_high[axis] = split - 1
            half.fill(tuple(other_low), tuple(other_high), half.background, active=False)
            stack.append((half, _active_box(half)))


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
    density_box = _active_box(density) if density is not None else None
    mask = None
    if density is not None and _fits(density_box, False):
        values, low = _dense(density, False)
        if values is not None:
            coarse = _downsample(values)
            mask = (coarse >= cull, tuple(v // 2 for v in low))
    out = {}
    culled_density = None
    # Density first: grids too large for one array are culled by its coarse copy.
    packed = any(g.name == "pyro" for g in grids)
    for grid in sorted(grids, key=lambda g: g.name != "density"):
        vector = "Vec3" in type(grid).__name__
        size = grid.transform.voxelSize()[0]
        if grid.name in _KEEP and not (packed and grid.name in _ON_PYRO_LATTICE):
            out[id(grid)] = grid
            continue
        box = _active_box(grid)
        if not _fits(box, vector):
            # The single-array rules: a grid on density's voxels is culled where
            # density is (only if their boxes match); others by their own values.
            masked = density_box is not None and abs(size - fine_size) < 1e-6 * fine_size
            if grid.name == "density":
                masked = False
            out[id(grid)] = _lighten_regions(
                grid, vector, cull, culled_density if masked and box == density_box else None,
                not masked and not vector and grid.name in _CULLED)
            if grid.name == "density":
                culled_density = out[id(grid)]
            continue
        values, low = _dense(grid, vector)
        if values is None:
            out[id(grid)] = grid
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
        elif not vector and grid.name in _CULLED:
            coarse = np.where(coarse >= cull, coarse, 0.0).astype(np.float32)
        result = type(grid)()
        result.name = grid.name
        result.gridClass = grid.gridClass
        result.transform = _coarse_transform(grid)
        result.copyFromArray(np.ascontiguousarray(coarse), ijk=coarse_low,
                             tolerance=(0.0, 0.0, 0.0) if vector else 0.0)
        result.saveFloatAsHalf = True
        _copy_fumaris_metadata(grid, result)
        out[id(grid)] = result
        if grid.name == "density":
            culled_density = result
    temporary = target + ".tmp"
    openvdb.write(temporary, grids=[out[id(grid)] for grid in grids], metadata=metadata)
    os.replace(temporary, target)
    return os.path.getsize(target)


def _lighten_regions(grid, vector, cull, culled_density, threshold):
    """As lighten_file's single array, region by region (same values).

    `culled_density` (already culled, same voxels) stands in for the density
    mask; without it, `threshold` culls the grid by its own values.
    """
    result = type(grid)()
    result.name = grid.name
    result.gridClass = grid.gridClass
    result.transform = _coarse_transform(grid)
    for part, low, shape in _regions(grid, vector):
        values = np.zeros(shape + ((3,) if vector else ()), np.float32)
        part.copyToArray(values, ijk=low)
        coarse = _downsample(values)
        del values
        coarse_low = tuple(v // 2 for v in low)
        if culled_density is not None:
            kept = np.zeros(coarse.shape[:3], np.float32)
            culled_density.copyToArray(kept, ijk=coarse_low)
            keep = kept >= cull
            if vector:
                coarse[~keep] = 0.0
            else:
                coarse = np.where(keep, coarse, 0.0).astype(np.float32)
        elif threshold:
            coarse = np.where(coarse >= cull, coarse, 0.0).astype(np.float32)
        result.copyFromArray(np.ascontiguousarray(coarse), ijk=coarse_low,
                             tolerance=(0.0, 0.0, 0.0) if vector else 0.0)
    result.saveFloatAsHalf = True
    _copy_fumaris_metadata(grid, result)
    return result


def _copy_fumaris_metadata(source, target):
    """Fumaris's own grid metadata (the "pyro" grid's density reference,
    which Thin Gas Auto is keyed from at import)."""
    for key, value in source.metadata.items():
        if key.startswith("fumaris_"):
            target[key] = value


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
