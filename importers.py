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
# On a version's volume shown with its eye: slot switches leave it alone.
SHOWN_MARKER = "fumaris_version_shown"
VOLUME_MARKER = "fumaris_volume"
VOLUME_PREFIX_MARKER = "fumaris_volume_prefix"
GENERATED_MATERIAL_MARKER = "fumaris_generated_material"
OWN_MATERIAL_MARKER = "fumaris_own_material"   # a version's copy (Sync Material off)

NODE_VOLUME_SHADER = "Fumaris Volume Shader"
NODE_DENSITY_GAIN = "Fumaris Density Gain"
# The looks and Scatter Glow are shared node groups (built by _shader_group);
# each material's Appearance values are the inputs of these group nodes.
# Temperature look: soot absorbs, and by Kirchhoff emits in proportion, with
# radiance from Wien's law at 550 nm, exp(-c * 26160 K * (1/T - 1/Tref)), so
# gas at Full Glow glows at Brightness and cooler gas falls off as it does on a
# camera exposed for the hottest gas. Colour is blackbody, shifted and
# saturated like a camera's clipped highlights (plain blackbody orange turns
# peach under AgX), or the golden ramp.
NODE_TEMPERATURE_FIRE = "Fumaris Temperature Fire"
# Pyro look (Houdini Pyro-Bake style): the same brightness law on the baked
# "heat" grid (temperature above ambient, blurred over 1-4 voxels), colour from
# a golden ramp over 0.45-1 x Full Glow (or blackbody), emitting like the
# Temperature look at Thin Gas density, up to twice as bright in thinner gas
# and less in denser smoke, which then shows dark soot over the fire.
NODE_PYRO_FIRE = "Fumaris Pyro Fire"
NODE_LOOK_MIX = "Fumaris Look"
# Scatter glow: the bridge's glow grid is the blurred density x Wien radiance
# at GLOW_REFERENCE_K (src/vdb_frame_writer.cpp); coloured by the gas at Full
# Glow, filtered by the smoke colour.
NODE_SCATTER_GLOW = "Fumaris Scatter Glow"
NODE_SMOKE_COLOR = "Fumaris Smoke Colour"
# Blue Core only (built when Blue Core > 0): fuel near ignition turns blue.
NODE_FRESH_FUEL = "Fumaris Fresh Fuel"
BLUE_CORE_COLOR = (0.12, 0.3, 1.0, 1.0)
# 2: Temperature look only, one emission closure, frames. 3: Colour Spread.
# 4: Density Remap. 5-6: Pyro look. 7: Pyro Blue Core. 8: Pyro Colour.
# 9: Temperature look Colour. 10: node groups (shared, values on the group
# nodes). Older graphs are rebuilt on the next update.
GRAPH_VERSION = 10
SHADER_GROUP_VERSION = 1
GRAPH_VERSION_KEY = "fumaris_graph_version"
PHYSICAL_COLOR_SHIFT = 1.3
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
        if not marker or obj.get(SHOWN_MARKER):
            continue  # not a slot's bake, or shown with its eye in Versions
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
    """Older graphs (before the node groups) and a Blue Core switched on or
    off since the graph was built are rebuilt in place."""
    nodes = material.node_tree.nodes
    if material.get(GRAPH_VERSION_KEY, 0) < GRAPH_VERSION:
        return True
    return _wants_core(settings) != (nodes.get(NODE_FRESH_FUEL) is not None)


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

    smoke_color = (*[float(value) for value in tuple(_appearance_value(
        settings, "shader_smoke_color", (0.6, 0.6, 0.6)))[:3]], 1.0)
    density = max(0.0, float(_appearance_value(settings, "shader_smoke_density", 2.0)))
    shader.inputs["Color"].default_value = smoke_color
    shader.inputs["Density"].default_value = density
    nodes[NODE_DENSITY_GAIN].inputs[1].default_value = density
    shader.inputs["Anisotropy"].default_value = max(
        -0.999999, min(0.999999, float(_appearance_value(settings, "shader_anisotropy", 0.3))))

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

    def set_inputs(node_name, values):
        node = nodes[node_name]
        for name, value in values.items():
            node.inputs[name].default_value = value

    def wien(look):
        # Brightness law exp(-c * 26160 * (1/T - 1/Tg)) = exp(X * -c + c * 26160 / Tg), X = 26160 / T.
        return {"Contrast": -look["contrast"], "Full Glow": look["contrast"] * WIEN_KELVIN / look["reference"],
                "Brightness": (look["brightness"] * density) if enabled else 0.0,
                "Tint": look["tint"], "Blue Core": look["core"]}

    temperature_look = look_values(False)
    pyro_values = look_values(True)
    active = pyro_values if pyro_look else temperature_look
    spread = max(0.05, float(_appearance_value(settings, "shader_color_spread", 1.0)))

    # Temperature look. Colour temperature = shift x multiplier x (Full Glow +
    # Colour Spread x (T - Full Glow)); Golden: the Pyro ramp over 0.45-1 x Full Glow.
    look = temperature_look
    golden = str(_appearance_value(settings, "shader_color", "BLACKBODY")) == "GOLDEN"
    set_inputs(NODE_TEMPERATURE_FIRE, {
        **wien(look),
        "Colour Scale": PHYSICAL_COLOR_SHIFT * look["multiplier"] * spread,
        "Colour Offset": PHYSICAL_COLOR_SHIFT * look["multiplier"] * look["reference"] * (1.0 - spread),
        "Golden": 1.0 if golden else 0.0,
        "Golden From": PYRO_COLOR_RANGE[0] * look["reference"] / look["multiplier"],
        "Golden To": PYRO_COLOR_RANGE[1] * look["reference"] / look["multiplier"],
    })

    # Pyro look: the same brightness law on the baked heat, Thin Gas, colour
    # range (Blackbody Scale shifts it).
    look = pyro_values
    thin_gas = max(1e-4, float(_appearance_value(settings, "shader_thin_gas", 1.0)))
    blackbody = str(_appearance_value(settings, "pyro_color", "GOLDEN")) == "BLACKBODY"
    set_inputs(NODE_PYRO_FIRE, {
        **wien(look),
        "1 / Thin Gas": 1.0 / thin_gas,
        "Golden From": PYRO_COLOR_RANGE[0] * look["reference"] / look["multiplier"],
        "Golden To": PYRO_COLOR_RANGE[1] * look["reference"] / look["multiplier"],
        "Blackbody": 1.0 if blackbody else 0.0,
        "Blackbody Scale": PHYSICAL_COLOR_SHIFT * look["multiplier"],
    })
    nodes[NODE_LOOK_MIX].inputs["Factor"].default_value = 1.0 if pyro_look else 0.0
    set_inputs(NODE_SMOKE_COLOR, {"Smoke Colour": smoke_color, "Hot Soot": 1.0 if pyro_look else 0.0})

    # Scatter Glow (the active look's values), on the flame's radiance scale at Contrast 1.
    look = active
    to_full_glow = math.exp(WIEN_KELVIN * (1.0 / look["reference"] - 1.0 / GLOW_REFERENCE_K))
    set_inputs(NODE_SCATTER_GLOW, {
        "Brightness": (SCATTER_GLOW_SCALE * look["scatter"] * look["brightness"] * density * to_full_glow)
        if enabled else 0.0,
        "Light Temperature": look["reference"] * PHYSICAL_COLOR_SHIFT * look["multiplier"],
        "Glow Spread": look["glow_spread"],
        "Glow Shadowing": -look["glow_shadowing"],
        "Smoke Colour": smoke_color,
    })

    if nodes.get(NODE_FRESH_FUEL) is not None:
        start_temperature, full_temperature = flame_temperature_range(
            float(_appearance_value(settings, "flame_temperature_min", 800.0)),
            float(_appearance_value(settings, "flame_temperature_max", 3000.0)))
        set_inputs(NODE_FRESH_FUEL, {"Flame Start": start_temperature, "Flame Full": full_temperature})
    material.diffuse_color = smoke_color
    material.node_tree.update_tag()
    return True


