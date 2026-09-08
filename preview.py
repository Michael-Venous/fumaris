import time
from array import array

import bpy
import gpu
from mathutils import Matrix
from . import diagnostics
from .utils import flame_temperature_range

try:
    import numpy as np
except ImportError:
    np = None

PREVIEW_MARKER = "fumaris_preview"
LEGACY_PREVIEW_MARKER = "plume_forge_preview"
PREVIEW_NAME = "Fumaris Preview"
_PREVIEWS = {}
_POINT_DRAW_HANDLE = None
_IMAGE_DRAW_HANDLE = None
_POINT_SHADER = None
_POINT_VERTEX_FORMAT = None
_IMAGE_SHADER = None
_IMAGE_BATCH = None
_IMAGE_UPLOAD_BUFFER = None
_IMAGE_UPLOAD_SHAPE = None


def capture_volume_preview(context, domain):
    props = domain.fumaris
    if getattr(props, "preview_mode", "points") != "volume":
        return {"valid": False}
    target = _largest_viewport(context, domain)
    if target is None:
        return {"valid": False}

    region, region_3d = target
    scale = max(0.25, min(1.0, float(props.preview_image_scale) / 100.0))
    width = max(16, int(round(region.width * scale)))
    height = max(16, int(round(region.height * scale)))
    dimension_scale = min(1.0, 2048.0 / width, 2048.0 / height)
    width = max(16, int(round(width * dimension_scale)))
    height = max(16, int(round(height * dimension_scale)))

    projection = getattr(region_3d, "window_matrix", None)
    if projection is None:
        projection = region_3d.perspective_matrix @ region_3d.view_matrix.inverted()
    projection = _zero_to_one_projection(projection)
    fps_base = max(1e-6, float(context.scene.render.fps_base))
    fps = float(context.scene.render.fps) / fps_base
    simulation_speed = max(1e-6, float(props.simulation_speed))
    flame_rate_scale = fps * float(props.num_sub_steps) / simulation_speed
    flame_start, flame_full = flame_temperature_range(
        float(props.flame_temperature_min), float(props.flame_temperature_max))
    return {
        "valid": True,
        "width": width,
        "height": height,
        "view_id": int(region.as_pointer()),
        "view": _flatten_matrix(region_3d.view_matrix),
        "projection": _flatten_matrix(projection),
        "max_ray_steps": int(props.preview_max_ray_steps),
        "smoke_density": float(props.shader_smoke_density),
        "smoke_color": [float(value) for value in props.shader_smoke_color],
        "flame_enabled": bool(props.shader_flame_enabled),
        "flame_brightness": float(props.shader_flame_brightness),
        "flame_temperature_min": flame_start,
        "flame_temperature_max": flame_full,
        "flame_rate_scale": flame_rate_scale,
        "temperature_multiplier": float(props.shader_temperature_multiplier),
        "shadows": bool(props.preview_shadows),
        "shadow_min_light": float(props.preview_shadow_min_light),
        "exposure": float(props.preview_exposure),
        "tone_mapping": props.preview_tone_mapping,
        "light_direction": list(props.preview_light_direction),
    }


def volume_preview_signature(preview):
    if not preview or not preview.get("valid", False):
        return None

    def rounded(values):
        return tuple(round(float(value), 7) for value in values)

    return (
        int(preview["width"]),
        int(preview["height"]),
        int(preview.get("view_id", 0)),
        rounded(preview["view"]),
        rounded(preview["projection"]),
        int(preview["max_ray_steps"]),
        round(float(preview["smoke_density"]), 7),
        rounded(preview["smoke_color"]),
        bool(preview["flame_enabled"]),
        round(float(preview["flame_brightness"]), 7),
        round(float(preview["flame_temperature_min"]), 7),
        round(float(preview["flame_temperature_max"]), 7),
        round(float(preview["flame_rate_scale"]), 7),
        round(float(preview["temperature_multiplier"]), 7),
        bool(preview["shadows"]),
        round(float(preview.get("shadow_min_light", 0.0)), 7),
        round(float(preview.get("exposure", 0.0)), 7),
        preview.get("tone_mapping", "none"),
        rounded(preview.get("light_direction", (1.0, 1.0, 1.0))),
    )


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
    vertex_buffer = gpu.types.GPUVertBuf(format=_point_vertex_format(), len=count)
    vertex_buffer.attr_fill("pos", positions)
    batch = gpu.types.GPUBatch(type="POINTS", buf=vertex_buffer)
    _PREVIEWS[domain.name] = {
        "type": "points",
        "domain": domain,
        "domain_name": domain.name,
        "point_size": max(0.001, float(preview.get("point_size", 0.1))),
        "buffer": vertex_buffer,
        "batch": batch,
    }
    _sync_draw_handlers()
    _tag_viewports()
    return (time.perf_counter() - started) * 1000.0


