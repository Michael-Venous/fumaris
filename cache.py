import ctypes
import json
import os
import re
import time

LOCK_FILE = ".fumaris.lock"
GUARD_FILE = ".fumaris.guard"
# Present in a cache slot's folder while the user has protected its bake.
# It lives with the files it guards, so it holds whichever scene or domain
# points at that folder.
PROTECT_FILE = ".fumaris_protected"


def is_protected(directory):
    return bool(directory) and os.path.isfile(os.path.join(directory, PROTECT_FILE))


def set_protected(directory, protected):
    path = os.path.join(directory, PROTECT_FILE)
    if protected:
        os.makedirs(directory, exist_ok=True)
        with open(path, "w", encoding="utf-8") as stream:
            stream.write("Fumaris will not bake over or delete this cache slot while this file exists.\n")
    else:
        try:
            os.remove(path)
        except FileNotFoundError:
            pass


class RefusedError(RuntimeError):
    """An action refused before anything ran (a protected version, nothing to
    emit): reported to the user, not kept as the domain's last failure."""


def require_unprotected(directory, action):
    if is_protected(directory):
        slot = os.path.basename(os.path.normpath(directory))
        raise RefusedError(
            f"Version '{slot}' is protected. Turn off Protect (the lock under Output) to {action}, "
            "or make a new version with + under Versions"
        )


def next_slot_name(current, taken):
    """Next free version of a slot name: main, main_001, main_002; v2, v3."""
    match = re.fullmatch(r"(.*?)([0-9]+)", current or "")
    if match:
        stem, number, width = match.group(1), int(match.group(2)), len(match.group(2))
    else:
        stem, number, width = (current or "main") + "_", 0, 3
    while True:
        number += 1
        candidate = f"{stem}{number:0{width}d}"
        if not taken(candidate):
            return candidate


class CacheLock:
    def __init__(self, directory):
        self.directory = os.path.normpath(directory)
        self.path = os.path.join(self.directory, LOCK_FILE)
        self._owned = False
        self._guard = None

    def acquire(self):
        os.makedirs(self.directory, exist_ok=True)
        if self._owned:
            raise RuntimeError("This cache lock is already acquired")
        self._guard = _acquire_guard(self.directory)
        try:
            return self._acquire_owner_file()
        except BaseException:
            self._guard.close()
            self._guard = None
            raise

    def _acquire_owner_file(self):
        try:
            descriptor = os.open(
                self.path,
                os.O_CREAT | os.O_EXCL | os.O_WRONLY,
            )
        except FileExistsError:
            if _lock_is_stale(self.path):
                try:
                    os.remove(self.path)
                except FileNotFoundError:
                    pass
                return self._acquire_owner_file()
            raise RuntimeError(
                f"Fumaris cache is already in use: {self.directory}"
            )
        try:
            with os.fdopen(descriptor, "w", encoding="utf-8") as stream:
                stream.write(str(os.getpid()))
        except BaseException:
            os.remove(self.path)
            raise
        self._owned = True
        return self

    def release(self):
        if not self._owned:
            return
        try:
            try:
                os.remove(self.path)
            except FileNotFoundError:
                pass
        finally:
            self._owned = False
            self._guard.close()
            self._guard = None

    def __enter__(self):
        return self.acquire()

    def __exit__(self, _type, _value, _traceback):
        self.release()


def cache_lock(directory):
    return CacheLock(directory)


def recover_cache_lock(directory, *, owner_pid=None):
    path = os.path.join(os.path.normpath(directory), LOCK_FILE)
    if not os.path.isfile(path):
        return False
    try:
        guard = _acquire_guard(directory)
    except RuntimeError:
        return False
    with guard:
        lock_pid = _lock_owner(path)
        if lock_pid is None or not _pid_is_alive(lock_pid) or lock_pid == owner_pid:
            try:
                os.remove(path)
            except FileNotFoundError:
                pass
            return True
        return False


