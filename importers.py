import math
import os
import re
import shutil
import time

import bpy
try:
    import openvdb
except ImportError:
    openvdb = None

from .diagnostics import console_log
from .utils import WIEN_KELVIN, flame_temperature_range, flame_value, is_pyro_look

PREFIX = "fumaris_"
_FRAME_DIGITS = re.compile(r"-?[0-9]+")
IMPORT_DIRECTORY = ".fumaris_import"
VOLUME_MARKER = "fumaris_volume"
VOLUME_PREFIX_MARKER = "fumaris_volume_prefix"
GENERATED_MATERIAL_MARKER = "fumaris_generated_material"
PREVIEW_OWNER_MARKER = "fumaris_preview_owner"

NODE_VOLUME_SHADER = "Fumaris Volume Shader"
# Blue Core only (built when Blue Core > 0): fuel near ignition turns blue.
NODE_FIRE_TEMPERATURE = "Fumaris Fire Temperature"
NODE_FLAME_CORE = "Fumaris Flame Core"
# Temperature look: soot absorbs, and by Kirchhoff emits in proportion, with
# radiance from Wien's law at 550 nm, exp(-c * 26160 K * (1/T - 1/Tref)), so
# gas at Full Glow glows at Brightness and cooler gas falls off as it does on a
# camera exposed for the hottest gas. Colour is blackbody, shifted and
# saturated like a camera's clipped highlights (plain blackbody orange turns
# peach under AgX).
NODE_WIEN_TERM = "Fumaris Wien Term"
# 2: Temperature look only (the Burn look and its burn grid removed), one
# emission closure, frames. 3: Colour Spread (colour shift is multiply-add).
# 4: Density Remap (density read through a remap node, not the shader attribute).
# 5-6: Pyro look. 7: Pyro Blue Core (each look has its own flame values).
# 8: Pyro Colour (golden ramp or blackbody).
# Older graphs are rebuilt on the next update.
GRAPH_VERSION = 8
NODE_DENSITY_REMAP = "Fumaris Density Remap"
NODE_DENSITY_GAIN = "Fumaris Density Gain"
GRAPH_VERSION_KEY = "fumaris_graph_version"
NODE_PHYSICAL_CONTRAST = "Fumaris Physical Contrast"
NODE_PHYSICAL_GAIN = "Fumaris Physical Gain"
NODE_PHYSICAL_COLOR_SCALE = "Fumaris Physical Color Scale"
PHYSICAL_COLOR_SHIFT = 1.3
# Scatter glow: emission glow x density, where the bridge's glow grid is the
# blurred density x Wien radiance at GLOW_REFERENCE_K (src/vdb_frame_writer.cpp).
# Coloured by the gas at Full Glow, filtered by the smoke colour.
NODE_SCATTER_GLOW_GAIN = "Fumaris Scatter Glow Gain"
NODE_SCATTER_GLOW_COLOR = "Fumaris Scatter Glow Color"
NODE_SCATTER_GLOW_ALBEDO = "Fumaris Scatter Glow Albedo"
# Glow Spread: blend of the baked glow and glow_wide (4x the radius).
NODE_SCATTER_GLOW_SPREAD = "Fumaris Scatter Glow Spread"
# Glow Shadowing: glow x exp(-shadowing x glow_depth) (smoke between it and the fire).
NODE_SCATTER_GLOW_SHADOW = "Fumaris Scatter Glow Shadowing"
# Flame Tint: multiplies the flame colour (not the scattered glow).
NODE_FLAME_TINT = "Fumaris Flame Tint"
# Pyro look (Houdini Pyro-Bake style, 2026-11-08; reworked the same day so it
# holds at any scene scale): fire brightness follows the Temperature look's law
# (Brightness, Full Glow, Contrast) but reads the baked "heat" grid (temperature
# above ambient, blurred over 1-4 voxels) for soft glowing cells, takes its
# colour from a golden ramp over 0.45-1 x Full Glow, and emits like the
# Temperature look at Thin Gas density, up to twice as bright in thinner gas
# and less in denser smoke, which then shows dark soot over the fire. Emission tied to absorption keeps the result
# independent of scene size (a fixed per-metre gain saturated large scenes).
NODE_PYRO_MIX = "Fumaris Pyro Look"
NODE_PYRO_GAIN = "Fumaris Pyro Gain"
NODE_PYRO_CONTRAST = "Fumaris Pyro Contrast"
NODE_PYRO_THIN = "Fumaris Pyro Thin Gas"
NODE_PYRO_COLOR_RANGE = "Fumaris Pyro Color Range"
NODE_PYRO_TINT = "Fumaris Pyro Tint"
NODE_PYRO_ALBEDO = "Fumaris Pyro Soot"
NODE_PYRO_HOT = "Fumaris Pyro Hot Soot"
NODE_PYRO_CORE = "Fumaris Pyro Blue Core"
# Pyro Colour: golden ramp (0) or real blackbody colour (1), and the blackbody's
# colour temperature scale (camera shift x Blackbody Scale).
NODE_PYRO_COLOR = "Fumaris Pyro Colour"
NODE_PYRO_BLACKBODY_SCALE = "Fumaris Pyro Blackbody Scale"
PYRO_RAMP = ((0.0, (1.0, 0.1, 0.0)), (0.35, (1.0, 0.32, 0.02)), (0.65, (1.0, 0.58, 0.14)),
             (1.0, (1.0, 0.86, 0.52)))
PYRO_COLOR_RANGE = (0.45, 1.0)  # x Full Glow: deep red to pale gold
PYRO_HOT_SOOT = 0.06            # albedo of hot soot (dark); cool smoke keeps Smoke Color
GLOW_REFERENCE_K = 1500.0
SCATTER_GLOW_SCALE = 1.0
PHYSICAL_SATURATION = 1.8


def hide_generated_volumes(directory, prefix=PREFIX, *, hide_render=False):
    """Hide this cache's imported volume and return enough state to restore it."""
    normalized = os.path.normpath(directory)
    hidden = []
    for obj in bpy.data.objects:
        if not _is_generated_volume(obj, normalized, prefix):
            continue
        hidden.append(
            (obj.name, bool(obj.hide_viewport), bool(obj.hide_render))
            if hide_render else (obj.name, bool(obj.hide_viewport))
        )
        obj.hide_viewport = True
        if hide_render:
            obj.hide_render = True
        obj.update_tag()
    return hidden


def restore_generated_volumes(directory, prefix, states):
    normalized = os.path.normpath(directory)
    for state in states:
        name, was_hidden = state[:2]
        obj = bpy.data.objects.get(name)
        if obj is None or not _is_generated_volume(obj, normalized, prefix):
            continue
        obj.hide_viewport = was_hidden
        if len(state) > 2:
            obj.hide_render = state[2]
        obj.update_tag()


def hide_recorded_previews(owner, except_volume=None):
    """Only one recorded take for this domain should be displayed at a time."""
    states = []
    for obj in bpy.data.objects:
        if obj == except_volume or obj.get(PREVIEW_OWNER_MARKER) != owner:
            continue
        states.append((obj.name, bool(obj.hide_viewport), bool(obj.hide_render)))
        obj.hide_viewport = obj.hide_render = True
        obj.update_tag()
    return states


