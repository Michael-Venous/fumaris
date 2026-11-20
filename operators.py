import hashlib
import json
import os
import re
import time

import bpy
from bpy.app.handlers import persistent
from bpy.props import BoolProperty, StringProperty
from .gn_sources import SOURCES as GN_SOURCES
from bpy.types import Operator

from .bake_state import mark_bake_cancelled, mark_bake_complete, mark_bake_running
from .cache import (
    RefusedError,
    cache_lock,
    is_protected,
    next_slot_name,
    read_settings_snapshot,
    apply_settings_snapshot,
    write_settings_snapshot,
    SETTINGS_FILE,
    require_unprotected,
    set_protected,
    recover_cache_lock,
)
from .cache_revision import BakeReplacement, recover_replacement
from .diagnostics import console_log
from . import diagnostics
from .exporters import build_frame, build_session, session_structure_signature
from .importers import (
    VOLUME_MARKER,
    baked_frame_count,
    delete_generated_data,
    delete_temporary_writes,
    domain_volumes,
    import_sequence,
    _frame_files,
)
from .jobs import FrameRangeJob, stop_message, writing_last_frames
from .protocol import PREVIEW_CACHE_EXPORTED, PREVIEW_RELEASED
from .preview import (
    capture_volume_preview,
    preview_targets,
    clear_all_previews,
    clear_preview,
    preview_image_pending,
    show_preview_payload,
    volume_preview_signature,
)
from .utils import (
    cache_slot_directory,
    ensure_cache_owner,
    owns_cache,
    output_directory_for_object,
    peek_output_directory_for_object,
    simulation_frame_range,
)
from .runtime import (
    GPU_INITIALIZATION_MESSAGE,
    gpu_notice_due,
    active_job,
    active_mode,
    blocking_job,
    release_parked_preview,
    background_bake,
    background_job,
    claim_job,
    clear_geometry_revisions,
    record_geometry_updates,
    release_job,
)

# How often a paused or playing preview looks for a changed view or setting.
# A request is cheap (~0.05 ms) and only one is in flight, so the bridge sets
# the pace; 30 Hz here capped orbiting at 30 fps at any preview size.
PREVIEW_CAPTURE_INTERVAL = 1.0 / 250.0
# While playing, a changed view is rerendered between simulation frames when
# the rerender (this many times its measured time) fits before the next frame
# is due; once the frame is due, at most once per frame. Playback of a 512
# Rising Smoke at 24 fps while orbiting at 144 Hz: 48 -> 131 images/s, view to
# image 23 -> 8 ms, simulation unchanged (output/preview_sched, 2026-11-08).
PREVIEW_RERENDER_MARGIN = 1.5
PREVIEW_RERENDER_DEFAULT_SECONDS = 0.005


SAVE_CANCEL_FILE = ".fumaris_cancel"   # the bridge stops a save when it appears


class FUMARIS_OT_bake(FrameRangeJob, Operator):
    bl_idname = "fumaris.bake"
    bl_label = "Bake"
    bl_description = "Simulate the whole frame range and write it to VDB files, imported as a volume object for rendering"

    _timer = None
    _worker = None
    _directory = ""
    _frame = 0
    _end_frame = 0
    _original_frame = 0
    _restore_frame = True
    _participants = ()
    _prefix = "fumaris_"
    _started_at = 0.0
    _completed_frames = ()
    _timer_interval = 0.001

    replace_confirmed: BoolProperty(options={"HIDDEN", "SKIP_SAVE"})
    replacement_slot: StringProperty(options={"HIDDEN", "SKIP_SAVE"})

    def _escape(self, context):
        """Escape: like Stop (keeps and imports the frames written so far)."""
        domain = bpy.data.objects.get(self._domain_name)
        if not getattr(self, "_accepted", False):
            self._stop_requested = True
            replacing = getattr(self, "_replacement", None) is not None
            _cancel_job(context, self)
            if domain and not replacing:
                mark_bake_cancelled(domain)
                self._store_bake_stats(domain)
        elif not getattr(self, "_stop_requested", False):
            self.request_stop(context)
        for area in context.screen.areas if context.screen else ():
            area.tag_redraw()
        return True

    @classmethod
    def poll(cls, context):
        domain = _active_domain(context)
        if background_job(domain):
            cls.poll_message_set("This domain is baking in the background")
            return False
        return bool(domain and not blocking_job())

    def invoke(self, context, _event):
        # Bake writes into the domain's current slot, clearing it first. With
        # a pinned bake (Versions) it asks: replace that one, or a new one.
        # The first bake needs no question. Scripts calling the operator
        # directly bake into the current slot, unasked.
        domain = _active_domain(context)
        if domain and not self.replace_confirmed:
            from .compare import bake_target, new_bake_slot
            target = bake_target(domain)
            if target is not None:
                bpy.ops.fumaris.bake_choose("INVOKE_DEFAULT", target=target["name"])
                return {"CANCELLED"}
            directory = output_directory_for_object(domain)
            if baked_frame_count(directory, domain.fumaris.output_prefix or "fumaris_") or is_protected(directory):
                # The current slot holds something that is not a pinned bake
                # (a preview save): keep it and bake into a new slot.
                domain.fumaris.cache_slot = new_bake_slot(domain)
        return self.execute(context)

    def execute(self, context):
        domain = _active_domain(context)
        if not domain:
            self.report({"ERROR"}, "Select a Fumaris domain")
            return {"CANCELLED"}

        self._domain_name = domain.name
        try:
            from .bridge import require_compatible_bridge
            from .jobs import bridge_executable
            executable = bridge_executable()
            if executable is None:
                raise RuntimeError("Bridge executable not found")
            require_compatible_bridge(executable)
            slot = self.replacement_slot or domain.fumaris.cache_slot
            require_unprotected(cache_slot_directory(domain, slot), "bake over it")
            ensure_cache_owner(domain)
            directory = cache_slot_directory(domain, slot)
            prefix = domain.fumaris.output_prefix or "fumaris_"
            props = domain.fumaris
            self._previous_bake = {key: getattr(props, key) for key in (
                "simulation_state", "baked_frames", "bake_elapsed", "bake_peak_vram_mb", "bake_peak_ram_mb")}
            self._replacement = BakeReplacement.begin(directory, prefix)
            claim_job(self, "baking")
            clear_all_previews()
            self._configure_job(
                context,
                domain,
                clear_cache=True,
                write_vdb=True,
                preview_enabled=bool(domain.fumaris.preview_bake),
                show_progress=True,
                output_directory=self._replacement.directory if self._replacement else directory,
                require_empty=True,
            )
        except Exception as error:
            # (a refusal is reported, not kept as the domain's last failure)
            if not isinstance(error, RefusedError):
                self._record_failure(str(error))
            release_job(self)
            self._restore_imported_volumes()
            self._cleanup_volume_staging()
            self._release_cache_lock()
            if getattr(self, "_replacement", None) is not None:
                self._settle_replacement(domain, False)
            self.report({"ERROR"}, str(error))
            return {"CANCELLED"}

        try:
            write_settings_snapshot(self._directory, domain, self._participants)
        except Exception as error:
            console_log(f"Fumaris could not store the bake settings: {error}")
        mark_bake_running(domain)
        domain.fumaris.baked_frames = 0
        domain.fumaris.bake_elapsed = 0.0
        domain.fumaris.bake_peak_vram_mb = 0.0
        domain.fumaris.bake_peak_ram_mb = 0.0
        if gpu_notice_due():
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
        replacing = getattr(self, "_replacement", None) is not None
        self._finish(context, keep_replacement=True)
        if replacing:
            if domain:
                self._store_bake_stats(domain)
            try:
                partial = self._settle_replacement(domain, not self._stop_requested)
            except Exception as error:
                partial = self._settle_replacement(domain, False)
                message = f"Could not replace the bake: {error}"
                if partial:
                    message += f". Partial output saved as '{os.path.basename(partial)}'"
                self._record_failure(message)
                self.report({"ERROR"}, message)
                return {"CANCELLED"}
            if self._stop_requested:
                text = "Fumaris bake stopped. The previous bake is kept"
                if partial:
                    text += f". Partial output saved as '{os.path.basename(partial)}'"
                self.report({"INFO"}, text)
                return {"FINISHED"}
        if domain and self.replacement_slot and domain.fumaris.cache_slot != self.replacement_slot:
            stats = {key: getattr(domain.fumaris, key) for key in (
                "bake_elapsed", "bake_peak_vram_mb", "bake_peak_ram_mb")}
            domain.fumaris.cache_slot = self.replacement_slot
            for key, value in stats.items():
                setattr(domain.fumaris, key, value)
            domain.fumaris.compare_selected = "vdb:" + self.replacement_slot
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
            if not replacing:
                self._store_bake_stats(domain)
            _activate(context, domain)
            from .sparks import problems
            for message in problems(domain):
                self.report({"ERROR"}, message)
        text = "Fumaris bake stopped" if self._stop_requested else "Fumaris bake complete"
        # The block pool filled: the frames are written, but the smoke is cut off.
        capacity = diagnostics.capacity_message(diagnostics.snapshot(getattr(self, "_diagnostic_key", None)))
        self.report({"WARNING"} if capacity else {"INFO"}, f"{text}. {capacity}" if capacity else text)
        return {"FINISHED"}

    def _cancelled(self, context, data):
        self._record_failure(data.get("message", "Bake failed"))
        domain = bpy.data.objects.get(self._domain_name)
        replacing = getattr(self, "_replacement", None) is not None
        if domain:
            clear_preview(domain)
            mark_bake_cancelled(domain)
        self._finish(context, keep_replacement=True)
        if domain:
            self._store_bake_stats(domain)
        partial = self._settle_replacement(domain, False) if replacing else ""
        message = stop_message(data.get("message", "Bake stopped"),
                               data.get("stderr") or self._worker.stderr())
        if partial:
            message += f". Partial output saved as '{os.path.basename(partial)}'"
        console_log(f"Fumaris bake stopped: {message}")
        self.report({"ERROR"}, message)
        return {"CANCELLED"}

    def _store_bake_stats(self, domain):
        self._sample_memory(force=True)
        props = domain.fumaris
        props.bake_elapsed = time.monotonic() - self._started_at
        props.bake_peak_vram_mb = self._peak_vram_bytes / (1024.0 * 1024.0)
        props.bake_peak_ram_mb = self._peak_ram_bytes / (1024.0 * 1024.0)
        # Kept with the bake (its cost in View Changes); a bake is pinned.
        try:
            from .compare import write_bake_stats, write_pinned
            state = diagnostics.snapshot(getattr(self, "_diagnostic_key", None))
            write_bake_stats(self._directory, props, (state or {}).get("peak_blocks", 0))
            write_pinned(self._directory, True)
        except OSError as error:
            console_log(f"Fumaris could not store bake statistics: {error}")


