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
from .utils import WIEN_KELVIN, flame_temperature_range, flame_value, glow_source_heat, is_pyro_look

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
# Look Controls: every Appearance value the material uses, as the inputs of one
# group node at the far left of the graph, named as in the Look panel. Sync
# (apply_generated_material_settings) writes these inputs and nothing else, so
# a material detached from Fumaris is tuned there by hand. The group derives
# what the stages need (Wien terms, colour shifts, the Scatter Glow scale).
NODE_CONTROLS = "Fumaris Look Controls"
# Fire: both looks (the shared groups below) and the switch between them.
NODE_FIRE = "Fumaris Fire"
# Hot Smoke Density: hot gas denser than cool smoke, x1 at Hot Smoke From to
# x Hot Smoke Density at Hot Smoke To (Houdini pyro: density 5 cold, 50 hot).
# x1 (off) folds out of the shader.
NODE_HOT_DENSITY = "Fumaris Hot Smoke Density"
# The looks and Scatter Glow are shared node groups (built by _shader_group),
# fed by the Look Controls.
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
NODE_LOOK_MIX = "Fumaris Look"   # in the Fire group
# Colour Ramp (Color: Ramp in either look): the material's own ramp, which the
# panel draws; both looks read it through "Ramp Colour". Its position is the
# gas temperature over 0.45-1 x Full Glow (NODE_RAMP_RANGE).
NODE_RAMP = "Fumaris Flame Ramp"
# Thin Gas (Pyro): the fixed value, or Auto, the ratio x the bake's per-frame
# density reference ("density_ref", 0 in older bakes: then the fixed value).
NODE_THIN_GAS = "Fumaris Thin Gas"
NODE_RAMP_RANGE = "Fumaris Flame Ramp Range"

# Scatter glow: the bridge's glow grid is the blurred density x Wien radiance
# at GLOW_REFERENCE_K (src/vdb_frame_writer.cpp); coloured by the gas at Full
# Glow, filtered by the smoke colour.
NODE_SCATTER_GLOW = "Fumaris Scatter Glow"
NODE_SCATTER_MASK = "Fumaris Scatter Soot Mask"
NODE_SMOKE_COLOR = "Fumaris Smoke Colour"
NODE_COLOR_MIX = "Fumaris Colour Mixing"
# Shadow rays need only the extinction (density): a Mix Shader by Is Shadow
# Ray gives them a Volume Absorption of the same density, so Cycles' SVM jumps
# over the look's nodes and grid reads on shadow rays. The image is identical
# (same seed, 2026-11-16 profile); sampling 1.36x faster biased, 3.8x unbiased.
NODE_SHADOW_MIX = "Fumaris Shadow Rays"
NODE_SHADOW_EXTINCTION = "Fumaris Shadow Extinction"
# Packed Pyro grids (VDB writer): one 20 cm vec3 grid, (heat, glow,
# glow_depth) on the heat lattice, instead of heat (20 cm) and glow and
# glow_depth (1.4 m+). Cycles unions the topology of every grid it reads into
# the volume mesh by resampling between transforms, so a coarse grid turned
# the smoke's bounds into 1.4 m blocks of marched empty space (and capped the
# ray-march steps there). Older caches keep the separate grids. The graph
# reads the layout of the caches it is used with (GRID_LAYOUT_KEY on each
# volume at import and on the material, see sync_grid_layout): "packed",
# "separate", or "both" (a material shared by older and newer caches; a
# missing grid reads 0, but its lookup still costs ~12%).
PACKED_PYRO_GRID = "pyro"
GRID_LAYOUT_KEY = "fumaris_grid_layout"
BUILT_GRID_LAYOUT_KEY = "fumaris_built_grid_layout"
# Thin Gas Auto: the frame's density reference (the writer's
# "fumaris_density_reference" metadata) as an object property keyed per frame
# at import (an object attribute is a constant, not a grid read per sample).
# Older caches also have it as the "density_ref" grid; the graph takes the
# larger of the two.
DENSITY_REFERENCE_PROPERTY = "fumaris_density_reference"
# Blue Core only (built when Blue Core > 0): fuel near ignition turns blue.
NODE_FRESH_FUEL = "Fumaris Fresh Fuel"
BLUE_CORE_COLOR = (0.12, 0.3, 1.0, 1.0)
# 2: Temperature look only, one emission closure, frames. 3: Colour Spread.
# 4: Density Remap. 5-6: Pyro look. 7: Pyro Blue Core. 8: Pyro Colour.
# 9: Temperature look Colour. 10: node groups (shared, values on the group
# nodes). Older graphs are rebuilt on the next update.
# 18: shadow rays skip the look (Is Shadow Ray), packed "pyro" grid, density
# reference from an object property. 19: Look Controls (all values on one
# node), Fire and Thin Gas groups, Hot Smoke Density, laid out in stages.
# Group 9: laid out in stages (same sockets and maths as 8, so graphs before
# 19, such as a version's own copy, still work).
GRAPH_VERSION = 20
SHADER_GROUP_VERSION = 9
GRAPH_VERSION_KEY = "fumaris_graph_version"
# Pyro Soot Mask: glow fades from full at 0.5 x to none at 1.5 x Thin Gas density.
PYRO_SOOT_MASK = (0.5, 1.5)
# Pyro: the gas's own temperature counts at this share against the baked,
# blurred heat (full weight drew a crackle of flat patches; the blurred heat
# alone gives round, popcorn-like cells; some local heat keeps bakes without
# a heat grid glowing).
PYRO_LOCAL_HEAT = 0.6
# Pyro presence: glow ramps in from no smoke to this x Thin Gas (clear air stays dark).
PYRO_PRESENCE = 0.2
PHYSICAL_COLOR_SHIFT = 1.3
PYRO_RAMP = ((0.0, (1.0, 0.1, 0.0)), (0.35, (1.0, 0.32, 0.02)), (0.65, (1.0, 0.58, 0.14)),
             (1.0, (1.0, 0.86, 0.52)))
PYRO_COLOR_RANGE = (0.45, 1.0)  # x Full Glow: deep red to pale gold
PYRO_HOT_SOOT = 0.06            # albedo of hot soot (dark); cool smoke keeps Smoke Color
GLOW_REFERENCE_K = 1500.0
# Scatter Glow keeps its strength at these look defaults (scatter_glow_scale).
SCATTER_DEFAULT_CONTRAST = 0.5
SCATTER_DEFAULT_FULL_GLOW = 2000.0
SCATTER_GLOW_SCALE = 1.0
# Glow Source Warm Gas: the glow grid is a 0..1 ramp (not Wien radiance at
# 1500 K); this brings it to the flame's brightness scale.
GLOW_HEAT_SCALE = 100.0
PHYSICAL_SATURATION = 1.8


# Volumes hidden by their domain's eye (Show in Viewport), shown again with it.
HIDDEN_WITH_DOMAIN_MARKER = "fumaris_hidden_with_domain"


def domain_volumes(domain):
    """The volumes imported from this domain's bakes (any version or slot)."""
    from .utils import peek_output_directory_for_object
    directory = peek_output_directory_for_object(domain)
    if not directory:
        return []
    root = os.path.normpath(os.path.dirname(directory))
    found = []
    for obj in bpy.data.objects:
        marker = obj.get(VOLUME_MARKER)
        if marker and os.path.normpath(marker).startswith(root):
            found.append(obj)
    return found


def set_domain_viewport_visible(domain, visible):
    """Show in Viewport: hide (or show again) the domain and its imported volumes.
    Showing again only unhides what the eye hid; renders are untouched."""
    for obj in [domain, *domain_volumes(domain)]:
        if not visible:
            if not obj.hide_viewport:
                obj[HIDDEN_WITH_DOMAIN_MARKER] = True
                obj.hide_viewport = True
        elif obj.get(HIDDEN_WITH_DOMAIN_MARKER):
            del obj[HIDDEN_WITH_DOMAIN_MARKER]
            obj.hide_viewport = False


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
            _FRAME_METADATA.pop(_metadata_key(path), None)
            os.remove(path)
        # The bake's particles (particles/<prefix>particles_####.ply): left
        # behind, a shorter bake into the slot showed the old run's particles
        # past its end (the particle objects read the folder frame by frame).
        particle_directory, particle_prefix = _particle_files(directory, prefix)
        for path in _frame_files(particle_directory, particle_prefix, ".ply"):
            os.remove(path)
        delete_temporary_writes(directory, prefix)
        try:
            os.rmdir(particle_directory)   # only when nothing else is in it
        except OSError:
            pass
        _delete_import_copies(directory)


def _particle_files(directory, prefix):
    """The folder and file-name prefix of a bake's particle frames
    (src/particles.cpp, particles.frame_files)."""
    return os.path.join(directory, "particles"), f"{prefix}particles_"


def delete_temporary_writes(directory, prefix=PREFIX):
    particle_directory, particle_prefix = _particle_files(directory, prefix)
    for folder, head, suffix in ((directory, prefix, ".vdb.tmp"),
                                 (particle_directory, particle_prefix, ".ply.tmp")):
        for path in _frame_files(folder, head, suffix):
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
    """Older graphs (before the Look Controls) and a Blue Core switched on or
    off since the graph was built are rebuilt in place."""
    nodes = material.node_tree.nodes
    if material.get(GRAPH_VERSION_KEY, 0) < GRAPH_VERSION or nodes.get(NODE_CONTROLS) is None:
        return True
    if material.get(GRID_LAYOUT_KEY, "separate") != material.get(BUILT_GRID_LAYOUT_KEY, "separate"):
        return True
    return _wants_core(settings) != (nodes.get(NODE_FRESH_FUEL) is not None)


# --- Look Controls -----------------------------------------------------------
# (panel, controls); a control is (name, kind, default, description[, min[,
# max]]), kind FLOAT, KELVIN, FACTOR, BOOL or RGBA. Names and descriptions are
# the Look panel's (Smoke's Color and Density read "Smoke Color" and "Smoke
# Density" here). Fumaris keeps separate Temperature and Pyro values for the
# Fire controls and writes the chosen Look's.
_CONTROLS = (
    ("Fire", (
        ("Flame", "BOOL", True, "Show burn-driven fire. Off: smoke only"),
        ("Pyro Look", "BOOL", False,
         "Look: on is Pyro (fire from the baked heat, glowing through thin gas; dense billows read as soot), "
         "off is Temperature (gas glows by its own temperature and density)"),
        ("Color Ramp", "BOOL", False,
         "Color: on is Ramp (the Flame Ramp in the Colour Ramp frame, over 0.45-1 x Full Glow), off is "
         "Blackbody"),
        ("Brightness", "FLOAT", 2.0, "Emission strength of the fire", 0.0),
        ("Full Glow", "KELVIN", 2000.0,
         "Temperature (K) at which fire glows at full Brightness; hotter gas is brighter, cooler gas falls off "
         "by Contrast", 1.0),
        ("Contrast", "FLOAT", 0.5,
         "How fast glow falls off as gas cools below Full Glow, which sets how wide flames look. 1 matches a "
         "camera exposed for the hottest gas (thin, sparkly flames); lower values let cooler gas glow too", 0.05),
        ("Color Spread", "FLOAT", 1.0,
         "Temperature look, Blackbody: how far fire colour moves with temperature around Full Glow. 1 is "
         "physical blackbody colour; lower values keep hot and cool gas closer to the Full Glow colour", 0.05),
        ("Blackbody Scale", "FLOAT", 1.0,
         "Multiplies temperature before the fire colour: above 1 shifts fire toward yellow-white, below 1 "
         "toward deep red. Brightness is not changed", 0.01),
        ("Flame Tint", "RGBA", (1.0, 1.0, 1.0, 1.0),
         "Multiplies the flame colour, for stylised or chemical flames (green copper, blue gas). White keeps "
         "the physical colour"),
    )),
    ("Blue Core", (
        ("Blue Core", "FACTOR", 0.0,
         "Blue emission where fuel is still burning near the source, as in fresh flames. Needs fuel in the "
         "simulation (the fuel grid, read in a Blue Core frame that Fumaris adds when Blue Core is above 0)",
         0.0, 1.0),
        ("Start Temperature", "KELVIN", 800.0, "Temperature (K) where generated fire emission begins", 0.0),
        ("Full Temperature", "KELVIN", 3000.0,
         "Temperature (K) where generated fire emission reaches full strength", 0.0),
    )),
    ("Thin Gas (Pyro)", (
        ("Thin Gas", "FLOAT", 3.0,
         "Pyro look: smoke density at which fire glows as in the Temperature look. Thinner gas glows (up to "
         "twice as bright); denser smoke fades to dark soot over the fire (none above 1.5 x)", 1e-4),
        ("Thin Gas Ratio", "FLOAT", 0.12,
         "Pyro look, Thin Gas Auto: Thin Gas as a share of the frame's typical (density-weighted mean) smoke "
         "density. Lower: only the thinnest skins of each billow glow; higher: more of the smoke glows", 1e-4),
        ("Thin Gas Auto", "BOOL", True,
         "Pyro look: set Thin Gas from each frame's smoke (Thin Gas Ratio x its typical density). Off: Thin "
         "Gas is the fixed value"),
    )),
    ("Scatter Glow", (
        ("Scatter Glow", "FLOAT", 1.0,
         "Fire light scattered into the surrounding smoke, so dark billows glow from inside near flames "
         "(the baked glow grid)", 0.0),
        ("Glow Spread", "FACTOR", 0.0,
         "How far Scatter Glow reaches into the smoke. 0 keeps fire light within Glow Radius of the flames; "
         "higher values blend in light spread four times farther", 0.0, 1.0),
        ("Glow Shadowing", "FLOAT", 0.5,
         "How much smoke between a spot and the fire blocks Scatter Glow. Higher values keep sooty caps dark",
         0.0),
        ("Glow Source Warm Gas", "BOOL", False,
         "Glow Source the cache was baked with: on is Warm Gas (Auto with the Pyro look), off is Fire "
         "Brightness. Must match the bake"),
    )),
    ("Smoke", (
        ("Smoke Color", "RGBA", (0.6, 0.6, 0.6, 1.0), "Smoke color"),
        ("Smoke Density", "FLOAT", 2.0, "Density multiplier of the smoke (the fire glows by density too)", 0.0),
        ("Anisotropy", "FLOAT", 0.3,
         "How much smoke scatters light forward (positive) or back (negative). Real smoke and soot scatter "
         "forward, so a backlit plume glows at its edges; 0 scatters evenly", -0.999999, 0.999999),
        ("Per-Emitter Color", "BOOL", False,
         "Each emitter's smoke carries its own Smoke Color: multiply by the bake's color grid"),
        ("Hot Soot", "FACTOR", 1.0,
         "Pyro look: how dark hot soot turns (0 keeps hot smoke the Smoke Color, 1 is full dark soot)", 0.0, 1.0),
    )),
    ("Hot Smoke Density", (
        ("Hot Smoke Density", "FLOAT", 1.0,
         "Hot gas is this many times denser than cool smoke (x1 at Hot Smoke From, rising to this at Hot "
         "Smoke To and above), as Houdini's pyro shading thickens fire. 1 is off. Render only", 0.0),
        ("Hot Smoke From", "KELVIN", 500.0, "Temperature (K) where smoke starts to thicken", 0.0),
        ("Hot Smoke To", "KELVIN", 1500.0, "Temperature (K) where smoke is Hot Smoke Density times as dense",
         0.0),
    )),
)
_CONTROL_SOCKETS = {"FLOAT": "NodeSocketFloat", "KELVIN": "NodeSocketFloat", "FACTOR": "NodeSocketFloat",
                    "BOOL": "NodeSocketBool", "RGBA": "NodeSocketColor"}
