"""GPU discovery never blocks drawing: the preferences start it on a thread
the first time they are drawn, and Refresh runs it again."""
import json
import re
import subprocess


UUID_PATTERN = re.compile(r"[0-9a-f]{32}\Z")


def device_environment(environment, uuid=""):
    """An explicit UI choice wins over a legacy index override."""
    env = environment.copy()
    if uuid:
        if not UUID_PATTERN.fullmatch(uuid) or uuid == "0" * 32:
            raise RuntimeError("Invalid saved GPU selection; refresh GPUs in Fumaris preferences")
        env["FUMARIS_GPU_DEVICE_UUID"] = uuid
        env.pop("FUMARIS_GPU_DEVICE_INDEX", None)
    return env


def discover_devices(executable, environment):
    result = subprocess.run(
        [executable, "--list-devices"], env=environment,
        stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
        text=True, timeout=15,
        **({"creationflags": subprocess.CREATE_NO_WINDOW} if hasattr(subprocess, "CREATE_NO_WINDOW") else {}),
    )
    if result.returncode:
        from .linux_runtime import explain_launch_failure
        message = result.stderr.strip()[-1000:] or "GPU discovery failed; update the Fumaris bridge"
        raise RuntimeError(explain_launch_failure(executable, environment, message, result.stderr))
    try:
        devices = json.loads(result.stdout)["devices"] or []
        if not isinstance(devices, list):
            raise ValueError("Invalid device list")
        uuids = [d["uuid"] for d in devices]
        return [d for d in devices if d["type"] in {"discrete", "integrated", "virtual"}
                and UUID_PATTERN.fullmatch(d["uuid"]) and d["uuid"] != "0" * 32
                and uuids.count(d["uuid"]) == 1]
    except (ValueError, KeyError, TypeError) as error:
        raise RuntimeError("Bridge does not provide a valid GPU list; update the Fumaris bridge") from error


_PREFERENCE = {"discrete": 3, "integrated": 2, "virtual": 1}


def automatic_device(devices, environment):
    """The GPU the bridge picks under Automatic (flow_device.cpp's
    selectFlowDevice): a FUMARIS_GPU_DEVICE_UUID override, else the first
    of the most preferred type (discrete, then integrated). None when it
    cannot be told (a FUMARIS_GPU_DEVICE_INDEX override counts Vulkan
    devices this list leaves out)."""
    uuid = (environment.get("FUMARIS_GPU_DEVICE_UUID") or "").strip().lower()
    if uuid:
        return next((device for device in devices if device["uuid"] == uuid), None)
    if environment.get("FUMARIS_GPU_DEVICE_INDEX"):
        return None
    best = None
    for device in devices:
        if best is None or _PREFERENCE.get(device["type"], 0) > _PREFERENCE.get(best["type"], 0):
            best = device
    return best


def not_recommended(device, devices):
    """An integrated GPU while a discrete one is present: slow for a
    simulation and sharing system memory, so it is not offered first."""
    return device["type"] == "integrated" and any(other["type"] == "discrete" for other in devices)
