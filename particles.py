"""Fumaris Particles: GPU tracers that ride the smoke (F12).

The solver moves them in every simulation step from the live velocity, so they
show in the live preview (drawn into its image) and a bake writes them next to
its VDBs: particles/<prefix>particles_####.ply, one binary PLY point cloud per
frame. A "<domain> Particles" object reads the frame the scene shows with a
Geometry Nodes Import PLY node (no Python handler: renders and render farms
see it like any modifier), turns it into a point cloud for Cycles and leaves
every attribute to Geometry Nodes (instancing, trails, sparkle).

Spawning: in the smoke (smoke and temperature in a range, a rate per second,
spread evenly), and from a collection of source objects: mesh surface or
volume at a rate, or every point of a point cloud / Geometry Nodes output.
Motion: drag toward the gas velocity (0 flies free, 10000 follows exactly),
gravity, colliders (bounce, stick, kill), lifetime, the domain's box.
Attributes of the imported points: velocity (Cycles motion blur), age (s),
temperature (K), smoke (density at the particle), size, random (0..1), id;
Col (smoke colour) with Colour Mixing.

Not yet: particles that emit smoke, two-way coupling, a velocity-only mode
without visible smoke (notes/experiments/particles-2026-11-19/README.md).
"""
import math
import os
import zlib

import bpy
from bpy.props import (BoolProperty, EnumProperty, FloatProperty, FloatVectorProperty, IntProperty,
                       PointerProperty)
from bpy.types import PropertyGroup

try:
    import numpy as np
except ImportError:   # Blender always has numpy; tests without it skip sources
    np = None

GROUP_NAME = "Fumaris Particles"
VERSION = 1
VERSION_KEY = "fumaris_particles_version"
DOMAIN_MARKER = "fumaris_particles_domain"   # on a particle object: its domain
MATERIAL_NAMES = {"TRACERS": "Fumaris Tracer", "EMBERS": "Fumaris Ember"}
# Bytes of GPU memory per particle (Particles.h: state, free flag, list, export record).
DEVICE_BYTES_PER_PARTICLE = 92
# Scales of the PLY's fixed-point properties (src/particles.cpp).
FILE_SCALES = {"age": 0.01, "smoke": 0.001, "size": 0.001, "random": 1.0 / 255.0}
# Spawn points from source objects per frame: more is slow in Python.
MAX_SOURCE_POINTS = 2_000_000

# The previous frame's world matrix of each source object (velocity, motion).
_SOURCE_MATRICES = {}


# ---------------------------------------------------------------- settings

KIND_DEFAULTS = {
    # Glittering tracers in white smoke, like an ink plume.
    "TRACERS": dict(max_particles=1_000_000, smoke_rate=200_000.0, smoke_min=0.05, temperature_min=0.0,
                    drag=10000.0, gravity=0.0, lifetime=5.0, lifetime_random=0.3, cooling_time=0.0,
                    size=0.006, size_random=0.5, collision="BOUNCE", bounce=0.2),
    # Embers thrown out of a fire: heavier than the gas, glowing by temperature.
    "EMBERS": dict(max_particles=200_000, smoke_rate=3_000.0, smoke_min=0.0, temperature_min=1300.0,
                   drag=3.0, gravity=1.0, lifetime=1.5, lifetime_random=0.4, cooling_time=0.6,
                   size=0.012, size_random=0.5, collision="BOUNCE", bounce=0.3),
}


def apply_kind_defaults(settings, kind=None):
    kind = kind or settings.kind
    values = dict(KIND_DEFAULTS[kind])
    if kind == "EMBERS":
        domain = settings.id_data
        flame = float(getattr(getattr(domain, "fumaris", None), "flame_temperature", 2000.0) or 2000.0)
        # As Sparks: burning gas peaks near 0.85 of Flame Temperature.
        values["temperature_min"] = 0.65 * flame
    for name, value in values.items():
        setattr(settings, name, value)


def _kind_updated(self, _context):
    apply_kind_defaults(self)
    _sync_objects(self.id_data)


def _look_updated(self, _context):
    _sync_objects(self.id_data)


