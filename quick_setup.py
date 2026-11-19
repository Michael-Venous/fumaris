"""Small, undoable starting scenes without changing existing simulations."""

import math

import bmesh
import bpy
from bpy.props import EnumProperty
from mathutils import Vector

from . import detail_stack
from .runtime import active_job


# Presets start from the domain defaults (properties.py)
# and change only what makes each effect what it is (2.0 preset pass,
# 2026-11-07): the look, combustion and fade settings stay the defaults.
# 2 GB of VRAM: the presets use at most ~8,300 blocks (the Explosion with its
# stem at frame 120; 2 GB holds 14,000+), and frames stay under ~32 ms (95th
# percentile) at 1 Sub-Step on an RTX 4060 Ti, so preview playback keeps 24 fps
# (output/qs_pass 2026-11-08, output/perf4 2026-11-09). The 2026-11-18
# Explosion peaks at ~6,400 blocks, with a p95 frame of 12.3 ms on the RTX 3090
# Ti (10.1 before); not yet re-measured on the 4060 Ti. Without its large
# Disturb (2026-11-19) it peaks at ~6,200 blocks and previews 3-7% faster.
_DOMAIN_DEFAULTS = {
    "resolution": 512,
    "simulation_memory": 2.0,
}


def _domain_defaults():
    # New domains reserve the free GPU memory less a reserve (Auto VRAM; user, 2026-11-14): the
    # result no longer depends on the amount, so more only means more room;
    # 2 GB stays the amount when Auto is turned off. Not in background
    # Blender: scripts and tests expect the fixed amount (most of a GPU each
    # would not fit several at once).
    return {**_DOMAIN_DEFAULTS, "simulation_memory_auto": not bpy.app.background}

# Fire looks shared by the Fire and Explosion setups, from the two explosions
# that looked right (cauliflower study and desert shot, 2026-11-17): dense,
# defined soot (Smoke Density 4.5), Sharp transport (crisp billows), thin gas
# cooling faster than dense cores (Wisp Cooling 3: glowing cores, dark wisps)
# and soot that does not fade on its own (Dissipation 0). The Pyro look's own
# defaults (Glow Heat 1500-2000 K, Full Glow 1700 K, Blackbody) match them.
# Fuel soot is dark (desert explosion v3, 2026-11-18): a mid-dark Smoke Color
# (0.28; real fuel smoke is darker still, but its billows then stop reading)
# and Hot Soot 0.25, so smoke beside the fire is lit brown rather than black;
# Thin Gas 0.08 keeps the fire in glowing seams between darker cells.
_PYRO_FIRE = {"shader_look": "PYRO", "shader_smoke_density": 4.5, "smoke_detail": "sharp",
              "wisp_cooling": 3.0, "dissipation": 0.0, "shader_smoke_color": (0.28, 0.255, 0.23),
              "shader_hot_soot": 0.25, "shader_thin_gas_ratio": 0.08}

# Grain on fast gas: Speed masks scale with the square root of the source size,
# as gas speeds do (2-5 m/s for the 2 m New Source).
_FAST_GRAIN = {"strength": 30.0, "size_per_size": 0.075,
               "masks": [{"field": "SMOKE", "blend": "MULTIPLY", "range": [0.0, 1.0], "values": [0.0, 1.0]},
                         {"field": "SPEED", "blend": "MULTIPLY", "range_per_root_size": [1.414, 3.536],
                          "values": [0.0, 1.0]}]}

