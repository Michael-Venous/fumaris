import json
import math
import platform
import textwrap
import tomllib
from collections import deque
from pathlib import Path


_STATES = {}


def console_log(message):
    try:
        print(message)
    except (OSError, ValueError):
        pass


def _key(domain):
    if domain is None or isinstance(domain, int):
        return domain
    return int(domain.session_uid)


def _vram_setting(domain):
    props = getattr(domain, "fumaris", None)
    if props is None:
        return None
    if getattr(props, "simulation_memory_auto", False):
        return "auto"
    return round(float(props.simulation_memory), 3)


def vram_setting_changed(domain, state):
    """True when VRAM (or Auto) differs from what the session started with."""
    started = (state or {}).get("vram_setting")
    return started is not None and started != _vram_setting(domain)


def begin(domain, mode):
    key = _key(domain)
    _STATES[key] = {
        "domain": domain.name,
        "mode": mode,
        "status": "Initializing",
        "active": True,
        "metrics": {},
        "log": deque(maxlen=80),
        # The VRAM setting this session started with: a later change applies
        # only when the simulation restarts.
        "vram_setting": _vram_setting(domain),
    }
    # Diagnostics are session-only, not part of the .blend or cache.
    while len(_STATES) > 64:
        _STATES.pop(next(iter(_STATES)))
    return key


def snapshot(domain):
    return _STATES.get(_key(domain))


def clear(domain=None):
    if domain is None:
        _STATES.clear()
    else:
        _STATES.pop(_key(domain), None)


def record(domain, data):
    state = snapshot(domain)
    if state is None:
        return
    for name, entry in data.items():
        if not (name.startswith(("flow_", "bridge_", "blender_")) or name in {
            "frame", "frame_ms", "preview_render_ms", "payload_bytes",
        }):
            continue
        if isinstance(entry, str):
            state["metrics"][name] = entry[:512]
        elif isinstance(entry, (int, float)) and math.isfinite(entry):
            state["metrics"][name] = entry
    if pool_use(state) >= 1.0:
        # Keep evidence of lost allocation visible if blocks are later freed.
        state["capacity_reached"] = True


def pool_use(state):
    """Return how full the fullest block pool is, 0 to 1.

    Every block has velocity storage; the blocks that hold smoke (not the air
    reserved around it) also have density storage, in a pool of their own.
    """
    capacity = value(state, "flow_active_block_capacity")
    use = value(state, "flow_active_blocks") / capacity if capacity > 0 else 0.0
    fine_capacity = value(state, "flow_fine_block_capacity")
    if fine_capacity > 0:
        use = max(use, value(state, "flow_fine_blocks") / fine_capacity)
    return use


def capacity_status(state):
    """Return a per-run capacity notice, including an exhausted run's history."""
    if not state:
        return None
    use = pool_use(state)
    if state.get("capacity_reached") or use >= 1.0:
        return "full"
    if use >= 0.95:
        return "near"
    return None


def set_status(domain, status):
    state = snapshot(domain)
    if state:
        state["status"] = status


def add_log(domain, message):
    state = snapshot(domain)
    if state is None:
        return
    for line in message.splitlines():
        line = line.strip()[:1024]
        if line and (not state["log"] or state["log"][-1] != line):
            state["log"].append(line)


def finish(domain):
    state = snapshot(domain)
    if state:
        state["active"] = False
        if state["status"] != "Failed":
            state["status"] = "Stopped"


def value(state, name):
    try:
        result = float(state["metrics"].get(name, 0))
        return result if math.isfinite(result) else 0.0
    except (TypeError, ValueError):
        return 0.0


def error_summary(state):
    if not state or state["status"] != "Failed":
        return ""
    errors = [line for line in state["log"] if line.lower().startswith("error:")]
    message = errors[-1] if errors else "Simulation stopped; expand for details."
    return textwrap.shorten(message, width=60, placeholder="...")