def add_density_material(volume_object, settings=None):
    material = bpy.data.materials.new("Fumaris Material")
    material.use_nodes = True
    material[GENERATED_MATERIAL_MARKER] = True
    _build_generated_material(material, blue_core=_wants_core(settings))
    apply_generated_material_settings(material, settings)
    volume_object.data.materials.append(material)


# --- Node groups -----------------------------------------------------------
# Shared by every generated material (the per-material values are the group
# nodes' inputs). Rebuilt in place when SHADER_GROUP_VERSION changes.

class _Graph:
    """Small helpers for building a node tree: labelled nodes in frames."""

    def __init__(self, tree):
        self.nodes = tree.nodes
        self.links = tree.links

    def node(self, kind, location, label="", parent=None, name=None, **settings):
        node = self.nodes.new(kind)
        node.location = location
        if label:
            node.label = label
        if parent is not None:
            node.parent = parent
        if name:
            node.name = name
        for key, value in settings.items():
            setattr(node, key, value)
        return node

    def math(self, operation, location, label, parent=None, inputs=(), clamp=False, name=None):
        node = self.node("ShaderNodeMath", location, label, parent, name, operation=operation, use_clamp=clamp)
        for index, value in inputs:
            node.inputs[index].default_value = value
        return node

    def mix(self, data_type, location, label, parent=None, blend="MIX", factor=None, name=None):
        node = self.node("ShaderNodeMix", location, label, parent, name, data_type=data_type, blend_type=blend)
        if factor is not None:
            node.inputs["Factor"].default_value = factor
        return node

    def frame(self, label, location, color):
        node = self.node("NodeFrame", location, label)
        node.use_custom_color = True
        node.color = color
        node.label_size = 18
        return node

    def link(self, output, input_socket):
        self.links.new(output, input_socket)


def _rgba(sockets):
    return [socket for socket in sockets if socket.type == "RGBA"]


def _float_out(node):
    return [socket for socket in node.outputs if socket.type == "VALUE"][0]