class FumarisParticleSettings(PropertyGroup):
    enabled: BoolProperty(
        name="Particles", default=False,
        description="Simulate GPU particles that ride the smoke: in the live preview and in bakes, "
                    "written beside the VDBs and imported as a point cloud")
    show_panel: BoolProperty(name="Particles", default=False, options={"SKIP_SAVE"})
    show_more: BoolProperty(name="More", default=False, options={"SKIP_SAVE"})
    kind: EnumProperty(
        name="Kind",
        items=[("TRACERS", "Tracers", "Light particles that follow the smoke exactly (glitter in an ink plume)"),
               ("EMBERS", "Embers", "Heavy hot particles thrown out of the fire, falling and cooling")],
        default="TRACERS", update=_kind_updated,
        description="Sets good defaults for the kind of particles; each value can be changed after")
    max_particles: IntProperty(
        name="Max Particles", default=1_000_000, min=1000, max=64 * 1024 * 1024, soft_max=10_000_000,
        description="Particles alive at most. Each takes 92 bytes of GPU memory, taken from Simulation Memory. "
                    "Spawns past it are dropped")
    seed: IntProperty(name="Seed", default=0, min=0, description="Another number gives other random particles")
    # spawning in the smoke
    spawn_smoke: BoolProperty(name="From Smoke", default=True,
                              description="Spawn particles in the smoke, spread evenly over the cells in range")
    smoke_rate: FloatProperty(
        name="Rate", default=200_000.0, min=0.0, soft_max=2_000_000.0, step=1000, precision=0,
        description="New particles per simulated second, spread evenly over the smoke in range")
    smoke_min: FloatProperty(name="Smoke Min", default=0.05, min=0.0, soft_max=2.0,
                             description="Spawn only where the smoke density is at least this")
    smoke_max: FloatProperty(name="Smoke Max", default=0.0, min=0.0, soft_max=10.0,
                             description="Spawn only where the smoke density is at most this (0: no limit)")
    temperature_min: FloatProperty(name="Temperature Min", default=0.0, min=0.0, soft_max=4000.0,
                                   description="Spawn only in gas at least this hot, in Kelvin (0: any)")
    temperature_max: FloatProperty(name="Temperature Max", default=0.0, min=0.0, soft_max=4000.0,
                                   description="Spawn only in gas at most this hot, in Kelvin (0: no limit)")
    spawn_jitter: FloatProperty(name="Spread", default=0.0, min=0.0, soft_max=4.0,
                                description="Extra random distance of a new particle from its cell, in cells")
    # spawning from objects
    source_collection: PointerProperty(
        name="Sources", type=bpy.types.Collection,
        description="Objects that emit particles: meshes from their surface or volume, point clouds and "
                    "Geometry Nodes points from every point, each frame")
    source_mode: EnumProperty(
        name="Emit From",
        items=[("SURFACE", "Surface", "Random points on the meshes' faces, Source Rate per second"),
               ("VOLUME", "Volume", "Random points inside the closed meshes, Source Rate per second"),
               ("POINTS", "Points", "One particle per point (vertex, point cloud or Geometry Nodes point) "
                                    "each frame. A 'velocity' attribute sets its speed, 'size' its size")],
        default="SURFACE")
    source_rate: FloatProperty(name="Source Rate", default=2000.0, min=0.0, soft_max=200_000.0, precision=0,
                               description="New particles per second from all source meshes together (Surface, Volume)")
    source_speed: FloatProperty(name="Normal Speed", default=0.0, soft_min=-10.0, soft_max=10.0, unit="VELOCITY",
                                description="Initial speed along the surface normal")
    source_inherit: FloatProperty(name="Inherit Velocity", default=1.0, min=0.0, soft_max=1.0,
                                  description="Share of the source object's own motion new particles keep")
    # motion
    drag: FloatProperty(
        name="Drag", default=10000.0, min=0.0, max=10000.0, soft_max=100.0,
        description="How strongly the gas carries a particle, per second: 0 flies free (a heavy ember), "
                    "10000 follows the smoke exactly (a tracer)")
    gravity: FloatProperty(name="Gravity", default=0.0, soft_min=-1.0, soft_max=2.0,
                           description="Share of the domain's gravity on the particles (negative: they rise)")
    lifetime: FloatProperty(name="Lifetime", default=5.0, min=0.0, soft_max=20.0, unit="TIME_ABSOLUTE",
                            description="Simulated seconds a particle lives (0: until it leaves the domain)")
    lifetime_random: FloatProperty(name="Lifetime Random", default=0.3, min=0.0, max=1.0, subtype="FACTOR",
                                   description="Each particle lives (1 ± this) × Lifetime")
    cooling_time: FloatProperty(
        name="Cooling", default=0.0, min=0.0, soft_max=5.0, unit="TIME_ABSOLUTE",
        description="Seconds in which a particle hotter than the gas loses two thirds of its heat "
                    "(0: it has the gas temperature)")
    collision: EnumProperty(
        name="Colliders",
        items=[("NONE", "Pass", "Particles go through the domain's colliders"),
               ("BOUNCE", "Bounce", "Particles bounce off the colliders"),
               ("STICK", "Stick", "Particles stay where they hit a collider and move with it"),
               ("KILL", "Kill", "Particles die when they hit a collider")],
        default="BOUNCE", description="What the domain's colliders do to the particles")
    bounce: FloatProperty(name="Bounce", default=0.2, min=0.0, max=1.0, subtype="FACTOR",
                          description="Share of the speed into the collider kept, away from it")
    friction: FloatProperty(name="Friction", default=0.1, min=0.0, max=1.0, subtype="FACTOR",
                            description="Share of the speed along the collider lost at each bounce")
    kill_outside: BoolProperty(name="Inside Domain", default=True,
                               description="Remove particles that leave the domain object's box "
                                           "(a domain that is an empty has none)")
    # look
    size: FloatProperty(name="Size", default=0.006, min=0.0, soft_max=0.1, precision=4, unit="LENGTH",
                        update=_look_updated, description="Radius of the rendered points")
    size_random: FloatProperty(name="Size Random", default=0.5, min=0.0, max=1.0, subtype="FACTOR",
                               description="Each particle's size is (1 ± this) × Size")
    color: FloatVectorProperty(name="Color", subtype="COLOR", size=3, min=0.0, max=1.0, default=(1.0, 1.0, 1.0),
                               update=_look_updated, description="Colour of tracers (times the smoke's with Colour Mixing)")
    brightness: FloatProperty(name="Brightness", default=0.1, min=0.0, soft_max=1.0, update=_look_updated,
                              description="Glow of embers (temperature × this / 100), or of tracers")
    show_in_preview: BoolProperty(name="Show in Preview", default=True,
                                  description="Draw the particles over the live preview")