def replace_recorded_previews(owner, volume):
    """Replace prior display objects, retaining their recorded files on disk."""
    for obj in list(bpy.data.objects):
        if (obj != volume and obj.type == "VOLUME"
                and obj.get(PREVIEW_OWNER_MARKER) == owner
                and obj.get(VOLUME_MARKER)):
            _remove_generated_volume_object(obj)


def restore_recorded_previews(states):
    for name, viewport, render in states or ():
        obj = bpy.data.objects.get(name)
        if obj is not None:
            obj.hide_viewport = viewport
            obj.hide_render = render
            obj.update_tag()


def baked_frame_count(directory, prefix=PREFIX):
    """Frames a bake has written to this cache slot folder."""
    return len(_frame_files(directory, prefix))


def show_only_slot_volumes(root, current):
    """Show the current slot's baked volume and hide the other slots' volumes.

    Every slot of a domain lives below `root`. Their imported volumes stay in
    the scene so switching back is instant; without this they would overlap.
    """
    root = os.path.normpath(root) + os.sep
    current = os.path.normpath(current)
    for obj in bpy.data.objects:
        marker = obj.get(VOLUME_MARKER)
        if not marker:
            continue
        marker = os.path.normpath(marker)
        if not marker.startswith(root):
            continue
        if marker == current:
            hidden = False
        elif marker.startswith(current + os.sep):
            continue  # this slot's preview recordings keep their own state
        else:
            hidden = True
        if obj.hide_viewport != hidden or obj.hide_render != hidden:
            obj.hide_viewport = hidden
            obj.hide_render = hidden
            obj.update_tag()


def delete_generated_data(directory, prefix=PREFIX):
    normalized = os.path.normpath(directory)
    for obj in list(bpy.data.objects):
        if obj.get(VOLUME_MARKER) == normalized:
            _remove_generated_volume_object(obj)

    if os.path.isdir(directory):
        for path in _frame_files(directory, prefix):
            os.remove(path)
        delete_temporary_writes(directory, prefix)
        _delete_import_copies(directory)


def delete_temporary_writes(directory, prefix=PREFIX):
    for path in _frame_files(directory, prefix, ".vdb.tmp"):
        try:
            os.remove(path)
        except FileNotFoundError:
            pass


def _socket(sockets, *names):
    for name in names:
        socket = sockets.get(name)
        if socket is not None:
            return socket
    raise KeyError(f"Missing node socket: {', '.join(names)}")


def _appearance_value(settings, name, default):
    return getattr(settings, name, default) if settings is not None else default


def _wants_core(settings):
    """Blue Core (either look) needs the fuel grid and the core nodes."""
    return any(float(flame_value(settings, "flame_core", 0.0, pyro)) > 0.0 for pyro in (False, True))


def _graph_needs_rebuild(material, settings):
    """Older graphs (with the removed Burn look) and a Blue Core switched on or
    off since the graph was built are rebuilt in place."""
    nodes = material.node_tree.nodes
    if material.get(GRAPH_VERSION_KEY, 0) < GRAPH_VERSION:
        return True
    return _wants_core(settings) != (nodes.get(NODE_FLAME_CORE) is not None)