def _shader_group(name, inputs, outputs, build):
    """A shared shader node group: (name, socket type, default) inputs and
    (name, socket type) outputs; build(graph, group_in, group_out) adds its nodes."""
    group = bpy.data.node_groups.get(name)
    if group is not None and group.bl_idname == "ShaderNodeTree" and group.get(GRAPH_VERSION_KEY) == SHADER_GROUP_VERSION:
        return group
    if group is None or group.bl_idname != "ShaderNodeTree":
        group = bpy.data.node_groups.new(name, "ShaderNodeTree")
    group.nodes.clear()
    group.interface.clear()
    for socket_name, socket_type, default in inputs:
        socket = group.interface.new_socket(socket_name, in_out="INPUT", socket_type=socket_type)
        if default is not None:
            socket.default_value = default
    for socket_name, socket_type in outputs:
        group.interface.new_socket(socket_name, in_out="OUTPUT", socket_type=socket_type)
    graph = _Graph(group)
    group_in = graph.node("NodeGroupInput", (-900, 0))
    group_out = graph.node("NodeGroupOutput", (900, 0))
    build(graph, group_in.outputs, group_out.inputs)
    group[GRAPH_VERSION_KEY] = SHADER_GROUP_VERSION
    return group


def _ramp(graph, location, label, parent):
    ramp = graph.node("ShaderNodeValToRGB", location, label, parent)
    elements = ramp.color_ramp.elements
    while len(elements) < len(PYRO_RAMP):
        elements.new(0.5)
    for element, (position, color) in zip(elements, PYRO_RAMP):
        element.position = position
        element.color = (*color, 1.0)
    return ramp


def _unit_luminance(graph, color, location, parent):
    """color / its luminance (floored at 0.01), as the Blackbody node, so the
    golden ramp and blackbody colours share one exposure."""
    x, y = location
    luminance = graph.node("ShaderNodeRGBToBW", (x, y - 160), "Luminance", parent)
    floor = graph.math("MAXIMUM", (x + 180, y - 160), "Luminance Floor", parent, ((1, 0.01),))
    inverse = graph.math("DIVIDE", (x + 360, y - 160), "1 / Luminance", parent, ((0, 1.0),))
    unit = graph.node("ShaderNodeVectorMath", (x + 360, y), "At Unit Luminance", parent, operation="SCALE")
    graph.link(color, luminance.inputs["Color"])
    graph.link(luminance.outputs["Val"], floor.inputs[0])
    graph.link(floor.outputs[0], inverse.inputs[1])
    graph.link(color, unit.inputs[0])
    graph.link(inverse.outputs[0], unit.inputs["Scale"])
    return unit.outputs[0]


def _blackbody(graph, temperature, location, parent, label="Blackbody"):
    """Blackbody colour, saturated like a camera's clipped highlights."""
    x, y = location
    node = graph.node("ShaderNodeBlackbody", (x, y), label, parent)
    saturation = graph.node("ShaderNodeHueSaturation", (x + 180, y), "Camera Saturation", parent)
    saturation.inputs["Saturation"].default_value = PHYSICAL_SATURATION
    graph.link(temperature, node.inputs["Temperature"])
    graph.link(node.outputs["Color"], saturation.inputs["Color"])
    return saturation.outputs["Color"]


def _flame_finish(graph, color, strength, fresh, core, tint, location, parent, out):
    """Blue Core (fresh fuel x strength), Flame Tint, x brightness."""
    x, y = location
    core_factor = graph.math("MULTIPLY", (x, y - 200), "Blue Core Amount", parent, clamp=True)
    core_mix = graph.mix("RGBA", (x, y), "Blue Core", parent)
    _rgba(core_mix.inputs)[1].default_value = BLUE_CORE_COLOR
    tinted = graph.mix("RGBA", (x + 200, y), "Flame Tint", parent, blend="MULTIPLY", factor=1.0)
    emission = graph.node("ShaderNodeVectorMath", (x + 400, y), "Colour x Brightness", parent, operation="SCALE")
    graph.link(fresh, core_factor.inputs[0])
    graph.link(core, core_factor.inputs[1])
    graph.link(core_factor.outputs[0], core_mix.inputs["Factor"])
    graph.link(color, _rgba(core_mix.inputs)[0])
    graph.link(_rgba(core_mix.outputs)[0], _rgba(tinted.inputs)[0])
    graph.link(tint, _rgba(tinted.inputs)[1])
    graph.link(_rgba(tinted.outputs)[0], emission.inputs[0])
    graph.link(strength, emission.inputs["Scale"])
    graph.link(emission.outputs[0], out["Emission"])


_FLAME_INPUTS = (
    ("Density", "NodeSocketFloat", 0.0),
    ("Fresh Fuel", "NodeSocketFloat", 0.0),
    ("Contrast", "NodeSocketFloat", -0.5),
    ("Full Glow", "NodeSocketFloat", 0.0),
    ("Brightness", "NodeSocketFloat", 1.0),
    ("Tint", "NodeSocketColor", (1.0, 1.0, 1.0, 1.0)),
    ("Blue Core", "NodeSocketFloat", 0.0),
    ("Golden From", "NodeSocketFloat", 720.0),
    ("Golden To", "NodeSocketFloat", 1600.0),
)


