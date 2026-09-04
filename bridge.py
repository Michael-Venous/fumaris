import os
import queue
import subprocess
import threading

if os.name == "nt":
    import ctypes
    from ctypes import wintypes

    class _ProcessMemoryCounters(ctypes.Structure):
        _fields_ = [
            ("cb", wintypes.DWORD),
            ("PageFaultCount", wintypes.DWORD),
            ("PeakWorkingSetSize", ctypes.c_size_t),
            ("WorkingSetSize", ctypes.c_size_t),
            ("QuotaPeakPagedPoolUsage", ctypes.c_size_t),
            ("QuotaPagedPoolUsage", ctypes.c_size_t),
            ("QuotaPeakNonPagedPoolUsage", ctypes.c_size_t),
            ("QuotaNonPagedPoolUsage", ctypes.c_size_t),
            ("PagefileUsage", ctypes.c_size_t),
            ("PeakPagefileUsage", ctypes.c_size_t),
        ]

from .protocol import (
    CANCEL,
    CANCELLED,
    FAILED,
    FRAME,
    PREVIEW_RENDER,
    SESSION_BEGIN,
    SESSION_END,
    SESSION_RESET,
    read_message,
    write_message,
)
from .utils import process_environment


class BridgeWorker:
    def __init__(self, executable, session, *, keep_alive=False):
        self.responses = queue.Queue()
        self._requests = queue.Queue(maxsize=2)
        self._executable = executable
        self._session = session
        self._keep_alive = keep_alive
        self._process = None
        self._process_lock = threading.Lock()
        self._stderr = []
        self._cancel_requested = threading.Event()
        self._thread = threading.Thread(target=self._run, daemon=True)

    def start(self):
        self._thread.start()

    def send_frame(self, frame):
        self._requests.put_nowait((FRAME, frame.header, frame.payload))

    def send_preview(self, preview):
        self._requests.put_nowait((PREVIEW_RENDER, preview, b""))

    def end_session(self):
        self._requests.put_nowait((SESSION_END, {}, b""))

    def reset_session(self, session):
        self._session = session
        self._requests.put_nowait((SESSION_RESET, session, b""))

    def cancel(self):
        self._cancel_requested.set()
        try:
            self._requests.put_nowait((CANCEL, {}, b""))
        except queue.Full:
            pass
        with self._process_lock:
            process = self._process
        if process and process.poll() is None:
            try:
                process.terminate()
            except OSError:
                pass

    def close(self, timeout=0.5):
        self.cancel()
        self._thread.join(timeout=timeout)
        with self._process_lock:
            process = self._process
        if self._thread.is_alive() and process and process.poll() is None:
            try:
                process.kill()
            except OSError:
                pass
        self._thread.join(timeout=timeout)

    def poll(self):
        responses = []
        while True:
            try:
                responses.append(self.responses.get_nowait())
            except queue.Empty:
                return responses

    def stderr(self):
        return "".join(self._stderr).strip()

    def memory_bytes(self):
        process = self._process
        return process_memory_bytes(process.pid) if process else 0

    def _run(self):
        try:
            with self._process_lock:
                if self._cancel_requested.is_set():
                    return
                self._process = subprocess.Popen(
                    [self._executable],
                    stdin=subprocess.PIPE,
                    stdout=subprocess.PIPE,
                    stderr=subprocess.PIPE,
                    env=process_environment(),
                    **_popen_options(),
                )
            threading.Thread(target=self._drain_stderr, daemon=True).start()

            self._receive()
            write_message(self._process.stdin, SESSION_BEGIN, self._session)
            self._receive()

            while True:
                try:
                    message_type, data, payload = self._requests.get(timeout=0.1)
                except queue.Empty:
                    if self._process.poll() is not None:
                        raise RuntimeError("Bridge exited while waiting for the next frame")
                    continue
                if self._process.poll() is not None:
                    raise RuntimeError("Bridge exited before the next frame was sent")
                write_message(self._process.stdin, message_type, data, payload)
                self._receive()
                if (
                    message_type == FRAME
                    and int(data["frame"]) >= self._session["end_frame"]
                    and not self._keep_alive
                ):
                    write_message(self._process.stdin, SESSION_END, {})
                    self._receive()
                    break
                if message_type in {SESSION_BEGIN, SESSION_RESET}:
                    continue
                if message_type in {CANCEL, SESSION_END} and not self._keep_alive:
                    break
        except Exception as error:
            message_type = (
                CANCELLED if self._cancel_requested.is_set() else FAILED
            )
            self.responses.put((
                message_type,
                {
                    "message": (
                        "Bake stopped"
                        if message_type == CANCELLED
                        else str(error)
                    ),
                    "stderr": self.stderr(),
                },
                b"",
            ))
        finally:
            self._close_process()

    def _receive(self):
        response = read_message(self._process.stdout)
        if response[0] == FAILED:
            raise RuntimeError(response[1].get("message", "Bridge failed"))
        self.responses.put(response)

    def _drain_stderr(self):
        while True:
            chunk = self._process.stderr.readline()
            if not chunk:
                return
            self._stderr.append(chunk.decode("utf-8", errors="replace"))
            if len(self._stderr) > 200:
                del self._stderr[:50]

    def _close_process(self):
        process = self._process
        if not process:
            return
        if process.stdin:
            try:
                process.stdin.close()
            except (BrokenPipeError, OSError, ValueError):
                pass
        try:
            process.wait(timeout=2.0)
        except subprocess.TimeoutExpired:
            process.kill()
            process.wait()


def _popen_options():
    if os.name == "nt":
        return {"creationflags": subprocess.CREATE_NO_WINDOW}
    return {"start_new_session": True}


def process_memory_bytes(pid):
    """Return a process working set without requiring third-party packages."""
    if os.name == "nt":
        return _windows_process_memory_bytes(pid)
    try:
        with open(f"/proc/{int(pid)}/statm", encoding="ascii") as status:
            resident_pages = int(status.read().split()[1])
        return resident_pages * os.sysconf("SC_PAGE_SIZE")
    except (OSError, ValueError, IndexError):
        return 0


def _windows_process_memory_bytes(pid):
    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    psapi = ctypes.WinDLL("psapi", use_last_error=True)
    kernel32.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
    kernel32.OpenProcess.restype = wintypes.HANDLE
    kernel32.CloseHandle.argtypes = [wintypes.HANDLE]
    kernel32.CloseHandle.restype = wintypes.BOOL
    psapi.GetProcessMemoryInfo.argtypes = [
        wintypes.HANDLE,
        ctypes.POINTER(_ProcessMemoryCounters),
        wintypes.DWORD,
    ]
    psapi.GetProcessMemoryInfo.restype = wintypes.BOOL
    handle = kernel32.OpenProcess(0x1000 | 0x0010, False, int(pid))
    if not handle:
        return 0
    try:
        counters = _ProcessMemoryCounters()
        counters.cb = ctypes.sizeof(counters)
        if not psapi.GetProcessMemoryInfo(
            handle,
            ctypes.byref(counters),
            counters.cb,
        ):
            return 0
        return int(counters.WorkingSetSize)
    finally:
        kernel32.CloseHandle(handle)