def apply_generated_material_settings(material, settings):
    if material is None or not material.use_nodes or material.node_tree is None:
        return False
    if _graph_needs_rebuild(material, settings):
        _build_generated_material(
            material, blue_core=_wants_core(settings))
    nodes = material.node_tree.nodes
    shader = nodes.get(NODE_VOLUME_SHADER)
    if shader is None:
        return False

    smoke_color = tuple(float(value) for value in _appearance_value(
        settings, "shader_smoke_color", (0.6, 0.6, 0.6)))
    density = max(0.0, float(_appearance_value(settings, "shader_smoke_density", 2.0)))
    shader.inputs["Color"].default_value = (*smoke_color[:3], 1.0)
    soot_mix = nodes.get(NODE_PYRO_ALBEDO)
    if soot_mix is not None:
        [socket for socket in soot_mix.inputs if socket.type == "RGBA"][0].default_value = (*smoke_color[:3], 1.0)
    shader.inputs["Density"].default_value = density
    gain_node = nodes.get(NODE_DENSITY_GAIN)
    if gain_node is not None:
        gain_node.inputs[1].default_value = density
    remap_node = nodes.get(NODE_DENSITY_REMAP)
    if remap_node is not None:
        # Materials made before Density Low / High were removed: pass through.
        remap_node.inputs[1].default_value = 1.0
        remap_node.inputs[2].default_value = 0.0
    shader.inputs["Anisotropy"].default_value = max(
        -0.95, min(0.95, float(_appearance_value(settings, "shader_anisotropy", 0.3))))

    enabled = bool(_appearance_value(settings, "shader_flame_enabled", True))
    # With Flame off the (hidden) Look must not change the smoke: no dark hot soot.
    pyro_look = is_pyro_look(settings) and enabled

    def look_values(pyro):
        """One look's flame controls (the looks keep separate values)."""
        return {
            "brightness": max(0.0, float(flame_value(settings, "brightness", 1.0, pyro))),
            "reference": max(1.0, float(flame_value(settings, "glow_temperature", 1600.0, pyro))),
            "contrast": max(0.05, float(flame_value(settings, "contrast", 0.5, pyro))),
            "multiplier": max(0.01, float(flame_value(settings, "temperature_multiplier", 1.0, pyro))),
            "core": max(0.0, min(1.0, float(flame_value(settings, "flame_core", 0.0, pyro)))),
            "tint": (*[max(0.0, float(value)) for value in tuple(
                flame_value(settings, "flame_tint", (1.0, 1.0, 1.0), pyro))[:3]], 1.0),
            "scatter": max(0.0, float(flame_value(settings, "scatter_glow", 1.0, pyro))),
            "glow_spread": max(0.0, min(1.0, float(flame_value(settings, "glow_spread", 0.0, pyro)))),
            "glow_shadowing": max(0.0, float(flame_value(settings, "glow_shadowing", 0.0, pyro))),
        }

    temperature_look = look_values(False)
    pyro_values = look_values(True)
    active = pyro_values if pyro_look else temperature_look
    spread = max(0.05, float(_appearance_value(settings, "shader_color_spread", 1.0)))

    # Temperature look. Flame: density x Brightness x Density x
    # exp(-c * 26160 * (1/T - 1/Tg)), with X = 26160 / T: exp(X * -c + c * 26160 / Tg).
    look = temperature_look
    wien = nodes[NODE_PHYSICAL_CONTRAST]
    wien.inputs[1].default_value = -look["contrast"]
    wien.inputs[2].default_value = look["contrast"] * WIEN_KELVIN / look["reference"]
    nodes[NODE_PHYSICAL_GAIN].inputs[1].default_value = (look["brightness"] * density) if enabled else 0.0
    # Colour temperature = shift x multiplier x (Full Glow + Colour Spread x (T - Full Glow)).
    color_scale = nodes[NODE_PHYSICAL_COLOR_SCALE]
    color_scale.inputs[1].default_value = PHYSICAL_COLOR_SHIFT * look["multiplier"] * spread
    color_scale.inputs[2].default_value = PHYSICAL_COLOR_SHIFT * look["multiplier"] * look["reference"] * (1.0 - spread)
    flame_tint = nodes.get(NODE_FLAME_TINT)
    if flame_tint is not None:
        [socket for socket in flame_tint.inputs if socket.type == "RGBA"][1].default_value = look["tint"]
    core = nodes.get(NODE_FLAME_CORE)
    if core is not None:
        core.inputs[1].default_value = look["core"]

    # Pyro look: switch, the same brightness law on the baked heat, colour
    # range (Blackbody Scale shifts it), tint, Blue Core and Thin Gas.
    if nodes.get(NODE_PYRO_MIX) is not None:
        look = pyro_values
        nodes[NODE_PYRO_MIX].inputs["Factor"].default_value = 1.0 if pyro_look else 0.0
        nodes[NODE_PYRO_HOT].inputs[1].default_value = 1.0 if pyro_look else 0.0
        pyro_contrast = nodes[NODE_PYRO_CONTRAST]
        pyro_contrast.inputs[1].default_value = -look["contrast"]
        pyro_contrast.inputs[2].default_value = look["contrast"] * WIEN_KELVIN / look["reference"]
        nodes[NODE_PYRO_GAIN].inputs[1].default_value = (look["brightness"] * density) if enabled else 0.0
        color_range = nodes[NODE_PYRO_COLOR_RANGE]
        color_range.inputs["From Min"].default_value = PYRO_COLOR_RANGE[0] * look["reference"] / look["multiplier"]
        color_range.inputs["From Max"].default_value = PYRO_COLOR_RANGE[1] * look["reference"] / look["multiplier"]
        [socket for socket in nodes[NODE_PYRO_TINT].inputs if socket.type == "RGBA"][1].default_value = look["tint"]
        thin_gas = max(1e-4, float(_appearance_value(settings, "shader_thin_gas", 1.0)))
        nodes[NODE_PYRO_THIN].inputs[1].default_value = 1.0 / thin_gas
        pyro_core = nodes.get(NODE_PYRO_CORE)
        if pyro_core is not None:
            pyro_core.inputs[1].default_value = look["core"]
        blackbody = str(_appearance_value(settings, "pyro_color", "GOLDEN")) == "BLACKBODY"
        nodes[NODE_PYRO_COLOR].inputs["Factor"].default_value = 1.0 if blackbody else 0.0
        nodes[NODE_PYRO_BLACKBODY_SCALE].inputs[1].default_value = PHYSICAL_COLOR_SHIFT * look["multiplier"]

    # Scatter Glow (shared nodes, the active look's values), on the flame's
    # radiance scale at Contrast 1.
    look = active
    to_full_glow = math.exp(WIEN_KELVIN * (1.0 / look["reference"] - 1.0 / GLOW_REFERENCE_K))
    nodes[NODE_SCATTER_GLOW_GAIN].inputs[1].default_value = (
        SCATTER_GLOW_SCALE * look["scatter"] * look["brightness"] * density * to_full_glow) if enabled else 0.0
    nodes[NODE_SCATTER_GLOW_COLOR].inputs["Temperature"].default_value = (
        look["reference"] * PHYSICAL_COLOR_SHIFT * look["multiplier"])
    glow_spread = nodes.get(NODE_SCATTER_GLOW_SPREAD)
    if glow_spread is not None:
        glow_spread.inputs["Factor"].default_value = look["glow_spread"]
    glow_shadow = nodes.get(NODE_SCATTER_GLOW_SHADOW)
    if glow_shadow is not None:
        glow_shadow.inputs[1].default_value = -look["glow_shadowing"]
    albedo = nodes[NODE_SCATTER_GLOW_ALBEDO]
    [socket for socket in albedo.inputs if socket.type == "RGBA"][1].default_value = (*smoke_color[:3], 1.0)

    if core is not None:
        start_temperature, full_temperature = flame_temperature_range(
            float(_appearance_value(settings, "flame_temperature_min", 800.0)),
            float(_appearance_value(settings, "flame_temperature_max", 3000.0)))
        fire_temperature = nodes[NODE_FIRE_TEMPERATURE]
        fire_temperature.inputs[1].default_value = start_temperature
        fire_temperature.inputs[2].default_value = full_temperature
    material.diffuse_color = (*smoke_color[:3], 1.0)
    material.node_tree.update_tag()
    return True


def add_density_material(volume_object, settings=None):
    material = bpy.data.materials.new("Fumaris Material")
    material.use_nodes = True
    material[GENERATED_MATERIAL_MARKER] = True
    _build_generated_material(material, blue_core=_wants_core(settings))
    apply_generated_material_settings(material, settings)
    volume_object.data.materials.append(material)