# Emitters are physical since 2.0: Expansion in 1/s, and Normal Velocity on a
# closed source is a volume source (v x area), so presets use smaller values
# than the old, mostly cancelled per-cell push (output/expansion, 2026-11-04).
PRESETS = {
    "SMOKE": {
        "name": "Rising Smoke",
        "description": "A continuous rising plume without flames",
        "domain": {"shader_flame_enabled": False},
        # Hot (Flame Temperature) smoke rises on its own heat.
        "emitter": {"emitter_smoke": 5.0, "emitter_temperature": 2.0,
                    "source_op_temperature": "add", "couple_rate_temperature": 2.0,
                    "emitter_fuel": 0.0, "normal_velocity": 0.25, "emitter_divergence": 0.2},
    },
    "FIRE": {
        "name": "Steady Fire",
        "description": "Continuous flames rising from a fuel bed, with smoke above",
        # Short-lived fuel ends the flames a few source widths up instead of
        # in a tall column; more Vorticity makes them flicker (2026-11-07).
        # A fuel fire, not a glowing ball (2026-11-19, steady-fire-2026-11-19):
        # burnt gas stops glowing within 0.6 s (Cooling Time), so the flames
        # end in tips that turn to smoke; half the soot per fuel burnt (Smoke
        # Amount 0.5) keeps the smoke from burying them, while enough remains
        # between the tongues to outline them. Pyro look: the fire glows by
        # its light in the gas, so Brightness 0.08 (1.5 washed every flame out
        # to peach-white in AgX), full glow at Flame Temperature and a steeper
        # Contrast give a yellow base, orange tongues and deep orange tips.
        "domain": {"shader_flame_enabled": True, "burn_time": 0.5, "vorticity": 4.0,
                   "cooling_time": 0.6, "smoke_per_burn": 0.5, **_PYRO_FIRE,
                   "pyro_brightness": 0.08, "pyro_glow_temperature": 2000.0, "pyro_contrast": 1.0},
        # Billows in the hot gas and crunch where it moves fast (sizes
        # relative to the source). Kept on 2026-11-19: without the Disturb, or
        # with Vorticity 2 or 1 instead of 4, the flames puffed about as much
        # but rose as a smoother, taller column (preset-ab-2026-11-19).
        "disturb": {"strength": 120.0, "detail": 3, "size_per_size": 0.3,
                    "masks": [{"field": "SMOKE", "blend": "MULTIPLY", "range": [0.0, 1.0], "values": [0.0, 1.0]},
                              {"field": "TEMPERATURE", "blend": "MULTIPLY", "range": [500.0, 1500.0], "values": [0.0, 1.0]}]},
        "grain": _FAST_GRAIN,
        # Flame tongues instead of stacked round lobes: Shred tears the hot
        # edges into strands and Vorticity is up to 5x stronger in flames
        # (1x at 500 K), so the puffs break into licking tongues.
        "layers": [{"type": "SHRED", "strength": 4.0},
                   {"type": "VORTICITY",
                    "masks": [{"field": "TEMPERATURE", "range": [500.0, 2000.0], "values": [1.0, 5.0]}]}],
        # New Source is a fuel bed on the 3D cursor: a cylinder 2 m across and
        # 0.25 m high, filled with fuel (Volume emission), instead of a ball
        # that glowed above the ground. A ground collider under it, as under a
        # real fire, takes the air in from the sides only, which wrinkles the
        # flame base into tongues; its margin is thin (0.1 m), as the default
        # 0.3 m held the air still inside the bed and starved the fire.
        "new_source": {"kind": "pool", "height": 0.25,
                       "emitter": {"mesh_emission_mode": "volume", "mesh_emission_distance": 0.5}},
        "ground": True,
        "ground_margin": 0.1,
        # The source only ignites the fuel; burning makes the heat (Fuel 1
        # burns at Flame Temperature with the default Fuel Energy).
        # Almost no source smoke: soot forms as the fuel burns, so under Smoke
        # Density 4.5 the source glows instead of being a dark ball of cool,
        # unburnt gas. Rich fuel (2) is written as Maximum at a fast rate, so
        # the gas crossing the thin bed takes it up fully (Blend at rate 2 gave
        # a weak, dull red fire) and keeps burning as it mixes with air.
        "emitter": {"emitter_smoke": 0.1, "emitter_temperature": 0.35,
                    "couple_rate_temperature": 20.0, "source_op_temperature": "max",
                    "emitter_fuel": 2.0, "source_op_fuel": "max", "couple_rate_fuel": 20.0,
                    "normal_velocity": 0.25, "emitter_divergence": 0.2},
    },
    "EXPLOSION": {
        "name": "Explosion",
        "description": "A short burst of expanding fire and smoke",
        # A grand ground explosion that balloons out wide and low rather than
        # shooting up into a mushroom on a stem (user, 2026-11-17; lessons of
        # the desert ground-explosion shot, mushroom_cloud README). Hot gas
        # rises at about g (1 - T_air / T) whatever its size, so a small
        # fireball races up its own height in a moment and reads as small:
        # low buoyancy and a slower Simulation Speed keep it big and heavy.
        # Oxygen Limit 0 burns the fuel in the blast (with 1 a dense fireball
        # stays full of fuel and glows evenly for the whole shot).
        # Fire turns to soot gradually, as in footage of real fuel bursts
        # (Mk 77 fire bombs, desert explosion v3, 2026-11-18): a short Burn
        # Time (0.7 s, about 1 s on screen at this Simulation Speed, as in
        # the shot) blackens the lean clumps first while rich and long-burning
        # ones (see Clumps) keep fire in pockets and at the base: black
        # appears on the tops at about 1.5 s, half the cloud is black by 2 s,
        # the last flames go out at about 4 s.
        # Our Pyro look (cauliflower/desert work): blackbody fire, warm-gas
        # glow over 1500-2000 K, dark fuel soot; brightness and Scatter Glow
        # are scaled for a fireball this size in daylight AgX. Hot gas is up
        # to 10x denser (500-1500 K, as Houdini's pyro shading): crisp, dark
        # lobes with glowing seams instead of a washed-out, evenly glowing ball.
        "domain": {"shader_flame_enabled": True, "buoyancy_per_temp": 0.5, "smoke_per_burn": 2.5,
                   "oxygen_limit": 0.0, "burn_time": 0.7, "simulation_speed": 0.75,
                   "shader_look": "PYRO", "pyro_brightness": 0.2,
                   "pyro_temperature_multiplier": 0.8, "pyro_scatter_glow": 2.0,
                   "shader_hot_density": 10.0, **_PYRO_FIRE},
        # Air Margin follows the blast's size (0.5 m for the 2 m New Source):
        # in metres it stayed under a voxel when a large explosion was
        # simulated at the same voxels per metre of blast, and the young
        # fireball grew flat, block-shaped faces (a sparse block is 16 voxels;
        # desert shot, 2026-11-17). Cooling Time grows as gas speeds do (1.6 s
        # at 2 m, ~15 s at the desert shot's ~170 m, where 20 s looked right),
        # so a big fireball still glows in its seams as it rises.
        "domain_per_size": {"air_margin": 0.25},
        "domain_per_root_size": {"cooling_time": 1.6 / math.sqrt(2.0)},
        # One fine Disturb, its swirls relative to the sources' largest
        # dimension (about four voxels at the default size), only where gas
        # moves at blast speed and carries smoke: crisp cells in the young
        # fireball, calm smoke once the blast slows, with no keyframes (an
        # unmasked strong fine layer churned the late smoke in the desert
        # shot). Grain crunches the fast front. No large Disturb (220, 0.3 x
        # the size, in 500-1500 K smoke) since 2026-11-19: it rolled the
        # fireball into many small, separate soot balls and streaks; without
        # it the clumps' lobes stay a few big, soft billows with a fine crust
        # of cells and fire in their seams, at the same width, height and soot
        # timing and a slightly cheaper preview (cauliflower study;
        # notes/experiments/preset-ab-2026-11-19).
        "disturb": [{"strength": 300.0, "detail": 3, "size_per_size": 0.125,
                     "masks": [{"field": "SPEED", "blend": "MULTIPLY", "range_per_root_size": [1.2, 3.6],
                                "values": [0.0, 1.0]},
                               {"field": "SMOKE", "blend": "MULTIPLY", "range": [0.0, 0.05], "values": [0.0, 1.0]}]}],
        "grain": _FAST_GRAIN,
        # Clumps: four dozen balls of fuel fly out of the source at random
        # speeds and burn for random times (Geometry Nodes, gn_sources), so the
        # fireball grows uneven lobes instead of one smooth ball (the puff
        # emitters of a Houdini/Axiom explosion). Thrown mostly sideways (Lift
        # 0.05, directions at most 0.35 up) with a strong swell, they set the
        # fireball's width at once as a low lens that rises later, as real
        # bursts do, instead of a ball. Fuel varies 0.15-4x from clump to
        # clump, and 30% of them keep feeding fuel for up to 2.5 s.
        "clumps": {"count": 48, "clump_size": 0.45, "speed_per_root_size": 12.0, "drag": 2.5,
                   "fuel": 16.0, "expansion": 28.0, "lift": 0.05, "upward_spread": 0.35,
                   "fuel_low": 0.15, "fuel_high": 4.0, "long_burn": 0.3, "long_burn_seconds": 2.5},
        # No forced jet: a sustained upward push sheds stacked vortex rings.
        # The source ignites the fuel at Flame Temperature (Temperature 1)
        # from its first frame, with little smoke of its own: soot forms as
        # the fuel burns, so the young fireball glows instead of starting as
        # a dark, unlit puff. Overlapping clumps write smoke and fuel as
        # Maximum, not Add: with Add every overlap is another density level
        # and the fire shows stepped layers.
        "emitter": {"emitter_smoke": 0.3, "emitter_temperature": 1.0,
                    "couple_rate_temperature": 60.0, "source_op_temperature": "max",
                    "source_op_smoke": "max", "source_op_fuel": "max",
                    "couple_rate_smoke": 10.0, "couple_rate_fuel": 10.0,
                    # Expansion burst: the source gas swells about 28x per
                    # second during its burst frames, a pressure blast that
                    # drives the fast, tearing front of a detonation.
                    "emitter_fuel": 6.0, "normal_velocity": 1.0, "emitter_divergence": 28.0,
                    "velocity": (0.0, 0.0, 0.0)},
        "burst_frames": 14,
        # No smoke stem (it stood the cloud on a column: a mushroom). A ground
        # collider under the source lets the blast spread along it.
        "ground": True,
        "duration": 120,
    },
    "FOG": {
        "name": "Ground Fog",
        "description": "Cold, heavy fog that spills out and creeps along the ground",
        # Cold smoke heavier than air sinks and spreads along the ground.
        "domain": {"shader_flame_enabled": False, "buoyancy_per_smoke": -0.4,
                   "shader_smoke_color": (0.8,) * 3},
        # Disturb rolls the spreading fog into billows; without it the heavy
        # sheet sloshed outward in regular concentric rings (2026-11-09,
        # output/qs_review/fog1-2).
        "disturb": {"strength": 25.0, "detail": 1, "size_per_size": 0.5},
        "emitter": {"emitter_smoke": 2.0, "emitter_temperature": 0.0,
                    "source_op_temperature": "add", "couple_rate_temperature": 2.0,
                    "emitter_fuel": 0.0, "normal_velocity": 0.5, "emitter_divergence": 0.3},
        "ground": True,
    },
    "STACK": {
        "name": "Chimney Smoke",
        "description": "A warm column of smoke blown upward from the source, as from a chimney or stack",
        # Disturb breaks the column into turbulent smoke a few source widths
        # up; without it the exhaust rose as a smooth tube.
        "domain": {"shader_flame_enabled": False},
        "disturb": {"strength": 60.0, "detail": 1, "size_per_size": 0.5},
        # Warm exhaust (a quarter of Flame Temperature) pushed up at 4 m/s.
        "emitter": {"emitter_smoke": 4.0, "emitter_temperature": 0.25,
                    "source_op_temperature": "add", "couple_rate_temperature": 2.0,
                    "emitter_fuel": 0.0, "normal_velocity": 0.0, "emitter_divergence": 0.0,
                    "velocity": (0.0, 0.0, 4.0), "couple_rate_velocity": 8.0},
    },
}


