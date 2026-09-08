import os
import time

import bpy
from bpy.app.handlers import persistent
from bpy.types import Operator

from .bake_state import mark_bake_cancelled, mark_bake_complete, mark_bake_running
from .cache import (
    cache_lock,
    delete_obsolete_cache_state,
    recover_cache_lock,
)
from .diagnostics import console_log
from . import diagnostics
from .exporters import build_session, session_structure_signature
from .importers import (
    VOLUME_MARKER,
    delete_generated_data,
    delete_temporary_writes,
    import_sequence,
    migrate_legacy_cache,
)
from .jobs import FrameRangeJob
from .preview import (
    capture_volume_preview,
    clear_all_previews,
    clear_preview,
    show_preview_payload,
    volume_preview_signature,
)
from .utils import (
    legacy_output_directories_for_object,
    output_directory_for_object,
    simulation_frame_range,
)
from .runtime import (
    GPU_INITIALIZATION_MESSAGE,
    active_job,
    active_mode,
    claim_job,
    clear_geometry_revisions,
    record_geometry_updates,
    release_job,
)


class FUMARIS_OT_bake(FrameRangeJob, Operator):
    bl_idname = "fumaris.bake"
    bl_label = "Bake"
    bl_description = "Bake the Fumaris simulation range to an imported VDB sequence"

    _timer = None
    _worker = None
    _domain_name = ""
    _directory = ""
    _frame = 0
    _end_frame = 0
    _original_frame = 0
    _restore_frame = True
    _participants = ()
    _prefix = "fumaris_"
    _started_at = 0.0
    _submitted = False
    _completed_frames = ()
    _timer_interval = 0.001

    @classmethod
    def poll(cls, context):
        domain = _active_domain(context)
        return bool(domain and not active_job())

    def execute(self, context):
        domain = _active_domain(context)
        if not domain:
            self.report({"ERROR"}, "Select a Fumaris domain")
            return {"CANCELLED"}

        self._domain_name = domain.name
        try:
            claim_job(self, "baking")
            clear_all_previews()
            self._configure_job(
                context,
                domain,
                clear_cache=True,
                write_vdb=True,
                preview_enabled=bool(domain.fumaris.preview_bake),
                show_progress=True,
            )
        except Exception as error:
            self._record_failure(str(error))
            release_job(self)
            self._restore_imported_volumes()
            self._cleanup_volume_staging()
            self._release_cache_lock()
            self.report({"ERROR"}, str(error))
            return {"CANCELLED"}

        mark_bake_running(domain)
        domain.fumaris.baked_frames = 0
        domain.fumaris.bake_elapsed = 0.0
        domain.fumaris.bake_peak_vram_mb = 0.0
        domain.fumaris.bake_peak_ram_mb = 0.0
        self.report({"INFO"}, GPU_INITIALIZATION_MESSAGE)
        return self._start_modal(context)

    def _after_frame_complete(self, context, domain, completed, data, payload):
        if domain:
            data["blender_preview_upload_ms"] = show_preview_payload(
                domain,
                data,
                payload,
            )

    def _complete(self, context):
        domain = bpy.data.objects.get(self._domain_name)
        self._finish(context)
        try:
            _import_cache(context, domain, self._directory, self._prefix)
        except Exception as error:
            if domain:
                mark_bake_cancelled(domain)
                self._store_bake_stats(domain)
            self.report({"ERROR"}, str(error))
            return {"CANCELLED"}

        if domain:
            if self._stop_requested:
                mark_bake_cancelled(domain)
            else:
                mark_bake_complete(domain)
            self._store_bake_stats(domain)
            _activate(context, domain)
        self.report({"INFO"}, "Fumaris bake stopped" if self._stop_requested else "Fumaris bake complete")
        return {"FINISHED"}

    def _cancelled(self, context, data):
        self._record_failure(data.get("message", "Bake failed"))
        domain = bpy.data.objects.get(self._domain_name)
        if domain:
            clear_preview(domain)
            mark_bake_cancelled(domain)
        self._finish(context)
        if domain:
            self._store_bake_stats(domain)
        message = data.get("message", "Bake stopped")
        stderr = data.get("stderr") or self._worker.stderr()
        if stderr:
            message = f"{message}: {stderr}"
        console_log(f"Fumaris bake stopped: {message}")
        self.report({"ERROR"}, message)
        return {"CANCELLED"}

    def _store_bake_stats(self, domain):
        self._sample_memory(force=True)
        props = domain.fumaris
        props.bake_elapsed = time.monotonic() - self._started_at
        props.bake_peak_vram_mb = self._peak_vram_bytes / (1024.0 * 1024.0)
        props.bake_peak_ram_mb = self._peak_ram_bytes / (1024.0 * 1024.0)


