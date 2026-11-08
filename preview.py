import time
from array import array

import bpy
import gpu
from mathutils import Matrix
from . import diagnostics, display_lut, scene_lighting
from .protocol import DISPLAY_LUT_KEY, SCENE_DEPTH_KEY
from .utils import WIEN_KELVIN, flame_temperature_range, flame_value, is_pyro_look

try:
    import numpy as np
except ImportError:
    np = None

PREVIEW_MARKER = "fumaris_preview"
_PREVIEWS = {}
_IMAGE_DRAW_HANDLE = None
_IMAGE_SHADER = None
_IMAGE_BATCH = None
_IMAGE_UPLOAD_BUFFER = None
_IMAGE_UPLOAD_SHAPE = None
# Latest viewport depth per region, resampled to that region's preview size.
_SCENE_DEPTHS = {}
_SCENE_DEPTH_SERIAL = 0
_SCENE_DEPTH_QUIET_UNTIL = 0.0
_SCENE_DEPTH_FAILED = False
# A draw skipped its depth readback during the quiet period; take it once after.
_SCENE_DEPTH_DEFERRED = False


def capture_volume_preview(context, domain, target=None):
    props = domain.fumaris
    if target is None:
        target = _largest_viewport(context, domain)
    if target is None:
        # Visibility controls drawing, not simulation. Keep a valid camera when
        # hidden/excluded/in local view rather than falling back to point readback.
        target = _largest_viewport(context)
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
    # The active Look's flame controls (Temperature and Pyro keep separate values).
    pyro_look = is_pyro_look(props)
    glow_temperature = max(1.0, float(flame_value(props, "glow_temperature", 1600.0)))
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
        "step_cells": float(props.preview_step_size),
        "smoke_density": float(props.shader_smoke_density),
        "smoke_color": [float(value) for value in props.shader_smoke_color],
        "flame_enabled": bool(props.shader_flame_enabled),
        "flame_brightness": float(flame_value(props, "brightness", 1.0)),
        "flame_temperature_min": flame_start,
        "flame_temperature_max": flame_full,
        "flame_rate_scale": flame_rate_scale,
        "flame_sharpness": float(props.shader_flame_sharpness),
        # The fire look, as the generated material (importers.py)
        "glow_temperature": glow_temperature,
        "glow_contrast": WIEN_KELVIN * max(0.05, float(flame_value(props, "contrast", 0.5))) / glow_temperature,
        "temperature_multiplier": float(flame_value(props, "temperature_multiplier", 1.0)),
        "color_spread": 1.0 if pyro_look else float(props.shader_color_spread),
        "pyro_look": pyro_look,
        "thin_gas": float(getattr(props, "shader_thin_gas", 1.0)),
        "flame_tint": [float(value) for value in flame_value(props, "flame_tint", (1.0, 1.0, 1.0))],
        "gas_expansion": float(props.gas_expansion),
        "scatter_glow": float(flame_value(props, "scatter_glow", 0.0)),
        "glow_radius": max(0.0, float(props.glow_radius)),
        "glow_spread": float(flame_value(props, "glow_spread", 0.0)),
        "glow_shadowing": float(flame_value(props, "glow_shadowing", 0.0)),
        "shadows": bool(props.preview_shadows),
        "shadow_min_light": float(props.preview_shadow_min_light),
        "light_scattering": float(props.preview_light_scattering),
        "flame_light": float(props.preview_fire_lighting),
        "exposure": max(-10.0, min(10.0, _scene_exposure(context) + float(props.preview_exposure_offset))),
        "tone_mapping": _preview_tone_mapping(context, props),
        "field_view": getattr(props, "preview_field_view", "look"),
        # sun, world, anisotropy, volume bounces and light_direction
        **scene_lighting.preview_lighting(context, props, domain),
        **_display_lut_request(context, props),
        **_scene_depth_request(props, int(region.as_pointer()), width, height),
    }


_TONE_MAPPING_VIEWS = {"agx": "AgX", "filmic": "Filmic", "none": "Standard"}


def _display_lut_request(context, props):
    """Blender's own view transform as a table (display_lut.py): the scene's for
    Scene, else the chosen view, so every mode matches Blender exactly."""
    override = _TONE_MAPPING_VIEWS.get(props.preview_tone_mapping)
    key, table = display_lut.scene_display_lut(context.scene, override)
    if key is None:
        return {}
    return {"display_lut_key": key, "display_lut_size": display_lut.SIZE, DISPLAY_LUT_KEY: table}


def _scene_exposure(context):
    view = getattr(context.scene, "view_settings", None)
    return float(getattr(view, "exposure", 0.0)) if view is not None else 0.0


def _preview_tone_mapping(context, props):
    mode = props.preview_tone_mapping
    if mode == "none":
        return "standard"
    if mode != "scene":
        return mode
    transform = getattr(getattr(context.scene, "view_settings", None), "view_transform", "AgX")
    if transform == "Raw":
        return "none"
    if transform == "Standard":
        return "standard"
    if transform.startswith("Filmic"):
        return "filmic"
    return "agx"