def _build_generated_material(material, blue_core=False):
    """Temperature-look volume material, kept short for the viewport.

    Reads density, temperature and the coarse glow grid (fuel only with Blue
    Core). One shared X = 26160 K / T feeds both Wien terms, and fire and
    scatter glow are summed into the Principled Volume's single emission, so
    Eevee evaluates one closure. Appearance settings live in the named nodes
    that apply_generated_material_settings writes.
    """
    nodes = material.node_tree.nodes
    links = material.node_tree.links
    nodes.clear()

    def frame(label, location, color):
        node = nodes.new("NodeFrame")
        node.label = label
        node.location = location
        node.use_custom_color = True
        node.color = color
        node.label_size = 18
        return node

    def math_node(operation, location, label, parent, name=None, clamp=False):
        node = nodes.new("ShaderNodeMath")
        node.operation = operation
        node.label = label
        node.location = location
        node.parent = parent
        node.use_clamp = clamp
        if name:
            node.name = name
        return node

    inputs = frame("Grids", (-1500, 200), (0.20, 0.24, 0.30))
    flame = frame("Fire (Temperature look)", (-1100, 400), (0.36, 0.22, 0.12))
    glow_frame = frame("Scatter Glow", (-1100, -350), (0.32, 0.26, 0.10))
    combine = frame("Emission", (-120, 200), (0.24, 0.20, 0.28))
    smoke = frame("Smoke", (300, 200), (0.22, 0.26, 0.22))

    volume_info = nodes.new("ShaderNodeVolumeInfo")
    volume_info.location = (-1480, 160)
    volume_info.parent = inputs
    # Density, floored at 0 (no remap: Density Low / High were removed).
    density_floor = nodes.new("ShaderNodeMath")
    density_floor.operation = "MAXIMUM"
    density_floor.label = "Density Floor"
    density_floor.location = (-1150, 60)
    density_floor.parent = inputs
    density_floor.inputs[1].default_value = 0.0
    links.new(volume_info.outputs["Density"], density_floor.inputs[0])
    density_value = density_floor.outputs[0]
    glow = nodes.new("ShaderNodeAttribute")
    glow.attribute_name = "glow"
    glow.label = "Glow (baked)"
    glow.location = (-1480, -380)
    glow.parent = inputs
    glow_wide = nodes.new("ShaderNodeAttribute")
    glow_wide.attribute_name = "glow_wide"
    glow_wide.label = "Glow, wide (baked)"
    glow_wide.location = (-1480, -560)
    glow_wide.parent = inputs
    glow_depth = nodes.new("ShaderNodeAttribute")
    glow_depth.attribute_name = "glow_depth"
    glow_depth.label = "Smoke to the fire (baked)"
    glow_depth.location = (-1480, -740)
    glow_depth.parent = inputs

    # Fire: shared Wien term, radiance, strength and colour.
    floor = math_node("MAXIMUM", (-1100, 380), "Temperature Floor", flame)
    floor.inputs[1].default_value = 300.0
    wien_term = math_node("DIVIDE", (-920, 380), "26160 K / T", flame, NODE_WIEN_TERM)
    wien_term.inputs[0].default_value = WIEN_KELVIN
    wien = math_node("MULTIPLY_ADD", (-740, 380), "Contrast / Full Glow", flame, NODE_PHYSICAL_CONTRAST)
    radiance = math_node("EXPONENT", (-560, 380), "Radiance", flame)
    absorbed = math_node("MULTIPLY", (-380, 380), "Soot Emission", flame)
    gain = math_node("MULTIPLY", (-200, 380), "Brightness x Density", flame, NODE_PHYSICAL_GAIN)
    color_scale = math_node("MULTIPLY_ADD", (-920, 180), "Camera Color Shift", flame, NODE_PHYSICAL_COLOR_SCALE)
    color_scale.inputs[1].default_value = PHYSICAL_COLOR_SHIFT
    color_scale.inputs[2].default_value = 0.0
    fire_blackbody = nodes.new("ShaderNodeBlackbody")
    fire_blackbody.location = (-740, 180)
    fire_blackbody.parent = flame
    fire_saturation = nodes.new("ShaderNodeHueSaturation")
    fire_saturation.location = (-560, 180)
    fire_saturation.parent = flame
    fire_saturation.inputs["Saturation"].default_value = PHYSICAL_SATURATION
    links.new(volume_info.outputs["Temperature"], floor.inputs[0])
    links.new(floor.outputs[0], wien_term.inputs[1])
    links.new(wien_term.outputs[0], wien.inputs[0])
    links.new(wien.outputs[0], radiance.inputs[0])
    links.new(radiance.outputs[0], absorbed.inputs[0])
    links.new(density_value, absorbed.inputs[1])
    links.new(absorbed.outputs[0], gain.inputs[0])
    links.new(volume_info.outputs["Temperature"], color_scale.inputs[0])
    links.new(color_scale.outputs[0], fire_blackbody.inputs["Temperature"])
    links.new(fire_blackbody.outputs["Color"], fire_saturation.inputs["Color"])
    fire_color = fire_saturation.outputs["Color"]

    fresh = None
    if blue_core:
        # Blue where fuel is still burning near ignition: fresh flame.
        fuel = nodes.new("ShaderNodeAttribute")
        fuel.attribute_name = "fuel"
        fuel.label = "Fuel"
        fuel.location = (-1480, -120)
        fuel.parent = inputs
        fire_temperature = nodes.new("ShaderNodeMapRange")
        fire_temperature.data_type = "FLOAT"
        fire_temperature.interpolation_type = "SMOOTHSTEP"
        fire_temperature.name = NODE_FIRE_TEMPERATURE
        fire_temperature.label = "Flame Temperature Range"
        fire_temperature.location = (-1100, -40)
        fire_temperature.parent = flame
        presence = math_node("MULTIPLY", (-920, -40), "Fuel Presence", flame, clamp=True)
        presence.inputs[1].default_value = 4.0
        cool = math_node("SUBTRACT", (-740, -40), "Near Ignition", flame)
        cool.inputs[0].default_value = 1.0
        fresh = math_node("MULTIPLY", (-560, -40), "Fresh Fuel", flame)
        core = math_node("MULTIPLY", (-380, -40), "Blue Core", flame, NODE_FLAME_CORE, clamp=True)
        core_mix = nodes.new("ShaderNodeMix")
        core_mix.data_type = "RGBA"
        core_mix.label = "Core Color"
        core_mix.location = (-380, 180)
        core_mix.parent = flame
        rgba = [socket for socket in core_mix.inputs if socket.type == "RGBA"]
        rgba[1].default_value = (0.12, 0.3, 1.0, 1.0)
        links.new(volume_info.outputs["Temperature"], fire_temperature.inputs[0])
        links.new(_socket(fuel.outputs, "Factor", "Fac"), presence.inputs[0])
        links.new(_socket(fire_temperature.outputs, "Result"), cool.inputs[1])
        links.new(presence.outputs[0], fresh.inputs[0])
        links.new(cool.outputs[0], fresh.inputs[1])
        links.new(fresh.outputs[0], core.inputs[0])
        links.new(core.outputs[0], core_mix.inputs["Factor"])
        links.new(fire_color, rgba[0])
        fire_color = [socket for socket in core_mix.outputs if socket.type == "RGBA"][0]

    # Scatter glow: light from elsewhere = baked glow minus this voxel's own
    # source (density x Wien radiance at GLOW_REFERENCE_K, from the same X).
    local_wien = math_node("MULTIPLY_ADD", (-1100, -380), "Relative to 1500 K", glow_frame)
    local_wien.inputs[1].default_value = -1.0
    local_wien.inputs[2].default_value = WIEN_KELVIN / GLOW_REFERENCE_K
    local_radiance = math_node("EXPONENT", (-920, -380), "Local Radiance", glow_frame)
    local_source = math_node("MULTIPLY", (-740, -380), "Local Source", glow_frame)
    excess = math_node("SUBTRACT", (-560, -380), "Glow from Elsewhere", glow_frame)
    positive = math_node("MAXIMUM", (-380, -380), "Positive", glow_frame)
    positive.inputs[1].default_value = 0.0
    glow_density = math_node("MULTIPLY", (-200, -380), "Glow x Density", glow_frame)
    glow_gain = math_node("MULTIPLY", (-20, -380), "Scatter Glow Gain", glow_frame, NODE_SCATTER_GLOW_GAIN)
    glow_blackbody = nodes.new("ShaderNodeBlackbody")
    glow_blackbody.name = NODE_SCATTER_GLOW_COLOR
    glow_blackbody.label = "Fire Light Color"
    glow_blackbody.location = (-560, -580)
    glow_blackbody.parent = glow_frame
    glow_saturation = nodes.new("ShaderNodeHueSaturation")
    glow_saturation.location = (-380, -580)
    glow_saturation.parent = glow_frame
    glow_saturation.inputs["Saturation"].default_value = PHYSICAL_SATURATION
    albedo = nodes.new("ShaderNodeMix")
    albedo.data_type = "RGBA"
    albedo.blend_type = "MULTIPLY"
    albedo.name = NODE_SCATTER_GLOW_ALBEDO
    albedo.label = "Smoke Albedo"
    albedo.location = (-200, -580)
    albedo.parent = glow_frame
    albedo.inputs["Factor"].default_value = 1.0
    links.new(wien_term.outputs[0], local_wien.inputs[0])
    links.new(local_wien.outputs[0], local_radiance.inputs[0])
    links.new(local_radiance.outputs[0], local_source.inputs[0])
    links.new(density_value, local_source.inputs[1])
    spread = nodes.new("ShaderNodeMix")
    spread.data_type = "FLOAT"
    spread.name = NODE_SCATTER_GLOW_SPREAD
    spread.label = "Glow Spread"
    spread.location = (-1280, -380)
    spread.parent = glow_frame
    spread.inputs["Factor"].default_value = 0.0
    float_inputs = [socket for socket in spread.inputs if socket.type == "VALUE" and socket.name != "Factor"]
    links.new(_socket(glow.outputs, "Factor", "Fac"), float_inputs[0])
    links.new(_socket(glow_wide.outputs, "Factor", "Fac"), float_inputs[1])
    shadow_exponent = math_node("MULTIPLY", (-1280, -560), "Glow Shadowing", glow_frame, NODE_SCATTER_GLOW_SHADOW)
    shadow_exponent.inputs[1].default_value = -0.0
    shadow = math_node("EXPONENT", (-1100, -560), "Light Reaching", glow_frame)
    shadowed = math_node("MULTIPLY", (-1100, -380), "Shadowed Glow", glow_frame)
    links.new(_socket(glow_depth.outputs, "Factor", "Fac"), shadow_exponent.inputs[0])
    links.new(shadow_exponent.outputs[0], shadow.inputs[0])
    links.new([socket for socket in spread.outputs if socket.type == "VALUE"][0], shadowed.inputs[0])
    links.new(shadow.outputs[0], shadowed.inputs[1])
    links.new(shadowed.outputs[0], excess.inputs[0])
    links.new(local_source.outputs[0], excess.inputs[1])
    links.new(excess.outputs[0], positive.inputs[0])
    links.new(positive.outputs[0], glow_density.inputs[0])
    links.new(density_value, glow_density.inputs[1])
    links.new(glow_density.outputs[0], glow_gain.inputs[0])
    links.new(glow_blackbody.outputs["Color"], glow_saturation.inputs["Color"])
    links.new(glow_saturation.outputs["Color"], [s for s in albedo.inputs if s.type == "RGBA"][0])

    # Emission colour = fire colour x fire strength + glow colour x glow strength.
    def vector_node(operation, location, label):
        node = nodes.new("ShaderNodeVectorMath")
        node.operation = operation
        node.label = label
        node.location = location
        node.parent = combine
        return node

    fire_part = vector_node("SCALE", (160, 300), "Fire")
    glow_part = vector_node("SCALE", (160, 60), "Glow")
    total = vector_node("ADD", (360, 200), "Fire + Glow")
    tint = nodes.new("ShaderNodeMix")
    tint.data_type = "RGBA"
    tint.blend_type = "MULTIPLY"
    tint.name = NODE_FLAME_TINT
    tint.label = "Flame Tint"
    tint.location = (-40, 300)
    tint.parent = combine
    tint.inputs["Factor"].default_value = 1.0
    tint_inputs = [socket for socket in tint.inputs if socket.type == "RGBA"]
    tint_inputs[1].default_value = (1.0, 1.0, 1.0, 1.0)
    links.new(fire_color, tint_inputs[0])
    links.new([socket for socket in tint.outputs if socket.type == "RGBA"][0], fire_part.inputs[0])
    links.new(gain.outputs[0], fire_part.inputs["Scale"])
    links.new([s for s in albedo.outputs if s.type == "RGBA"][0], glow_part.inputs[0])
    links.new(glow_gain.outputs[0], glow_part.inputs["Scale"])
    links.new(fire_part.outputs[0], total.inputs[0])
    links.new(glow_part.outputs[0], total.inputs[1])

    shader = nodes.new("ShaderNodeVolumePrincipled")
    shader.name = NODE_VOLUME_SHADER
    shader.location = (620, 200)
    shader.parent = smoke
    # Density comes from the remap node (times Density, set on update).
    shader.inputs["Density Attribute"].default_value = ""
    density_gain = nodes.new("ShaderNodeMath")
    density_gain.operation = "MULTIPLY"
    density_gain.name = NODE_DENSITY_GAIN
    density_gain.label = "x Density"
    density_gain.location = (440, 60)
    density_gain.parent = smoke
    density_gain.inputs[1].default_value = 2.0
    links.new(density_value, density_gain.inputs[0])
    links.new(density_gain.outputs[0], shader.inputs["Density"])
    shader.inputs["Temperature Attribute"].default_value = "temperature"
    shader.inputs["Blackbody Intensity"].default_value = 0.0
    shader.inputs["Emission Strength"].default_value = 1.0
    # Pyro look: golden ramp(heat) x Brightness x Density x Wien(heat) x
    # 2 soft density / (1 + soft density / Thin Gas), plus the shared Scatter
    # Glow; mixed against the Temperature look by NODE_PYRO_MIX (0 / 1), so
    # switching needs no rebuild.
    pyro = frame("Pyro look", (-1100, -1000), (0.36, 0.18, 0.10))
    heat = nodes.new("ShaderNodeAttribute")
    heat.attribute_name = "heat"
    heat.label = "Heat above ambient (baked)"
    heat.location = (-1480, -920)
    heat.parent = inputs
    soft = nodes.new("ShaderNodeAttribute")
    soft.attribute_name = "density_soft"
    soft.label = "Soft density (baked)"
    soft.location = (-1480, -1100)
    soft.parent = inputs
    heat_kelvin = math_node("ADD", (-1280, -900), "Heat (K)", pyro)
    heat_kelvin.inputs[1].default_value = 293.15
    # The hotter of the gas and its blurred surroundings: hot cores keep their
    # peak, nearby cooler smoke picks up a soft glow.
    pyro_temperature = math_node("MAXIMUM", (-1100, -900), "Glow Temperature", pyro)
    pyro_term = math_node("DIVIDE", (-920, -900), "26160 K / Heat", pyro)
    pyro_term.inputs[0].default_value = WIEN_KELVIN
    pyro_wien = math_node("MULTIPLY_ADD", (-740, -900), "Contrast / Full Glow", pyro, NODE_PYRO_CONTRAST)
    pyro_radiance = math_node("EXPONENT", (-560, -900), "Radiance", pyro)
    soft_floor = math_node("MAXIMUM", (-1100, -1160), "Soft Floor", pyro)
    soft_floor.inputs[1].default_value = 0.0
    thin = math_node("MULTIPLY_ADD", (-920, -1160), "1 + Density / Thin Gas", pyro, NODE_PYRO_THIN)
    thin.inputs[2].default_value = 1.0
    thin_weight = math_node("DIVIDE", (-740, -1160), "Glowing Density", pyro)
    # x 2: at Thin Gas both looks emit alike; thinner gas glows up to twice as
    # bright, denser smoke turns to soot (it absorbs more than it emits).
    thin_double = math_node("MULTIPLY", (-560, -1300), "x 2", pyro)
    thin_double.inputs[1].default_value = 2.0
    pyro_source = math_node("MULTIPLY", (-560, -1160), "Soot Emission", pyro)
    pyro_gain = math_node("MULTIPLY", (-380, -1160), "Brightness x Density", pyro, NODE_PYRO_GAIN)
    color_range = nodes.new("ShaderNodeMapRange")
    color_range.name = NODE_PYRO_COLOR_RANGE
    color_range.label = "Color Range"
    color_range.location = (-920, -1360)
    color_range.parent = pyro
    ramp = nodes.new("ShaderNodeValToRGB")
    ramp.location = (-740, -1360)
    ramp.parent = pyro
    elements = ramp.color_ramp.elements
    while len(elements) < len(PYRO_RAMP):
        elements.new(0.5)
    for element, (position, color) in zip(elements, PYRO_RAMP):
        element.position = position
        element.color = (*color, 1.0)
    pyro_tint = nodes.new("ShaderNodeMix")
    pyro_tint.data_type = "RGBA"
    pyro_tint.blend_type = "MULTIPLY"
    pyro_tint.name = NODE_PYRO_TINT
    pyro_tint.location = (-460, -1360)
    pyro_tint.parent = pyro
    pyro_tint.inputs["Factor"].default_value = 1.0
    # Unit luminance, like the Blackbody node, so both looks share one exposure.
    ramp_luminance = nodes.new("ShaderNodeRGBToBW")
    ramp_luminance.location = (-460, -1520)
    ramp_luminance.parent = pyro
    luminance_floor = math_node("MAXIMUM", (-280, -1520), "Luminance Floor", pyro)
    luminance_floor.inputs[1].default_value = 0.01
    unit_luminance = math_node("DIVIDE", (-100, -1520), "1 / Luminance", pyro)
    unit_luminance.inputs[0].default_value = 1.0
    unit_ramp = nodes.new("ShaderNodeVectorMath")
    unit_ramp.operation = "SCALE"
    unit_ramp.label = "Ramp at Unit Luminance"
    unit_ramp.location = (-100, -1360)
    unit_ramp.parent = pyro
    # Blackbody colour, as the Temperature look (camera shift, saturation 1.8).
    blackbody_scale = math_node("MULTIPLY", (-920, -1760), "Colour Temperature", pyro, NODE_PYRO_BLACKBODY_SCALE)
    blackbody_scale.inputs[1].default_value = PHYSICAL_COLOR_SHIFT
    pyro_blackbody = nodes.new("ShaderNodeBlackbody")
    pyro_blackbody.location = (-740, -1760)
    pyro_blackbody.parent = pyro
    blackbody_saturation = nodes.new("ShaderNodeHueSaturation")
    blackbody_saturation.location = (-560, -1760)
    blackbody_saturation.parent = pyro
    blackbody_saturation.inputs["Saturation"].default_value = PHYSICAL_SATURATION
    color_choice = nodes.new("ShaderNodeMix")
    color_choice.data_type = "RGBA"
    color_choice.name = NODE_PYRO_COLOR
    color_choice.label = "Colour: Golden / Blackbody"
    color_choice.location = (80, -1560)
    color_choice.parent = pyro
    color_choice.inputs["Factor"].default_value = 0.0
    pyro_fire = vector_node("SCALE", (160, -200), "Pyro Fire")
    pyro_total = vector_node("ADD", (160, -380), "Pyro Fire + Glow")
    look = nodes.new("ShaderNodeMix")
    look.data_type = "RGBA"
    look.name = NODE_PYRO_MIX
    look.label = "Look: Temperature / Pyro"
    look.location = (360, -40)
    look.parent = combine
    look.inputs["Factor"].default_value = 0.0
    heat_value = _socket(heat.outputs, "Factor", "Fac")
    soft_value = _socket(soft.outputs, "Factor", "Fac")
    links.new(heat_value, heat_kelvin.inputs[0])
    links.new(heat_kelvin.outputs[0], pyro_temperature.inputs[0])
    links.new(volume_info.outputs["Temperature"], pyro_temperature.inputs[1])
    links.new(pyro_temperature.outputs[0], pyro_term.inputs[1])
    links.new(pyro_term.outputs[0], pyro_wien.inputs[0])
    links.new(pyro_wien.outputs[0], pyro_radiance.inputs[0])
    links.new(soft_value, soft_floor.inputs[0])
    links.new(soft_floor.outputs[0], thin.inputs[0])
    links.new(soft_floor.outputs[0], thin_weight.inputs[0])
    links.new(thin.outputs[0], thin_weight.inputs[1])
    links.new(pyro_radiance.outputs[0], pyro_source.inputs[0])
    links.new(thin_weight.outputs[0], thin_double.inputs[0])
    links.new(thin_double.outputs[0], pyro_source.inputs[1])
    links.new(pyro_source.outputs[0], pyro_gain.inputs[0])
    links.new(pyro_temperature.outputs[0], color_range.inputs["Value"])
    links.new(_socket(color_range.outputs, "Result"), ramp.inputs["Fac"])
    pyro_tint_inputs = [socket for socket in pyro_tint.inputs if socket.type == "RGBA"]
    links.new(ramp.outputs["Color"], unit_ramp.inputs[0])
    links.new(unit_luminance.outputs[0], unit_ramp.inputs["Scale"])
    links.new(pyro_temperature.outputs[0], blackbody_scale.inputs[0])
    links.new(blackbody_scale.outputs[0], pyro_blackbody.inputs["Temperature"])
    links.new(pyro_blackbody.outputs["Color"], blackbody_saturation.inputs["Color"])
    choice_inputs = [socket for socket in color_choice.inputs if socket.type == "RGBA"]
    links.new(unit_ramp.outputs[0], choice_inputs[0])
    links.new(blackbody_saturation.outputs["Color"], choice_inputs[1])
    pyro_color = [socket for socket in color_choice.outputs if socket.type == "RGBA"][0]
    if fresh is not None:
        # Pyro Blue Core: the same fresh-flame mask, with its own strength.
        pyro_core = math_node("MULTIPLY", (-560, -1560), "Blue Core", pyro, NODE_PYRO_CORE, clamp=True)
        pyro_core_mix = nodes.new("ShaderNodeMix")
        pyro_core_mix.data_type = "RGBA"
        pyro_core_mix.label = "Core Color"
        pyro_core_mix.location = (-560, -1700)
        pyro_core_mix.parent = pyro
        core_inputs = [socket for socket in pyro_core_mix.inputs if socket.type == "RGBA"]
        core_inputs[1].default_value = (0.12, 0.3, 1.0, 1.0)
        links.new(fresh.outputs[0], pyro_core.inputs[0])
        links.new(pyro_core.outputs[0], pyro_core_mix.inputs["Factor"])
        links.new(pyro_color, core_inputs[0])
        pyro_color = [socket for socket in pyro_core_mix.outputs if socket.type == "RGBA"][0]
    links.new(pyro_color, pyro_tint_inputs[0])
    links.new([socket for socket in pyro_tint.outputs if socket.type == "RGBA"][0], pyro_fire.inputs[0])
    links.new(ramp.outputs["Color"], ramp_luminance.inputs["Color"])
    links.new(ramp_luminance.outputs["Val"], luminance_floor.inputs[0])
    links.new(luminance_floor.outputs[0], unit_luminance.inputs[1])
    links.new(pyro_gain.outputs[0], pyro_fire.inputs["Scale"])
    links.new(pyro_fire.outputs[0], pyro_total.inputs[0])
    links.new(glow_part.outputs[0], pyro_total.inputs[1])
    look_inputs = [socket for socket in look.inputs if socket.type == "RGBA"]
    links.new(total.outputs[0], look_inputs[0])
    links.new(pyro_total.outputs[0], look_inputs[1])
    links.new([socket for socket in look.outputs if socket.type == "RGBA"][0], shader.inputs["Emission Color"])
    # Soot colour: hot soot dark in the Pyro look (factor = look x hot).
    hot = nodes.new("ShaderNodeMapRange")
    hot.interpolation_type = "SMOOTHSTEP"
    hot.inputs["From Min"].default_value = 500.0
    hot.inputs["From Max"].default_value = 900.0
    hot.location = (160, -420)
    hot.parent = smoke
    hot_look = math_node("MULTIPLY", (360, -420), "Hot x Pyro", smoke, NODE_PYRO_HOT)
    hot_look.inputs[1].default_value = 0.0
    soot = nodes.new("ShaderNodeMix")
    soot.data_type = "RGBA"
    soot.name = NODE_PYRO_ALBEDO
    soot.label = "Soot Color"
    soot.location = (440, -200)
    soot.parent = smoke
    soot_inputs = [socket for socket in soot.inputs if socket.type == "RGBA"]
    soot_inputs[1].default_value = (PYRO_HOT_SOOT, PYRO_HOT_SOOT, PYRO_HOT_SOOT, 1.0)
    links.new(volume_info.outputs["Temperature"], hot.inputs["Value"])
    links.new(_socket(hot.outputs, "Result"), hot_look.inputs[0])
    links.new(hot_look.outputs[0], soot.inputs["Factor"])
    links.new([socket for socket in soot.outputs if socket.type == "RGBA"][0], shader.inputs["Color"])
    output = nodes.new("ShaderNodeOutputMaterial")
    output.location = (900, 200)
    output.parent = smoke
    links.new(shader.outputs["Volume"], output.inputs["Volume"])
    material[GRAPH_VERSION_KEY] = GRAPH_VERSION