def _temperature_fire_group():
    """Temperature look: soot emits by Kirchhoff with Wien radiance at 550 nm,
    exp(-c * 26160 K * (1/T - 1/Full Glow)) x density x Brightness; colour
    blackbody (camera shift and saturation) or the golden ramp."""
    def build(graph, i, out):
        law = graph.frame("Brightness: Wien's law", (-660, 360), (0.36, 0.22, 0.12))
        floor = graph.math("MAXIMUM", (-660, 300), "Temperature Floor", law, ((1, 300.0),))
        term = graph.math("DIVIDE", (-480, 300), "26160 K / T", law, ((0, WIEN_KELVIN),))
        wien = graph.math("MULTIPLY_ADD", (-300, 300), "x Contrast + Full Glow", law)
        radiance = graph.math("EXPONENT", (-120, 300), "Radiance", law)
        soot = graph.math("MULTIPLY", (60, 300), "x Density", law)
        strength = graph.math("MULTIPLY", (240, 300), "x Brightness", law)
        graph.link(i["Temperature"], floor.inputs[0])
        graph.link(floor.outputs[0], term.inputs[1])
        graph.link(term.outputs[0], wien.inputs[0])
        graph.link(i["Contrast"], wien.inputs[1])
        graph.link(i["Full Glow"], wien.inputs[2])
        graph.link(wien.outputs[0], radiance.inputs[0])
        graph.link(radiance.outputs[0], soot.inputs[0])
        graph.link(i["Density"], soot.inputs[1])
        graph.link(soot.outputs[0], strength.inputs[0])
        graph.link(i["Brightness"], strength.inputs[1])

        colour = graph.frame("Colour", (-660, -60), (0.30, 0.20, 0.14))
        shift = graph.math("MULTIPLY_ADD", (-660, -100), "Camera Colour Shift", colour)
        blackbody = _blackbody(graph, shift.outputs[0], (-480, -100), colour)
        golden_range = graph.node("ShaderNodeMapRange", (-660, -320), "Golden Range", colour)
        ramp = _ramp(graph, (-480, -320), "Golden Ramp", colour)
        golden = _unit_luminance(graph, ramp.outputs["Color"], (-240, -320), colour)
        choice = graph.mix("RGBA", (180, -100), "Blackbody / Golden", colour)
        graph.link(i["Temperature"], shift.inputs[0])
        graph.link(i["Colour Scale"], shift.inputs[1])
        graph.link(i["Colour Offset"], shift.inputs[2])
        graph.link(floor.outputs[0], golden_range.inputs["Value"])
        graph.link(i["Golden From"], golden_range.inputs["From Min"])
        graph.link(i["Golden To"], golden_range.inputs["From Max"])
        graph.link(_socket(golden_range.outputs, "Result"), ramp.inputs["Fac"])
        graph.link(i["Golden"], choice.inputs["Factor"])
        graph.link(blackbody, _rgba(choice.inputs)[0])
        graph.link(golden, _rgba(choice.inputs)[1])

        finish = graph.frame("Blue Core, Tint", (400, -60), (0.24, 0.20, 0.28))
        _flame_finish(graph, _rgba(choice.outputs)[0], strength.outputs[0], i["Fresh Fuel"], i["Blue Core"],
                      i["Tint"], (420, -100), finish, out)

    return _shader_group(
        "Fumaris Temperature Fire",
        (("Temperature", "NodeSocketFloat", 300.0), *_FLAME_INPUTS,
         ("Colour Scale", "NodeSocketFloat", PHYSICAL_COLOR_SHIFT),
         ("Colour Offset", "NodeSocketFloat", 0.0),
         ("Golden", "NodeSocketFloat", 0.0)),
        (("Emission", "NodeSocketColor"),),
        build)


