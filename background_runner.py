"""A background bake's own Blender (Bake in Background, background.py).

    blender --background --factory-startup <job>/../scene.blend \
        --python background_runner.py -- <job directory>

The loaded file is a snapshot of the scene. This bakes one of its domains
through the bake operator's code (FrameRangeJob), ticked in a loop instead of
by a window's timer, and reports through files in the job directory:
status.json (written every frame) and cancel (Stop, from the main process).
"""
import json
import os
import sys

JOB_FILE = "job.json"
STATUS_FILE = "status.json"
CANCEL_FILE = "cancel"

if __name__ == "__main__":
    # Started as a script: import the add-on this file belongs to from its
    # own folder (factory settings load no add-ons), then run as its module.
    import importlib
    from types import SimpleNamespace

    try:
        # The console is the job's log file: keep it current, for a bake that dies.
        sys.stdout.reconfigure(line_buffering=True)
    except (AttributeError, ValueError):
        pass
    _directory = os.path.dirname(os.path.abspath(__file__))
    sys.path.insert(0, os.path.dirname(_directory))
    _job_directory = sys.argv[sys.argv.index("--") + 1]
    with open(os.path.join(_job_directory, JOB_FILE), encoding="utf-8") as _stream:
        _job = json.load(_stream)
    _addon = importlib.import_module(os.path.basename(_directory))
    # Before registering: a background bake leaves the other caches alone.
    importlib.import_module(_addon.__name__ + ".runtime").set_background_bake(
        SimpleNamespace(**_job["preferences"]), _job["blend_directory"])
    _addon.register()
    _module = importlib.import_module(_addon.__name__ + ".background_runner")
    sys.exit(_module.run(_job_directory, _job))

import time
import traceback

import bpy

from . import diagnostics
from .bake_state import mark_bake_cancelled, mark_bake_complete, mark_bake_running
from .cache import _pid_is_alive, write_settings_snapshot
from .diagnostics import console_log
from .jobs import FrameRangeJob
from .operators import FUMARIS_OT_bake, _cancel_job
from .runtime import GPU_INITIALIZATION_MESSAGE, claim_job, release_job

# The loop's pause when the bridge has nothing new (the operator's timer).
TICK_SECONDS = 0.001
# status.json is also written between frames, so the main process can tell a
# long frame from a bake that hangs.
HEARTBEAT_SECONDS = 1.0
PARENT_CHECK_SECONDS = 2.0
# How often the loop looks for the cancel file.
CANCEL_CHECK_SECONDS = 0.25
# With its main Blender gone, a bake that was told to stop but is still
# inside a frame after this long is ended (nobody is left to end it).
ORPHAN_SECONDS = 60.0
# Status updates the frames-per-second figure is measured over.
RATE_FRAMES = 8


def write_json(path, data):
    """Readers never see a half-written file."""
    temporary = f"{path}.{os.getpid()}.tmp"
    with open(temporary, "w", encoding="utf-8") as stream:
        json.dump(data, stream)
    try:
        os.replace(temporary, path)
    except OSError:
        # Windows: the reader has it open right now; the next write replaces it.
        try:
            os.remove(temporary)
        except OSError:
            pass