def update_generated_materials(domain, settings):
    updated = 0
    seen = set()
    # A generated material picked as the domain's Volume Material is still
    # Fumaris's own graph: keep it following Appearance too.
    custom = getattr(settings, "volume_material", None)
    if custom is not None and custom.get(GENERATED_MATERIAL_MARKER):
        updated += int(apply_generated_material_settings(custom, settings))
        seen.add(custom.name)
    cache_id = str(getattr(settings, "cache_id", "")).strip()
    if not cache_id:
        return updated
    from .utils import output_directory_for_object

    directory = os.path.normpath(output_directory_for_object(domain))
    prefix = str(getattr(settings, "output_prefix", PREFIX))
    for obj in bpy.data.objects:
        if not _is_generated_volume(obj, directory, prefix):
            continue
        for material in obj.data.materials:
            if material and material.get(GENERATED_MATERIAL_MARKER) and material.name not in seen:
                updated += int(apply_generated_material_settings(material, settings))
                seen.add(material.name)
    return updated


def import_sequence(
    directory,
    frame_start,
    prefix=PREFIX,
    material=None,
    selectable=True,
    appearance=None,
    frame_end=None,
):
    files = _sequence_files(directory, prefix)
    if frame_end is not None:
        files = [path for path in files if frame_start <= _frame_number(path, prefix) <= frame_end]
        expected = list(range(frame_start, frame_end + 1))
        if [_frame_number(path, prefix) for path in files] != expected:
            raise RuntimeError("Recorded preview has missing committed VDB frames")
    if not files:
        raise RuntimeError("The bridge completed without writing VDB files")
    readable = _files_with_grids(files)
    if frame_end is not None and len(readable) != len(files):
        raise RuntimeError("Recorded preview contains an unreadable committed VDB frame")
    files = readable
    if not files:
        raise RuntimeError("The bridge wrote VDB files, but none contained readable grids")

    previous_active = bpy.context.view_layer.objects.active
    previous_selection = list(bpy.context.selected_objects)
    _remove_volume_for_directory(directory, prefix)
    import_directory, import_files = _stage_import_sequence(directory, files, prefix)
    existing = set(bpy.data.objects)
    bpy.ops.object.volume_import(
        filepath=import_files[0],
        files=[{"name": os.path.basename(import_files[0])}],
        directory=import_directory,
        use_sequence_detection=False,
    )

    created = [obj for obj in bpy.data.objects if obj not in existing and obj.type == "VOLUME"]
    if not created:
        _restore_selection(previous_selection, previous_active)
        raise RuntimeError("Blender did not create a Volume object")

    volume_object = created[0]
    volume_object.name = "Fumaris Volume"
    volume_object.location = (0.0, 0.0, 0.0)
    volume_object[VOLUME_MARKER] = os.path.normpath(directory)
    volume_object[VOLUME_PREFIX_MARKER] = prefix
    sequence_start = _frame_number(files[0], prefix)
    sequence_end = _frame_number(files[-1], prefix)
    _configure_volume(
        volume_object,
        import_files[0],
        sequence_end - sequence_start + 1,
        sequence_start,
    )
    volume_object.select_set(False)
    volume_object.hide_select = not bool(selectable)
    if material is not None:
        volume_object.data.materials.append(material)
        if material.get(GENERATED_MATERIAL_MARKER):
            apply_generated_material_settings(material, appearance)
    else:
        add_density_material(
            volume_object,
            settings=appearance,
        )
    _restore_selection(previous_selection, previous_active)
    return volume_object