def _pyro_fire_group():
    """Pyro look: the same brightness law on the hotter of the gas and its
    baked, blurred heat; emits like the Temperature look at Thin Gas density
    (2 soft density / (1 + soft density / Thin Gas)); golden ramp or blackbody."""
    def build(graph, i, out):
        law = graph.frame("Brightness: Wien's law on the baked heat", (-660, 360), (0.36, 0.18, 0.10))
        kelvin = graph.math("ADD", (-660, 300), "Heat (K)", law, ((1, 293.15),))
        hotter = graph.math("MAXIMUM", (-480, 300), "Glow Temperature", law)
        term = graph.math("DIVIDE", (-300, 300), "26160 K / T", law, ((0, WIEN_KELVIN),))
        wien = graph.math("MULTIPLY_ADD", (-120, 300), "x Contrast + Full Glow", law)
        radiance = graph.math("EXPONENT", (60, 300), "Radiance", law)
        graph.link(i["Heat"], kelvin.inputs[0])
        graph.link(kelvin.outputs[0], hotter.inputs[0])
        graph.link(i["Temperature"], hotter.inputs[1])
        graph.link(hotter.outputs[0], term.inputs[1])
        graph.link(term.outputs[0], wien.inputs[0])
        graph.link(i["Contrast"], wien.inputs[1])
        graph.link(i["Full Glow"], wien.inputs[2])
        graph.link(wien.outputs[0], radiance.inputs[0])

        thin = graph.frame("Thin Gas", (-660, 120), (0.30, 0.24, 0.16))
        soft = graph.math("MAXIMUM", (-660, 60), "Soft Density", thin, ((1, 0.0),))
        denominator = graph.math("MULTIPLY_ADD", (-480, 60), "1 + Density / Thin Gas", thin, ((2, 1.0),))
        glowing = graph.math("DIVIDE", (-300, 60), "Glowing Density", thin)
        double = graph.math("MULTIPLY", (-120, 60), "x 2", thin, ((1, 2.0),))
        source = graph.math("MULTIPLY", (60, 60), "Radiance x Glowing", thin)
        strength = graph.math("MULTIPLY", (240, 60), "x Brightness", thin)
        graph.link(i["Soft Density"], soft.inputs[0])
        graph.link(soft.outputs[0], denominator.inputs[0])
        graph.link(i["1 / Thin Gas"], denominator.inputs[1])
        graph.link(soft.outputs[0], glowing.inputs[0])
        graph.link(denominator.outputs[0], glowing.inputs[1])
        graph.link(glowing.outputs[0], double.inputs[0])
        graph.link(radiance.outputs[0], source.inputs[0])
        graph.link(double.outputs[0], source.inputs[1])
        graph.link(source.outputs[0], strength.inputs[0])
        graph.link(i["Brightness"], strength.inputs[1])

        colour = graph.frame("Colour", (-660, -200), (0.30, 0.20, 0.14))
        golden_range = graph.node("ShaderNodeMapRange", (-660, -240), "Golden Range", colour)
        ramp = _ramp(graph, (-480, -240), "Golden Ramp", colour)
        golden = _unit_luminance(graph, ramp.outputs["Color"], (-240, -240), colour)
        scale = graph.math("MULTIPLY", (-660, -560), "Colour Temperature", colour)
        blackbody = _blackbody(graph, scale.outputs[0], (-480, -560), colour)
        choice = graph.mix("RGBA", (180, -240), "Golden / Blackbody", colour)
        graph.link(hotter.outputs[0], golden_range.inputs["Value"])
        graph.link(i["Golden From"], golden_range.inputs["From Min"])
        graph.link(i["Golden To"], golden_range.inputs["From Max"])
        graph.link(_socket(golden_range.outputs, "Result"), ramp.inputs["Fac"])
        graph.link(hotter.outputs[0], scale.inputs[0])
        graph.link(i["Blackbody Scale"], scale.inputs[1])
        graph.link(i["Blackbody"], choice.inputs["Factor"])
        graph.link(golden, _rgba(choice.inputs)[0])
        graph.link(blackbody, _rgba(choice.inputs)[1])

        finish = graph.frame("Blue Core, Tint", (400, -200), (0.24, 0.20, 0.28))
        _flame_finish(graph, _rgba(choice.outputs)[0], strength.outputs[0], i["Fresh Fuel"], i["Blue Core"],
                      i["Tint"], (420, -240), finish, out)

    return _shader_group(
        "Fumaris Pyro Fire",
        (("Heat", "NodeSocketFloat", 0.0), ("Temperature", "NodeSocketFloat", 300.0),
         ("Soft Density", "NodeSocketFloat", 0.0), *_FLAME_INPUTS[1:],
         ("1 / Thin Gas", "NodeSocketFloat", 1.0),
         ("Blackbody", "NodeSocketFloat", 0.0),
         ("Blackbody Scale", "NodeSocketFloat", PHYSICAL_COLOR_SHIFT)),
        (("Emission", "NodeSocketColor"),),
        build)


