import time

import bpy
import gpu

PREVIEW_MARKER = "fumaris_preview"
LEGACY_PREVIEW_MARKER = "plume_forge_preview"
PREVIEW_NAME = "Fumaris Preview"
_PREVIEWS = {}
_DRAW_HANDLE = None
_SHADER = None
_VERTEX_FORMAT = None


def update_density_points(domain, data, payload):
    started = time.perf_counter()
    preview = data.get("preview") or {}
    if preview.get("type") != "density_points" or not payload:
        clear_preview(domain)
        return 0.0

    count = int(preview.get("count", 0))
    if count <= 0:
        clear_preview(domain)
        return 0.0

    stride = int(preview.get("stride", 0))
    expected_bytes = count * stride * 4
    if stride != 3 or len(payload) != expected_bytes:
        raise RuntimeError(
            f"Invalid preview payload: count={count}, stride={stride}, "
            f"bytes={len(payload)}"
        )
    positions = memoryview(payload).cast("f", shape=[count, 3])
    vertex_buffer = gpu.types.GPUVertBuf(format=_vertex_format(), len=count)
    vertex_buffer.attr_fill("pos", positions)
    batch = gpu.types.GPUBatch(type="POINTS", buf=vertex_buffer)
    _PREVIEWS[domain.name] = {
        "domain_name": domain.name,
        "point_size": max(0.001, float(preview.get("point_size", 0.1))),
        "buffer": vertex_buffer,
        "batch": batch,
    }
    _ensure_draw_handler()
    _tag_viewports()
    return (time.perf_counter() - started) * 1000.0


def show_preview_payload(domain, data, payload):
    return update_density_points(domain, data, payload)


def clear_preview(domain):
    _PREVIEWS.pop(domain.name, None)
    _remove_legacy_preview_objects(domain.name)
    if not _PREVIEWS:
        _remove_draw_handler()
    _tag_viewports()


def clear_all_previews():
    _PREVIEWS.clear()
    _remove_draw_handler()
    for obj in list(bpy.data.objects):
        if obj.get(PREVIEW_MARKER) or obj.get(LEGACY_PREVIEW_MARKER):
            _remove_object(obj)
    _tag_viewports()


def refresh_preview_display():
    _tag_viewports()


def _draw_previews():
    if not _PREVIEWS:
        return
    region = bpy.context.region
    if region is None:
        return
    shader = _shader()
    uses_program_point_size = gpu.platform.backend_type_get() == "OPENGL"
    gpu.state.blend_set("ALPHA")
    if uses_program_point_size:
        gpu.state.program_point_size_set(True)
    try:
        projection = gpu.matrix.get_projection_matrix()
        shader.bind()
        shader.uniform_float(
            "ModelViewProjectionMatrix",
            projection @ gpu.matrix.get_model_view_matrix(),
        )
        shader.uniform_float("ProjectionMatrix", projection)
        shader.uniform_float("viewportSize", (region.width, region.height))
        for preview in tuple(_PREVIEWS.values()):
            domain = bpy.data.objects.get(preview["domain_name"])
            if domain is None:
                continue
            shader.uniform_float("worldSize", _world_point_size(domain, preview))
            shader.uniform_float("color", _point_color(domain))
            preview["batch"].draw(shader)
    finally:
        if uses_program_point_size:
            gpu.state.program_point_size_set(False)
        gpu.state.blend_set("NONE")


def _ensure_draw_handler():
    global _DRAW_HANDLE
    if _DRAW_HANDLE is None:
        _DRAW_HANDLE = bpy.types.SpaceView3D.draw_handler_add(
            _draw_previews,
            (),
            "WINDOW",
            "POST_VIEW",
        )


def _remove_draw_handler():
    global _DRAW_HANDLE
    if _DRAW_HANDLE is not None:
        try:
            bpy.types.SpaceView3D.draw_handler_remove(_DRAW_HANDLE, "WINDOW")
        except (ReferenceError, ValueError):
            pass
        _DRAW_HANDLE = None


def _shader():
    global _SHADER
    if _SHADER is None:
        info = gpu.types.GPUShaderCreateInfo()
        info.push_constant("MAT4", "ModelViewProjectionMatrix")
        info.push_constant("MAT4", "ProjectionMatrix")
        info.push_constant("VEC2", "viewportSize")
        info.push_constant("FLOAT", "worldSize")
        info.push_constant("VEC4", "color")
        info.vertex_in(0, "VEC3", "pos")
        info.fragment_out(0, "VEC4", "FragColor")
        info.vertex_source("""
            void main()
            {
                vec4 clip = ModelViewProjectionMatrix * vec4(pos, 1.0);
                gl_Position = clip;
                float depth = max(abs(clip.w), 1e-6);
                gl_PointSize = max(
                    1.0,
                    worldSize * viewportSize.y * 0.5 *
                    abs(ProjectionMatrix[1][1]) / depth
                );
            }
        """)
        info.fragment_source("""
            void main()
            {
                FragColor = color;
            }
        """)
        _SHADER = gpu.shader.create_from_info(info)
    return _SHADER


def _vertex_format():
    global _VERTEX_FORMAT
    if _VERTEX_FORMAT is None:
        _VERTEX_FORMAT = gpu.types.GPUVertFormat()
        _VERTEX_FORMAT.attr_add(
            id="pos",
            comp_type="F32",
            len=3,
            fetch_mode="FLOAT",
        )
    return _VERTEX_FORMAT


def _world_point_size(domain, preview):
    scale = max(0.05, float(getattr(domain.fumaris, "preview_dot_size", 1.0)))
    return preview["point_size"] * scale * 0.1


def _point_color(domain):
    props = domain.fumaris
    color = tuple(float(value) for value in getattr(props, "preview_color", (0.35, 0.65, 1.0)))
    opacity = max(0.0, min(1.0, float(getattr(props, "preview_opacity", 0.65))))
    return (*color[:3], opacity)


def _remove_legacy_preview_objects(domain_name):
    for obj in list(bpy.data.objects):
        if (
            obj.get(PREVIEW_MARKER) == domain_name
            or obj.get(LEGACY_PREVIEW_MARKER) == domain_name
        ):
            _remove_object(obj)


def _remove_object(obj):
    data_block = obj.data if obj.type in {"MESH", "POINTCLOUD"} else None
    bpy.data.objects.remove(obj, do_unlink=True)
    if data_block and data_block.users == 0:
        if data_block.id_type == "POINTCLOUD":
            bpy.data.pointclouds.remove(data_block)
        elif data_block.id_type == "MESH":
            bpy.data.meshes.remove(data_block)


def _tag_viewports():
    try:
        for window in bpy.context.window_manager.windows:
            for area in window.screen.areas:
                if area.type == "VIEW_3D":
                    area.tag_redraw()
    except Exception:
        pass