# --- Replay -------------------------------------------------------------------
# A looping cached preview replays its cache instead of simulating when
# nothing it was simulated from has changed. Each frame's fingerprint is what
# the simulation received (the frame packet without the view); a pass also
# keeps the fingerprint of the settings it started with, to notice changes
# made during it.

def packet_digest(packet):
    """What the simulation receives in a frame, independent of the view: the
    header without the viewport request, each payload array by its content
    (the view's depth image comes first in a previewed frame, shifting every
    offset)."""
    payload = memoryview(bytes(packet.payload))
    starts = set()

    def offsets(node):
        if isinstance(node, dict):
            if isinstance(node.get("offset"), int):
                starts.add(node["offset"])
            for value in node.values():
                offsets(value)
        elif isinstance(node, (list, tuple)):
            for value in node:
                offsets(value)
    offsets(packet.header)
    bounds = sorted(starts | {len(payload)})
    ends = {start: next((b for b in bounds if b > start), len(payload)) for start in starts}

    def canonical(node):
        if isinstance(node, dict):
            if isinstance(node.get("offset"), int) and "count" in node:
                if node.get("reuse"):
                    return {"reuse": True}
                if not node["count"]:
                    return {"count": 0}
                start = node["offset"]
                return {"count": node["count"],
                        "bytes": hashlib.blake2b(payload[start:ends[start]], digest_size=16).hexdigest()}
            return {key: canonical(value) for key, value in node.items()}
        if isinstance(node, (list, tuple)):
            return [canonical(value) for value in node]
        return node
    header = {key: canonical(value) for key, value in packet.header.items()
              if key not in ("volume_preview", "preview_mode")}
    return hashlib.blake2b(json.dumps(header, sort_keys=True, default=str).encode(), digest_size=16).hexdigest()


def _fumaris_animation(obj):
    """obj's animated Fumaris settings: their names, and their keys and
    drivers (the animation as authored, the same on every frame)."""
    anim = getattr(obj, "animation_data", None)
    if anim is None:
        return set(), []
    curves = list(anim.drivers)
    action = anim.action
    if action is not None:
        layered = False
        for layer in getattr(action, "layers", ()):
            for strip in layer.strips:
                for bag in getattr(strip, "channelbags", ()):
                    layered = True
                    curves.extend(bag.fcurves)
        if not layered and hasattr(action, "fcurves"):
            curves.extend(action.fcurves)
    names, keys = set(), []
    for curve in curves:
        path = curve.data_path
        if path.startswith("fumaris."):
            path = path[len("fumaris."):]
            names.add(path.split(".")[0].split("[")[0])
        elif not path.startswith("fumaris_particles."):
            continue
        driver = getattr(curve, "driver", None)
        keys.append((path, curve.array_index, curve.mute,
                     [(tuple(key.co), key.interpolation, tuple(key.handle_left), tuple(key.handle_right))
                      for key in curve.keyframe_points],
                     driver.expression if driver is not None else None))
    return names, sorted(keys, key=str)


def settings_digest(domain, participants, resolution_scale):
    """The simulation settings of the domain and its sources (no look, no
    view). Animated settings count by their keys, not this frame's value, so
    playing through the animation is not a change."""
    from . import detail_stack, particles
    from .cache import _rna_values
    from .exporters import _domain_state
    from .presets import _belongs
    sources, animation = {}, {}
    for item in participants:
        obj = item.get("object") if isinstance(item, dict) else item
        props = getattr(obj, "fumaris", None)
        if props is not None:
            animated, animation[obj.name] = _fumaris_animation(obj)
            sources[obj.name] = {key: value for key, value in _rna_values(props).items()
                                 if _belongs(key, "source") and key not in animated}
    animated, animation[""] = _fumaris_animation(domain)
    state = {key: value for key, value in _domain_state(domain.fumaris, resolution_scale).items()
             if key not in animated}
    # The packet exports particle modifiers separately from the domain. Use
    # their settings here too, including source collection and mask changes.
    animated_paths = {curve[0] for curve in animation[""]}
    particle_system = {key: value for key, value in particles.system_to_dict(domain).items()
                       if f"fumaris_particles.{key}" not in animated_paths}
    layers = []
    for index, layer in enumerate(detail_stack.to_list(domain.fumaris)):
        if layer["layer_type"] != "PARTICLES":
            continue
        path = f"detail_layers[{index}]"
        fields = layer.pop("particles")
        masks = layer.pop("masks")
        layers.append({
            **{key: value for key, value in layer.items() if f"{path}.{key}" not in animated_paths},
            "particles": {key: value for key, value in fields.items()
                          if f"{path}.particles.{key}" not in animated_paths},
            "masks": [{key: value for key, value in mask.items()
                       if f"{path}.masks[{i}].{key}" not in animated_paths}
                      for i, mask in enumerate(masks)],
        })
    data = {"domain": state, "sources": sources, "animation": animation,
            "particle_system": particle_system, "particle_layers": layers}
    return hashlib.blake2b(json.dumps(data, sort_keys=True, default=str).encode(), digest_size=16).hexdigest()


_ranges_parsed = {"text": None, "frames": frozenset()}
# "first-last:step" or "frame"; frames may be negative (warmup).
_RANGE = re.compile(r"(-?\d+)(?:-(-?\d+):(\d+))?")


def cached_frames(job):
    """The frames the live preview's cache holds (the bridge's "ranges")."""
    text = str((getattr(job, "_preview_cache", None) or {}).get("ranges", "") or "")
    if text != _ranges_parsed["text"]:
        frames = set()
        for part in filter(None, text.split(",")):
            match = _RANGE.fullmatch(part.strip())
            if not match:
                continue
            first, last, step = match.groups()
            if last is None:
                frames.add(int(first))
            else:
                frames.update(range(int(first), int(last) + 1, max(1, int(step))))
        _ranges_parsed.update(text=text, frames=frozenset(frames))
    return _ranges_parsed["frames"]


_settings_now = {"key": None, "at": 0.0, "digest": None}


def stale_frames(job, domain):
    """Cached frames made by a run that mixed settings, or with settings that
    are no longer the current ones (the next pass simulates and replaces them)."""
    frames = cached_frames(job)
    if not frames or domain is None:
        return frozenset()
    now = time.monotonic()
    key = (id(job), domain.name)
    if _settings_now["key"] != key or now - _settings_now["at"] > 0.5:
        _settings_now.update(key=key, at=now, digest=settings_digest(
            domain, getattr(job, "_participants", ()), getattr(job, "_resolution_scale", 1.0)))
    current = _settings_now["digest"]
    runs = getattr(job, "_frame_runs", {})
    settings = getattr(job, "_frame_settings", {})
    stale_runs = getattr(job, "_stale_runs", set())
    return frozenset(frame for frame in frames
                     if runs.get(frame) in stale_runs or settings.get(frame, current) != current)


def tag_timelines(context):
    """Redraw every Timeline (each window), for the preview cache strip."""
    manager = getattr(context, "window_manager", None) or bpy.context.window_manager
    for window in getattr(manager, "windows", ()):
        for area in window.screen.areas if window.screen else ():
            if area.type == "DOPESHEET_EDITOR":
                area.tag_redraw()