def _scatter_glow_group():
    """Light from the fire scattered by smoke: the baked glow (blurred density
    x Wien radiance at 1500 K; blended toward glow_wide by Glow Spread, dimmed
    by the smoke toward the fire, exp(-shadowing x glow_depth)) minus this
    voxel's own source, x density, coloured by the gas at Full Glow and the
    smoke colour."""
    def build(graph, i, out):
        light = graph.frame("Glow from elsewhere", (-660, 360), (0.32, 0.26, 0.10))
        spread = graph.mix("FLOAT", (-660, 300), "Glow Spread", light)
        shadow_exponent = graph.math("MULTIPLY", (-660, 80), "x Shadowing", light)
        reaching = graph.math("EXPONENT", (-480, 80), "Light Reaching", light)
        shadowed = graph.math("MULTIPLY", (-480, 300), "Shadowed Glow", light)
        floor = graph.math("MAXIMUM", (-480, -120), "Temperature Floor", light, ((1, 300.0),))
        term = graph.math("DIVIDE", (-300, -120), "26160 K / T", light, ((0, WIEN_KELVIN),))
        relative = graph.math("MULTIPLY_ADD", (-120, -120), "Relative to 1500 K", light,
                              ((1, -1.0), (2, WIEN_KELVIN / GLOW_REFERENCE_K)))
        local = graph.math("EXPONENT", (60, -120), "Local Radiance", light)
        own = graph.math("MULTIPLY", (240, -120), "Own Source", light)
        excess = graph.math("SUBTRACT", (-300, 300), "Minus Own Source", light)
        positive = graph.math("MAXIMUM", (-120, 300), "Positive", light, ((1, 0.0),))
        scattered = graph.math("MULTIPLY", (60, 300), "x Density", light)
        strength = graph.math("MULTIPLY", (240, 300), "x Brightness", light)
        float_in = [socket for socket in spread.inputs if socket.type == "VALUE" and socket.name != "Factor"]
        graph.link(i["Glow Spread"], spread.inputs["Factor"])
        graph.link(i["Glow"], float_in[0])
        graph.link(i["Glow Wide"], float_in[1])
        graph.link(i["Glow Depth"], shadow_exponent.inputs[0])
        graph.link(i["Glow Shadowing"], shadow_exponent.inputs[1])
        graph.link(shadow_exponent.outputs[0], reaching.inputs[0])
        graph.link(_float_out(spread), shadowed.inputs[0])
        graph.link(reaching.outputs[0], shadowed.inputs[1])
        graph.link(i["Temperature"], floor.inputs[0])
        graph.link(floor.outputs[0], term.inputs[1])
        graph.link(term.outputs[0], relative.inputs[0])
        graph.link(relative.outputs[0], local.inputs[0])
        graph.link(local.outputs[0], own.inputs[0])
        graph.link(i["Density"], own.inputs[1])
        graph.link(shadowed.outputs[0], excess.inputs[0])
        graph.link(own.outputs[0], excess.inputs[1])
        graph.link(excess.outputs[0], positive.inputs[0])
        graph.link(positive.outputs[0], scattered.inputs[0])
        graph.link(i["Density"], scattered.inputs[1])
        graph.link(scattered.outputs[0], strength.inputs[0])
        graph.link(i["Brightness"], strength.inputs[1])

        colour = graph.frame("Colour", (-660, -300), (0.30, 0.24, 0.12))
        blackbody = _blackbody(graph, i["Light Temperature"], (-480, -340), colour, "Fire Light Colour")
        albedo = graph.mix("RGBA", (-120, -340), "x Smoke Colour", colour, blend="MULTIPLY", factor=1.0)
        emission = graph.node("ShaderNodeVectorMath", (420, 100), "Colour x Brightness", None, operation="SCALE")
        graph.link(blackbody, _rgba(albedo.inputs)[0])
        graph.link(i["Smoke Colour"], _rgba(albedo.inputs)[1])
        graph.link(_rgba(albedo.outputs)[0], emission.inputs[0])
        graph.link(strength.outputs[0], emission.inputs["Scale"])
        graph.link(emission.outputs[0], out["Emission"])

    return _shader_group(
        "Fumaris Scatter Glow",
        (("Temperature", "NodeSocketFloat", 300.0), ("Density", "NodeSocketFloat", 0.0),
         ("Glow", "NodeSocketFloat", 0.0), ("Glow Wide", "NodeSocketFloat", 0.0),
         ("Glow Depth", "NodeSocketFloat", 0.0), ("Glow Spread", "NodeSocketFloat", 0.0),
         ("Glow Shadowing", "NodeSocketFloat", 0.0), ("Brightness", "NodeSocketFloat", 0.0),
         ("Light Temperature", "NodeSocketFloat", GLOW_REFERENCE_K * PHYSICAL_COLOR_SHIFT),
         ("Smoke Colour", "NodeSocketColor", (0.6, 0.6, 0.6, 1.0))),
        (("Emission", "NodeSocketColor"),),
        build)


def _fresh_fuel_group():
    """Blue Core mask: fuel still burning near ignition (fresh flame)."""
    def build(graph, i, out):
        presence = graph.math("MULTIPLY", (-480, 120), "Fuel Presence", None, ((1, 4.0),), clamp=True)
        heat = graph.node("ShaderNodeMapRange", (-480, -80), "Flame Temperature Range", None,
                          data_type="FLOAT", interpolation_type="SMOOTHSTEP")
        near = graph.math("SUBTRACT", (-280, -80), "Near Ignition", None, ((0, 1.0),))
        fresh = graph.math("MULTIPLY", (-80, 40), "Fresh Fuel", None)
        graph.link(i["Fuel"], presence.inputs[0])
        graph.link(i["Temperature"], heat.inputs[0])
        graph.link(i["Flame Start"], heat.inputs[1])
        graph.link(i["Flame Full"], heat.inputs[2])
        graph.link(_socket(heat.outputs, "Result"), near.inputs[1])
        graph.link(presence.outputs[0], fresh.inputs[0])
        graph.link(near.outputs[0], fresh.inputs[1])
        graph.link(fresh.outputs[0], out["Fresh Fuel"])

    return _shader_group(
        "Fumaris Fresh Fuel",
        (("Fuel", "NodeSocketFloat", 0.0), ("Temperature", "NodeSocketFloat", 300.0),
         ("Flame Start", "NodeSocketFloat", 800.0), ("Flame Full", "NodeSocketFloat", 3000.0)),
        (("Fresh Fuel", "NodeSocketFloat"),),
        build)


def _smoke_color_group():
    """Smoke colour; with Hot Soot (the Pyro look) hot soot turns dark."""
    def build(graph, i, out):
        hot = graph.node("ShaderNodeMapRange", (-480, 0), "Hot (500-900 K)", None, interpolation_type="SMOOTHSTEP")
        hot.inputs["From Min"].default_value = 500.0
        hot.inputs["From Max"].default_value = 900.0
        amount = graph.math("MULTIPLY", (-280, 0), "x Hot Soot", None)
        soot = graph.mix("RGBA", (-80, 120), "Soot Colour", None)
        _rgba(soot.inputs)[1].default_value = (PYRO_HOT_SOOT, PYRO_HOT_SOOT, PYRO_HOT_SOOT, 1.0)
        graph.link(i["Temperature"], hot.inputs["Value"])
        graph.link(_socket(hot.outputs, "Result"), amount.inputs[0])
        graph.link(i["Hot Soot"], amount.inputs[1])
        graph.link(amount.outputs[0], soot.inputs["Factor"])
        graph.link(i["Smoke Colour"], _rgba(soot.inputs)[0])
        graph.link(_rgba(soot.outputs)[0], out["Colour"])

    return _shader_group(
        "Fumaris Smoke Colour",
        (("Temperature", "NodeSocketFloat", 300.0), ("Smoke Colour", "NodeSocketColor", (0.6, 0.6, 0.6, 1.0)),
         ("Hot Soot", "NodeSocketFloat", 0.0)),
        (("Colour", "NodeSocketColor"),),
        build)


