"""GPU discovery is explicit; drawing preferences never starts a subprocess."""
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
        raise RuntimeError(result.stderr.strip()[-1000:] or "GPU discovery failed; update the Fumaris bridge")
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