class FUMARIS_OT_preview_play(FrameRangeJob, Operator):
    bl_idname = "fumaris.preview_play"
    bl_label = "Play"
    bl_description = "Simulate and show the smoke live in the viewport; its frames are cached for scrubbing and export while paused"

    _timer = None
    _worker = None
    _directory = ""
    _frame = 0
    _end_frame = 0
    _original_frame = 0
    _restore_frame = False
    _participants = ()
    _prefix = "fumaris_"
    _started_at = 0.0
    _completed_frames = ()
    _session_signature = None
    _timer_interval = 0.001
    _next_submit_at = 0.0

    domain_name: bpy.props.StringProperty(options={'HIDDEN', 'SKIP_SAVE'})

    @classmethod
    def description(cls, context, _properties):
        from .playback import preview_description
        return preview_description(context, "play")

    @classmethod
    def poll(cls, context):
        from .playback import scene_domains
        return not blocking_job() and any(
            domain.fumaris.simulation_state != "baking"
            for domain in scene_domains(context)
        )

    def execute(self, context):
        name = getattr(self, "domain_name", "")
        domain = (context.view_layer.objects.get(name) if name
                  else _active_domain(context) or _playback_domain(context))
        if not domain or domain.fumaris.smoke_object_type != 'domain':
            self.report({"ERROR"}, "Select a Fumaris domain")
            return {"CANCELLED"}
        if blocking_job() or domain.fumaris.simulation_state == "baking":
            self.report({"WARNING"}, "Stop the active job before previewing")
            return {"CANCELLED"}
        job = background_job(domain)
        if job:
            waiting = "is queued to bake" if job.state == "queued" else "is baking"
            self.report({"WARNING"}, f"'{domain.name}' {waiting} in the background: stop that bake or wait for it")
            return {"CANCELLED"}
        if _is_playing(context):
            self.report({"WARNING"}, "Stop Blender playback before starting Fumaris preview")
            return {"CANCELLED"}

        if not capture_volume_preview(context, domain).get("valid", False):
            self.report({"WARNING"}, "Open a 3D Viewport before starting Fumaris preview")
            return {"CANCELLED"}

        previous_slot = domain.fumaris.cache_slot
        if domain.fumaris.simulation_state == "baked":
            # The bake stays in Versions (its eye shows it again); the preview
            # runs in an empty slot, so nothing is written over it.
            from .compare import free_slot_name
            prefix = domain.fumaris.output_prefix or "fumaris_"
            domain.fumaris.cache_slot = free_slot_name(
                "live", lambda slot: bool(baked_frame_count(cache_slot_directory(domain, slot), prefix)))
        from .playback import choose_domain
        choose_domain(context.scene, domain)
        self._domain_name = domain.name
        self._pause_requested = False
        self._loop_reset_pending = False
        try:
            claim_job(self, "previewing")
            clear_all_previews()
            self._start_preview_session(context, domain, clear_preview_image=True)
        except Exception as error:
            if not isinstance(error, RefusedError):
                self._record_failure(str(error))
            self._finish(context)
            if domain.fumaris.cache_slot != previous_slot:
                domain.fumaris.cache_slot = previous_slot
            self.report({"ERROR"}, str(error))
            return {"CANCELLED"}

        domain.fumaris.baked_frames = 0
        if gpu_notice_due():
            self.report({"INFO"}, GPU_INITIALIZATION_MESSAGE)
        console_log(f"Fumaris preview started for {domain.name} frame {self._frame}")
        return self._start_modal(context)

    def _start_preview_session(self, context, domain, *, clear_preview_image=False, require_emitter=True):
        signature = session_structure_signature(domain)
        if clear_preview_image:
            clear_preview(domain)
        self._preview_owner_directory = output_directory_for_object(domain)
        self._preview_cache = {}
        self._live_frame = None
        self._frame_fingerprints = {}
        # Per cached frame: the pass (run) that simulated it and its settings.
        self._frame_runs = {}
        self._frame_settings = {}
        self._run_id = 1
        self._stale_runs = set()
        self._replaying = False
        self._replay_waiting = False
        self._pass_settings = None
        self._pass_stale_frame = None
        pending = getattr(self, "_export_pending", None) or {}
        if pending.get("lock") is not None:
            pending["lock"].release()
        self._export_pending = None
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
            require_emitter=require_emitter,
        )
        self._hide_imported_volumes()
        self._session_signature = signature
        self._next_submit_at = 0.0
        self._preview_render_submitted = False
        self._preview_display_deadline = 0.0
        self._preview_rerendered_since_frame = False
        self._has_volume_preview_frame = False
        self._last_preview_signature = None
        self._next_preview_capture_at = 0.0
        self._preview_render_count = 0

    def _finish(self, context):
        # However the preview ends (Stop, the end, an error), its cache strip
        # leaves the Timeline now rather than at the next redraw.
        self._preview_cache = {}
        super()._finish(context)
        tag_timelines(context)

    def _escape(self, context):
        """Escape: cancels a save; stops a playing preview (like Stop). A paused
        preview leaves Escape to Blender (it stops timeline playback)."""
        pending = getattr(self, "_export_pending", None)
        if pending:
            if not pending.get("cancelled"):
                pending["cancelled"] = True
                # The bridge reads no messages while it writes; it checks for
                # this file before each frame and removes what it wrote.
                try:
                    with open(os.path.join(pending["directory"], SAVE_CANCEL_FILE), "w"):
                        pass
                except OSError as error:
                    console_log(f"Fumaris could not cancel the save: {error}")
                diagnostics.set_status(self._diagnostic_key, "Cancelling the save")
            return True
        if getattr(self, "_pause_requested", False):
            return False
        if getattr(self, "can_park", None) is not None and self.can_park(context):
            self.park(context)   # like Stop: the cached frames stay (F8)
        else:
            _cancel_job(context, self)
        for area in context.screen.areas if context.screen else ():
            area.tag_redraw()
        return True

    def _restart_preview_session(self, context, domain, keep_cache=True):
        """Simulate the range again. The cache stays and the new pass replaces
        frames as it reaches them (keep_cache False starts it empty)."""
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
            require_emitter=False,
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
        session["preview_cache_keep"] = bool(keep_cache)
        if not keep_cache:
            self._frame_fingerprints = {}
            self._frame_runs, self._frame_settings, self._stale_runs = {}, {}, set()
            self._preview_cache = {}
        self._run_id = getattr(self, "_run_id", 0) + 1
        self._replaying = False
        self._replay_waiting = False
        self._pass_settings = None
        self._pass_stale_frame = None
        self._live_frame = None
        self._worker.reset_session(session)

    def restart_in_place(self, context):
        """Restart from the start frame in the running bridge (a session reset,
        a few ms) instead of a new bridge process (about 1 s: process start,
        GPU setup, the block pool). False when it cannot: the session's
        structure or the GPU changed, which need a new session."""
        domain = bpy.data.objects.get(self._domain_name)
        if domain is None or getattr(self, "_worker", None) is None or not getattr(self, "_accepted", False):
            return False
        if getattr(self, "_parked", False):
            return False   # its simulation was released: a new bridge starts over
        if session_structure_signature(domain) != self._session_signature:
            return False
        from .utils import get_addon_preferences
        if getattr(get_addon_preferences(), "gpu_device_uuid", "") != getattr(self._worker, "_gpu_uuid", ""):
            return False   # another GPU chosen: only a new bridge process uses it
        if getattr(self, "_export_pending", None):
            return False   # saving a version reads the cache this would drop
        # Frames may be in flight: send no more and reset once they are back
        # (_ready_to_submit); resetting under them left the job waiting.
        self._restart_pending = True
        self._loop_reset_pending = False
        if getattr(self, "_pause_requested", False):
            self.set_paused(context, False)
        self._ready_to_submit(context)
        return True

    def _restart_now(self, context, domain):
        self._restart_pending = False
        self._restart_preview_session(context, domain, keep_cache=False)
        context.scene.frame_set(self._frame)
        console_log(f"Fumaris preview restarted for {domain.name}")

    def _replayable(self, domain):
        """The cache holds the whole range (at its step) from a pass nothing
        has changed since."""
        if not self._worker._session.get("preview_cache") or self._pass_stale_frame is not None:
            return False
        if self._pass_settings is not None and self._pass_settings != settings_digest(
                domain, self._participants, getattr(self, "_resolution_scale", 1.0)):
            return False   # changed while paused at the end
        start, end = simulation_frame_range(domain)
        frames = cached_frames(self)
        if start not in frames:
            return False   # the oldest frames rolled out (RAM limit), or none yet
        if stale_frames(self, domain):
            return False   # some come from a run that mixed settings: simulate and replace them
        step = int(self._worker._session.get("preview_cache_step", 1) or 0)
        if step <= 0:
            return True    # Auto: whatever it kept, held between
        # Every kept frame of the range; the stats trail the last copies by a
        # frame or two, which the bridge finishes before it draws one.
        needed = range(start, end - 2 * step + 1, step)
        return all(frame in frames for frame in needed)

    def _next_pass(self, context, domain):
        """At the end of the range with Loop on: replay the cache when it is
        whole and current; simulate again otherwise (an empty cache when this
        run mixed settings, else the oldest frames roll out as it goes)."""
        if self._pass_stale_frame is not None:
            # Simulate again; its frames replace the stale ones as it goes (the
            # cache is only ever emptied by the RAM limit).
            console_log(f"Fumaris preview: settings changed at frame {self._pass_stale_frame}; simulating again")
            self._restart_preview_session(context, domain, keep_cache=True)
        elif self._replayable(domain):
            self._begin_replay(context, domain)
        else:
            self._restart_preview_session(context, domain, keep_cache=True)

    def _begin_replay(self, context, domain):
        from .participants import participants as gather_participants
        start, _end = simulation_frame_range(domain)
        self._replaying = True
        self._replay_waiting = False
        self._replay_participants = gather_participants(domain.fumaris)
        self._frame = start
        self._submitted = False
        self._loop_reset_pending = False
        self._pass_stale_frame = None
        self._next_submit_at = 0.0
        self._replay_clock = (time.monotonic(), start)   # replay keeps to the preview frame rate
        diagnostics.set_status(self._diagnostic_key, "Replaying the cache")
        console_log(f"Fumaris preview replays its cache for {self._domain_name}")

    def _submit_replay_frame(self, context, domain):
        """One replayed frame: the cached frame drawn with the live view, and
        the frame's fingerprint checked against the one it was cached with."""
        self._last_frame_submit_started = time.monotonic()
        self._evaluate_frame(context)
        packet = build_frame(
            context, self._frame, self._replay_participants, domain=domain,
            resolution_scale=getattr(self, "_resolution_scale", 1.0),
            volume_stage_dir=self._volume_staging.name, preview_enabled=True,
            volume_preview={"valid": False},
        )
        if self._frame_fingerprints.get(self._frame) != packet_digest(packet):
            # Changed since it was cached: simulate again from the start now,
            # so the change shows at once (as without a cache). The cache is
            # kept; its frames are stale until the new pass replaces them.
            console_log(f"Fumaris preview: changed since frame {self._frame} was cached; simulating again")
            self._stale_runs.update(self._frame_runs.values())
            self._restart_preview_session(context, domain, keep_cache=True)
            return
        preview = capture_volume_preview(context, domain)
        self._submitted = True
        self._replay_waiting = True
        if not preview.get("valid", False):
            # No viewport to draw into: the replay still moves on.
            self._replay_frame_done(context, domain)
            return
        preview.update(cache_frame=self._frame, cache_frame_enabled=True)
        self._last_submitted_volume_preview = preview
        self._worker.send_preview(preview)
        self._preview_sent_at = time.monotonic()
        self._preview_render_submitted = True

    def _replay_frame_done(self, context, domain):
        self._replay_waiting = False
        self._submitted = False
        self._has_volume_preview_frame = True
        self._view_signatures = {}   # every other viewport draws the new frame
        self._last_preview_signature = volume_preview_signature(
            getattr(self, "_last_submitted_volume_preview", None))
        started = getattr(self, "_last_frame_submit_started", time.monotonic())
        self._next_submit_at = started + _preview_frame_duration(
            context.scene, domain.fumaris if domain else None)
        if self._stop_requested:
            if not self._ending_session:
                self._ending_session = True
                self._worker.end_session()
            return
        _start, end = simulation_frame_range(domain)
        if self._frame >= end:
            self._end_of_range(context, self._frame)
            return
        # Like Blender's own playback (the scene's Sync): Play Every Frame
        # slows when drawing falls behind the preview frame rate (no catching
        # up, which would leave the bridge no time to prefetch the next cached
        # frame); Frame Dropping and Sync to Audio skip ahead to the frame due.
        duration = _preview_frame_duration(context.scene, domain.fumaris if domain else None)
        frame = self._frame + 1
        clock = getattr(self, "_replay_clock", None)
        if duration > 0.0 and clock is not None:
            now = time.monotonic()
            due = clock[1] + int((now - clock[0]) / duration)
            if context.scene.sync_mode != "NONE":
                frame = max(frame, due)
            elif now > clock[0] + (frame - clock[1]) * duration:
                clock = self._replay_clock = (now, frame)
        self._frame = min(frame, end)
        if duration > 0.0 and clock is not None:
            # Due on the replay's own clock, not a frame after the last one
            # started: timer latency would add up into skipped frames.
            self._next_submit_at = clock[0] + (self._frame - clock[1]) * duration

    def _pipeline_depth(self, context):
        """Two frames in flight while playing every frame as fast as the GPU
        goes (Frame Rate Uncapped, no frame dropping): the bridge then runs
        the next frame's CPU work during this frame's render. Replays,
        rerenders, pauses, loop resets and the last frame keep to one."""
        domain = bpy.data.objects.get(self._domain_name)
        if (
            domain is None
            or not getattr(self, "_preview_enabled", True)
            or getattr(self, "_replaying", False)
            or getattr(self, "_pause_requested", False)
            or getattr(self, "_loop_reset_pending", False)
            or getattr(self, "_export_pending", None)
            or self._stop_requested
            or self._frame > self._end_frame
            or context.scene.sync_mode != "NONE"
            or _preview_frame_duration(context.scene, domain.fumaris) > 0.0
        ):
            return 1
        return 2

    def _ready_to_submit(self, context):
        if getattr(self, "_parked", False) and self._parked_stale(context):
            console_log(f"Fumaris: the settings of {self._domain_name} changed; its kept preview cache was released")
            _cancel_job(context, self)
            return False
        if getattr(self, "_preview_render_submitted", False):
            return False
        if self._in_flight > 0 and self._frame > self._end_frame:
            return False   # the last frame is in flight; nothing to send ahead
        # Do not block the main thread preparing the next frame before Blender
        # has had an opportunity to upload and draw the image just received.
        # A hidden/closed viewport must never stall the simulation indefinitely.
        deadline = getattr(self, "_preview_display_deadline", 0.0)
        if deadline:
            domain = bpy.data.objects.get(self._domain_name)
            if time.monotonic() < deadline and preview_image_pending(domain):
                # Render the next view while Blender draws this one (a request
                # costs the main thread ~0.05 ms; no frame is built). Playing,
                # only when it fits before the next frame is due.
                self._submit_changed_volume_preview(context)
                return False
            self._preview_display_deadline = 0.0
        if getattr(self, "_export_pending", None):
            # Writing the cache: no frame or rerender until it is done.
            return False
        if getattr(self, "_restart_pending", False):
            if self._in_flight == 0 and not getattr(self, "_preview_render_submitted", False):
                domain = bpy.data.objects.get(self._domain_name)
                if domain is None:
                    raise RuntimeError("The active Fumaris domain was deleted")
                self._restart_now(context, domain)
            return False
        if (
            getattr(self, "_loop_reset_pending", False)
            and not getattr(self, "_pause_requested", False)
        ):
            domain = bpy.data.objects.get(self._domain_name)
            if domain is None:
                raise RuntimeError("The active Fumaris domain was deleted")
            self._next_pass(context, domain)
            return False
        if self._submit_changed_volume_preview(context):
            return False
        if getattr(self, "_pause_requested", False):
            return False
        return time.monotonic() >= getattr(self, "_next_submit_at", 0.0)

    def _submit_changed_volume_preview(self, context):
        if not getattr(self, "_has_volume_preview_frame", False):
            return False
        if self._submit_other_viewports(context):
            return True
        if (
            not getattr(self, "_pause_requested", False)
            and getattr(self, "_preview_rerendered_since_frame", False)
            and not self._rerender_fits_before_frame()
        ):
            # Playing: the due frame carries the latest view.
            return False
        now = time.monotonic()
        if now < getattr(self, "_next_preview_capture_at", 0.0):
            return False
        self._next_preview_capture_at = now + PREVIEW_CAPTURE_INTERVAL

        domain = bpy.data.objects.get(self._domain_name)
        if domain is None:
            return False
        preview = capture_volume_preview(context, domain)
        if preview.get("valid", False):
            frame = self._cache_frame(context)
            if frame is not None:
                preview.update(cache_frame=frame, cache_frame_enabled=True)
        signature = volume_preview_signature(preview)
        if signature is None or signature == self._last_preview_signature:
            return False
        self._worker.send_preview(preview)
        self._preview_sent_at = time.monotonic()
        self._preview_render_submitted = True
        self._preview_rerendered_since_frame = True
        self._last_preview_signature = signature
        return True

    def _cache_frame(self, context):
        """The frame to draw from cache, or None for the live grid."""
        frame = int(context.scene.frame_current)
        paused = getattr(self, "_pause_requested", False)
        if getattr(self, "_replaying", False) and not paused:
            return frame
        cache = getattr(self, "_preview_cache", None) or {}
        if not paused or not int(cache.get("frames", 0)):
            return None
        first, last = int(cache["first"]), int(cache["last"])
        if getattr(self, "_parked", False):
            return frame if first <= frame <= last else None
        live = getattr(self, "_live_frame", None)
        return frame if live is not None and first <= frame < live else None

    def _rerender_fits_before_frame(self):
        """A rerender (by its measured time, with margin) finishes before the
        next simulation frame is due."""
        seconds = getattr(self, "_preview_rerender_seconds", PREVIEW_RERENDER_DEFAULT_SECONDS)
        return time.monotonic() + PREVIEW_RERENDER_MARGIN * seconds < getattr(self, "_next_submit_at", 0.0)

    def _submit_other_viewports(self, context):
        """All Viewports: rerender the retained volume once for each other
        viewport after every frame, and when one of their views changes."""
        domain = bpy.data.objects.get(self._domain_name)
        if domain is None or not getattr(domain.fumaris, "preview_all_viewports", False):
            return False
        targets = preview_targets(context, domain)[1:]
        signatures = getattr(self, "_view_signatures", None)
        if signatures is None:
            signatures = self._view_signatures = {}
        for target in targets:
            preview = capture_volume_preview(context, domain, target)
            # The same cached frame as the main view while paused (its
            # signature includes it, so scrubbing redraws every view).
            if preview.get("valid", False):
                frame = self._cache_frame(context)
                if frame is not None:
                    preview.update(cache_frame=frame, cache_frame_enabled=True)
            signature = volume_preview_signature(preview)
            view_id = int(preview.get("view_id", 0))
            if signature is None or signatures.get(view_id) == signature:
                continue
            self._worker.send_preview(preview)
            self._preview_render_submitted = True
            signatures[view_id] = signature
            return True
        return False

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
            self._start_preview_session(context, domain, clear_preview_image=True, require_emitter=False)
            return
        if getattr(self, "_replaying", False):
            self._submit_replay_frame(context, domain)
            return
        super()._submit_frame(context)

    def _after_frame_packet(self, packet):
        """A simulated frame: remember what it was simulated from; a change of
        settings during the pass marks the run stale (its cache mixes them)."""
        if not self._worker._session.get("preview_cache"):
            return
        domain = bpy.data.objects.get(self._domain_name)
        self._frame_fingerprints[self._frame] = packet_digest(packet)
        from .participants import rebind
        current = settings_digest(domain, rebind(self._participants), getattr(self, "_resolution_scale", 1.0))
        self._frame_runs[self._frame] = self._run_id
        self._frame_settings[self._frame] = current
        if self._pass_settings is None:
            self._pass_settings = current
        elif current != self._pass_settings and self._pass_stale_frame is None:
            self._pass_stale_frame = self._frame
            self._stale_runs.add(self._run_id)

    def export_cache(self, context, directory, slot, first, last, lock=None):
        """Save Sequence / Save Frame: the bridge writes the cached frames into a new
        version; its reply finishes it (_handle_worker_message). Playing
        pauses until it is written (_ready_to_submit)."""
        domain = bpy.data.objects.get(self._domain_name)
        if domain is None:
            raise RuntimeError("The active Fumaris domain was deleted")
        from .exporters import _domain_state
        props = domain.fumaris
        header = {
            "output_directory": directory,
            "output_prefix": props.output_prefix or "fumaris_",
            "domain": _domain_state(props, self._resolution_scale),
            "first": int(first),
            "last": int(last),
            # The Output settings now, not when the preview started.
            "vdb_compression": getattr(props, "vdb_compression", "active_mask"),
            "vdb_precision": getattr(props, "vdb_precision", "half"),
        }
        # The version's folder stays locked (as for a bake) until the reply.
        frames = cached_frames(self)
        total = max(1, sum(1 for frame in frames if int(first) <= frame <= int(last)))
        self._export_pending = {"directory": directory, "slot": slot, "started": time.monotonic(), "lock": lock,
                                "total": total, "written": 0, "checked": 0.0}
        self._export_error = None
        diagnostics.set_status(self._diagnostic_key, f"Saving version '{slot}': 0/{total} frames")
        # The cursor counts the frames written (Blender's progress cursor).
        context.window_manager.progress_begin(0, total)
        self._worker.send_preview_cache_export(header)

    def _update_diagnostics(self, context):
        super()._update_diagnostics(context)
        pending = getattr(self, "_export_pending", None)
        if not pending:
            return
        now = time.monotonic()
        if now - pending.get("checked", 0.0) < 0.2:
            return
        pending["checked"] = now
        # Frames written so far: the bridge writes one file per frame.
        try:
            written = sum(1 for entry in os.scandir(pending["directory"])
                          if entry.name.endswith(".vdb") and entry.is_file())
        except OSError:
            return
        if written != pending.get("written"):
            pending["written"] = written
            context.window_manager.progress_update(min(written, pending["total"]))
            diagnostics.set_status(self._diagnostic_key,
                                   f"Saving version '{pending['slot']}': {written}/{pending['total']} frames")
            for area in context.screen.areas if context.screen else ():
                if area.type == "PROPERTIES":
                    area.tag_redraw()

    def _handle_worker_message(self, context, message_type, data, _payload):
        if message_type == PREVIEW_RELEASED:
            before = float(data.get("flow_device_memory_before", 0)) / 2**30
            after = float(data.get("flow_device_memory_bytes", 0)) / 2**30
            self._parked_gpu_bytes = int(float(data.get("flow_device_memory_bytes", 0)))
            diagnostics.record(self._diagnostic_key, {"bridge_preview_release_ms": float(data.get("release_ms", 0.0))})
            console_log(f"Fumaris preview stopped, cached frames kept: GPU memory {before:.2f} -> {after:.2f} GiB "
                        f"({float(data.get('release_ms', 0.0)):.0f} ms)")
            for area in context.screen.areas if context.screen else ():
                area.tag_redraw()
            return True
        if message_type != PREVIEW_CACHE_EXPORTED:
            return False
        pending = getattr(self, "_export_pending", None) or {}
        self._export_pending = None
        context.window_manager.progress_end()
        domain = bpy.data.objects.get(self._domain_name)
        directory = pending.get("directory")
        frames = int(data.get("frames", 0))
        error = data.get("error")
        lock = pending.get("lock")
        if lock is not None:
            lock.release()
        cancelled = bool(pending.get("cancelled"))
        if directory:
            try:
                os.remove(os.path.join(directory, SAVE_CANCEL_FILE))
            except OSError:
                pass
        if error:
            # The save failed (or Escape cancelled it); the preview and its
            # cache carry on, so it can be retried (another Output directory,
            # freed disk space).
            self._export_error = None if cancelled else str(error)
            self._export_report = None
            if directory:
                from .compare import forget_scan
                try:
                    from .cache import GUARD_FILE
                    for name in (GUARD_FILE,):
                        path = os.path.join(directory, name)
                        if os.path.isfile(path):
                            os.remove(path)
                    os.rmdir(directory)
                except OSError:
                    pass
                forget_scan()
            diagnostics.set_status(self._diagnostic_key, "Paused" if self._pause_requested else "Running")
            console_log(f"Fumaris preview save cancelled ({directory})" if cancelled
                        else f"Fumaris could not save the preview to {directory}: {error}")
            for area in context.screen.areas if context.screen else ():
                area.tag_redraw()
            return True
        if domain is not None and directory:
            from .compare import forget_scan, write_note
            write_settings_snapshot(directory, domain, self._participants)
            scale = int(round(100 * float(getattr(self, "_resolution_scale", 1.0))))
            first, last = int(data.get("first", -1)), int(data.get("last", -1))
            what = f"frame {first} from the preview" if first == last else "from the preview"
            # A save gets its own copy of the material (Sync Material off).
            write_note(directory, what + (f" at {scale}%" if scale != 100 else ""), sync_material=False)
            forget_scan()
        diagnostics.record(self._diagnostic_key, {"bridge_preview_cache_export_ms": float(data.get("export_ms", 0.0))})
        diagnostics.set_status(self._diagnostic_key, "Paused" if self._pause_requested else "Running")
        console_log(f"Fumaris exported {frames} cached preview frames to {directory}")
        self._export_report = (frames, pending.get("slot"), int(data.get("first", -1)), int(data.get("last", -1)))
        self._export_warning = ("Velocity was not kept by this preview: turn on velocity export before Play to save it"
                                if int(float(data.get("velocity_missing", 0) or 0)) else None)
        if self._export_warning:
            console_log(f"Fumaris: {self._export_warning}")
        for area in context.screen.areas if context.screen else ():
            area.tag_redraw()
        return True

    def _preview_start_frame(self, _context, domain):
        return simulation_frame_range(domain)[0]

    def _after_frame_complete(self, context, domain, completed, data, payload):
        self._preview_rerendered_since_frame = False
        self._live_frame = int(completed)
        if data.get("preview_cache"):
            self._preview_cache = dict(data["preview_cache"])
            tag_timelines(context)
        # A new simulation frame: every other viewport needs a new image.
        self._view_signatures = {}
        if domain:
            data["blender_preview_upload_ms"] = show_preview_payload(
                domain,
                data,
                payload,
            )
        preview = data.get("preview") or {}
        self._has_volume_preview_frame = preview.get("type") == "volume_rgba8"
        if self._has_volume_preview_frame:
            self._preview_display_deadline = time.monotonic() + 0.1
            self._last_preview_signature = volume_preview_signature(
                getattr(self, "_last_submitted_volume_preview", None)
            )
        started = getattr(self, "_last_frame_submit_started", time.monotonic())
        self._next_submit_at = started + _preview_frame_duration(
            context.scene, domain.fumaris if domain else None)

    def _after_preview_complete(self, _context, domain, data, payload):
        if data.get("preview_cache"):
            self._preview_cache = dict(data["preview_cache"])
        sent = getattr(self, "_preview_sent_at", None)
        if sent is not None:
            # Smoothed rerender time, for fitting rerenders between frames.
            elapsed = max(0.0, time.monotonic() - sent)
            previous = getattr(self, "_preview_rerender_seconds", PREVIEW_RERENDER_DEFAULT_SECONDS)
            self._preview_rerender_seconds = 0.8 * previous + 0.2 * elapsed
            self._preview_sent_at = None
        if domain:
            data["blender_preview_upload_ms"] = show_preview_payload(
                domain,
                data,
                payload,
            )
            if (data.get("preview") or {}).get("type") == "volume_rgba8":
                self._preview_display_deadline = time.monotonic() + 0.1
        # Timings stay in Diagnostics; a console line per rerender spammed
        # (and on Windows slowed) orbiting at display rate.
        self._preview_render_count = getattr(self, "_preview_render_count", 0) + 1
        if getattr(self, "_replay_waiting", False):
            self._replay_frame_done(_context, domain)

    def _complete(self, context):
        domain = bpy.data.objects.get(self._domain_name)
        self._finish(context)
        if domain:
            clear_preview(domain)
            domain.fumaris.simulation_state = "idle"
            domain.fumaris.bake_elapsed = time.monotonic() - self._started_at
        console_log(f"Fumaris preview complete for {self._domain_name}")
        return {"FINISHED"}

    # F8: Stop keeps the cached frames. The preview stays paused on its cache
    # (scrub, play back, Save Sequence) while the bridge frees the
    # simulation's GPU memory; Play starts over from the first frame, a
    # second Stop, a settings change or any other job releases the cache.
    def can_park(self, context):
        domain = bpy.data.objects.get(self._domain_name)
        cache = getattr(self, "_preview_cache", None) or {}
        return bool(
            domain is not None
            and domain.fumaris.preview_cache
            and not getattr(self, "_parked", False)
            and getattr(self, "_accepted", False)
            and getattr(self, "_worker", None) is not None
            and not getattr(self, "_export_pending", None)
            and not getattr(self, "_finished", False)
            and int(float(cache.get("frames", 0) or 0)) > 0
        )

    def park(self, context):
        domain = bpy.data.objects.get(self._domain_name)
        self._parked = True
        self._pause_requested = True
        self._replaying = False
        self._replay_waiting = False
        self._restart_pending = False
        self._loop_reset_pending = False
        self._parked_signature, self._parked_settings = self._parked_digest(domain)
        self._parked_checked_at = time.monotonic()
        self._worker.send_preview_release()
        # Redraw the view from the cache (the live state is about to go).
        self._last_preview_signature = None
        self._next_preview_capture_at = 0.0
        domain.fumaris.simulation_state = "idle"
        diagnostics.set_status(self._diagnostic_key, "Stopped (cache kept)")
        console_log(f"Fumaris preview stopped for {self._domain_name}; its cached frames are kept")
        tag_timelines(context)

    def _parked_stale(self, context):
        """The domain or its sources changed since Stop (checked twice a second)."""
        now = time.monotonic()
        if now < getattr(self, "_parked_checked_at", 0.0) + 0.5:
            return False
        self._parked_checked_at = now
        domain = bpy.data.objects.get(self._domain_name)
        if domain is None:
            return True
        return self._parked_digest(domain) != (self._parked_signature, self._parked_settings)

    def _parked_digest(self, domain):
        """(session structure, simulation settings); None where the domain
        cannot give one (no emitter collection)."""
        from .participants import participants as gather_participants
        try:
            structure = session_structure_signature(domain)
        except Exception:
            structure = None
        try:
            sources = gather_participants(domain.fumaris)
        except Exception:
            sources = []
        return structure, settings_digest(domain, sources, getattr(self, "_resolution_scale", 1.0))

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
        # Scrubbed back through the cache: continue from the live frame (or,
        # replaying, from the frame the replay had reached).
        resume_at = self._frame if getattr(self, "_replaying", False) else getattr(self, "_live_frame", None)
        if resume_at is not None and context.scene.frame_current != resume_at:
            context.scene.frame_set(resume_at)
        if getattr(self, "_replaying", False):
            self._replay_clock = (time.monotonic(), self._frame)
        self._next_submit_at = 0.0
        if getattr(self, "_loop_reset_pending", False):
            # Let any pending viewport response arrive before resetting Flow.
            self._ready_to_submit(context)
        console_log(f"Fumaris preview resumed for {self._domain_name}")

    def _end_of_range(self, context, completed):
        domain = bpy.data.objects.get(self._domain_name)
        if domain is None:
            return self._cancelled(context, {"message": "The active Fumaris domain was deleted"})
        if domain.fumaris.preview_loop:
            # Loop on: start over (a cached preview keeps its cache; the new
            # pass replaces each frame as it reaches it),
            # unless paused (Resume then starts over). Loop off: pause on the
            # last frame, keeping any cache to scrub, play back or save.
            if getattr(self, "_pause_requested", False):
                self._submitted = False
                self._loop_reset_pending = True
                return None
            try:
                self._next_pass(context, domain)
            except Exception as error:
                return self._cancelled(context, {"message": str(error)})
            return None
        # Pause at the end (keeping the preview cache, if any, to scrub, play
        # back or save); Resume simulates the range again from the start.
        self._submitted = False
        self._loop_reset_pending = True
        if not getattr(self, "_pause_requested", False):
            self._pause_requested = True
            diagnostics.set_status(self._diagnostic_key, "Paused at the end")
            console_log(f"Fumaris preview reached the end of its range for {self._domain_name}")
        return None

    def _cancelled(self, context, data):
        self._record_failure(data.get("message", "Preview failed"))
        domain = bpy.data.objects.get(self._domain_name)
        if domain:
            clear_preview(domain)
            domain.fumaris.simulation_state = "idle"
            domain.fumaris.bake_elapsed = time.monotonic() - self._started_at
        self._finish(context)
        message = stop_message(data.get("message", "Preview stopped"),
                               data.get("stderr") or self._worker.stderr())
        console_log(f"Fumaris preview stopped: {message}")
        self.report({"ERROR"}, message)
        return {"CANCELLED"}