def _files_with_grids(files):
    started = time.perf_counter()
    valid = []
    skipped = []
    for path in files:
        try:
            if _grid_metadata(path):
                valid.append(path)
        except Exception:
            skipped.append(path)
    if skipped:
        console_log(
            f"Fumaris skipped {len(skipped)} unreadable VDB frame(s): "
            + ", ".join(os.path.basename(path) for path in skipped)
        )
    console_log(
        "Fumaris VDB metadata validation: "
        f"{len(valid)}/{len(files)} frames in "
        f"{(time.perf_counter() - started) * 1000.0:.3f}ms"
    )
    return valid


def _grid_metadata(path):
    if openvdb is not None:
        return openvdb.readAllGridMetadata(path)
    volume = bpy.data.volumes.new("Fumaris VDB Probe")
    try:
        volume.filepath = path
        volume.grids.load()
        return tuple(volume.grids)
    finally:
        bpy.data.volumes.remove(volume)


def _frame_number(path, prefix, suffix=".vdb"):
    name = os.path.basename(path)
    if not name.startswith(prefix) or not name.endswith(suffix):
        return None
    digits = name[len(prefix):len(name) - len(suffix)]
    if not _FRAME_DIGITS.fullmatch(digits):
        return None
    return int(digits)


def _frame_files(directory, prefix, suffix=".vdb"):
    """Bridge-written frame files only; other files sharing the prefix are the user's."""
    try:
        with os.scandir(directory) as entries:
            numbered = [
                (frame, entry.path)
                for entry in entries
                if (frame := _frame_number(entry.name, prefix, suffix)) is not None
            ]
    except (FileNotFoundError, NotADirectoryError):
        return []
    return [path for _frame, path in sorted(numbered)]