# What the Look Controls group hands the graph, one output per link, in the
# order of the bus lanes (top to bottom; see _route_bus).
_CONTROL_OUTPUTS = (
    ("Smoke Color", "RGBA"), ("Hot Soot", "FLOAT"), ("Per-Emitter Color", "FLOAT"),
    ("Hot Smoke Extra", "FLOAT"), ("Hot Smoke From", "FLOAT"), ("Hot Smoke To", "FLOAT"),
    ("Smoke Density", "FLOAT"), ("Anisotropy", "FLOAT"),
    ("Blue Core Start", "FLOAT"), ("Blue Core Full", "FLOAT"),
    ("Ramp From", "FLOAT"), ("Ramp To", "FLOAT"),
    ("Thin Gas", "FLOAT"), ("Thin Gas Ratio", "FLOAT"), ("Thin Gas Auto", "FLOAT"),
    ("Pyro Look", "FLOAT"), ("Wien Contrast", "FLOAT"), ("Wien Full Glow", "FLOAT"),
    ("Fire Brightness", "FLOAT"), ("Flame Tint", "RGBA"), ("Blue Core", "FLOAT"),
    ("Colour Scale", "FLOAT"), ("Colour Offset", "FLOAT"), ("Color Ramp", "FLOAT"),
    ("Blackbody Scale", "FLOAT"),
    ("Glow Spread", "FLOAT"), ("Glow Shadowing", "FLOAT"), ("Glow Brightness", "FLOAT"),
    ("Own Source", "FLOAT"), ("Light Temperature", "FLOAT"), ("Glow Color Ramp", "FLOAT"),
    ("Glow Smoke Color", "RGBA"), ("Scatter Soot Mask", "FLOAT"),
)


def control_values(settings):
    """The Look Controls' values for these Appearance settings (None: the
    defaults). Sync writes exactly these."""
    pyro = is_pyro_look(settings)

    def look(key, default):
        return flame_value(settings, key, default, pyro)

    smoke_color = (*[float(value) for value in tuple(_appearance_value(
        settings, "shader_smoke_color", (0.6, 0.6, 0.6)))[:3]], 1.0)
    start, full = flame_temperature_range(
        float(_appearance_value(settings, "flame_temperature_min", 800.0)),
        float(_appearance_value(settings, "flame_temperature_max", 3000.0)))
    colour = _appearance_value(settings, "pyro_color" if pyro else "shader_color", "BLACKBODY")
    return {
        "Flame": bool(_appearance_value(settings, "shader_flame_enabled", True)),
        "Pyro Look": pyro,
        "Color Ramp": str(colour) == "GOLDEN",
        "Brightness": max(0.0, float(look("brightness", 1.0))),
        "Full Glow": max(1.0, float(look("glow_temperature", 1600.0))),
        "Contrast": max(0.05, float(look("contrast", 0.5))),
        "Color Spread": max(0.05, float(_appearance_value(settings, "shader_color_spread", 1.0))),
        "Blackbody Scale": max(0.01, float(look("temperature_multiplier", 1.0))),
        "Flame Tint": (*[max(0.0, float(value)) for value in tuple(look("flame_tint", (1.0, 1.0, 1.0)))[:3]], 1.0),
        "Blue Core": max(0.0, min(1.0, float(look("flame_core", 0.0)))),
        "Start Temperature": start,
        "Full Temperature": full,
        "Thin Gas": max(1e-4, float(_appearance_value(settings, "shader_thin_gas", 3.0))),
        "Thin Gas Ratio": max(1e-4, float(_appearance_value(settings, "shader_thin_gas_ratio", 0.12))),
        "Thin Gas Auto": bool(_appearance_value(settings, "shader_thin_gas_auto", True)),
        "Scatter Glow": max(0.0, float(look("scatter_glow", 1.0))),
        "Glow Spread": max(0.0, min(1.0, float(look("glow_spread", 0.0)))),
        "Glow Shadowing": max(0.0, float(look("glow_shadowing", 0.0))),
        "Glow Source Warm Gas": bool(glow_source_heat(settings)),
        "Smoke Color": smoke_color,
        "Smoke Density": max(0.0, float(_appearance_value(settings, "shader_smoke_density", 2.0))),
        "Anisotropy": max(-0.999999, min(0.999999, float(_appearance_value(settings, "shader_anisotropy", 0.3)))),
        "Per-Emitter Color": bool(_appearance_value(settings, "color_mixing", False)),
        "Hot Soot": max(0.0, min(1.0, float(_appearance_value(settings, "shader_hot_soot", 1.0)))),
        "Hot Smoke Density": max(0.0, float(_appearance_value(settings, "shader_hot_density", 1.0))),
        "Hot Smoke From": max(0.0, float(_appearance_value(settings, "shader_hot_density_from", 500.0))),
        "Hot Smoke To": max(0.0, float(_appearance_value(settings, "shader_hot_density_to", 1500.0))),
    }


def apply_generated_material_settings(material, settings):
    """Appearance -> the Look Controls node (every value the graph uses)."""
    if material is None or not material.use_nodes or material.node_tree is None:
        return False
    if _graph_needs_rebuild(material, settings):
        _build_generated_material(
            material, blue_core=_wants_core(settings))
    nodes = material.node_tree.nodes
    controls = nodes.get(NODE_CONTROLS)
    if controls is None or nodes.get(NODE_VOLUME_SHADER) is None:
        return False
    values = control_values(settings)
    for name, value in values.items():
        controls.inputs[name].default_value = value
    # Experiment only (no Appearance setting): a soft cap on Pyro fire brightness.
    nodes[NODE_FIRE].inputs["Soft Cap"].default_value = float(os.environ.get("FUMARIS_EXP_PYRO_CAP", "0"))
    material.diffuse_color = values["Smoke Color"]
    # Linear interpolation's slope jumps at every voxel face: dense volumes
    # rendered concentric rings, one per voxel layer, wherever their surface
    # crosses the grid (Explosion fireball, beta 3 review R31; an analytic
    # ellipsoid shows them too). Cubic is smooth; ~1.6x the render time.
    cycles = getattr(material, "cycles", None)
    if cycles is not None and hasattr(cycles, "volume_interpolation"):
        smooth = bool(_appearance_value(settings, "shader_smooth_voxels", True))
        cycles.volume_interpolation = "CUBIC" if smooth else "LINEAR"
    material.node_tree.update_tag()
    return True


def scatter_glow_scale(full_glow, contrast):
    """Scatter Glow on the flame's brightness scale. The baked glow is Wien
    radiance at 1500 K; the flame falls off by Contrast, so the glow does too:
    exp(26160 K x Contrast x (1/Full Glow - 1/1500 K)), times a constant that
    keeps the defaults (Contrast 0.5, Full Glow 2000 K) where they were. The
    former Contrast-1 scale made a Full Glow under 1500 K blow the glow up
    (x567 at 1100 K) over a flame that brightens far less. The Look Controls
    group computes the same (_SCATTER_SCALE_OFFSET)."""
    return math.exp(WIEN_KELVIN * (contrast * (1.0 / full_glow - 1.0 / GLOW_REFERENCE_K) + _SCATTER_SCALE_OFFSET))


_SCATTER_SCALE_OFFSET = ((1.0 / SCATTER_DEFAULT_FULL_GLOW - 1.0 / GLOW_REFERENCE_K)
                         - SCATTER_DEFAULT_CONTRAST * (1.0 / SCATTER_DEFAULT_FULL_GLOW - 1.0 / GLOW_REFERENCE_K))


def add_density_material(volume_object, settings=None, grid_layout="separate"):
    material = bpy.data.materials.new("Fumaris Material")
    material.use_nodes = True
    material[GENERATED_MATERIAL_MARKER] = True
    material[GRID_LAYOUT_KEY] = grid_layout
    _build_generated_material(material, blue_core=_wants_core(settings))
    apply_generated_material_settings(material, settings)
    volume_object.data.materials.append(material)


# --- Layout -----------------------------------------------------------------
# Node sizes as the node editor draws them (UI scale 1), to place nodes on a
# grid without overlaps; deterministic (no drawing needed).
_ROW = 22.0
_HEADER = 32.0
_OPTION_ROWS = {"ShaderNodeMath": 2.0, "ShaderNodeVectorMath": 1.0, "ShaderNodeMapRange": 3.0,
                "ShaderNodeMix": 4.2, "ShaderNodeValToRGB": 6.4, "ShaderNodeAttribute": 2.0,
                "ShaderNodeGroup": 1.0, "ShaderNodeVolumePrincipled": 0.0, "ShaderNodeMixShader": 0.0,
                "ShaderNodeOutputMaterial": 1.0}


def _shown(socket):
    return socket.enabled and not socket.hide


def _input_rows(socket):
    if socket.type == "VECTOR" and not socket.is_linked and not socket.hide_value:
        return 4.0
    return 1.0


def _panel_rows(node):
    tree = node.node_tree if node.bl_idname == "ShaderNodeGroup" else None
    return 0 if tree is None else sum(1 for item in tree.interface.items_tree if item.item_type == "PANEL")


def _node_height(node):
    if node.bl_idname == "NodeReroute":
        return 0.0
    if node.hide:
        return 26.0
    rows = sum(1 for socket in node.outputs if _shown(socket)) + _OPTION_ROWS.get(node.bl_idname, 0.0)
    rows += sum(_input_rows(socket) for socket in node.inputs if _shown(socket)) + _panel_rows(node)
    return _HEADER + rows * _ROW


def _socket_y(node, socket):
    """The y of an input socket's centre (estimated as _node_height)."""
    y = _node_y(node) - _HEADER - _ROW * (sum(1 for s in node.outputs if _shown(s))
                                             + _OPTION_ROWS.get(node.bl_idname, 0.0))
    panels = {}
    tree = node.node_tree if node.bl_idname == "ShaderNodeGroup" else None
    if tree is not None:
        panels = {item.identifier: item.parent.name for item in tree.interface.items_tree
                  if item.item_type == "SOCKET" and item.in_out == "INPUT" and item.parent is not None}
    current_panel = ""
    for candidate in node.inputs:
        if not _shown(candidate):
            continue
        panel = panels.get(candidate.identifier, "")
        if panel != current_panel:
            current_panel = panel
            y -= _ROW
        if candidate == socket:
            return y - _ROW * 0.5
        y -= _ROW * _input_rows(candidate)
    raise KeyError(socket.name)


def _place(node, x, y):
    """Top-left at (x, y) in the tree (not relative to the node's frame)."""
    if hasattr(node, "location_absolute"):
        node.location_absolute = (x, y)
    else:
        node.location = (x, y)


def _grid(rows, x, y, gap_x=60.0, gap_y=36.0, widths=None):
    """Place rows of nodes (top to bottom, None leaves a cell empty) on a grid
    from the top-left (x, y): each column as wide as its widest node (or
    widths), each row as tall as its tallest. Returns (right, bottom)."""
    columns = max(len(row) for row in rows)
    if widths is None:
        widths = [max((row[c].width for row in rows if c < len(row) and row[c] is not None), default=0.0)
                  for c in range(columns)]
    for row in rows:
        left, height = x, 0.0
        for column, node in enumerate(row):
            if node is not None:
                _place(node, left, y)
                height = max(height, _node_height(node))
            left += widths[column] + gap_x
        y -= height + gap_y
    return x + sum(widths) + gap_x * (len(widths) - 1), y + gap_y


