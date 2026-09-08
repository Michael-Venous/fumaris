import json
import math
import platform
import textwrap
from collections import deque


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


def begin(domain, mode):
    key = _key(domain)
    _STATES[key] = {
        "domain": domain.name,
        "mode": mode,
        "status": "Initializing",
        "active": True,
        "metrics": {},
        "log": deque(maxlen=80),
    }
    # Diagnostics are session-only, not part of the .blend or cache.
    while len(_STATES) > 64:
        _STATES.pop(next(iter(_STATES)))
    return key


def snapshot(domain):
    return _STATES.get(_key(domain))


def clear():
    _STATES.clear()


def record(domain, data):
    state = snapshot(domain)
    if state is None:
        return
    for name, value in data.items():
        if not (name.startswith(("flow_", "bridge_", "blender_")) or name in {
            "frame", "frame_ms", "preview_render_ms", "payload_bytes",
        }):
            continue
        if isinstance(value, str):
            state["metrics"][name] = value[:512]
        elif isinstance(value, (int, float)) and math.isfinite(value):
            state["metrics"][name] = value


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
    if "detail memory limit reached" in message.lower():
        return "Smoke detail memory limit reached."
    return textwrap.shorten(message, width=60, placeholder="...")


def warnings(state):
    result = []
    capacity = value(state, "flow_active_block_capacity")
    active = value(state, "flow_active_blocks")
    if capacity and active / capacity >= 0.85:
        result.append("Sparse blocks nearly full: increase capacity or lower resolution.")
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
    return result


def report(domain):
    import bpy
    import gpu
    from . import bl_info

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
        "fumaris_version": ".".join(map(str, bl_info["version"])),
        "blender_version": bpy.app.version_string,
        "platform": platform.platform(),
        "viewport_gpu": viewport,
        "memory_note": "Flow allocation counters are not total GPU usage/free VRAM. RAM is process RSS; shared pages may be counted twice.",
        "timing_note": "Texture upload is the latest completed Blender draw upload, not necessarily the reported simulation frame. GPU timings may describe an earlier completed frame.",
    }
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