def _sequence_files(directory, prefix):
    return _frame_files(directory, prefix)


def _stage_import_sequence(directory, files, prefix=PREFIX):
    first_frame = _frame_number(files[0], prefix)
    if first_frame is None:
        raise RuntimeError("Cannot stage an unnumbered Fumaris VDB frame")
    root = os.path.join(directory, IMPORT_DIRECTORY)
    os.makedirs(root, exist_ok=True)
    target = os.path.join(root, str(time.time_ns()))
    os.makedirs(target, exist_ok=False)

    staged = []
    try:
        for path in files:
            frame = _frame_number(path, prefix)
            if frame is None:
                raise RuntimeError("Cannot stage an unnumbered Fumaris VDB frame")
            # Blender treats a minus sign as part of the sequence prefix.
            # Positive import-only indices keep warmup frames and gaps intact.
            filename = f"{prefix}{frame - first_frame + 1:04d}.vdb"
            destination = os.path.join(target, filename)
            try:
                os.symlink(os.path.abspath(path), destination)
            except OSError:
                try:
                    # Windows normally needs extra permission for symlinks.
                    # Cache frames are published by replacement, so hard links
                    # also preserve the imported version without copying it.
                    os.link(path, destination)
                except OSError:
                    shutil.copy2(path, destination)
            staged.append(destination)
    except Exception:
        shutil.rmtree(target, ignore_errors=True)
        raise

    _prune_import_copies(root, target)
    return target, staged