class FUMARIS_OT_preview_stop(Operator):
    bl_idname = "fumaris.preview_stop"
    bl_label = "Stop Preview"
    bl_description = "Stop the live preview; the next Play starts again from the first frame"

    @classmethod
    def description(cls, context, _properties):
        from .playback import preview_description
        return preview_description(context, "stop")

    @classmethod
    def poll(cls, context):
        return active_mode() == "previewing"

    def execute(self, context):
        job = active_job() if active_mode() == "previewing" else None
        if job is None:
            self.report({"WARNING"}, "This domain has no active preview")
            return {"CANCELLED"}
        if getattr(job, "can_park", None) is not None and job.can_park(context):
            # F8: the cached frames stay; a second Stop releases them.
            job.park(context)
            for area in context.screen.areas if context.screen else ():
                area.tag_redraw()
            return {"FINISHED"}
        _cancel_job(context, job)
        return {"FINISHED"}


class FUMARIS_OT_preview_show_here(Operator):
    bl_idname = "fumaris.preview_show_here"
    bl_label = "Show Fumaris Preview Here"
    bl_description = "Show the Fumaris live preview in this 3D viewport (when All Viewports is off). Lasts until Blender closes"

    @classmethod
    def poll(cls, context):
        area = getattr(context, "area", None)
        return area is not None and area.type == "VIEW_3D"

    def execute(self, context):
        from .preview import pin_preview_view
        region = next((item for item in context.area.regions if item.type == "WINDOW"), None)
        job = active_job() if active_mode() == "previewing" else None
        domain = bpy.data.objects.get(job._domain_name) if job is not None else _active_domain(context)
        domains = [domain] if domain is not None else [
            obj for obj in context.scene.objects
            if getattr(obj, "fumaris", None) and obj.fumaris.smoke_object_type == "domain"]
        if region is None or not domains:
            self.report({"WARNING"}, "No Fumaris domain to show here")
            return {"CANCELLED"}
        for item in domains:
            pin_preview_view(item, region)
        if job is not None:
            # Rerender for the new viewport at once, also while paused.
            job._last_preview_signature = None
            job._view_signatures = {}
        return {"FINISHED"}


