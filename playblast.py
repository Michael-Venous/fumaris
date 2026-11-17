"""Save Playblast: the live preview's smoke over the viewport, frame by frame, to a
movie (and its PNG frames) kept in the domain's Versions, beside its snapshots,
with the settings it was made with (View Changes, Load Settings).

The simulation runs from the start of the range as fast as it can (not in real
time): for each frame the scene is set to it, the largest 3D viewport is drawn
offscreen as it looks (overlays off), its depth hides the smoke behind objects,
the bridge simulates the frame and draws the smoke for that exact view, and the
two are layered (the preview's premultiplied, display-encoded image over the
viewport's display-encoded colours). Blender's own movie writer then makes the
MP4, so nothing beyond Blender is needed. Nothing is kept in the preview cache.
"""
import os
import tempfile
import time

import bpy
from bpy.types import Operator

from . import exporters, protocol
from .bridge import BridgeWorker
from .diagnostics import console_log
from .runtime import active_job
from .utils import simulation_frame_range

try:
    import numpy as np
except ImportError:  # pragma: no cover - Blender ships numpy
    np = None


def _largest_view(context):
    """(area, region, space) of the largest 3D viewport in this window."""
    best = None
    for area in context.screen.areas if context.screen else ():
        if area.type != "VIEW_3D":
            continue
        region = next((r for r in area.regions if r.type == "WINDOW"), None)
        if region is None:
            continue
        size = region.width * region.height
        if best is None or size > best[0]:
            best = (size, area, region, area.spaces.active)
    return best[1:] if best else None


PLAYBLAST_SUFFIX = "_playblast"
MOVIE_NAME = "playblast.mp4"


def new_playblast_directory(domain):
    """A new folder for a playblast in the domain's Versions (its snapshot folder)."""
    from .compare import snapshot_root
    root = snapshot_root(domain, create=True) or tempfile.gettempdir()
    stamp = time.strftime("%Y%m%d-%H%M%S")
    path = os.path.join(root, stamp + PLAYBLAST_SUFFIX)
    index = 1
    while os.path.exists(path):
        index += 1
        path = os.path.join(root, f"{stamp}-{index}{PLAYBLAST_SUFFIX}")
    os.makedirs(path)
    return path


def _encode_movie(frames_dir, names, width, height, fps, fps_base, movie_path):
    """PNG frames -> MP4 (H.264) with Blender's movie writer, in a temporary scene.
    The frames are display-encoded already: the scene uses the Standard view."""
    scene = bpy.data.scenes.new("Fumaris Playblast")
    window = bpy.context.window
    previous = window.scene if window else None
    try:
        render = scene.render
        render.resolution_x, render.resolution_y = width + width % 2, height + height % 2
        render.resolution_percentage = 100
        render.fps, render.fps_base = fps, fps_base
        scene.frame_start, scene.frame_end = 1, len(names)
        scene.view_settings.view_transform = "Standard"
        scene.view_settings.look = "None"
        if hasattr(render.image_settings, "media_type"):  # Blender 5: movies are a media type
            render.image_settings.media_type = "VIDEO"
        render.image_settings.file_format = "FFMPEG"
        render.ffmpeg.format = "MPEG4"
        render.ffmpeg.codec = "H264"
        render.ffmpeg.constant_rate_factor = "HIGH"
        render.use_sequencer = True
        render.use_compositing = False
        render.filepath = movie_path
        editor = scene.sequence_editor_create()
        strips = editor.strips if hasattr(editor, "strips") else editor.sequences
        strip = strips.new_image("frames", os.path.join(frames_dir, names[0]), 1, 1)
        for name in names[1:]:
            strip.elements.append(name)
        if window:
            window.scene = scene
        with bpy.context.temp_override(scene=scene):
            bpy.ops.render.render(animation=True, scene=scene.name)
    finally:
        if window and previous is not None:
            window.scene = previous
        bpy.data.scenes.remove(scene)


