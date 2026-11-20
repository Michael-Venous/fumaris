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
import struct
import tempfile
import time
import zlib
from concurrent.futures import ThreadPoolExecutor

import bpy
from bpy.types import Operator

from . import exporters, protocol
from .bridge import BridgeWorker
from .diagnostics import console_log
from .runtime import active_job, blocking_job
from .utils import simulation_frame_range

try:
    import numpy as np
except ImportError:  # pragma: no cover - Blender ships numpy
    np = None


def _largest_view(context):
    """(area, region, space) of the 3D viewport to record: the one Save
    Playblast was started from, else the largest in this window."""
    area = getattr(context, "area", None)
    if area is not None and area.type == "VIEW_3D":
        region = next((r for r in area.regions if r.type == "WINDOW"), None)
        if region is not None:
            return area, region, area.spaces.active
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


# A playblast through a camera is at most this many pixels on its longer side
# (the live smoke is drawn at up to 2048 too).
CAMERA_FRAME_LIMIT = 2048


def view_camera(context, space):
    """The camera a viewport looks through, or None when it does not."""
    region_3d = space.region_3d
    if region_3d is None or region_3d.view_perspective != "CAMERA":
        return None
    if getattr(space, "use_local_camera", False) and space.camera is not None:
        return space.camera
    return context.scene.camera


def camera_frame_size(scene):
    """(width, height) of a playblast through the scene's camera: the render
    resolution (with its %) at its displayed aspect, the longer side at most
    CAMERA_FRAME_LIMIT."""
    render = scene.render
    scale = render.resolution_percentage / 100.0
    width = max(1.0, render.resolution_x * scale * render.pixel_aspect_x)
    height = max(1.0, render.resolution_y * scale * render.pixel_aspect_y)
    # Square pixels at the render's height (or width, for portrait frames).
    if render.pixel_aspect_x != render.pixel_aspect_y:
        unit = min(render.pixel_aspect_x, render.pixel_aspect_y)
        width, height = width / unit, height / unit
    fit = min(1.0, CAMERA_FRAME_LIMIT / max(width, height))
    return max(16, int(round(width * fit))), max(16, int(round(height * fit)))


def recorded_view(context, space, region, size=None):
    """What a playblast frame shows: (view matrix, projection matrix,
    perspective). The viewport's own view for a free view, which the
    region's size frames; through a camera, the camera's frame (what a
    render shows, inside the passepartout), at `size`."""
    region_3d = space.region_3d
    camera = view_camera(context, space)
    if camera is None or size is None:
        return region_3d.view_matrix.copy(), region_3d.window_matrix.copy(), bool(region_3d.is_perspective)
    render = context.scene.render
    depsgraph = context.evaluated_depsgraph_get()
    evaluated = camera.evaluated_get(depsgraph)
    projection = evaluated.calc_matrix_camera(
        depsgraph,
        x=max(1, int(render.resolution_x * render.resolution_percentage / 100)),
        y=max(1, int(render.resolution_y * render.resolution_percentage / 100)),
        scale_x=render.pixel_aspect_x,
        scale_y=render.pixel_aspect_y,
    )
    view = evaluated.matrix_world.normalized().inverted()
    perspective = getattr(camera.data, "type", "PERSP") != "ORTHO"
    return view, projection, perspective


class _View:
    """The region and region_3d preview.capture_volume_preview reads, for a
    view that is not the region's own (a camera frame)."""

    def __init__(self, region, width, height, view, projection, perspective):
        self.width, self.height = width, height
        self.view_matrix, self.window_matrix = view, projection
        self.perspective_matrix = projection @ view
        self.is_perspective = perspective
        self._pointer = region.as_pointer()

    def as_pointer(self):
        return self._pointer


PLAYBLAST_SUFFIX = "_playblast"
MOVIE_NAME = "playblast.mp4"
PNG_END = b"\x00\x00\x00\x00IEND\xaeB\x60\x82"


def _png_header(path):
    """Keep Blender's RGB PNG header, including its colour profile and metadata."""
    with open(path, "rb") as file:
        header = bytearray(file.read(8))
        while True:
            chunk = file.read(8)
            if len(chunk) != 8:
                raise RuntimeError("Incomplete playblast PNG")
            size, kind = struct.unpack(">I4s", chunk)
            if kind == b"IDAT":
                return bytes(header)
            header.extend(chunk)
            header.extend(file.read(size + 4))


