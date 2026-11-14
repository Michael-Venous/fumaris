"""Bake in Background: several domains bake at once, each in its own Blender.

A background bake is a second Blender process (background_runner.py) that
loads a snapshot of the scene and bakes one domain with its own bridge, on
the GPU its worker was given (Preferences > Baking). The one foreground job
(runtime.active_job) is untouched: a live preview or a normal Bake runs
beside them.

This is the main process's side: the queue and the workers, the files a job
reports through, and what happens in the scene when a bake ends.

    <temporary files>/fumaris_bake_jobs/<batch>/   one per click (Bake All: one for all its domains)
        owner.json     the Blender that made it (stale folders are removed)
        scene.blend    the snapshot
        <n>/           one job
            job.json       what to bake, written when its worker starts
            status.json    written by the bake every frame
            cancel         Stop
            log.txt        the bake's console
"""
import atexit
import json
import os
import shutil
import subprocess
import tempfile
import time
from collections import deque
from types import SimpleNamespace

import bpy
from bpy.app.handlers import persistent
from bpy.props import BoolProperty, EnumProperty, StringProperty
from bpy.types import Operator

from . import diagnostics, runtime
from .background_runner import CANCEL_FILE, JOB_FILE, STATUS_FILE, write_json
from .bridge import _popen_options
from .cache import (
    _pid_is_alive,
    cache_lock,
    is_protected,
    recover_cache_lock,
    require_unprotected,
)
from .devices import device_environment
from .diagnostics import console_log
from .exporters import bridge_executable, build_session
from .importers import VOLUME_MARKER, baked_frame_count, delete_generated_data, delete_temporary_writes
from .preview import clear_preview
from .runtime import active_job, active_mode, background_job
from .utils import (
    addon_directory,
    cache_slot_directory,
    get_addon_preferences,
    output_directory_for_object,
    peek_output_directory_for_object,
    runtime_validation_error,
)

JOBS_DIRECTORY = "fumaris_bake_jobs"
LOGS_DIRECTORY = "fumaris_bake_logs"
OWNER_FILE = "owner.json"
SNAPSHOT_FILE = "scene.blend"
LOG_FILE = "log.txt"
RUNNER = "background_runner.py"
# Logs of failed bakes kept in the temporary folder.
KEPT_LOGS = 20

POLL_SECONDS = 1.0
# After Stop, a bake that writes no status for this long is ended: it cannot
# see the request (its Blender is busy inside one frame).
STOP_SECONDS = 30.0
# A bake that reported its end has this long to close its Blender.
EXIT_SECONDS = 10.0
# Quitting Blender waits this long for bakes to stop; a bake still inside a
# frame finishes it and exits on its own.
QUIT_SECONDS = 2.0
# The scene's side of a finished bake waits this long for someone else's
# modal operator (a transform, orbiting the view) to end.
MODAL_SECONDS = 10.0

_FINAL = {"finished", "stopped", "failed"}

_jobs = []                      # queued, running and ended-but-not-imported, oldest first
_batches = []
messages = deque(maxlen=50)     # (level, text) of what was reported, newest last


class _Batch:
    """One snapshot and the jobs that bake from it."""

    def __init__(self):
        root = jobs_root()
        os.makedirs(root, exist_ok=True)
        self.directory = tempfile.mkdtemp(prefix="bake_", dir=root)
        self.snapshot = os.path.join(self.directory, SNAPSHOT_FILE)
        self.blend_directory = os.path.dirname(bpy.data.filepath)
        self.jobs = []
        write_json(os.path.join(self.directory, OWNER_FILE), {"pid": os.getpid()})

    def release(self, job):
        """A job is done with its files; the snapshot goes with the last one."""
        shutil.rmtree(job.path, ignore_errors=True)
        if job in self.jobs:
            self.jobs.remove(job)
        if not self.jobs:
            shutil.rmtree(self.directory, ignore_errors=True)
            if self in _batches:
                _batches.remove(self)


class Job:
    """One domain's background bake. Draw code reads these fields only."""

    def __init__(self, context, domain, directory, prefix, batch):
        self.uid = int(domain.session_uid)      # finds the domain after a rename
        self.domain_name = domain.name
        self.scene = context.scene.name
        self.view_layer = context.view_layer.name
        self.directory = directory
        self.prefix = prefix
        self.slot = domain.fumaris.cache_slot
        self.resolution = int(domain.fumaris.resolution)
        self.batch = batch
        self.path = os.path.join(batch.directory, str(len(batch.jobs)))
        # queued, starting, running, stopping, ended (waiting for the scene)
        self.state = "queued"
        self.result = ""            # finished, stopped or failed, once ended
        self.error = ""
        self.waiting = ""           # why an ended bake is not in the scene yet
        self.status = {}
        self.process = None
        self.log = None
        self.worker = -1
        self.gpu = ""
        self.heard = 0.0
        self.stop_requested = 0.0
        self.ended = 0.0
        self.detached = False       # its scene was closed: nothing to finish there
        batch.jobs.append(self)
        os.makedirs(self.path)

    @property
    def active(self):
        return self.state in {"starting", "running", "stopping"}


