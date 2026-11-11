"""Scene lighting for the live preview, in Cycles units.

The preview lights the smoke like Cycles does with the scene's own lights and
world: one sun (the strongest light, shadowed), the world's mean radiance with
sky visibility, the material's anisotropy and Cycles' volume bounces. A sun of
strength S gives irradiance S, so thin smoke scatters S * density * phase, as
Cycles renders it.
"""
import math

import bpy
from mathutils import Vector

_HDRI_CACHE = {}
_INV_FOUR_PI = 1.0 / (4.0 * math.pi)


def _luminance(rgb):
    return 0.2126 * rgb[0] + 0.7152 * rgb[1] + 0.0722 * rgb[2]


def _lamp_light(obj, center):
    """(irradiance rgb at center, unit direction toward the light) or None."""
    light = obj.data
    color = Vector(light.color)
    if light.type == "SUN":
        direction = (obj.matrix_world.to_3x3() @ Vector((0.0, 0.0, 1.0))).normalized()
        return color * float(light.energy), direction
    offset = obj.matrix_world.translation - center
    distance = max(offset.length, 1e-3)
    # Point and spot lights spread their power over the sphere, area lights over
    # the half facing them; distant enough to treat as one direction.
    spread = 2.0 * math.pi if light.type == "AREA" else 4.0 * math.pi
    return color * (float(light.energy) / (spread * distance * distance)), offset / distance


def _world_background(world):
    """(color rgb or None, strength, environment image node or None)."""
    if world is None:
        return None, 0.0, None
    if not world.use_nodes or world.node_tree is None:
        return tuple(world.color), 1.0, None
    output = next((node for node in world.node_tree.nodes
                   if node.type == "OUTPUT_WORLD" and node.is_active_output), None)
    if output is None or not output.inputs["Surface"].is_linked:
        return None, 0.0, None
    node = output.inputs["Surface"].links[0].from_node
    if node.type != "BACKGROUND":
        return tuple(world.color), 1.0, None
    strength_input = node.inputs["Strength"]
    strength = 1.0 if strength_input.is_linked else float(strength_input.default_value)
    color_input = node.inputs["Color"]
    if not color_input.is_linked:
        return tuple(color_input.default_value)[:3], strength, None
    source = color_input.links[0].from_node
    if source.type == "TEX_ENVIRONMENT" and source.image is not None:
        return None, strength, source
    # Sky textures and other node setups: the world's viewport colour.
    return tuple(world.color), strength, None


def _environment_rotation_z(node):
    vector = node.inputs.get("Vector")
    if vector is None or not vector.is_linked:
        return 0.0
    mapping = vector.links[0].from_node
    if mapping.type != "MAPPING":
        return 0.0
    rotation = mapping.inputs.get("Rotation")
    return float(rotation.default_value[2]) if rotation is not None and not rotation.is_linked else 0.0