def update_volume_image(domain, data, payload):
    started = time.perf_counter()
    preview = data.get("preview") or {}
    width = int(preview.get("width", 0))
    height = int(preview.get("height", 0))
    expected_bytes = width * height * 4
    if preview.get("type") != "volume_rgba8":
        clear_preview(domain)
        return 0.0
    if width <= 0 or height <= 0 or len(payload) != expected_bytes:
        raise RuntimeError(
            f"Invalid volume preview payload: width={width}, height={height}, "
            f"bytes={len(payload)}"
        )

    _PREVIEWS[domain.name] = {
        "type": "volume",
        "domain": domain,
        "domain_name": domain.name,
        "view_id": int(preview.get("view_id", 0)),
        "width": width,
        "height": height,
        "pixels": bytes(payload),
        "texture": None,
    }
    _sync_draw_handlers()
    _tag_viewports()
    return (time.perf_counter() - started) * 1000.0


def show_preview_payload(domain, data, payload):
    preview_type = (data.get("preview") or {}).get("type")
    if preview_type == "volume_rgba8":
        return update_volume_image(domain, data, payload)
    if preview_type == "density_points":
        return update_density_points(domain, data, payload)
    clear_preview(domain)
    return 0.0


def clear_preview(domain):
    # The job's original name remains usable after the object is deleted/renamed.
    name = domain if isinstance(domain, str) else domain.name
    _PREVIEWS.pop(name, None)
    if not isinstance(domain, str):
        for key, preview in tuple(_PREVIEWS.items()):
            if preview.get("domain") == domain:
                _PREVIEWS.pop(key, None)
    _remove_legacy_preview_objects(name)
    _sync_draw_handlers()
    _tag_viewports()


def clear_all_previews():
    _PREVIEWS.clear()
    _sync_draw_handlers()
    objects = getattr(bpy.data, "objects", None)
    if objects is not None:
        for obj in list(objects):
            if obj.get(PREVIEW_MARKER) or obj.get(LEGACY_PREVIEW_MARKER):
                _remove_object(obj)
    _tag_viewports()


def refresh_preview_display():
    _tag_viewports()


def _domain_visible(domain, view_layer, viewport=None):
    if domain is None:
        return False
    if viewport is not None and viewport.type != "VIEW_3D":
        viewport = None
    try:
        return (
            domain.fumaris.smoke_object_type == "domain"
            and domain.visible_get(view_layer=view_layer, viewport=viewport)
        )
    except (ReferenceError, RuntimeError):
        return False


def _visible_preview_domain(preview):
    # Keep object identity: a replacement object with the same name is not its owner.
    domain = preview.get("domain")
    context = bpy.context
    if _domain_visible(domain, context.view_layer, context.space_data):
        return domain
    return None


def _draw_point_previews():
    point_previews = tuple(
        preview for preview in _PREVIEWS.values()
        if preview["type"] == "points"
    )
    if not point_previews:
        return
    region = bpy.context.region
    if region is None:
        return
    shader = _point_shader()
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
        for preview in point_previews:
            domain = _visible_preview_domain(preview)
            if domain is None:
                continue
            shader.uniform_float("worldSize", _world_point_size(domain, preview))
            shader.uniform_float("color", _point_color(domain))
            preview["batch"].draw(shader)
    finally:
        if uses_program_point_size:
            gpu.state.program_point_size_set(False)
        gpu.state.blend_set("NONE")


