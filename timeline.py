"""The live preview's cache on Blender's Timeline.

Blender draws its own cache bar only for built-in physics caches, so this
draws a thin strip along the bottom of the Timeline while a cached preview
runs: blue where a frame is cached, orange where a cached frame comes from a
run that mixed settings (the next pass simulates and replaces it).
"""
import bpy
import gpu
from gpu_extras.batch import batch_for_shader

from .runtime import active_job, active_mode

CACHED = (0.30, 0.62, 1.00, 0.85)
STALE = (1.00, 0.52, 0.18, 0.90)
HEIGHT = 4.0   # pixels at UI scale 1
_handle = None


def _spans(frames):
    """Consecutive frames as (first, last) spans."""
    spans = []
    for frame in sorted(frames):
        if spans and frame == spans[-1][1] + 1:
            spans[-1][1] = frame
        else:
            spans.append([frame, frame])
    return spans


def _draw():
    context = bpy.context
    space = context.space_data
    if space is None or getattr(space, "mode", "") != "TIMELINE":
        return
    job = active_job()
    if job is None or active_mode() != "previewing" or not job._worker._session.get("preview_cache"):
        return
    from .operators import cached_frames, stale_frames
    frames = cached_frames(job)
    if not frames:
        return
    stale = stale_frames(job, bpy.data.objects.get(getattr(job, "_domain_name", "")))
    region = context.region
    view = region.view2d
    scale = context.preferences.system.ui_scale or 1.0
    bottom, top = 1.0, 1.0 + HEIGHT * scale
    shader = gpu.shader.from_builtin("UNIFORM_COLOR")
    gpu.state.blend_set("ALPHA")
    for color, group in ((CACHED, frames - stale), (STALE, stale)):
        vertices, indices = [], []
        for first, last in _spans(group):
            left = view.view_to_region(first - 0.5, 0.0, clip=False)[0]
            right = view.view_to_region(last + 0.5, 0.0, clip=False)[0]
            if right < 0 or left > region.width:
                continue
            base = len(vertices)
            vertices += [(left, bottom), (right, bottom), (right, top), (left, top)]
            indices += [(base, base + 1, base + 2), (base, base + 2, base + 3)]
        if vertices:
            batch = batch_for_shader(shader, "TRIS", {"pos": vertices}, indices=indices)
            shader.uniform_float("color", color)
            batch.draw(shader)
    gpu.state.blend_set("NONE")


def register():
    global _handle
    if _handle is None and not bpy.app.background:
        _handle = bpy.types.SpaceDopeSheetEditor.draw_handler_add(_draw, (), "WINDOW", "POST_PIXEL")


def unregister():
    global _handle
    if _handle is not None:
        bpy.types.SpaceDopeSheetEditor.draw_handler_remove(_handle, "WINDOW")
        _handle = None