def jobs_root():
    """Blender's Temporary Files folder (the system's when unset)."""
    base = bpy.context.preferences.filepaths.temporary_directory
    if not base or not os.path.isabs(base) or not os.path.isdir(base):
        base = tempfile.gettempdir()
    return os.path.join(base, JOBS_DIRECTORY)


def jobs():
    return list(_jobs)


def worker_count():
    prefs = get_addon_preferences()
    return max(1, min(8, int(getattr(prefs, "bake_workers", 1) or 1)))


def worker_gpus():
    """UUIDs of the GPUs the workers take in turn; "" is the bridge's own choice."""
    prefs = get_addon_preferences()
    ticked = [gpu.uuid for gpu in getattr(prefs, "bake_gpus", ()) if gpu.use and gpu.uuid]
    return ticked or [getattr(prefs, "gpu_device_uuid", "") or ""]


def _scripts_allowed():
    """Auto Run Python Scripts as this session has it: drivers must evaluate
    in the bake as they do here, and no script runs that would not run here."""
    override = getattr(bpy.app, "autoexec_override", None)
    if override is not None:
        return bool(override)
    return bool(bpy.context.preferences.filepaths.use_scripts_auto_execute)


# --- Starting ------------------------------------------------------------------

def _check(context, domain):
    """What FUMARIS_OT_bake refuses when it starts, checked at the click.
    Returns (version folder, prefix)."""
    if background_job(domain) is not None:
        raise RuntimeError(f"'{domain.name}' is already baking in the background")
    foreground = active_job()
    if foreground is not None and getattr(foreground, "_domain_name", None) == domain.name:
        raise RuntimeError(f"Stop the {'bake' if active_mode() == 'baking' else 'preview'} of '{domain.name}' first")
    error = runtime_validation_error()
    if error:
        raise RuntimeError(error)
    if bridge_executable() is None:
        raise RuntimeError("Bridge executable not found")
    session, _participants = build_session(context, domain=domain, write_vdb=True, preview_enabled=False)
    prefix = session["output_prefix"]
    if not all(character.isalnum() or character in "._-" for character in prefix):
        raise RuntimeError("Output prefix may contain only letters, numbers, '.', '_', and '-'")
    directory = output_directory_for_object(domain)
    if any(os.path.normpath(job.directory) == os.path.normpath(directory) for job in _jobs):
        raise RuntimeError(f"Fumaris cache is already in use: {directory}")
    require_unprotected(directory, "bake over it")
    # Refuse a version another Blender is writing before the scene is saved.
    os.makedirs(directory, exist_ok=True)
    with cache_lock(directory):
        pass
    return directory, prefix


def _clear(domain, directory, prefix):
    """Empty the version, as a bake does before its first frame. The lock
    refuses a version another Blender is writing."""
    os.makedirs(directory, exist_ok=True)
    with cache_lock(directory):
        clear_preview(domain)
        delete_generated_data(directory, prefix)
    props = domain.fumaris
    props.simulation_state = "idle"
    props.baked_frames = 0
    props.bake_elapsed = 0.0
    props.bake_peak_vram_mb = 0.0
    props.bake_peak_ram_mb = 0.0


def start(context, domains):
    """Queue a background bake of each domain's current version, from one
    snapshot of the scene as it is now. Returns (jobs, [(domain name, why not)])."""
    errors = []
    checked = []
    for domain in dict.fromkeys(domains):
        try:
            checked.append((domain, *_check(context, domain)))
        except (RuntimeError, OSError) as error:
            errors.append((domain.name, str(error)))
    if not checked:
        return [], errors

    cleanup_stale()
    batch = _Batch()
    try:
        # A copy: the scene keeps its file name, its unsaved state and its
        # own relative paths; later edits do not reach the bake.
        bpy.ops.wm.save_as_mainfile(filepath=batch.snapshot, copy=True, compress=False,
                                    relative_remap=True, check_existing=False)
    except Exception as error:
        shutil.rmtree(batch.directory, ignore_errors=True)
        raise RuntimeError(f"Could not save the scene for the background bake: {error}") from error
    _batches.append(batch)

    started = []
    for domain, directory, prefix in checked:
        try:
            _clear(domain, directory, prefix)
        except (RuntimeError, OSError) as error:
            errors.append((domain.name, str(error)))
            continue
        job = Job(context, domain, directory, prefix, batch)
        diagnostics.begin(domain, "Background Bake")
        diagnostics.set_status(job.uid, "Queued")
        runtime._BACKGROUND_JOBS[job.uid] = job
        _jobs.append(job)
        started.append(job)
    if not batch.jobs:
        shutil.rmtree(batch.directory, ignore_errors=True)
        _batches.remove(batch)
    from .compare import forget_scan
    forget_scan()
    _launch_queued()
    _ensure_timer()
    return started, errors


def _launch_queued():
    """Give free workers the oldest queued jobs."""
    busy = {job.worker for job in _jobs if job.active}
    for job in _jobs:
        if job.state != "queued":
            continue
        worker = next((index for index in range(worker_count()) if index not in busy), None)
        if worker is None:
            return
        busy.add(worker)
        _launch(job, worker)