def _scene_depth_request(props, view_id, width, height):
    global _SCENE_DEPTH_SERIAL, _SCENE_DEPTH_DEFERRED
    if not getattr(props, "preview_occlusion", False):
        return {}
    if _SCENE_DEPTH_DEFERRED and time.monotonic() >= _SCENE_DEPTH_QUIET_UNTIL:
        # Draws skipped the readback while images streamed in: one redraw
        # now captures the final depth (published by a later request).
        _SCENE_DEPTH_DEFERRED = False
        _tag_viewports()
    depth = _SCENE_DEPTHS.get(view_id)
    if depth is None or depth["size"] != (width, height):
        # The draw handler has not seen this size yet; it triggers a rerender.
        return {}
    # Preview requests are polled even while paused and without another draw.
    # Publish a throttled change once the quiet period ends.
    if depth.get("pending", False) and time.monotonic() >= _SCENE_DEPTH_QUIET_UNTIL:
        _SCENE_DEPTH_SERIAL += 1
        depth["serial"] = _SCENE_DEPTH_SERIAL
        depth["pending"] = False
    return {"depth_serial": depth["serial"], SCENE_DEPTH_KEY: depth["data"]}


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
        round(float(preview.get("step_cells", 1.0)), 4),
        round(float(preview["smoke_density"]), 7),
        rounded(preview["smoke_color"]),
        bool(preview["flame_enabled"]),
        round(float(preview["flame_brightness"]), 7),
        round(float(preview["flame_temperature_min"]), 7),
        round(float(preview["flame_temperature_max"]), 7),
        round(float(preview["flame_rate_scale"]), 7),
        round(float(preview.get("flame_sharpness", 1.0)), 7),
        round(float(preview.get("glow_temperature", 1600.0)), 7),
        round(float(preview.get("glow_contrast", 0.0)), 7),
        round(float(preview["temperature_multiplier"]), 7),
        bool(preview["shadows"]),
        round(float(preview.get("shadow_min_light", 0.0)), 7),
        round(float(preview.get("light_scattering", 0.5)), 7),
        round(float(preview.get("flame_light", 1.0)), 7),
        round(float(preview.get("exposure", 0.0)), 7),
        preview.get("tone_mapping", "none"),
        preview.get("field_view", "look"),
        tuple(round(float(value), 7) for value in preview.get("flame_tint", (1.0, 1.0, 1.0))),
        rounded(preview.get("light_direction", (1.0, 1.0, 1.0))),
        bool(preview.get("physical_lighting", False)),
        preview.get("display_lut_key"),
        round(float(preview.get("scatter_glow", 0.0)), 7),
        round(float(preview.get("glow_radius", 0.0)), 7),
        round(float(preview.get("glow_spread", 0.0)), 7),
        round(float(preview.get("glow_shadowing", 0.0)), 7),
        rounded(preview.get("sun_radiance", (0.0, 0.0, 0.0))),
        rounded(preview.get("ambient_radiance", (0.0, 0.0, 0.0))),
        round(float(preview.get("phase_g", 0.0)), 7),
        int(preview.get("volume_bounces", 0)),
        round(float(preview.get("color_spread", 1.0)), 7),
        bool(preview.get("pyro_look", False)),
        round(float(preview.get("thin_gas", 1.0)), 7),
        # the Expansion view is scaled by Gas Expansion
        round(float(preview.get("gas_expansion", 0.0)), 7),
        preview.get("depth_serial"),
    )


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

    view_id = int(preview.get("view_id", 0))
    if not getattr(domain.fumaris, "preview_all_viewports", False):
        # One viewport at a time: drop the image of a viewport it left.
        for key in tuple(_PREVIEWS):
            if key[0] == domain.name and key[1] != view_id:
                _PREVIEWS.pop(key, None)
    _PREVIEWS[(domain.name, view_id)] = {
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
    _quiet_scene_depth()
    _tag_viewports()
    return (time.perf_counter() - started) * 1000.0


def show_preview_payload(domain, data, payload):
    preview_type = (data.get("preview") or {}).get("type")
    if preview_type == "volume_rgba8":
        return update_volume_image(domain, data, payload)
    # Bakes without a preview complete every frame here; only clear a live image.
    if _has_preview(domain):
        clear_preview(domain)
    return 0.0


def _has_preview(domain):
    name = domain if isinstance(domain, str) else domain.name
    return any(
        key[0] == name or preview.get("domain") == domain
        for key, preview in _PREVIEWS.items()
    )


def preview_image_pending(domain):
    """True until the latest images have reached the viewport draw callback."""
    if not domain:
        return False
    return any(
        key[0] == domain.name and preview.get("pixels") is not None
        and not preview.get("upload_failed")
        for key, preview in _PREVIEWS.items()
    )


def clear_preview(domain):
    # The job's original name remains usable after the object is deleted/renamed.
    name = domain if isinstance(domain, str) else domain.name
    for key, preview in tuple(_PREVIEWS.items()):
        if key[0] == name or (not isinstance(domain, str) and preview.get("domain") == domain):
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
            if obj.get(PREVIEW_MARKER):
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


def _draw_volume_previews():
    global _SCENE_DEPTH_DEFERRED
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

    for preview in previews:
        settings = getattr(preview.get("domain"), "fumaris", None)
        if getattr(settings, "preview_occlusion", False) and preview.get("width"):
            size = (preview["width"], preview["height"])
            known = _SCENE_DEPTHS.get(view_id)
            if known is not None and known["size"] == size and time.monotonic() < _SCENE_DEPTH_QUIET_UNTIL:
                # Images are streaming in (orbiting or playing): a changed depth
                # would only wait for the quiet period, so skip the readback
                # (1-5 ms and a GPU sync per draw) until it ends.
                _SCENE_DEPTH_DEFERRED = True
            else:
                _capture_scene_depth(view_id, *size)
            break

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


def _quiet_scene_depth():
    """Redraws caused by a new preview image must not request another one."""
    global _SCENE_DEPTH_QUIET_UNTIL
    _SCENE_DEPTH_QUIET_UNTIL = time.monotonic() + 0.25


def _capture_scene_depth(view_id, width, height):
    """Store the viewport's depth so the bridge can hide smoke behind objects."""
    global _SCENE_DEPTH_SERIAL, _SCENE_DEPTH_FAILED
    if np is None or _SCENE_DEPTH_FAILED:
        return
    try:
        x, y, region_width, region_height = gpu.state.viewport_get()
        buffer = gpu.state.active_framebuffer_get().read_depth(
            x, y, region_width, region_height)
        depth = np.frombuffer(buffer, dtype=np.float32).reshape(
            (region_height, region_width))
    except Exception as error:
        # Unsupported on this backend: keep the unoccluded preview working.
        _SCENE_DEPTH_FAILED = True
        print(f"Fumaris scene occlusion is unavailable: {error}")
        return
    rows = ((np.arange(height) + 0.5) * (region_height / height)).astype(np.intp)
    columns = ((np.arange(width) + 0.5) * (region_width / width)).astype(np.intp)
    # Blender's rows run bottom to top; the preview image is top row first.
    sampled = np.ascontiguousarray(depth[::-1][rows[:, None], columns[None, :]])
    if sampled.max() < 0.5:
        # Standard depth keeps even nearby geometry close to 1. A buffer that
        # never gets there is reversed (far = 0); the bridge expects far = 1.
        sampled = 1.0 - sampled
    previous = _SCENE_DEPTHS.get(view_id)
    same_size = previous is not None and previous["size"] == (width, height)
    if same_size and np.array_equal(previous["data"], sampled):
        return
    serial = previous["serial"] if previous else 0
    pending = same_size and time.monotonic() < _SCENE_DEPTH_QUIET_UNTIL
    if not pending:
        _SCENE_DEPTH_SERIAL += 1
        serial = _SCENE_DEPTH_SERIAL
    _SCENE_DEPTHS[view_id] = {
        "size": (width, height), "data": sampled, "serial": serial, "pending": pending,
    }


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
        # RGBA32F takes the float staging data as is; RGBA8 made Blender
        # convert it on the CPU (960x540: 2.7 ms vs 0.5 ms). Values are the
        # same k / 255, so the image is identical.
        texture = gpu.types.GPUTexture(
            (preview["width"], preview["height"]),
            format="RGBA32F",
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
    global _IMAGE_DRAW_HANDLE, _IMAGE_UPLOAD_BUFFER, _IMAGE_UPLOAD_SHAPE
    needs_images = any(
        preview["type"] == "volume" for preview in _PREVIEWS.values()
    )
    if not needs_images:
        _IMAGE_UPLOAD_BUFFER = None
        _IMAGE_UPLOAD_SHAPE = None
        _SCENE_DEPTHS.clear()
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


# Viewport the user chose with "Show Fumaris Preview Here", per domain name:
# the WINDOW region pointer (valid for this Blender session only).
_PINNED_VIEWS = {}


def pin_preview_view(domain, region):
    _PINNED_VIEWS[domain.name] = int(region.as_pointer())
    _tag_viewports()


def pinned_view(domain):
    return _PINNED_VIEWS.get(domain.name) if domain is not None else None


def preview_targets(context, domain=None):
    """3D viewports that can show the preview, preferred first.

    Order: the viewport pinned with Show Fumaris Preview Here; then viewports
    in the window that runs the preview (a newly opened window no longer takes
    the preview away from it); then the largest. Viewports hiding the domain
    are skipped when a domain is given.
    """
    window_manager = getattr(context, "window_manager", None)
    if window_manager is None:
        return []
    own_window = getattr(context, "window", None)
    pinned = pinned_view(domain)
    candidates = []
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
            if region is None or region_3d is None:
                continue
            rank = (
                int(region.as_pointer()) == pinned,
                own_window is not None and window == own_window,
                region.width * region.height,
            )
            candidates.append((rank, region, region_3d))
    candidates.sort(key=lambda item: item[0], reverse=True)
    return [(region, region_3d) for _, region, region_3d in candidates]


def _largest_viewport(context, domain=None):
    """The preferred preview viewport (see preview_targets)."""
    targets = preview_targets(context, domain)
    return targets[0] if targets else None


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
        if obj.get(PREVIEW_MARKER) == domain_name:
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