def _hide_unused(nodes):
    """Group Input/Output nodes show only the sockets they link (several
    Group Inputs each feed one stage, instead of noodles across the group)."""
    for node in nodes:
        if node.bl_idname in ("NodeGroupInput", "NodeGroupOutput"):
            for socket in (*node.inputs, *node.outputs):
                if not socket.is_linked:
                    socket.hide = True


# --- Node groups -----------------------------------------------------------
# Shared by every generated material (the per-material values are on the
# Look Controls node). Rebuilt in place when SHADER_GROUP_VERSION changes.

class _Graph:
    """Small helpers for building a node tree: labelled nodes in frames."""

    def __init__(self, tree):
        self.tree = tree
        self.nodes = tree.nodes
        self.links = tree.links

    def node(self, kind, label="", parent=None, name=None, **settings):
        node = self.nodes.new(kind)
        if label:
            node.label = label
        if parent is not None:
            node.parent = parent
        if name:
            node.name = name
        for key, value in settings.items():
            setattr(node, key, value)
        return node

    def math(self, operation, label, parent=None, inputs=(), clamp=False, name=None):
        node = self.node("ShaderNodeMath", label, parent, name, operation=operation, use_clamp=clamp)
        for index, value in inputs:
            node.inputs[index].default_value = value
        return node

    def mix(self, data_type, label, parent=None, blend="MIX", factor=None, name=None):
        node = self.node("ShaderNodeMix", label, parent, name, data_type=data_type, blend_type=blend)
        if factor is not None:
            node.inputs["Factor"].default_value = factor
        return node

    def frame(self, label, color):
        node = self.node("NodeFrame", label)
        node.use_custom_color = True
        node.color = color
        node.label_size = 18
        node.shrink = True
        return node

    def group_input(self, parent=None, label=""):
        """One of several Group Input nodes: each shows the sockets it links."""
        return self.node("NodeGroupInput", label, parent)

    def link(self, output, input_socket):
        self.links.new(output, input_socket)


# Frame colours by stage.
_FRAME_INPUT = (0.20, 0.24, 0.30)
_FRAME_CONTROLS = (0.16, 0.30, 0.34)
_FRAME_FIRE = (0.36, 0.22, 0.12)
_FRAME_COLOUR = (0.30, 0.20, 0.14)
_FRAME_CORE = (0.24, 0.20, 0.28)
_FRAME_GLOW = (0.32, 0.26, 0.10)
_FRAME_THIN = (0.30, 0.24, 0.16)
_FRAME_SMOKE = (0.22, 0.26, 0.22)
_FRAME_OUTPUT = (0.18, 0.18, 0.18)


def _rgba(sockets):
    return [socket for socket in sockets if socket.type == "RGBA"]


def _float_out(node):
    return [socket for socket in node.outputs if socket.type == "VALUE"][0]


def _float_in(node):
    """A float Mix node's A and B."""
    return [socket for socket in node.inputs if socket.type == "VALUE" and socket.name != "Factor"]


def _new_socket(group, name, in_out, socket_type, default=None, parent=None, description="",
                minimum=None, maximum=None, subtype=None):
    socket = group.interface.new_socket(name, in_out=in_out, socket_type=socket_type, parent=parent)
    if description:
        socket.description = description
    if subtype is not None:
        socket.subtype = subtype
    if minimum is not None:
        socket.min_value = minimum
    if maximum is not None:
        socket.max_value = maximum
    if default is not None and in_out == "INPUT":
        socket.default_value = default
    return socket


def _shader_group(name, inputs, outputs, build, panels=()):
    """A shared shader node group: (name, socket type, default) inputs and
    (name, socket type) outputs, then panels of inputs, (panel name,
    [(name, socket type, default, description, min, max, subtype)]).
    build(graph, group_in, group_out) adds the nodes; group_in() makes another
    Group Input node (each stage gets its own)."""
    group = bpy.data.node_groups.get(name)
    if group is not None and group.bl_idname == "ShaderNodeTree" and group.get(GRAPH_VERSION_KEY) == SHADER_GROUP_VERSION:
        return group
    if group is None or group.bl_idname != "ShaderNodeTree":
        group = bpy.data.node_groups.new(name, "ShaderNodeTree")
    group.nodes.clear()
    group.interface.clear()
    for socket_name, socket_type in outputs:
        _new_socket(group, socket_name, "OUTPUT", socket_type)
    for socket_name, socket_type, default in inputs:
        _new_socket(group, socket_name, "INPUT", socket_type, default)
    for panel_name, sockets in panels:
        panel = group.interface.new_panel(panel_name)
        for socket_name, socket_type, default, description, minimum, maximum, subtype in sockets:
            _new_socket(group, socket_name, "INPUT", socket_type, default, panel, description, minimum, maximum,
                        subtype)
    graph = _Graph(group)
    group_out = graph.node("NodeGroupOutput")

    def group_in(parent=None):
        return graph.group_input(parent).outputs

    build(graph, group_in, group_out)
    _hide_unused(group.nodes)
    group[GRAPH_VERSION_KEY] = SHADER_GROUP_VERSION
    return group


def _ramp(graph, label, parent):
    ramp = graph.node("ShaderNodeValToRGB", label, parent)
    elements = ramp.color_ramp.elements
    while len(elements) < len(PYRO_RAMP):
        elements.new(0.5)
    for element, (position, color) in zip(elements, PYRO_RAMP):
        element.position = position
        element.color = (*color, 1.0)
    return ramp


def _unit_luminance(graph, color, parent):
    """color / its luminance (floored at 0.01), as the Blackbody node, so the
    golden ramp and blackbody colours share one exposure. Returns (the
    output, rows for _grid)."""
    luminance = graph.node("ShaderNodeRGBToBW", "Luminance", parent)
    floor = graph.math("MAXIMUM", "Luminance Floor", parent, ((1, 0.01),))
    inverse = graph.math("DIVIDE", "1 / Luminance", parent, ((0, 1.0),))
    unit = graph.node("ShaderNodeVectorMath", "At Unit Luminance", parent, operation="SCALE")
    graph.link(color, luminance.inputs["Color"])
    graph.link(luminance.outputs["Val"], floor.inputs[0])
    graph.link(floor.outputs[0], inverse.inputs[1])
    graph.link(color, unit.inputs[0])
    graph.link(inverse.outputs[0], unit.inputs["Scale"])
    return unit.outputs[0], (luminance, floor, inverse, unit)


def _blackbody(graph, temperature, parent, label="Blackbody"):
    """Blackbody colour, saturated like a camera's clipped highlights."""
    node = graph.node("ShaderNodeBlackbody", label, parent)
    saturation = graph.node("ShaderNodeHueSaturation", "Camera Saturation", parent)
    saturation.inputs["Saturation"].default_value = PHYSICAL_SATURATION
    graph.link(temperature, node.inputs["Temperature"])
    graph.link(node.outputs["Color"], saturation.inputs["Color"])
    return saturation.outputs["Color"], (node, saturation)


def _flame_finish(graph, color, strength, i, parent, out):
    """Blue Core (fresh fuel x strength), Flame Tint, x brightness. Returns
    the nodes as rows for _grid."""
    core_factor = graph.math("MULTIPLY", "Blue Core Amount", parent, clamp=True)
    core_mix = graph.mix("RGBA", "Blue Core", parent)
    _rgba(core_mix.inputs)[1].default_value = BLUE_CORE_COLOR
    tinted = graph.mix("RGBA", "Flame Tint", parent, blend="MULTIPLY", factor=1.0)
    emission = graph.node("ShaderNodeVectorMath", "Colour x Brightness", parent, operation="SCALE")
    graph.link(i["Fresh Fuel"], core_factor.inputs[0])
    graph.link(i["Blue Core"], core_factor.inputs[1])
    graph.link(core_factor.outputs[0], core_mix.inputs["Factor"])
    graph.link(color, _rgba(core_mix.inputs)[0])
    graph.link(_rgba(core_mix.outputs)[0], _rgba(tinted.inputs)[0])
    graph.link(i["Tint"], _rgba(tinted.inputs)[1])
    graph.link(_rgba(tinted.outputs)[0], emission.inputs[0])
    graph.link(strength, emission.inputs["Scale"])
    graph.link(emission.outputs[0], out.inputs["Emission"])
    return core_factor, core_mix, tinted, emission


def _stages(stages, x=0.0, y=0.0, gap=90.0):
    """Lay out (input node, rows) stages top to bottom: the stage's Group
    Input at the left, its rows of nodes to the right. Returns (right, bottom)."""
    right = x
    _hide_unused([group_input for group_input, _ in stages if group_input is not None])
    for group_input, rows in stages:
        width = group_input.width + gap if group_input is not None else 0.0
        if group_input is not None:
            _place(group_input, x, y)
        stage_right, bottom = _grid(rows, x + width, y)
        if group_input is not None:
            bottom = min(bottom, y - _node_height(group_input))
        right = max(right, stage_right)
        y = bottom - gap * 1.4
    return right, y


_FLAME_INPUTS = (
    ("Density", "NodeSocketFloat", 0.0),
    ("Fresh Fuel", "NodeSocketFloat", 0.0),
    ("Contrast", "NodeSocketFloat", -0.5),
    ("Full Glow", "NodeSocketFloat", 0.0),
    ("Brightness", "NodeSocketFloat", 1.0),
    ("Tint", "NodeSocketColor", (1.0, 1.0, 1.0, 1.0)),
    ("Blue Core", "NodeSocketFloat", 0.0),
)


def _temperature_fire_group():
    """Temperature look: soot emits by Kirchhoff with Wien radiance at 550 nm,
    exp(-c * 26160 K * (1/T - 1/Full Glow)) x density x Brightness; colour
    blackbody (camera shift and saturation) or the golden ramp."""
    def build(graph, group_in, out):
        law = graph.frame("Brightness: Wien's law", _FRAME_FIRE)
        i = group_in(law)
        floor = graph.math("MAXIMUM", "Temperature Floor", law, ((1, 300.0),))
        term = graph.math("DIVIDE", "26160 K / T", law, ((0, WIEN_KELVIN),))
        wien = graph.math("MULTIPLY_ADD", "x Contrast + Full Glow", law)
        radiance = graph.math("EXPONENT", "Radiance", law)
        soot = graph.math("MULTIPLY", "x Density", law)
        strength = graph.math("MULTIPLY", "x Brightness", law)
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

        colour = graph.frame("Colour: blackbody or ramp", _FRAME_COLOUR)
        c = group_in(colour)
        shift = graph.math("MULTIPLY_ADD", "Camera Colour Shift", colour)
        blackbody, blackbody_nodes = _blackbody(graph, shift.outputs[0], colour)
        golden, golden_nodes = _unit_luminance(graph, c["Ramp Colour"], colour)
        choice = graph.mix("RGBA", "Blackbody / Ramp", colour)
        graph.link(c["Temperature"], shift.inputs[0])
        graph.link(c["Colour Scale"], shift.inputs[1])
        graph.link(c["Colour Offset"], shift.inputs[2])
        graph.link(c["Golden"], choice.inputs["Factor"])
        graph.link(blackbody, _rgba(choice.inputs)[0])
        graph.link(golden, _rgba(choice.inputs)[1])

        finish = graph.frame("Blue Core, Tint, Brightness", _FRAME_CORE)
        f = group_in(finish)
        finish_nodes = _flame_finish(graph, _rgba(choice.outputs)[0], strength.outputs[0], f, finish, out)
        right, bottom = _stages((
            (i[0].node, [[floor, term, wien, radiance, soot, strength]]),
            (c[0].node, [[shift, *blackbody_nodes, None, choice], [None, *golden_nodes]]),
            (f[0].node, [[*finish_nodes]]),
        ))
        _place(out, right + 120, 0.0)

    return _shader_group(
        "Fumaris Temperature Fire",
        (("Temperature", "NodeSocketFloat", 300.0), *_FLAME_INPUTS,
         ("Colour Scale", "NodeSocketFloat", PHYSICAL_COLOR_SHIFT),
         ("Colour Offset", "NodeSocketFloat", 0.0),
         ("Golden", "NodeSocketFloat", 0.0),
         ("Ramp Colour", "NodeSocketColor", (1.0, 0.32, 0.02, 1.0))),
        (("Emission", "NodeSocketColor"),),
        build)


