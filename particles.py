"""Fumaris Particles: GPU tracers that ride the smoke (F12), as domain modifiers.

A "Spawn Particles" modifier in the domain's Modifiers stack (detail_stack.py)
is one kind of particle: where it spawns (in the smoke, spread by the
modifier's masks, the same field masks the Detail & Shaping modifiers use;
and/or from a collection of objects), how it moves (drag, gravity, lifetime,
colliders) and how it looks (size, colour, material). A domain holds up to
four, e.g. tracers in cool smoke and embers from the fire; each particle
carries its modifier's index (its spawn group). What all of them share is the
domain's particle system (FumarisParticleSettings on the domain object: Max
Particles, Seed, Inside Domain, the preview's share), drawn under the stack.

The solver moves them in every simulation step from the live velocity, so they
show in the live preview (drawn into its image) and a bake writes them next to
its VDBs: particles/<prefix>particles_####.ply, one binary PLY point cloud per
frame, with a "group" attribute (the modifier's index in the bake). Each
modifier gets a "<domain> <Kind>" object that reads the frame the scene shows
with a Geometry Nodes Import PLY node, keeps its group's points (no Python
handler: renders and render farms see it like any modifier), turns them into a
point cloud for Cycles and leaves every attribute to Geometry Nodes.

Attributes of the imported points: velocity (Cycles motion blur), age (s),
temperature (K), smoke (density at the particle), size, random (0..1), id,
group; Col (smoke colour) with Colour Mixing.

Files with Particles v1 (one system on the domain object) are migrated on load
(migrate_domain): the same particles, now one modifier.
"""
import math
import os
import zlib

import bpy
from bpy.app.handlers import persistent
from bpy.props import (BoolProperty, EnumProperty, FloatProperty, FloatVectorProperty, IntProperty,
                       PointerProperty)
from bpy.types import PropertyGroup

try:
    import numpy as np
except ImportError:   # Blender always has numpy; tests without it skip sources
    np = None

GROUP_NAME = "Fumaris Particles"
VERSION = 2          # node group: 2 keeps one spawn group's points ("Group")
VERSION_KEY = "fumaris_particles_version"
DOMAIN_MARKER = "fumaris_particles_domain"   # on a particle object: its domain
UID_MARKER = "fumaris_particles_uid"         # on a particle object: its modifier
MATERIAL_NAMES = {"TRACERS": "Fumaris Tracer", "EMBERS": "Fumaris Ember"}
KIND_LABELS = {"TRACERS": "Tracers", "EMBERS": "Embers"}
# Spawn Particles modifiers per domain (spawn groups the bridge carries).
MAX_GROUPS = 4
# Particles v1 settings moved into modifiers (FumarisParticleSettings.migrated).
MIGRATED = 2
# Bytes of GPU memory per particle (Particles.h: state, free flag, list, export record).
DEVICE_BYTES_PER_PARTICLE = 92
# Scales of the PLY's fixed-point properties (src/particles.cpp).
FILE_SCALES = {"age": 0.01, "smoke": 0.001, "size": 0.001, "random": 1.0 / 255.0}
# Spawn points from source objects per frame.
MAX_SOURCE_POINTS = 2_000_000

# The previous frame's world matrix of each source object (velocity, motion).
_SOURCE_MATRICES = {}


# ---------------------------------------------------------------- settings

KIND_DEFAULTS = {
    # Glittering tracers in white smoke, like an ink plume.
    "TRACERS": dict(rate=200_000.0, drag=10000.0, gravity=0.0, lifetime=5.0, lifetime_random=0.3,
                    cooling_time=0.0, size=0.006, size_random=0.5, collision="BOUNCE", bounce=0.2,
                    brightness=6.0),
    # Embers thrown out of a fire: heavier than the gas, glowing by temperature.
    "EMBERS": dict(rate=3_000.0, drag=3.0, gravity=1.0, lifetime=1.5, lifetime_random=0.4,
                   cooling_time=0.6, size=0.012, size_random=0.5, collision="BOUNCE", bounce=0.3,
                   brightness=0.1),
}
# Max Particles a domain starts with for its first modifier of a kind.
KIND_MAX_PARTICLES = {"TRACERS": 1_000_000, "EMBERS": 200_000}


def default_masks(kind, flame_kelvin):
    """Where a new modifier of this kind spawns in the smoke (editable masks)."""
    if kind == "EMBERS":
        # In burning gas, as Sparks: it peaks near 0.85 of Flame Temperature.
        return [{"field": "TEMPERATURE", "range": [0.6 * flame_kelvin, 0.7 * flame_kelvin], "values": [0.0, 1.0]}]
    # Tracers: evenly in the visible smoke, fading in from thin wisps.
    return [{"field": "SMOKE", "range": [0.05, 0.1], "values": [0.0, 1.0]}]


def _flame(domain):
    return float(getattr(getattr(domain, "fumaris", None), "flame_temperature", 2000.0) or 2000.0)


def _mask_list(layer):
    return [{"field": m.field, "blend": m.blend, "range": [round(m.range[0], 4), round(m.range[1], 4)],
             "values": [round(m.values[0], 4), round(m.values[1], 4)]} for m in layer.masks]


def _plain_masks(masks):
    from . import detail_stack
    out = []
    for mask in masks:
        entry = {"field": mask["field"], "blend": mask.get("blend", "MULTIPLY"),
                 "range": [round(float(v), 4) for v in mask.get("range", detail_stack.FIELD_DEFAULT_RANGES.get(mask["field"], (0.0, 1.0)))],
                 "values": [round(float(v), 4) for v in mask.get("values", (0.0, 1.0))]}
        out.append(entry)
    return out