def _source_error(context, sources):
    if not sources:
        return "Select a mesh source, or choose New Source"
    for obj in sources:
        if obj.type != "MESH":
            return f"{obj.name} is not a mesh; choose mesh sources or New Source"
        if not obj.is_editable:
            return f"{obj.name} is linked and cannot be edited; choose New Source"
        if obj.fumaris.smoke_object_type != "none":
            return f"{obj.name} already has a Fumaris role; choose New Source"
        # Activating an unassigned object in an existing participant collection
        # could silently make it emit into two simulations.
        for domain in bpy.data.objects:
            props = getattr(domain, "fumaris", None)
            if props is None or props.smoke_object_type != "domain":
                continue
            collection = props.emitter_collection
            if collection and obj.name in collection.all_objects:
                return f"{obj.name} is already in {domain.name}'s emitters; choose New Source"
    return None


def _apply_settings(props, values):
    for name, value in values.items():
        setattr(props, name, value)


def _new_collection(name, parent):
    collection = bpy.data.collections.new(name)
    parent.children.link(collection)
    return collection


_ROOT_MARKER = "fumaris_root"
_ROLE_LABELS = (("emitter", "Emitters"), ("collider", "Colliders"),
                ("effector", "Effectors"), ("outflow", "Outflows"))


def fumaris_root(scene):
    """The scene's top-level Fumaris collection, which holds one group per
    domain: found by its marker (a renamed root still works), else made."""
    for child in scene.collection.children:
        if child.get(_ROOT_MARKER):
            return child
    root = _new_collection("Fumaris", scene.collection)
    root[_ROOT_MARKER] = True
    return root


def new_domain_group(scene, name):
    """Fumaris > name > the domain's Emitters, Colliders, Effectors and
    Outflows collections. Returns (group, {role: collection})."""
    group = _new_collection(name, fumaris_root(scene))
    return group, {role: _new_collection(f"{group.name} {label}", group) for role, label in _ROLE_LABELS}