def _pyro_fire_group():
    """Pyro look: Wien's brightness law on the fire light, the hotter of the
    baked, blurred heat and part of the gas's own heat (Local Heat), so gas
    near fire glows and billows glow round and soft instead of in flat
    patches. Gas glows by volume (Thin Gas x presence), not by density, so each
    billow is brightest through its middle; the Soot Mask fades it out of smoke
    denser than Thin Gas (none above 1.5 x), so dense billow tops read as soot
    and the fire shows between them: the cauliflower look of Houdini's pyro
    shading. Golden ramp or blackbody."""
    def build(graph, group_in, out):
        law = graph.frame("Brightness: Wien's law on the baked heat", _FRAME_FIRE)
        i = group_in(law)
        kelvin = graph.math("ADD", "Heat (K)", law, ((1, 293.15),))
        # The gas's own heat above ambient counts at Local Heat: 293 K + w (T - 293 K).
        local = graph.math("MULTIPLY_ADD", "Local Heat", law)
        hotter = graph.math("MAXIMUM", "Glow Temperature", law)
        term = graph.math("DIVIDE", "26160 K / T", law, ((0, WIEN_KELVIN),))
        wien = graph.math("MULTIPLY_ADD", "x Contrast + Full Glow", law)
        radiance = graph.math("EXPONENT", "Radiance", law)
        graph.link(i["Heat"], kelvin.inputs[0])
        graph.link(kelvin.outputs[0], hotter.inputs[0])
        graph.link(i["Temperature"], local.inputs[0])
        graph.link(i["Local Heat"], local.inputs[1])
        graph.link(i["Local Offset"], local.inputs[2])
        graph.link(local.outputs[0], hotter.inputs[1])
        graph.link(hotter.outputs[0], term.inputs[1])
        graph.link(term.outputs[0], wien.inputs[0])
        graph.link(i["Contrast"], wien.inputs[1])
        graph.link(i["Full Glow"], wien.inputs[2])
        graph.link(wien.outputs[0], radiance.inputs[0])
        # Soft cap (Houdini's pyro fire ramp levels off): R x (1 + k) / (1 + k R),
        # linear for dim gas, at most (1 + k) / k times Full Glow brightness.
        capped_num = graph.math("MULTIPLY", "R (1 + k)", law)
        capped_den = graph.math("MULTIPLY_ADD", "1 + k R", law, ((2, 1.0),))
        capped = graph.math("DIVIDE", "Soft Cap", law)
        graph.link(radiance.outputs[0], capped_den.inputs[0])
        graph.link(i["Cap"], capped_den.inputs[1])
        graph.link(radiance.outputs[0], capped_num.inputs[0])
        graph.link(i["Cap Gain"], capped_num.inputs[1])
        graph.link(capped_num.outputs[0], capped.inputs[0])
        graph.link(capped_den.outputs[0], capped.inputs[1])

        thin = graph.frame("Thin Gas: glows by volume, dense smoke is soot", _FRAME_THIN)
        t = group_in(thin)
        dens = graph.math("MAXIMUM", "Density", thin, ((1, 0.0),))
        # Gas glows by volume, not by its density: each billow glows brightest
        # through its middle, as fire seen through hot gas does. Presence keeps
        # clear air dark; the Soot Mask turns dense smoke to soot.
        presence = graph.node("ShaderNodeMapRange", "Presence", thin, interpolation_type="SMOOTHSTEP")
        mask = graph.node("ShaderNodeMapRange", "Soot Mask", thin, interpolation_type="SMOOTHSTEP")
        mask.inputs["To Min"].default_value = 1.0
        mask.inputs["To Max"].default_value = 0.0
        glowing = graph.math("MULTIPLY", "Glowing", thin)
        amount = graph.math("MULTIPLY", "x Thin Gas", thin)
        source = graph.math("MULTIPLY", "Radiance x Glowing", thin)
        strength = graph.math("MULTIPLY", "x Brightness", thin)
        graph.link(t["Density"], dens.inputs[0])
        graph.link(dens.outputs[0], presence.inputs["Value"])
        graph.link(t["Presence To"], presence.inputs["From Max"])
        graph.link(dens.outputs[0], mask.inputs["Value"])
        graph.link(t["Soot From"], mask.inputs["From Min"])
        graph.link(t["Soot To"], mask.inputs["From Max"])
        graph.link(_socket(presence.outputs, "Result"), glowing.inputs[0])
        graph.link(_socket(mask.outputs, "Result"), glowing.inputs[1])
        graph.link(glowing.outputs[0], amount.inputs[0])
        graph.link(t["Thin Gas"], amount.inputs[1])
        graph.link(capped.outputs[0], source.inputs[0])
        graph.link(amount.outputs[0], source.inputs[1])
        graph.link(source.outputs[0], strength.inputs[0])
        graph.link(t["Brightness"], strength.inputs[1])

        colour = graph.frame("Colour: ramp or blackbody", _FRAME_COLOUR)
        c = group_in(colour)
        golden, golden_nodes = _unit_luminance(graph, c["Ramp Colour"], colour)
        scale = graph.math("MULTIPLY", "Colour Temperature", colour)
        blackbody, blackbody_nodes = _blackbody(graph, scale.outputs[0], colour)
        choice = graph.mix("RGBA", "Ramp / Blackbody", colour)
        graph.link(hotter.outputs[0], scale.inputs[0])
        graph.link(c["Blackbody Scale"], scale.inputs[1])
        graph.link(c["Blackbody"], choice.inputs["Factor"])
        graph.link(golden, _rgba(choice.inputs)[0])
        graph.link(blackbody, _rgba(choice.inputs)[1])

        finish = graph.frame("Blue Core, Tint, Brightness", _FRAME_CORE)
        f = group_in(finish)
        finish_nodes = _flame_finish(graph, _rgba(choice.outputs)[0], strength.outputs[0], f, finish, out)
        right, _ = _stages((
            (i[0].node, [[kelvin, hotter, term, wien, radiance, capped_num, capped], [local, None, None, None, None,
                                                                                      capped_den]]),
            (t[0].node, [[dens, presence, glowing, amount, source, strength], [None, mask]]),
            (c[0].node, [[None, *golden_nodes, choice], [scale, *blackbody_nodes]]),
            (f[0].node, [[*finish_nodes]]),
        ))
        _place(out, right + 120, 0.0)

    return _shader_group(
        "Fumaris Pyro Fire",
        (("Heat", "NodeSocketFloat", 0.0), ("Temperature", "NodeSocketFloat", 300.0),
         ("Density", "NodeSocketFloat", 0.0), *_FLAME_INPUTS[1:],
         ("Thin Gas", "NodeSocketFloat", 3.0), ("Presence To", "NodeSocketFloat", 0.6),
         ("Soot From", "NodeSocketFloat", 1.5), ("Soot To", "NodeSocketFloat", 4.5),
         ("Local Heat", "NodeSocketFloat", 1.0), ("Local Offset", "NodeSocketFloat", 0.0),
         ("Cap", "NodeSocketFloat", 0.0), ("Cap Gain", "NodeSocketFloat", 1.0),
         ("Blackbody", "NodeSocketFloat", 0.0),
         ("Blackbody Scale", "NodeSocketFloat", PHYSICAL_COLOR_SHIFT),
         ("Ramp Colour", "NodeSocketColor", (1.0, 0.32, 0.02, 1.0))),
        (("Emission", "NodeSocketColor"),),
        build)


def _fire_group():
    """Both fire looks and the switch between them (Pyro Look), from the Look
    Controls' Wien terms and colour values; the Pyro look's fixed parts (Local
    Heat, the Thin Gas masks) are set here."""
    def build(graph, group_in, out):
        temperature = graph.frame("Temperature look", _FRAME_FIRE)
        a = group_in(temperature)
        temperature_fire = graph.node("ShaderNodeGroup", "Temperature Look", temperature, NODE_TEMPERATURE_FIRE)
        temperature_fire.node_tree = _temperature_fire_group()
        temperature_fire.width = 220
        for socket in ("Temperature", "Density", "Fresh Fuel", "Contrast", "Full Glow", "Brightness", "Blue Core",
                       "Colour Scale", "Colour Offset", "Ramp Colour"):
            graph.link(a[socket], temperature_fire.inputs[socket])
        graph.link(a["Flame Tint"], temperature_fire.inputs["Tint"])
        graph.link(a["Color Ramp"], temperature_fire.inputs["Golden"])

        pyro = graph.frame("Pyro look", _FRAME_FIRE)
        b = group_in(pyro)
        pyro_fire = graph.node("ShaderNodeGroup", "Pyro Look", pyro, NODE_PYRO_FIRE)
        pyro_fire.node_tree = _pyro_fire_group()
        pyro_fire.width = 220
        pyro_fire.inputs["Local Heat"].default_value = PYRO_LOCAL_HEAT
        pyro_fire.inputs["Local Offset"].default_value = 293.15 * (1.0 - PYRO_LOCAL_HEAT)
        for socket in ("Heat", "Temperature", "Density", "Fresh Fuel", "Contrast", "Full Glow", "Brightness",
                       "Blue Core", "Thin Gas", "Blackbody Scale", "Ramp Colour"):
            graph.link(b[socket], pyro_fire.inputs[socket])
        graph.link(b["Flame Tint"], pyro_fire.inputs["Tint"])
        graph.link(b["Soft Cap"], pyro_fire.inputs["Cap"])
        cap_gain = graph.math("ADD", "1 + Soft Cap", pyro, ((0, 1.0),))
        graph.link(b["Soft Cap"], cap_gain.inputs[1])
        graph.link(cap_gain.outputs[0], pyro_fire.inputs["Cap Gain"])
        blackbody = graph.math("SUBTRACT", "Blackbody = not Ramp", pyro, ((0, 1.0),))
        graph.link(b["Color Ramp"], blackbody.inputs[1])
        graph.link(blackbody.outputs[0], pyro_fire.inputs["Blackbody"])
        scaled = []
        for socket, scale in (("Presence To", PYRO_PRESENCE), ("Soot From", PYRO_SOOT_MASK[0]),
                              ("Soot To", PYRO_SOOT_MASK[1])):
            node = graph.math("MULTIPLY", "Thin Gas x %.2f (%s)" % (scale, socket), pyro, ((1, scale),))
            graph.link(b["Thin Gas"], node.inputs[0])
            graph.link(node.outputs[0], pyro_fire.inputs[socket])
            scaled.append(node)

        look = graph.mix("RGBA", "Look: Temperature / Pyro", None, factor=0.0, name=NODE_LOOK_MIX)
        switch = group_in()
        graph.link(switch["Pyro Look"], look.inputs["Factor"])
        graph.link(temperature_fire.outputs["Emission"], _rgba(look.inputs)[0])
        graph.link(pyro_fire.outputs["Emission"], _rgba(look.inputs)[1])
        graph.link(_rgba(look.outputs)[0], out.inputs["Emission"])

        _, bottom = _stages(((a[0].node, [[temperature_fire]]),))
        right, _ = _stages(((b[0].node, [[None, pyro_fire]]),), y=bottom)
        _place(pyro_fire, _node_x(pyro_fire) + 260.0, _node_y(pyro_fire))
        right += 260.0
        # The fixed helpers, collapsed, level with the sockets they feed.
        for node, socket in ((cap_gain, "Cap Gain"), (blackbody, "Blackbody"), *zip(scaled, (
                "Presence To", "Soot From", "Soot To"))):
            node.hide = True
            node.width = 170
            _place(node, _node_x(pyro_fire) - 230.0, _socket_y(pyro_fire, pyro_fire.inputs[socket]) + 13.0)
        _place(switch[0].node, right + 80, 0.0)
        _place(look, right + 80 + switch[0].node.width + 60, 0.0)
        _place(out, look.location.x + look.width + 80, 0.0)

    return _shader_group(
        "Fumaris Fire",
        (("Temperature", "NodeSocketFloat", 300.0), ("Heat", "NodeSocketFloat", 0.0),
         ("Density", "NodeSocketFloat", 0.0), ("Fresh Fuel", "NodeSocketFloat", 0.0),
         ("Ramp Colour", "NodeSocketColor", (1.0, 0.32, 0.02, 1.0)), ("Thin Gas", "NodeSocketFloat", 3.0),
         ("Pyro Look", "NodeSocketFloat", 0.0), ("Contrast", "NodeSocketFloat", -0.5),
         ("Full Glow", "NodeSocketFloat", 0.0), ("Brightness", "NodeSocketFloat", 1.0),
         ("Flame Tint", "NodeSocketColor", (1.0, 1.0, 1.0, 1.0)), ("Blue Core", "NodeSocketFloat", 0.0),
         ("Colour Scale", "NodeSocketFloat", PHYSICAL_COLOR_SHIFT), ("Colour Offset", "NodeSocketFloat", 0.0),
         ("Color Ramp", "NodeSocketFloat", 0.0), ("Blackbody Scale", "NodeSocketFloat", PHYSICAL_COLOR_SHIFT),
         ("Soft Cap", "NodeSocketFloat", 0.0)),
        (("Emission", "NodeSocketColor"),),
        build)