def _delete_import_copies(directory):
    root = os.path.join(directory, IMPORT_DIRECTORY)
    if os.path.isdir(root):
        shutil.rmtree(root, ignore_errors=True)


def _prune_import_copies(root, keep):
    keep = os.path.normpath(keep)
    for name in os.listdir(root):
        path = os.path.join(root, name)
        if os.path.normpath(path) == keep:
            continue
        if os.path.isdir(path):
            shutil.rmtree(path, ignore_errors=True)


def _restore_selection(selected, active):
    for obj in bpy.context.selected_objects:
        obj.select_set(False)
    for obj in selected:
        if obj.name in bpy.data.objects:
            obj.select_set(True)
    if active and active.name in bpy.data.objects:
        bpy.context.view_layer.objects.active = active


def _configure_volume(volume_object, filepath, frame_count, frame_start):
    volume = volume_object.data
    volume.filepath = filepath
    if bpy.data.filepath:
        try:
            volume.filepath = bpy.path.relpath(filepath)
        except ValueError:
            # Windows cannot express a path on another drive relative to the file.
            pass
    volume.is_sequence = True
    volume.frame_offset = 0
    volume.frame_start = frame_start
    volume.frame_duration = frame_count
    volume.sequence_mode = "EXTEND"
    volume.display.density = 1.0
    volume.display.use_slice = False
    volume.display.interpolation_method = "LINEAR"
    volume.render.space = "OBJECT"
    volume.render.step_size = 0.0
    scene = bpy.context.scene
    current_frame = scene.frame_current
    load_frame = frame_start + max(0, frame_count - 1)
    grid_names = ()
    try:
        if current_frame != load_frame:
            scene.frame_set(load_frame)
        # Sequence frame selection lives on evaluated Volume data. Loading
        # the original datablock can try frame 0 (a nonexistent import file),
        # especially when the scene already sits at this take's last frame.
        volume.update_tag()
        volume_object.update_tag()
        bpy.context.view_layer.update()
        depsgraph = bpy.context.evaluated_depsgraph_get()
        evaluated_volume = volume_object.evaluated_get(depsgraph).data
        evaluated_volume.grids.load()
        grid_names = tuple(grid.name for grid in evaluated_volume.grids)
    finally:
        if scene.frame_current != current_frame:
            scene.frame_set(current_frame)
    # OpenVDB's read order can be alphabetical even when density is written
    # first. Select the smoke field for newly imported volumes; otherwise burn
    # may be selected and make a successful bake look empty in Solid shading.
    for index, name in enumerate(grid_names):
        if name == "density":
            volume.grids.active_index = index
            break
    if "velocity" in grid_names:
        volume.velocity_grid = "velocity"
        volume.velocity_unit = "SECOND"
    volume.update_tag()
    volume_object.update_tag()


def _remove_volume_for_directory(directory, prefix=PREFIX):
    normalized = os.path.normpath(directory)
    for obj in list(bpy.data.objects):
        if not _is_generated_volume(obj, normalized, prefix):
            continue
        _remove_generated_volume_object(obj)


def _remove_generated_volume_object(obj):
    volume = obj.data if obj.type == "VOLUME" else None
    materials = tuple(volume.materials) if volume else ()
    bpy.data.objects.remove(obj, do_unlink=True)
    if volume and volume.users == 0:
        bpy.data.volumes.remove(volume)
    # Repeated pause imports replace the volume. Only discard our now-orphan
    # materials; keep user materials, shared materials and explicit fake users.
    for material in dict.fromkeys(materials):
        if (material is not None and material.get(GENERATED_MATERIAL_MARKER)
                and material.users == 0 and not material.use_fake_user):
            bpy.data.materials.remove(material)


def _is_generated_volume(obj, directory, prefix):
    marker = obj.get(VOLUME_MARKER)
    if marker:
        return (
            os.path.normpath(marker) == directory
            and obj.get(VOLUME_PREFIX_MARKER, prefix) == prefix
        )
    if obj.type != "VOLUME":
        return False
    filepath = getattr(obj.data, "filepath", "")
    if not filepath:
        return False
    absolute = os.path.normpath(bpy.path.abspath(filepath))
    import_root = os.path.normpath(os.path.join(directory, IMPORT_DIRECTORY))
    try:
        in_import_root = os.path.commonpath((absolute, import_root)) == import_root
    except ValueError:
        in_import_root = False
    return (
        (os.path.dirname(absolute) == directory or in_import_root)
        and os.path.basename(absolute).startswith(prefix)
    )