# ---------------------------------------------------------------- frames

def settings_of(domain):
    return getattr(domain, "fumaris_particles", None)


def memory_bytes(domain):
    settings = settings_of(domain)
    if settings is None or not settings.enabled:
        return 0
    return int(settings.max_particles) * DEVICE_BYTES_PER_PARTICLE


def _domain_bounds(domain, depsgraph):
    evaluated = domain.evaluated_get(depsgraph)
    from mathutils import Vector
    corners = [evaluated.matrix_world @ Vector(corner) for corner in evaluated.bound_box]
    low = [min(c[i] for c in corners) for i in range(3)]
    high = [max(c[i] for c in corners) for i in range(3)]
    return low, high


def _rng(seed, frame, name):
    return np.random.default_rng([int(seed), int(frame) & 0x7FFFFFFF, zlib.crc32(name.encode("utf-8"))])


def _count_this_frame(rate, frame, fps, share=1.0):
    """Particles a source spawns this frame: the whole number of particles due
    by its end minus those due by the frame before (no drift, deterministic)."""
    per_frame = max(0.0, rate) * share / max(fps, 1e-6)
    return max(0, int(math.floor(per_frame * frame)) - int(math.floor(per_frame * (frame - 1))))


def _matrix_np(matrix):
    return np.array([list(row) for row in matrix], dtype=np.float64)


def _mesh_triangles(evaluated):
    mesh = evaluated.to_mesh()
    try:
        mesh.calc_loop_triangles()
        count = len(mesh.loop_triangles)
        if count == 0:
            return None
        verts = np.empty(len(mesh.vertices) * 3, dtype=np.float64)
        mesh.vertices.foreach_get("co", verts)
        tris = np.empty(count * 3, dtype=np.int64)
        mesh.loop_triangles.foreach_get("vertices", tris)
        return verts.reshape(-1, 3), tris.reshape(-1, 3)
    finally:
        evaluated.to_mesh_clear()


def _surface_samples(verts, tris, count, rng):
    a, b, c = verts[tris[:, 0]], verts[tris[:, 1]], verts[tris[:, 2]]
    cross = np.cross(b - a, c - a)
    area = np.linalg.norm(cross, axis=1)
    total = area.sum()
    if total <= 0.0:
        return None, None
    pick = rng.choice(len(tris), size=count, p=area / total)
    r1 = np.sqrt(rng.random(count))
    r2 = rng.random(count)
    points = (1 - r1)[:, None] * a[pick] + (r1 * (1 - r2))[:, None] * b[pick] + (r1 * r2)[:, None] * c[pick]
    normals = cross[pick] / np.maximum(area[pick], 1e-12)[:, None]
    return points, normals


def _volume_samples(verts, tris, count, rng):
    """Points inside a closed mesh: candidates in its box kept when the
    nearest face looks away from them (mathutils BVH, one point at a time)."""
    from mathutils import Vector
    from mathutils.bvhtree import BVHTree
    tree = BVHTree.FromPolygons([Vector(v) for v in verts], [tuple(t) for t in tris])
    low, high = verts.min(axis=0), verts.max(axis=0)
    kept = []
    tries = 0
    while len(kept) < count and tries < 8:
        tries += 1
        candidates = low + rng.random((max(64, 2 * (count - len(kept))), 3)) * (high - low)
        for point in candidates:
            location, normal, _index, _distance = tree.find_nearest(Vector(point))
            if location is not None and (Vector(point) - location).dot(normal) < 0.0:
                kept.append(point)
                if len(kept) >= count:
                    break
    if not kept:
        return None
    return np.array(kept, dtype=np.float64)