def _launch(job, worker):
    gpus = worker_gpus()
    job.worker = worker
    job.gpu = gpus[worker % len(gpus)]
    prefs = get_addon_preferences()
    custom_bridge = getattr(prefs, "executable_path", "") or ""
    try:
        write_json(os.path.join(job.path, JOB_FILE), {
            "domain": job.domain_name,
            "scene": job.scene,
            "view_layer": job.view_layer,
            "output_directory": job.directory,
            "blend_directory": job.batch.blend_directory,
            "parent_pid": os.getpid(),
            # The preferences a bake reads (the bake's Blender has none).
            "preferences": {
                "executable_path": bpy.path.abspath(custom_bridge) if custom_bridge else "",
                "gpu_device_uuid": job.gpu,
                "gpu_device_name": "",
                "default_output_dir": "",
                "preview_cache_memory_gb": float(getattr(prefs, "preview_cache_memory_gb", 0.0) or 0.0),
            },
        })
        environment = os.environ.copy()
        if job.gpu:
            environment = device_environment(environment, job.gpu)
        command = [
            bpy.app.binary_path, "--background", "--factory-startup", "-noaudio",
            "--enable-autoexec" if _scripts_allowed() else "--disable-autoexec",
            job.batch.snapshot,
            "--python", os.path.join(addon_directory(), RUNNER),
            "--", job.path,
        ]
        job.log = open(os.path.join(job.path, LOG_FILE), "wb")
        job.process = subprocess.Popen(command, stdin=subprocess.DEVNULL, stdout=job.log,
                                       stderr=subprocess.STDOUT, env=environment, **_popen_options())
    except Exception as error:
        _end(job, "failed", f"Could not start the background bake: {error}")
        return
    job.state = "starting"
    job.heard = time.monotonic()
    diagnostics.set_status(job.uid, "Initializing")
    console_log(f"Fumaris background bake of {job.domain_name} started (worker {worker + 1}"
                + (f", GPU {job.gpu}" if job.gpu else "") + f"): {job.path}")


# --- Following -----------------------------------------------------------------

def _ensure_timer():
    if bpy.app.background or bpy.app.timers.is_registered(_tick):
        return
    bpy.app.timers.register(_tick, first_interval=POLL_SECONDS, persistent=True)


def _tick():
    try:
        poll()
    except Exception as error:
        console_log(f"Fumaris background bakes: {error}")
    return POLL_SECONDS if _jobs else None


def poll():
    """About once a second while there are jobs: read what each bake wrote,
    finish the ended ones and start queued ones. No draw code reads disk."""
    changed = False
    for job in list(_jobs):
        if job.active:
            changed |= _follow(job)
        if job.state == "ended":
            changed |= _finish(job)
    _launch_queued()
    if changed or any(job.active for job in _jobs):
        _tag_redraw()


def _read_status(job):
    try:
        with open(os.path.join(job.path, STATUS_FILE), encoding="utf-8") as stream:
            data = json.load(stream)
        return data if isinstance(data, dict) else None
    except (OSError, ValueError):
        return None


def _follow(job):
    """Returns True when something to show changed."""
    now = time.monotonic()
    status = _read_status(job)
    changed = False
    if status is not None and status != job.status:
        job.status = status
        job.heard = now
        changed = True
        _mirror_diagnostics(job)
    code = job.process.poll()
    if code is not None:
        # Its last word may have been written since the read above.
        job.status = _read_status(job) or job.status
    reported = job.status.get("state")
    if code is not None:
        if reported in _FINAL:
            _end(job, reported, job.status.get("error", ""))
        elif job.state == "stopping":
            # Ended by force after Stop: the frames it finished stay.
            _end(job, "stopped", "", crashed=True)
        else:
            _end(job, "failed", f"The background bake ended unexpectedly (exit code {code})", crashed=True)
        return True
    if reported in _FINAL:
        # Told its end; its Blender is closing.
        if now - job.heard > EXIT_SECONDS:
            _terminate(job)
        return changed
    if job.state == "stopping":
        if now - max(job.heard, job.stop_requested) > STOP_SECONDS:
            _terminate(job)
    elif reported == "stopping":
        job.state = "stopping"
        job.stop_requested = now
    elif reported == "running" and job.state != "running":
        job.state = "running"
        changed = True
    return changed


def _mirror_diagnostics(job):
    """The bake's metrics and messages in this Blender's Diagnostics, so the
    panel shows its memory, capacity warnings and errors like a foreground bake's."""
    state = diagnostics.snapshot(job.uid)
    if state is None:
        return
    status = job.status
    diagnostics.record(job.uid, status.get("metrics", {}))
    if status.get("capacity_reached"):
        state["capacity_reached"] = True
    for line in status.get("log", ()):
        if line not in state["log"]:
            diagnostics.add_log(job.uid, line)
    if status.get("state") == "running":
        diagnostics.set_status(job.uid, "Running" if status.get("accepted") else "Initializing")


def _terminate(job):
    process = job.process
    if process is not None and process.poll() is None:
        try:
            process.terminate()
        except OSError:
            pass