_SOLO_KEY = "fumaris_solo"


class FUMARIS_OT_participant_select(Operator):
    bl_idname = "fumaris.participant_select"
    bl_label = "Select Participant"
    bl_description = "Select this object (Shift: add to the selection)"
    bl_options = {"REGISTER", "UNDO"}

    name: bpy.props.StringProperty()
    # The sidebar list: make it active and show its settings (Physics tab).
    activate: bpy.props.BoolProperty(options={"HIDDEN", "SKIP_SAVE"})

    @classmethod
    def description(cls, context, properties):
        if properties.activate:
            return "Select this object and show its Fumaris settings in the Properties editor (Shift: add to the selection)"
        return cls.bl_description

    def invoke(self, context, event):
        obj = bpy.data.objects.get(self.name)
        if obj is None or obj.name not in context.view_layer.objects:
            self.report({"WARNING"}, f"{self.name} is not in this view layer")
            return {"CANCELLED"}
        if not event.shift:
            for other in context.selected_objects:
                other.select_set(False)
        obj.select_set(True)
        if not self.activate:
            # Keep the domain's panel: select without making the object active.
            return {"FINISHED"}
        context.view_layer.objects.active = obj
        if not _show_physics_tab(context.window_manager):
            # The editor offers the new object's tabs only once it redraws.
            manager = context.window_manager
            bpy.app.timers.register(lambda: _show_physics_tab(manager) and None, first_interval=0.05)
        return {"FINISHED"}