def warnings(state):
    result = []
    if capacity_status(state) == "full":
        result.append("Simulation capacity reached: smoke may be cut off. Increase VRAM or lower Resolution, then restart.")
    elif pool_use(state) >= 0.85:
        result.append("Sparse blocks nearly full: increase VRAM or lower Resolution.")
    requested = value(state, "flow_requested_resolution")
    actual = value(state, "flow_effective_resolution")
    if requested and actual and actual < requested * 0.95:
        result.append("Grid is coarser than requested: check Auto Cell Size and block capacity.")
    if state["metrics"].get("flow_device_type") == "cpu":
        result.append("Flow selected a CPU Vulkan device; check your GPU driver/device selection.")
    for line in state["log"]:
        if any(word in line.lower() for word in (
            "warning", "error", "failed", "fallback", "exceed", "capacity",
        )) and line not in result:
            result.append(line)
    # Last, so it stays among the warnings the panel shows.
    message = memory_spill_warning(state)
    if message:
        result.append(message)
    return result


def memory_usage(state):
    """Return (used_bytes, allocated_bytes) for the simulation, or None.

    The pool is allocated in full when the simulation starts, so "used" is the
    share of that allocation holding active blocks. Density storage (about four
    fifths of the memory) is counted by the blocks that have it.
    """
    if not state:
        return None
    allocated = value(state, "flow_device_memory_bytes")
    capacity = value(state, "flow_active_block_capacity")
    active = value(state, "flow_active_blocks")
    if allocated <= 0 or capacity <= 0:
        return None
    use = active / capacity
    fine_capacity = value(state, "flow_fine_block_capacity")
    if fine_capacity > 0:
        use = 0.8 * value(state, "flow_fine_blocks") / fine_capacity + 0.2 * use
    return allocated * min(1.0, use), allocated


def memory_full_advice(state):
    """What to do when the simulation does not fit in GPU memory. Under
    Auto there is no VRAM amount to lower: Auto already took what was free."""
    again = "bake again" if (state or {}).get("mode") in ("Bake", "Background Bake") else "press Play again"
    if (state or {}).get("vram_setting") == "auto":
        return f"Close other GPU programs, or turn Auto off and set a lower VRAM, then {again}."
    return f"Lower VRAM or close other GPU programs, then {again}."


def memory_spill_warning(state):
    """Explain a simulation that no longer fits in GPU memory."""
    spilled = value(state, "flow_system_memory_fallback_bytes") if state else 0.0
    if spilled <= 0:
        return ""
    return (
        f"GPU memory is full: {spilled / 1073741824:.1f} GB of simulation data went to "
        "system memory, so it runs very slowly. " + memory_full_advice(state)
    )


def report(domain):
    import bpy
    import gpu

    # Blender's extension loader removes legacy bl_info from the package.
    with Path(__file__).with_name("blender_manifest.toml").open("rb") as stream:
        version = tomllib.load(stream)["version"]

    state = snapshot(domain)
    viewport = {}
    try:
        viewport = {
            "backend": gpu.platform.backend_type_get(),
            "device": gpu.platform.renderer_get(),
            "vendor": gpu.platform.vendor_get(),
            "driver": gpu.platform.version_get(),
        }
    except (RuntimeError, AttributeError, SystemError):
        pass
    data = {
        "fumaris_version": version,
        "blender_version": bpy.app.version_string,
        "platform": platform.platform(),
        "viewport_gpu": viewport,
        "memory_note": "Flow allocation counters are not total GPU usage/free VRAM. RAM is process RSS; shared pages may be counted twice.",
        "timing_note": "Texture upload is the latest completed Blender draw upload, not necessarily the reported simulation frame. GPU timings may describe an earlier completed frame.",
    }
    try:
        from .linux_runtime import runtime_diagnostics
        from .utils import executable_path, process_environment
        executable = executable_path()
        linux = runtime_diagnostics(executable, process_environment(executable))
        if linux is not None:
            data["linux_runtime"] = linux
    except Exception:
        pass
    if state:
        data.update({
            "domain": domain.name,
            "mode": state["mode"],
            "status": state["status"],
            "active": state["active"],
            "metrics": dict(state["metrics"]),
            "warnings": warnings(state),
            "recent_bridge_log": list(state["log"]),
        })
    return json.dumps(data, indent=2, sort_keys=True)