def _scatter_glow_group():
    """Light from the fire scattered by smoke: the baked glow (blurred density
    x Wien radiance at 1500 K; blended toward glow_wide by Glow Spread, dimmed
    by the smoke toward the fire, exp(-shadowing x glow_depth)) minus this
    voxel's own source, x density, coloured by the gas at Full Glow and the
    smoke colour."""
    def build(graph, group_in, out):
        light = graph.frame("Glow from elsewhere: spread, shadowed", _FRAME_GLOW)
        i = group_in(light)
        spread = graph.mix("FLOAT", "Glow Spread", light)
        shadow_exponent = graph.math("MULTIPLY", "x Shadowing", light)
        reaching = graph.math("EXPONENT", "Light Reaching", light)
        shadowed = graph.math("MULTIPLY", "Shadowed Glow", light)
        graph.link(i["Glow Spread"], spread.inputs["Factor"])
        graph.link(i["Glow"], _float_in(spread)[0])
        graph.link(i["Glow Wide"], _float_in(spread)[1])
        graph.link(i["Glow Depth"], shadow_exponent.inputs[0])
        graph.link(i["Glow Shadowing"], shadow_exponent.inputs[1])
        graph.link(shadow_exponent.outputs[0], reaching.inputs[0])
        graph.link(_float_out(spread), shadowed.inputs[0])
        graph.link(reaching.outputs[0], shadowed.inputs[1])

        own_frame = graph.frame("Minus this voxel's own fire (Wien at 1500 K)", _FRAME_GLOW)
        o = group_in(own_frame)
        floor = graph.math("MAXIMUM", "Temperature Floor", own_frame, ((1, 300.0),))
        term = graph.math("DIVIDE", "26160 K / T", own_frame, ((0, WIEN_KELVIN),))
        relative = graph.math("MULTIPLY_ADD", "Relative to 1500 K", own_frame,
                              ((1, -1.0), (2, WIEN_KELVIN / GLOW_REFERENCE_K)))
        local = graph.math("EXPONENT", "Local Radiance", own_frame)
        own = graph.math("MULTIPLY", "Own Source", own_frame)
        # Warm Gas glow source: the glow is not on the Wien scale, nothing to subtract.
        own_weight = graph.math("MULTIPLY", "x Own Source Weight", own_frame)
        excess = graph.math("SUBTRACT", "Minus Own Source", own_frame)
        positive = graph.math("MAXIMUM", "Positive", own_frame, ((1, 0.0),))
        scattered = graph.math("MULTIPLY", "x Density", own_frame)
        strength = graph.math("MULTIPLY", "x Brightness", own_frame)
        graph.link(o["Temperature"], floor.inputs[0])
        graph.link(floor.outputs[0], term.inputs[1])
        graph.link(term.outputs[0], relative.inputs[0])
        graph.link(relative.outputs[0], local.inputs[0])
        graph.link(local.outputs[0], own.inputs[0])
        graph.link(o["Density"], own.inputs[1])
        graph.link(shadowed.outputs[0], excess.inputs[0])
        graph.link(own.outputs[0], own_weight.inputs[0])
        graph.link(o["Own Source"], own_weight.inputs[1])
        graph.link(own_weight.outputs[0], excess.inputs[1])
        graph.link(excess.outputs[0], positive.inputs[0])
        graph.link(positive.outputs[0], scattered.inputs[0])
        graph.link(o["Density"], scattered.inputs[1])
        graph.link(scattered.outputs[0], strength.inputs[0])
        graph.link(o["Brightness"], strength.inputs[1])

        colour = graph.frame("Colour: the fire's light x smoke colour", _FRAME_COLOUR)
        c = group_in(colour)
        blackbody, blackbody_nodes = _blackbody(graph, c["Light Temperature"], colour, "Fire Light Colour")
        # Color: Ramp: the fire light takes the ramp's colour at this voxel's
        # temperature (as Houdini's scatter colour ramp), at unit luminance.
        ramp, ramp_nodes = _unit_luminance(graph, c["Ramp Colour"], colour)
        light_colour = graph.mix("RGBA", "Blackbody / Ramp", colour)
        graph.link(c["Use Ramp"], light_colour.inputs["Factor"])
        graph.link(blackbody, _rgba(light_colour.inputs)[0])
        graph.link(ramp, _rgba(light_colour.inputs)[1])
        albedo = graph.mix("RGBA", "x Smoke Colour", colour, blend="MULTIPLY", factor=1.0)
        emission = graph.node("ShaderNodeVectorMath", "Colour x Brightness", None, operation="SCALE")
        graph.link(_rgba(light_colour.outputs)[0], _rgba(albedo.inputs)[0])
        graph.link(c["Smoke Colour"], _rgba(albedo.inputs)[1])
        graph.link(_rgba(albedo.outputs)[0], emission.inputs[0])
        graph.link(strength.outputs[0], emission.inputs["Scale"])
        graph.link(emission.outputs[0], out.inputs["Emission"])

        right, _ = _stages((
            (i[0].node, [[spread, shadowed], [shadow_exponent, reaching]]),
            (o[0].node, [[floor, term, relative, local, own, own_weight], [None, None, excess, positive, scattered,
                                                                           strength]]),
            (c[0].node, [[*blackbody_nodes, None, light_colour, albedo], [*ramp_nodes]]),
        ))
        _place(emission, right + 100, 0.0)
        _place(out, right + 100 + emission.width + 80, 0.0)

    return _shader_group(
        "Fumaris Scatter Glow",
        (("Temperature", "NodeSocketFloat", 300.0), ("Density", "NodeSocketFloat", 0.0),
         ("Glow", "NodeSocketFloat", 0.0), ("Glow Wide", "NodeSocketFloat", 0.0),
         ("Glow Depth", "NodeSocketFloat", 0.0), ("Glow Spread", "NodeSocketFloat", 0.0),
         ("Glow Shadowing", "NodeSocketFloat", 0.0), ("Brightness", "NodeSocketFloat", 0.0),
         ("Own Source", "NodeSocketFloat", 1.0),
         ("Light Temperature", "NodeSocketFloat", GLOW_REFERENCE_K * PHYSICAL_COLOR_SHIFT),
         ("Use Ramp", "NodeSocketFloat", 0.0), ("Ramp Colour", "NodeSocketColor", (1.0, 0.32, 0.02, 1.0)),
         ("Smoke Colour", "NodeSocketColor", (0.6, 0.6, 0.6, 1.0))),
        (("Emission", "NodeSocketColor"),),
        build)


def _fresh_fuel_group():
    """Blue Core mask: fuel still burning near ignition (fresh flame)."""
    def build(graph, group_in, out):
        i = group_in()
        presence = graph.math("MULTIPLY", "Fuel Presence", None, ((1, 4.0),), clamp=True)
        heat = graph.node("ShaderNodeMapRange", "Flame Temperature Range", None,
                          data_type="FLOAT", interpolation_type="SMOOTHSTEP")
        near = graph.math("SUBTRACT", "Near Ignition", None, ((0, 1.0),))
        fresh = graph.math("MULTIPLY", "Fresh Fuel", None)
        graph.link(i["Fuel"], presence.inputs[0])
        graph.link(i["Temperature"], heat.inputs[0])
        graph.link(i["Flame Start"], heat.inputs[1])
        graph.link(i["Flame Full"], heat.inputs[2])
        graph.link(_socket(heat.outputs, "Result"), near.inputs[1])
        graph.link(presence.outputs[0], fresh.inputs[0])
        graph.link(near.outputs[0], fresh.inputs[1])
        graph.link(fresh.outputs[0], out.inputs["Fresh Fuel"])
        right, _ = _stages(((i[0].node, [[presence, None, fresh], [heat, near]]),))
        _place(out, right + 80, 0.0)

    return _shader_group(
        "Fumaris Fresh Fuel",
        (("Fuel", "NodeSocketFloat", 0.0), ("Temperature", "NodeSocketFloat", 300.0),
         ("Flame Start", "NodeSocketFloat", 800.0), ("Flame Full", "NodeSocketFloat", 3000.0)),
        (("Fresh Fuel", "NodeSocketFloat"),),
        build)


def _smoke_color_group():
    """Smoke colour; with Hot Soot (the Pyro look) hot soot turns dark."""
    def build(graph, group_in, out):
        i = group_in()
        hot = graph.node("ShaderNodeMapRange", "Hot (500-900 K)", None, interpolation_type="SMOOTHSTEP")
        hot.inputs["From Min"].default_value = 500.0
        hot.inputs["From Max"].default_value = 900.0
        amount = graph.math("MULTIPLY", "x Hot Soot", None)
        soot = graph.mix("RGBA", "Soot Colour", None)
        _rgba(soot.inputs)[1].default_value = (PYRO_HOT_SOOT, PYRO_HOT_SOOT, PYRO_HOT_SOOT, 1.0)
        graph.link(i["Temperature"], hot.inputs["Value"])
        graph.link(_socket(hot.outputs, "Result"), amount.inputs[0])
        graph.link(i["Hot Soot"], amount.inputs[1])
        graph.link(amount.outputs[0], soot.inputs["Factor"])
        graph.link(i["Smoke Colour"], _rgba(soot.inputs)[0])
        graph.link(_rgba(soot.outputs)[0], out.inputs["Colour"])
        right, _ = _stages(((i[0].node, [[hot, amount, soot]]),))
        _place(out, right + 80, 0.0)

    return _shader_group(
        "Fumaris Smoke Colour",
        (("Temperature", "NodeSocketFloat", 300.0), ("Smoke Colour", "NodeSocketColor", (0.6, 0.6, 0.6, 1.0)),
         ("Hot Soot", "NodeSocketFloat", 0.0)),
        (("Colour", "NodeSocketColor"),),
        build)


def _thin_gas_group():
    """Thin Gas (Pyro): the fixed value, or Auto, Thin Gas Ratio x the frame's
    density reference (when the bake has one); and the Soot Mask's range."""
    def build(graph, group_in, out):
        i = group_in()
        ratio = graph.math("MULTIPLY", "x Thin Gas Ratio", None)
        has_reference = graph.math("GREATER_THAN", "Baked Reference?", None, ((1, 0.0),))
        use_auto = graph.math("MULTIPLY", "x Auto", None)
        thin = graph.mix("FLOAT", "Fixed / Auto", None)
        graph.link(i["Density Reference"], ratio.inputs[0])
        graph.link(i["Thin Gas Ratio"], ratio.inputs[1])
        graph.link(i["Density Reference"], has_reference.inputs[0])
        graph.link(has_reference.outputs[0], use_auto.inputs[0])
        graph.link(i["Thin Gas Auto"], use_auto.inputs[1])
        graph.link(use_auto.outputs[0], thin.inputs["Factor"])
        graph.link(i["Thin Gas"], _float_in(thin)[0])
        graph.link(ratio.outputs[0], _float_in(thin)[1])
        graph.link(_float_out(thin), out.inputs["Thin Gas"])
        scaled = []
        for socket, scale in (("Soot From", PYRO_SOOT_MASK[0]), ("Soot To", PYRO_SOOT_MASK[1])):
            node = graph.math("MULTIPLY", "x %.2f" % scale, None, ((1, scale),))
            graph.link(_float_out(thin), node.inputs[0])
            graph.link(node.outputs[0], out.inputs[socket])
            scaled.append(node)
        right, _ = _stages(((i[0].node, [[has_reference, use_auto, None, None], [ratio, None, thin, scaled[0]],
                                         [None, None, None, scaled[1]]]),))
        _place(out, right + 80, 0.0)

    return _shader_group(
        "Fumaris Thin Gas",
        (("Density Reference", "NodeSocketFloat", 0.0), ("Thin Gas", "NodeSocketFloat", 3.0),
         ("Thin Gas Ratio", "NodeSocketFloat", 0.12), ("Thin Gas Auto", "NodeSocketFloat", 1.0)),
        (("Thin Gas", "NodeSocketFloat"), ("Soot From", "NodeSocketFloat"), ("Soot To", "NodeSocketFloat")),
        build)


def _hot_density_group():
    """Hot Smoke Density: density x (1 + (ratio - 1) x the temperature's place
    from Hot Smoke From to To, clamped). Extra = ratio - 1; 0 (off) folds the
    whole group out of the compiled shader."""
    def build(graph, group_in, out):
        i = group_in()
        hot = graph.node("ShaderNodeMapRange", "Hot (From-To K)", None)
        extra = graph.math("MULTIPLY", "x Extra", None)
        gain = graph.math("ADD", "1 + ", None, ((1, 1.0),))
        density = graph.math("MULTIPLY", "Density x", None)
        graph.link(i["Temperature"], hot.inputs["Value"])
        graph.link(i["From"], hot.inputs["From Min"])
        graph.link(i["To"], hot.inputs["From Max"])
        graph.link(_socket(hot.outputs, "Result"), extra.inputs[0])
        graph.link(i["Extra"], extra.inputs[1])
        graph.link(extra.outputs[0], gain.inputs[0])
        graph.link(i["Density"], density.inputs[0])
        graph.link(gain.outputs[0], density.inputs[1])
        graph.link(density.outputs[0], out.inputs["Density"])
        right, _ = _stages(((i[0].node, [[None, None, None, density], [hot, extra, gain]]),))
        _place(out, right + 80, 0.0)

    return _shader_group(
        "Fumaris Hot Smoke Density",
        (("Density", "NodeSocketFloat", 0.0), ("Temperature", "NodeSocketFloat", 300.0),
         ("Extra", "NodeSocketFloat", 0.0), ("From", "NodeSocketFloat", 500.0), ("To", "NodeSocketFloat", 1500.0)),
        (("Density", "NodeSocketFloat"),),
        build)


