"""Viewport display of Fumaris effectors: influence bounds and direction.

Drawn for every visible effector object (brighter when selected), in the same
world-space shape the bridge simulates (exporters._effector_state and
src/effector_runtime.cpp): rotation and position from the object, sizes in
world units (object scale does not change them).
"""
import math

import bpy
import gpu
from gpu_extras.batch import batch_for_shader
from mathutils import Vector

_HANDLE = None
_SEGMENTS = 48
_COLOR = (0.35, 0.85, 1.0, 0.45)
_COLOR_SELECTED = (0.55, 0.95, 1.0, 0.95)
_COLOR_INNER = (1.0, 0.75, 0.3, 0.7)


def _circle(matrix, radius, axis, z=0.0):
    """Line-segment pairs of a circle in a local plane (axis: normal 'X'/'Y'/'Z')."""
    points = []
    for index in range(_SEGMENTS):
        for step in (index, index + 1):
            angle = 2.0 * math.pi * step / _SEGMENTS
            a, b = radius * math.cos(angle), radius * math.sin(angle)
            local = {"Z": (a, b, z), "Y": (a, z, b), "X": (z, a, b)}[axis]
            points.append(matrix @ Vector(local))
    return points


def _arrow(matrix, start, end, head):
    start, end = Vector(start), Vector(end)
    direction = end - start
    if direction.length < 1e-6:
        return []
    side = direction.orthogonal().normalized() * head
    other = direction.cross(side).normalized() * head
    back = end - direction.normalized() * head * 2.0
    local = [start, end, end, back + side, end, back - side, end, back + other, end, back - other]
    return [matrix @ point for point in local]


def effector_lines(obj):
    """(outline, inner) world-space segment point lists for one effector."""
    props = obj.fumaris
    world = obj.matrix_world
    matrix = world.to_quaternion().to_matrix().to_4x4()
    matrix.translation = world.translation
    effector = props.effector_type
    radius = max(0.001, props.effector_radius)
    if effector != "vortex" and props.effector_use_max_distance and props.effector_max_distance > 0.0:
        radius = max(0.001, props.effector_max_distance)
    outline, inner = [], []
    if effector == "vortex":
        height = max(0.001, props.effector_vortex_height)
        bottom = {"positive": 0.0, "negative": -height}.get(props.effector_z_direction, -0.5 * height)
        top = bottom + height
        core = min(radius, max(0.001, props.effector_vortex_core_radius))
        for z in (bottom, top):
            outline += _circle(matrix, radius, "Z", z)
            inner += _circle(matrix, core, "Z", z)
        for index in range(4):
            angle = 0.5 * math.pi * index
            x, y = radius * math.cos(angle), radius * math.sin(angle)
            outline += [matrix @ Vector((x, y, bottom)), matrix @ Vector((x, y, top))]
        sign = 1.0 if props.effector_strength >= 0.0 else -1.0
        # Spin direction: a short tangent arrow on the core at mid height.
        mid = 0.5 * (bottom + top)
        inner += _arrow(matrix, (core, 0.0, mid), (core, sign * core, mid), 0.12 * core)
        if abs(props.effector_vortex_lift) > 1e-6:
            lift = math.copysign(0.5 * height, props.effector_vortex_lift)
            inner += _arrow(matrix, (0.0, 0.0, mid), (0.0, 0.0, mid + lift), 0.08 * radius)
        return outline, inner
    for axis in ("X", "Y", "Z"):
        outline += _circle(matrix, radius, axis)
    if props.effector_use_min_distance and props.effector_min_distance > 0.0:
        minimum = min(radius, props.effector_min_distance)
        for axis in ("X", "Y", "Z"):
            inner += _circle(matrix, minimum, axis)
    strength = props.effector_strength
    head = 0.08 * radius
    if effector == "wind":
        inner += _arrow(matrix, (0.0, 0.0, 0.0), (0.0, 0.0, math.copysign(radius, strength or 1.0)), head)
    elif effector == "force":
        for direction in ((1, 0, 0), (-1, 0, 0), (0, 1, 0), (0, -1, 0), (0, 0, 1), (0, 0, -1)):
            near, far = Vector(direction) * 0.35 * radius, Vector(direction) * 0.8 * radius
            if strength < 0.0:
                near, far = far, near
            inner += _arrow(matrix, near, far, head)
    if props.effector_z_direction != "both":
        # Only one side of local Z acts: mark the active half with a disc.
        inner += _circle(matrix, radius, "Z")
    return outline, inner


def _draw():
    context = bpy.context
    view_layer = context.view_layer
    space = context.space_data
    selected_lines, lines, inner_lines = [], [], []
    for obj in context.scene.objects:
        props = getattr(obj, "fumaris", None)
        if props is None or props.smoke_object_type != "effector":
            continue
        try:
            if not obj.visible_get(view_layer=view_layer, viewport=space):
                continue
        except (RuntimeError, ReferenceError):
            continue
        outline, inner = effector_lines(obj)
        (selected_lines if obj.select_get(view_layer=view_layer) else lines).extend(outline)
        inner_lines.extend(inner)
    if not (lines or selected_lines or inner_lines):
        return
    shader = gpu.shader.from_builtin("UNIFORM_COLOR")
    gpu.state.blend_set("ALPHA")
    gpu.state.depth_test_set("LESS_EQUAL")
    try:
        for points, color in ((lines, _COLOR), (selected_lines, _COLOR_SELECTED), (inner_lines, _COLOR_INNER)):
            if not points:
                continue
            batch = batch_for_shader(shader, "LINES", {"pos": [tuple(p) for p in points]})
            shader.bind()
            shader.uniform_float("color", color)
            batch.draw(shader)
    finally:
        gpu.state.blend_set("NONE")
        gpu.state.depth_test_set("NONE")


def register():
    global _HANDLE
    if _HANDLE is None and not bpy.app.background:
        _HANDLE = bpy.types.SpaceView3D.draw_handler_add(_draw, (), "WINDOW", "POST_VIEW")


def unregister():
    global _HANDLE
    if _HANDLE is not None:
        bpy.types.SpaceView3D.draw_handler_remove(_HANDLE, "WINDOW")
        _HANDLE = None