def stop(job):
    """Stop: the bake ends after the frame it is on, keeping its frames. A
    second Stop ends its Blender now."""
    if job.state == "queued":
        _end(job, "stopped", "")
    elif job.state == "stopping":
        _terminate(job)
    elif job.active:
        try:
            with open(os.path.join(job.path, CANCEL_FILE), "w", encoding="utf-8"):
                pass
        except OSError:
            _terminate(job)
        job.state = "stopping"
        job.stop_requested = time.monotonic()
        diagnostics.set_status(job.uid, "Stopping")
    if job.state == "ended":
        _finish(job)
    _tag_redraw()


# --- Ending --------------------------------------------------------------------

def _end(job, result, error, *, crashed=False):
    """The bake's process is over (or never started): what is left is the
    scene's side, which _finish does when the scene can take it."""
    job.state = "ended"
    job.result = result
    job.error = error
    job.ended = time.monotonic()
    if job.log is not None:
        job.log.close()
        job.log = None
    if crashed:
        _recover_crashed(job)


def _recover_crashed(job):
    """A bake that died left its lock, maybe a half-written frame, and no
    statistics: leave the version as a failed foreground bake would."""
    from .compare import write_bake_stats, write_pinned
    try:
        recover_cache_lock(job.directory)
        if os.path.isdir(job.directory):
            with cache_lock(job.directory):
                delete_temporary_writes(job.directory, job.prefix)
    except (RuntimeError, OSError) as error:
        console_log(f"Fumaris could not recover cache for {job.domain_name}: {error}")
    if not baked_frame_count(job.directory, job.prefix):
        return
    status = job.status
    try:
        write_bake_stats(job.directory, SimpleNamespace(
            output_prefix=job.prefix,
            bake_elapsed=float(status.get("elapsed", 0.0)),
            bake_peak_vram_mb=float(status.get("peak_vram_bytes", 0)) / (1024.0 * 1024.0),
            bake_peak_ram_mb=float(status.get("peak_ram_bytes", 0)) / (1024.0 * 1024.0),
            resolution=job.resolution,
        ))
        write_pinned(job.directory, True)
        status["stats"] = True
    except OSError as error:
        console_log(f"Fumaris could not store bake statistics: {error}")


def _find_domain(job):
    for obj in bpy.data.objects:
        if obj.session_uid == job.uid:
            return obj if getattr(obj, "fumaris", None) and obj.fumaris.smoke_object_type == "domain" else None
    return None


def _window_for(domain):
    """A window showing the domain's scene: the volume is imported there."""
    for window in bpy.context.window_manager.windows:
        if window.view_layer.objects.get(domain.name) == domain:
            return window
    return None


def _import_blocker(job, window):
    """Why the volume cannot be imported right now ("" when it can)."""
    try:
        if bpy.app.is_job_running("RENDER") or bpy.app.is_job_running("OBJECT_BAKE"):
            return "when the render is done"
    except TypeError:
        pass
    with bpy.context.temp_override(window=window):
        if bpy.context.mode != "OBJECT":
            return "in Object Mode"
    if time.monotonic() - job.ended < MODAL_SECONDS:
        for other in bpy.context.window_manager.windows:
            for operator in getattr(other, "modal_operators", ()):
                if "fumaris" not in operator.bl_idname.lower():
                    return "in a moment"
    return ""


def _finish(job):
    """The scene's side of an ended bake, as FUMARIS_OT_bake leaves it: the
    volume imported and shown, the domain's state and statistics, Versions.
    While the scene cannot take the volume the job waits (tried again each
    poll). Returns True when something to show changed."""
    from .compare import forget_scan, read_bake_stats
    from .operators import _import_cache

    domain = None if job.detached else _find_domain(job)
    name = domain.name if domain is not None else job.domain_name
    frames = baked_frame_count(job.directory, job.prefix)
    foreground = active_job()
    # The version the domain shows now (not if it was switched meanwhile).
    current = (
        domain is not None
        and os.path.normpath(peek_output_directory_for_object(domain) or "") == os.path.normpath(job.directory)
        and not (foreground is not None and getattr(foreground, "_domain_name", None) == domain.name)
    )
    imported = False
    import_error = ""
    if current and frames and job.result in {"finished", "stopped"}:
        window = _window_for(domain)
        if window is not None:
            blocker = _import_blocker(job, window)
            if blocker:
                if job.waiting != blocker:
                    job.waiting = blocker
                    return True
                return False
            try:
                with bpy.context.temp_override(window=window):
                    _import_cache(bpy.context, domain, job.directory, job.prefix)
                imported = True
            except Exception as error:
                import_error = str(error)

    # The statistics file, when this bake wrote it (an earlier bake's stays
    # in the folder until then).
    stats = read_bake_stats(job.directory) if job.status.get("stats") else {}
    if current:
        props = domain.fumaris
        if job.result == "finished" and not import_error:
            props.simulation_state = "baked"
        elif job.result == "stopped" or frames or stats:
            props.simulation_state = "stopped"
        props.baked_frames = frames
        props.bake_elapsed = float(stats.get("seconds", job.status.get("elapsed", 0.0)))
        props.bake_peak_vram_mb = float(stats.get("peak_vram_mb", 0.0))
        props.bake_peak_ram_mb = float(stats.get("peak_ram_mb", 0.0))
    forget_scan()

    if job.result == "failed" or import_error:
        log = _keep_log(job)
        error = import_error or job.error or "The bake failed"
        diagnostics.add_log(job.uid, "Error: " + error)
        diagnostics.set_status(job.uid, "Failed")
        diagnostics.finish(job.uid)
        text = f"Background bake of '{name}' failed: {error}"
        if log:
            text += f". Log: {log}"
        level = "ERROR"
    else:
        diagnostics.finish(job.uid)
        if job.result == "stopped":
            text = (f"Background bake of '{name}' stopped: {frames} frames kept" if frames
                    else f"Background bake of '{name}' stopped")
        else:
            text = f"Background bake of '{name}' complete: {frames} frames"
        if domain is None:
            if frames and not job.detached:
                text += f". Its domain is no longer in the scene; the frames are in {job.directory}"
        elif frames and not imported:
            text += f". It is version '{job.slot}' in Versions"
        level = "INFO"

    _forget(job)
    if job.detached:
        console_log("Fumaris: " + text)
    else:
        if imported and not bpy.app.background:
            # An undo step of its own: undoing a later edit keeps the volume.
            try:
                with bpy.context.temp_override(window=_window_for(domain) or bpy.context.window_manager.windows[0]):
                    bpy.ops.ed.undo_push(message="Fumaris Background Bake")
            except Exception as error:
                console_log(f"Fumaris cleanup warning: {error}")
        report(level, text)
    return True