def _new_source(collection, name, position, shape=None):
    """New Source: a 2 m icosphere centred on the 3D cursor, or with a "pool"
    shape a low cylinder 2 m across standing on it (a fuel bed)."""
    mesh = bpy.data.meshes.new(name)
    bm = bmesh.new()
    try:
        if shape and shape.get("kind") == "pool":
            height = shape["height"]
            bmesh.ops.create_cone(bm, cap_ends=True, cap_tris=False, segments=48, radius1=1.0, radius2=1.0,
                                  depth=height)
            bmesh.ops.translate(bm, vec=(0.0, 0.0, 0.5 * height), verts=bm.verts)
        else:
            # Smooth enough not to show facets when scaled up for large effects.
            bmesh.ops.create_icosphere(bm, subdivisions=4, radius=1.0)
        bm.to_mesh(mesh)
    finally:
        bm.free()
    obj = bpy.data.objects.new(name, mesh)
    collection.objects.link(obj)
    obj.location = position
    if shape:
        _apply_settings(obj.fumaris, shape.get("emitter", {}))
    return obj


def _new_debris(collection, name, minimum, maximum, start_frame):
    """Debris thrown out of an explosion: particles that leave smoke trails.

    A hidden icosphere the size of the sources emits a few dozen particles in
    the first two frames, flung outward and upward and pulled down by scene
    gravity. Each particle is a small point emitter of hot smoke, so its head
    glows briefly and its path leaves a thin trail that cools to grey.
    """
    size = max(0.1, max(maximum - minimum))
    mesh = bpy.data.meshes.new(name)
    bm = bmesh.new()
    try:
        bmesh.ops.create_icosphere(bm, subdivisions=2, radius=0.5 * size)
        bm.to_mesh(mesh)
    finally:
        bm.free()
    obj = bpy.data.objects.new(name, mesh)
    collection.objects.link(obj)
    obj.location = (minimum + maximum) * 0.5
    obj.hide_render = True
    obj.display_type = "WIRE"
    modifier = obj.modifiers.new(name, "PARTICLE_SYSTEM")
    settings = modifier.particle_system.settings
    settings.name = name
    settings.count = 32
    settings.frame_start = start_frame
    settings.frame_end = start_frame + 1
    # Ejecta speed grows with the blast size: ~8 m/s at 2 m, ~27 m/s at 20 m.
    speed = 6.0 * math.sqrt(size)
    # Short-lived: trails reach about twice the source size before they
    # stop, so they do not claim memory far from the fireball.
    settings.lifetime = 30
    settings.lifetime_random = 0.4
    settings.emit_from = "FACE"
    settings.normal_factor = speed
    settings.object_align_factor = (0.0, 0.0, 0.6 * speed)
    settings.factor_random = 0.5 * speed
    settings.physics_type = "NEWTON"
    settings.render_type = "NONE"
    settings.display_method = "DOT"
    props = obj.fumaris
    props.smoke_object_type = "emitter"
    props.participant_enabled = True
    props.participant_type = "particles"
    props.particle_system_name = modifier.particle_system.name
    props.particle_subtype = "point_cloud"
    # About the distance a particle moves per solver step, so its stamps
    # join into a trail instead of a row of beads.
    props.point_radius = 0.04 * size
    _apply_settings(props, {"emitter_smoke": 2.0, "emitter_temperature": 0.6,
                            "source_op_temperature": "max", "emitter_fuel": 0.0,
                            "normal_velocity": 0.0, "emitter_divergence": 0.0})
    return obj


def _new_clumps(collection, name, minimum, maximum, start_frame, preset):
    """The explosion's clumps: a Geometry Nodes point source (gn_sources.build_clumps)
    sized to the sources, burning over the burst frames with the source's channels."""
    from . import gn_sources
    clumps = preset["clumps"]
    size = max(0.1, max(maximum - minimum))
    obj = bpy.data.objects.new(name, bpy.data.meshes.new(name))
    collection.objects.link(obj)
    obj.location = (minimum + maximum) * 0.5
    obj.hide_render = True
    tree = gn_sources.build_clumps(name)
    inputs = tree.interface.items_tree
    for key, value in (("Start Frame", float(start_frame)), ("Duration", float(preset.get("burst_frames", 8))),
                       ("Clumps", int(clumps["count"])), ("Radius", 0.5 * size),
                       ("Clump Size", clumps["clump_size"]), ("Speed", clumps["speed_per_root_size"] * math.sqrt(size)),
                       ("Drag", clumps["drag"]), ("Fuel", clumps["fuel"]), ("Expansion", clumps["expansion"]),
                       ("Lift", clumps.get("lift", 0.4)), ("Fuel Low", clumps.get("fuel_low", 0.4)),
                       ("Fuel High", clumps.get("fuel_high", 1.6)), ("Long Burn", clumps.get("long_burn", 0.0)),
                       ("Long Burn Frames", clumps.get("long_burn_seconds", 2.0) * _fps()),
                       ("Upward Spread", clumps.get("upward_spread", 1.0))):
        inputs[key].default_value = value
    modifier = obj.modifiers.new(name, "NODES")
    modifier.node_group = tree
    props = obj.fumaris
    props.smoke_object_type = "emitter"
    props.participant_enabled = True
    props.participant_type = "geometry_nodes"
    props.gn_subtype = "point_cloud"
    props.point_radius = 1.0
    # The source's burst channels; fuel and expansion come per clump.
    _apply_settings(props, {key: value for key, value in preset["emitter"].items()
                            if key not in ("normal_velocity", "velocity")})
    return obj


def _fps():
    scene = bpy.context.scene
    return scene.render.fps / scene.render.fps_base if scene else 24.0