def _show_physics_tab(manager):
    """Show the Physics tab in every Properties editor; False if one did not
    offer it yet."""
    shown = True
    for window in manager.windows:
        for area in window.screen.areas:
            if area.type != "PROPERTIES":
                continue
            for space in area.spaces:
                if space.type == "PROPERTIES":
                    try:
                        space.context = "PHYSICS"
                    except TypeError:
                        shown = False  # no Physics tab (yet) for this object
            area.tag_redraw()
    return shown


class FUMARIS_OT_participant_solo(Operator):
    bl_idname = "fumaris.participant_solo"
    bl_label = "Solo Participant"
    bl_description = "Simulate only this participant of its kind (other emitters, colliders, effectors or outflows are disabled); click again to restore them. Works while previewing"
    bl_options = {"REGISTER", "UNDO"}

    name: bpy.props.StringProperty()

    @classmethod
    def poll(cls, context):
        return _active_domain(context) is not None

    def execute(self, context):
        from .participants import participant_overview
        domain = _active_domain(context)
        rows = participant_overview(domain.fumaris, limit=100000)
        solo = domain.get(_SOLO_KEY)
        if solo is not None:
            # Restore the saved states, whichever object is clicked.
            saved = dict(solo.get("saved", {}))
            for obj, _role, _reason in rows:
                if obj.name in saved:
                    obj.fumaris.participant_enabled = bool(saved[obj.name])
            previous = solo.get("object")
            del domain[_SOLO_KEY]
            if previous == self.name:
                return {"FINISHED"}
        target = bpy.data.objects.get(self.name)
        if target is None:
            return {"CANCELLED"}
        role = next((role for obj, role, _ in rows if obj == target), None)
        saved = {}
        for obj, item_role, reason in rows:
            # Only participants the simulation can use (enabled or not).
            if item_role != role or reason not in (None, "disabled"):
                continue
            saved[obj.name] = bool(obj.fumaris.participant_enabled)
            obj.fumaris.participant_enabled = obj == target
        domain[_SOLO_KEY] = {"object": target.name, "saved": saved}
        return {"FINISHED"}


def solo_object_name(domain):
    solo = domain.get(_SOLO_KEY) if domain is not None else None
    return solo.get("object") if solo is not None else None


def _draw_view_menu(self, _context):
    self.layout.separator()
    self.layout.operator("fumaris.preview_show_here", icon="HIDE_OFF")


class FUMARIS_OT_preview_pause(Operator):
    bl_idname = "fumaris.preview_pause"
    bl_label = "Pause or Resume Preview"
    bl_description = "Pause or resume the live preview. While paused the smoke stays on screen and redraws as you move the view or change its look"

    @classmethod
    def description(cls, context, _properties):
        from .playback import preview_description
        return preview_description(context, "pause")

    @classmethod
    def poll(cls, context):
        if active_mode() != "previewing":
            return False
        job = active_job()
        if not getattr(job, "_accepted", False) or getattr(job, "_parked", False):
            return False
        if getattr(job, "_pause_requested", False) and getattr(job, "_export_pending", None):
            cls.poll_message_set("Saving a version: Resume once it is written")
            return False
        return True

    def execute(self, context):
        job = active_job() if active_mode() == "previewing" else None
        if job is None:
            self.report({"WARNING"}, "This domain has no active preview")
            return {"CANCELLED"}
        if getattr(job, "_pause_requested", False) and getattr(job, "_export_pending", None):
            self.report({"INFO"}, "Saving a version: Resume once it is written")
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
        # A preview's bridge exits on its own thread: a large one took a
        # second or two to release its GPU and pinned memory, and Stop froze
        # Blender meanwhile. A save in progress is waited for (its files).
        job._close_without_wait = (getattr(job, "_job_mode", None) == "previewing"
                                   and not getattr(job, "_export_pending", None))
        job._worker.close(wait=not job._close_without_wait)
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


def _import_cache(context, domain, directory, prefix, *, frame_start=None, frame_end=None):
    if domain is None:
        return
    selected = list(context.selected_objects)
    active = context.view_layer.objects.active
    clear_preview(domain)
    props = domain.fumaris
    try:
        from .compare import apply_material_sync, version_material
        volume = import_sequence(
            directory,
            simulation_frame_range(domain)[0] if frame_start is None else frame_start,
            prefix,
            # A bake syncs its material with Appearance unless turned off.
            material=version_material(domain, directory, default=True),
            selectable=props.volume_selectable,
            appearance=props,
            frame_end=frame_end,
            name=f"{domain.name} Volume",
        )
        apply_material_sync(domain, directory, default=True)
        if volume is not None and not props.viewport_visible:
            # A hidden domain's new bake starts hidden too (Show in Viewport).
            from .importers import HIDDEN_WITH_DOMAIN_MARKER
            if not volume.hide_viewport:
                volume[HIDDEN_WITH_DOMAIN_MARKER] = True
                volume.hide_viewport = True
    finally:
        _restore_selection(context, domain, selected, active)
    return volume


def _stop_active_job():
    job = active_job()
    if job is not None:
        _cancel_job(bpy.context, job)


def _slot_contents(domain):
    """(folder, baked frames) of the domain's current slot."""
    directory = output_directory_for_object(domain)
    return directory, baked_frame_count(directory, domain.fumaris.output_prefix or "fumaris_")


class FUMARIS_OT_bake_choose(Operator):
    bl_idname = "fumaris.bake_choose"
    bl_label = "Bake"
    bl_description = "Replace a pinned bake or bake a new one"
    bl_options = {"INTERNAL"}

    target: bpy.props.StringProperty()

    @classmethod
    def poll(cls, context):
        return FUMARIS_OT_bake.poll(context)

    def invoke(self, context, _event):
        target = self.target

        def draw(menu, _context):
            layout = menu.layout
            layout.operator_context = "INVOKE_DEFAULT"
            replace = layout.operator("fumaris.bake_into", text=f"Replace '{target}'", icon="FILE_REFRESH")
            replace.slot = target
            layout.operator("fumaris.bake_into", text="New Bake", icon="ADD").slot = ""
        context.window_manager.popup_menu(draw, title="Bake", icon="RENDER_ANIMATION")
        return {"FINISHED"}

    def execute(self, _context):
        return {"FINISHED"}


class FUMARIS_OT_bake_into(Operator):
    bl_idname = "fumaris.bake_into"
    bl_label = "Bake Into"
    bl_description = "Bake into this version (replacing its frames), or into a new one"
    bl_options = {"INTERNAL"}

    slot: bpy.props.StringProperty()

    @classmethod
    def description(cls, _context, properties):
        if properties.slot:
            return f"Bake again into '{properties.slot}': its frames are replaced"
        return "Bake into a new version, keeping the others"

    @classmethod
    def poll(cls, context):
        return FUMARIS_OT_bake.poll(context)

    def invoke(self, context, _event):
        return self.execute(context)

    def execute(self, context):
        from .compare import new_bake_slot
        domain = _active_domain(context)
        slot = self.slot or new_bake_slot(domain)
        try:
            require_unprotected(cache_slot_directory(domain, slot), "bake over it")
        except RuntimeError as error:
            self.report({"ERROR"}, str(error))
            return {"CANCELLED"}
        if not self.slot:
            domain.fumaris.cache_slot = slot
            domain.fumaris.compare_selected = "vdb:" + slot
        # The old slot stays current until an explicit replacement succeeds.
        bpy.ops.fumaris.bake("INVOKE_DEFAULT", replace_confirmed=True, replacement_slot=self.slot)
        return {"FINISHED"}


class FUMARIS_OT_cache_slot_new(Operator):
    bl_idname = "fumaris.cache_slot_new"
    bl_label = "New Version"
    bl_description = "Start a new, empty version (the next free name), keeping the current one on disk"

    @classmethod
    def poll(cls, context):
        return bool(_active_domain(context) and not blocking_job())

    def execute(self, context):
        domain = _active_domain(context)
        props = domain.fumaris

        def taken(slot):
            directory = cache_slot_directory(domain, slot)
            return bool(
                is_protected(directory)
                or baked_frame_count(directory, props.output_prefix or "fumaris_")
            )

        previous = props.cache_slot
        props.cache_slot = next_slot_name(previous.strip() or "main", taken)
        self.report({"INFO"}, f"New version '{props.cache_slot}'. '{previous}' is kept on disk")
        return {"FINISHED"}


class FUMARIS_OT_cache_slot_restore_settings(Operator):
    bl_idname = "fumaris.cache_slot_restore_settings"
    bl_label = "Restore Settings from Slot"
    bl_description = "Set the domain and its sources back to the settings this version was made with (sources are matched by name)"
    bl_options = {"REGISTER", "UNDO"}

    @classmethod
    def poll(cls, context):
        domain = _active_domain(context)
        if not domain or blocking_job():
            return False
        # Polls run while drawing: peek, never create the cache identity.
        directory = peek_output_directory_for_object(domain)
        return bool(directory) and os.path.isfile(os.path.join(directory, SETTINGS_FILE))

    def execute(self, context):
        domain = _active_domain(context)
        data = read_settings_snapshot(output_directory_for_object(domain))
        if not data:
            self.report({"ERROR"}, "This version has no stored settings (baked before Fumaris 2.0)")
            return {"CANCELLED"}
        changed, restored, missing = apply_settings_snapshot(domain, data, bpy.data.objects)
        message = f"Restored {changed} settings from '{domain.fumaris.cache_slot}' (baked {data.get('written', '?')})"
        if missing:
            message += f"; not found: {', '.join(missing[:4])}"
            self.report({"WARNING"}, message)
        else:
            self.report({"INFO"}, message)
        return {"FINISHED"}