class BackgroundBake(FrameRangeJob):
    """The bake operator without a window: no timer, no progress cursor, and
    its reports go to the status file. run() starts and ticks it."""

    _timer = None
    _worker = None
    _domain_name = ""
    _directory = ""
    _participants = ()
    _prefix = "fumaris_"
    _completed_frames = ()

    _cancelled = FUMARIS_OT_bake._cancelled

    def __init__(self):
        self.message = ""
        self.error = ""
        self.stored_stats = False

    def _store_bake_stats(self, domain):
        FUMARIS_OT_bake._store_bake_stats(self, domain)
        # The version's statistics file is this bake's (not an earlier one's).
        self.stored_stats = True

    def report(self, kinds, message):
        console_log(f"Fumaris background bake: {message}")
        self.message = message
        if "ERROR" in kinds:
            self.error = message

    def start(self, context, domain, directory):
        """FUMARIS_OT_bake.execute, into the folder the main process resolved."""
        self._domain_name = domain.name
        try:
            claim_job(self, "baking")
            self._configure_job(
                context,
                domain,
                clear_cache=True,
                write_vdb=True,
                # No viewport to show a preview in.
                preview_enabled=False,
                show_progress=False,
                output_directory=directory,
            )
        except Exception as error:
            self._record_failure(str(error))
            release_job(self)
            self._restore_imported_volumes()
            self._cleanup_volume_staging()
            self._release_cache_lock()
            self.report({"ERROR"}, str(error))
            return False

        try:
            write_settings_snapshot(self._directory, domain, self._participants)
        except Exception as error:
            console_log(f"Fumaris could not store the bake settings: {error}")
        mark_bake_running(domain)
        domain.fumaris.baked_frames = 0
        domain.fumaris.bake_elapsed = 0.0
        domain.fumaris.bake_peak_vram_mb = 0.0
        domain.fumaris.bake_peak_ram_mb = 0.0
        self.report({"INFO"}, GPU_INITIALIZATION_MESSAGE)
        return True

    def stop(self, context):
        """FUMARIS_OT_stop.execute: the frames written so far are kept."""
        if not getattr(self, "_accepted", False):
            domain = bpy.data.objects.get(self._domain_name)
            self._stop_requested = True
            _cancel_job(context, self)
            if domain:
                mark_bake_cancelled(domain)
                self._store_bake_stats(domain)
            self.report({"INFO"}, "Fumaris bake stopped before simulation started")
            return
        self.request_stop(context)

    def tick(self, context):
        """FrameRangeJob.modal for one timer event."""
        try:
            return self._modal_tick(context)
        except Exception as error:
            self.request_cancel()
            return self._cancelled(context, {"message": str(error)})

    def _complete(self, context):
        # FUMARIS_OT_bake._complete without the import: the volume belongs in
        # the main process's scene, which imports it when this bake has ended.
        domain = bpy.data.objects.get(self._domain_name)
        self._finish(context)
        if domain:
            if self._stop_requested:
                mark_bake_cancelled(domain)
            else:
                mark_bake_complete(domain)
            self._store_bake_stats(domain)
        self.report({"INFO"}, "Fumaris bake stopped" if self._stop_requested else "Fumaris bake complete")
        return {"FINISHED"}


class _Status:
    """status.json: what the main process shows and finishes the bake with."""

    def __init__(self, directory, job):
        self._path = os.path.join(directory, STATUS_FILE)
        self._written = 0.0
        self._frames = -1
        self._samples = []      # (time, frames finished), for the rate
        self.data = {
            "state": "starting",
            "pid": os.getpid(),
            "domain": job["domain"],
            "message": "",
            "error": "",
            "accepted": False,
            "start": 0,
            "end": 0,
            "frame": 0,
            "frames": 0,
            "fps": 0.0,
            "elapsed": 0.0,
            "peak_vram_bytes": 0,
            "peak_ram_bytes": 0,
            "stats": False,
            "metrics": {},
            "log": [],
        }

    def write(self, **values):
        self.data.update(values)
        self.data["time"] = time.time()
        self._written = time.monotonic()
        write_json(self._path, self.data)

    def update(self, bake, *, force=False):
        """Every finished frame, and a heartbeat between them."""
        frames = len(bake._completed_frames)
        now = time.monotonic()
        if frames == self._frames and not force and now - self._written < HEARTBEAT_SECONDS:
            return
        if frames != self._frames:
            self._frames = frames
            self._samples = (self._samples + [(now, frames)])[-RATE_FRAMES:]
        span = self._samples[-1][0] - self._samples[0][0] if len(self._samples) > 1 else 0.0
        state = diagnostics.snapshot(getattr(bake, "_diagnostic_key", None)) or {}
        self.write(
            message=bake.message,
            error=bake.error,
            accepted=bool(getattr(bake, "_accepted", False)),
            start=int(bake._progress_start_frame),
            end=int(bake._end_frame),
            frame=int(bake._completed_frames[-1]) if frames else int(bake._progress_start_frame),
            frames=frames,
            fps=(self._samples[-1][1] - self._samples[0][1]) / span if span > 0.0 else 0.0,
            elapsed=now - bake._started_at,
            peak_vram_bytes=int(bake._peak_vram_bytes),
            peak_ram_bytes=int(bake._peak_ram_bytes),
            stats=bake.stored_stats,
            metrics=dict(state.get("metrics", {})),
            capacity_reached=bool(state.get("capacity_reached", False)),
            log=list(state.get("log", ()))[-20:],
        )