def _draw_volume_previews():
    region = bpy.context.region
    if region is None:
        return
    view_id = int(region.as_pointer())
    previews = tuple(
        preview for preview in _PREVIEWS.values()
        if (
            preview["type"] == "volume"
            and preview["view_id"] == view_id
            and _visible_preview_domain(preview) is not None
        )
    )
    if not previews:
        return

    shader = _image_shader()
    batch = _image_batch()
    gpu.state.blend_set("ALPHA_PREMULT")
    gpu.state.depth_test_set("NONE")
    try:
        shader.bind()
        for preview in previews:
            texture = _volume_texture(preview)
            if texture is None:
                continue
            shader.uniform_sampler("image", texture)
            batch.draw(shader)
    finally:
        gpu.state.blend_set("NONE")


def _volume_texture(preview):
    global _IMAGE_UPLOAD_BUFFER, _IMAGE_UPLOAD_SHAPE
    texture = preview.get("texture")
    if texture is not None:
        return texture
    pixels = preview.get("pixels")
    if pixels is None or preview.get("upload_failed"):
        return None
    started = time.perf_counter()
    try:
        if np is not None:
            shape = (preview["height"], preview["width"], 4)
            if _IMAGE_UPLOAD_BUFFER is None or _IMAGE_UPLOAD_SHAPE != shape:
                _IMAGE_UPLOAD_BUFFER = gpu.types.Buffer("FLOAT", shape)
                _IMAGE_UPLOAD_SHAPE = shape
            buffer = _IMAGE_UPLOAD_BUFFER
            # GPUTexture consumes the staging data before returning. Keep one
            # CPU buffer, not a second float image allocation for every draw.
            np.multiply(
                np.frombuffer(pixels, dtype=np.uint8), np.float32(1.0 / 255.0),
                out=np.frombuffer(buffer, dtype=np.float32),
            )
        else:
            normalized = array("f", (value / 255.0 for value in pixels))
            buffer = gpu.types.Buffer(
                "FLOAT", (preview["height"], preview["width"], 4), normalized,
            )
        texture = gpu.types.GPUTexture(
            (preview["width"], preview["height"]),
            format="RGBA8",
            data=buffer,
        )
    except Exception as error:
        preview["upload_failed"] = True
        preview["upload_error"] = str(error)
        diagnostics.add_log(preview.get("domain"), f"Error: preview upload failed: {error}")
        print(f"Fumaris volume preview upload failed: {error}")
        return None
    preview["texture"] = texture
    preview["upload_ms"] = (time.perf_counter() - started) * 1000.0
    diagnostics.record(preview.get("domain"), {
        "blender_texture_upload_ms": preview["upload_ms"],
        "blender_preview_width": preview["width"],
        "blender_preview_height": preview["height"],
    })
    preview.pop("pixels", None)
    return texture


def _sync_draw_handlers():
    global _POINT_DRAW_HANDLE, _IMAGE_DRAW_HANDLE, _IMAGE_UPLOAD_BUFFER, _IMAGE_UPLOAD_SHAPE
    needs_points = any(
        preview["type"] == "points" for preview in _PREVIEWS.values()
    )
    needs_images = any(
        preview["type"] == "volume" for preview in _PREVIEWS.values()
    )
    if not needs_images:
        _IMAGE_UPLOAD_BUFFER = None
        _IMAGE_UPLOAD_SHAPE = None
    if needs_points and _POINT_DRAW_HANDLE is None:
        _POINT_DRAW_HANDLE = bpy.types.SpaceView3D.draw_handler_add(
            _draw_point_previews,
            (),
            "WINDOW",
            "POST_VIEW",
        )
    elif not needs_points and _POINT_DRAW_HANDLE is not None:
        _remove_draw_handler(_POINT_DRAW_HANDLE)
        _POINT_DRAW_HANDLE = None
    if needs_images and _IMAGE_DRAW_HANDLE is None:
        _IMAGE_DRAW_HANDLE = bpy.types.SpaceView3D.draw_handler_add(
            _draw_volume_previews,
            (),
            "WINDOW",
            "POST_PIXEL",
        )
    elif not needs_images and _IMAGE_DRAW_HANDLE is not None:
        _remove_draw_handler(_IMAGE_DRAW_HANDLE)
        _IMAGE_DRAW_HANDLE = None


def _remove_draw_handler(handle):
    try:
        bpy.types.SpaceView3D.draw_handler_remove(handle, "WINDOW")
    except (ReferenceError, ValueError):
        pass


def _point_shader():
    global _POINT_SHADER
    if _POINT_SHADER is None:
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
        _POINT_SHADER = gpu.shader.create_from_info(info)
    return _POINT_SHADER