def _forget(job):
    if job in _jobs:
        _jobs.remove(job)
    if runtime._BACKGROUND_JOBS.get(job.uid) is job:
        del runtime._BACKGROUND_JOBS[job.uid]
    job.batch.release(job)


def _keep_log(job):
    """A failed bake's console, kept in the temporary folder. Returns its path."""
    source = os.path.join(job.path, LOG_FILE)
    if not os.path.isfile(source):
        return ""
    folder = os.path.join(os.path.dirname(jobs_root()), LOGS_DIRECTORY)
    safe = "".join(character if character.isalnum() or character in "._-" else "_" for character in job.domain_name)
    target = os.path.join(folder, f"{safe}_{time.strftime('%Y%m%d-%H%M%S')}.log")
    try:
        os.makedirs(folder, exist_ok=True)
        shutil.copyfile(source, target)
        kept = sorted((entry.path for entry in os.scandir(folder) if entry.name.endswith(".log")),
                      key=os.path.getmtime)
        for path in kept[:-KEPT_LOGS]:
            os.remove(path)
    except OSError:
        return ""
    return target


def report(level, text):
    """An operator-style report from the timer: the status bar, the Info
    editor and (errors) a popup. Reports of operators called from Python are
    not shown, so a one-tick modal operator makes it."""
    messages.append((level, text))
    console_log("Fumaris: " + text)
    if bpy.app.background:
        return
    windows = bpy.context.window_manager.windows
    if not windows:
        return
    try:
        with bpy.context.temp_override(window=windows[0]):
            bpy.ops.fumaris.background_report("INVOKE_DEFAULT", level=level, message=text)
    except Exception as error:
        console_log(f"Fumaris cleanup warning: {error}")


def _tag_redraw():
    try:
        for window in bpy.context.window_manager.windows:
            for area in window.screen.areas if window.screen else ():
                if area.type in {"PROPERTIES", "VIEW_3D"}:
                    area.tag_redraw()
    except (AttributeError, ReferenceError):
        # No window manager (Blender is closing).
        pass


# --- Closing -------------------------------------------------------------------

def _detach_all():
    """Their scene is going away: every queued job is dropped and every bake
    told to stop, with nothing to finish in the scene."""
    for job in list(_jobs):
        if job.state == "queued":
            _forget(job)
            continue
        job.detached = True
        if runtime._BACKGROUND_JOBS.get(job.uid) is job:
            del runtime._BACKGROUND_JOBS[job.uid]
        if job.state in {"starting", "running"}:
            stop(job)


@persistent
def _stop_before_load(_dummy=None):
    # Like the foreground job: bakes belong to the scene they were started
    # in. They stop after their frame; the timer removes their files.
    if _jobs:
        console_log(f"Fumaris stopped {len(_jobs)} background bake(s): another file was opened")
    _detach_all()


def shutdown():
    """Blender quits or the add-on is turned off: bakes stop after the frame
    they are on (a bake also stops when its Blender is gone)."""
    if not _jobs:
        return
    _detach_all()
    deadline = time.monotonic() + QUIT_SECONDS
    while time.monotonic() < deadline and any(job.active and job.process.poll() is None for job in _jobs):
        time.sleep(0.05)
    for job in list(_jobs):
        if job.log is not None:
            job.log.close()
            job.log = None
        if job.process is None or job.process.poll() is not None:
            if job.active:
                _end(job, "stopped", "", crashed=job.status.get("state") not in _FINAL)
            _forget(job)
    # Folders of bakes still finishing a frame are removed by the next start.
    _jobs.clear()
    _batches.clear()
    runtime._BACKGROUND_JOBS.clear()