def _evaluated_points(obj, depsgraph):
    """World positions (and velocities, sizes when the geometry has them) of
    every point an object's evaluated geometry holds: mesh vertices, point
    clouds, Geometry Nodes output (instances included)."""
    positions, velocities, sizes = [], [], []
    for instance in depsgraph.object_instances:
        source = instance.parent.original if instance.is_instance and instance.parent else instance.object.original
        if source != obj:
            continue
        data = getattr(instance.object, "data", None)
        if not isinstance(data, (bpy.types.Mesh, bpy.types.PointCloud)):
            continue
        attributes = data.attributes
        position = attributes.get("position")
        if position is None or len(position.data) == 0:
            continue
        count = len(position.data)
        local = np.empty(count * 3, dtype=np.float64)
        position.data.foreach_get("vector", local)
        matrix = _matrix_np(instance.matrix_world)
        positions.append(local.reshape(-1, 3) @ matrix[:3, :3].T + matrix[:3, 3])
        velocity = attributes.get("velocity")
        if velocity is not None and velocity.data_type == "FLOAT_VECTOR" and velocity.domain == "POINT":
            values = np.empty(count * 3, dtype=np.float64)
            velocity.data.foreach_get("vector", values)
            velocities.append(values.reshape(-1, 3) @ matrix[:3, :3].T)
        else:
            velocities.append(np.zeros((count, 3)))
        size = attributes.get("size")
        if size is not None and size.data_type == "FLOAT" and size.domain == "POINT":
            values = np.empty(count, dtype=np.float64)
            size.data.foreach_get("value", values)
            sizes.append(values)
        else:
            sizes.append(np.ones(count))
    if not positions:
        return None
    return np.concatenate(positions), np.concatenate(velocities), np.concatenate(sizes)


def _source_objects(settings):
    collection = settings.source_collection
    if collection is None:
        return []
    return sorted({obj for obj in collection.all_objects if obj.type in {"MESH", "POINTCLOUD", "CURVES", "EMPTY"}},
                  key=lambda obj: obj.name)


def _source_spawns(settings, depsgraph, frame, fps, scene_speed):
    """This frame's spawn points from the source collection: positions,
    velocities (scene units per second), times (0..1 in the frame), sizes."""
    if np is None:
        return None
    objects = _source_objects(settings)
    if not objects:
        return None
    parts = []
    meshes = [obj for obj in objects if obj.type == "MESH"] if settings.source_mode != "POINTS" else []
    # Surface and Volume: the rate is shared by the meshes by their area.
    shares = {}
    triangles = {}
    for obj in meshes:
        evaluated = obj.evaluated_get(depsgraph)
        found = _mesh_triangles(evaluated)
        if found is None:
            continue
        verts, tris = found
        triangles[obj.name] = (verts, tris, _matrix_np(evaluated.matrix_world))
        a, b, c = verts[tris[:, 0]], verts[tris[:, 1]], verts[tris[:, 2]]
        shares[obj.name] = float(np.linalg.norm(np.cross(b - a, c - a), axis=1).sum())
    total_share = sum(shares.values()) or 1.0
    for obj in objects:
        rng = _rng(settings.seed, frame, obj.name)
        name = obj.name
        if name in triangles:
            verts, tris, matrix = triangles[name]
            count = _count_this_frame(settings.source_rate, frame, fps, shares[name] / total_share)
            count = min(count, MAX_SOURCE_POINTS)
            if count <= 0:
                continue
            normals = None
            if settings.source_mode == "VOLUME":
                local = _volume_samples(verts, tris, count, rng)
            else:
                local, normals = _surface_samples(verts, tris, count, rng)
            if local is None:
                continue
            count = len(local)
            world = local @ matrix[:3, :3].T + matrix[:3, 3]
            times = rng.random(count)
            velocity = np.zeros((count, 3))
            previous = _SOURCE_MATRICES.get(name)
            if previous is not None and previous[0] == frame - 1:
                before = local @ previous[1][:3, :3].T + previous[1][:3, 3]
                velocity += settings.source_inherit * (world - before) * fps
                # where the surface was at each particle's moment in the frame
                world = before + (world - before) * times[:, None]
            if normals is not None and settings.source_speed != 0.0:
                world_normals = normals @ np.linalg.inv(matrix[:3, :3])
                world_normals /= np.maximum(np.linalg.norm(world_normals, axis=1), 1e-12)[:, None]
                velocity += settings.source_speed * world_normals
            parts.append((world, velocity, times, np.ones(count)))
        elif obj.type != "MESH" or settings.source_mode == "POINTS":
            found = _evaluated_points(obj, depsgraph)
            if found is None:
                continue
            world, velocity, sizes = found
            if len(world) > MAX_SOURCE_POINTS:
                world, velocity, sizes = world[:MAX_SOURCE_POINTS], velocity[:MAX_SOURCE_POINTS], sizes[:MAX_SOURCE_POINTS]
            parts.append((world, velocity, rng.random(len(world)), sizes))
        _SOURCE_MATRICES[name] = (frame, _matrix_np(obj.evaluated_get(depsgraph).matrix_world))
    if not parts:
        return None
    return tuple(np.concatenate([part[i] for part in parts]) for i in range(4))