class FUMARIS_OT_playblast(Operator):
    bl_idname = "fumaris.playblast"
    bl_label = "Save Playblast"
    bl_description = ("Simulate the whole range as fast as possible and save the viewport with the live smoke "
                      "to a movie (and its PNG frames) in Versions, with the current settings. "
                      "Uses the largest 3D viewport as it is shown, without overlays; Esc stops it")
    bl_options = {"REGISTER"}

    domain_name: bpy.props.StringProperty(options={"HIDDEN"})

    @classmethod
    def poll(cls, context):
        return np is not None and active_job() is None and _largest_view(context) is not None

    def _domain(self, context):
        if self.domain_name:
            return bpy.data.objects.get(self.domain_name)
        from .playback import resolve_domain
        obj = context.object
        if obj is not None and getattr(obj, "fumaris", None) and obj.fumaris.smoke_object_type == "domain":
            return obj
        return resolve_domain(context)

    def invoke(self, context, _event):
        domain = self._domain(context)
        if domain is None:
            self.report({"WARNING"}, "Choose a Fumaris domain")
            return {"CANCELLED"}
        view = _largest_view(context)
        if view is None:
            self.report({"WARNING"}, "Open a 3D viewport to playblast")
            return {"CANCELLED"}
        self._area, self._region, self._space = view
        self._domain_name = domain.name
        scene = context.scene
        self._start, self._end = simulation_frame_range(domain)
        self._original_frame = scene.frame_current
        self._width, self._height = self._region.width, self._region.height
        self._directory = new_playblast_directory(domain)
        self._names = []
        self._depth_serial = 1 << 30
        session, self._participants = exporters.build_session(
            context, start_frame=self._start, end_frame=self._end, domain=domain,
            write_vdb=False, preview_enabled=True)
        self._worker = BridgeWorker(exporters.bridge_executable(), session, keep_alive=True)
        self._worker.start()
        self._accepted = False
        self._frame = self._start
        self._waiting = False
        self._image = bpy.data.images.new("Fumaris Playblast", self._width, self._height, alpha=False)
        self._started = time.monotonic()
        self._timer = context.window_manager.event_timer_add(0.001, window=context.window)
        context.window_manager.modal_handler_add(self)
        context.workspace.status_text_set(f"Fumaris playblast: starting ({self._start}–{self._end}), Esc to stop")
        return {"RUNNING_MODAL"}

    # Viewport -----------------------------------------------------------------

    def _draw_viewport(self, context):
        """The viewport as shown (no overlays), display-encoded RGBA, and its depth."""
        import gpu
        space, region = self._space, self._region
        region_3d = space.region_3d
        overlays = space.overlay.show_overlays
        space.overlay.show_overlays = False
        offscreen = gpu.types.GPUOffScreen(self._width, self._height)
        try:
            offscreen.draw_view3d(context.scene, context.view_layer, space, region,
                                  region_3d.view_matrix, region_3d.window_matrix, do_color_management=True)
            with offscreen.bind():
                framebuffer = gpu.state.active_framebuffer_get()
                color = framebuffer.read_color(0, 0, self._width, self._height, 4, 0, "FLOAT")
                color.dimensions = self._width * self._height * 4
                colors = np.array(color, dtype=np.float32).reshape(self._height, self._width, 4)
                depth = framebuffer.read_depth(0, 0, self._width, self._height)
                depth.dimensions = self._width * self._height
                depths = np.array(depth, dtype=np.float32).reshape(self._height, self._width)
        finally:
            offscreen.free()
            space.overlay.show_overlays = overlays
        return colors, depths

    def _request(self, context, domain, depths):
        from . import preview
        region_3d = self._space.region_3d
        request = preview.capture_volume_preview(context, domain, (self._region, region_3d))
        # Full viewport size (whatever Preview Resolution is), with this frame's depth.
        scale = min(1.0, 2048.0 / self._width, 2048.0 / self._height)
        width, height = max(16, int(round(self._width * scale))), max(16, int(round(self._height * scale)))
        request["width"], request["height"] = width, height
        request.pop(protocol.SCENE_DEPTH_KEY, None)
        request.pop("depth_serial", None)
        if getattr(domain.fumaris, "preview_occlusion", False):
            self._depth_serial += 1
            request["depth_serial"] = self._depth_serial
            request[protocol.SCENE_DEPTH_KEY] = preview.sample_scene_depth(
                depths, width, height, getattr(region_3d, "is_perspective", True))
        return request

    def _write_frame(self, background, data, payload):
        smoke = data.get("preview") or {}
        width, height = int(smoke.get("width", 0)), int(smoke.get("height", 0))
        frame = background[..., :3].copy()
        if payload and width * height * 4 == len(payload):
            image = np.frombuffer(payload, dtype=np.uint8).reshape(height, width, 4)[::-1].astype(np.float32) / 255.0
            if (width, height) != (self._width, self._height):
                rows = ((np.arange(self._height) + 0.5) * (height / self._height)).astype(np.intp)
                columns = ((np.arange(self._width) + 0.5) * (width / self._width)).astype(np.intp)
                image = image[rows[:, None], columns[None, :]]
            # Premultiplied smoke (glow may exceed its alpha) over the viewport.
            frame = np.minimum(image[..., :3] + frame * (1.0 - image[..., 3:4]), 1.0)
        pixels = np.concatenate([frame, np.ones((self._height, self._width, 1), np.float32)], axis=2)
        self._image.pixels.foreach_set(pixels.ravel())
        name = f"frame_{len(self._names) + 1:05d}.png"
        self._image.filepath_raw = os.path.join(self._directory, name)
        self._image.file_format = "PNG"
        self._image.save()
        self._names.append(name)

    # Modal --------------------------------------------------------------------

    def modal(self, context, event):
        if event.type == "ESC":
            self._finish(context, cancelled=True)
            return {"CANCELLED"}
        if event.type != "TIMER":
            return {"PASS_THROUGH"}
        domain = bpy.data.objects.get(self._domain_name)
        if domain is None:
            self._finish(context, cancelled=True)
            return {"CANCELLED"}
        try:
            for kind, data, payload in self._worker.poll():
                if kind == protocol.FAILED:
                    raise RuntimeError(str((data or {}).get("message", data)))
                if kind == protocol.SESSION_ACCEPTED:
                    self._accepted = True
                elif kind == protocol.FRAME_COMPLETE and self._waiting:
                    self._write_frame(self._background, data, payload)
                    self._waiting = False
                    self._frame += 1
            if not self._accepted or self._waiting:
                return {"RUNNING_MODAL"}
            if self._frame > self._end:
                return self._complete(context)
            scene = context.scene
            scene.frame_set(self._frame)
            self._background, depths = self._draw_viewport(context)
            request = self._request(context, domain, depths)
            self._worker.send_frame(exporters.build_frame(
                context, self._frame, self._participants, domain=domain,
                preview_enabled=True, volume_preview=request))
            self._waiting = True
            done = self._frame - self._start
            context.workspace.status_text_set(
                f"Fumaris playblast: frame {self._frame} ({done}/{self._end - self._start + 1}), Esc to stop")
        except Exception as error:  # noqa: BLE001 - report and clean up
            self.report({"ERROR"}, f"Playblast failed: {error}")
            console_log(f"Fumaris playblast failed: {error}")
            self._finish(context, cancelled=True)
            return {"CANCELLED"}
        return {"RUNNING_MODAL"}

    def _keep(self, context, movie=None):
        """List the playblast in the domain's Versions: settings, frames, movie."""
        from . import cache, compare
        domain = bpy.data.objects.get(self._domain_name)
        if domain is None or not self._names:
            return
        values = cache.settings_snapshot_data(domain, self._participants)
        values.update(frames=[self._start, self._start + len(self._names) - 1],
                      movie=os.path.basename(movie) if movie and os.path.isfile(movie) else "")
        compare.write_sidecar(self._directory, **values)
        domain.fumaris.compare_selected = "blast:" + self._directory
        compare.forget_scan()

    def _complete(self, context):
        self._finish(context, cancelled=False)
        movie = os.path.join(self._directory, MOVIE_NAME)
        scene = context.scene
        try:
            _encode_movie(self._directory, self._names, self._width, self._height,
                          scene.render.fps, scene.render.fps_base, movie)
            self.report({"INFO"}, f"Playblast saved in Versions ({len(self._names)} frames, "
                                  f"{time.monotonic() - self._started:.0f} s)")
        except Exception as error:  # noqa: BLE001 - the PNG frames are still there
            self.report({"WARNING"}, f"Playblast frames saved in Versions; the movie failed: {error}")
        self._keep(context, movie)
        return {"FINISHED"}

    def _finish(self, context, cancelled):
        wm = context.window_manager
        if getattr(self, "_timer", None) is not None:
            wm.event_timer_remove(self._timer)
            self._timer = None
        worker = getattr(self, "_worker", None)
        if worker is not None:
            try:
                if not cancelled:
                    worker.end_session()
            finally:
                worker.close(timeout=5)
            self._worker = None
        image = getattr(self, "_image", None)
        if image is not None:
            bpy.data.images.remove(image)
            self._image = None
        context.workspace.status_text_set(None)
        context.scene.frame_set(self._original_frame)
        if cancelled and self._names:
            # The frames made so far stay in Versions (no movie).
            self._keep(context)
            self.report({"INFO"}, f"Playblast stopped: {len(self._names)} frames kept in Versions")
        elif cancelled:
            import shutil
            shutil.rmtree(self._directory, ignore_errors=True)


CLASSES = (FUMARIS_OT_playblast,)


def register():
    for cls in CLASSES:
        bpy.utils.register_class(cls)


def unregister():
    for cls in reversed(CLASSES):
        bpy.utils.unregister_class(cls)
