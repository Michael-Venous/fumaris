"""Auto Simulation Memory and what a Simulation Memory budget buys.

The bridge sizes the sparse pool from Simulation Memory (src/memory_budget.cpp)
and resolves Auto from the GPU's free memory when the simulation starts
(src/vram_budget.cpp). This module mirrors both so the panel can show the
voxel size and the region the budget covers before anything runs. It uses the
estimate's raw-texel fallback; the bridge asks the driver for exact texture
sizes, so its capacity differs by a few percent. Keep the constants in step
with the C++ (tools/tests/memory_budget_test.cpp and test_memory_auto.py check
the same values). Drawing never starts a subprocess.
"""
import functools
import threading

AUTO_FRACTION = 0.8
AUTO_MINIMUM_BYTES = 512 << 20
DEFAULT_FALLBACK_BYTES = 1 << 30

# src/memory_budget.cpp
_FIXED_OVERHEAD_BYTES = 96 << 20
_PER_LOCATION_BUFFER_BYTES = 1536
_VEC3_LEAF_BYTES = 6272
_NANOVDB_NODE_SHARE = 0.03
_PREVIEW_FIXED_BYTES = 80 << 20
_RGBA16F, _RG16F, _R16F, _R8 = 8, 4, 2, 1

# Flow density cell: 32 scene units / Resolution (src/simulation.cpp).
_FLOW_UNITS_PER_RESOLUTION = 32.0


def _density_bits(small_blocks):
    return (4, 3, 3) if small_blocks else (5, 4, 4)


def _coarser(bits):
    return tuple(value - 1 for value in bits)


def pool_dimensions(capacity, bits, max_texture_dimension=0):
    """NvFlowSparsePool_dimensions: atlas blocks per axis for a capacity."""
    device = max_texture_dimension or 2048
    extent_x = min(device, 4096)
    extent_yz = min(device, 2048)
    limits = (
        extent_x // ((1 << bits[0]) + 2),
        extent_yz // ((1 << bits[1]) + 2),
        extent_yz // ((1 << bits[2]) + 2),
    )
    if capacity >= limits[0] * limits[1] * limits[2]:
        return limits
    x = y = z = 1
    while x * y * z < capacity:
        can_x, can_y, can_z = x < limits[0], y < limits[1], z < limits[2]
        if can_x and (x <= y or not can_y) and (x <= z or not can_z):
            x += 1
        elif can_y and (y <= z or not can_z):
            y += 1
        elif can_z:
            z += 1
        else:
            break
    return x, y, z


def _atlas_inventory(small_blocks, effector_sources, preview):
    density = _density_bits(small_blocks)
    velocity = _coarser(density)
    textures = [
        (density, _RGBA16F, 3),
        (density, _R8, 1),
        (velocity, _RGBA16F, 5 if effector_sources else 4),
        (velocity, _RG16F, 2),
        (velocity, _R8, 1),
    ]
    if preview:
        textures.append((velocity, _R16F, 1))
    level = _coarser(velocity)
    while True:
        coarsest = level[1] <= 1 or level[2] <= 1
        textures.append((level, _RG16F, 3 if coarsest else 2))
        if coarsest:
            return textures
        level = _coarser(level)


def _buffer_growth_size(size):
    if size > 64 << 20:
        granularity = 16 << 20
        target = size + size // 4
        return (target + granularity - 1) // granularity * granularity
    result = 65536
    while result < size:
        result *= 2
    return result


def _leaves_per_block(bits):
    result = 1
    for value in bits:
        result *= 1 << (value - 3) if value > 3 else 1
    return result