class FUMARIS_OT_preview_play(FrameRangeJob, Operator):
    bl_idname = "fumaris.preview_play"
    bl_label = "Preview Play"
    bl_description = "Play a looping live Flow preview over the Fumaris simulation range without writing VDB files"

    _timer = None
    _worker = None
    _domain_name = ""
    _directory = ""
    _frame = 0
    _end_frame = 0
    _original_frame = 0
    _restore_frame = False
    _participants = ()
    _prefix = "fumaris_"
    _started_at = 0.0
    _submitted = False
    _completed_frames = ()
    _session_signature = None
    _timer_interval = 0.001
    _next_submit_at = 0.0

    @classmethod
    def poll(cls, context):
        domain = _active_domain(context)
        return bool(
            domain
            and not active_job()
            and domain.fumaris.simulation_state not in {"baking", "baked"}
        )

    def execute(self, context):
        domain = _active_domain(context)
        if not domain:
            self.report({"ERROR"}, "Select a Fumaris domain")
            return {"CANCELLED"}
        if _is_playing(context):
            self.report({"WARNING"}, "Stop Blender playback before starting Fumaris preview")
            return {"CANCELLED"}

        self._domain_name = domain.name
        self._pause_requested = False
        self._loop_reset_pending = False
        try:
            claim_job(self, "previewing")
            clear_all_previews()
            self._start_preview_session(context, domain, clear_preview_points=True)
        except Exception as error:
            self._record_failure(str(error))
            release_job(self)
            self._restore_imported_volumes()
            self._cleanup_volume_staging()
            self.report({"ERROR"}, str(error))
            return {"CANCELLED"}

        domain.fumaris.baked_frames = 0
        self.report({"INFO"}, GPU_INITIALIZATION_MESSAGE)
        console_log(f"Fumaris preview started for {domain.name} frame {self._frame}")
        return self._start_modal(context)

    def _start_preview_session(self, context, domain, *, clear_preview_points=False):
        signature = session_structure_signature(domain)
        if clear_preview_points:
            clear_preview(domain)
        self._configure_job(
            context,
            domain,
            clear_cache=False,
            start_frame=self._preview_start_frame(context, domain),
            end_frame=simulation_frame_range(domain)[1],
            restore_frame=False,
            write_vdb=False,
            preview_enabled=True,
            resolution_scale=_preview_resolution_scale(domain),
            show_progress=False,
            keep_alive=True,
        )
        self._hide_imported_volumes()
        self._session_signature = signature
        self._next_submit_at = 0.0
        self._preview_render_submitted = False
        self._preview_rerendered_since_frame = False
        self._has_volume_preview_frame = False
        self._last_preview_signature = None
        self._next_preview_capture_at = 0.0
        self._preview_render_count = 0

    def _restart_preview_session(self, context, domain):
        signature = session_structure_signature(domain)
        start, end = simulation_frame_range(domain)
        clear_preview(domain)
        self._frame = start
        self._end_frame = end
        self._submitted = False
        self._accepted = False
        self._stop_requested = False
        self._ending_session = False
        self._completed_frames = []
        self._resolution_scale = _preview_resolution_scale(domain)
        domain.fumaris.baked_frames = 0
        session, self._participants = build_session(
            context,
            start_frame=start,
            end_frame=end,
            domain=domain,
            write_vdb=False,
            preview_enabled=True,
            resolution_scale=self._resolution_scale,
            log_participants=False,
        )
        self._prefix = session["output_prefix"]
        self._session_signature = signature
        self._next_submit_at = 0.0
        self._preview_render_submitted = False
        self._preview_rerendered_since_frame = False
        self._has_volume_preview_frame = False
        self._last_preview_signature = None
        self._next_preview_capture_at = 0.0
        self._loop_reset_pending = False
        self._loop_reset_started = time.perf_counter()
        self._worker.reset_session(session)

    def _ready_to_submit(self, context):
        if getattr(self, "_preview_render_submitted", False):
            return False
        if (
            getattr(self, "_loop_reset_pending", False)
            and not getattr(self, "_pause_requested", False)
        ):
            domain = bpy.data.objects.get(self._domain_name)
            if domain is None:
                raise RuntimeError("The active Fumaris domain was deleted")
            self._restart_preview_session(context, domain)
            return False
        if self._submit_changed_volume_preview(context):
            return False
        if getattr(self, "_pause_requested", False):
            return False
        return time.monotonic() >= getattr(self, "_next_submit_at", 0.0)

    def _submit_changed_volume_preview(self, context):
        if not getattr(self, "_has_volume_preview_frame", False):
            return False
        if (
            getattr(self, "_preview_rerendered_since_frame", False)
            and not getattr(self, "_pause_requested", False)
        ):
            return False
        now = time.monotonic()
        if now < getattr(self, "_next_preview_capture_at", 0.0):
            return False
        self._next_preview_capture_at = now + (1.0 / 30.0)

        domain = bpy.data.objects.get(self._domain_name)
        if domain is None or domain.fumaris.preview_mode != "volume":
            return False
        preview = capture_volume_preview(context, domain)
        signature = volume_preview_signature(preview)
        if signature is None or signature == self._last_preview_signature:
            return False
        self._worker.send_preview(preview)
        self._preview_render_submitted = True
        self._preview_rerendered_since_frame = True
        self._last_preview_signature = signature
        return True

    def _submit_frame(self, context):
        domain = bpy.data.objects.get(self._domain_name)
        if domain is None:
            raise RuntimeError("The active Fumaris domain was deleted")
        signature = session_structure_signature(domain)
        if signature != self._session_signature:
            console_log(
                f"Fumaris preview restarted for {domain.name}: "
                "session structure changed"
            )
            if getattr(self, "_worker", None):
                self._worker.close()
            self._restore_imported_volumes()
            self._cleanup_volume_staging()
            self._started_at = time.monotonic()
            self._completed_frames = []
            self._submitted = False
            self._accepted = False
            self._stop_requested = False
            self._ending_session = False
            self._start_preview_session(context, domain, clear_preview_points=True)
            return
        super()._submit_frame(context)

    def _preview_start_frame(self, _context, domain):
        return simulation_frame_range(domain)[0]

    def _after_frame_complete(self, context, domain, completed, data, payload):
        self._preview_rerendered_since_frame = False
        if domain:
            data["blender_preview_upload_ms"] = show_preview_payload(
                domain,
                data,
                payload,
            )
        preview = data.get("preview") or {}
        self._has_volume_preview_frame = preview.get("type") == "volume_rgba8"
        if self._has_volume_preview_frame:
            self._last_preview_signature = volume_preview_signature(
                getattr(self, "_last_submitted_volume_preview", None)
            )
        started = getattr(self, "_last_frame_submit_started", time.monotonic())
        self._next_submit_at = started + _scene_frame_duration(context.scene)

    def _after_preview_complete(self, _context, domain, data, payload):
        if domain:
            data["blender_preview_upload_ms"] = show_preview_payload(
                domain,
                data,
                payload,
            )
        self._preview_render_count = getattr(self, "_preview_render_count", 0) + 1
        console_log(
            "Fumaris viewport rerender: "
            f"total={float(data.get('preview_render_ms', 0.0)):.3f}ms "
            f"submit={float(data.get('flow_submit_ms', 0.0)):.3f}ms "
            f"wait={float(data.get('flow_wait_ms', 0.0)):.3f}ms"
        )

    def _complete(self, context):
        domain = bpy.data.objects.get(self._domain_name)
        self._finish(context)
        if domain:
            clear_preview(domain)
            domain.fumaris.simulation_state = "idle"
            domain.fumaris.bake_elapsed = time.monotonic() - self._started_at
        console_log(f"Fumaris preview complete for {self._domain_name}")
        return {"FINISHED"}

    def set_paused(self, context, paused):
        paused = bool(paused)
        if paused == getattr(self, "_pause_requested", False):
            return
        self._pause_requested = paused
        diagnostics.set_status(getattr(self, "_diagnostic_key", None), "Paused" if paused else "Running")
        if paused:
            console_log(f"Fumaris preview paused for {self._domain_name}")
            return

        domain = bpy.data.objects.get(self._domain_name)
        if domain is None:
            raise RuntimeError("The active Fumaris domain was deleted")
        self._next_submit_at = 0.0
        if getattr(self, "_loop_reset_pending", False):
            # Let any pending viewport response arrive before resetting Flow.
            self._ready_to_submit(context)
        console_log(f"Fumaris preview resumed for {self._domain_name}")

    def _end_of_range(self, context, completed):
        domain = bpy.data.objects.get(self._domain_name)
        if domain is None:
            return self._cancelled(context, {"message": "The active Fumaris domain was deleted"})
        if getattr(self, "_pause_requested", False):
            self._submitted = False
            self._loop_reset_pending = True
            return None
        try:
            self._restart_preview_session(context, domain)
        except Exception as error:
            return self._cancelled(context, {"message": str(error)})
        return None

    def _cancelled(self, context, data):
        self._record_failure(data.get("message", "Preview failed"))
        domain = bpy.data.objects.get(self._domain_name)
        if domain:
            clear_preview(domain)
            domain.fumaris.simulation_state = "idle"
            domain.fumaris.bake_elapsed = time.monotonic() - self._started_at
        self._finish(context)
        message = data.get("message", "Preview stopped")
        stderr = data.get("stderr") or self._worker.stderr()
        if stderr:
            message = f"{message}: {stderr}"
        console_log(f"Fumaris preview stopped: {message}")
        return {"CANCELLED"}