def _acquire_guard(directory):
    # Never unlink this file: contenders must lock the same inode. The OS
    # releases its lock even if Blender crashes before removing the PID file.
    stream = open(os.path.join(directory, GUARD_FILE), "a+b")
    try:
        if os.name == "nt":
            import msvcrt
            if os.fstat(stream.fileno()).st_size == 0:
                stream.write(b"\0")
                stream.flush()
            stream.seek(0)
            try:
                msvcrt.locking(stream.fileno(), msvcrt.LK_NBLCK, 1)
            except OSError as error:
                raise RuntimeError(f"Fumaris cache is already in use: {directory}") from error
        else:
            import fcntl
            try:
                fcntl.flock(stream.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError as error:
                raise RuntimeError(f"Fumaris cache is already in use: {directory}") from error
        return stream
    except BaseException:
        stream.close()
        raise


def _lock_is_stale(path):
    pid = _lock_owner(path)
    return pid is None or not _pid_is_alive(pid)


def _lock_owner(path):
    try:
        with open(path, encoding="utf-8") as stream:
            return int(stream.read().strip())
    except (FileNotFoundError, ValueError):
        return None


def _pid_is_alive(pid):
    if pid <= 0:
        return False
    if os.name != "nt":
        try:
            os.kill(pid, 0)
            return True
        except PermissionError:
            return True
        except ProcessLookupError:
            return False

    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel32.OpenProcess.argtypes = (ctypes.c_ulong, ctypes.c_int, ctypes.c_ulong)
    kernel32.OpenProcess.restype = ctypes.c_void_p
    kernel32.GetExitCodeProcess.argtypes = (
        ctypes.c_void_p,
        ctypes.POINTER(ctypes.c_ulong),
    )
    kernel32.GetExitCodeProcess.restype = ctypes.c_int
    kernel32.CloseHandle.argtypes = (ctypes.c_void_p,)
    kernel32.CloseHandle.restype = ctypes.c_int
    handle = kernel32.OpenProcess(0x1000, False, pid)
    if not handle:
        return ctypes.get_last_error() == 5
    try:
        exit_code = ctypes.c_ulong()
        if not kernel32.GetExitCodeProcess(handle, ctypes.byref(exit_code)):
            return True
        return exit_code.value == 259
    finally:
        kernel32.CloseHandle(handle)


# Takes: every bake stores the settings it was baked with in its cache slot,
# so a slot can be compared with, or restored into, the current scene.
SETTINGS_FILE = ".fumaris_settings.json"
# Bookkeeping and identity: never restored from a slot.
_SNAPSHOT_SKIP = {
    "cache_slot", "cache_id", "output_dir", "output_prefix", "baked_frames",
    "bake_elapsed", "bake_peak_vram_mb", "bake_peak_ram_mb", "simulation_state",
}


def _rna_values(props):
    """Plain values of a property group's editable settings (no pointers)."""
    values = {}
    for prop in props.bl_rna.properties:
        name = prop.identifier
        if name in ("rna_type", "name") or name in _SNAPSHOT_SKIP or prop.is_readonly:
            continue
        if prop.type in {"POINTER", "COLLECTION"}:
            continue
        if name.startswith(("show_", "compare_")):
            continue
        try:
            value = getattr(props, name)
        except AttributeError:
            continue
        if prop.type in {"FLOAT", "INT", "BOOLEAN"} and getattr(prop, "array_length", 0):
            value = list(value)
        elif prop.type == "ENUM" and getattr(prop, "is_enum_flag", False):
            value = sorted(value)
        values[name] = value
    return values


def write_settings_snapshot(directory, domain, participants=()):
    """Write the domain's and its participants' settings for this bake."""
    data = settings_snapshot_data(domain, participants)
    os.makedirs(directory, exist_ok=True)
    path = os.path.join(directory, SETTINGS_FILE)
    temporary = path + ".tmp"
    with open(temporary, "w", encoding="utf-8") as stream:
        json.dump(data, stream, indent=1, sort_keys=True)
    os.replace(temporary, path)
    return path


def settings_snapshot_data(domain, participants=()):
    """The domain's and its participants' settings, as a settings snapshot holds them."""
    from . import detail_stack
    data = {
        "version": 1,
        "written": time.strftime("%Y-%m-%d %H:%M:%S"),
        "domain": domain.name,
        "settings": _rna_values(domain.fumaris),
        "detail_layers": detail_stack.to_list(domain.fumaris),
        "participants": {},
    }
    for participant in participants:
        obj = participant.get("object") if isinstance(participant, dict) else participant
        if obj is None or obj == domain or not hasattr(obj, "fumaris"):
            continue
        data["participants"][obj.name] = _rna_values(obj.fumaris)
    return data


def read_settings_snapshot(directory):
    path = os.path.join(directory, SETTINGS_FILE)
    if not os.path.isfile(path):
        return None
    with open(path, encoding="utf-8") as stream:
        return json.load(stream)


def _apply_values(props, values):
    changed = 0
    retry = []
    for name, value in values.items():
        if name in _SNAPSHOT_SKIP or not hasattr(props, name):
            continue
        result = _apply_value(props, name, value)
        if result is None:
            retry.append((name, value))
        else:
            changed += result
    # Choices that depend on another setting (Velocity Resolution offers Full
    # only with Full-Resolution Channels) are accepted once it is applied.
    for name, value in retry:
        changed += _apply_value(props, name, value) or 0
    return changed


def _apply_value(props, name, value):
    """1 if changed, 0 if already equal, None if rejected."""
    try:
        current = getattr(props, name)
        if hasattr(current, "__len__") and not isinstance(current, str) and not isinstance(value, str):
            if isinstance(current, set):
                value = set(value)
            elif list(current) == list(value):
                return 0
        elif current == value:
            return 0
        setattr(props, name, value)
        return 1
    except (TypeError, ValueError, AttributeError):
        return None


def apply_settings_snapshot(domain, data, objects):
    """Restore a snapshot; participants are matched by object name.

    Returns (changed settings, participants restored, participants missing).
    """
    from . import detail_stack
    changed = _apply_values(domain.fumaris, data.get("settings", {}))
    layers = data.get("detail_layers", [])
    if detail_stack.to_list(domain.fumaris) != layers:
        detail_stack.from_list(domain.fumaris, layers)
        changed += 1
    restored, missing = 0, []
    for name, values in data.get("participants", {}).items():
        obj = objects.get(name)
        if obj is None or not hasattr(obj, "fumaris"):
            missing.append(name)
            continue
        changed += _apply_values(obj.fumaris, values)
        restored += 1
    return changed, restored, missing