def apply_kind_defaults(spawn, kind=None):
    """A kind's motion and look on a modifier; its masks too while they are
    still the other kind's defaults (edited masks stay)."""
    kind = kind or spawn.kind
    for name, value in KIND_DEFAULTS[kind].items():
        setattr(spawn, name, value)
    layer = owner_layer(spawn)
    if layer is None:
        return
    domain = spawn.id_data
    flame = _flame(domain)
    other = "TRACERS" if kind == "EMBERS" else "EMBERS"
    if not len(layer.masks) or _mask_list(layer) == _plain_masks(default_masks(other, flame)):
        from . import detail_stack
        layer.masks.clear()
        for mask in default_masks(kind, flame):
            detail_stack.add_mask(layer, **mask)


def owner_layer(spawn):
    """The Modifiers stack entry a modifier's particle settings belong to."""
    domain = spawn.id_data
    for layer in getattr(getattr(domain, "fumaris", None), "detail_layers", ()):
        if layer.particles == spawn:
            return layer
    return None


def _kind_updated(self, _context):
    apply_kind_defaults(self)
    _sync_objects(self.id_data)


def _look_updated(self, _context):
    _sync_objects(self.id_data)


class FumarisSpawnSettings(PropertyGroup):
    """One Spawn Particles modifier (a Modifiers stack entry's particles)."""
    uid: IntProperty(name="Identifier", default=0, min=0, options={"HIDDEN"},
                     description="Ties the modifier to the objects that show its particles")
    kind: EnumProperty(
        name="Kind",
        items=[("TRACERS", "Tracers", "Light particles that follow the smoke exactly (glitter in an ink plume)",
                "PARTICLES", 0),
               ("EMBERS", "Embers", "Heavy hot particles thrown out of the fire, falling and cooling",
                "LIGHT_SUN", 1)],
        default="TRACERS", update=_kind_updated,
        description="Sets good defaults for the kind of particles; each value can be changed after")
    spawn_smoke: BoolProperty(
        name="Smoke", default=True,
        description="Spawn in the smoke, spread over it by the masks below (denser where they give more)")
    spawn_objects: BoolProperty(
        name="Objects", default=False,
        description="Spawn from a collection of objects: mesh surfaces or volumes, point clouds, "
                    "Geometry Nodes points")
    rate: FloatProperty(
        name="Rate", default=200_000.0, min=0.0, soft_max=2_000_000.0, step=1000, precision=0,
        description="New particles per simulated second in the smoke, spread over it by the masks")
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
    mask_objects: BoolProperty(
        name="Mask Objects", default=False,
        description="The masks also thin out the particles from objects: each is kept with the masks' "
                    "factor where it starts (0 to 1)")
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
    # look
    size: FloatProperty(name="Size", default=0.006, min=0.0, soft_max=0.1, precision=4, unit="LENGTH",
                        update=_look_updated, description="Radius of the rendered points")
    size_random: FloatProperty(name="Size Random", default=0.5, min=0.0, max=1.0, subtype="FACTOR",
                               description="Each particle's size is (1 ± this) × Size")
    color: FloatVectorProperty(name="Color", subtype="COLOR", size=3, min=0.0, max=1.0, default=(1.0, 1.0, 1.0),
                               update=_look_updated, description="Colour of tracers (times the smoke's with Colour Mixing)")
    brightness: FloatProperty(name="Brightness", default=6.0, min=0.0, soft_max=20.0, update=_look_updated,
                              description="Tracers: strength of their glitter flashes. Embers: glow "
                                          "(temperature × this / 100; about 0.1)")
    material: PointerProperty(
        name="Material", type=bpy.types.Material, update=_look_updated,
        description="Material of this modifier's points (made from the kind's when empty; Color and "
                    "Brightness change the made one)")
    show_in_preview: BoolProperty(name="Show in Preview", default=True,
                                  description="Draw these particles over the live preview")
    show_more: BoolProperty(name="More", default=False, options={"SKIP_SAVE"})


class FumarisParticleSettings(PropertyGroup):
    """The domain's particle system, shared by its Spawn Particles modifiers;
    and the Particles v1 settings files from before the modifiers still hold
    (read once, by migrate_domain)."""
    show_system: BoolProperty(name="Particle System", default=False, options={"SKIP_SAVE"})
    max_particles: IntProperty(
        name="Max Particles", default=1_000_000, min=1000, max=64 * 1024 * 1024, soft_max=10_000_000,
        description="Particles alive at most, of every Spawn Particles modifier together. Each takes 92 "
                    "bytes of GPU memory, taken from Simulation Memory. Spawns past it are dropped")
    seed: IntProperty(name="Seed", default=0, min=0, description="Another number gives other random particles")
    kill_outside: BoolProperty(name="Inside Domain", default=True,
                               description="Remove particles that leave the domain object's box "
                                           "(a domain that is an empty has none)")
    show_in_preview: BoolProperty(name="Show in Preview", default=True,
                                  description="Draw the particles over the live preview")
    preview_amount: FloatProperty(name="Amount", default=0.25, min=0.0, max=1.0, subtype="FACTOR",
                                  description="Share of the particles the live preview draws (one pixel each)")
    migrated: IntProperty(name="Migrated", default=0, options={"HIDDEN"},
                          description="Particles v1 settings moved into Spawn Particles modifiers")
    # Particles v1 (before the Modifiers stack): read by migrate_domain only.
    enabled: BoolProperty(name="Particles (v1)", default=False, options={"HIDDEN"})
    kind: EnumProperty(items=[("TRACERS", "Tracers", ""), ("EMBERS", "Embers", "")], default="TRACERS",
                       options={"HIDDEN"})
    spawn_smoke: BoolProperty(default=True, options={"HIDDEN"})
    smoke_rate: FloatProperty(default=200_000.0, options={"HIDDEN"})
    smoke_min: FloatProperty(default=0.05, options={"HIDDEN"})
    smoke_max: FloatProperty(default=0.0, options={"HIDDEN"})
    temperature_min: FloatProperty(default=0.0, options={"HIDDEN"})
    temperature_max: FloatProperty(default=0.0, options={"HIDDEN"})
    spawn_jitter: FloatProperty(default=0.0, options={"HIDDEN"})
    source_collection: PointerProperty(type=bpy.types.Collection, options={"HIDDEN"})
    source_mode: EnumProperty(items=[("SURFACE", "Surface", ""), ("VOLUME", "Volume", ""), ("POINTS", "Points", "")],
                              default="SURFACE", options={"HIDDEN"})
    source_rate: FloatProperty(default=2000.0, options={"HIDDEN"})
    source_speed: FloatProperty(default=0.0, options={"HIDDEN"})
    source_inherit: FloatProperty(default=1.0, options={"HIDDEN"})
    drag: FloatProperty(default=10000.0, options={"HIDDEN"})
    gravity: FloatProperty(default=0.0, options={"HIDDEN"})
    lifetime: FloatProperty(default=5.0, options={"HIDDEN"})
    lifetime_random: FloatProperty(default=0.3, options={"HIDDEN"})
    cooling_time: FloatProperty(default=0.0, options={"HIDDEN"})
    collision: EnumProperty(items=[("NONE", "Pass", ""), ("BOUNCE", "Bounce", ""), ("STICK", "Stick", ""),
                                   ("KILL", "Kill", "")], default="BOUNCE", options={"HIDDEN"})
    bounce: FloatProperty(default=0.2, options={"HIDDEN"})
    friction: FloatProperty(default=0.1, options={"HIDDEN"})
    size: FloatProperty(default=0.006, options={"HIDDEN"})
    size_random: FloatProperty(default=0.5, options={"HIDDEN"})
    color: FloatVectorProperty(subtype="COLOR", size=3, default=(1.0, 1.0, 1.0), options={"HIDDEN"})
    brightness: FloatProperty(default=6.0, options={"HIDDEN"})