def frame_state(domain, depsgraph, payload, frame, append_array):
    """The frame packet's "particles" section, or None when they are off."""
    settings = settings_of(domain)
    if settings is None or not settings.enabled:
        return None
    scene = bpy.context.scene
    fps = scene.render.fps / scene.render.fps_base
    low, high = _domain_bounds(domain, depsgraph)
    # An empty domain (unbounded sparse grid) has no box to keep them in.
    boxed = domain.type == "MESH" and all(h - l > 1e-6 for l, h in zip(low, high))
    kelvin_max = settings.temperature_max if settings.temperature_max > 0.0 else 1e30
    state = {
        "enabled": True,
        "max_particles": int(settings.max_particles),
        "seed": int(settings.seed),
        "smoke_rate": float(settings.smoke_rate) if settings.spawn_smoke else 0.0,
        "smoke_min": float(settings.smoke_min),
        "smoke_max": float(settings.smoke_max) if settings.smoke_max > 0.0 else 1e30,
        "temperature_min": float(settings.temperature_min),
        "temperature_max": float(kelvin_max),
        "spawn_jitter": float(settings.spawn_jitter),
        "drag": float(settings.drag),
        "gravity": float(settings.gravity),
        "lifetime": float(settings.lifetime),
        "lifetime_random": float(settings.lifetime_random),
        "size_random": float(settings.size_random),
        "cooling_time": float(settings.cooling_time),
        "collision": settings.collision.lower(),
        "bounce": float(settings.bounce),
        "friction": float(settings.friction),
        "kill_outside": bool(settings.kill_outside) and boxed,
        "bounds_min": [float(v) for v in low],
        "bounds_max": [float(v) for v in high],
        "draw": ("glow" if settings.kind == "EMBERS" else "color") if settings.show_in_preview else "off",
        "draw_color": [float(v) for v in settings.color],
        "write_cache": False,
    }
    spawns = _source_spawns(settings, depsgraph, frame, fps, 1.0)
    if spawns is not None:
        positions, velocities, times, sizes = spawns
        state["spawn_positions"] = append_array(payload, np.ascontiguousarray(positions, dtype=np.float32).ravel())
        state["spawn_velocities"] = append_array(payload, np.ascontiguousarray(velocities, dtype=np.float32).ravel())
        state["spawn_times"] = append_array(payload, np.ascontiguousarray(times, dtype=np.float32))
        state["spawn_sizes"] = append_array(payload, np.ascontiguousarray(sizes, dtype=np.float32))
    return state


# ---------------------------------------------------------------- import

def cache_directory(output_directory):
    return os.path.join(output_directory, "particles")


def frame_files(output_directory, prefix):
    directory = cache_directory(output_directory)
    try:
        names = os.listdir(directory)
    except OSError:
        return []
    head = f"{prefix}particles_"
    return sorted(os.path.join(directory, name) for name in names
                  if name.startswith(head) and name.endswith(".ply"))


def _socket(tree, name, kind, default=None, minimum=None, description=""):
    item = tree.interface.new_socket(name, in_out="INPUT", socket_type=kind, description=description)
    if default is not None:
        item.default_value = default
    if minimum is not None and hasattr(item, "min_value"):
        item.min_value = minimum
    return item


def _math(tree, operation, a, b, location):
    node = tree.nodes.new("ShaderNodeMath")
    node.operation = operation
    node.location = location
    for index, value in enumerate((a, b)):
        if isinstance(value, (int, float)):
            node.inputs[index].default_value = value
        else:
            tree.links.new(value, node.inputs[index])
    return node.outputs[0]


def _named(tree, name, location, data_type="FLOAT"):
    node = tree.nodes.new("GeometryNodeInputNamedAttribute")
    node.data_type = data_type
    node.inputs["Name"].default_value = name
    node.location = location
    return node.outputs["Attribute"]


def _store(tree, geometry, name, value, location, data_type="FLOAT"):
    node = tree.nodes.new("GeometryNodeStoreNamedAttribute")
    node.data_type = data_type
    node.inputs["Name"].default_value = name
    node.location = location
    tree.links.new(geometry, node.inputs["Geometry"])
    tree.links.new(value, node.inputs["Value"])
    return node.outputs["Geometry"]