def cleanup_stale():
    """Remove job folders nobody owns: a crashed session's, or ones whose
    bakes outlived their Blender."""
    root = jobs_root()
    tracked = {os.path.normpath(batch.directory) for batch in _batches}
    try:
        folders = [entry.path for entry in os.scandir(root) if entry.is_dir(follow_symlinks=False)]
    except OSError:
        return
    for folder in folders:
        if os.path.normpath(folder) in tracked:
            continue
        try:
            with open(os.path.join(folder, OWNER_FILE), encoding="utf-8") as stream:
                owner = int(json.load(stream).get("pid", 0))
        except (OSError, ValueError, AttributeError, TypeError):
            owner = 0
        if owner and owner != os.getpid() and _pid_is_alive(owner):
            continue    # another running Blender's
        if any(_pid_is_alive(pid) for pid in _bake_pids(folder)):
            continue    # a bake is still finishing its frame
        shutil.rmtree(folder, ignore_errors=True)


def _bake_pids(folder):
    pids = []
    try:
        entries = [entry.path for entry in os.scandir(folder) if entry.is_dir(follow_symlinks=False)]
    except OSError:
        return pids
    for path in entries:
        try:
            with open(os.path.join(path, STATUS_FILE), encoding="utf-8") as stream:
                state = json.load(stream)
            if state.get("state") not in _FINAL:
                pids.append(int(state.get("pid", 0)))
        except (OSError, ValueError, AttributeError, TypeError):
            continue
    return pids


# --- Panel ---------------------------------------------------------------------

def _duration(seconds):
    seconds = int(round(max(0.0, seconds)))
    if seconds < 60:
        return f"{seconds}s"
    minutes, seconds = divmod(seconds, 60)
    if minutes < 60:
        return f"{minutes}m{seconds:02d}s"
    hours, minutes = divmod(minutes, 60)
    return f"{hours}h{minutes:02d}m"


def progress(job):
    """(fraction or None, line, second line) of a job's status."""
    if job.state == "queued":
        ahead = sum(1 for other in _jobs[:_jobs.index(job)] if other.state == "queued") if job in _jobs else 0
        return None, "Queued: starts next" if not ahead else f"Queued: {ahead} ahead", ""
    if job.state == "ended":
        # (The frames the bake reported: draw code reads no disk.)
        return None, f"Baked {int(job.status.get('frames', 0))} frames", f"Its volume appears {job.waiting or 'in a moment'}"
    status = job.status
    total = max(1, int(status.get("end", 0)) - int(status.get("start", 0)) + 1)
    frames = int(status.get("frames", 0))
    if job.state == "stopping":
        return frames / total, f"Stopping after frame {frames} of {total}", ""
    if not status.get("accepted"):
        return None, "Starting…", "First-run shaders can take several minutes"
    fps = float(status.get("fps", 0.0))
    rate = f"{fps:.1f} fps · {_duration((total - frames) / fps)} left" if fps > 0.0 else ""
    return frames / total, f"Frame {frames} of {total}", rate


def draw_status(layout, job):
    """A job's progress: in the domain panel and in the sidebar's list."""
    fraction, line, detail = progress(job)
    column = layout.column(align=True)
    if fraction is None:
        column.label(text=line, icon="TIME" if job.state != "ended" else "CHECKMARK")
    else:
        try:
            column.progress(factor=min(1.0, fraction), type="BAR", text=line)
        except (AttributeError, TypeError):
            column.label(text=line, icon="TIME")
    if detail:
        column.label(text=detail)


def stop_label(job):
    return "Stop Now" if job.state == "stopping" else "Stop"


# --- Operators -----------------------------------------------------------------

def _is_domain(obj):
    return bool(obj and getattr(obj, "fumaris", None) and obj.fumaris.smoke_object_type == "domain"
                and not obj.get(VOLUME_MARKER))


def _active_domain(context):
    obj = context.object
    return obj if _is_domain(obj) else None


def _bake_into(domain, slot):
    """FUMARIS_OT_bake_into: a pinned version to replace, or ("") a new one."""
    from .compare import new_bake_slot
    slot = slot or new_bake_slot(domain)
    require_unprotected(cache_slot_directory(domain, slot), "bake over it")
    domain.fumaris.cache_slot = slot
    domain.fumaris.compare_selected = "vdb:" + slot


def _keep_what_is_there(domain):
    """FUMARIS_OT_bake.invoke without a pinned bake: something else in the
    current version (a preview save) is kept, the bake goes to a new one."""
    from .compare import new_bake_slot
    directory = output_directory_for_object(domain)
    if baked_frame_count(directory, domain.fumaris.output_prefix or "fumaris_") or is_protected(directory):
        domain.fumaris.cache_slot = new_bake_slot(domain)