# ------------------------------------------------------------ the modifiers

def settings_of(domain):
    return getattr(domain, "fumaris_particles", None)


def is_spawn(layer):
    return layer.layer_type == "PARTICLES"


def spawn_layers(props, enabled_only=True):
    """The domain's Spawn Particles modifiers in stack order (the first
    MAX_GROUPS enabled ones are sent: their index is the spawn group)."""
    layers = [layer for layer in getattr(props, "detail_layers", ()) if is_spawn(layer)]
    if enabled_only:
        layers = [layer for layer in layers if layer.enabled][:MAX_GROUPS]
    return layers


def active(domain):
    props = getattr(domain, "fumaris", None)
    return bool(props is not None and spawn_layers(props))


def new_uid(props):
    return 1 + max((layer.particles.uid for layer in getattr(props, "detail_layers", ()) if is_spawn(layer)),
                   default=0)


def init_spawn(props, layer, kind="TRACERS", masks=True):
    """A new Spawn Particles modifier: its kind's defaults, masks, an identifier."""
    spawn = layer.particles
    spawn.uid = new_uid(props)
    spawn["kind"] = 1 if kind == "EMBERS" else 0   # no update: the defaults follow
    for name, value in KIND_DEFAULTS[kind].items():
        setattr(spawn, name, value)
    if masks:
        from . import detail_stack
        for mask in default_masks(kind, _flame(props.id_data)):
            detail_stack.add_mask(layer, **mask)
    system = settings_of(props.id_data)
    if system is not None and len(spawn_layers(props, enabled_only=False)) == 1:
        # The domain's first particles: a budget that suits their kind.
        system.max_particles = KIND_MAX_PARTICLES[kind]
    return spawn


def label(layer):
    spawn = layer.particles
    return KIND_LABELS.get(spawn.kind, "Particles")


def summary(layer, narrow=False):
    """The list row's text after the label: rate and source."""
    spawn = layer.particles
    parts = []
    if spawn.spawn_smoke:
        rate = spawn.rate
        parts.append(f"{rate / 1000:g}k/s" if rate >= 1000 else f"{rate:g}/s")
    if spawn.spawn_objects:
        parts.append(spawn.source_collection.name if spawn.source_collection and not narrow else "objects")
    return parts


_SYSTEM_KEYS = ("max_particles", "seed", "kill_outside", "show_in_preview", "preview_amount")


def system_to_dict(domain):
    """The particle system's settings for presets and versions."""
    settings = settings_of(domain)
    return {key: getattr(settings, key) for key in _SYSTEM_KEYS} if settings is not None else {}


def system_from_dict(domain, data):
    """Restores system_to_dict's settings; the number changed."""
    settings = settings_of(domain)
    changed = 0
    for key, value in (data or {}).items():
        if key in _SYSTEM_KEYS and settings is not None and getattr(settings, key) != value:
            try:
                setattr(settings, key, value)
                changed += 1
            except (TypeError, ValueError):
                pass
    return changed


def memory_bytes(domain):
    settings = settings_of(domain)
    if settings is None or not active(domain):
        return 0
    return int(settings.max_particles) * DEVICE_BYTES_PER_PARTICLE


# ---------------------------------------------------------------- frames

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


