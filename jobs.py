import os
import re
import tempfile
import time

import bpy

from .bridge import BridgeWorker, process_memory_bytes
from .cache import RefusedError, cache_lock, require_unprotected
from .diagnostics import console_log
from . import diagnostics
from .exporters import bridge_executable, build_frame, build_session
from .importers import (
    baked_frame_count,
    delete_generated_data,
    delete_temporary_writes,
    hide_generated_volumes,
    restore_generated_volumes,
)
from .protocol import (
    CANCELLED,
    FAILED,
    FRAME_COMPLETE,
    PREVIEW_COMPLETE,
    READY,
    SESSION_ACCEPTED,
    SESSION_COMPLETE,
)
from .preview import capture_volume_preview, clear_preview
from .runtime import release_job
from .utils import (
    output_directory_for_object,
    runtime_validation_error,
    simulation_frame_range,
)


# A bake's status once every frame is simulated and its files are still
# being written (the panel's and a background bake's).
WRITING_LAST_FRAMES = "Writing the last frames to disk…"


def writing_last_frames(job):
    """True for a bake waiting for its last frames' files."""
    check = getattr(job, "writing_last_frames", None)
    return bool(check()) if callable(check) else False


class FrameRangeJob:
    # Frames sent to the bridge and not yet complete. Playing a live preview
    # sends one frame ahead (_pipeline_depth 2), so the bridge overlaps the
    # next frame's CPU work with the frame on the GPU; bakes send one at a time.
    _in_flight = 0

    # The domain, by name, found again after a rename: the job keeps the
    # object's session_uid beside its name. (A renamed domain stopped its
    # preview or bake with "The active Fumaris domain was deleted" and left
    # it marked baking, 2026-11-14 UX test.) A name another object now has
    # resolves to "" so nothing acts on the wrong object.
    _domain_name_value = ""
    _domain_uid = None

    @property
    def _domain_name(self):
        name = self._domain_name_value or ""
        uid = self._domain_uid
        if uid is None or not name:
            return name
        obj = bpy.data.objects.get(name)
        if obj is None or obj.session_uid != uid:
            found = next((candidate for candidate in bpy.data.objects if candidate.session_uid == uid), None)
            if found is not None:
                self._domain_name_value = name = found.name
                obj = found
            elif obj is not None and getattr(obj, "fumaris", None) is not None and obj.fumaris.smoke_object_type == "domain":
                # the same domain under a new identity (as after an undo)
                self._domain_uid = int(obj.session_uid)
            else:
                return "" if obj is not None else name
        # Deleted with Blender's X, a domain the scene still points at stays
        # in the file in no scene: the job must not go on simulating it.
        if obj is not None and not obj.users_scene:
            return ""
        return name

    @_domain_name.setter
    def _domain_name(self, value):
        self._domain_name_value = value
        obj = bpy.data.objects.get(value) if value else None
        self._domain_uid = int(obj.session_uid) if obj is not None else None

    @property
    def _submitted(self):
        return self._in_flight > 0

    @_submitted.setter
    def _submitted(self, value):
        self._in_flight = max(1, self._in_flight) if value else 0

    def _pipeline_depth(self, _context):
        return 1

    def _configure_job(
        self,
        context,
        domain,
        *,
        clear_cache,
        start_frame=None,
        end_frame=None,
        restore_frame=True,
        write_vdb=True,
        preview_enabled=True,
        resolution_scale=1.0,
        show_progress=True,
        completed_frames=None,
        keep_alive=False,
        output_directory=None,
        require_emitter=True,
    ):
        runtime_error = runtime_validation_error()
        if runtime_error:
            raise RuntimeError(runtime_error)
        executable = bridge_executable()
        if executable is None:
            raise RuntimeError("Bridge executable not found")

        self._domain_name = domain.name
        self._directory = output_directory or output_directory_for_object(domain)
        sim_start, sim_end = simulation_frame_range(domain)
        self._frame = sim_start if start_frame is None else start_frame
        self._end_frame = sim_end if end_frame is None else end_frame
        self._original_frame = context.scene.frame_current
        self._restore_frame = restore_frame
        self._started_at = time.monotonic()
        self._submitted = False
        self._accepted = False
        self._stop_requested = False
        self._ending_session = False
        self._stop_now = False
        self._show_progress = bool(show_progress)
        self._finished = False
        self._failed = False
        self._resolution_scale = resolution_scale
        self._progress_start_frame = self._frame
        self._completed_frames = list(completed_frames or [])
        self._cache_lock = None
        self._peak_ram_bytes = 0
        self._peak_vram_bytes = 0
        self._next_memory_sample = 0.0
        self._track_metrics = True
        mode = "Preview Recording" if write_vdb and keep_alive else "Bake" if write_vdb else "Preview"
        self._diagnostic_key = diagnostics.begin(domain, mode)
        self._preview_enabled = bool(preview_enabled)

        session, self._participants = build_session(
            context,
            start_frame=self._frame,
            end_frame=self._end_frame,
            domain=domain,
            write_vdb=write_vdb,
            preview_enabled=preview_enabled,
            resolution_scale=resolution_scale,
            log_participants=True,
            require_emitter=require_emitter,
        )
        session["output_directory"] = self._directory

        self._prefix = session["output_prefix"]
        if not all(character.isalnum() or character in "._-" for character in self._prefix):
            raise RuntimeError(
                "Output prefix may contain only letters, numbers, '.', '_', and '-'"
            )

        if clear_cache:
            require_unprotected(self._directory, "bake over it")
        try:
            os.makedirs(self._directory, exist_ok=True)
        except OSError as error:
            # The OS message ("[Errno 2] No such file or directory: ...") names
            # the cache subfolder, not the folder the user chose.
            from .cache import RefusedError
            chosen = getattr(domain.fumaris, "output_dir", "") or "the output folder"
            raise RefusedError(
                f"Can't write to {chosen} ({error.strerror or error}). "
                "Choose another Output Directory (Output panel)") from error
        if write_vdb:
            self._cache_lock = cache_lock(self._directory).acquire()
        try:
            if clear_cache:
                delete_generated_data(self._directory, self._prefix)
        except Exception:
            self._release_cache_lock()
            raise
        try:
            self._volume_staging = tempfile.TemporaryDirectory(
                prefix="fumaris_volume_"
            )
            self._worker = BridgeWorker(executable, session, keep_alive=keep_alive)
            self._worker.start()
        except Exception:
            self._cleanup_volume_staging()
            self._release_cache_lock()
            raise

    def _start_modal(self, context):
        window_manager = context.window_manager
        interval = getattr(self, "_timer_interval", 0.001)
        self._next_modal_tick = 0.0
        try:
            self._timer = window_manager.event_timer_add(
                interval,
                window=context.window,
            )
            if self._show_progress:
                window_manager.progress_begin(
                    0,
                    self._end_frame - self._progress_start_frame + 1,
                )
            window_manager.modal_handler_add(self)
        except Exception as error:
            # (a refusal is reported, not kept as the domain's last failure)
            if not isinstance(error, RefusedError):
                self._record_failure(str(error))
            self.cancel(context)
            self.report({"ERROR"}, str(error))
            return {"CANCELLED"}
        return {"RUNNING_MODAL"}

    def cancel(self, context):
        """Blender-driven cancellation must release the same resources as Stop."""
        try:
            domain = bpy.data.objects.get(getattr(self, "_domain_name", ""))
            if domain:
                domain.fumaris.simulation_state = (
                    "stopped" if getattr(self, "_job_mode", None) == "baking" else "idle"
                )
            if getattr(self, "_worker", None):
                self.request_cancel()
        finally:
            self._finish(context)

    def modal(self, context, event):
        if getattr(self, "_finished", False):
            return {"FINISHED"}
        # Escape (when nothing else used it: menus, transforms and text fields
        # come first) stops a bake, a playing preview or a save.
        if event.type == "ESC" and event.value == "PRESS":
            try:
                if self._escape(context):
                    return {"FINISHED"} if getattr(self, "_finished", False) else {"RUNNING_MODAL"}
            except Exception as error:
                console_log(f"Fumaris: Escape failed: {error}")
            return {"PASS_THROUGH"}
        if event.type != "TIMER":
            return {"PASS_THROUGH"}
        # Blender's public Event API does not expose the originating timer.
        # Filter it when available, otherwise throttle shared TIMER events.
        event_timer = getattr(event, "timer", None)
        if event_timer is not None and event_timer != self._timer:
            return {"PASS_THROUGH"}
        now = time.monotonic()
        if now < getattr(self, "_next_modal_tick", 0.0):
            return {"PASS_THROUGH"}
        self._next_modal_tick = now + getattr(self, "_timer_interval", 0.001)

        try:
            return self._modal_tick(context)
        except Exception as error:
            self.request_cancel()
            return self._cancelled(context, {"message": str(error)})

    def _modal_tick(self, context):
        self._sample_memory()
        self._update_diagnostics(context)

        for message_type, data, payload in self._worker.poll():
            if self._handle_worker_message(context, message_type, data, payload):
                continue
            if message_type == READY:
                continue
            if message_type == SESSION_ACCEPTED:
                self._accepted = True
                diagnostics.record(getattr(self, "_diagnostic_key", None), data)
                diagnostics.set_status(getattr(self, "_diagnostic_key", None), "Running")
                reset_started = getattr(self, "_loop_reset_started", None)
                if reset_started is not None:
                    total_ms = (time.perf_counter() - reset_started) * 1000.0
                    flow_ms = float(data.get("session_reset_ms", 0.0))
                    self._last_loop_reset_ms = total_ms
                    self._last_loop_flow_reset_ms = flow_ms
                    console_log(
                        "Fumaris preview loop reset: "
                        f"total={total_ms:.3f}ms flow={flow_ms:.3f}ms"
                    )
                    self._loop_reset_started = None
                if self._stop_requested:
                    if not self._ending_session:
                        self._ending_session = True
                        self._worker.end_session()
                    continue
                if self._in_flight < self._pipeline_depth(context) and self._ready_to_submit(context):
                    try:
                        self._submit_frame(context)
                    except Exception as error:
                        self.request_cancel()
                        return self._cancelled(context, {"message": str(error)})
                continue
            if message_type == FRAME_COMPLETE:
                result = self._frame_complete(context, data, payload)
                if result:
                    return result
                continue
            if message_type == PREVIEW_COMPLETE:
                self._preview_render_submitted = False
                diagnostics.record(getattr(self, "_diagnostic_key", None), data)
                domain = bpy.data.objects.get(self._domain_name)
                self._after_preview_complete(context, domain, data, payload)
                continue
            if message_type == SESSION_COMPLETE:
                if getattr(self, "_ignore_session_complete", False):
                    self._ignore_session_complete = False
                    continue
                self._final_flush_ms = float(
                    data.get("bridge_vdb_final_flush_ms", 0.0)
                )
                if self._final_flush_ms > 0.0:
                    console_log(
                        "Fumaris VDB final flush: "
                        f"{self._final_flush_ms:.3f}ms"
                    )
                return self._complete(context)
            if message_type == CANCELLED and getattr(self, "_stop_now", False):
                return self._stopped_while_writing(context)
            if message_type in {CANCELLED, FAILED}:
                return self._cancelled(context, data)

        if (
            self._accepted
            and self._in_flight < self._pipeline_depth(context)
            and not self._stop_requested
            and not self._ending_session
            and self._ready_to_submit(context)
        ):
            try:
                self._submit_frame(context)
            except Exception as error:
                self.request_cancel()
                return self._cancelled(context, {"message": str(error)})
        return {"RUNNING_MODAL"}

    def _handle_worker_message(self, _context, _message_type, _data, _payload):
        return False

    def request_cancel(self):
        if self._worker:
            self._worker.cancel()

    def _record_failure(self, message):
        self._failed = True
        key = getattr(self, "_diagnostic_key", None)
        diagnostics.add_log(key, "Error: " + message)
        diagnostics.set_status(key, "Failed")
        diagnostics.finish(key)

    def _escape(self, context):
        """Escape pressed: True when it stopped something (overridden)."""
        return False

    def writing_last_frames(self):
        """A bake whose frames are all simulated, waiting while the bridge
        writes the last ones to disk (minutes on a slow disk at high
        resolution): the status says so instead of "0 s left"."""
        return bool(
            getattr(self, "_job_mode", None) == "baking"
            and getattr(self, "_accepted", False)
            and getattr(self, "_ending_session", False)
            and not self._submitted
            and not getattr(self, "_finished", False)
        )

    def request_stop(self, _context=None):
        if self.writing_last_frames():
            # Nothing is left to simulate, only files to write: Stop ends the
            # bridge now. The frames already on disk are kept and imported;
            # the ones still being written are lost.
            self._stop_requested = True
            self._stop_now = True
            self._worker.cancel()
            console_log(f"Fumaris stop requested for {self._domain_name} while writing its last frames")
            return
        self._stop_requested = True
        if self._accepted and not self._submitted and not self._ending_session:
            self._ending_session = True
            self._worker.end_session()
        domain = bpy.data.objects.get(self._domain_name)
        if domain:
            domain.fumaris.simulation_state = "stopped"
        console_log(f"Fumaris stop requested for {self._domain_name}")

    def _submit_frame(self, context):
        domain = bpy.data.objects.get(self._domain_name)
        if domain is None:
            raise RuntimeError("The active Fumaris domain was deleted")

        self._last_frame_submit_started = time.monotonic()
        timing_start = time.perf_counter()
        self._evaluate_frame(context)
        timing_evaluated = time.perf_counter()
        preview_request = (
            capture_volume_preview(context, domain)
            if getattr(self, "_preview_enabled", True)
            else {"valid": False}
        )
        self._last_submitted_volume_preview = preview_request
        from .participants import rebind
        packet = build_frame(
            context,
            self._frame,
            rebind(self._participants),
            domain=domain,
            resolution_scale=getattr(self, "_resolution_scale", 1.0),
            volume_stage_dir=self._volume_staging.name,
            preview_enabled=getattr(self, "_preview_enabled", True),
            volume_preview=preview_request,
        )
        timing_packet = time.perf_counter()
        self._after_frame_packet(packet)
        self._worker.send_frame(packet)
        timing_sent = time.perf_counter()
        self._sample_memory(force=True)
        self._last_submit_timing = {
            "evaluate_ms": (timing_evaluated - timing_start) * 1000.0,
            "packet_ms": (timing_packet - timing_evaluated) * 1000.0,
            "send_ms": (timing_sent - timing_packet) * 1000.0,
            "payload_bytes": len(packet.payload),
        }
        diagnostics.record(getattr(self, "_diagnostic_key", None), {
            "blender_evaluate_ms": self._last_submit_timing["evaluate_ms"],
            "blender_packet_ms": self._last_submit_timing["packet_ms"],
            "blender_send_ms": self._last_submit_timing["send_ms"],
            "payload_bytes": len(packet.payload),
        })
        self._in_flight += 1
        if self._pipeline_depth(context) > 1:
            # The next frame may go out before this one completes.
            self._frame = self._next_frame_after_submit(context, self._frame)

    def _next_frame_after_submit(self, _context, submitted):
        return submitted + 1

    def _after_frame_packet(self, _packet):
        pass

    def _ready_to_submit(self, _context):
        return True

    def _evaluate_frame(self, context):
        context.scene.frame_set(self._frame)

    def _frame_complete(self, context, data, payload):
        domain = bpy.data.objects.get(self._domain_name)
        completed = int(data.get("frame", self._frame))
        self._in_flight = max(0, self._in_flight - 1)
        ahead = self._frame   # frames already sent ahead are not sent again
        self._completed_frames.append(completed)
        self._peak_vram_bytes = max(
            self._peak_vram_bytes,
            int(data.get("flow_device_memory_bytes", 0)),
        )
        self._sample_memory(force=True)
        if domain:
            domain.fumaris.baked_frames = len(self._completed_frames)
            if self._show_progress:
                context.window_manager.progress_update(len(self._completed_frames))

        handler_start = time.perf_counter()
        self._after_frame_complete(context, domain, completed, data, payload)
        handler_ms = (time.perf_counter() - handler_start) * 1000.0
        data["blender_round_trip_ms"] = 1000.0 * (
            time.monotonic() - getattr(self, "_last_frame_submit_started", time.monotonic())
        )
        diagnostics.record(getattr(self, "_diagnostic_key", None), data)
        submit = getattr(self, "_last_submit_timing", {})
        flow_ms = float(data.get("flow_submit_ms", 0.0)) + float(
            data.get("flow_wait_ms", 0.0)
        )
        bridge_ms = float(data.get("frame_ms", 0.0))
        blender_ms = sum((
            submit.get("evaluate_ms", 0.0),
            submit.get("packet_ms", 0.0),
            submit.get("send_ms", 0.0),
            handler_ms,
        ))
        console_log(
            f"Fumaris frame {completed}: "
            f"total={blender_ms + bridge_ms:.3f}ms "
            f"blender={blender_ms:.3f}ms bridge={bridge_ms:.3f}ms "
            f"flow={flow_ms:.3f}ms "
            f"vdb={float(data.get('bridge_vdb_write_ms', 0.0)):.3f}ms "
            f"volume={float(data.get('bridge_volume_load_ms', 0.0)):.3f}ms "
            f"volume_cache={int(data.get('bridge_volume_cache_hits', 0))}/"
            f"{int(data.get('bridge_volume_cache_misses', 0))} "
            f"effector={float(data.get('bridge_effector_setup_ms', 0.0)):.3f}ms "
            f"effector_cache={int(data.get('bridge_effector_cache_hits', 0))}/"
            f"{int(data.get('bridge_effector_cache_misses', 0))} "
            f"payload={int(data.get('payload_bytes', 0))} bytes"
        )
        console_log(
            "  timings: "
            f"eval={submit.get('evaluate_ms', 0.0):.3f}ms "
            f"packet={submit.get('packet_ms', 0.0):.3f}ms "
            f"queue={submit.get('send_ms', 0.0):.3f}ms "
            f"parse={float(data.get('bridge_parse_ms', 0.0)):.3f}ms "
            f"setup={float(data.get('bridge_setup_ms', 0.0)):.3f}ms "
            f"params={float(data.get('bridge_params_ms', 0.0)):.3f}ms "
            f"submit={float(data.get('flow_submit_ms', 0.0)):.3f}ms "
            f"wait={float(data.get('flow_wait_ms', 0.0)):.3f}ms "
            f"readback={float(data.get('bridge_readback_ms', 0.0)):.3f}ms "
            f"preview={float(data.get('bridge_preview_ms', 0.0)):.3f}ms "
            f"preview_upload={float(data.get('blender_preview_upload_ms', 0.0)):.3f}ms "
            f"points={int(data.get('bridge_exact_point_count', 0))} "
            f"point_setup={float(data.get('bridge_exact_point_setup_ms', 0.0)):.3f}ms "
            f"vdb_copy={float(data.get('bridge_vdb_host_copy_ms', 0.0)):.3f}ms "
            f"vdb_copy_rate={float(data.get('bridge_vdb_copy_gbps', 0.0)):.2f}GB/s "
            f"vdb_alloc={float(data.get('bridge_vdb_allocation_ms', 0.0)):.3f}ms "
            f"vdb_slot={float(data.get('bridge_vdb_staging_slot_wait_ms', 0.0)):.3f}ms "
            f"vdb_budget={float(data.get('bridge_vdb_byte_budget_wait_ms', 0.0)):.3f}ms "
            f"vdb_writer={float(data.get('bridge_vdb_queue_wait_ms', 0.0)):.3f}ms "
            f"vdb_convert={float(data.get('bridge_vdb_convert_ms', 0.0)):.3f}ms "
            f"vdb_file={float(data.get('bridge_vdb_file_write_ms', 0.0)):.3f}ms "
            f"vdb_mem={int(data.get('bridge_vdb_in_flight_bytes', 0)) / (1024 * 1024):.1f}MiB "
            f"substeps={int(data.get('flow_effective_substeps', 1))} "
            f"voxel=({float(data.get('flow_density_voxel_size_x', 0.0)):.6g},"
            f"{float(data.get('flow_density_voxel_size_y', 0.0)):.6g},"
            f"{float(data.get('flow_density_voxel_size_z', 0.0)):.6g}) "
            f"effective_res={float(data.get('flow_effective_resolution', 0.0)):.1f} "
            f"blocks={int(data.get('flow_active_blocks', 0))}/"
            f"{int(data.get('flow_active_block_capacity', 0))} "
            f"flow_mem={int(data.get('flow_device_memory_bytes', 0)) / (1024 * 1024):.1f}MiB "
            f"post={handler_ms:.3f}ms"
        )
        profile_frame = int(data.get("flow_gpu_profile_frame", -1))
        if profile_frame >= 0:
            console_log(
                f"  flow gpu frame {profile_frame}: "
                f"total={float(data.get('flow_gpu_total_ms', 0.0)):.3f}ms "
                f"alloc={float(data.get('flow_gpu_allocation_ms', 0.0)):.3f}ms "
                f"density={float(data.get('flow_gpu_density_ms', 0.0)):.3f}ms "
                f"velocity={float(data.get('flow_gpu_velocity_ms', 0.0)):.3f}ms "
                f"pressure={float(data.get('flow_gpu_pressure_ms', 0.0)):.3f}ms "
                f"vorticity={float(data.get('flow_gpu_vorticity_ms', 0.0)):.3f}ms "
                f"emitters={float(data.get('flow_gpu_emitter_ms', 0.0)):.3f}ms "
                f"nanovdb={float(data.get('flow_gpu_nanovdb_ms', 0.0)):.3f}ms "
                f"other={float(data.get('flow_gpu_other_ms', 0.0)):.3f}ms"
            )

        if self._stop_requested:
            if not self._ending_session:
                self._ending_session = True
                self._worker.end_session()
            return None
        if completed >= self._worker._session["end_frame"]:
            return self._end_of_range(context, completed)

        next_frame = self._next_frame_after_complete(context, completed)
        if next_frame is None:
            return None
        self._frame = max(next_frame, ahead) if self._in_flight > 0 else next_frame
        if not self._ready_to_submit(context):
            return None
        try:
            self._submit_frame(context)
        except Exception as error:
            self.request_cancel()
            return self._cancelled(context, {"message": str(error)})
        return None

    def _after_frame_complete(self, _context, _domain, _completed, _data, _payload):
        pass

    def _after_preview_complete(self, _context, _domain, _data, _payload):
        pass

    def _next_frame_after_complete(self, _context, completed):
        return completed + 1

    def _stopped_while_writing(self, context):
        """Stopped while the bridge wrote its last frames: a stopped bake of
        the frames that reached the disk."""
        try:
            on_disk = baked_frame_count(self._directory, self._prefix)
        except OSError:
            on_disk = len(self._completed_frames)
        self._completed_frames = list(self._completed_frames)[:on_disk]
        domain = bpy.data.objects.get(self._domain_name)
        if domain:
            domain.fumaris.baked_frames = len(self._completed_frames)
        return self._complete(context)

    def _end_of_range(self, context, _completed):
        self._ending_session = True
        return None

    def _finish(self, context):
        self._finished = True
        release_job(self)

        # An already-removed timer or failed restoration must not strand the
        # bridge, cache lock, or active-job reference during operator teardown.
        def cleanup(action, *args, **kwargs):
            try:
                return action(*args, **kwargs)
            except Exception as error:
                console_log(f"Fumaris cleanup warning: {error}")
                return None

        cleanup(diagnostics.finish, getattr(self, "_diagnostic_key", None))
        cleanup(clear_preview, getattr(self, "_domain_name", ""))
        # A finished or stopped bake (or a preview's saves) shows in Versions now.
        from .compare import forget_scan
        cleanup(forget_scan)
        # A Save Sequence/Frame still running holds its version's lock.
        pending = getattr(self, "_export_pending", None) or {}
        if pending.get("lock") is not None:
            cleanup(pending["lock"].release)
        if pending:
            cleanup(context.window_manager.progress_end)   # its cursor counter
        self._export_pending = None
        timer = getattr(self, "_timer", None)
        self._timer = None
        if timer:
            cleanup(context.window_manager.event_timer_remove, timer)
        if getattr(self, "_show_progress", False):
            cleanup(context.window_manager.progress_end)
        if getattr(self, "_track_metrics", False):
            cleanup(self._sample_memory, force=True)
        worker = getattr(self, "_worker", None)
        if worker:
            cleanup(worker.close, timeout=0.0 if getattr(self, "_failed", False) else 0.5,
                    wait=not getattr(self, "_close_without_wait", False))
            for line in cleanup(worker.poll_stderr) or ():
                diagnostics.add_log(getattr(self, "_diagnostic_key", None), line)
        cleanup(self._restore_imported_volumes)
        cleanup(self._cleanup_volume_staging)
        cleanup(self._delete_interrupted_writes)
        cleanup(self._release_cache_lock)
        if getattr(self, "_restore_frame", False) and context.scene.frame_current != self._original_frame:
            cleanup(context.scene.frame_set, self._original_frame)

    def _delete_interrupted_writes(self):
        """A stopped bridge can leave the frame it was writing as a .tmp file."""
        if getattr(self, "_cache_lock", None) is None:
            return
        try:
            delete_temporary_writes(self._directory, self._prefix)
        except OSError:
            # Still held by the exiting bridge; the next bake or load removes it.
            pass

    def _sample_memory(self, *, force=False):
        if not self._track_metrics:
            return
        now = time.monotonic()
        if now < self._next_memory_sample:
            return
        self._next_memory_sample = now + 0.5
        blender_bytes = process_memory_bytes(os.getpid())
        worker = getattr(self, "_worker", None)
        bridge_bytes = worker.memory_bytes() if worker else 0
        self._peak_ram_bytes = max(
            self._peak_ram_bytes,
            blender_bytes + bridge_bytes,
        )
        diagnostics.record(getattr(self, "_diagnostic_key", None), {
            "blender_rss_bytes": blender_bytes,
            "bridge_rss_bytes": bridge_bytes,
        })

    def _update_diagnostics(self, context):
        now = time.monotonic()
        if now < getattr(self, "_next_diagnostic_update", 0.0):
            return
        self._next_diagnostic_update = now + 0.25
        worker = getattr(self, "_worker", None)
        if worker:
            for line in worker.poll_stderr():
                diagnostics.add_log(getattr(self, "_diagnostic_key", None), line)
        if context and context.screen:
            for area in context.screen.areas:
                if area.type == "PROPERTIES":
                    area.tag_redraw()

    def _hide_imported_volumes(self):
        if getattr(self, "_hidden_imported_volumes", None) is not None:
            return
        self._hidden_imported_volumes = ((
            self._directory,
            self._prefix,
            hide_generated_volumes(self._directory, self._prefix),
        ),)

    def _restore_imported_volumes(self):
        states = getattr(self, "_hidden_imported_volumes", None)
        if states is None:
            return
        for directory, prefix, volume_states in states:
            restore_generated_volumes(directory, prefix, volume_states)
        self._hidden_imported_volumes = None

    def _cleanup_volume_staging(self):
        staging = getattr(self, "_volume_staging", None)
        if staging is not None:
            staging.cleanup()
            self._volume_staging = None

    def _release_cache_lock(self):
        lock = getattr(self, "_cache_lock", None)
        if lock is not None:
            lock.release()
            self._cache_lock = None

    def _complete(self, _context):
        raise NotImplementedError

    def _cancelled(self, _context, _data):
        raise NotImplementedError


