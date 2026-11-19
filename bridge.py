import os
import queue
import sys
import subprocess
import threading
import time
from collections import deque

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
    CACHE_FLUSH,
    CANCEL,
    CANCELLED,
    FAILED,
    FRAME,
    PREVIEW_CACHE_EXPORT,
    PREVIEW_RENDER,
    SESSION_BEGIN,
    SESSION_COMPLETE,
    SESSION_END,
    SESSION_RESET,
    read_message,
    pack_preview_payload,
    reset_packed_display_lut,
    split_scene_depth,
    write_message,
)
from .utils import process_environment, get_addon_preferences
from .devices import device_environment, discover_devices
from .memory_auto import update_readings as update_memory_readings

_TRACE = bool(os.environ.get("FUMARIS_WORKER_TRACE"))

# Bridges closed without waiting (a stopped preview) whose processes may
# still be exiting: one holding 20 GB of GPU memory and gigabytes of pinned
# host memory took a second or two, which Stop spent blocking Blender. A new
# bridge waits for them on its own thread before it reads free GPU memory
# (Auto Simulation Memory) or starts.
_RETIRING = []
_RETIRING_LOCK = threading.Lock()
_RETIRING_WAIT_SECONDS = 20.0
# A cancelled bridge still running after this is killed.
_KILL_AFTER_SECONDS = 2.0
# After the last frame the bridge still writes its queued VDB files: on a hard
# disk at high resolution that took minutes (beta 2 killed it after 30 s,
# losing the last frames and leaving the bake waiting forever). The worker now
# waits as long as the bridge shows any sign of work: a reply, a log line, CPU
# time, or a file in its output folder changing. Only a bridge showing none of
# them for _HUNG_SECONDS is taken as hung (reported as failed, then killed).
_READER_POLL_SECONDS = 0.25
_ACTIVITY_CHECK_SECONDS = 10.0
_HUNG_SECONDS = 600.0
# An exited bridge's output ends at once; a reader still blocked after this
# (an inherited pipe) is given up on.
_EXITED_GRACE_SECONDS = 5.0


def _wait_for_retiring(own):
    deadline = time.monotonic() + _RETIRING_WAIT_SECONDS
    with _RETIRING_LOCK:
        workers = [worker for worker in _RETIRING if worker is not own]
    for worker in workers:
        worker._thread.join(timeout=max(0.0, deadline - time.monotonic()))
    with _RETIRING_LOCK:
        _RETIRING[:] = [worker for worker in _RETIRING if worker._thread.is_alive()]


