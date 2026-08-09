import os
import tempfile
import time

import bpy

from .bridge import BridgeWorker, process_memory_bytes
from .cache import cache_lock
from .diagnostics import console_log
from .exporters import bridge_executable, build_frame, build_session
from .importers import (
    delete_generated_data,
    hide_generated_volumes,
    migrate_legacy_cache,
    restore_generated_volumes,
)
from .protocol import (
    CANCELLED,
    FAILED,
    FRAME_COMPLETE,
    READY,
    SESSION_ACCEPTED,
    SESSION_COMPLETE,
)
from .runtime import release_job
from .utils import (
    legacy_output_directories_for_object,
    output_directory_for_object,
    runtime_validation_error,
    simulation_frame_range,
)


class FrameRangeJob:
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
        preview_max_points=None,
        resolution_scale=1.0,
        show_progress=True,
        completed_frames=None,
        keep_alive=False,
    ):
        runtime_error = runtime_validation_error()
        if runtime_error:
            raise RuntimeError(runtime_error)
        executable = bridge_executable()
        if executable is None:
            raise RuntimeError("Bridge executable not found")

        self._domain_name = domain.name
        self._directory = output_directory_for_object(domain)
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
        self._show_progress = bool(show_progress)
        self._finished = False
        self._resolution_scale = resolution_scale
        self._progress_start_frame = self._frame
        self._completed_frames = list(completed_frames or [])
        self._cache_lock = None
        self._peak_ram_bytes = 0
        self._peak_vram_bytes = 0
        self._next_memory_sample = 0.0
        self._track_metrics = bool(write_vdb)

        session, self._participants = build_session(
            context,
            start_frame=self._frame,
            end_frame=self._end_frame,
            domain=domain,
            write_vdb=write_vdb,
            preview_enabled=preview_enabled,
            preview_max_points=preview_max_points,
            resolution_scale=resolution_scale,
            log_participants=True,
        )

        self._prefix = session["output_prefix"]
        if not all(character.isalnum() or character in "._-" for character in self._prefix):
            raise RuntimeError(
                "Output prefix may contain only letters, numbers, '.', '_', and '-'"
            )

        os.makedirs(self._directory, exist_ok=True)
        self._legacy_directories = legacy_output_directories_for_object(domain)
        for legacy_directory in self._legacy_directories:
            with cache_lock(legacy_directory):
                migrate_legacy_cache(legacy_directory, self._directory, self._prefix)
                if clear_cache:
                    delete_generated_data(legacy_directory, self._prefix)
        if write_vdb:
            self._cache_lock = cache_lock(self._directory).acquire()
        try:
            if clear_cache:
                delete_generated_data(self._directory, self._prefix)
        except Exception:
            self._release_cache_lock()
            raise
        self._volume_staging = tempfile.TemporaryDirectory(
            prefix="fumaris_volume_"
        )

        self._worker = BridgeWorker(executable, session, keep_alive=keep_alive)
        try:
            self._worker.start()
        except Exception:
            self._cleanup_volume_staging()
            self._release_cache_lock()
            raise

    def _start_modal(self, context):
        window_manager = context.window_manager
        interval = getattr(self, "_timer_interval", 0.001)
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
        return {"RUNNING_MODAL"}

    def modal(self, context, event):
        if getattr(self, "_finished", False):
            return {"FINISHED"}
        if event.type != "TIMER":
            return {"PASS_THROUGH"}

        self._sample_memory()

        for message_type, data, payload in self._worker.poll():
            if message_type == READY:
                continue
            if message_type == SESSION_ACCEPTED:
                self._accepted = True
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
                if not self._submitted and self._ready_to_submit(context):
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
            if message_type in {CANCELLED, FAILED}:
                return self._cancelled(context, data)

        if self._accepted and not self._submitted and self._ready_to_submit(context):
            try:
                self._submit_frame(context)
            except Exception as error:
                self.request_cancel()
                return self._cancelled(context, {"message": str(error)})
        return {"RUNNING_MODAL"}

    def request_cancel(self):
        if self._worker:
            self._worker.cancel()

    def request_stop(self, _context=None):
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
        packet = build_frame(
            context,
            self._frame,
            self._participants,
            domain=domain,
            resolution_scale=getattr(self, "_resolution_scale", 1.0),
            volume_stage_dir=self._volume_staging.name,
        )
        timing_packet = time.perf_counter()
        self._worker.send_frame(packet)
        timing_sent = time.perf_counter()
        self._sample_memory(force=True)
        self._last_submit_timing = {
            "evaluate_ms": (timing_evaluated - timing_start) * 1000.0,
            "packet_ms": (timing_packet - timing_evaluated) * 1000.0,
            "send_ms": (timing_sent - timing_packet) * 1000.0,
            "payload_bytes": len(packet.payload),
        }
        self._submitted = True

    def _ready_to_submit(self, _context):
        return True

    def _evaluate_frame(self, context):
        context.scene.frame_set(self._frame)

    def _frame_complete(self, context, data, payload):
        domain = bpy.data.objects.get(self._domain_name)
        completed = int(data.get("frame", self._frame))
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
            self._submitted = False
            return None
        self._frame = next_frame
        self._submitted = False
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

    def _next_frame_after_complete(self, _context, completed):
        return completed + 1

    def _end_of_range(self, context, _completed):
        self._ending_session = True
        return None

    def _finish(self, context):
        self._finished = True
        if self._timer:
            context.window_manager.event_timer_remove(self._timer)
            self._timer = None
        if self._show_progress:
            context.window_manager.progress_end()
        self._sample_memory(force=True)
        if getattr(self, "_worker", None):
            self._worker.close()
        self._restore_imported_volumes()
        self._cleanup_volume_staging()
        self._release_cache_lock()
        release_job(self)
        if self._restore_frame and context.scene.frame_current != self._original_frame:
            context.scene.frame_set(self._original_frame)

    def _sample_memory(self, *, force=False):
        if not self._track_metrics:
            return
        now = time.monotonic()
        if not force and now < self._next_memory_sample:
            return
        self._next_memory_sample = now + 0.1
        blender_bytes = process_memory_bytes(os.getpid())
        worker = getattr(self, "_worker", None)
        bridge_bytes = worker.memory_bytes() if worker else 0
        self._peak_ram_bytes = max(
            self._peak_ram_bytes,
            blender_bytes + bridge_bytes,
        )

    def _hide_imported_volumes(self):
        if getattr(self, "_hidden_imported_volumes", None) is not None:
            return
        self._hidden_imported_volumes = tuple(
            (
                directory,
                self._prefix,
                hide_generated_volumes(directory, self._prefix),
            )
            for directory in (
                self._directory,
                *getattr(self, "_legacy_directories", ()),
            )
        )

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