def _save_png(path, header, frame):
    """Write an RGB frame off the main thread. Publish only complete files."""
    # PNG Sub filter: each byte is stored relative to the same channel of
    # the preceding pixel. Blender's framebuffer rows start at the bottom.
    top_down = frame[::-1]
    scanlines = np.empty((frame.shape[0], 1 + 3 * frame.shape[1]), dtype=np.uint8)
    scanlines[:, 0] = 1
    scanlines[:, 1:4] = top_down[:, 0]
    np.subtract(top_down[:, 1:], top_down[:, :-1], out=scanlines[:, 4:].reshape(frame.shape[0], -1, 3))
    data = zlib.compress(scanlines, level=1)
    temporary = path + ".tmp"
    try:
        with open(temporary, "wb") as file:
            file.write(header)
            file.write(struct.pack(">I4s", len(data), b"IDAT"))
            file.write(data)
            file.write(struct.pack(">I", zlib.crc32(data, zlib.crc32(b"IDAT"))))
            file.write(PNG_END)
        os.replace(temporary, path)
    finally:
        if os.path.exists(temporary):
            os.remove(temporary)


def _composite_frame(background, preview, payload):
    frame = background[..., :3]
    width, height = int(preview.get("width", 0)), int(preview.get("height", 0))
    if not payload or width * height * 4 != len(payload):
        return frame.copy()
    image = np.frombuffer(payload, dtype=np.uint8).reshape(height, width, 4)[::-1]
    target_height, target_width = frame.shape[:2]
    if (width, height) != (target_width, target_height):
        rows = ((np.arange(target_height) + 0.5) * (height / target_height)).astype(np.intp)
        columns = ((np.arange(target_width) + 0.5) * (width / target_width)).astype(np.intp)
        image = image[rows[:, None], columns[None, :]]
    # Both inputs are 8-bit display colours. Round B * (255 - A) / 255 to
    # the nearest byte before adding premultiplied smoke. Since 255 is odd,
    # there is no half-way case. The glow can exceed its alpha.
    value = np.multiply(frame, 255 - image[..., 3:4], dtype=np.uint16)
    value += 127
    value //= 255
    value += image[..., :3]
    np.minimum(value, 255, out=value)
    return value.astype(np.uint8)


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
                      "Records the 3D viewport it is started from (else the largest) as it is shown, without "
                      "overlays; looking through a camera, the camera's frame at the render resolution. "
                      "Esc stops it")
    bl_options = {"REGISTER"}

    domain_name: bpy.props.StringProperty(options={"HIDDEN", "SKIP_SAVE"})
    # Asked before recording (Brent, 2026-11-19: playblasts of the same frames
    # looked alike in Versions). Passed by a script: no dialog.
    name: bpy.props.StringProperty(name="Name", description="How this playblast is listed in Versions",
                                   options={"SKIP_SAVE"})

    @classmethod
    def poll(cls, context):
        return np is not None and blocking_job() is None and _largest_view(context) is not None

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
        if _largest_view(context) is None:
            self.report({"WARNING"}, "Open a 3D viewport to playblast")
            return {"CANCELLED"}
        if self.name.strip():
            return self.execute(context)
        start, end = simulation_frame_range(domain)
        self.name = f"Playblast · frames {start}–{end}"
        return context.window_manager.invoke_props_dialog(
            self, width=320, title="Save Playblast", confirm_text="Record")

    def draw(self, _context):
        layout = self.layout
        layout.prop(self, "name")
        layout.label(text="Kept on disk in Versions, not in the .blend", icon="INFO")

    def execute(self, context):
        domain = self._domain(context)
        view = _largest_view(context)
        if domain is None or view is None:
            self.report({"WARNING"}, "Choose a Fumaris domain and open a 3D viewport")
            return {"CANCELLED"}
        self._area, self._region, self._space = view
        self._domain_name = domain.name
        scene = context.scene
        self._start, self._end = simulation_frame_range(domain)
        self._original_frame = scene.frame_current
        # Framed like the viewport: the region's size for a free view; through
        # a camera, the camera frame at the render's aspect (R37, Brent).
        self._camera = view_camera(context, self._space) is not None
        if self._camera:
            self._width, self._height = camera_frame_size(scene)
        else:
            self._width, self._height = self._region.width, self._region.height
        self._directory = new_playblast_directory(domain)
        self._names = []
        # Listed and selected from its first frame under its name, so the list
        # never shows another playblast as the one being made.
        from . import compare
        self._note = self.name.strip() or f"Playblast · frames {self._start}–{self._end}"
        compare.write_sidecar(self._directory, note=self._note, frames=[self._start, self._end])
        domain.fumaris.compare_selected = "blast:" + self._directory
        compare.forget_scan()
        self._depth_serial = 1 << 30
        session, self._participants = exporters.build_session(
            context, start_frame=self._start, end_frame=self._end, domain=domain,
            write_vdb=False, preview_enabled=True)
        self._worker = BridgeWorker(exporters.bridge_executable(), session, keep_alive=True)
        self._worker.start()
        self._accepted = False
        self._frame = self._start
        self._waiting = False
        self._png_writer = ThreadPoolExecutor(max_workers=1)
        self._png_task = None
        self._png_header = None
        self._started = time.monotonic()
        self._timer = context.window_manager.event_timer_add(0.001, window=context.window)
        context.window_manager.modal_handler_add(self)
        context.workspace.status_text_set(f"Fumaris playblast: starting ({self._start}–{self._end}), Esc to stop")
        return {"RUNNING_MODAL"}

    # Viewport -----------------------------------------------------------------

    def _draw_viewport(self, context, read_depth):
        """The viewport as shown (no overlays), display-encoded RGBA, and its depth."""
        import gpu
        space, region = self._space, self._region
        view, projection, self._perspective = recorded_view(
            context, space, region, (self._width, self._height) if self._camera else None)
        self._view = (view, projection)
        overlays = space.overlay.show_overlays
        space.overlay.show_overlays = False
        offscreen = gpu.types.GPUOffScreen(self._width, self._height)
        try:
            offscreen.draw_view3d(context.scene, context.view_layer, space, region,
                                  view, projection, do_color_management=True)
            with offscreen.bind():
                framebuffer = gpu.state.active_framebuffer_get()
                color = framebuffer.read_color(0, 0, self._width, self._height, 4, 0, "UBYTE")
                colors = np.frombuffer(color, dtype=np.uint8).reshape(self._height, self._width, 4)
                depths = None
                if read_depth:
                    depth = framebuffer.read_depth(0, 0, self._width, self._height)
                    depths = np.frombuffer(depth, dtype=np.float32).reshape(self._height, self._width)
        finally:
            offscreen.free()
            space.overlay.show_overlays = overlays
        return colors, depths

    def _request(self, context, domain, depths):
        from . import preview
        view, projection = self._view
        target = _View(self._region, self._width, self._height, view, projection, self._perspective)
        request = preview.capture_volume_preview(context, domain, (target, target))
        # Full viewport size (whatever Preview Resolution is), with this frame's depth.
        scale = min(1.0, 2048.0 / self._width, 2048.0 / self._height)
        width, height = max(16, int(round(self._width * scale))), max(16, int(round(self._height * scale)))
        request["width"], request["height"] = width, height
        request.pop(protocol.SCENE_DEPTH_KEY, None)
        request.pop("depth_serial", None)
        if depths is not None:
            self._depth_serial += 1
            request["depth_serial"] = self._depth_serial
            request[protocol.SCENE_DEPTH_KEY] = preview.sample_scene_depth(
                depths, width, height, self._perspective)
        return request

    def _write_frame(self, background, data, payload):
        frame = _composite_frame(background, data.get("preview") or {}, payload)
        self._flush_png()
        name = f"frame_{len(self._names) + 1:05d}.png"
        path = os.path.join(self._directory, name)
        if self._png_header is None:
            from .compare import save_png
            pixels = np.ones((*frame.shape[:2], 4), dtype=np.float32)
            pixels[..., :3] = frame / np.float32(255)
            save_png(pixels, path)
            self._png_header = _png_header(path)
            self._names.append(name)
        else:
            self._png_task = (name, self._png_writer.submit(_save_png, path, self._png_header, frame))

    def _flush_png(self):
        if self._png_task is not None:
            name, task = self._png_task
            self._png_task = None
            task.result()
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
            self._background, depths = self._draw_viewport(context, domain.fumaris.preview_occlusion)
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
                      movie=os.path.basename(movie) if movie and os.path.isfile(movie) else "",
                      note=self._note)
        compare.write_sidecar(self._directory, **values)
        domain.fumaris.compare_selected = "blast:" + self._directory
        compare.forget_scan()
        # An Image Editor showing a playblast now shows this one (it kept the
        # previous movie while the list moved on, 2026-11-19).
        entry = compare.find_entry(domain, "blast:" + self._directory, fresh=True)
        if entry is not None:
            compare.refresh_playblast_viewers(context, entry)

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
        if self._png_writer is not None:
            try:
                self._flush_png()
            except Exception as error:
                if not cancelled:
                    raise
                self.report({"WARNING"}, f"Could not save the last playblast frame: {error}")
            finally:
                self._png_writer.shutdown()
                self._png_writer = None
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