class FUMARIS_OT_preview_stop(Operator):
    bl_idname = "fumaris.preview_stop"
    bl_label = "Preview Stop"
    bl_description = "Stop the active Fumaris live preview and close its bridge process"

    @classmethod
    def poll(cls, context):
        return active_mode() == "previewing"

    def execute(self, context):
        job = active_job() if active_mode() == "previewing" else None
        if job is None:
            self.report({"WARNING"}, "This domain has no active preview")
            return {"CANCELLED"}
        _cancel_job(context, job)
        return {"FINISHED"}


class FUMARIS_OT_preview_pause(Operator):
    bl_idname = "fumaris.preview_pause"
    bl_label = "Pause or Resume Preview"
    bl_description = "Freeze or resume simulation while keeping the Flow volume available for viewport rerenders"

    @classmethod
    def poll(cls, context):
        return active_mode() == "previewing"

    def execute(self, context):
        job = active_job() if active_mode() == "previewing" else None
        if job is None:
            self.report({"WARNING"}, "This domain has no active preview")
            return {"CANCELLED"}
        try:
            job.set_paused(
                context,
                not getattr(job, "_pause_requested", False),
            )
        except Exception as error:
            _cancel_job(context, job)
            self.report({"ERROR"}, str(error))
            return {"CANCELLED"}
        for area in context.screen.areas:
            area.tag_redraw()
        return {"FINISHED"}