def _report_start(operator, started, errors):
    for name, error in errors:
        operator.report({"ERROR"}, f"'{name}' is not baked: {error}")
    if not started:
        return {"CANCELLED"}
    workers = worker_count()
    if len(started) == 1:
        text = f"Baking '{started[0].domain_name}' in the background"
    else:
        text = f"Baking {len(started)} domains in the background"
    queued = sum(1 for job in _jobs if job.state == "queued")
    if queued:
        text += f" ({workers} at a time, {queued} queued)"
    operator.report({"INFO"}, text)
    return {"FINISHED"}


class FUMARIS_OT_bake_background(Operator):
    bl_idname = "fumaris.bake_background"
    bl_label = "Bake in Background"
    bl_description = ("Bake this domain in a separate Blender process, from the scene as it is now: keep working, "
                      "preview or bake other domains meanwhile. Preferences > Baking sets how many bake at once and on which GPUs")

    # Set by the Replace / New Bake menu. Scripts calling the operator
    # directly bake into the current version, unasked.
    slot: StringProperty(options={"HIDDEN", "SKIP_SAVE"})
    new: BoolProperty(options={"HIDDEN", "SKIP_SAVE"})

    @classmethod
    def poll(cls, context):
        domain = _active_domain(context)
        if domain is None:
            return False
        if background_job(domain) is not None:
            cls.poll_message_set("This domain is baking in the background")
            return False
        foreground = active_job()
        if foreground is not None and getattr(foreground, "_domain_name", None) == domain.name:
            cls.poll_message_set("Stop this domain's preview or bake first")
            return False
        return True

    def invoke(self, context, _event):
        # The same question as Bake: with a pinned bake, replace it or bake
        # a new one.
        domain = _active_domain(context)
        if not (self.slot or self.new):
            from .compare import bake_target
            target = bake_target(domain)
            if target is not None:
                name = target["name"]

                def draw(menu, _context):
                    layout = menu.layout
                    layout.operator_context = "INVOKE_DEFAULT"
                    layout.operator("fumaris.bake_background", text=f"Replace '{name}'", icon="FILE_REFRESH").slot = name
                    layout.operator("fumaris.bake_background", text="New Bake", icon="ADD").new = True
                context.window_manager.popup_menu(draw, title="Bake in Background", icon="RENDER_ANIMATION")
                return {"CANCELLED"}
            _keep_what_is_there(domain)
        return self.execute(context)

    def execute(self, context):
        domain = _active_domain(context)
        if domain is None:
            self.report({"ERROR"}, "Select a Fumaris domain")
            return {"CANCELLED"}
        try:
            if self.slot or self.new:
                _bake_into(domain, self.slot)
            started, errors = start(context, [domain])
        except RuntimeError as error:
            self.report({"ERROR"}, str(error))
            return {"CANCELLED"}
        return _report_start(self, started, errors)


def bake_all_domains(context, scope="AUTO"):
    """Bake All's domains: every domain in the scene (ALL), the selected ones
    (SELECTED), or the selected ones if any, else all (AUTO, for scripts)."""
    from .playback import scene_domains
    domains = [obj for obj in scene_domains(context) if _is_domain(obj)]
    selected = [obj for obj in domains if obj.select_get()]
    if scope == "ALL":
        return domains
    if scope == "SELECTED":
        return selected
    return selected or domains


class FUMARIS_OT_bake_all(Operator):
    bl_idname = "fumaris.bake_all"
    bl_label = "Bake All"
    bl_description = ("Bake every domain in the scene in the background. Preferences > Baking sets how many "
                      "bake at once and on which GPUs; the others wait their turn")

    # The sidebar's two buttons: every domain, or the selected ones. AUTO
    # (scripts): the selected ones when some are selected, else every domain.
    scope: EnumProperty(
        items=[
            ("AUTO", "Auto", "The selected domains when some are selected, else every domain"),
            ("ALL", "All", "Every domain in the scene"),
            ("SELECTED", "Selected", "The selected domains"),
        ],
        default="AUTO",
        options={"HIDDEN", "SKIP_SAVE"},
    )

    # Scripts calling the operator directly bake into the current versions.
    versions: EnumProperty(
        items=[
            ("CURRENT", "Current", "Bake into each domain's current version"),
            ("REPLACE", "Replace", "Replace each domain's pinned bake"),
            ("NEW", "New", "Bake new versions, keeping the pinned bakes"),
        ],
        default="CURRENT",
        options={"HIDDEN", "SKIP_SAVE"},
    )

    @classmethod
    def poll(cls, context):
        from .playback import scene_domains
        return bool(scene_domains(context))

    @classmethod
    def description(cls, _context, properties):
        if properties.scope == "SELECTED":
            return ("Bake the selected domains in the background. Preferences > Baking sets how many bake at once "
                    "and on which GPUs; the others wait their turn")
        return cls.bl_description

    def _waiting(self, context):
        return [domain for domain in bake_all_domains(context, self.scope) if background_job(domain) is None]

    def invoke(self, context, _event):
        from .compare import bake_target
        if self.versions == "CURRENT" and any(bake_target(domain) is not None for domain in self._waiting(context)):
            def draw(menu, _context):
                layout = menu.layout
                layout.operator_context = "EXEC_DEFAULT"
                for text, icon, versions in (("Replace Pinned Bakes", "FILE_REFRESH", "REPLACE"), ("New Bakes", "ADD", "NEW")):
                    operator = layout.operator("fumaris.bake_all", text=text, icon=icon)
                    operator.versions = versions
                    operator.scope = scope
            scope = self.scope
            context.window_manager.popup_menu(draw, title="Bake Selected" if scope == "SELECTED" else "Bake All",
                                              icon="RENDER_ANIMATION")
            return {"CANCELLED"}
        if self.versions == "CURRENT":
            # No pinned bake anywhere: as Bake, nothing to ask.
            self.versions = "REPLACE"
        return self.execute(context)

    def execute(self, context):
        from .compare import bake_target
        domains = self._waiting(context)
        if not domains:
            self.report({"INFO"}, "Every domain is already baking in the background")
            return {"CANCELLED"}
        errors = []
        ready = []
        for domain in domains:
            try:
                if self.versions != "CURRENT":
                    target = bake_target(domain)
                    if target is None:
                        _keep_what_is_there(domain)
                    else:
                        _bake_into(domain, target["name"] if self.versions == "REPLACE" else "")
                ready.append(domain)
            except RuntimeError as error:
                errors.append((domain.name, str(error)))
        try:
            started, start_errors = start(context, ready)
        except RuntimeError as error:
            self.report({"ERROR"}, str(error))
            return {"CANCELLED"}
        return _report_start(self, started, errors + start_errors)


