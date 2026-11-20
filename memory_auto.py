"""Auto Simulation Memory: the amount Auto reserves and the panel's Auto line.

The bridge resolves Auto from the GPU's free memory when the simulation starts
(src/vram_budget.cpp); auto_memory_bytes mirrors its rule (vram_budget_test.cpp
and test_memory_auto.py check the same values). Drawing never starts a
subprocess.
"""
import threading

# Auto leaves max(1 GiB, 6% of the GPU) of the free memory to everything else
# (the bridge's buffers outside the block pool, Blender, the desktop); it was
# 80% of free memory, which left 4.7 GB of a free 24 GB card idle (2026-11-17).
AUTO_RESERVE_SHARE = 0.06
AUTO_RESERVE_MINIMUM_BYTES = 1 << 30
AUTO_MINIMUM_BYTES = 512 << 20
DEFAULT_FALLBACK_BYTES = 1 << 30


def auto_memory_bytes(free_bytes=None, device_local_bytes=None,
                      fallback_bytes=DEFAULT_FALLBACK_BYTES, reserve_share=AUTO_RESERVE_SHARE):
    """autoSimulationMemory: free memory less the reserve, half the device
    without a budget reading, otherwise the explicit value; never below the
    minimum."""
    reserve_share = min(0.95, max(0.0, float(reserve_share)))
    if free_bytes is not None:
        reserve = max(AUTO_RESERVE_MINIMUM_BYTES, int(int(device_local_bytes or 0) * reserve_share))
        result = max(0, int(free_bytes) - reserve)
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
    """The chosen GPU; under Automatic, a FUMARIS_GPU_DEVICE_UUID launch
    override (the GPU the bridge then takes, as Preferences names it)."""
    import os
    from .utils import get_addon_preferences
    chosen = getattr(get_addon_preferences(), "gpu_device_uuid", "") or ""
    return chosen or os.environ.get("FUMARIS_GPU_DEVICE_UUID", "").strip().lower()


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


def free_bytes_for_display(props, state=None):
    """The free GPU memory behind the Auto amount budget_bytes_for_display
    shows (at the last start, or now), or None if unknown."""
    if not getattr(props, "simulation_memory_auto", False):
        return None
    metrics = (state or {}).get("metrics", {}) if state else {}
    try:
        used = int(float(metrics.get("flow_auto_memory_bytes", 0)))
        free = int(float(metrics.get("flow_free_memory_at_start_bytes", 0)))
    except (TypeError, ValueError):
        used = free = 0
    if used > 0:
        return free if free > 0 else None
    reading = selected_reading(_gpu_uuid())
    return reading.get("free_bytes") if reading else None


def budget_lines(props, state=None):
    """['Auto VRAM: 9.0 GB', '10.0 GB free, 1.0 GB left'], or None: what Auto
    takes and what it leaves to other programs, so a full GPU reads as other
    programs using it, not as Fumaris asking too much. Two short lines: one
    line was cut off at the default Properties width (beta 3 review)."""
    budget, when = budget_bytes_for_display(props, state)
    if budget is None:
        return None
    gib = float(1 << 30)
    head = f"Auto VRAM: {budget / gib:.1f} GB"
    free = free_bytes_for_display(props, state)
    if free is None:
        return [head, when] if when else [head]
    left = max(0, int(free) - int(budget))
    return [head, f"{free / gib:.1f} GB free, {left / gib:.1f} GB left"]


def budget_text(props, state=None):
    """'Auto VRAM: 9.0 GB (10.0 GB free, 1.0 GB left)', or None."""
    lines = budget_lines(props, state)
    if lines is None:
        return None
    return lines[0] + "".join(f" ({line})" for line in lines[1:])


# Memory per block with Full-Resolution Channels against without it, at the
# same VRAM (2 GB: 24,391 blocks; 6,859 for a fire, 8,400 for smoke;
# notes/experiments/fullres-tradeoffs-2026-11-19 section 1). Auto keeps the
# amount, so the same VRAM holds that many times fewer blocks.
FULL_RESOLUTION_BLOCK_COST_FIRE = 3.6
FULL_RESOLUTION_BLOCK_COST_SMOKE = 2.9
# Pool-independent buffers in a run's estimate (the bridge's fixed overhead
# and the preview's), not scaled with the block cost.
_FIXED_ESTIMATE_BYTES = 176 << 20