def _cancel_job(context, job):
    job._finished = True
    clear_preview(getattr(job, "_domain_name", ""))
    domain = bpy.data.objects.get(getattr(job, "_domain_name", ""))
    if domain:
        domain.fumaris.simulation_state = "idle"
        clear_preview(domain)
    job.request_cancel()
    if getattr(job, "_worker", None):
        job._worker.close()
    job._finish(context)


def _restore_selection(context, domain, selected, active):
    for obj in context.selected_objects:
        obj.select_set(False)
    for obj in selected:
        if bpy.data.objects.get(obj.name):
            obj.select_set(True)
    if active and bpy.data.objects.get(active.name):
        context.view_layer.objects.active = active
    else:
        context.view_layer.objects.active = domain


def _import_cache(context, domain, directory, prefix):
    if domain is None:
        return
    selected = list(context.selected_objects)
    active = context.view_layer.objects.active
    clear_preview(domain)
    props = domain.fumaris
    fps = float(context.scene.render.fps) / max(
        1e-6,
        float(context.scene.render.fps_base),
    )
    flame_rate_scale = (
        fps
        * float(props.num_sub_steps)
        / max(1e-6, float(props.simulation_speed))
    )
    import_sequence(
        directory,
        simulation_frame_range(domain)[0],
        prefix,
        material=props.volume_material,
        selectable=props.volume_selectable,
        appearance=props,
        flame_rate_scale=flame_rate_scale,
    )
    _restore_selection(context, domain, selected, active)