class BridgeWorker:
    def __init__(self, executable, session, *, keep_alive=False):
        # Read Blender RNA on the main thread, before the worker is started.
        prefs = get_addon_preferences()
        self._gpu_uuid = getattr(prefs, "gpu_device_uuid", "")
        self._environment = device_environment(
            process_environment(executable), self._gpu_uuid)
        self.responses = queue.Queue()
        self._requests = queue.Queue(maxsize=2)
        self._executable = executable
        self._session = session
        self._keep_alive = keep_alive
        self._process = None
        self._process_lock = threading.Lock()
        self._stderr = deque(maxlen=200)
        self._pending_stderr = deque(maxlen=200)
        self._stderr_lock = threading.Lock()
        self._stderr_thread = None
        self._cancel_requested = threading.Event()
        self._cancel_at = None
        self._failed = threading.Event()
        self._failure_lock = threading.Lock()
        self._closing = threading.Event()
        # Signs of life while waiting for the last frames' files.
        self._messages_received = 0
        self._stderr_lines = 0
        self._reader_thread = None
        self._thread = threading.Thread(target=self._run, daemon=True)

    def start(self):
        reset_packed_display_lut()
        self._thread.start()

    def send_frame(self, frame):
        self._requests.put_nowait((FRAME, frame.header, frame.payload))

    def send_preview(self, preview):
        payload = bytearray()
        preview = pack_preview_payload(preview, payload)
        self._requests.put_nowait((PREVIEW_RENDER, preview, bytes(payload)))

    def send_cache_flush(self):
        """Acknowledged (CACHE_FLUSHED) once every queued VDB file is written."""
        self._requests.put_nowait((CACHE_FLUSH, {}, b""))

    def send_preview_cache_export(self, header):
        """Write the preview cache's frames as VDB files (PREVIEW_CACHE_EXPORTED)."""
        self._requests.put_nowait((PREVIEW_CACHE_EXPORT, header, b""))

    def end_session(self):
        self._requests.put_nowait((SESSION_END, {}, b""))

    def reset_session(self, session):
        self._session = session
        self._requests.put_nowait((SESSION_RESET, session, b""))

    def cancel(self):
        if self._cancel_at is None:
            self._cancel_at = time.monotonic()
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

    def close(self, timeout=0.5, wait=True):
        """Stop the bridge. wait=False returns at once: the process is told
        to stop and exits on the worker thread (killed if it lingers), and
        the next bridge waits for it before starting."""
        self.cancel()
        if not wait:
            if self._thread.is_alive():
                with _RETIRING_LOCK:
                    if self not in _RETIRING:
                        _RETIRING.append(self)
            return
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
        with self._stderr_lock:
            return "".join(self._stderr).strip()

    def poll_stderr(self):
        with self._stderr_lock:
            lines = list(self._pending_stderr)
            self._pending_stderr.clear()
        return lines

    def memory_bytes(self):
        process = self._process
        return process_memory_bytes(process.pid) if process else 0

    def _run(self):
        """Writer thread: starts the bridge and writes every request as soon as
        it is queued. Replies come back on their own thread (_read_responses),
        so a frame can be sent while the one before it is still running: the
        bridge overlaps the next frame's CPU work with a frame's GPU time."""
        try:
            _wait_for_retiring(self)
            if self._gpu_uuid:
                available = discover_devices(self._executable, self._environment)
                update_memory_readings(available)
                if not any(device["uuid"] == self._gpu_uuid for device in available):
                    raise RuntimeError(
                        "Selected Fumaris GPU is unavailable; refresh GPUs or choose Automatic in preferences")
            with self._process_lock:
                if self._cancel_requested.is_set():
                    return
                self._process = subprocess.Popen(
                    [self._executable],
                    stdin=subprocess.PIPE,
                    stdout=subprocess.PIPE,
                    stderr=subprocess.PIPE,
                    env=self._environment,
                    **_popen_options(),
                )
            self._stderr_thread = threading.Thread(target=self._drain_stderr, daemon=True)
            self._stderr_thread.start()
            self._reader_thread = threading.Thread(target=self._read_responses, daemon=True)
            self._reader_thread.start()

            write_message(self._process.stdin, SESSION_BEGIN, self._session)

            while not self._failed.is_set():
                try:
                    message_type, data, payload = self._requests.get(timeout=0.1)
                except queue.Empty:
                    if self._process.poll() is not None:
                        raise RuntimeError("Bridge exited while waiting for the next frame")
                    if self._cancel_at is not None and time.monotonic() - self._cancel_at > _KILL_AFTER_SECONDS:
                        self._process.kill()
                    continue
                if self._process.poll() is not None:
                    raise RuntimeError("Bridge exited before the next frame was sent")
                if message_type in {CANCEL, SESSION_END} and not self._keep_alive:
                    self._closing.set()
                write_message(self._process.stdin, message_type, data, payload)
                if _TRACE:
                    print(f"WORKER sent {message_type} frame={data.get('frame') if isinstance(data, dict) else None}", file=sys.stderr, flush=True)
                if (
                    message_type == FRAME
                    and int(data["frame"]) >= self._session["end_frame"]
                    and not self._keep_alive
                ):
                    # The bridge answers the frames first, then the session end.
                    self._closing.set()
                    write_message(self._process.stdin, SESSION_END, {})
                    break
                if message_type in {CANCEL, SESSION_END} and not self._keep_alive:
                    break
            # Every request is written: the replies still to come end the
            # reader (the session's completion, or the stream closing).
            self._wait_for_reader()
        except Exception as error:
            self._report_failure(error)
        finally:
            self._closing.set()
            self._close_process()

    def _wait_for_reader(self):
        """Wait for the session's last replies: as long as the bridge lives
        and works (writing the last frames can take minutes), ending early
        only when it failed, was cancelled (killed after _KILL_AFTER_SECONDS)
        or shows no sign of work for _HUNG_SECONDS."""
        reader = self._reader_thread
        if reader is None:
            return
        process = self._process
        last_activity = self._activity()
        active_at = next_check = time.monotonic()
        exited_at = None
        while reader.is_alive():
            reader.join(timeout=_READER_POLL_SECONDS)
            if not reader.is_alive() or self._failed.is_set():
                return
            now = time.monotonic()
            if process.poll() is not None:
                # Its output ends with it: the reader sees the end at once.
                exited_at = exited_at if exited_at is not None else now
                if now - exited_at > _EXITED_GRACE_SECONDS:
                    self._report_failure(RuntimeError(_ENDED_EARLY_MESSAGE))
                    return
                continue
            if self._cancel_at is not None:
                if now - self._cancel_at > _KILL_AFTER_SECONDS:
                    try:
                        process.kill()
                    except OSError:
                        pass
                continue
            if now >= next_check:
                next_check = now + _ACTIVITY_CHECK_SECONDS
                activity = self._activity()
                if activity != last_activity:
                    last_activity, active_at = activity, now
                elif now - active_at > _HUNG_SECONDS:
                    minutes = max(1, int(round(_HUNG_SECONDS / 60.0)))
                    self._report_failure(RuntimeError(
                        "The simulation process stopped responding while writing the last frames "
                        f"(no progress for {minutes} minute{'s' if minutes != 1 else ''})"))
                    return

    def _activity(self):
        """What changes while the bridge works: replies, log lines, its CPU
        time and its output folder's files (None where unknown)."""
        process = self._process
        return (
            self._messages_received,
            self._stderr_lines,
            process_cpu_seconds(process.pid) if process is not None else None,
            _folder_activity(self._session.get("output_directory"), self._session.get("output_prefix")),
        )

    def _read_responses(self):
        """Reader thread: every reply, in order, to self.responses."""
        try:
            while True:
                response = read_message(self._process.stdout)
                self._messages_received += 1
                if _TRACE:
                    print(f"WORKER got {response[0]} frame={response[1].get('frame') if isinstance(response[1], dict) else None} payload={len(response[2])}", file=sys.stderr, flush=True)
                if response[0] == FAILED:
                    raise RuntimeError(response[1].get("message", "Bridge failed"))
                self.responses.put(response)
                if response[0] in {SESSION_COMPLETE, CANCELLED} and not self._keep_alive:
                    return
        except Exception as error:
            if self._closing.is_set() and isinstance(error, (EOFError, OSError, ValueError)):
                # The stream closed as the session ended without its last
                # reply (SESSION_COMPLETE or CANCELLED): the bridge was
                # stopped (CANCELLED) or died (FAILED). A job always hears.
                error = RuntimeError(_ENDED_EARLY_MESSAGE)
            self._report_failure(error)

    def _report_failure(self, error):
        """One CANCELLED or FAILED reply, whichever thread hit the problem first."""
        with self._failure_lock:
            if self._failed.is_set():
                return
            self._failed.set()
        message_type = (
            CANCELLED if self._cancel_requested.is_set() else FAILED
        )
        message = "Bake stopped"
        if message_type == FAILED:
            message = self._failure_message(error)
        self.responses.put((
            message_type,
            {
                "message": message,
                "stderr": self.stderr(),
            },
            b"",
        ))

    def _receive(self):
        response = read_message(self._process.stdout)
        if response[0] == FAILED:
            raise RuntimeError(response[1].get("message", "Bridge failed"))
        self.responses.put(response)

    def _failure_message(self, error):
        # A bridge that never started or already exited may have hit a loader
        # problem: add actionable Linux diagnostics. Fail-open by design.
        message = str(error)
        try:
            process = self._process
            if process is not None and process.poll() is None:
                return message
            if self._stderr_thread:
                self._stderr_thread.join(timeout=0.5)
            from .linux_runtime import explain_launch_failure
            return explain_launch_failure(
                self._executable, self._environment, message, self.stderr())
        except Exception:
            return message

    def _drain_stderr(self):
        while True:
            chunk = self._process.stderr.readline()
            if not chunk:
                return
            line = chunk.decode("utf-8", errors="replace")
            self._stderr_lines += 1
            with self._stderr_lock:
                self._stderr.append(line)
                self._pending_stderr.append(line)

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
        if self._stderr_thread:
            self._stderr_thread.join(timeout=1.0)
        if process.stdout:
            process.stdout.close()
        if process.stderr and (not self._stderr_thread or not self._stderr_thread.is_alive()):
            process.stderr.close()