def build_group():
    """The shared node group: the shown frame's PLY as a point cloud."""
    tree = next((group for group in bpy.data.node_groups
                 if group.bl_idname == "GeometryNodeTree" and group.get(VERSION_KEY) == VERSION), None)
    if tree is not None:
        return tree
    tree = bpy.data.node_groups.new(GROUP_NAME, "GeometryNodeTree")
    tree.interface.new_socket("Geometry", in_out="OUTPUT", socket_type="NodeSocketGeometry")
    _socket(tree, "Directory", "NodeSocketString", "",
            description="Folder of the particle cache (the bake's particles folder). Fumaris keeps it on the bake the domain shows")
    _socket(tree, "Prefix", "NodeSocketString", "fumaris_", description="File name prefix of the bake")
    _socket(tree, "Frame Offset", "NodeSocketInt", 0, description="Shows the cache this many frames later")
    _socket(tree, "Size", "NodeSocketFloat", 0.006, 0.0, "Radius of the points (times each particle's size)")
    _socket(tree, "Material", "NodeSocketMaterial", description="Material of the points")
    nodes = tree.nodes
    group_in = nodes.new("NodeGroupInput")
    group_in.location = (-1200, 0)
    group_out = nodes.new("NodeGroupOutput")
    group_out.location = (1600, 0)

    time = nodes.new("GeometryNodeInputSceneTime")
    time.location = (-1200, -300)
    floor = _math(tree, "FLOOR", time.outputs["Frame"], 0.0, (-1000, -300))
    shifted = _math(tree, "SUBTRACT", floor, group_in.outputs["Frame Offset"], (-850, -300))
    to_int = nodes.new("FunctionNodeFloatToInt")
    to_int.location = (-700, -300)
    tree.links.new(shifted, to_int.inputs[0])
    path = nodes.new("FunctionNodeFormatString")
    path.location = (-500, -100)
    path.format_items.new("STRING", "dir")
    path.format_items.new("STRING", "prefix")
    path.format_items.new("INT", "frame")
    path.inputs["Format"].default_value = "{dir}/{prefix}particles_{frame:04}.ply"
    tree.links.new(group_in.outputs["Directory"], path.inputs["dir"])
    tree.links.new(group_in.outputs["Prefix"], path.inputs["prefix"])
    tree.links.new(to_int.outputs[0], path.inputs["frame"])
    read = nodes.new("GeometryNodeImportPLY")
    read.location = (-300, 0)
    tree.links.new(path.outputs["String"], read.inputs["Path"])
    points = nodes.new("GeometryNodeMeshToPoints")
    points.location = (-100, 0)
    tree.links.new(read.outputs[0], points.inputs["Mesh"])
    geometry = points.outputs["Points"]

    velocity = nodes.new("ShaderNodeCombineXYZ")
    velocity.location = (0, -400)
    for axis, name in zip("XYZ", ("vx", "vy", "vz")):
        tree.links.new(_named(tree, name, (-200, -400 - 120 * "XYZ".index(axis))), velocity.inputs[axis])
    # Cycles blurs points along "velocity" (scene units per second).
    geometry = _store(tree, geometry, "velocity", velocity.outputs["Vector"], (150, 0), "FLOAT_VECTOR")
    x = 300
    for name, scale in FILE_SCALES.items():
        geometry = _store(tree, geometry, name, _math(tree, "MULTIPLY", _named(tree, name, (x - 150, -700)), scale, (x, -700)),
                          (x, 0))
        x += 150
    identifier = _named(tree, "id", (x, -500))
    to_id = nodes.new("FunctionNodeFloatToInt")
    to_id.location = (x + 100, -500)
    tree.links.new(identifier, to_id.inputs[0])
    geometry = _store(tree, geometry, "id", to_id.outputs[0], (x + 150, 0), "INT")
    remove = nodes.new("GeometryNodeRemoveAttribute")
    remove.location = (x + 300, 0)
    remove.inputs["Name"].default_value = "v?"
    if "Pattern Mode" in remove.inputs:
        remove.inputs["Pattern Mode"].default_value = "Wildcard"
    tree.links.new(geometry, remove.inputs["Geometry"])
    radius = nodes.new("GeometryNodeSetPointRadius")
    radius.location = (x + 500, 0)
    tree.links.new(remove.outputs["Geometry"], radius.inputs["Points"])
    tree.links.new(_math(tree, "MULTIPLY", group_in.outputs["Size"], _named(tree, "size", (x + 300, -300)), (x + 450, -300)),
                   radius.inputs["Radius"])
    material = nodes.new("GeometryNodeSetMaterial")
    material.location = (x + 700, 0)
    tree.links.new(radius.outputs["Points"], material.inputs["Geometry"])
    tree.links.new(group_in.outputs["Material"], material.inputs["Material"])
    tree.links.new(material.outputs["Geometry"], group_out.inputs["Geometry"])
    tree[VERSION_KEY] = VERSION
    return tree