def _masks(masks, size):
    """Preset masks for a source of this size: a range_per_root_size (Speed
    masks) scales with the square root of the size, as the blast speed does."""
    if masks is None:
        return None   # the layer type's default masks
    resolved = []
    for mask in masks:
        mask = dict(mask)
        per_root = mask.pop("range_per_root_size", None)
        if per_root:
            mask["range"] = [value * math.sqrt(size) for value in per_root]
        resolved.append(mask)
    return resolved


def _new_ground(collection, name, minimum, maximum, margin=None):
    """A collider floor just under the sources, ten source widths across
    (margin: its Collider Margin, if not the default)."""
    size = 10.0 * max(1.0, max(maximum - minimum))
    mesh = bpy.data.meshes.new(name)
    bm = bmesh.new()
    try:
        bmesh.ops.create_grid(bm, x_segments=1, y_segments=1, size=0.5 * size)
        bm.to_mesh(mesh)
    finally:
        bm.free()
    obj = bpy.data.objects.new(name, mesh)
    collection.objects.link(obj)
    obj.location = ((minimum.x + maximum.x) * 0.5, (minimum.y + maximum.y) * 0.5, minimum.z - 0.05)
    obj.fumaris.smoke_object_type = "collider"
    obj.fumaris.participant_enabled = True
    if margin is not None:
        obj.fumaris.collider_margin = margin
    return obj


def _source_bounds(context, sources):
    depsgraph = context.evaluated_depsgraph_get()
    positions = []
    for obj in sources:
        evaluated = obj.evaluated_get(depsgraph)
        for corner in evaluated.bound_box:
            position = evaluated.matrix_world @ Vector(corner)
            if all(math.isfinite(value) for value in position):
                positions.append(position)
    if not positions:
        positions = [context.scene.cursor.location.copy()]
    minimum = Vector(tuple(min(point[axis] for point in positions) for axis in range(3)))
    maximum = Vector(tuple(max(point[axis] for point in positions) for axis in range(3)))
    return minimum, maximum


def _new_domain(collection, name, minimum, maximum):
    # A settings anchor, not a physical boundary for the sparse simulation.
    obj = bpy.data.objects.new(name, None)
    collection.objects.link(obj)
    obj.location = (minimum + maximum) * 0.5
    obj.empty_display_type = "PLAIN_AXES"
    obj.empty_display_size = max(0.5, max(maximum - minimum) * 0.5)
    obj.hide_render = True
    return obj


def _configure_source(obj, preset, start_frame, current_frame):
    props = obj.fumaris
    props.smoke_object_type = "emitter"
    props.participant_enabled = True
    if obj.particle_systems:
        props.participant_type = "particles"
        props.particle_system_name = obj.particle_systems[0].name
        props.particle_subtype = "point_cloud"
    elif any(modifier.type == "NODES" for modifier in obj.modifiers):
        props.participant_type = "geometry_nodes"
        props.gn_subtype = "mesh"
    else:
        props.participant_type = "mesh"
    _apply_settings(props, preset["emitter"])
    duration = preset.get("burst_frames")
    if duration:
        animation = obj.animation_data
        action = animation.action if animation else None
        if action and (action.users > 1 or action.library or not action.is_editable):
            # Object copies can share the same Action and even the same Action
            # Slot. Keep the burst keys private without losing other channels
            # or switching to another slot when the copied action is assigned.
            slot = animation.action_slot
            slot_identifier = slot.identifier if slot else None
            animation.action = action.copy()
            if slot_identifier:
                animation.action_slot = animation.action.slots[slot_identifier]
        # Boolean channels use discrete interpolation. Animate only the new
        # Fumaris enable channel, leaving transforms and other animation intact.
        burst_end = start_frame + duration - 1
        stem = preset.get("stem")
        end = burst_end
        if stem:
            # After the blast the source keeps venting warm, sooty gas (no fuel,
            # no expansion) that fades out: the column the rising fireball
            # draws up under itself, so the cloud stands on a stem.
            stem_values = {"emitter_fuel": 0.0, "emitter_divergence": stem.get("expansion", 0.0),
                           "emitter_smoke": stem["smoke"], "emitter_temperature": stem["temperature"],
                           "normal_velocity": stem.get("normal_velocity", 0.0)}
            end = burst_end + stem["frames"]
            for name, value in stem_values.items():
                burst = getattr(props, name)
                props.keyframe_insert(name, frame=burst_end)
                setattr(props, name, value)
                props.keyframe_insert(name, frame=burst_end + 1)
                if name in ("emitter_smoke", "emitter_temperature"):
                    props.keyframe_insert(name, frame=end - stem.get("fade", 0))
                    setattr(props, name, 0.0)
                    props.keyframe_insert(name, frame=end)
                setattr(props, name, burst)
        props.participant_enabled = True
        props.keyframe_insert("participant_enabled", frame=end)
        props.participant_enabled = False
        props.keyframe_insert("participant_enabled", frame=end + 1)
        props.participant_enabled = current_frame <= end