_GPU_LIST_LINE = re.compile(r"Flow device (\d+ name=|selected )")


def stop_message(message, stderr):
    """A stop's report: the reason, then what the bridge said beyond its
    routine GPU list (which read as part of every reason, 2026-11-14) and
    MoltenVK's start-up banner on macOS ("[mvk-info] ..." and its indented
    extension list, which buried the reason, 2026-11-19)."""
    lines = []
    in_banner = False
    for line in (stderr or "").splitlines():
        if line.startswith("[mvk-info]"):
            in_banner = True
            continue
        if in_banner and line[:1] in ("\t", " "):
            continue
        in_banner = False
        # Not the bridge's routine status lines ("Auto Simulation Memory: ...",
        # per-frame timings), which read as part of the reason (2026-11-19
        # review); anything else it said is kept.
        if line.strip() and not _GPU_LIST_LINE.match(line) and not _ROUTINE_LINE.match(line.strip()):
            lines.append(line.strip())
    return f"{message}: {' '.join(lines)}" if lines else message


_ROUTINE_LINE = re.compile(
    r"(Auto Simulation Memory:|Fumaris frame \d+|timings:|Fumaris VDB |Fumaris preview loop reset|"
    r"Fumaris Flow runtime loaded|Fumaris background bake:|Initializing Flow|Fumaris: point cloud|"
    r"Flow GPU labels|PROFILE_LABEL|TRACE )")


def recover_removed_job(operator):
    # Bypass bpy.types.Operator.__getattribute__, which resolves invalid RNA
    # even for Python-only fields. Never invoke methods on the removed wrapper.
    try:
        state = dict(object.__getattribute__(operator, "__dict__"))
        job = FrameRangeJob()
        # A plain attribute, not FrameRangeJob's _domain_name property, on
        # operators that do not inherit FrameRangeJob.
        name = state.pop("_domain_name", None)
        job.__dict__.update(state)
        if name is not None and "_domain_name_value" not in state:
            job._domain_name_value = name
        job._restore_frame = False
        job.cancel(bpy.context)
        console_log("Fumaris recovered a removed simulation operator")
    except Exception as error:
        console_log(f"Fumaris removed-operator cleanup warning: {error}")