def _stop_active_job():
    job = active_job()
    if job is not None:
        _cancel_job(bpy.context, job)


class FUMARIS_OT_delete(Operator):
    bl_idname = "fumaris.delete"
    bl_label = "Delete Baked"
    bl_description = "Delete Fumaris VDB cache files, imported volumes, and the live preview for this domain"

    @classmethod
    def poll(cls, context):
        domain = _active_domain(context)
        return bool(domain and not active_job())

    def execute(self, context):
        domain = _active_domain(context)
        if not domain:
            self.report({"ERROR"}, "Select a Fumaris domain")
            return {"CANCELLED"}
        if active_job():
            self.report({"ERROR"}, "Stop the active Fumaris simulation first")
            return {"CANCELLED"}
        prefix = domain.fumaris.output_prefix or "fumaris_"
        directory = output_directory_for_object(domain)
        try:
            for legacy_directory in legacy_output_directories_for_object(domain):
                recover_cache_lock(legacy_directory, owner_pid=os.getpid())
                with cache_lock(legacy_directory):
                    migrate_legacy_cache(legacy_directory, directory, prefix)
                    delete_generated_data(legacy_directory, prefix)
                    delete_obsolete_cache_state(legacy_directory)
            recover_cache_lock(directory, owner_pid=os.getpid())
            with cache_lock(directory):
                clear_preview(domain)
                delete_generated_data(directory, prefix)
                delete_obsolete_cache_state(directory)
        except RuntimeError as error:
            self.report({"ERROR"}, str(error))
            return {"CANCELLED"}
        domain.fumaris.simulation_state = "idle"
        domain.fumaris.baked_frames = 0
        domain.fumaris.bake_elapsed = 0.0
        domain.fumaris.bake_peak_vram_mb = 0.0
        domain.fumaris.bake_peak_ram_mb = 0.0
        return {"FINISHED"}


class _ActiveBakeOperator:
    @classmethod
    def poll(cls, context):
        return active_mode() == "baking"

    def _active_job(self, context):
        return active_job() if active_mode() == "baking" else None


class FUMARIS_OT_stop(_ActiveBakeOperator, Operator):
    bl_idname = "fumaris.stop"
    bl_label = "Stop"
    bl_description = "Stop the active bake and import the frames written so far"

    def execute(self, context):
        job = self._active_job(context)
        if job is None:
            self.report({"WARNING"}, "This domain has no active bake")
            return {"CANCELLED"}
        if not getattr(job, "_accepted", False):
            domain = bpy.data.objects.get(getattr(job, "_domain_name", ""))
            job._stop_requested = True
            _cancel_job(context, job)
            if domain:
                mark_bake_cancelled(domain)
                job._store_bake_stats(domain)
            self.report({"INFO"}, "Fumaris bake stopped before simulation started")
            return {"FINISHED"}
        job.request_stop(context)
        return {"FINISHED"}


def _activate(context, obj):
    for selected in context.selected_objects:
        selected.select_set(False)
    obj.select_set(True)
    context.view_layer.objects.active = obj


def _is_imported_volume(obj):
    return bool(obj and obj.get(VOLUME_MARKER))


def _active_domain(context):
    obj = context.object
    if (
        obj
        and hasattr(obj, "fumaris")
        and obj.fumaris.smoke_object_type == "domain"
        and not _is_imported_volume(obj)
    ):
        return obj
    return None


class FUMARIS_OT_copy_diagnostics(Operator):
    bl_idname = "fumaris.copy_diagnostics"
    bl_label = "Copy Diagnostics"
    bl_description = "Copy system, GPU, timing, memory and recent bridge messages for support; may include paths from error messages"

    @classmethod
    def poll(cls, context):
        return _active_domain(context) is not None

    def execute(self, context):
        context.window_manager.clipboard = diagnostics.report(_active_domain(context))
        self.report({"INFO"}, "Fumaris diagnostics copied")
        return {"FINISHED"}


CLASSES = (
    FUMARIS_OT_copy_diagnostics,
    FUMARIS_OT_bake,
    FUMARIS_OT_preview_play,
    FUMARIS_OT_preview_pause,
    FUMARIS_OT_preview_stop,
    FUMARIS_OT_delete,
    FUMARIS_OT_stop,
)