class FUMARIS_OT_bake_background_stop(Operator):
    bl_idname = "fumaris.bake_background_stop"
    bl_label = "Stop Background Bake"
    bl_description = "Stop this domain's background bake after the frame it is on. The frames written so far are kept and imported"

    # Empty: the active domain.
    domain: StringProperty(options={"HIDDEN", "SKIP_SAVE"})
    everything: BoolProperty(options={"HIDDEN", "SKIP_SAVE"})

    @classmethod
    def description(cls, context, properties):
        if properties.everything:
            return "Stop every background bake after the frame it is on, and drop the queued ones. The frames written so far are kept"
        domain = bpy.data.objects.get(properties.domain) if properties.domain else _active_domain(context)
        job = background_job(domain)
        if job is not None and job.state == "stopping":
            return "The bake is finishing the frame it is on. Click to end it now: that frame is lost"
        if job is not None and job.state == "queued":
            return "Take this domain out of the queue"
        return cls.bl_description

    @classmethod
    def poll(cls, _context):
        return bool(_jobs)

    def execute(self, context):
        if self.everything:
            for job in list(_jobs):
                if job.state in {"queued", "starting", "running"} and not job.detached:
                    stop(job)
            return {"FINISHED"}
        domain = bpy.data.objects.get(self.domain) if self.domain else _active_domain(context)
        job = background_job(domain)
        if job is None:
            self.report({"WARNING"}, "This domain has no background bake")
            return {"CANCELLED"}
        stop(job)
        return {"FINISHED"}


class FUMARIS_OT_background_report(Operator):
    bl_idname = "fumaris.background_report"
    bl_label = "Fumaris Background Bake"
    bl_description = "Show what a background bake reported"
    bl_options = {"INTERNAL"}

    level: StringProperty(default="INFO", options={"HIDDEN", "SKIP_SAVE"})
    message: StringProperty(options={"HIDDEN", "SKIP_SAVE"})

    def invoke(self, context, _event):
        self._timer = context.window_manager.event_timer_add(0.01, window=context.window)
        context.window_manager.modal_handler_add(self)
        return {"RUNNING_MODAL"}

    def modal(self, context, event):
        if event.type != "TIMER":
            return {"PASS_THROUGH"}
        context.window_manager.event_timer_remove(self._timer)
        self.report({self.level}, self.message)
        return {"FINISHED"}

    def execute(self, _context):
        self.report({self.level}, self.message)
        return {"FINISHED"}


CLASSES = (
    FUMARIS_OT_bake_background,
    FUMARIS_OT_bake_all,
    FUMARIS_OT_bake_background_stop,
    FUMARIS_OT_background_report,
)


def _remove_handler_named(handlers, name):
    for handler in list(handlers):
        if getattr(handler, "__name__", "") == name:
            handlers.remove(handler)


def register():
    for cls in CLASSES:
        bpy.utils.register_class(cls)
    if runtime.background_bake() is not None:
        # A background bake's own Blender starts none itself.
        return
    _remove_handler_named(bpy.app.handlers.load_pre, "_stop_before_load")
    bpy.app.handlers.load_pre.append(_stop_before_load)
    atexit.register(shutdown)
    try:
        cleanup_stale()
    except Exception as error:
        console_log(f"Fumaris cleanup warning: {error}")


def unregister():
    if runtime.background_bake() is None:
        shutdown()
        atexit.unregister(shutdown)
        _remove_handler_named(bpy.app.handlers.load_pre, "_stop_before_load")
        if bpy.app.timers.is_registered(_tick):
            bpy.app.timers.unregister(_tick)
    for cls in reversed(CLASSES):
        bpy.utils.unregister_class(cls)