def build_material(kind):
    """The shared material of tracers (glittering white) or embers (glowing by temperature)."""
    material = next((item for item in bpy.data.materials
                     if item.get(VERSION_KEY) == VERSION and item.get("fumaris_particles_kind") == kind), None)
    if material is not None:
        return material
    material = bpy.data.materials.new(MATERIAL_NAMES[kind])
    material.use_nodes = True
    tree = material.node_tree
    tree.nodes.clear()
    output = tree.nodes.new("ShaderNodeOutputMaterial")
    output.location = (600, 0)
    if kind == "EMBERS":
        temperature = tree.nodes.new("ShaderNodeAttribute")
        temperature.attribute_name = "temperature"
        temperature.location = (-600, 0)
        blackbody = tree.nodes.new("ShaderNodeBlackbody")
        blackbody.location = (-300, 100)
        emission = tree.nodes.new("ShaderNodeEmission")
        emission.location = (200, 0)
        strength = tree.nodes.new("ShaderNodeMath")
        strength.operation = "MULTIPLY"
        strength.location = (-300, -150)
        strength.inputs[1].default_value = 0.01
        scaled = tree.nodes.new("ShaderNodeMath")
        scaled.operation = "MULTIPLY"
        scaled.location = (-100, -150)
        scaled.inputs[1].default_value = 0.1
        scaled.label = "Brightness"
        tree.links.new(temperature.outputs["Fac"], blackbody.inputs["Temperature"])
        tree.links.new(blackbody.outputs["Color"], emission.inputs["Color"])
        tree.links.new(temperature.outputs["Fac"], strength.inputs[0])
        tree.links.new(strength.outputs[0], scaled.inputs[0])
        tree.links.new(scaled.outputs[0], emission.inputs["Strength"])
        tree.links.new(emission.outputs["Emission"], output.inputs["Surface"])
    else:
        bsdf = tree.nodes.new("ShaderNodeBsdfPrincipled")
        bsdf.location = (200, 0)
        bsdf.inputs["Base Color"].default_value = (1.0, 1.0, 1.0, 1.0)
        bsdf.inputs["Metallic"].default_value = 1.0
        bsdf.inputs["Roughness"].default_value = 0.2
        # Glitter: each flake catches the light at its own angle.
        rand = tree.nodes.new("ShaderNodeAttribute")
        rand.attribute_name = "random"
        rand.location = (-500, -200)
        sparkle = tree.nodes.new("ShaderNodeMapRange")
        sparkle.location = (-250, -200)
        sparkle.inputs["To Min"].default_value = 0.05
        sparkle.inputs["To Max"].default_value = 0.45
        tree.links.new(rand.outputs["Fac"], sparkle.inputs["Value"])
        tree.links.new(sparkle.outputs["Result"], bsdf.inputs["Roughness"])
        emission = tree.nodes.new("ShaderNodeRGB")
        emission.location = (-250, 150)
        emission.outputs[0].default_value = (1.0, 1.0, 1.0, 1.0)
        emission.label = "Color"
        tree.links.new(emission.outputs[0], bsdf.inputs["Base Color"])
        tree.links.new(emission.outputs[0], bsdf.inputs["Emission Color"])
        bsdf.inputs["Emission Strength"].default_value = 0.0
        tree.links.new(bsdf.outputs["BSDF"], output.inputs["Surface"])
    material[VERSION_KEY] = VERSION
    material["fumaris_particles_kind"] = kind
    return material


def _apply_look(material, settings):
    """The domain's Color and Brightness on its material (a copy per domain)."""
    tree = material.node_tree
    if settings.kind == "EMBERS":
        node = next((n for n in tree.nodes if n.label == "Brightness"), None)
        if node is not None:
            node.inputs[1].default_value = settings.brightness
    else:
        rgb = next((n for n in tree.nodes if n.label == "Color"), None)
        if rgb is not None:
            rgb.outputs[0].default_value = (*settings.color, 1.0)
        bsdf = next((n for n in tree.nodes if n.type == "BSDF_PRINCIPLED"), None)
        if bsdf is not None:
            bsdf.inputs["Emission Strength"].default_value = settings.brightness if settings.brightness > 0.1 else 0.0


def _input(modifier, name):
    """A value of the group on one object's modifier, by its name (as sparks._input)."""
    item = next((item for item in modifier.node_group.interface.items_tree
                 if item.item_type == "SOCKET" and item.in_out == "INPUT" and item.name == name), None)
    return getattr(modifier.properties.inputs, item.identifier, None) if item else None


def _set_input(modifier, name, value):
    item = _input(modifier, name)
    if item is not None and item.value != value:
        item.value = value


def particle_objects(domain):
    return [obj for obj in bpy.data.objects if obj.get(DOMAIN_MARKER) == domain]


def _modifier(obj):
    return next((m for m in obj.modifiers if m.type == "NODES" and m.node_group is not None
                 and m.node_group.get(VERSION_KEY)), None)


def _material_for(domain, settings):
    """The domain's own copy of its kind's material."""
    name = f"{domain.name} {MATERIAL_NAMES[settings.kind]}"
    material = bpy.data.materials.get(name)
    if material is None or material.get("fumaris_particles_kind") != settings.kind:
        material = build_material(settings.kind).copy()
        material.name = name
    _apply_look(material, settings)
    return material


def _sync_objects(domain):
    settings = settings_of(domain)
    if settings is None:
        return
    for obj in particle_objects(domain):
        modifier = _modifier(obj)
        if modifier is None:
            continue
        _set_input(modifier, "Size", float(settings.size))
        _set_input(modifier, "Material", _material_for(domain, settings))
        obj.update_tag()


def add_particle_object(context, domain, directory):
    """The point-cloud object reading the domain's particle cache in `directory`."""
    settings = settings_of(domain)
    tree = build_group()
    mesh = bpy.data.meshes.new(f"{domain.name} Particles")
    obj = bpy.data.objects.new(f"{domain.name} Particles", mesh)
    collections = [c for c in domain.users_collection if c.name in context.scene.collection.children_recursive or
                   c == context.scene.collection]
    (collections[0] if collections else context.scene.collection).objects.link(obj)
    modifier = obj.modifiers.new(GROUP_NAME, "NODES")
    modifier.node_group = tree
    obj[DOMAIN_MARKER] = domain
    _set_input(modifier, "Directory", cache_directory(directory))
    _set_input(modifier, "Prefix", domain.fumaris.output_prefix or "fumaris_")
    _set_input(modifier, "Size", float(settings.size) if settings else 0.006)
    _set_input(modifier, "Material", _material_for(domain, settings))
    return obj