def _controls_group():
    """The Look Controls: the Look panel's values (inputs, in panels) and
    what the graph derives from them (outputs, one per link, see
    _CONTROL_OUTPUTS). The derivations repeat what Fumaris computed in Python
    before graph 19, in the same order."""
    def build(graph, group_in, out):
        o = out.inputs
        stages = []

        def stage(label, color):
            frame = graph.frame(label, color)
            return frame, group_in(frame)

        def times(label, frame, a, b, operation="MULTIPLY"):
            node = graph.math(operation, label, frame)
            for socket, value in zip(node.inputs, (a, b)):
                if isinstance(value, (int, float)):
                    socket.default_value = value
                else:
                    graph.link(value, socket)
            return node

        def flame_on(frame, i):
            """Pyro x Flame: the look in use (no fire: no Pyro soot either)."""
            return times("Pyro x Flame", frame, i["Pyro Look"], i["Flame"])

        # Smoke
        frame, i = stage("Smoke", _FRAME_SMOKE)
        pyro = flame_on(frame, i)
        extra = times("Hot Smoke Density - 1", frame, i["Hot Smoke Density"], 1.0, "SUBTRACT")
        for name in ("Smoke Color", "Per-Emitter Color", "Hot Smoke From", "Hot Smoke To", "Smoke Density",
                     "Anisotropy"):
            graph.link(i[name], o[name])
        hot_soot = times("x Hot Soot", frame, pyro.outputs[0], i["Hot Soot"])
        graph.link(hot_soot.outputs[0], o["Hot Soot"])
        graph.link(extra.outputs[0], o["Hot Smoke Extra"])
        stages.append((i[0].node, [[pyro, hot_soot], [extra]]))

        # Fire inputs: Blue Core range, Colour Ramp range, Thin Gas
        frame, i = stage("Colour Ramp range: 0.45-1 x Full Glow / Blackbody Scale", _FRAME_COLOUR)
        graph.link(i["Start Temperature"], o["Blue Core Start"])
        graph.link(i["Full Temperature"], o["Blue Core Full"])
        low = times("0.45 x Full Glow", frame, PYRO_COLOR_RANGE[0], i["Full Glow"])
        ramp_from = times("/ Blackbody Scale", frame, low.outputs[0], i["Blackbody Scale"], "DIVIDE")
        ramp_to = times("Full Glow / Blackbody Scale", frame, i["Full Glow"], i["Blackbody Scale"], "DIVIDE")
        graph.link(ramp_from.outputs[0], o["Ramp From"])
        graph.link(ramp_to.outputs[0], o["Ramp To"])
        for name in ("Thin Gas", "Thin Gas Ratio", "Thin Gas Auto"):
            graph.link(i[name], o[name])
        stages.append((i[0].node, [[low, ramp_from], [None, ramp_to]]))

        # Fire: Wien's law terms and colour
        frame, i = stage("Fire: Wien's law terms, brightness x density, colour shift", _FRAME_FIRE)
        pyro = flame_on(frame, i)
        contrast = times("-Contrast", frame, i["Contrast"], -1.0)
        full_glow = times("Contrast x 26160 K", frame, i["Contrast"], WIEN_KELVIN)
        full_glow_term = times("/ Full Glow", frame, full_glow.outputs[0], i["Full Glow"], "DIVIDE")
        brightness = times("Brightness x Smoke Density", frame, i["Brightness"], i["Smoke Density"])
        brightness_on = times("x Flame", frame, brightness.outputs[0], i["Flame"])
        shift = times("Blackbody Scale x Camera Shift", frame, PHYSICAL_COLOR_SHIFT, i["Blackbody Scale"])
        colour_scale = times("x Color Spread", frame, shift.outputs[0], i["Color Spread"])
        offset = times("x Full Glow", frame, shift.outputs[0], i["Full Glow"])
        unspread = times("1 - Color Spread", frame, 1.0, i["Color Spread"], "SUBTRACT")
        colour_offset = times("x (1 - Color Spread)", frame, offset.outputs[0], unspread.outputs[0])
        for node, name in ((pyro, "Pyro Look"), (contrast, "Wien Contrast"), (full_glow_term, "Wien Full Glow"),
                           (brightness_on, "Fire Brightness"), (colour_scale, "Colour Scale"),
                           (colour_offset, "Colour Offset"), (shift, "Blackbody Scale")):
            graph.link(node.outputs[0], o[name])
        for name in ("Flame Tint", "Blue Core", "Color Ramp"):
            graph.link(i[name], o[name])
        stages.append((i[0].node, [[pyro, None], [contrast, None], [full_glow, full_glow_term],
                                   [brightness, brightness_on], [shift, colour_scale], [unspread, None],
                                   [offset, colour_offset]]))

        # Scatter Glow: the flame's brightness scale (scatter_glow_scale), or
        # the Warm Gas glow's (GLOW_HEAT_SCALE)
        frame, i = stage("Scatter Glow: on the flame's brightness scale", _FRAME_GLOW)
        inverse = times("1 / Full Glow", frame, 1.0, i["Full Glow"], "DIVIDE")
        relative = times("- 1 / 1500 K", frame, inverse.outputs[0], 1.0 / GLOW_REFERENCE_K, "SUBTRACT")
        exponent = graph.math("MULTIPLY_ADD", "x Contrast + offset", frame, ((2, _SCATTER_SCALE_OFFSET),))
        graph.link(i["Contrast"], exponent.inputs[0])
        graph.link(relative.outputs[0], exponent.inputs[1])
        wien = times("x 26160 K", frame, exponent.outputs[0], WIEN_KELVIN)
        scale = times("Scale", frame, wien.outputs[0], 0.0, "EXPONENT")
        source = graph.mix("FLOAT", "Fire Brightness / Warm Gas", frame)
        graph.link(i["Glow Source Warm Gas"], source.inputs["Factor"])
        graph.link(scale.outputs[0], _float_in(source)[0])
        _float_in(source)[1].default_value = GLOW_HEAT_SCALE
        glow = times("Scatter Glow x Brightness", frame, i["Scatter Glow"], i["Brightness"])
        glow_density = times("x Smoke Density", frame, glow.outputs[0], i["Smoke Density"])
        glow_scaled = times("x Scale", frame, glow_density.outputs[0], _float_out(source))
        glow_on = times("x Flame", frame, glow_scaled.outputs[0], i["Flame"])
        own = times("1 - Warm Gas", frame, 1.0, i["Glow Source Warm Gas"], "SUBTRACT")
        shadowing = times("-Glow Shadowing", frame, i["Glow Shadowing"], -1.0)
        light = times("Full Glow x Camera Shift", frame, i["Full Glow"], PHYSICAL_COLOR_SHIFT)
        light_scaled = times("x Blackbody Scale", frame, light.outputs[0], i["Blackbody Scale"])
        pyro = flame_on(frame, i)
        mask = times("1 - Pyro: Soot Mask", frame, 1.0, pyro.outputs[0], "SUBTRACT")
        for node, name in ((shadowing, "Glow Shadowing"), (glow_on, "Glow Brightness"), (own, "Own Source"),
                           (light_scaled, "Light Temperature"), (mask, "Scatter Soot Mask")):
            graph.link(node.outputs[0], o[name])
        graph.link(i["Glow Spread"], o["Glow Spread"])
        graph.link(i["Color Ramp"], o["Glow Color Ramp"])
        graph.link(i["Smoke Color"], o["Glow Smoke Color"])
        stages.append((i[0].node, [[inverse, relative, exponent, wien, scale, source],
                                   [glow, glow_density, glow_scaled, glow_on], [own, shadowing],
                                   [light, light_scaled], [pyro, mask]]))
        right, bottom = _stages(stages)
        _hide_unused([out])
        # Level with the middle of the stages, far enough right that the
        # lines to it clear the stages' nodes.
        _place(out, right + 420.0, bottom * 0.5 + _node_height(out) * 0.5)

    kinds = {"FACTOR": "FACTOR", "KELVIN": "COLOR_TEMPERATURE"}
    panels = tuple((panel, [(name, _CONTROL_SOCKETS[kind], default, description, *bounds, *([None] * (2 - len(bounds))),
                             kinds.get(kind)) for name, kind, default, description, *bounds in controls])
                   for panel, controls in _CONTROLS)
    return _shader_group(
        "Fumaris Look Controls", (),
        tuple((name, "NodeSocketColor" if kind == "RGBA" else "NodeSocketFloat") for name, kind in _CONTROL_OUTPUTS),
        build, panels)


def _route_bus(graph, routes, gutter, lane_gap=14.0, drop_gap=14.0, margin=36.0):
    """Wire one node's outputs to far inputs as a bus of square lines, with
    no crossings: routes are (output, input socket, corridor top y) in
    output order; each runs right from the gutter along its own lane in the
    corridor (below the inputs' row of nodes), up the gap just left of its
    node, and right into its socket (three reroutes). Within a corridor, the
    nearer and higher sockets take the higher lanes and the drops farther
    left, so no line crosses another."""
    corridors = {}
    for output, socket, top in routes:
        corridors.setdefault(top, []).append((output, socket))
    for top, links in corridors.items():
        order = sorted(links, key=lambda link: (link[1].node.location_absolute.x,
                                                -_socket_y(link[1].node, link[1])))
        drops = {}
        for _, socket in order:
            drops.setdefault(socket.node.name, []).append(socket)
        for lane, (output, socket) in enumerate(order):
            node = socket.node
            later = len(drops[node.name]) - 1 - drops[node.name].index(socket)
            x = node.location_absolute.x - margin - drop_gap * later
            y = top - lane_gap * lane
            socket_y = _socket_y(node, socket)
            points = [(gutter, y), (x, y), (x, socket_y)]
            previous = output
            for point in points:
                reroute = graph.node("NodeReroute")
                _place(reroute, *point)
                graph.link(previous, reroute.inputs[0])
                previous = reroute.outputs[0]
            graph.link(previous, socket)