class FUMARIS_OT_cache_make_lighter(Operator):
    bl_idname = "fumaris.cache_make_lighter"
    bl_label = "Make Lighter Copy"
    bl_description = ("Copy this bake into a new version at half the resolution, without the faintest smoke: "
                      "about 8 times smaller on a large bake (less on a small one, whose files are mostly fixed "
                      "overhead), for layout, previews and render tests. The domain then shows the copy; "
                      "the original version is kept in Versions")

    cull: bpy.props.FloatProperty(
        name="Drop Below",
        description="Smoke density below which voxels are dropped",
        default=0.01,
        min=0.0,
        soft_max=0.1,
    )

    @classmethod
    def poll(cls, context):
        domain = _active_domain(context)
        if not domain or blocking_job():
            return False
        if "_lite" in (domain.fumaris.cache_slot or ""):
            cls.poll_message_set("This version is already a lighter copy")
            return False
        directory = peek_output_directory_for_object(domain)
        return bool(directory) and bool(baked_frame_count(directory, domain.fumaris.output_prefix or "fumaris_"))

    def execute(self, context):
        from .lighter import make_lighter_copy

        domain = _active_domain(context)
        props = domain.fumaris
        prefix = props.output_prefix or "fumaris_"
        try:
            ensure_cache_owner(domain)
        except (RuntimeError, OSError) as error:
            self.report({"ERROR"}, str(error))
            return {"CANCELLED"}
        source = output_directory_for_object(domain)
        files = [os.path.basename(path) for path in _frame_files(source, prefix)]

        def taken(slot):
            directory = cache_slot_directory(domain, slot)
            return bool(is_protected(directory) or baked_frame_count(directory, prefix))

        base = f"{props.cache_slot.strip() or 'main'}_lite"
        slot = base if not taken(base) else next_slot_name(base, taken)
        target = cache_slot_directory(domain, slot)
        wm = context.window_manager
        wm.progress_begin(0, max(1, len(files)))
        try:
            before, after = make_lighter_copy(
                source, target, files, self.cull,
                progress=lambda done, _total: wm.progress_update(done))
        except Exception as error:
            self.report({"ERROR"}, f"Make Lighter Copy failed: {error}")
            return {"CANCELLED"}
        finally:
            wm.progress_end()
        # The domain shows the copy, imported like a finished bake.
        props.cache_slot = slot
        try:
            _import_cache(context, domain, target, prefix)
        except Exception as error:
            self.report({"ERROR"}, f"Version '{slot}' was written but not imported: {error}")
            return {"CANCELLED"}
        mark_bake_complete(domain)
        self.report({"INFO"}, f"Version '{slot}': {len(files)} frames, {before / 1e6:.0f} MB -> {after / 1e6:.0f} MB")
        return {"FINISHED"}


class FUMARIS_OT_add_sparks(Operator):
    bl_idname = "fumaris.add_sparks"
    bl_label = "Add Sparks"
    bl_description = "Add sparks and embers that spawn in hot gas and fly through the baked velocity (Geometry Nodes on a new object). Needs a bake with VDB velocity"
    bl_options = {"REGISTER", "UNDO"}

    @classmethod
    def poll(cls, context):
        domain = _active_domain(context)
        return bool(domain)

    def execute(self, context):
        from .sparks import add_sparks, missing_grids, shown_volume
        domain = _active_domain(context)
        props = domain.fumaris
        files = _frame_files(output_directory_for_object(domain), props.output_prefix or "fumaris_")
        if not files:
            self.report({"ERROR"}, "Bake the simulation first: sparks follow the baked volume")
            return {"CANCELLED"}
        missing = missing_grids(files[-1])
        if missing:
            # Without the gas velocity sparks would only fall; without temperature none start.
            for name in missing:
                setattr(props, f"export_{name}_vdb", True)
            self.report({"ERROR"}, f"This bake has no {' and '.join(missing)}: now turned on in Output. Bake again, then add sparks")
            return {"CANCELLED"}
        volume = shown_volume(domain)
        if volume is None:
            self.report({"ERROR"}, f"Version '{props.cache_slot}' is not in the scene: import it in Versions, then add sparks")
            return {"CANCELLED"}
        sparks = add_sparks(context, domain, volume, name=f"{domain.name} Sparks")
        self.report({"INFO"}, f"Added '{sparks.name}': its settings are on its Geometry Nodes modifier")
        return {"FINISHED"}


class FUMARIS_OT_add_source(Operator):
    bl_idname = "fumaris.add_source"
    bl_label = "Add Source"
    bl_description = "Add a ready-made Geometry Nodes source to this domain's emitters at the 3D cursor; its settings are on the object's Geometry Nodes modifier"
    bl_options = {"REGISTER", "UNDO"}

    kind: bpy.props.EnumProperty(
        name="Source",
        items=GN_SOURCES,
    )

    @classmethod
    def poll(cls, context):
        return bool(_active_domain(context))

    def execute(self, context):
        from .gn_sources import add_source
        domain = _active_domain(context)
        obj = add_source(context, self.kind, domain)
        if not domain.fumaris.shader_flame_enabled:
            # Every source burns; a smoke setup (Rising Smoke) shows no flames.
            self.report({"WARNING"}, f"Added '{obj.name}' to {domain.name}'s emitters. Its fire is not shaded: "
                        "turn on Flame in the domain's Look (hot smoke will glow too)")
        else:
            self.report({"INFO"}, f"Added '{obj.name}' to {domain.name}'s emitters")
        return {"FINISHED"}


class FUMARIS_OT_gravity_from_scene(Operator):
    bl_idname = "fumaris.gravity_from_scene"
    bl_label = "Gravity from Scene"
    bl_description = "Copy the Scene's gravity (Scene Properties > Gravity) into this domain's Gravity. The domain keeps its own value afterwards"
    bl_options = {"REGISTER", "UNDO"}

    @classmethod
    def poll(cls, context):
        return bool(_active_domain(context)) and context.scene is not None

    def execute(self, context):
        domain = _active_domain(context)
        domain.fumaris.gravity = tuple(context.scene.gravity)
        return {"FINISHED"}


class FUMARIS_OT_preset_save(Operator):
    bl_idname = "fumaris.preset_save"
    bl_label = "Save Preset"
    bl_description = "Save this object's Fumaris settings as a preset (domain or source), available in every file"

    name: bpy.props.StringProperty(name="Name", default="My Preset")

    @classmethod
    def poll(cls, context):
        obj = context.object
        return bool(obj and hasattr(obj, "fumaris") and obj.fumaris.smoke_object_type != "none")

    def invoke(self, context, _event):
        return context.window_manager.invoke_props_dialog(self)

    def execute(self, context):
        from .presets import save_preset
        path, count = save_preset(context.object.fumaris, self.name)
        self.report({"INFO"}, f"Saved preset '{self.name}' ({count} settings)")
        return {"FINISHED"}


class FUMARIS_OT_preset_apply(Operator):
    bl_idname = "fumaris.preset_apply"
    bl_label = "Apply Preset"
    bl_description = "Apply a saved preset to this object"
    bl_options = {"REGISTER", "UNDO"}

    name: bpy.props.StringProperty(name="Name")

    @classmethod
    def poll(cls, context):
        obj = context.object
        return bool(obj and hasattr(obj, "fumaris") and obj.fumaris.smoke_object_type != "none")

    def execute(self, context):
        from .presets import apply_preset
        try:
            count = apply_preset(context.object.fumaris, self.name)
        except OSError as error:
            self.report({"ERROR"}, str(error))
            return {"CANCELLED"}
        self.report({"INFO"}, f"Applied preset '{self.name}' ({count} settings)")
        return {"FINISHED"}


class FUMARIS_OT_preset_delete(Operator):
    bl_idname = "fumaris.preset_delete"
    bl_label = "Delete Preset"
    bl_description = "Delete a saved preset"

    name: bpy.props.StringProperty(name="Name")

    def invoke(self, context, _event):
        return context.window_manager.invoke_confirm(self, _event)

    def execute(self, context):
        from .presets import delete_preset
        props = context.object.fumaris
        delete_preset(props, self.name)
        return {"FINISHED"}


class FUMARIS_OT_cache_protect(Operator):
    bl_idname = "fumaris.cache_protect"
    bl_label = "Protect"
    bl_description = "Lock this version: Bake and Delete are refused until it is unlocked"

    @classmethod
    def poll(cls, context):
        return bool(_active_domain(context) and not blocking_job())

    def execute(self, context):
        domain = _active_domain(context)
        try:
            ensure_cache_owner(domain)
            directory = output_directory_for_object(domain)
            protect = not is_protected(directory)
            if protect and not baked_frame_count(directory, domain.fumaris.output_prefix or "fumaris_"):
                self.report({"WARNING"}, f"Version '{domain.fumaris.cache_slot}' holds no baked frames: nothing to protect")
                return {"CANCELLED"}
            with cache_lock(directory):
                set_protected(directory, protect)
        except (RuntimeError, OSError) as error:
            self.report({"ERROR"}, f"Could not change protection: {error}")
            return {"CANCELLED"}
        self.report(
            {"INFO"},
            f"Version '{domain.fumaris.cache_slot}' is "
            + ("protected" if protect else "no longer protected"),
        )
        return {"FINISHED"}


class FUMARIS_OT_delete(Operator):
    bl_idname = "fumaris.delete"
    bl_label = "Delete Baked"
    bl_description = "Delete this version's baked VDB files, imported volumes, and live preview"

    @classmethod
    def poll(cls, context):
        domain = _active_domain(context)
        if background_job(domain):
            cls.poll_message_set("This domain is baking in the background")
            return False
        return bool(domain and not blocking_job())

    def invoke(self, context, event):
        domain = _active_domain(context)
        if not domain:
            return self.execute(context)
        directory, frames = _slot_contents(domain)
        if is_protected(directory) or not frames:
            return self.execute(context)
        return context.window_manager.invoke_confirm(
            self,
            event,
            title="Delete Bake?",
            message=(
                f"Delete {frames} baked frames from version "
                f"'{domain.fumaris.cache_slot}'? The files cannot be recovered."
            ),
            confirm_text="Delete",
            icon="WARNING",
        )

    def execute(self, context):
        domain = _active_domain(context)
        if not domain:
            self.report({"ERROR"}, "Select a Fumaris domain")
            return {"CANCELLED"}
        if blocking_job():
            self.report({"ERROR"}, "Stop the active Fumaris simulation first")
            return {"CANCELLED"}
        prefix = domain.fumaris.output_prefix or "fumaris_"
        try:
            ensure_cache_owner(domain)
            directory = output_directory_for_object(domain)
            require_unprotected(directory, "delete it")
            recover_cache_lock(directory, owner_pid=os.getpid())
            with cache_lock(directory):
                recover_replacement(directory, prefix)
                require_unprotected(directory, "delete it")
                frames = baked_frame_count(directory, prefix)
                clear_preview(domain)
                delete_generated_data(directory, prefix)
        except (RuntimeError, OSError) as error:
            self.report({"ERROR"}, str(error))
            return {"CANCELLED"}
        diagnostics.clear(domain)
        from .compare import forget_scan
        forget_scan()
        domain.fumaris.simulation_state = "idle"
        domain.fumaris.baked_frames = 0
        domain.fumaris.bake_elapsed = 0.0
        domain.fumaris.bake_peak_vram_mb = 0.0
        domain.fumaris.bake_peak_ram_mb = 0.0
        if not frames:
            self.report({"INFO"}, f"Version '{domain.fumaris.cache_slot}' held no baked frames")
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

    @classmethod
    def description(cls, context, _properties):
        job = active_job() if active_mode() == "baking" else None
        if writing_last_frames(job):
            return ("Every frame is simulated and the last ones are being written to disk. "
                    "Stop ends it now: the frames already written are kept and imported, the rest are lost")
        return cls.bl_description

    def execute(self, context):
        job = self._active_job(context)
        if job is None:
            self.report({"WARNING"}, "This domain has no active bake")
            return {"CANCELLED"}
        if not getattr(job, "_accepted", False):
            domain = bpy.data.objects.get(getattr(job, "_domain_name", ""))
            job._stop_requested = True
            replacing = getattr(job, "_replacement", None) is not None
            _cancel_job(context, job)
            if domain and not replacing:
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