def _build_generated_material(material, blue_core=False):
    """Fumaris's volume material: the grids (left), the fire looks and Scatter
    Glow as shared node groups (middle), the smoke and Principled Volume
    (right). One emission closure for Eevee. Appearance settings are the group
    nodes' inputs, which apply_generated_material_settings writes."""
    tree = material.node_tree
    tree.nodes.clear()
    graph = _Graph(tree)
    grids = graph.frame("Grids", (-1000, 420), (0.20, 0.24, 0.30))
    fire = graph.frame("Fire", (-330, 420), (0.36, 0.22, 0.12))
    glow_frame = graph.frame("Scatter Glow", (-330, -560), (0.32, 0.26, 0.10))
    smoke = graph.frame("Smoke", (380, 420), (0.22, 0.26, 0.22))

    info = graph.node("ShaderNodeVolumeInfo", (-1000, 360), "", grids)
    density = graph.math("MAXIMUM", (-760, 200), "Density Floor", grids, ((1, 0.0),))
    graph.link(info.outputs["Density"], density.inputs[0])

    def attribute(grid, label, y):
        node = graph.node("ShaderNodeAttribute", (-1000, y), label, grids, attribute_name=grid)
        return _socket(node.outputs, "Factor", "Fac")

    heat = attribute("heat", "Heat above ambient (baked)", 120)
    soft = attribute("density_soft", "Soft density (baked)", -60)
    glow = attribute("glow", "Glow (baked)", -560)
    glow_wide = attribute("glow_wide", "Glow, wide (baked)", -740)
    glow_depth = attribute("glow_depth", "Smoke to the fire (baked)", -920)
    temperature = info.outputs["Temperature"]

    def group(kind, location, label, name, parent):
        node = graph.node("ShaderNodeGroup", location, label, parent, name)
        node.node_tree = kind()
        node.width = 220
        return node

    temperature_fire = group(_temperature_fire_group, (-330, 360), "Temperature Look", NODE_TEMPERATURE_FIRE, fire)
    pyro_fire = group(_pyro_fire_group, (-330, 0), "Pyro Look", NODE_PYRO_FIRE, fire)
    for node in (temperature_fire, pyro_fire):
        graph.link(temperature, node.inputs["Temperature"])
    graph.link(density.outputs[0], temperature_fire.inputs["Density"])
    graph.link(heat, pyro_fire.inputs["Heat"])
    graph.link(soft, pyro_fire.inputs["Soft Density"])
    if blue_core:
        fuel = attribute("fuel", "Fuel", -240)
        fresh = group(_fresh_fuel_group, (-600, -240), "Blue Core: Fresh Fuel", NODE_FRESH_FUEL, fire)
        graph.link(fuel, fresh.inputs["Fuel"])
        graph.link(temperature, fresh.inputs["Temperature"])
        for node in (temperature_fire, pyro_fire):
            graph.link(fresh.outputs["Fresh Fuel"], node.inputs["Fresh Fuel"])
    look = graph.mix("RGBA", (-40, 260), "Look: Temperature / Pyro", fire, factor=0.0, name=NODE_LOOK_MIX)
    graph.link(temperature_fire.outputs["Emission"], _rgba(look.inputs)[0])
    graph.link(pyro_fire.outputs["Emission"], _rgba(look.inputs)[1])

    scatter = group(_scatter_glow_group, (-330, -620), "Scatter Glow", NODE_SCATTER_GLOW, glow_frame)
    graph.link(temperature, scatter.inputs["Temperature"])
    graph.link(density.outputs[0], scatter.inputs["Density"])
    graph.link(glow, scatter.inputs["Glow"])
    graph.link(glow_wide, scatter.inputs["Glow Wide"])
    graph.link(glow_depth, scatter.inputs["Glow Depth"])
    emission = graph.node("ShaderNodeVectorMath", (160, 260), "Fire + Glow", fire, operation="ADD")
    graph.link(_rgba(look.outputs)[0], emission.inputs[0])
    graph.link(scatter.outputs["Emission"], emission.inputs[1])

    colour = group(_smoke_color_group, (380, 160), "Smoke Colour", NODE_SMOKE_COLOR, smoke)
    graph.link(temperature, colour.inputs["Temperature"])
    gain = graph.math("MULTIPLY", (380, 0), "x Density", smoke, ((1, 2.0),), name=NODE_DENSITY_GAIN)
    graph.link(density.outputs[0], gain.inputs[0])
    shader = graph.node("ShaderNodeVolumePrincipled", (660, 360), "", smoke, NODE_VOLUME_SHADER)
    shader.inputs["Density Attribute"].default_value = ""
    shader.inputs["Temperature Attribute"].default_value = "temperature"
    shader.inputs["Blackbody Intensity"].default_value = 0.0
    shader.inputs["Emission Strength"].default_value = 1.0
    graph.link(_rgba(colour.outputs)[0], shader.inputs["Color"])
    graph.link(gain.outputs[0], shader.inputs["Density"])
    graph.link(emission.outputs[0], shader.inputs["Emission Color"])
    output = graph.node("ShaderNodeOutputMaterial", (940, 360), "", smoke)
    graph.link(shader.outputs["Volume"], output.inputs["Volume"])
    material[GRAPH_VERSION_KEY] = GRAPH_VERSION