def _remove_handler_named(handlers, name):
    for handler in list(handlers):
        if getattr(handler, "__name__", "") == name:
            handlers.remove(handler)


def _recover_orphaned_bakes():
    objects = getattr(bpy.data, "objects", None)
    if objects is None:
        return
    owner_pid = os.getpid() if not active_job() else None
    for obj in objects:
        if not hasattr(obj, "fumaris"):
            continue
        props = obj.fumaris
        if props.smoke_object_type != "domain":
            continue
        directory = output_directory_for_object(obj)
        try:
            recover_cache_lock(directory, owner_pid=owner_pid)
            if not os.path.isdir(directory):
                if props.simulation_state == "baking":
                    props.simulation_state = "stopped"
                continue
            with cache_lock(directory):
                delete_temporary_writes(
                    directory,
                    props.output_prefix or "fumaris_",
                )
                if props.simulation_state == "baking":
                    props.simulation_state = "stopped"
        except RuntimeError:
            # Another Blender process may still be writing this cache.
            continue
        except OSError as error:
            if props.simulation_state == "baking":
                props.simulation_state = "stopped"
            console_log(f"Fumaris could not recover cache for {obj.name}: {error}")


@persistent
def _recover_orphaned_bakes_after_load(_dummy=None):
    _recover_orphaned_bakes()


@persistent
def _clear_previews_before_load(_dummy=None):
    _stop_active_job()
    clear_all_previews()
    clear_geometry_revisions()
    diagnostics.clear()


@persistent
def _track_geometry_updates(_scene, depsgraph):
    record_geometry_updates(depsgraph)


def _is_playing(context):
    return bool(context.screen and context.screen.is_animation_playing)


def _scene_frame_duration(scene):
    fps = float(getattr(scene.render, "fps", 24.0))
    fps_base = float(getattr(scene.render, "fps_base", 1.0))
    if fps <= 0.0 or fps_base <= 0.0:
        return 1.0 / 24.0
    return fps_base / fps


def _preview_resolution_scale(domain):
    percent = float(getattr(domain.fumaris, "preview_resolution_percent", 100.0))
    return max(0.0, min(100.0, percent)) / 100.0


@persistent
def _cancel_preview_before_native_playback(_scene=None, _depsgraph=None):
    if active_mode() != "previewing":
        return
    console_log("Fumaris preview stopped because Blender playback started")
    _cancel_job(bpy.context, active_job())


def register():
    for cls in CLASSES:
        bpy.utils.register_class(cls)
    _recover_orphaned_bakes()
    _remove_handler_named(bpy.app.handlers.load_post, "_recover_orphaned_bakes_after_load")
    if _recover_orphaned_bakes_after_load not in bpy.app.handlers.load_post:
        bpy.app.handlers.load_post.append(_recover_orphaned_bakes_after_load)
    _remove_handler_named(bpy.app.handlers.load_pre, "_clear_previews_before_load")
    if _clear_previews_before_load not in bpy.app.handlers.load_pre:
        bpy.app.handlers.load_pre.append(_clear_previews_before_load)
    _remove_handler_named(bpy.app.handlers.depsgraph_update_post, "_track_geometry_updates")
    if _track_geometry_updates not in bpy.app.handlers.depsgraph_update_post:
        bpy.app.handlers.depsgraph_update_post.append(_track_geometry_updates)
    _remove_handler_named(bpy.app.handlers.animation_playback_pre, "_cancel_preview_before_native_playback")
    if _cancel_preview_before_native_playback not in bpy.app.handlers.animation_playback_pre:
        bpy.app.handlers.animation_playback_pre.append(_cancel_preview_before_native_playback)


def unregister():
    _stop_active_job()
    clear_all_previews()
    diagnostics.clear()
    _remove_handler_named(bpy.app.handlers.load_pre, "_clear_previews_before_load")
    _remove_handler_named(bpy.app.handlers.load_post, "_recover_orphaned_bakes_after_load")
    _remove_handler_named(bpy.app.handlers.depsgraph_update_post, "_track_geometry_updates")
    _remove_handler_named(bpy.app.handlers.animation_playback_pre, "_cancel_preview_before_native_playback")
    for cls in reversed(CLASSES):
        bpy.utils.unregister_class(cls)