class FUMARIS_OT_quick_setup(bpy.types.Operator):
    bl_idname = "fumaris.quick_setup"
    bl_label = "Fumaris Quick Setup"
    bl_description = "Create a ready-to-preview smoke or fire setup with linked emitter collections"
    bl_options = {"REGISTER", "UNDO"}

    preset: EnumProperty(
        name="Effect",
        items=[(key, values["name"], values["description"]) for key, values in PRESETS.items()],
        default="SMOKE",
    )
    source_mode: EnumProperty(
        name="Emit From",
        items=[
            ("SELECTED", "Selected Objects", "Use selected mesh objects without an existing Fumaris role"),
            ("NEW", "New Source", "Create a source at the 3D cursor; leave selected objects unchanged"),
        ],
        default="NEW",
    )
    debris: bpy.props.BoolProperty(
        name="Debris Trails",
        description="Explosion only: throw a few dozen particles out of the blast that leave hot, smoky trails",
        default=False,
    )

    @classmethod
    def poll(cls, context):
        if context.mode != "OBJECT":
            cls.poll_message_set("Switch to Object Mode to create a Fumaris setup")
            return False
        if active_job():
            cls.poll_message_set("Stop the active Fumaris simulation before creating a setup")
            return False
        return context.scene is not None

    def invoke(self, context, event):
        self.source_mode = "NEW" if _source_error(context, context.selected_objects) else "SELECTED"
        return context.window_manager.invoke_props_dialog(self, width=360)

    def draw(self, context):
        layout = self.layout
        layout.prop(self, "preset")
        layout.prop(self, "source_mode")
        if self.source_mode == "SELECTED":
            error = _source_error(context, context.selected_objects)
            if error:
                layout.label(text=error, icon="ERROR")
            else:
                layout.label(text=f"{len(context.selected_objects)} selected mesh source(s)", icon="MESH_DATA")
        if self.preset == "EXPLOSION":
            layout.prop(self, "debris")
        layout.label(text="Creates a simulation empty and its collections under Fumaris", icon="MOD_FLUID")

    def execute(self, context):
        sources = list(context.selected_objects) if self.source_mode == "SELECTED" else []
        if self.source_mode == "SELECTED":
            error = _source_error(context, sources)
            if error:
                self.report({"WARNING"}, error)
                return {"CANCELLED"}
        preset = PRESETS[self.preset]
        # A second Rising Smoke is "Rising Smoke 2", its objects named to match.
        name = _unique_group_name(preset["name"])
        root, collections = new_domain_group(context.scene, name)
        if not sources:
            sources = [_new_source(collections["emitter"], f"{name} Source", context.scene.cursor.location,
                                   preset.get("new_source"))]
        else:
            for source in sources:
                collections["emitter"].objects.link(source)
        minimum, maximum = _source_bounds(context, sources)
        domain = _new_domain(root, f"{name} Simulation", minimum, maximum)
        props = domain.fumaris
        props.smoke_object_type = "domain"
        props.sim_start_frame = context.scene.frame_start
        props.sim_end_frame = props.sim_start_frame + preset["duration"] - 1 if "duration" in preset else context.scene.frame_end
        # Short effects keep their own length; the rest follow the timeline.
        props.use_scene_frame_range = "duration" not in preset
        _apply_settings(props, _domain_defaults())
        _apply_settings(props, preset["domain"])
        size = max(maximum - minimum)
        # Settings that follow the sources' size (the domain defaults suit 2 m).
        basis = max(0.1, size)
        _apply_settings(props, {name: factor * basis for name, factor in preset.get("domain_per_size", {}).items()})
        _apply_settings(props, {name: factor * math.sqrt(basis)
                                for name, factor in preset.get("domain_per_root_size", {}).items()})
        disturbs = preset.get("disturb") or ()
        for disturb in [disturbs] if isinstance(disturbs, dict) else disturbs:
            # Disturb's size is in world units: scale it to the sources so
            # large explosions get large billows (0.1 m did nothing at 20 m).
            detail_stack.add_layer(props, "DISTURB", masks=_masks(disturb.get("masks"), basis),
                                   strength=disturb["strength"], detail=disturb["detail"],
                                   size=max(0.05, disturb["size_per_size"] * size))
        grain = preset.get("grain")
        if grain:
            detail_stack.add_layer(props, "GRAIN", masks=_masks(grain.get("masks"), basis), strength=grain["strength"],
                                   size=max(0.02, grain["size_per_size"] * size))
        for layer in preset.get("layers", ()):
            # Other layers (Shred, Vorticity, Cooling...): strength and masks as given.
            detail_stack.add_layer(props, layer["type"], masks=_masks(layer.get("masks"), basis),
                                   **{key: value for key, value in layer.items() if key not in ("type", "masks")})
        for role, collection in collections.items():
            setattr(props, f"{role}_collection", collection)
        for source in sources:
            _configure_source(source, preset, props.sim_start_frame, context.scene.frame_current)
        if preset.get("ground"):
            _new_ground(collections["collider"], f"{name} Ground", minimum, maximum, preset.get("ground_margin"))
        if preset.get("clumps"):
            _new_clumps(collections["emitter"], f"{name} Clumps", minimum, maximum, props.sim_start_frame, preset)
        if self.debris and self.preset == "EXPLOSION":
            _new_debris(collections["emitter"], f"{name} Debris", minimum, maximum, props.sim_start_frame)
        for obj in context.selected_objects:
            obj.select_set(False)
        domain.select_set(True)
        context.view_layer.objects.active = domain
        if hasattr(context.scene, "fumaris_preview_domain"):
            from .playback import choose_domain
            choose_domain(context.scene, domain)
        context.view_layer.update()
        self.report({"INFO"}, f"{name} ready: press Play")
        return {"FINISHED"}


def _unique_group_name(base):
    if base not in bpy.data.collections:
        return base
    number = 2
    while f"{base} {number}" in bpy.data.collections:
        number += 1
    return f"{base} {number}"


class FUMARIS_OT_domain_create(bpy.types.Operator):
    bl_idname = "fumaris.domain_create"
    bl_label = "New Domain"
    bl_description = ("Create an empty simulation domain at the 3D cursor, with its Emitters, Colliders, Effectors "
                      "and Outflows collections under the Fumaris collection, and choose it in the Fumaris tab")
    bl_options = {"REGISTER", "UNDO"}

    # Empty: the Fumaris tab's name box (then Simulation).
    name: bpy.props.StringProperty(name="Name")

    @classmethod
    def poll(cls, context):
        return FUMARIS_OT_quick_setup.poll(context)

    def execute(self, context):
        scene = context.scene
        typed = getattr(scene, "fumaris_new_domain_name", "").strip()
        name = _unique_group_name(self.name.strip() or typed or "Simulation")
        if typed:
            scene.fumaris_new_domain_name = ""   # used once
        group, collections = new_domain_group(context.scene, name)
        cursor = context.scene.cursor.location
        domain = _new_domain(group, f"{name} Domain", cursor - Vector((1, 1, 1)), cursor + Vector((1, 1, 1)))
        props = domain.fumaris
        props.smoke_object_type = "domain"
        props.sim_start_frame = context.scene.frame_start
        props.sim_end_frame = context.scene.frame_end
        _apply_settings(props, _domain_defaults())
        for role, collection in collections.items():
            setattr(props, f"{role}_collection", collection)
        for obj in context.selected_objects:
            obj.select_set(False)
        domain.select_set(True)
        context.view_layer.objects.active = domain
        from .playback import choose_domain
        choose_domain(context.scene, domain)
        self.report({"INFO"}, f"{domain.name} ready: add emitters from the Fumaris tab")
        return {"FINISHED"}


