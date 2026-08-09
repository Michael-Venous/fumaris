import ctypes
import os
import shutil

LOCK_FILE = ".fumaris.lock"
_OBSOLETE_FILES = ("fumaris_cache.json", "plume_forge_cache.json")
_OBSOLETE_DIRECTORIES = (
    ".fumaris_preview_cache",
    ".plume_forge_preview_cache",
)


def delete_obsolete_cache_state(directory):
    for name in _OBSOLETE_FILES:
        try:
            os.remove(os.path.join(directory, name))
        except OSError:
            pass
    for name in _OBSOLETE_DIRECTORIES:
        shutil.rmtree(os.path.join(directory, name), ignore_errors=True)


class CacheLock:
    def __init__(self, directory):
        self.directory = os.path.normpath(directory)
        self.path = os.path.join(self.directory, LOCK_FILE)
        self._owned = False

    def acquire(self):
        os.makedirs(self.directory, exist_ok=True)
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
                return self.acquire()
            raise RuntimeError(
                f"Fumaris cache is already in use: {self.directory}"
            )
        with os.fdopen(descriptor, "w", encoding="utf-8") as stream:
            stream.write(str(os.getpid()))
        self._owned = True
        return self

    def release(self):
        if not self._owned:
            return
        try:
            os.remove(self.path)
        except FileNotFoundError:
            pass
        self._owned = False

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
    lock_pid = _lock_owner(path)
    if lock_pid is None or not _pid_is_alive(lock_pid) or lock_pid == owner_pid:
        try:
            os.remove(path)
        except FileNotFoundError:
            pass
        return True
    return False


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