def full_resolution_forecast(props, state, fire=False):
    """(needed_bytes, budget_bytes) for the last run's peak blocks with
    Full-Resolution Channels, when it is on now and the last run (state) was
    without it; else None. fire: the run kept a fire's fuel and burn at half
    resolution (they move to the density grid too)."""
    if not state or state.get("full_resolution", True) or not getattr(props, "full_resolution_velocity", False):
        return None
    metrics = state.get("metrics", {})

    def number(name):
        try:
            return float(metrics.get(name, 0) or 0)
        except (TypeError, ValueError):
            return 0.0

    peak = float(state.get("peak_blocks", 0) or 0)
    capacity = number("flow_active_block_capacity")
    if peak <= 0 or capacity <= 0:
        return None
    # What the run's pool cost per block: its own estimate, or the amount it
    # started with (an older bridge reports no estimate).
    pool = number("flow_estimated_memory_bytes")
    if pool <= 0:
        started = state.get("vram_setting")
        pool = number("flow_auto_memory_bytes") if started == "auto" else float(started or 0) * (1 << 30)
    if pool <= 0:
        return None
    fixed = min(pool / 2, _FIXED_ESTIMATE_BYTES)
    per_block = (pool - fixed) / capacity
    cost = FULL_RESOLUTION_BLOCK_COST_FIRE if fire else FULL_RESOLUTION_BLOCK_COST_SMOKE
    budget, _when = budget_bytes_for_display(props, state)
    if not budget:
        return None
    return int(fixed + peak * per_block * cost), int(budget)


def full_resolution_lines(props, state, fire=False):
    """(lines, alert) for the forecast, or None: two short lines that fit the
    default Properties width. alert: the peak would fill 85% of VRAM or more
    (the panel's "nearly full" notice), or not fit."""
    forecast = full_resolution_forecast(props, state, fire)
    if forecast is None:
        return None
    needed, budget = forecast
    gib = float(1 << 30)
    need = needed / gib
    amount = f"~{need:.0f}" if need >= 10 else f"~{need:.1f}"
    amount += f" of {budget / gib:.1f} GB"
    use = needed / budget
    if use >= 1.0:
        second, alert = f"{amount}: smoke cut off", True
    elif use >= 0.85:
        second, alert = f"{amount}: nearly full", True
    else:
        second, alert = f"{amount} VRAM", False
    return ["Last run's peak at full res:", second], alert


def draw_full_resolution_forecast(layout, props, state, fire=False, alerts_only=False):
    """The forecast under Full-Resolution Channels (or, alerts_only, under
    VRAM): what the last run's peak needs with its 3-4x cost per block."""
    result = full_resolution_lines(props, state, fire)
    if result is None:
        return False
    lines, alert = result
    if alerts_only and not alert:
        return False
    column = layout.column(align=True)
    for index, line in enumerate(lines):
        row = column.row()
        row.alert = alert
        row.label(text=line, icon=("ERROR" if alert else "INFO") if index == 0 else "BLANK1")
    if alert:
        row = column.row()
        row.alert = True
        row.label(text="Raise VRAM or lower Resolution" if not getattr(props, "simulation_memory_auto", False)
                  else "Lower Resolution or free VRAM", icon="BLANK1")
    return True


def draw_budget_summary(layout, props, state=None):
    """Read-only line under Resolution / VRAM: the Auto amount."""
    budget, _when = budget_bytes_for_display(props, state)
    if getattr(props, "simulation_memory_auto", False):
        gib = float(1 << 30)
        free = free_bytes_for_display(props, state)
        text = f"Auto VRAM {budget / gib:.1f} GB" if budget is not None else "Auto VRAM: free memory at start"
        if budget is not None and free is not None:
            text += f" · {max(0, free - budget) / gib:.1f} GB left"
        layout.label(text=text)
    elif budget is not None:
        # A set amount above what the GPU has: the bridge reserves what fits
        # and spills into system memory only when the smoke needs more.
        # Two short lines: one was cut off at the default panel width.
        reading = selected_reading(_gpu_uuid())
        total = reading.get("device_local_bytes") if reading else None
        if total and budget > total:
            column = layout.column(align=True)
            row = column.row()
            row.alert = True
            row.label(text=f"Above this GPU's {total / (1 << 30):.0f} GB:", icon="ERROR")
            row = column.row()
            row.alert = True
            row.label(text="the rest is slow (system RAM)", icon="BLANK1")


def auto_toggled(props, _context):
    if props.simulation_memory_auto:
        refresh_readings_async()