_ROLE_NAMES = dict(_ROLE_LABELS)


def _add_error(obj, role, domain):
    """Why obj cannot join domain as role, or None."""
    from .participants import participant_for_object
    if obj.fumaris.smoke_object_type == "domain":
        return "a domain"
    if not obj.is_editable:
        return "linked from a library"
    kind = obj.fumaris.smoke_object_type
    if kind not in {"none", role}:
        return f"already a Fumaris {kind}"
    if participant_for_object(obj, role, 0, "") is None:
        return f"{obj.type.lower()} objects cannot be {_ROLE_NAMES[role].lower()} yet"
    return None


class FUMARIS_OT_participants_add_selected(bpy.types.Operator):
    bl_idname = "fumaris.participants_add_selected"
    bl_label = "Add Participant"
    bl_description = "Add selected objects to the chosen domain, or create a participant at the 3D cursor"
    bl_options = {"REGISTER", "UNDO"}

    role: EnumProperty(name="Role", items=[(role, label[:-1], "") for role, label in _ROLE_LABELS])

    @classmethod
    def description(cls, context, properties):
        if not any(obj.fumaris.smoke_object_type != "domain" for obj in context.selected_objects):
            return f"Create a new {_ROLE_NAMES[properties.role][:-1].lower()} at the 3D cursor in the chosen domain"
        return (f"Make the selected objects {_ROLE_NAMES[properties.role].lower()} of the chosen domain: each is "
                "linked into the domain's collection for that role and keeps its other collections")

    @classmethod
    def poll(cls, context):
        from .playback import resolve_domain
        if context.mode != "OBJECT":
            cls.poll_message_set("Switch to Object Mode")
            return False
        if active_job():
            cls.poll_message_set("Stop the active Fumaris simulation first")
            return False
        if resolve_domain(context) is None:
            cls.poll_message_set("Choose a domain in the Fumaris tab")
            return False
        return True

    def execute(self, context):
        from .playback import resolve_domain
        domain = resolve_domain(context)
        props = domain.fumaris
        attribute = f"{self.role}_collection"
        collection = getattr(props, attribute)
        if collection is None:
            # A domain made by hand: give it the role's collection beside it.
            parent = domain.users_collection[0] if domain.users_collection else context.scene.collection
            collection = _new_collection(f"{domain.name} {_ROLE_NAMES[self.role]}", parent)
            setattr(props, attribute, collection)
        selected = [obj for obj in context.selected_objects if obj.fumaris.smoke_object_type != "domain"]
        if not selected:
            name = f"{domain.name} {_ROLE_NAMES[self.role][:-1]}"
            if self.role == "effector":
                obj = bpy.data.objects.new(name, None)
                collection.objects.link(obj)
                obj.empty_display_type = "SPHERE"
            elif self.role == "collider":
                mesh = bpy.data.meshes.new(name)
                bm = bmesh.new()
                try:
                    bmesh.ops.create_cube(bm, size=2.0)
                    bm.to_mesh(mesh)
                finally:
                    bm.free()
                obj = bpy.data.objects.new(name, mesh)
                collection.objects.link(obj)
            else:
                obj = _new_source(collection, name, context.scene.cursor.location)
            obj.location = context.scene.cursor.location
            obj.fumaris.smoke_object_type = self.role
            obj.fumaris.participant_enabled = True
            for other in context.selected_objects:
                other.select_set(False)
            obj.select_set(True)
            context.view_layer.objects.active = obj
            self.report({"INFO"}, f"Created {obj.name} in {domain.name}")
            return {"FINISHED"}
        added, skipped = [], []
        for obj in sorted(context.selected_objects, key=lambda item: item.name):
            error = _add_error(obj, self.role, domain)
            if error:
                skipped.append(f"{obj.name} ({error})")
                continue
            if obj.name not in collection.all_objects:
                collection.objects.link(obj)
            obj.fumaris.smoke_object_type = self.role
            added.append(obj.name)
        role = _ROLE_NAMES[self.role].lower() if len(added) != 1 else _ROLE_NAMES[self.role][:-1].lower()
        if not added:
            self.report({"WARNING"}, "Nothing added: " + "; ".join(skipped))
            return {"CANCELLED"}
        message = f"Added {len(added)} {role} to {domain.name}"
        if skipped:
            self.report({"WARNING"}, message + "; skipped " + "; ".join(skipped))
        else:
            self.report({"INFO"}, message)
        return {"FINISHED"}


class FUMARIS_OT_domain_choose(bpy.types.Operator):
    bl_idname = "fumaris.domain_choose"
    bl_label = "Choose Domain"
    bl_description = ("Choose this domain for the Fumaris tab, playback and Add actions, select it and show its "
                      "settings. It stays chosen while you select its emitters and colliders")
    bl_options = {"REGISTER", "UNDO"}

    name: bpy.props.StringProperty(options={"HIDDEN"})

    @classmethod
    def poll(cls, context):
        if active_job():
            cls.poll_message_set("Stop the active Fumaris simulation to choose another domain")
            return False
        return True

    def invoke(self, context, event):
        domain = bpy.data.objects.get(self.name)
        if domain is None or domain.name not in context.view_layer.objects:
            self.report({"WARNING"}, f"{self.name} is not in this view layer")
            return {"CANCELLED"}
        from .playback import choose_domain
        choose_domain(context.scene, domain)
        return bpy.ops.fumaris.participant_select("INVOKE_DEFAULT", name=domain.name, activate=True)