def follow_bakes(context=None):
    """Every domain with particles reads the particle cache of the bake it
    shows: its particle object is pointed there, made when the bake has one."""
    from .utils import peek_output_directory_for_object
    context = context or bpy.context
    for domain in [obj for obj in bpy.data.objects if getattr(obj, "fumaris", None) is not None
                   and obj.fumaris.smoke_object_type == "domain"]:
        directory = peek_output_directory_for_object(domain)
        objects = particle_objects(domain)
        if not directory:
            continue
        directory = os.path.normpath(directory)
        has_files = bool(frame_files(directory, domain.fumaris.output_prefix or "fumaris_"))
        if not objects and has_files and settings_of(domain) is not None:
            try:
                objects = [add_particle_object(context, domain, directory)]
            except Exception as error:   # never break an import
                print(f"Fumaris particles: could not add the particle object: {error}")
                continue
        for obj in objects:
            modifier = _modifier(obj)
            if modifier is not None:
                _set_input(modifier, "Directory", cache_directory(directory))
                _set_input(modifier, "Prefix", domain.fumaris.output_prefix or "fumaris_")
                obj.update_tag()


# ---------------------------------------------------------------- panel

def draw_domain_section(layout, props, section, pair, nested_foldout, is_baking):
    """The domain panel's Particles section (ui._draw_domain)."""
    domain = props.id_data
    settings = settings_of(domain)
    if settings is None:
        return
    box = layout.box()
    box.enabled = not is_baking
    row = box.row(align=True)
    row.alignment = "LEFT"
    row.prop(settings, "show_panel", text="", icon="TRIA_DOWN" if settings.show_panel else "TRIA_RIGHT", emboss=False)
    row.prop(settings, "enabled", text="")
    row.label(text="Particles", icon="PARTICLES")
    if not settings.show_panel:
        return
    column = box.column(align=True)
    column.active = settings.enabled
    column.row(align=True).prop(settings, "kind", expand=True)
    column.separator(factor=0.5)
    limits = pair(column)
    limits.prop(settings, "max_particles")
    limits.prop(settings, "seed")
    column.label(text=f"GPU memory: {memory_bytes(domain) / 2**20:.0f} MB" if settings.enabled else
                 f"GPU memory when on: {settings.max_particles * DEVICE_BYTES_PER_PARTICLE / 2**20:.0f} MB")

    column.separator()
    smoke = column.row(align=True)
    smoke.prop(settings, "spawn_smoke")
    rate = smoke.row(align=True)
    rate.active = settings.spawn_smoke
    rate.prop(settings, "smoke_rate")
    if settings.spawn_smoke:
        ranges = pair(column)
        ranges.prop(settings, "smoke_min", text="Smoke Min")
        ranges.prop(settings, "smoke_max", text="Max")
        heat = pair(column)
        heat.prop(settings, "temperature_min", text="Heat Min (K)")
        heat.prop(settings, "temperature_max", text="Max")
    column.separator(factor=0.5)
    column.prop(settings, "source_collection")
    if settings.source_collection is not None:
        source = pair(column)
        source.prop(settings, "source_mode", text="")
        sub = source.row(align=True)
        sub.active = settings.source_mode != "POINTS"
        sub.prop(settings, "source_rate", text="Rate")
        speeds = pair(column)
        speeds.prop(settings, "source_speed")
        speeds.prop(settings, "source_inherit", text="Inherit")

    column.separator()
    motion = pair(column)
    motion.prop(settings, "drag")
    motion.prop(settings, "gravity")
    life = pair(column)
    life.prop(settings, "lifetime")
    life.prop(settings, "lifetime_random", text="Random")
    hits = pair(column)
    hits.prop(settings, "collision", text="")
    bounce = hits.row(align=True)
    bounce.active = settings.collision == "BOUNCE"
    bounce.prop(settings, "bounce")
    column.prop(settings, "kill_outside")

    column.separator()
    look = pair(column)
    look.prop(settings, "size")
    look.prop(settings, "size_random", text="Random")
    if settings.kind == "EMBERS":
        column.prop(settings, "brightness")
    else:
        column.prop(settings, "color")
    column.prop(settings, "show_in_preview")
    more, opened = nested_foldout(column, settings, "show_more", "More")
    if opened:
        more.prop(settings, "cooling_time")
        more.prop(settings, "friction")
        more.prop(settings, "spawn_jitter")
        more.prop(settings, "brightness")
    objects = particle_objects(domain)
    if objects:
        column.label(text=f"Shown by '{objects[0].name}'", icon="OUTLINER_OB_POINTCLOUD")


CLASSES = (FumarisParticleSettings,)


def register():
    for cls in CLASSES:
        bpy.utils.register_class(cls)
    bpy.types.Object.fumaris_particles = PointerProperty(type=FumarisParticleSettings)


def unregister():
    if hasattr(bpy.types.Object, "fumaris_particles"):
        del bpy.types.Object.fumaris_particles
    for cls in reversed(CLASSES):
        bpy.utils.unregister_class(cls)