def _parent_gone(parent):
    if os.name != "nt":
        # A dead parent hands its children on, also before it is reaped.
        return os.getppid() != parent
    return not _pid_is_alive(parent)


def _show_scene(job):
    """The scene and view layer the bake was started from."""
    window = bpy.context.window
    scene = bpy.data.scenes.get(job.get("scene", ""))
    if window is None or scene is None:
        return
    if window.scene != scene:
        window.scene = scene
    layer = scene.view_layers.get(job.get("view_layer", ""))
    if layer is not None and window.view_layer != layer:
        window.view_layer = layer


def run(directory, job):
    """Bake job["domain"] of the loaded snapshot. Returns the exit code."""
    status = _Status(directory, job)
    status.write()
    try:
        return _bake(directory, job, status)
    except Exception as error:
        console_log(traceback.format_exc())
        status.write(state="failed", error=str(error) or type(error).__name__)
        return 1


def _bake(directory, job, status):
    _show_scene(job)
    context = bpy.context
    domain = bpy.data.objects.get(job["domain"])
    if domain is None or domain.fumaris.smoke_object_type != "domain":
        status.write(state="failed", error=f"'{job['domain']}' is not a Fumaris domain in the snapshot")
        return 1

    cancel = os.path.join(directory, CANCEL_FILE)
    if os.path.exists(cancel):
        # Stopped while this Blender was starting: nothing was written.
        status.write(state="stopped")
        return 0

    bake = BackgroundBake()
    if not bake.start(context, domain, job["output_directory"]):
        status.write(state="failed", message=bake.message, error=bake.error or "The bake could not start")
        return 1
    status.update(bake, force=True)
    status.write(state="running")

    parent = int(job.get("parent_pid", 0))
    next_parent_check = 0.0
    next_cancel_check = 0.0
    orphaned = None     # when the main process was found gone
    while not bake._finished:
        now = time.monotonic()
        if parent and orphaned is None and now >= next_parent_check:
            next_parent_check = now + PARENT_CHECK_SECONDS
            if _parent_gone(parent):
                orphaned = now
        if not bake._stop_requested:
            cancelled = False
            if now >= next_cancel_check:
                next_cancel_check = now + CANCEL_CHECK_SECONDS
                cancelled = os.path.exists(cancel)
            # Stop in the main process, or the main process is gone (nothing
            # could stop or finish this bake any more).
            if orphaned is not None or cancelled:
                console_log("Fumaris background bake: "
                            + ("its Blender closed" if orphaned is not None else "Stop requested"))
                bake.stop(context)
                status.write(state="stopping")
                continue
        elif orphaned is not None and now - orphaned > ORPHAN_SECONDS:
            console_log("Fumaris background bake: ended inside a frame, its Blender closed")
            _cancel_job(context, bake)
            domain = bpy.data.objects.get(bake._domain_name)
            if domain:
                mark_bake_cancelled(domain)
                bake._store_bake_stats(domain)
            break
        bake.tick(context)
        status.update(bake)
        time.sleep(TICK_SECONDS)

    failed = bool(getattr(bake, "_failed", False))
    status.update(bake, force=True)
    status.write(state="failed" if failed else "stopped" if bake._stop_requested else "finished",
                 error=(bake.error or "The bake failed") if failed else "")
    return 1 if failed else 0