def _playback_domain(context):
    from .playback import resolve_domain
    return resolve_domain(context)


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


class FUMARIS_OT_edit_flame_ramp(Operator):
    bl_idname = "fumaris.edit_flame_ramp"
    bl_label = "Edit Ramp"
    bl_description = "Create the domain's Fumaris material now so its Colour Ramp can be edited before a bake"
    bl_options = {"REGISTER", "UNDO"}

    domain_name: StringProperty(options={"HIDDEN"})

    def execute(self, context):
        from .importers import synced_material
        domain = bpy.data.objects.get(self.domain_name) if self.domain_name else _active_domain(context)
        if domain is None:
            self.report({"WARNING"}, "Select a Fumaris domain")
            return {"CANCELLED"}
        synced_material(domain, create=True)
        return {"FINISHED"}


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
    FUMARIS_OT_edit_flame_ramp,
    FUMARIS_OT_copy_diagnostics,
    FUMARIS_OT_bake,
    FUMARIS_OT_bake_choose,
    FUMARIS_OT_bake_into,
    FUMARIS_OT_cache_slot_new,
    FUMARIS_OT_cache_slot_restore_settings,
    FUMARIS_OT_cache_make_lighter,
    FUMARIS_OT_add_sparks,
    FUMARIS_OT_gravity_from_scene,
    FUMARIS_OT_add_source,
    FUMARIS_OT_preset_save,
    FUMARIS_OT_preset_apply,
    FUMARIS_OT_preset_delete,
    FUMARIS_OT_cache_protect,
    FUMARIS_OT_preview_play,
    FUMARIS_OT_preview_pause,
    FUMARIS_OT_preview_stop,
    FUMARIS_OT_preview_show_here,
    FUMARIS_OT_participant_select,
    FUMARIS_OT_participant_solo,
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
    unloaded = []
    for obj in objects:
        if not hasattr(obj, "fumaris"):
            continue
        props = obj.fumaris
        if props.smoke_object_type != "domain":
            continue
        directory = output_directory_for_object(obj)
        try:
            if owns_cache(directory):
                recover_cache_lock(directory, owner_pid=owner_pid)
            if not os.path.isdir(directory):
                if props.simulation_state == "baking":
                    props.simulation_state = "stopped"
                continue
            if owns_cache(directory):
                with cache_lock(directory):
                    prefix = props.output_prefix or "fumaris_"
                    recover_replacement(directory, prefix)
                    delete_temporary_writes(directory, prefix)
            if props.simulation_state == "baking":
                props.simulation_state = "stopped"
            if _kept_frames_unloaded(obj, directory):
                unloaded.append(obj.name)
        except RuntimeError:
            # Another Blender process may still be writing this cache.
            continue
        except OSError as error:
            if props.simulation_state == "baking":
                props.simulation_state = "stopped"
            console_log(f"Fumaris could not recover cache for {obj.name}: {error}")
    if unloaded:
        # After the load, with the file's windows in place.
        bpy.app.timers.register(lambda: _load_kept_frames(unloaded), first_interval=0.0)


def _kept_frames_unloaded(domain, directory):
    """A bake stopped while another file was open (or Blender quit) kept its
    frames and statistics in the domain's folder, but the file was saved
    before it ended: no volume shows them (beta 3 review R30)."""
    props = domain.fumaris
    if props.simulation_state not in {"idle", "baking", "stopped"}:
        return False
    current = os.path.normpath(directory)
    if any(os.path.normpath(volume[VOLUME_MARKER]) == current for volume in domain_volumes(domain)):
        return False
    from .compare import read_bake_stats
    try:
        return bool(read_bake_stats(directory)) and baked_frame_count(directory, props.output_prefix or "fumaris_") > 0
    except OSError:
        return False


def _load_kept_frames(names):
    from .background import report
    for name in names:
        domain = bpy.data.objects.get(name)
        if domain is None or active_job():
            continue
        props = domain.fumaris
        directory = output_directory_for_object(domain)
        prefix = props.output_prefix or "fumaris_"
        if not _kept_frames_unloaded(domain, directory):
            continue
        frames = baked_frame_count(directory, prefix)
        try:
            _import_cache(bpy.context, domain, directory, prefix)
        except Exception as error:
            console_log(f"Fumaris could not load the kept frames of {name}: {error}")
            continue
        start, end = simulation_frame_range(domain)
        props.simulation_state = "baked" if frames >= end - start + 1 else "stopped"
        props.baked_frames = frames
        report("INFO", f"Loaded {frames} baked frames of '{name}' (a bake that ended while another file was open)")
    return None


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


def _preview_frame_duration(scene, props):
    """Seconds between live preview frames (Preview Settings > Frame Rate)."""
    mode = getattr(props, "preview_frame_rate", "SCENE") if props is not None else "SCENE"
    if mode == "UNCAPPED":
        return 0.0
    if mode == "CUSTOM":
        return 1.0 / max(1.0, float(getattr(props, "preview_custom_fps", 30.0)))
    return _scene_frame_duration(scene)


def _preview_resolution_scale(domain):
    percent = float(getattr(domain.fumaris, "preview_resolution_percent", 100.0))
    return max(0.0, percent) / 100.0


# Imported volumes hidden for timeline playback carry this mark until shown
# again (also after a crash or a save mid-playback: shown on load).
PLAYBACK_HIDDEN_MARKER = "fumaris_hidden_for_playback"
# How many volumes the playing timeline hides now: the 3D Viewport and
# Timeline headers say so while it plays (playback.draw_header).
HIDDEN_FOR_PLAYBACK = [0]


def _tag_headers():
    for window in getattr(bpy.context.window_manager, "windows", ()):
        for area in window.screen.areas:
            if area.type in {"VIEW_3D", "DOPESHEET_EDITOR"}:
                for region in area.regions:
                    if region.type == "HEADER":
                        region.tag_redraw()


def _hide_volumes_enabled():
    from .utils import get_addon_preferences
    prefs = get_addon_preferences()
    return bool(getattr(prefs, "hide_volumes_during_playback", True)) if prefs is not None else True


def _show_playback_hidden_volumes():
    for obj in bpy.data.objects:
        if obj.get(PLAYBACK_HIDDEN_MARKER):
            del obj[PLAYBACK_HIDDEN_MARKER]
            obj.hide_viewport = False
    if HIDDEN_FOR_PLAYBACK[0]:
        HIDDEN_FOR_PLAYBACK[0] = 0
        _tag_headers()


@persistent
def _hide_volumes_for_playback(_scene=None, _depsgraph=None):
    """Playing the timeline loads each imported VDB frame: hide imported
    volumes in the viewport while it plays (Hide Volumes While Playing)."""
    if not _hide_volumes_enabled():
        return
    hidden = 0
    for obj in bpy.data.objects:
        if obj.type == "VOLUME" and _is_imported_volume(obj) and not obj.hide_viewport:
            obj[PLAYBACK_HIDDEN_MARKER] = True
            obj.hide_viewport = True
            hidden += 1
    if hidden:
        # (Brent, 2026-11-19: "the play head no longer plays through the VDB sequence")
        HIDDEN_FOR_PLAYBACK[0] = hidden
        _tag_headers()


@persistent
def _show_volumes_after_playback(_scene=None, _depsgraph=None):
    _show_playback_hidden_volumes()


@persistent
def _show_playback_hidden_volumes_after_load(_dummy=None):
    _show_playback_hidden_volumes()


@persistent
def _cancel_preview_before_native_playback(_scene=None, _depsgraph=None):
    if active_mode() != "previewing":
        return
    job = active_job()
    if getattr(job, "_pause_requested", False) and int((getattr(job, "_preview_cache", None) or {}).get("frames", 0)):
        # Paused with cached frames: Blender's playback plays them back.
        return
    console_log("Fumaris preview stopped because Blender playback started")
    _cancel_job(bpy.context, job)


def _release_parked(job):
    _cancel_job(bpy.context, job)


def register():
    from . import runtime
    runtime._PARKED_RELEASE[0] = _release_parked
    for cls in CLASSES:
        bpy.utils.register_class(cls)
    bpy.types.VIEW3D_MT_view.append(_draw_view_menu)
    # A background bake's own Blender bakes one domain of a snapshot: every
    # other cache belongs to the main process and its other bakes.
    if background_bake() is None:
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
    for handlers, handler in (
        (bpy.app.handlers.animation_playback_pre, _hide_volumes_for_playback),
        (bpy.app.handlers.animation_playback_post, _show_volumes_after_playback),
        (bpy.app.handlers.load_post, _show_playback_hidden_volumes_after_load),
    ):
        _remove_handler_named(handlers, handler.__name__)
        handlers.append(handler)


def unregister():
    _stop_active_job()
    clear_all_previews()
    diagnostics.clear()
    _remove_handler_named(bpy.app.handlers.load_pre, "_clear_previews_before_load")
    _remove_handler_named(bpy.app.handlers.load_post, "_recover_orphaned_bakes_after_load")
    _remove_handler_named(bpy.app.handlers.depsgraph_update_post, "_track_geometry_updates")
    _remove_handler_named(bpy.app.handlers.animation_playback_pre, "_cancel_preview_before_native_playback")
    _remove_handler_named(bpy.app.handlers.animation_playback_pre, "_hide_volumes_for_playback")
    _remove_handler_named(bpy.app.handlers.animation_playback_post, "_show_volumes_after_playback")
    _remove_handler_named(bpy.app.handlers.load_post, "_show_playback_hidden_volumes_after_load")
    try:
        _show_playback_hidden_volumes()
    except (AttributeError, RuntimeError):  # restricted context while Blender quits
        pass
    bpy.types.VIEW3D_MT_view.remove(_draw_view_menu)
    for cls in reversed(CLASSES):
        bpy.utils.unregister_class(cls)