def _hdri_stats(node):
    """(sun irradiance rgb, sun direction or None, mean radiance rgb of the rest)
    of an equirectangular environment at strength 1, cached per image."""
    image = node.image
    rotation = _environment_rotation_z(node)
    key = (image.name, image.filepath, tuple(image.size), round(rotation, 5))
    cached = _HDRI_CACHE.get(key)
    if cached is not None:
        return cached
    import numpy as np
    width, height = image.size
    if width < 2 or height < 2:
        return (0.0, 0.0, 0.0), None, (0.0, 0.0, 0.0)
    pixels = np.empty(width * height * 4, dtype=np.float32)
    image.pixels.foreach_get(pixels)
    pixels = pixels.reshape(height, width, 4)[..., :3]
    # Average down to at most 512 x 256 so a 4K image takes a moment, not seconds.
    step = max(1, width // 512)
    if step > 1:
        h2, w2 = height // step, width // step
        pixels = pixels[:h2 * step, :w2 * step].reshape(h2, step, w2, step, 3).mean(axis=(1, 3))
        height, width = h2, w2
    pixels = np.nan_to_num(np.maximum(pixels, 0.0), posinf=0.0)
    # Row 0 is the bottom (Blender stores images bottom-up): elevation -90..90.
    elevation = (np.arange(height) + 0.5) / height * math.pi - 0.5 * math.pi
    azimuth = (np.arange(width) + 0.5) / width * 2.0 * math.pi - math.pi
    solid = (2.0 * math.pi / width) * (math.pi / height) * np.cos(elevation)[:, None]
    luminance = pixels @ np.array([0.2126, 0.7152, 0.0722], dtype=np.float32)
    # The sun: the brightest pixels holding a concentrated share of the light.
    threshold = max(float(np.percentile(luminance, 99.9)), 20.0 * float(np.median(luminance)))
    mask = luminance >= threshold
    weights = solid * mask
    sun_rgb = (pixels * weights[..., None]).sum(axis=(0, 1))
    sun_direction = None
    # A sun only if those pixels hold a real share (5%) of the light.
    if float((luminance * weights).sum()) > 0.05 * float((luminance * solid).sum()):
        # Blender's equirectangular lookup: u = atan2(d.y, -d.x) / 2pi + 0.5.
        cos_e = np.cos(elevation)[:, None]
        dx = -np.cos(azimuth)[None, :] * cos_e
        dy = np.sin(azimuth)[None, :] * cos_e
        dz = np.sin(elevation)[:, None] * np.ones((1, width))
        w = luminance * weights
        d = Vector((float((dx * w).sum()), float((dy * w).sum()), float((dz * w).sum())))
        if d.length > 0.0:
            # The mapping rotates the lookup vector; undo it for world space.
            cos_r, sin_r = math.cos(-rotation), math.sin(-rotation)
            d = Vector((cos_r * d.x - sin_r * d.y, sin_r * d.x + cos_r * d.y, d.z)).normalized()
            sun_direction = d
    if sun_direction is None:
        sun_rgb = np.zeros(3)
        mask = np.zeros_like(mask)
    rest = (pixels * (solid * ~mask)[..., None]).sum(axis=(0, 1)) * _INV_FOUR_PI
    result = (tuple(float(v) for v in sun_rgb), sun_direction, tuple(float(v) for v in rest))
    _HDRI_CACHE[key] = result
    return result


def preview_lighting(context, props, domain):
    """Request fields for the bridge's physical preview lighting."""
    scene = context.scene
    center = domain.matrix_world.translation.copy()
    sun = Vector((0.0, 0.0, 0.0))
    direction = None
    ambient = Vector((0.0, 0.0, 0.0))

    custom_light = props.preview_lighting == "CUSTOM"
    # Custom replaces the scene's lighting (lights and world) with one light
    # and a uniform grey sky of its own (Sky), shadowed by the smoke.
    color, strength, environment = (
        ((1.0, 1.0, 1.0), float(getattr(props, "preview_custom_sky", 0.0)), None)
        if custom_light else _world_background(scene.world))
    hdri_sun = None
    if environment is not None:
        sun_rgb, sun_direction, rest = _hdri_stats(environment)
        ambient = Vector(rest) * strength
        if sun_direction is not None:
            hdri_sun = (Vector(sun_rgb) * strength, sun_direction)
    elif color is not None:
        ambient = Vector(color) * strength

    candidates = []
    if custom_light:
        custom = Vector(props.preview_light_direction)
        if custom.length > 0.0:
            candidates.append((Vector((1.0, 1.0, 1.0)) * float(props.preview_light_strength),
                               custom.normalized()))
    else:
        for obj in scene.objects:
            if obj.type != "LIGHT" or obj.hide_render:
                continue
            lamp = _lamp_light(obj, center)
            if lamp is not None and _luminance(lamp[0]) > 0.0:
                candidates.append(lamp)
        if hdri_sun is not None:
            candidates.append(hdri_sun)
    candidates.sort(key=lambda item: _luminance(item[0]), reverse=True)
    if candidates:
        sun, direction = candidates[0]
        # Only one light casts preview shadows; the others light the smoke
        # evenly (a directional irradiance E scatters like radiance E / 4pi).
        for other, _direction in candidates[1:]:
            ambient += other * _INV_FOUR_PI
    elif hdri_sun is not None:
        ambient += hdri_sun[0] * _INV_FOUR_PI
    if direction is None:
        direction = Vector(props.preview_light_direction)
        if direction.length == 0.0:
            direction = Vector((0.0, 0.0, 1.0))
        direction.normalize()

    cycles = getattr(scene, "cycles", None)
    bounces = 0
    if cycles is not None:
        bounces = min(int(getattr(cycles, "volume_bounces", 0)), int(getattr(cycles, "max_bounces", 0)))
    return {
        "physical_lighting": True,
        "sun_radiance": [max(0.0, float(v)) for v in sun],
        "ambient_radiance": [max(0.0, float(v)) for v in ambient],
        "light_direction": [float(v) for v in direction],
        "phase_g": max(-0.999999, min(0.999999, float(props.shader_anisotropy))),
        "volume_bounces": max(0, bounces),
    }