def _image_shader():
    global _IMAGE_SHADER
    if _IMAGE_SHADER is None:
        interface = gpu.types.GPUStageInterfaceInfo("fumaris_preview_image")
        interface.smooth("VEC2", "texCoord")
        info = gpu.types.GPUShaderCreateInfo()
        info.sampler(0, "FLOAT_2D", "image")
        info.vertex_in(0, "VEC2", "pos")
        info.vertex_in(1, "VEC2", "uv")
        info.vertex_out(interface)
        info.fragment_out(0, "VEC4", "FragColor")
        info.vertex_source("""
            void main()
            {
                texCoord = uv;
                gl_Position = vec4(pos, 0.0, 1.0);
            }
        """)
        info.fragment_source("""
            void main()
            {
                FragColor = texture(image, vec2(texCoord.x, 1.0 - texCoord.y));
            }
        """)
        _IMAGE_SHADER = gpu.shader.create_from_info(info)
    return _IMAGE_SHADER


def _image_batch():
    global _IMAGE_BATCH
    if _IMAGE_BATCH is None:
        vertex_format = gpu.types.GPUVertFormat()
        vertex_format.attr_add(id="pos", comp_type="F32", len=2, fetch_mode="FLOAT")
        vertex_format.attr_add(id="uv", comp_type="F32", len=2, fetch_mode="FLOAT")
        vertices = gpu.types.GPUVertBuf(format=vertex_format, len=6)
        vertices.attr_fill(
            "pos",
            ((-1.0, -1.0), (1.0, -1.0), (1.0, 1.0),
             (-1.0, -1.0), (1.0, 1.0), (-1.0, 1.0)),
        )
        vertices.attr_fill(
            "uv",
            ((0.0, 0.0), (1.0, 0.0), (1.0, 1.0),
             (0.0, 0.0), (1.0, 1.0), (0.0, 1.0)),
        )
        _IMAGE_BATCH = gpu.types.GPUBatch(type="TRIS", buf=vertices)
    return _IMAGE_BATCH


def _point_vertex_format():
    global _POINT_VERTEX_FORMAT
    if _POINT_VERTEX_FORMAT is None:
        _POINT_VERTEX_FORMAT = gpu.types.GPUVertFormat()
        _POINT_VERTEX_FORMAT.attr_add(
            id="pos",
            comp_type="F32",
            len=3,
            fetch_mode="FLOAT",
        )
    return _POINT_VERTEX_FORMAT


def _world_point_size(domain, preview):
    scale = max(0.05, float(getattr(domain.fumaris, "preview_dot_size", 1.0)))
    return preview["point_size"] * scale * 0.1


def _point_color(domain):
    props = domain.fumaris
    color = tuple(float(value) for value in getattr(props, "preview_color", (0.35, 0.65, 1.0)))
    opacity = max(0.0, min(1.0, float(getattr(props, "preview_opacity", 0.65))))
    return (*color[:3], opacity)


def _largest_viewport(context, domain=None):
    candidates = []
    window_manager = getattr(context, "window_manager", None)
    if window_manager is None:
        return None
    for window in window_manager.windows:
        for area in window.screen.areas:
            if area.type != "VIEW_3D":
                continue
            if domain is not None and not _domain_visible(
                domain, window.view_layer, area.spaces.active
            ):
                continue
            region = next(
                (item for item in area.regions if item.type == "WINDOW"),
                None,
            )
            region_3d = getattr(area.spaces.active, "region_3d", None)
            if region is not None and region_3d is not None:
                candidates.append((region.width * region.height, region, region_3d))
    if not candidates:
        return None
    _, region, region_3d = max(candidates, key=lambda item: item[0])
    return region, region_3d


def _zero_to_one_projection(projection):
    depth_conversion = Matrix((
        (1.0, 0.0, 0.0, 0.0),
        (0.0, 1.0, 0.0, 0.0),
        (0.0, 0.0, 0.5, 0.5),
        (0.0, 0.0, 0.0, 1.0),
    ))
    return depth_conversion @ projection


def _flatten_matrix(matrix):
    return [float(value) for row in matrix for value in row]


def _remove_legacy_preview_objects(domain_name):
    objects = getattr(bpy.data, "objects", None)
    if objects is None:
        return
    for obj in list(objects):
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