def _triangle_bins(verts, tris, bins):
    """Triangles by the XY cells (bins x bins over the mesh's box) their
    footprint covers, as CSR (starts, triangle indices): the candidates a
    vertical ray from a point in a cell can cross."""
    low, high = verts[:, :2].min(axis=0), verts[:, :2].max(axis=0)
    extent = np.maximum(high - low, 1e-9)
    xy = verts[tris][:, :, :2]                      # (T, 3, 2)
    lo = np.floor((xy.min(axis=1) - low) / extent * bins).astype(np.int64).clip(0, bins - 1)
    hi = np.floor((xy.max(axis=1) - low) / extent * bins).astype(np.int64).clip(0, bins - 1)
    span = hi - lo + 1
    counts = span[:, 0] * span[:, 1]
    tri_of = np.repeat(np.arange(len(tris)), counts)
    # the k-th cell of each triangle's footprint
    first = np.repeat(np.cumsum(counts) - counts, counts)
    k = np.arange(len(tri_of)) - first
    width = span[tri_of, 0]
    cell = (lo[tri_of, 1] + k // width) * bins + lo[tri_of, 0] + k % width
    order = np.argsort(cell, kind="stable")
    starts = np.searchsorted(cell[order], np.arange(bins * bins + 1))
    return low, extent, starts, tri_of[order]


def _inside(points, verts, tris, binned, bins):
    """Whether each point is inside the closed mesh: the parity of the faces a
    ray straight up from it crosses (vectorized over points and faces)."""
    low, extent, starts, members = binned
    cell = np.floor((points[:, :2] - low) / extent * bins).astype(np.int64)
    outside = np.any((cell < 0) | (cell >= bins), axis=1)
    cell = cell.clip(0, bins - 1)
    flat = cell[:, 1] * bins + cell[:, 0]
    begin, end = starts[flat], starts[flat + 1]
    counts = np.where(outside, 0, end - begin)
    owner = np.repeat(np.arange(len(points)), counts)
    offset = np.arange(len(owner)) - np.repeat(np.cumsum(counts) - counts, counts)
    tri = members[np.repeat(begin, counts) + offset]
    a, b, c = verts[tris[tri, 0]], verts[tris[tri, 1]], verts[tris[tri, 2]]
    p = points[owner]
    # barycentric coordinates of p's xy in the face's xy projection
    v0, v1, v2 = b[:, :2] - a[:, :2], c[:, :2] - a[:, :2], p[:, :2] - a[:, :2]
    det = v0[:, 0] * v1[:, 1] - v0[:, 1] * v1[:, 0]
    good = np.abs(det) > 1e-18
    det = np.where(good, det, 1.0)
    u = (v2[:, 0] * v1[:, 1] - v2[:, 1] * v1[:, 0]) / det
    v = (v0[:, 0] * v2[:, 1] - v0[:, 1] * v2[:, 0]) / det
    hit = good & (u >= 0.0) & (v >= 0.0) & (u + v <= 1.0)
    z = a[:, 2] + u * (b[:, 2] - a[:, 2]) + v * (c[:, 2] - a[:, 2])
    hit &= z > p[:, 2]
    crossings = np.bincount(owner[hit], minlength=len(points))
    return (crossings % 2 == 1) & ~outside


def _volume_samples(verts, tris, count, rng):
    """Points inside a closed mesh: candidates in its box kept when a ray
    straight up crosses its faces an odd number of times. Vectorized (faces
    binned by their XY footprint): about 10 ms per 100,000 candidates."""
    low, high = verts.min(axis=0), verts.max(axis=0)
    bins = int(min(128, max(4, math.sqrt(len(tris)) // 2)))
    binned = _triangle_bins(verts, tris, bins)
    kept = []
    found = 0
    fill = 0.5
    for _attempt in range(8):
        wanted = count - found
        if wanted <= 0:
            break
        batch = int(min(4 * MAX_SOURCE_POINTS, max(64, 1.3 * wanted / max(fill, 0.02))))
        candidates = low + rng.random((batch, 3)) * (high - low)
        inside = np.zeros(batch, dtype=bool)
        for start in range(0, batch, 200_000):
            inside[start:start + 200_000] = _inside(candidates[start:start + 200_000], verts, tris, binned, bins)
        fill = max(inside.mean(), 1e-3)
        chosen = candidates[inside][:wanted]
        kept.append(chosen)
        found += len(chosen)
    if not found:
        return None
    return np.concatenate(kept)


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


def _source_objects(spawn):
    collection = spawn.source_collection
    if collection is None:
        return []
    return sorted({obj for obj in collection.all_objects if obj.type in {"MESH", "POINTCLOUD", "CURVES", "EMPTY"}},
                  key=lambda obj: obj.name)


def _source_spawns(spawn, seed, depsgraph, frame, fps):
    """This frame's spawn points of one modifier from its source collection:
    positions, velocities (scene units per second), times (0..1 in the
    frame), sizes."""
    if np is None:
        return None
    objects = _source_objects(spawn)
    if not objects:
        return None
    parts = []
    meshes = [obj for obj in objects if obj.type == "MESH"] if spawn.source_mode != "POINTS" else []
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
    # Two modifiers on one collection draw other points (the first keeps v1's).
    salt = "" if spawn.uid <= 1 else f"#{spawn.uid}"
    for obj in objects:
        name = obj.name
        rng = _rng(seed, frame, name + salt)
        key = (spawn.uid, name)
        if name in triangles:
            verts, tris, matrix = triangles[name]
            count = _count_this_frame(spawn.source_rate, frame, fps, shares[name] / total_share)
            count = min(count, MAX_SOURCE_POINTS)
            if count <= 0:
                _SOURCE_MATRICES[key] = (frame, matrix)
                continue
            normals = None
            if spawn.source_mode == "VOLUME":
                local = _volume_samples(verts, tris, count, rng)
            else:
                local, normals = _surface_samples(verts, tris, count, rng)
            if local is None:
                continue
            count = len(local)
            world = local @ matrix[:3, :3].T + matrix[:3, 3]
            times = rng.random(count)
            velocity = np.zeros((count, 3))
            previous = _SOURCE_MATRICES.get(key)
            if previous is not None and previous[0] == frame - 1:
                before = local @ previous[1][:3, :3].T + previous[1][:3, 3]
                velocity += spawn.source_inherit * (world - before) * fps
                # where the surface was at each particle's moment in the frame
                world = before + (world - before) * times[:, None]
            if normals is not None and spawn.source_speed != 0.0:
                world_normals = normals @ np.linalg.inv(matrix[:3, :3])
                world_normals /= np.maximum(np.linalg.norm(world_normals, axis=1), 1e-12)[:, None]
                velocity += spawn.source_speed * world_normals
            parts.append((world, velocity, times, np.ones(count)))
        elif obj.type != "MESH" or spawn.source_mode == "POINTS":
            found = _evaluated_points(obj, depsgraph)
            if found is None:
                continue
            world, velocity, sizes = found
            if len(world) > MAX_SOURCE_POINTS:
                world, velocity, sizes = world[:MAX_SOURCE_POINTS], velocity[:MAX_SOURCE_POINTS], sizes[:MAX_SOURCE_POINTS]
            parts.append((world, velocity, rng.random(len(world)), sizes))
        _SOURCE_MATRICES[key] = (frame, _matrix_np(obj.evaluated_get(depsgraph).matrix_world))
    if not parts:
        return None
    return tuple(np.concatenate([part[i] for part in parts]) for i in range(4))


def group_state(layer, system):
    """One Spawn Particles modifier as the bridge reads it (ParticleGroup)."""
    from . import detail_stack
    spawn = layer.particles
    drawn = system.show_in_preview and spawn.show_in_preview
    return {
        "rate": float(spawn.rate) if spawn.spawn_smoke else 0.0,
        "masks": detail_stack.export_masks(layer),
        "mask_points": bool(spawn.mask_objects),
        "spawn_jitter": float(spawn.spawn_jitter),
        "drag": float(spawn.drag),
        "gravity": float(spawn.gravity),
        "lifetime": float(spawn.lifetime),
        "lifetime_random": float(spawn.lifetime_random),
        "size_random": float(spawn.size_random),
        "cooling_time": float(spawn.cooling_time),
        "collision": spawn.collision.lower(),
        "bounce": float(spawn.bounce),
        "friction": float(spawn.friction),
        "draw": ("glow" if spawn.kind == "EMBERS" else "color") if drawn else "off",
        "draw_color": [float(v) for v in spawn.color],
    }


def frame_state(domain, depsgraph, payload, frame, append_array):
    """The frame packet's "particles" section, or None without Spawn Particles modifiers."""
    settings = settings_of(domain)
    props = getattr(domain, "fumaris", None)
    if settings is None or props is None:
        return None
    layers = spawn_layers(props)
    if not layers:
        return None
    scene = bpy.context.scene
    fps = scene.render.fps / scene.render.fps_base
    low, high = _domain_bounds(domain, depsgraph)
    # An empty domain (unbounded sparse grid) has no box to keep them in.
    boxed = domain.type == "MESH" and all(h - l > 1e-6 for l, h in zip(low, high))
    state = {
        "enabled": True,
        "max_particles": int(settings.max_particles),
        "seed": int(settings.seed),
        "groups": [group_state(layer, settings) for layer in layers],
        "kill_outside": bool(settings.kill_outside) and boxed,
        "bounds_min": [float(v) for v in low],
        "bounds_max": [float(v) for v in high],
        "draw_fraction": float(settings.preview_amount),
        "write_cache": False,
    }
    parts = []
    for index, layer in enumerate(layers):
        if not layer.particles.spawn_objects:
            continue
        spawns = _source_spawns(layer.particles, settings.seed, depsgraph, frame, fps)
        if spawns is not None:
            parts.append(spawns + (np.full(len(spawns[0]), index, dtype=np.uint32),))
    if parts:
        positions, velocities, times, sizes, groups = (np.concatenate([part[i] for part in parts]) for i in range(5))
        state["spawn_positions"] = append_array(payload, np.ascontiguousarray(positions, dtype=np.float32).ravel())
        state["spawn_velocities"] = append_array(payload, np.ascontiguousarray(velocities, dtype=np.float32).ravel())
        state["spawn_times"] = append_array(payload, np.ascontiguousarray(times, dtype=np.float32))
        state["spawn_sizes"] = append_array(payload, np.ascontiguousarray(sizes, dtype=np.float32))
        if len(parts) > 1 or parts[0][4][:1].any():
            state["spawn_groups"] = append_array(payload, np.ascontiguousarray(groups, dtype=np.uint32))
    return state


# ---------------------------------------------------------------- migration

def _range_masks(field, low, high, epsilon):
    """Masks that keep low <= value <= high (0: no limit) as v1's ranges did."""
    masks = []
    if low > 0.0:
        masks.append({"field": field, "range": [low - epsilon, low], "values": [0.0, 1.0]})
    if high > 0.0:
        masks.append({"field": field, "range": [high, high + epsilon], "values": [1.0, 0.0]})
    return masks


def migrate_domain(domain):
    """Particles v1 (one system on the domain object) into a Spawn Particles
    modifier: the same spawning (its smoke and temperature ranges as masks
    that keep the same cells), motion and look, and its point-cloud object.
    Returns the modifier, or None when there was nothing to move."""
    settings = settings_of(domain)
    props = getattr(domain, "fumaris", None)
    if settings is None or props is None or settings.migrated >= MIGRATED:
        return None
    settings.migrated = MIGRATED
    if not settings.enabled:
        return None
    from . import detail_stack
    layer = props.detail_layers.add()
    layer.layer_type = "PARTICLES"
    spawn = layer.particles
    spawn.uid = new_uid(props)
    spawn["kind"] = 1 if settings.kind == "EMBERS" else 0
    for name in ("spawn_jitter", "source_collection", "source_mode", "source_rate", "source_speed",
                 "source_inherit", "drag", "gravity", "lifetime", "lifetime_random", "cooling_time",
                 "collision", "bounce", "friction", "size", "size_random", "color", "brightness"):
        setattr(spawn, name, getattr(settings, name))
    spawn.spawn_smoke = bool(settings.spawn_smoke)
    spawn.rate = settings.smoke_rate
    spawn.spawn_objects = settings.source_collection is not None
    for mask in (_range_masks("SMOKE", settings.smoke_min, settings.smoke_max, 1e-6) +
                 _range_masks("TEMPERATURE", settings.temperature_min, settings.temperature_max, 1e-3)):
        detail_stack.add_mask(layer, **mask)
    props.detail_layers_index = len(props.detail_layers) - 1
    for obj in particle_objects(domain):
        obj[UID_MARKER] = spawn.uid
        _update_object(obj, domain, layer)
    return layer


def migrate_all():
    for obj in bpy.data.objects:
        if getattr(obj, "fumaris", None) is not None and obj.fumaris.smoke_object_type == "domain":
            try:
                migrate_domain(obj)
            except Exception as error:   # never break loading a file
                print(f"Fumaris particles: could not move {obj.name}'s particles into a modifier: {error}")


@persistent
def _migrate_after_load(_dummy=None):
    migrate_all()


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
    """The shared node group: the shown frame's PLY, one spawn group's points, as a point cloud."""
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
    _socket(tree, "Group", "NodeSocketInt", 0, 0,
            "Spawn group shown: the Spawn Particles modifier's place among the bake's (0 the first). "
            "Fumaris keeps it on the modifier's")
    _socket(tree, "Size", "NodeSocketFloat", 0.006, 0.0, "Radius of the points (times each particle's size)")
    _socket(tree, "Material", "NodeSocketMaterial", description="Material of the points")
    nodes = tree.nodes
    group_in = nodes.new("NodeGroupInput")
    group_in.location = (-1200, 0)
    group_out = nodes.new("NodeGroupOutput")
    group_out.location = (1900, 0)

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
    # Only this modifier's particles (a cache without "group" is all group 0).
    other = nodes.new("FunctionNodeCompare")
    other.location = (-300, -350)
    other.data_type = "FLOAT"
    other.operation = "NOT_EQUAL"
    other.inputs["Epsilon"].default_value = 0.5
    tree.links.new(_named(tree, "group", (-500, -350)), other.inputs["A"])
    tree.links.new(group_in.outputs["Group"], other.inputs["B"])
    keep = nodes.new("GeometryNodeDeleteGeometry")
    keep.location = (-150, 0)
    keep.domain = "POINT"
    tree.links.new(read.outputs[0], keep.inputs["Geometry"])
    tree.links.new(other.outputs["Result"], keep.inputs["Selection"])
    points = nodes.new("GeometryNodeMeshToPoints")
    points.location = (0, 0)
    tree.links.new(keep.outputs["Geometry"], points.inputs["Mesh"])
    geometry = points.outputs["Points"]

    velocity = nodes.new("ShaderNodeCombineXYZ")
    velocity.location = (100, -400)
    for axis, name in zip("XYZ", ("vx", "vy", "vz")):
        tree.links.new(_named(tree, name, (-100, -400 - 120 * "XYZ".index(axis))), velocity.inputs[axis])
    # Cycles blurs points along "velocity" (scene units per second).
    geometry = _store(tree, geometry, "velocity", velocity.outputs["Vector"], (250, 0), "FLOAT_VECTOR")
    x = 400
    for name, scale in FILE_SCALES.items():
        geometry = _store(tree, geometry, name, _math(tree, "MULTIPLY", _named(tree, name, (x - 150, -700)), scale, (x, -700)),
                          (x, 0))
        x += 150
    for name in ("id", "group"):
        to_int = nodes.new("FunctionNodeFloatToInt")
        to_int.location = (x + 100, -500)
        tree.links.new(_named(tree, name, (x, -500)), to_int.inputs[0])
        geometry = _store(tree, geometry, name, to_int.outputs[0], (x + 150, 0), "INT")
        x += 200
    # the velocity's components are in "velocity" now
    for index, name in enumerate(("vx", "vy", "vz")):
        remove = nodes.new("GeometryNodeRemoveAttribute")
        remove.location = (x + 300, 120 * index)
        remove.inputs["Name"].default_value = name
        tree.links.new(geometry, remove.inputs["Geometry"])
        geometry = remove.outputs["Geometry"]
    radius = nodes.new("GeometryNodeSetPointRadius")
    radius.location = (x + 500, 0)
    tree.links.new(geometry, radius.inputs["Points"])
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
        # Glitter: soft white flakes, each flashing now and then as it turns
        # (a per-particle phase from "random", advancing with "age").
        bsdf = tree.nodes.new("ShaderNodeBsdfPrincipled")
        bsdf.location = (300, 0)
        bsdf.inputs["Roughness"].default_value = 0.35
        rgb = tree.nodes.new("ShaderNodeRGB")
        rgb.location = (-250, 200)
        rgb.outputs[0].default_value = (1.0, 1.0, 1.0, 1.0)
        rgb.label = "Color"
        tree.links.new(rgb.outputs[0], bsdf.inputs["Base Color"])
        tree.links.new(rgb.outputs[0], bsdf.inputs["Emission Color"])
        rand = tree.nodes.new("ShaderNodeAttribute")
        rand.attribute_name = "random"
        rand.location = (-900, -200)
        age = tree.nodes.new("ShaderNodeAttribute")
        age.attribute_name = "age"
        age.location = (-900, -400)

        def math(operation, a, b, location, label=""):
            node = tree.nodes.new("ShaderNodeMath")
            node.operation = operation
            node.location = location
            node.label = label
            for index, value in enumerate((a, b)):
                if isinstance(value, float):
                    node.inputs[index].default_value = value
                else:
                    tree.links.new(value, node.inputs[index])
            return node

        phase = math("MULTIPLY_ADD", rand.outputs["Fac"], 44.0, (-650, -250))
        tree.links.new(math("MULTIPLY", age.outputs["Fac"], 9.0, (-650, -450), "Twinkle").outputs[0], phase.inputs[2])
        wave = math("SINE", phase.outputs[0], 0.0, (-450, -250))
        unit = math("MULTIPLY_ADD", wave.outputs[0], 0.5, (-300, -250))
        unit.inputs[2].default_value = 0.5
        flash = math("POWER", unit.outputs[0], 24.0, (-150, -250))
        strength = math("MULTIPLY", flash.outputs[0], 6.0, (50, -250), "Brightness")
        tree.links.new(strength.outputs[0], bsdf.inputs["Emission Strength"])
        tree.links.new(bsdf.outputs["BSDF"], output.inputs["Surface"])
    material[VERSION_KEY] = VERSION
    material["fumaris_particles_kind"] = kind
    return material


def _apply_look(material, spawn):
    """A modifier's Color and Brightness on the material made for it."""
    tree = material.node_tree
    if spawn.kind == "EMBERS":
        node = next((n for n in tree.nodes if n.label == "Brightness"), None)
        if node is not None:
            node.inputs[1].default_value = spawn.brightness
    else:
        rgb = next((n for n in tree.nodes if n.label == "Color"), None)
        if rgb is not None:
            rgb.outputs[0].default_value = (*spawn.color, 1.0)
        flash = next((n for n in tree.nodes if n.label == "Brightness"), None)
        if flash is not None:
            flash.inputs[1].default_value = spawn.brightness


def _input(modifier, name):
    """A value of the group on one object's modifier, by its name (as sparks._input)."""
    item = next((item for item in modifier.node_group.interface.items_tree
                 if item.item_type == "SOCKET" and item.in_out == "INPUT" and item.name == name), None)
    return getattr(modifier.properties.inputs, item.identifier, None) if item else None


def _set_input(modifier, name, value):
    item = _input(modifier, name)
    if item is not None and item.value != value:
        item.value = value


def particle_objects(domain, uid=None):
    found = [obj for obj in bpy.data.objects if obj.get(DOMAIN_MARKER) == domain]
    if uid is not None:
        found = [obj for obj in found if int(obj.get(UID_MARKER, 1)) == uid]
    return found


def _modifier(obj):
    return next((m for m in obj.modifiers if m.type == "NODES" and m.node_group is not None
                 and m.node_group.get(VERSION_KEY)), None)


def _layer_by_uid(domain, uid):
    for layer in spawn_layers(domain.fumaris, enabled_only=False):
        if layer.particles.uid == uid:
            return layer
    return None


def _material_for(domain, layer):
    """The modifier's material: the user's own, or one made from its kind's
    (a copy per modifier, carrying its Color and Brightness)."""
    spawn = layer.particles
    material = spawn.material
    if material is not None and material.get("fumaris_particles_kind") is None:
        return material
    if material is None or material.get("fumaris_particles_kind") != spawn.kind:
        name = f"{domain.name} {MATERIAL_NAMES[spawn.kind]}"
        existing = bpy.data.materials.get(name)
        taken = {other.particles.material for other in spawn_layers(domain.fumaris, enabled_only=False)
                 if other.particles.uid != spawn.uid}
        if existing is not None and existing.get("fumaris_particles_kind") == spawn.kind and existing not in taken:
            material = existing   # Particles v1's copy for this domain
        else:
            material = build_material(spawn.kind).copy()
            material.name = name
        spawn.material = material
    _apply_look(material, spawn)
    return material


def _update_object(obj, domain, layer, group=None):
    """The object's node group (the current version) and the modifier's look on it."""
    modifier = _modifier(obj)
    if modifier is None:
        return
    if modifier.node_group.get(VERSION_KEY) != VERSION:
        # an object from an older add-on: the new group, the same inputs
        values = {name: getattr(_input(modifier, name), "value", None)
                  for name in ("Directory", "Prefix", "Frame Offset")}
        modifier.node_group = build_group()
        for name, value in values.items():
            if value is not None:
                _set_input(modifier, name, value)
    spawn = layer.particles
    _set_input(modifier, "Size", float(spawn.size))
    _set_input(modifier, "Material", _material_for(domain, layer))
    if group is not None:
        _set_input(modifier, "Group", int(group))
    obj.update_tag()


def _sync_objects(domain):
    props = getattr(domain, "fumaris", None)
    if props is None:
        return
    for obj in particle_objects(domain):
        layer = _layer_by_uid(domain, int(obj.get(UID_MARKER, 1)))
        if layer is not None:
            _update_object(obj, domain, layer)


def add_particle_object(context, domain, directory, layer=None, group=0):
    """The point-cloud object reading one modifier's particles from the
    domain's particle cache in `directory`."""
    layers = spawn_layers(domain.fumaris, enabled_only=False)
    layer = layer or (layers[0] if layers else None)
    if layer is None:
        return None
    tree = build_group()
    name = f"{domain.name} {label(layer)}"
    mesh = bpy.data.meshes.new(name)
    obj = bpy.data.objects.new(name, mesh)
    collections = [c for c in domain.users_collection if c.name in context.scene.collection.children_recursive or
                   c == context.scene.collection]
    (collections[0] if collections else context.scene.collection).objects.link(obj)
    modifier = obj.modifiers.new(GROUP_NAME, "NODES")
    modifier.node_group = tree
    obj[DOMAIN_MARKER] = domain
    obj[UID_MARKER] = layer.particles.uid
    _set_input(modifier, "Directory", cache_directory(directory))
    _set_input(modifier, "Prefix", domain.fumaris.output_prefix or "fumaris_")
    _update_object(obj, domain, layer, group)
    return obj


def baked_groups(domain, directory):
    """The uids of the Spawn Particles modifiers a bake simulated, in group
    order: from the bake's settings snapshot, else the current stack."""
    from .cache import read_settings_snapshot
    try:
        data = read_settings_snapshot(directory) or {}
    except Exception:
        data = {}
    entries = data.get("detail_layers")
    if entries is None:
        return [layer.particles.uid for layer in spawn_layers(domain.fumaris)]
    uids = [int(entry.get("particles", {}).get("uid", 1)) for entry in entries
            if entry.get("layer_type") == "PARTICLES" and entry.get("enabled", True)]
    if not uids and settings_of(domain) is not None and settings_of(domain).enabled:
        uids = [1]   # a Particles v1 bake: one group, migrated as the first modifier
    return uids[:MAX_GROUPS]


def follow_bakes(context=None):
    """Every domain with particles reads the particle cache of the bake it
    shows: each modifier's object is pointed there and at its group, made
    when the bake has particles of it."""
    from .utils import peek_output_directory_for_object
    context = context or bpy.context
    for domain in [obj for obj in bpy.data.objects if getattr(obj, "fumaris", None) is not None
                   and obj.fumaris.smoke_object_type == "domain"]:
        directory = peek_output_directory_for_object(domain)
        if not directory or settings_of(domain) is None:
            continue
        directory = os.path.normpath(directory)
        has_files = bool(frame_files(directory, domain.fumaris.output_prefix or "fumaris_"))
        groups = baked_groups(domain, directory) if has_files else []
        for group, uid in enumerate(groups):
            layer = _layer_by_uid(domain, uid)
            if layer is None or particle_objects(domain, uid):
                continue
            try:
                add_particle_object(context, domain, directory, layer, group)
            except Exception as error:   # never break an import
                print(f"Fumaris particles: could not add the particle object: {error}")
        for obj in particle_objects(domain):
            modifier = _modifier(obj)
            if modifier is None:
                continue
            uid = int(obj.get(UID_MARKER, 1))
            layer = _layer_by_uid(domain, uid)
            if layer is not None:
                # not in this bake: a group no particle has
                _update_object(obj, domain, layer, groups.index(uid) if uid in groups else 255)
            _set_input(modifier, "Directory", cache_directory(directory))
            _set_input(modifier, "Prefix", domain.fumaris.output_prefix or "fumaris_")
            obj.update_tag()


# ---------------------------------------------------------------- panel

def draw_modifier(layout, layer, domain):
    """The active Spawn Particles modifier's settings (detail_stack.draw)."""
    from . import ui
    pair, nested_foldout = ui._pair, ui._nested_foldout
    spawn = layer.particles
    column = layout.column(align=True)
    column.row(align=True).prop(spawn, "kind", expand=True)
    column.separator(factor=0.5)
    sources = column.row(align=True)
    sources.label(text="Spawn From")
    sources.prop(spawn, "spawn_smoke", toggle=True)
    sources.prop(spawn, "spawn_objects", toggle=True)
    if spawn.spawn_smoke:
        column.prop(spawn, "rate")
    if spawn.spawn_objects:
        column.separator(factor=0.5)
        column.prop(spawn, "source_collection")
        if spawn.source_collection is not None:
            source = pair(column)
            source.prop(spawn, "source_mode", text="")
            sub = source.row(align=True)
            sub.active = spawn.source_mode != "POINTS"
            sub.prop(spawn, "source_rate", text="Rate")
            speeds = pair(column)
            speeds.prop(spawn, "source_speed")
            speeds.prop(spawn, "source_inherit", text="Inherit")
    column.separator()
    motion = pair(column)
    motion.prop(spawn, "drag")
    motion.prop(spawn, "gravity")
    life = pair(column)
    life.prop(spawn, "lifetime")
    life.prop(spawn, "lifetime_random", text="Random")
    hits = pair(column)
    hits.prop(spawn, "collision", text="")
    bounce = hits.row(align=True)
    bounce.active = spawn.collision == "BOUNCE"
    bounce.prop(spawn, "bounce")
    column.separator()
    look = pair(column)
    look.prop(spawn, "size")
    look.prop(spawn, "size_random", text="Random")
    look = pair(column)
    if spawn.kind != "EMBERS":
        look.prop(spawn, "color", text="")
    look.prop(spawn, "brightness")
    column.prop(spawn, "show_in_preview")
    more, opened = nested_foldout(column, spawn, "show_more", "More")
    if opened:
        more.prop(spawn, "material")
        more.prop(spawn, "cooling_time")
        more.prop(spawn, "friction")
        more.prop(spawn, "spawn_jitter")
        mask_objects = more.row(align=True)
        mask_objects.active = spawn.spawn_objects
        mask_objects.prop(spawn, "mask_objects")
    objects = particle_objects(domain, spawn.uid)
    if objects:
        column.label(text=f"Shown by '{objects[0].name}'", icon="OUTLINER_OB_POINTCLOUD")


def draw_system(layout, domain):
    """The particle system every Spawn Particles modifier shares (under the stack)."""
    from . import ui
    settings = settings_of(domain)
    if settings is None:
        return
    count = len(spawn_layers(domain.fumaris))
    title = "Particle System" if ui._NARROW else f"Particle System · shared by {count} modifier" + ("s" if count != 1 else "")
    body, opened = ui._nested_foldout(layout, settings, "show_system", title)
    if not opened:
        return
    limits = ui._pair(body)
    limits.prop(settings, "max_particles")
    limits.prop(settings, "seed")
    body.label(text=f"GPU memory: {settings.max_particles * DEVICE_BYTES_PER_PARTICLE / 2**20:.0f} MB")
    body.prop(settings, "kill_outside")
    shown = body.row(align=True)
    shown.prop(settings, "show_in_preview")
    amount = shown.row(align=True)
    amount.active = settings.show_in_preview
    amount.prop(settings, "preview_amount")
    ui._wrapped(body, "A bake writes them to particles/ beside its VDBs", "INFO")


PROPERTY_CLASSES = (FumarisSpawnSettings,)
CLASSES = (FumarisParticleSettings,)


def register():
    for cls in CLASSES:
        bpy.utils.register_class(cls)
    bpy.types.Object.fumaris_particles = PointerProperty(type=FumarisParticleSettings)
    if _migrate_after_load not in bpy.app.handlers.load_post:
        bpy.app.handlers.load_post.append(_migrate_after_load)
    # A file already open when the add-on is enabled.
    bpy.app.timers.register(lambda: (migrate_all(), None)[1], first_interval=0.0)


def unregister():
    if _migrate_after_load in bpy.app.handlers.load_post:
        bpy.app.handlers.load_post.remove(_migrate_after_load)
    if hasattr(bpy.types.Object, "fumaris_particles"):
        del bpy.types.Object.fumaris_particles
    for cls in reversed(CLASSES):
        bpy.utils.unregister_class(cls)