class FUMARIS_OT_participant_remove(bpy.types.Operator):
    bl_idname = "fumaris.participant_remove"
    bl_label = "Remove from Domain"
    bl_description = ("Take this object out of the chosen domain: unlink it from the domain's collections and clear "
                      "its Fumaris role. The object stays in the scene")
    bl_options = {"REGISTER", "UNDO"}

    name: bpy.props.StringProperty(options={"HIDDEN"})

    @classmethod
    def poll(cls, context):
        return not active_job()

    def execute(self, context):
        from .playback import resolve_domain
        obj = bpy.data.objects.get(self.name)
        domain = resolve_domain(context)
        if obj is None or domain is None:
            return {"CANCELLED"}
        props = domain.fumaris
        for role, _label in _ROLE_LABELS:
            top = getattr(props, f"{role}_collection")
            if top is None:
                continue
            for collection in [top, *top.children_recursive]:
                if obj.name in collection.objects:
                    if len(obj.users_collection) == 1:
                        # Its only collection: keep it in the scene.
                        context.scene.collection.objects.link(obj)
                    collection.objects.unlink(obj)
        obj.fumaris.smoke_object_type = "none"
        self.report({"INFO"}, f"Removed {obj.name} from {domain.name}")
        return {"FINISHED"}


class FUMARIS_OT_object_delete(bpy.types.Operator):
    bl_idname = "fumaris.object_delete"
    bl_label = "Delete"
    bl_description = "Delete this object from the file"
    bl_options = {"REGISTER", "UNDO"}

    name: bpy.props.StringProperty(options={"HIDDEN"})

    @classmethod
    def description(cls, context, properties):
        obj = bpy.data.objects.get(properties.name)
        if obj is not None and obj.fumaris.smoke_object_type == "domain":
            return ("Delete this domain object. Its collections and the objects in them are kept; "
                    "its baked files stay on disk")
        return "Delete this object"

    @classmethod
    def poll(cls, context):
        if active_job():
            cls.poll_message_set("Stop the active Fumaris simulation first")
            return False
        return True

    def execute(self, context):
        obj = bpy.data.objects.get(self.name)
        if obj is None:
            return {"CANCELLED"}
        name = obj.name
        if getattr(context.scene, "fumaris_preview_domain", None) == obj:
            context.scene.fumaris_preview_domain = None
        bpy.data.objects.remove(obj, do_unlink=True)
        self.report({"INFO"}, f"Deleted {name}")
        return {"FINISHED"}


class FUMARIS_OT_object_rename(bpy.types.Operator):
    bl_idname = "fumaris.object_rename"
    bl_label = "Rename"
    bl_description = "Rename this object"
    bl_options = {"REGISTER", "UNDO"}

    name: bpy.props.StringProperty(options={"HIDDEN"})
    new_name: bpy.props.StringProperty(name="Name")

    def invoke(self, context, event):
        obj = bpy.data.objects.get(self.name)
        if obj is None:
            return {"CANCELLED"}
        self.new_name = obj.name
        return context.window_manager.invoke_props_dialog(self, width=260)

    def draw(self, context):
        self.layout.activate_init = True   # type at once
        self.layout.prop(self, "new_name", text="")

    def execute(self, context):
        obj = bpy.data.objects.get(self.name)
        new_name = self.new_name.strip()
        if obj is None or not new_name:
            return {"CANCELLED"}
        obj.name = new_name
        return {"FINISHED"}


def _button_context_menu(self, context):
    """Right-click on a domain or object in the Fumaris tab: delete it."""
    op = getattr(context, "button_operator", None)
    try:
        identifier = op.bl_rna.identifier if op is not None else ""
    except (AttributeError, ReferenceError):
        return
    if identifier not in {"FUMARIS_OT_domain_choose", "FUMARIS_OT_participant_select"}:
        return
    obj = bpy.data.objects.get(op.name)
    if obj is None:
        return
    layout = self.layout
    layout.separator()
    layout.label(text=obj.name, icon="OBJECT_DATA")
    layout.operator("fumaris.object_rename", text="Rename…", icon="GREASEPENCIL").name = obj.name
    if identifier == "FUMARIS_OT_domain_choose":
        layout.operator("fumaris.object_delete", text="Delete Domain (keeps its collections)",
                        icon="TRASH").name = obj.name
        return
    layout.operator("fumaris.participant_remove", icon="REMOVE").name = obj.name
    layout.operator("fumaris.object_delete", text="Delete Object", icon="TRASH").name = obj.name


def _draw_add_menu(self, context):
    obj = context.object
    if obj is None or obj.fumaris.smoke_object_type == "none":
        self.layout.operator(FUMARIS_OT_quick_setup.bl_idname, text="Fumaris Quick Setup…", icon="MOD_FLUID")


_CLASSES = (FUMARIS_OT_quick_setup, FUMARIS_OT_domain_create, FUMARIS_OT_participants_add_selected,
            FUMARIS_OT_domain_choose, FUMARIS_OT_participant_remove, FUMARIS_OT_object_delete,
            FUMARIS_OT_object_rename)


def register():
    bpy.types.Scene.fumaris_new_domain_name = bpy.props.StringProperty(
        name="Domain Name", description="Name for the next New Domain (empty: Simulation)")
    for cls in _CLASSES:
        bpy.utils.register_class(cls)
    bpy.types.UI_MT_button_context_menu.append(_button_context_menu)
    bpy.types.VIEW3D_MT_add.append(_draw_add_menu)


def unregister():
    bpy.types.VIEW3D_MT_add.remove(_draw_add_menu)
    bpy.types.UI_MT_button_context_menu.remove(_button_context_menu)
    for cls in reversed(_CLASSES):
        bpy.utils.unregister_class(cls)
    del bpy.types.Scene.fumaris_new_domain_name
