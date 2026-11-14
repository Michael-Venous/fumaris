"""Auto Simulation Memory: the amount Auto reserves and the panel's Auto line.

The bridge resolves Auto from the GPU's free memory when the simulation starts
(src/vram_budget.cpp); auto_memory_bytes mirrors its rule (vram_budget_test.cpp
and test_memory_auto.py check the same values). Drawing never starts a
subprocess.
"""
import threading

AUTO_FRACTION = 0.8
AUTO_MINIMUM_BYTES = 512 << 20
DEFAULT_FALLBACK_BYTES = 1 << 30


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
    """Read-only line under Resolution / VRAM: the Auto amount."""
    budget, when = budget_bytes_for_display(props, state)
    column = layout.column(align=True)
    if getattr(props, "simulation_memory_auto", False):
        if budget is None:
            column.label(text=f"Auto: {AUTO_FRACTION:.0%} of free GPU memory at start", icon="MEMORY")
        else:
            column.label(text=f"Auto: {budget / (1 << 30):.1f} GB ({when})", icon="MEMORY")
    elif budget is not None:
        # A set amount above what the GPU has spills into system memory.
        reading = selected_reading(_gpu_uuid())
        total = reading.get("device_local_bytes") if reading else None
        if total and budget > total:
            row = column.row()
            row.alert = True
            row.label(text=f"More than this GPU's {total / (1 << 30):.0f} GB: it will run slowly", icon="ERROR")
    if budget is not None:
        column.separator()


def auto_toggled(props, _context):
    if props.simulation_memory_auto:
        refresh_readings_async()