_ENDED_EARLY_MESSAGE = "The simulation process ended before the last frames were written"


def _folder_activity(directory, prefix):
    """(files, bytes, newest change) of the bridge's files in its output
    folder, or None: a writer still writing changes it."""
    if not directory or not prefix:
        return None
    count = size = newest = 0
    try:
        with os.scandir(directory) as entries:
            for entry in entries:
                if not entry.name.startswith(prefix):
                    continue
                try:
                    info = entry.stat(follow_symlinks=False)
                except OSError:
                    continue
                count += 1
                size += info.st_size
                newest = max(newest, info.st_mtime_ns)
    except OSError:
        return None
    return count, size, newest


def process_cpu_seconds(pid):
    """A process's CPU time so far (user and system), or None where unknown."""
    try:
        if os.name == "nt":
            return _windows_process_cpu_seconds(pid)
        with open(f"/proc/{int(pid)}/stat", encoding="ascii") as stream:
            fields = stream.read().rsplit(")", 1)[1].split()
        # utime and stime: fields 14 and 15 of proc(5), after the name.
        return (int(fields[11]) + int(fields[12])) / os.sysconf("SC_CLK_TCK")
    except (OSError, ValueError, IndexError, AttributeError):
        return None   # macOS has no /proc: the other signs of work count


def _windows_process_cpu_seconds(pid):
    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel32.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
    kernel32.OpenProcess.restype = wintypes.HANDLE
    kernel32.CloseHandle.argtypes = [wintypes.HANDLE]
    kernel32.CloseHandle.restype = wintypes.BOOL
    kernel32.GetProcessTimes.argtypes = [wintypes.HANDLE] + [ctypes.POINTER(wintypes.FILETIME)] * 4
    kernel32.GetProcessTimes.restype = wintypes.BOOL
    handle = kernel32.OpenProcess(0x1000, False, int(pid))   # PROCESS_QUERY_LIMITED_INFORMATION
    if not handle:
        return None
    try:
        times = [wintypes.FILETIME() for _ in range(4)]
        if not kernel32.GetProcessTimes(handle, *(ctypes.byref(t) for t in times)):
            return None
        # Kernel and user time, in 100 ns units.
        return sum((t.dwHighDateTime << 32 | t.dwLowDateTime) for t in times[2:]) / 1e7
    finally:
        kernel32.CloseHandle(handle)


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