def synced_material(domain, create=True):
    """The material versions with Sync Material on share: the domain's Volume
    Material when one is set, else Fumaris's managed material (kept in step
    with Appearance; made on first use)."""
    props = domain.fumaris
    if props.volume_material is not None:
        return props.volume_material
    material = props.managed_material
    if material is None and create:
        material = bpy.data.materials.new(f"Fumaris Material ({domain.name})")
        material.use_nodes = True
        material[GENERATED_MATERIAL_MARKER] = True
        _build_generated_material(material, blue_core=_wants_core(props))
        apply_generated_material_settings(material, props)
        props.managed_material = material
    return material


def own_material_copy(volume_object, source, label):
    """Sync Material off: the object's own copy (unchanged by Appearance)."""
    material = source.copy() if source is not None else None
    if material is None:
        material = bpy.data.materials.new("Fumaris Material")
        material.use_nodes = True
        material[GENERATED_MATERIAL_MARKER] = True
        _build_generated_material(material)
    material.name = f"Fumaris Material {label}"
    material[OWN_MATERIAL_MARKER] = True
    _assign_material(volume_object, material)
    return material


def _assign_material(volume_object, material):
    materials = volume_object.data.materials
    if len(materials):
        materials[0] = material
    else:
        materials.append(material)


def update_generated_materials(domain, settings):
    updated = 0
    seen = set()
    # A generated material picked as the domain's Volume Material is still
    # Fumaris's own graph: keep it following Appearance too.
    custom = getattr(settings, "volume_material", None)
    if custom is not None and custom.get(GENERATED_MATERIAL_MARKER):
        updated += int(apply_generated_material_settings(custom, settings))
        seen.add(custom.name)
    managed = getattr(settings, "managed_material", None)
    if managed is not None and managed.name not in seen:
        updated += int(apply_generated_material_settings(managed, settings))
        seen.add(managed.name)
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
            # A version's own copy (Sync Material off) keeps its look.
            if (material and material.get(GENERATED_MATERIAL_MARKER) and not material.get(OWN_MATERIAL_MARKER)
                    and material.name not in seen):
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
    _move_out_of_participant_collections(volume_object)
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
        last_filepath=import_files[-1],
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


def _participant_collections():
    """Every domain's emitter, collider, effector and outflow collections,
    with their child collections."""
    found = set()
    for obj in bpy.data.objects:
        props = getattr(obj, "fumaris", None)
        if props is None or props.smoke_object_type != "domain":
            continue
        for name in ("emitter_collection", "collider_collection", "effector_collection", "outflow_collection"):
            top = getattr(props, name, None)
            if top is not None:
                found.add(top)
                found.update(top.children_recursive)
    return found


def _move_out_of_participant_collections(obj):
    """Volume Import links to the active collection. Inside a participant
    collection the volume would be taken for a participant and, because a
    domain points at that collection, Blender evaluates it (reloading a VDB
    every frame) even while it is hidden. Move it to the nearest parent
    collection that is not a participant collection."""
    participants = _participant_collections()
    inside = [collection for collection in obj.users_collection if collection in participants]
    if not inside:
        return
    scene = bpy.context.scene
    parents = {child: parent for parent in bpy.data.collections for child in parent.children}
    target = inside[0]
    while target is not None and target in participants:
        target = parents.get(target)
    target = target or scene.collection
    if obj.name not in target.objects:
        target.objects.link(obj)
    for collection in inside:
        collection.objects.unlink(obj)


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


def _configure_volume(volume_object, filepath, frame_count, frame_start, last_filepath=None):
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
    # The last frame's grids (early frames can lack some), read from its file's
    # metadata: evaluating the volume there loaded that frame's voxels, which
    # Blender then keeps in its memory cache (about one frame of RAM for every
    # import, until the Memory Cache Limit), and moved the scene frame twice.
    grid_names = _file_grid_names(last_filepath)
    if grid_names is None:
        grid_names = _evaluated_grid_names(volume_object, frame_start + max(0, frame_count - 1))
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


def _file_grid_names(path):
    """Grid names in the file's order (as Blender lists them), from its
    metadata alone; None without the openvdb module or an unreadable file."""
    if openvdb is None or not path:
        return None
    try:
        return tuple(grid.name for grid in openvdb.readAllGridMetadata(path))
    except Exception:
        return None


def _evaluated_grid_names(volume_object, load_frame):
    """Grid names at load_frame as Blender loads them (without the openvdb module)."""
    volume = volume_object.data
    scene = bpy.context.scene
    current_frame = scene.frame_current
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
        return tuple(grid.name for grid in evaluated_volume.grids)
    finally:
        if scene.frame_current != current_frame:
            scene.frame_set(current_frame)


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