def _build_generated_material(material, blue_core=False):
    """Fumaris's volume material, left to right: the Look Controls (every
    Appearance value), then three rows of stages, each reading its own grids:
    Smoke (colour, Hot Smoke Density, density), Fire (Colour Ramp, Thin Gas,
    Blue Core, both looks) and Scatter Glow; the Principled Volume and the
    output on the right. The Look Controls feed the stages through a bus of
    square lines in the corridors under each row. One emission closure for
    Eevee. Sync writes the Look Controls' inputs."""
    tree = material.node_tree
    # A rebuild keeps the user's Colour Ramp and the Principled Volume's values
    # Fumaris does not set (Emission Strength and the like).
    kept_ramp = None
    old_ramp = tree.nodes.get(NODE_RAMP)
    if old_ramp is not None and old_ramp.type == "VALTORGB":
        kept_ramp = (old_ramp.color_ramp.interpolation, old_ramp.color_ramp.color_mode,
                     [(e.position, tuple(e.color)) for e in old_ramp.color_ramp.elements])
    kept_shader = {}
    old_shader = tree.nodes.get(NODE_VOLUME_SHADER)
    if old_shader is not None and old_shader.bl_idname == "ShaderNodeVolumePrincipled":
        kept_shader = {socket.identifier: (socket.default_value if isinstance(socket.default_value, (float, str))
                                           else tuple(socket.default_value))
                       for socket in old_shader.inputs
                       if not socket.is_linked and hasattr(socket, "default_value")
                       and socket.name not in ("Color", "Density", "Anisotropy")}
    tree.nodes.clear()
    graph = _Graph(tree)
    layout = material.get(GRID_LAYOUT_KEY, "separate")
    material[BUILT_GRID_LAYOUT_KEY] = layout

    def group(kind, label, name, parent, width=220):
        node = graph.node("ShaderNodeGroup", label, parent, name)
        node.node_tree = kind()
        node.width = width
        return node

    def attribute(grid, label, parent):
        node = graph.node("ShaderNodeAttribute", label, parent, attribute_name=grid)
        return node, _socket(node.outputs, "Factor", "Fac")

    def volume_info(parent):
        info = graph.node("ShaderNodeVolumeInfo", "", parent)
        floor = graph.math("MAXIMUM", "Density Floor", parent, ((1, 0.0),))
        graph.link(info.outputs["Density"], floor.inputs[0])
        return info, floor

    unpacked_reads = {}

    def pyro_attribute(grid, component, label, parent):
        """The packed grid's component, the older cache's own grid, or (both)
        their sum: one of the two is missing and reads 0. Returns (output,
        nodes in order). One packed read per frame."""
        if layout == "separate":
            node, output = attribute(grid, label, parent)
            return output, [node]
        if parent.name not in unpacked_reads:
            packed = graph.node("ShaderNodeAttribute", "Pyro grids, packed (baked)", parent,
                                attribute_name=PACKED_PYRO_GRID)
            unpacked = graph.node("ShaderNodeSeparateXYZ", "Heat, Glow, Smoke to the fire", parent)
            graph.link(packed.outputs["Vector"], unpacked.inputs[0])
            unpacked_reads[parent.name] = packed, unpacked
        packed, unpacked = unpacked_reads[parent.name]
        if layout == "packed":
            return unpacked.outputs[component], [packed, unpacked]
        older, older_output = attribute(grid, label + " (older bakes)", parent)
        both = graph.math("ADD", label, parent)
        graph.link(older_output, both.inputs[0])
        graph.link(unpacked.outputs[component], both.inputs[1])
        return both.outputs[0], [packed, unpacked, older, both]

    controls_frame = graph.frame("Look Controls: every value of the look (Fumaris syncs these)", _FRAME_CONTROLS)
    controls = group(_controls_group, "Look Controls", NODE_CONTROLS, controls_frame, width=300)
    c = controls.outputs

    # --- Smoke row: colour, Hot Smoke Density, density
    smoke_grids = graph.frame("Smoke: grids", _FRAME_INPUT)
    smoke_info, smoke_density = volume_info(smoke_grids)
    color_grid, color_grid_out = attribute("color", "Smoke colour (baked)", smoke_grids)
    smoke = graph.frame("Smoke", _FRAME_SMOKE)
    colour = group(_smoke_color_group, "Smoke Colour", NODE_SMOKE_COLOR, smoke)
    graph.link(smoke_info.outputs["Temperature"], colour.inputs["Temperature"])
    # Colour Mixing: the bake's "color" grid (the smoke's colour per voxel, white
    # where there is no smoke) multiplies the smoke colour. The factor is 0
    # unless the domain mixes colours, so a volume without the grid is unchanged.
    mixed = graph.mix("RGBA", "Colour Mixing", smoke, blend="MULTIPLY", factor=0.0, name=NODE_COLOR_MIX)
    graph.link(_rgba(colour.outputs)[0], _rgba(mixed.inputs)[0])
    # The grid's colour (the attribute's Color output; its Fac is one number,
    # which turned every colour grey in renders up to beta 3).
    graph.link(color_grid.outputs["Color"], _rgba(mixed.inputs)[1])
    hot = group(_hot_density_group, "Hot Smoke Density", NODE_HOT_DENSITY, smoke, width=200)
    graph.link(smoke_density.outputs[0], hot.inputs["Density"])
    graph.link(smoke_info.outputs["Temperature"], hot.inputs["Temperature"])
    gain = graph.math("MULTIPLY", "x Smoke Density", smoke, ((1, 2.0),), name=NODE_DENSITY_GAIN)
    graph.link(hot.outputs[0], gain.inputs[0])

    # --- Fire row: Blue Core, Colour Ramp, Thin Gas, the looks
    fire_grids = graph.frame("Fire: grids", _FRAME_INPUT)
    fire_info, fire_density = volume_info(fire_grids)
    temperature = fire_info.outputs["Temperature"]
    heat, heat_nodes = pyro_attribute("heat", "X", "Heat above ambient (baked)", fire_grids)
    reference_property = graph.node("ShaderNodeAttribute", "Density reference (keyed)", fire_grids,
                                    attribute_type="OBJECT", attribute_name=DENSITY_REFERENCE_PROPERTY)
    reference = _socket(reference_property.outputs, "Factor", "Fac")
    reference_nodes = [reference_property]
    if layout != "packed":
        # Older bakes: also the "density_ref" grid (volumes imported before
        # the property was keyed). Packed caches have no such grid.
        older_reference, older_output = attribute("density_ref", "Density reference (older bakes)", fire_grids)
        either = graph.math("MAXIMUM", "Density reference", fire_grids)
        graph.link(older_output, either.inputs[0])
        graph.link(reference, either.inputs[1])
        reference = either.outputs[0]
        reference_nodes = [older_reference, reference_property, either]
    fire_frame = graph.frame("Fire", _FRAME_FIRE)
    fresh = None
    core_nodes = []
    if blue_core:
        core_frame = graph.frame("Blue Core: fresh fuel", _FRAME_CORE)
        fuel, fuel_out = attribute("fuel", "Fuel", core_frame)
        fresh = group(_fresh_fuel_group, "Blue Core: Fresh Fuel", NODE_FRESH_FUEL, core_frame, width=200)
        graph.link(fuel_out, fresh.inputs["Fuel"])
        graph.link(temperature, fresh.inputs["Temperature"])
        core_nodes = [fuel, fresh]
    # Colour Ramp, shared by both looks and Scatter Glow (Color: Ramp), by
    # the gas temperature (as Houdini's fire colour ramp, and as the live
    # preview can follow; Pyro brightness keeps the baked heat).
    ramp_frame = graph.frame("Colour Ramp (Color: Ramp)", _FRAME_COLOUR)
    floor_t = graph.math("MAXIMUM", "Temperature Floor", ramp_frame, ((1, 300.0),))
    ramp_range = graph.node("ShaderNodeMapRange", "Ramp Range", ramp_frame, NODE_RAMP_RANGE)
    flame_ramp = _ramp(graph, "Flame Ramp", ramp_frame)
    flame_ramp.name = NODE_RAMP
    if kept_ramp is not None:
        interpolation, color_mode, elements = kept_ramp
        cr = flame_ramp.color_ramp
        cr.interpolation, cr.color_mode = interpolation, color_mode
        while len(cr.elements) > 1:
            cr.elements.remove(cr.elements[-1])
        cr.elements[0].position, cr.elements[0].color = elements[0]
        for position, color in elements[1:]:
            cr.elements.new(position).color = color
    graph.link(temperature, floor_t.inputs[0])
    graph.link(floor_t.outputs[0], ramp_range.inputs["Value"])
    graph.link(_socket(ramp_range.outputs, "Result"), flame_ramp.inputs["Fac"])
    thin_frame = graph.frame("Thin Gas (Pyro)", _FRAME_THIN)
    thin = group(_thin_gas_group, "Thin Gas", NODE_THIN_GAS, thin_frame, width=200)
    graph.link(reference, thin.inputs["Density Reference"])
    fire = group(_fire_group, "Fire: Temperature / Pyro look", NODE_FIRE, fire_frame, width=240)
    graph.link(temperature, fire.inputs["Temperature"])
    graph.link(heat, fire.inputs["Heat"])
    graph.link(fire_density.outputs[0], fire.inputs["Density"])
    if fresh is not None:
        graph.link(fresh.outputs["Fresh Fuel"], fire.inputs["Fresh Fuel"])
    graph.link(flame_ramp.outputs["Color"], fire.inputs["Ramp Colour"])
    graph.link(thin.outputs["Thin Gas"], fire.inputs["Thin Gas"])

    # --- Scatter Glow row
    glow_grids = graph.frame("Scatter Glow: grids", _FRAME_INPUT)
    glow_info, glow_density_floor = volume_info(glow_grids)
    glow, glow_nodes = pyro_attribute("glow", "Y", "Glow (baked)", glow_grids)
    glow_wide_node, glow_wide = attribute("glow_wide", "Glow, wide (baked)", glow_grids)
    glow_depth, depth_nodes = pyro_attribute("glow_depth", "Z", "Smoke to the fire (baked)", glow_grids)
    glow_frame = graph.frame("Scatter Glow", _FRAME_GLOW)
    scatter = group(_scatter_glow_group, "Scatter Glow", NODE_SCATTER_GLOW, glow_frame)
    graph.link(glow_info.outputs["Temperature"], scatter.inputs["Temperature"])
    graph.link(glow_density_floor.outputs[0], scatter.inputs["Density"])
    graph.link(glow, scatter.inputs["Glow"])
    graph.link(glow_wide, scatter.inputs["Glow Wide"])
    graph.link(glow_depth, scatter.inputs["Glow Depth"])
    graph.link(flame_ramp.outputs["Color"], scatter.inputs["Ramp Colour"])
    # Pyro: the fire's light shows only in gas thinner than Thin Gas (the Soot
    # Mask, as Houdini's pyro emission mask does for scatter): every billow gets
    # a glowing skin around a darker core. Temperature: no mask (both To = 1).
    scatter_mask = graph.node("ShaderNodeMapRange", "Scatter: Soot Mask", glow_frame,
                              NODE_SCATTER_MASK, interpolation_type="SMOOTHSTEP")
    scatter_mask.inputs["To Min"].default_value = 1.0
    graph.link(glow_density_floor.outputs[0], scatter_mask.inputs["Value"])
    graph.link(thin.outputs["Soot From"], scatter_mask.inputs["From Min"])
    graph.link(thin.outputs["Soot To"], scatter_mask.inputs["From Max"])
    masked_glow = graph.node("ShaderNodeVectorMath", "Scatter x Soot Mask", glow_frame, operation="SCALE")
    graph.link(scatter.outputs["Emission"], masked_glow.inputs[0])
    graph.link(_socket(scatter_mask.outputs, "Result"), masked_glow.inputs["Scale"])

    # --- Shader and output
    out_frame = graph.frame("Volume", _FRAME_OUTPUT)
    emission = graph.node("ShaderNodeVectorMath", "Fire + Glow", None, operation="ADD")
    graph.link(fire.outputs["Emission"], emission.inputs[0])
    graph.link(masked_glow.outputs[0], emission.inputs[1])
    shader = graph.node("ShaderNodeVolumePrincipled", "", out_frame, NODE_VOLUME_SHADER)
    shader.width = 240
    shader.inputs["Density Attribute"].default_value = ""
    shader.inputs["Temperature Attribute"].default_value = "temperature"
    shader.inputs["Blackbody Intensity"].default_value = 0.0
    shader.inputs["Emission Strength"].default_value = 1.0
    for identifier, value in kept_shader.items():
        socket = next((s for s in shader.inputs if s.identifier == identifier), None)
        if socket is not None and not socket.is_linked:
            try:
                socket.default_value = value
            except (AttributeError, TypeError, ValueError):
                pass
    graph.link(_rgba(mixed.outputs)[0], shader.inputs["Color"])
    graph.link(gain.outputs[0], shader.inputs["Density"])
    graph.link(emission.outputs[0], shader.inputs["Emission Color"])
    # Shadow rays: the same extinction without the look (see NODE_SHADOW_MIX).
    light_path = graph.node("ShaderNodeLightPath", "", out_frame)
    for socket in light_path.outputs:
        socket.hide = socket.name != "Is Shadow Ray"
    extinction = graph.node("ShaderNodeVolumeAbsorption", "Shadow rays: density only", out_frame,
                            NODE_SHADOW_EXTINCTION)
    extinction.inputs["Color"].default_value = (0.0, 0.0, 0.0, 1.0)
    graph.link(gain.outputs[0], extinction.inputs["Density"])
    shadow_mix = graph.node("ShaderNodeMixShader", "Look / Shadow Rays", out_frame, NODE_SHADOW_MIX)
    graph.link(light_path.outputs["Is Shadow Ray"], shadow_mix.inputs["Fac"])
    graph.link(shader.outputs["Volume"], shadow_mix.inputs[1])
    graph.link(extinction.outputs["Volume"], shadow_mix.inputs[2])
    output = graph.node("ShaderNodeOutputMaterial", "", out_frame)
    graph.link(shadow_mix.outputs["Shader"], output.inputs["Volume"])

    # --- Layout: explicit columns; the corridor under each row carries the
    # Look Controls' bus to that row (_route_bus), so no line crosses a node.
    lane, frame_gap, drop_room = 14.0, 140.0, 110.0

    def bottom_of(*nodes):
        return min(_node_y(node) - _node_height(node) for node in nodes)

    def column(nodes, x, y, gap=40.0):
        for node in nodes:
            _place(node, x, y)
            y -= _node_height(node) + gap
        return y + gap

    _place(controls, 0.0, 0.0)
    gutter = controls.width + 70.0
    grid_a = gutter + 150.0                 # grid reads
    grid_b = grid_a + 200.0 + 40.0          # their floors / unpacking
    prep = grid_b + 160.0 + frame_gap + drop_room
    # Smoke row
    column([smoke_info, color_grid], grid_a, 0.0)
    column([smoke_density], grid_b, 0.0)
    column([colour, hot], prep, -60.0, gap=60.0)
    smoke_next = prep + colour.width + drop_room + 40.0
    _place(mixed, smoke_next, -60.0)
    _place(gain, smoke_next, min(_node_y(hot), bottom_of(mixed) - 40.0))
    smoke_corridor = bottom_of(smoke_info, color_grid, colour, hot, mixed, gain) - 90.0
    # Fire row
    top = smoke_corridor - lane * 8 - 150.0
    reads_bottom = column([fire_info, *heat_nodes[::2]], grid_a, top)
    column([fire_density, *heat_nodes[1::2]], grid_b, top)
    reads_bottom = column(reference_nodes[:2], grid_a, min(reads_bottom, bottom_of(fire_density, *heat_nodes)) - 40.0)
    if len(reference_nodes) > 2:
        _place(reference_nodes[2], grid_b, _node_y(reference_nodes[1]))
    y = top
    if core_nodes:
        _place(core_nodes[0], prep, y)
        _place(core_nodes[1], prep + core_nodes[0].width + drop_room, y)
        y = bottom_of(*core_nodes) - frame_gap
    _place(floor_t, prep, y)
    _place(ramp_range, prep + floor_t.width + drop_room, y)
    _place(flame_ramp, _node_x(ramp_range) + ramp_range.width + 60.0, y)
    _place(thin, prep, bottom_of(floor_t, ramp_range, flame_ramp) - frame_gap)
    fire_x = _node_x(flame_ramp) + flame_ramp.width + 120.0 + drop_room
    _place(fire, fire_x, top)
    fire_corridor = min(reads_bottom, bottom_of(fire, thin)) - 90.0
    # Scatter Glow row
    top = fire_corridor - lane * 17 - 150.0
    reads = []
    for node in (*glow_nodes, glow_wide_node, *depth_nodes):
        if node not in reads:
            reads.append(node)
    firsts = [node for node in reads if node.bl_idname == "ShaderNodeAttribute"]
    column([glow_info, *firsts], grid_a, top)
    column([glow_density_floor, *[node for node in reads if node not in firsts]], grid_b, top)
    _place(scatter, fire_x, top)
    _place(scatter_mask, fire_x + scatter.width + drop_room + 60.0, top)
    _place(masked_glow, _node_x(scatter_mask) + scatter_mask.width + 60.0, top)
    glow_corridor = bottom_of(glow_info, *reads, scatter, scatter_mask) - 90.0
    # Fire + Glow, the Principled Volume and the output
    right = max(_node_x(masked_glow) + masked_glow.width, fire_x + fire.width) + 140.0
    _place(emission, right, (_node_y(fire) + _node_y(scatter)) * 0.5)
    shader_x = right + emission.width + frame_gap
    _place(shader, shader_x, -60.0)
    _place(light_path, shader_x, bottom_of(shader) - 60.0)
    _place(extinction, shader_x, bottom_of(light_path) - 40.0)
    _place(shadow_mix, shader_x + shader.width + 80.0, -60.0)
    _place(output, _node_x(shadow_mix) + shadow_mix.width + 80.0, -60.0)

    def into(name, socket, corridor_y):
        return c[name], socket, corridor_y

    routes = [
        into("Smoke Color", colour.inputs["Smoke Colour"], smoke_corridor),
        into("Hot Soot", colour.inputs["Hot Soot"], smoke_corridor),
        into("Per-Emitter Color", mixed.inputs["Factor"], smoke_corridor),
        into("Hot Smoke Extra", hot.inputs["Extra"], smoke_corridor),
        into("Hot Smoke From", hot.inputs["From"], smoke_corridor),
        into("Hot Smoke To", hot.inputs["To"], smoke_corridor),
        into("Smoke Density", gain.inputs[1], smoke_corridor),
        into("Anisotropy", shader.inputs["Anisotropy"], smoke_corridor),
    ]
    if fresh is not None:
        routes += [into("Blue Core Start", fresh.inputs["Flame Start"], fire_corridor),
                   into("Blue Core Full", fresh.inputs["Flame Full"], fire_corridor)]
    routes += [
        into("Ramp From", ramp_range.inputs["From Min"], fire_corridor),
        into("Ramp To", ramp_range.inputs["From Max"], fire_corridor),
        into("Thin Gas", thin.inputs["Thin Gas"], fire_corridor),
        into("Thin Gas Ratio", thin.inputs["Thin Gas Ratio"], fire_corridor),
        into("Thin Gas Auto", thin.inputs["Thin Gas Auto"], fire_corridor),
        *[into(name, fire.inputs[socket], fire_corridor) for name, socket in (
            ("Pyro Look", "Pyro Look"), ("Wien Contrast", "Contrast"), ("Wien Full Glow", "Full Glow"),
            ("Fire Brightness", "Brightness"), ("Flame Tint", "Flame Tint"), ("Blue Core", "Blue Core"),
            ("Colour Scale", "Colour Scale"), ("Colour Offset", "Colour Offset"), ("Color Ramp", "Color Ramp"),
            ("Blackbody Scale", "Blackbody Scale"))],
        *[into(name, scatter.inputs[socket], glow_corridor) for name, socket in (
            ("Glow Spread", "Glow Spread"), ("Glow Shadowing", "Glow Shadowing"),
            ("Glow Brightness", "Brightness"), ("Own Source", "Own Source"),
            ("Light Temperature", "Light Temperature"), ("Glow Color Ramp", "Use Ramp"),
            ("Glow Smoke Color", "Smoke Colour"))],
        into("Scatter Soot Mask", scatter_mask.inputs["To Max"], glow_corridor),
    ]
    _route_bus(graph, routes, gutter, lane_gap=lane)
    for socket in controls.outputs:
        socket.hide = not socket.is_linked
    material[GRAPH_VERSION_KEY] = GRAPH_VERSION