def estimate_bytes(capacity, *, small_blocks=False, effector_sources=True,
                   preview=False, export_velocity=False):
    """estimateSimulationBytes with the raw-texel fallback (no device query)."""
    capacity = max(1, int(capacity))
    density = _density_bits(small_blocks)
    dims = pool_dimensions(capacity, density)
    total = _FIXED_OVERHEAD_BYTES + _PER_LOCATION_BUFFER_BYTES * capacity
    if preview:
        total += _PREVIEW_FIXED_BYTES
    for bits, texel_bytes, count in _atlas_inventory(small_blocks, effector_sources, preview):
        raw = texel_bytes
        for axis in range(3):
            raw *= dims[axis] * ((1 << bits[axis]) + 2)
        total += (raw + raw // 10) * count
    if export_velocity:
        slots = dims[0] * dims[1] * dims[2]
        leaves = slots * _leaves_per_block(_coarser(density)) * _VEC3_LEAF_BYTES
        total += _buffer_growth_size(int(float(leaves) * (1.0 + _NANOVDB_NODE_SHARE)))
    return total


def capacity_for_budget(budget_bytes, **config):
    """locationCapacityForBudget: largest capacity whose estimate fits."""
    limits = pool_dimensions(1 << 62, _density_bits(config.get("small_blocks", False)))
    low, high = 1, limits[0] * limits[1] * limits[2]
    if estimate_bytes(low, **config) > budget_bytes:
        return low
    while low < high:
        mid = low + (high - low + 1) // 2
        if estimate_bytes(mid, **config) <= budget_bytes:
            low = mid
        else:
            high = mid - 1
    return low


def auto_memory_bytes(free_bytes=None, device_local_bytes=None,
                      fallback_bytes=DEFAULT_FALLBACK_BYTES, fraction=AUTO_FRACTION):
    """autoSimulationMemory: share of free memory, half the device without a
    budget reading, otherwise the explicit value; never below the minimum."""
    fraction = min(1.0, max(0.05, float(fraction)))
    if free_bytes is not None:
        result = int(max(0, free_bytes) * fraction)
    elif device_local_bytes:
        result = int(device_local_bytes) // 2
    else:
        result = int(fallback_bytes) if fallback_bytes else DEFAULT_FALLBACK_BYTES
    return max(result, AUTO_MINIMUM_BYTES)


def voxel_size(resolution):
    return _FLOW_UNITS_PER_RESOLUTION / max(1, int(resolution))


def block_cells(small_blocks):
    bits = _density_bits(small_blocks)
    return (1 << bits[0]) * (1 << bits[1]) * (1 << bits[2])


@functools.lru_cache(maxsize=64)
def _cached_capacity(budget_bytes, small_blocks, effector_sources, preview, export_velocity):
    return capacity_for_budget(
        budget_bytes, small_blocks=small_blocks, effector_sources=effector_sources,
        preview=preview, export_velocity=export_velocity)


def region_for_budget(budget_bytes, resolution, *, small_blocks=False, effector_sources=True,
                      preview=False, export_velocity=False):
    """(voxel size, Flow locations, region volume) for a budget at a resolution.

    Cached because the panel redraws often while the inputs rarely change.
    """
    config = {"small_blocks": bool(small_blocks)}
    capacity = _cached_capacity(int(budget_bytes), bool(small_blocks), bool(effector_sources),
                                bool(preview), bool(export_velocity))
    voxel = voxel_size(resolution)
    cells = capacity * block_cells(config["small_blocks"])
    return voxel, capacity, cells * voxel ** 3


# Latest --list-devices memory per GPU UUID; written by device discovery
# (preferences refresh, job start, the Auto toggle), read while drawing.
_READINGS = {}
_READINGS_LOCK = threading.Lock()
_REFRESHING = False


def update_readings(devices):
    with _READINGS_LOCK:
        for device in devices:
            uuid = device.get("uuid")
            if not uuid or "memory_device_local_bytes" not in device:
                continue
            try:
                free = device.get("memory_free_bytes")
                _READINGS[uuid] = {
                    "type": device.get("type", ""),
                    "device_local_bytes": int(device["memory_device_local_bytes"]),
                    "free_bytes": int(free) if free is not None else None,
                }
            except (TypeError, ValueError):
                continue


def _device_preference(kind):
    return {"discrete": 3, "integrated": 2, "virtual": 1}.get(kind, 0)


def selected_reading(gpu_uuid=""):
    """Reading for the chosen GPU, or the one Automatic selection prefers."""
    with _READINGS_LOCK:
        if gpu_uuid:
            return _READINGS.get(gpu_uuid)
        best = None
        for reading in _READINGS.values():
            if best is None or _device_preference(reading["type"]) > _device_preference(best["type"]):
                best = reading
        return best


def refresh_readings_async():
    """Read free GPU memory in the background (one bridge --list-devices)."""
    global _REFRESHING
    if _REFRESHING:
        return
    from .devices import discover_devices
    from .utils import executable_path, process_environment, runtime_validation_error
    if runtime_validation_error():
        return
    executable = executable_path()
    environment = process_environment(executable)
    _REFRESHING = True

    def run():
        global _REFRESHING
        try:
            update_readings(discover_devices(executable, environment))
        except Exception:
            pass
        finally:
            _REFRESHING = False

    threading.Thread(target=run, daemon=True).start()


def _gpu_uuid():
    from .utils import get_addon_preferences
    return getattr(get_addon_preferences(), "gpu_device_uuid", "") or ""


def _format_length(value):
    if value < 1.0:
        return f"{value * 100.0:.3g} cm"
    return f"{value:.3g} m"


def _format_volume(value):
    if value >= 1.0e6:
        return f"{value / 1.0e6:.3g} million m³"
    if value >= 1.0:
        return f"{value:,.0f} m³" if value >= 100.0 else f"{value:.3g} m³"
    return f"{value * 1.0e3:.3g} litres"


def budget_bytes_for_display(props, state=None):
    """(bytes, note) behind Simulation Memory, or (None, note) if unknown."""
    explicit = int(float(props.simulation_memory) * (1 << 30))
    if not getattr(props, "simulation_memory_auto", False):
        return explicit, ""
    metrics = (state or {}).get("metrics", {}) if state else {}
    try:
        used = int(float(metrics.get("flow_auto_memory_bytes", 0)))
    except (TypeError, ValueError):
        used = 0
    if used > 0:
        return used, "at last start"
    reading = selected_reading(_gpu_uuid())
    if reading is None:
        return None, ""
    return auto_memory_bytes(reading["free_bytes"], reading["device_local_bytes"], explicit), "now"


def draw_budget_summary(layout, props, state=None):
    """Read-only lines under Resolution / Memory: Auto amount, voxel, region."""
    budget, when = budget_bytes_for_display(props, state)
    column = layout.column(align=True)
    if getattr(props, "simulation_memory_auto", False):
        if budget is None:
            column.label(text=f"Auto: {AUTO_FRACTION:.0%} of free GPU memory at start", icon="MEMORY")
        else:
            column.label(text=f"Auto: {budget / (1 << 30):.1f} GB ({when})", icon="MEMORY")
    voxel = voxel_size(props.resolution)
    if budget is None:
        column.label(text=f"Voxel {_format_length(voxel)}")
        return
    effectors = props.effector_collection is not None and len(props.effector_collection.all_objects) > 0
    _voxel, _capacity, volume = region_for_budget(
        budget, props.resolution,
        small_blocks=bool(props.small_sparse_blocks),
        effector_sources=effectors,
        export_velocity=bool(getattr(props, "export_velocity_vdb", False)),
    )
    column.label(text=f"Voxel {_format_length(voxel)} · space ≈ {_format_volume(volume)} "
                      f"({_format_length(volume ** (1.0 / 3.0))} cube)")


def apply_new_domain_default(props):
    """New domains start on Auto; domains with a memory setting keep it."""
    if (props.smoke_object_type == "domain"
            and props.get("simulation_memory_auto") is None
            and props.get("simulation_memory") is None
            and props.get("sparse_block_capacity") is None):
        props.simulation_memory_auto = True


def auto_toggled(props, _context):
    if props.simulation_memory_auto:
        refresh_readings_async()