def linked_control(socket):
    """The Look Controls output feeding this input socket through the bus
    reroutes (its name), or None."""
    while socket.is_linked:
        link = socket.links[0]
        if link.from_node.bl_idname != "NodeReroute":
            return link.from_socket.name if link.from_node.name == NODE_CONTROLS else None
        socket = link.from_node.inputs[0]
    return None


def _node_y(node):
    return node.location_absolute.y if hasattr(node, "location_absolute") else node.location.y


def _node_x(node):
    return node.location_absolute.x if hasattr(node, "location_absolute") else node.location.x


def flame_ramp_samples(domain, count=32):
    """The Colour Ramp of the domain's synced material, sampled evenly over 0..1
    (for the live preview), or None when there is none."""
    material = synced_material(domain, create=False) if domain is not None else None
    node = material.node_tree.nodes.get(NODE_RAMP) if material is not None and material.node_tree else None
    if node is None or node.type != "VALTORGB":
        return None
    ramp = node.color_ramp
    return [tuple(round(float(c), 5) for c in ramp.evaluate(index / (count - 1))[:3]) for index in range(count)]


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
    sync_grid_layout(material)


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
    name="Fumaris Volume",
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
    # Named after its domain by the callers that know it: every volume
    # called "Fumaris Volume.00N" read as swapped between domains when a
    # re-import freed and re-took a name (beta 3 review). Volumes are found
    # by VOLUME_MARKER, never by name.
    volume_object.name = name
    volume_object.location = (0.0, 0.0, 0.0)
    volume_object[VOLUME_MARKER] = os.path.normpath(directory)
    volume_object[VOLUME_PREFIX_MARKER] = prefix
    sequence_start = _frame_number(files[0], prefix)
    sequence_end = _frame_number(files[-1], prefix)
    grid_names = _configure_volume(
        volume_object,
        import_files[0],
        sequence_end - sequence_start + 1,
        sequence_start,
        last_filepath=import_files[-1],
    )
    layout = "packed" if PACKED_PYRO_GRID in grid_names else "separate"
    volume_object[GRID_LAYOUT_KEY] = layout
    _key_density_reference(volume_object, files, prefix)
    volume_object.select_set(False)
    volume_object.hide_select = not bool(selectable)
    if material is not None:
        volume_object.data.materials.append(material)
        if material.get(GENERATED_MATERIAL_MARKER):
            sync_grid_layout(material)
            apply_generated_material_settings(material, appearance)
    else:
        add_density_material(
            volume_object,
            settings=appearance,
            grid_layout=layout,
        )
    _restore_selection(previous_selection, previous_active)
    # Sparks made for the volume this one replaces carry on with it.
    from .sparks import follow_bakes
    follow_bakes()
    from .particles import follow_bakes as follow_particle_bakes
    follow_particle_bakes()
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


# What an import needs from each frame file (has it grids, its Thin Gas Auto
# density reference), kept per file version (size, modification time). An
# import read every frame's metadata twice; on a hard disk, once a bake's
# files had left the page cache, the first pass took 1.3-2.4 s for 90 frames
# at Resolution 768 (R36, 2026-11-19), with Blender frozen at the end of every
# bake. A bake now reads each frame as it is published, still cached
# (remember_written_frames), and a background bake hands what it read to the
# main process (frame_metadata / adopt_frame_metadata).
_FRAME_METADATA = {}        # normalized path -> (size, mtime_ns, (grids, density reference))
_FRAME_METADATA_LIMIT = 50000


def _metadata_key(path):
    return os.path.normcase(os.path.abspath(path))


def _read_frame_summary(path):
    """(grid count, density reference or None) from the file. Raises when unreadable."""
    if openvdb is None:
        return len(_grid_metadata(path)), None
    reference = None
    grids = openvdb.readAllGridMetadata(path)
    for grid in grids:
        if grid.name in (PACKED_PYRO_GRID, "density_ref"):
            metadata = grid.metadata
            if DENSITY_REFERENCE_PROPERTY in metadata:
                reference = float(metadata[DENSITY_REFERENCE_PROPERTY])
                break
    return len(grids), reference


def _frame_summary(path):
    stat = os.stat(path)
    key = _metadata_key(path)
    known = _FRAME_METADATA.get(key)
    if known is not None and known[0] == stat.st_size and known[1] == stat.st_mtime_ns:
        return known[2]
    summary = _read_frame_summary(path)
    if len(_FRAME_METADATA) >= _FRAME_METADATA_LIMIT:
        _FRAME_METADATA.clear()
    _FRAME_METADATA[key] = (stat.st_size, stat.st_mtime_ns, summary)
    return summary


def remember_written_frames(directory, prefix=PREFIX, seen=None):
    """Read the frames a running bake has published since the last call (a
    set of names, updated), while their files are still in the page cache."""
    if seen is None:
        seen = set()
    for path in _frame_files(directory, prefix):
        name = os.path.basename(path)
        if name in seen:
            continue
        try:
            _frame_summary(path)
        except Exception:
            continue    # read again (or skipped) by the import
        seen.add(name)
    return seen


def frame_metadata(directory, prefix=PREFIX):
    """What is known of a folder's frames, by file name: for another process."""
    known = {}
    for path in _frame_files(directory, prefix):
        entry = _FRAME_METADATA.get(_metadata_key(path))
        if entry is not None:
            known[os.path.basename(path)] = [entry[0], entry[1], entry[2][0], entry[2][1]]
    return known


def adopt_frame_metadata(directory, known):
    """frame_metadata from another process; files changed since are read again."""
    for name, (size, mtime_ns, grids, reference) in (known or {}).items():
        key = _metadata_key(os.path.join(directory, name))
        _FRAME_METADATA[key] = (int(size), int(mtime_ns), (int(grids), None if reference is None else float(reference)))


def _files_with_grids(files):
    started = time.perf_counter()
    valid = []
    skipped = []
    for path in files:
        try:
            if _frame_summary(path)[0]:
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
    # The velocity grid (Cycles' motion blur) has to be set while the
    # datablock holds grids, and the original holds none once it is a
    # sequence (its frames live on the evaluated copy): load the first frame
    # as a single file first. The name stays set; the sequence unloads it.
    volume.is_sequence = False
    volume.grids.load()
    if any(grid.name == "velocity" for grid in volume.grids):
        volume.velocity_grid = "velocity"
        volume.velocity_unit = "SECOND"
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
    volume.update_tag()
    volume_object.update_tag()
    return grid_names


def _volume_grid_layout(obj):
    """The grid layout of a volume's cache (GRID_LAYOUT_KEY, noted at
    import); "separate" for Fumaris volumes imported before it was noted (an
    older writer's cache), None for other volumes (unknown)."""
    layout = obj.get(GRID_LAYOUT_KEY)
    if layout in ("packed", "separate"):
        return layout
    return "separate" if obj.get(VOLUME_MARKER) else None


def sync_grid_layout(material):
    """Point a generated material's graph at the grid layouts of the volumes
    that use it: "packed", "separate", or "both" when it is shared by older
    and newer caches. A changed layout rebuilds the graph in place, keeping
    its values (Appearance's, or a version's own look). Returns the layout."""
    if material is None or not material.get(GENERATED_MATERIAL_MARKER):
        return None
    layouts = {_volume_grid_layout(obj) for obj in bpy.data.objects
               if obj.type == "VOLUME" and any(slot is material for slot in obj.data.materials)}
    layouts.discard(None)
    current = material.get(GRID_LAYOUT_KEY, "separate")
    layout = (layouts.pop() if len(layouts) == 1 else "both") if layouts else current
    material[GRID_LAYOUT_KEY] = layout
    tree = material.node_tree
    if (material.use_nodes and tree is not None and tree.nodes.get(NODE_VOLUME_SHADER) is not None
            and material.get(BUILT_GRID_LAYOUT_KEY, "separate") != layout
            and material.get(GRAPH_VERSION_KEY, 0) >= GRAPH_VERSION):
        _rebuild_keeping_values(material)
    return layout


def _rebuild_keeping_values(material):
    """Rebuild the graph, restoring the input values of its named nodes (what
    apply_generated_material_settings writes) with no settings at hand."""
    kept = {}
    for node in material.node_tree.nodes:
        if not node.name.startswith("Fumaris "):
            continue
        values = []
        for index, socket in enumerate(node.inputs):
            if socket.is_linked or not hasattr(socket, "default_value"):
                continue
            value = socket.default_value
            values.append((index, value if isinstance(value, (int, float, str, bool)) else tuple(value)))
        kept[node.name] = values
    _build_generated_material(material, blue_core=material.node_tree.nodes.get(NODE_FRESH_FUEL) is not None)
    nodes = material.node_tree.nodes
    for name, values in kept.items():
        node = nodes.get(name)
        if node is None:
            continue
        for index, value in values:
            if index < len(node.inputs) and not node.inputs[index].is_linked:
                try:
                    node.inputs[index].default_value = value
                except (AttributeError, TypeError, ValueError):
                    pass
    material.node_tree.update_tag()


def _key_density_reference(volume_object, files, prefix=PREFIX):
    """Thin Gas Auto's per-frame density reference as the volume object's
    DENSITY_REFERENCE_PROPERTY, keyed (constant interpolation) at the scene
    frame each file shows at, from the writer's "fumaris_density_reference"
    metadata (on the packed "pyro" grid, or on "density_ref" in older bakes);
    0 (the fixed Thin Gas) where a frame has none. Without the openvdb module
    nothing is keyed: older bakes' graphs still read the "density_ref" grid,
    packed ones fall back to the fixed Thin Gas."""
    if openvdb is None:
        console_log("Fumaris: no openvdb module, so Thin Gas Auto has no density reference for packed caches")
        return False
    if not files:
        return False
    volume = volume_object.data
    first = _frame_number(files[0], prefix)
    keys = []
    for path in files:
        frame = _frame_number(path, prefix)
        if frame is None:
            continue
        value = 0.0
        try:
            reference = _frame_summary(path)[1]
            if reference is not None:
                value = reference
        except Exception:
            pass
        # _stage_import_sequence numbers the files from 1; Blender shows
        # file index i at scene frame frame_start + i - 1 - frame_offset.
        index = frame - first + 1
        keys.append((volume.frame_start + index - 1 - volume.frame_offset, value))
    if not keys:
        return False
    volume_object[DENSITY_REFERENCE_PROPERTY] = keys[0][1]
    if len({value for _, value in keys}) == 1:
        return True
    data_path = f'["{DENSITY_REFERENCE_PROPERTY}"]'
    had_action = volume_object.animation_data is not None and volume_object.animation_data.action is not None
    volume_object.keyframe_insert(data_path, frame=keys[0][0])
    if not had_action and volume_object.animation_data is not None and volume_object.animation_data.action is not None:
        # Ours: removed with the volume (_remove_generated_volume_object).
        volume_object.animation_data.action[DENSITY_REFERENCE_PROPERTY] = True
    curve = _property_fcurve(volume_object, data_path)
    if curve is not None and len(curve.keyframe_points) == 1:
        curve.keyframe_points.add(len(keys) - 1)
        for point, (frame, value) in zip(curve.keyframe_points, keys):
            point.co = (frame, value)
    else:
        for frame, value in keys[1:]:
            volume_object[DENSITY_REFERENCE_PROPERTY] = value
            volume_object.keyframe_insert(data_path, frame=frame)
        curve = _property_fcurve(volume_object, data_path)
    if curve is not None:
        for point in curve.keyframe_points:
            point.interpolation = "CONSTANT"
        curve.update()
    return True


def _property_fcurve(obj, data_path):
    animation = obj.animation_data
    if animation is None or animation.action is None:
        return None
    from bpy_extras import anim_utils
    channelbag = anim_utils.action_get_channelbag_for_slot(animation.action, animation.action_slot)
    return channelbag.fcurves.find(data_path) if channelbag is not None else None


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
    action = obj.animation_data.action if obj.animation_data is not None else None
    bpy.data.objects.remove(obj, do_unlink=True)
    if volume and volume.users == 0:
        bpy.data.volumes.remove(volume)
    # The density reference keys made at import (_key_density_reference).
    if action is not None and action.get(DENSITY_REFERENCE_PROPERTY) and action.users == 0:
        bpy.data.actions.remove(action)
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
