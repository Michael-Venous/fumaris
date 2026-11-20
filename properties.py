from math import atan2, cos, hypot, isfinite, pi, sin

import bpy
from bpy.props import (
    BoolProperty,
    CollectionProperty,
    EnumProperty,
    FloatProperty,
    FloatVectorProperty,
    IntProperty,
    PointerProperty,
    StringProperty,
)
from bpy.types import PropertyGroup

from .detail_stack import FumarisDetailLayer
from .memory_auto import auto_toggled


_EXPORT_RESOLUTION_ITEMS = (
    ("FULL", "Full", "The simulation's resolution", 0),
    ("HALF", "Half", "Half resolution (2x2x2 averages): about 8 times fewer voxels", 1),
    ("QUARTER", "Quarter", "A quarter of the resolution (4x4x4 averages): about 64 times fewer voxels", 2),
)
_VELOCITY_RESOLUTION_ITEMS = _EXPORT_RESOLUTION_ITEMS[1:]


def _full_resolution_get(self):
    return bool(self.full_resolution_channels) or bool(self.get("full_resolution_velocity", False))


def _full_resolution_set(self, value):
    self.full_resolution_channels = bool(value)
    # the old option's stored value, from a file saved before the two merged
    if "full_resolution_velocity" in self.keys():
        del self["full_resolution_velocity"]


def _velocity_resolution_items(self, _context):
    # Full only when motion is simulated at full resolution.
    return _EXPORT_RESOLUTION_ITEMS if getattr(self, "full_resolution_velocity", False) else _VELOCITY_RESOLUTION_ITEMS


def _object_type_updated(self, _context):
    # A domain is a bounding volume: show it as a wireframe so the smoke inside is visible.
    obj = self.id_data
    if self.smoke_object_type == "domain" and getattr(obj, "type", "") != "EMPTY":
        obj.display_type = "WIRE"
    # Made a domain in the panel: Auto VRAM on, as Quick Setup and New Domain
    # do. Not in background Blender: scripts and tests expect the fixed amount
    # (most of a GPU each would not fit several at once).
    if self.smoke_object_type == "domain" and not bpy.app.background:
        self.simulation_memory_auto = True
    # A new domain starts with the last cache directory chosen, in any scene.
    if self.smoke_object_type == "domain" and not self.output_dir.strip():
        from .utils import get_addon_preferences
        prefs = get_addon_preferences()
        remembered = getattr(prefs, "default_output_dir", "").strip() if prefs else ""
        if remembered:
            self.output_dir = remembered


def _output_dir_updated(self, context):
    """Remember a domain's cache directory for new domains (saved with the
    preferences)."""
    if self.smoke_object_type != "domain":
        return
    from .utils import get_addon_preferences
    prefs = get_addon_preferences()
    value = self.output_dir.strip()
    if prefs is not None and value and prefs.default_output_dir != value:
        prefs.default_output_dir = value
        context.preferences.is_dirty = True


def _compare_view_updated(self, _context):
    from .compare import apply_compare_view
    apply_compare_view(self.id_data)


def _cache_slot_updated(self, _context):
    from .bake_state import sync_cache_slot
    sync_cache_slot(self.id_data)


def _viewport_visible_updated(self, _context):
    from .importers import set_domain_viewport_visible
    domain = self.id_data
    if isinstance(domain, bpy.types.Object) and self.smoke_object_type == "domain":
        set_domain_viewport_visible(domain, self.viewport_visible)


def _appearance_updated(self, _context):
    from .importers import update_generated_materials
    from .preview import refresh_preview_display

    domain = getattr(self, "id_data", None)
    if domain is not None:
        try:
            update_generated_materials(domain, self)
        except (AttributeError, ReferenceError, RuntimeError) as error:
            print(f"Fumaris material update failed: {error}")
    refresh_preview_display()


def _preview_light_angles(props):
    x, y, z = props.preview_light_direction
    if not all(isfinite(value) for value in (x, y, z)) or hypot(x, y, z) < 1e-6:
        x = y = z = 1.0
    return atan2(y, x), atan2(z, hypot(x, y))


def _set_preview_light_angles(props, azimuth, elevation):
    if not isfinite(azimuth) or not isfinite(elevation):
        return
    elevation = max(-pi / 2, min(pi / 2, elevation))
    horizontal = cos(elevation)
    # Keep the existing world-space vector as the sole saved/animated value.
    props.preview_light_direction = (
        horizontal * cos(azimuth), horizontal * sin(azimuth), sin(elevation),
    )


def _preview_light_azimuth_get(props):
    return _preview_light_angles(props)[0]


def _preview_light_azimuth_set(props, value):
    _set_preview_light_angles(props, value, _preview_light_angles(props)[1])


def _preview_light_elevation_get(props):
    return _preview_light_angles(props)[1]


def _preview_light_elevation_set(props, value):
    _set_preview_light_angles(props, _preview_light_angles(props)[0], value)


_SOURCE_OPERATIONS = (
    ("blend", "Blend", "Pull the field toward the emitter value at the coupling rate. Overlapping emitters settle on a mix, and a low value thins denser smoke"),
    ("add", "Add", "Inject value × coupling every second. Overlapping emitters accumulate and nothing is removed"),
    ("max", "Maximum", "Raise the field toward the emitter value but never lower what is already there"),
    ("replace", "Replace", "Set the field to the emitter value everywhere inside the emitter"),
)


# Looks of Blender's color management, by the number files store. AgX has its
# own set ("AgX - Punchy"); every other view takes the plain contrast looks.
_PREVIEW_LOOKS_AGX = (
    ("none", "None", "No look", 0),
    ("punchy", "Punchy", "More saturated and contrasty; fire reads warmer", 1),
    ("greyscale", "Greyscale", "Black and white", 2),
    ("very_high", "Very High Contrast", "", 3),
    ("high", "High Contrast", "", 4),
    ("medium_high", "Medium High Contrast", "", 5),
    ("base", "Base Contrast", "", 6),
    ("medium_low", "Medium Low Contrast", "", 7),
    ("low", "Low Contrast", "", 8),
    ("very_low", "Very Low Contrast", "", 9),
)
_PREVIEW_LOOKS_PLAIN = tuple(
    (identifier, "Medium Contrast" if identifier == "base" else name, description, number)
    for identifier, name, description, number in _PREVIEW_LOOKS_AGX
    if identifier not in {"punchy", "greyscale"})


def _preview_look_items(self, _context):
    return _PREVIEW_LOOKS_AGX if self.preview_tone_mapping == "agx" else _PREVIEW_LOOKS_PLAIN


class FumarisSettings(PropertyGroup):
    """Custom PropertyGroup storing all simulation parameters for Fumaris smoke simulation."""

    show_diagnostics: BoolProperty(name="Diagnostics", default=False)

    smoke_object_type: EnumProperty(
        name="Type",
        description="What this object is in a Fumaris simulation: the Domain that simulates, or an emitter, collider, effector or outflow",
        items=[
            ("domain", "Domain", "Bake an isolated simulation domain"),
            ("emitter", "Emitter", "Emit smoke into a domain that references this object"),
            ("collider", "Collider", "Collide with smoke in a domain that references this object"),
            ("effector", "Effector", "Apply velocity forces to smoke in a domain"),
            ("outflow", "Outflow", "Remove smoke, fire, and fuel inside this object"),
            ("none", "None", "Exclude this object from Fumaris simulations"),
        ],
        default="none",
        update=_object_type_updated,
    )

    participant_enabled: BoolProperty(
        name="Enabled",
        description="Use this object when it appears in a domain emitter/collider collection",
        default=True,
    )

    participant_type: EnumProperty(
        name="Participant Type",
        description="Emitter source shape used when this object is in a domain emitter collection",
        items=[
            ("mesh", "Mesh", "Emit from the object's final mesh, after all modifiers (Geometry Nodes included). A mesh that only moves as an object is sent to the GPU once, then just its transform each frame. Instances that are not realized are ignored"),
            ("box", "Box", "Box emitter using this object's transform"),
            ("sphere", "Sphere", "Sphere emitter at this object's origin"),
            ("particles", "Particles", "Blender particle system emitter"),
            ("geometry_nodes", "Geometry Nodes", "Use this object's Geometry Nodes output as points, a mesh or volume grids, including instances that are not realized. Its geometry is sent again every frame"),
            ("openvdb", "OpenVDB", "Load an OpenVDB file as a volume emitter"),
            ("collection_points", "Collection Points", "Batch mesh objects from a collection as spheres with automatic motion velocity"),
            ("collection_mesh", "Collection Mesh", "Combine collection meshes into one emitter with shared channels and automatic motion velocity"),
        ],
        default="mesh",
    )

    source_collection: PointerProperty(
        name="Source Collection",
        description="Source mesh objects, including child collections; source objects need no Fumaris role",
        type=bpy.types.Collection,
    )

    collection_point_center: EnumProperty(
        name="Point Center",
        description="Where each source object of the collection emits from",
        items=[
            ("bounds", "Bounds Center", "Place each point at the evaluated mesh bounds center"),
            ("origin", "Object Origin", "Place each point at its evaluated object origin"),
        ],
        default="bounds",
    )

    collection_radius_mode: EnumProperty(
        name="Radius From",
        description="How the emitting radius of each source object is chosen",
        items=[
            ("bounds", "Object Size", "Use half the largest scaled local bounding-box dimension; approximates each mesh as a sphere"),
            ("fixed", "Fixed Radius", "Use the same world-space radius for all source objects"),
        ],
        default="bounds",
    )

    collection_radius_scale: FloatProperty(
        name="Radius Scale",
        description="Multiplier for radii derived from object size",
        default=1.0,
        min=0.0,
        soft_max=5.0,
    )

    gn_subtype: EnumProperty(
        name="Geometry Type",
        description="Type of evaluated Geometry Nodes output to use",
        items=[
            ("point_cloud", "Point Cloud", "Use evaluated points as per-point sphere emitters"),
            ("mesh", "Mesh", "Emit from the output mesh and its instances (no Realize Instances needed), resent every frame. For an ordinary mesh without instances, Shape Mesh does the same and is cheaper when the mesh does not deform"),
            (
                "volume",
                "Volume",
                "Use evaluated Geometry Nodes volume grids",
            ),
        ],
        default="point_cloud",
    )

    particle_subtype: EnumProperty(
        name="Particle Type",
        description="How to emit from the particle-system object",
        items=[
            ("point_cloud", "Point Cloud", "Use particle positions as per-particle sphere emitters"),
            ("mesh", "Mesh", "Use the evaluated object mesh as a mesh emitter"),
        ],
        default="point_cloud",
    )


    collider_type: EnumProperty(
        name="Collider Type",
        description="Shape the collider blocks smoke with",
        items=[
            ("mesh", "Mesh", "The evaluated mesh, as it deforms"),
            ("box", "Box", "A box from this object's transform (fastest)"),
            ("sphere", "Sphere", "Use a spherical velocity/pressure influence collider"),
        ],
        default="mesh",
    )

    # Emitter Settings
    emitter_radius: FloatProperty(
        name="Emitter Radius",
        description="Radius of the spherical emitter",
        default=1.0,
        min=1e-12,
        subtype="DISTANCE",
        soft_min=0.01,
    )

    emitter_temperature: FloatProperty(
        name="Emitter Temperature",
        description="Source temperature as a share of the domain's Flame Temperature: 1 is Flame Temperature",
        default=0.35,
        min=0.0,
        soft_max=5.0,
    )

    emitter_influence: BoolProperty(
        name="Influence Only",
        description="Act on existing smoke instead of emitting: steer it, heat it and expand it, without adding smoke or fuel",
        default=False,
    )

    emitter_smoke: FloatProperty(
        name="Smoke Density",
        description="Density of emitted smoke",
        default=5.0,
        min=0.0,
        soft_max=5.0,
    )

    emitter_color: FloatVectorProperty(
        name="Smoke Color",
        description="Color of this emitter's smoke, with Per-Emitter Smoke Color on. Not the flame",
        default=(1.0, 1.0, 1.0), min=0.0, soft_max=1.0, size=3, subtype="COLOR",
    )

    emitter_fuel: FloatProperty(
        name="Fuel",
        description="Combustible fuel emitted into the simulation",
        default=1.0,
        min=0.0,
        soft_max=5.0,
    )

    emitter_burning: BoolProperty(
        name="Burning",
        description="Burn in place like a gas jet: heat the gas, add smoke and expand it, without emitting fuel",
        default=False,
    )

    emitter_burn_rate: FloatProperty(
        name="Burn Rate",
        description="How strongly the Burning emitter burns, as fuel burnt per second: it scales the heat, smoke and expansion it makes",
        default=2.0,
        min=0.0,
        soft_max=20.0,
    )

    emitter_burn: FloatProperty(
        name="Burn",
        description="Initial burn/flame amount emitted into the simulation",
        default=0.0,
        min=0.0,
        soft_max=5.0,
    )

    # 0.2 matches the push of the older per-cell default Divergence 2 (2026-11-04).
    emitter_divergence: FloatProperty(
        name="Expansion",
        description="How fast the emitter adds volume, pushing smoke outward (negative pulls inward)",
        default=0.2,
        soft_min=-10.0,
        soft_max=10.0,
    )

    emitter_global_influence: FloatProperty(
        name="Influence",
        description="Scale this emitter's channel coupling and expansion together. 0 turns the emitter off; 1 keeps its usual strength",
        default=1.0,
        min=0.0,
        soft_max=1.0,
    )

    # Flow exposes an independent coupling rate for every emitted channel.
    couple_rate_velocity: FloatProperty(
        name="Velocity",
        description="How strongly the emitter pushes smoke along its velocity or movement",
        default=8.0,
        min=0.0,
        soft_max=200.0,
    )

    couple_rate_divergence: FloatProperty(
        name="Expansion",
        description="How strongly the emitter applies its Expansion",
        default=2.0,
        min=0.0,
        soft_max=200.0,
    )

    couple_rate_temperature: FloatProperty(
        name="Temperature",
        description="How quickly the emitter heats the gas in its shape",
        default=10.0,
        min=0.0,
        soft_max=200.0,
    )

    couple_rate_fuel: FloatProperty(
        name="Fuel",
        description="How quickly the emitter supplies its Fuel",
        default=2.0,
        min=0.0,
        soft_max=200.0,
    )

    couple_rate_burn: FloatProperty(
        name="Burn",
        description="How quickly the emitter applies its Burn to start combustion",
        default=2.0,
        min=0.0,
        soft_max=200.0,
    )

    couple_rate_smoke: FloatProperty(
        name="Smoke",
        description="How quickly the emitter fills its shape with Smoke",
        default=2.0,
        min=0.0,
        soft_max=200.0,
    )

    # How each emitted channel is written into the simulation: smoke, fuel and
    # burn Add (2026-11-08, user); velocity keeps Blend (Add would keep
    # accelerating the air like a force). Temperature is Maximum at coupling 10
    # (2026-11-09): the source holds its gas at Temperature (0.35 = 700 K, past
    # the 500 K ignition in about 0.1 s) and burning makes the rest of the heat,
    # so fire colour varies; Add heated lingering gas to Flame Temperature.
    source_op_smoke: EnumProperty(
        name="Smoke Operation",
        description="How this emitter writes smoke into the simulation",
        items=_SOURCE_OPERATIONS,
        default="add",
    )

    source_op_temperature: EnumProperty(
        name="Temperature Operation",
        description="How this emitter writes temperature into the simulation",
        items=_SOURCE_OPERATIONS,
        default="max",
    )

    source_op_fuel: EnumProperty(
        name="Fuel Operation",
        description="How this emitter writes fuel into the simulation",
        items=_SOURCE_OPERATIONS,
        default="add",
    )

    source_op_burn: EnumProperty(
        name="Burn Operation",
        description="How this emitter writes burn into the simulation",
        items=_SOURCE_OPERATIONS,
        default="add",
    )

    source_op_divergence: EnumProperty(
        name="Expansion Operation",
        description="How this emitter writes its Expansion into the simulation",
        items=(
            ("max", "Maximum", "Apply the Expansion exactly wherever the emitter couples, at any coupling, resolution and Sub-Steps; the stronger of it and existing expansion (combustion, other emitters) applies"),
            ("blend", "Blend", "Pull the expansion toward the emitter's at the Divergence coupling rate each step: lower coupling expands more gently. Depends on Sub-Steps, as Blend does for other channels"),
            ("replace", "Replace", "Set the expansion to the emitter's everywhere inside the emitter, replacing combustion's"),
        ),
        default="max",
    )

    source_op_velocity: EnumProperty(
        name="Velocity Operation",
        description="How velocity is written, per axis: Add pushes like a force, Maximum only raises, Blend pulls toward it, Replace sets it",
        items=_SOURCE_OPERATIONS,
        default="blend",
    )

    emitter_apply_post_pressure: BoolProperty(
        name="Apply After Pressure",
        description="Apply this emitter after the pressure solve instead of before it",
        default=False,
    )

    sphere_multisample: BoolProperty(
        name="Motion Multisampling",
        description="Sample a moving sphere emitter multiple times to reduce gaps during fast motion",
        default=False,
    )

    sphere_trace_samples: IntProperty(
        name="Trace Samples",
        description="Number of additional motion samples for a moving sphere emitter",
        default=4,
        min=1,
        max=256,
        soft_max=64,
    )

    point_enable_interpolation: BoolProperty(
        name="Interpolate Points",
        description="Interpolate point emitter data to reduce flicker between sparse samples",
        default=True,
    )

    # Simulation Settings
    velocity: FloatVectorProperty(
        name="Emission Velocity",
        description="Velocity the emitter blows its smoke with (in addition to its own motion), at the Velocity coupling rate",
        default=(0.0, 0.0, 0.0),
        size=3,
        subtype="VELOCITY",
    )

    normal_velocity: FloatProperty(
        name="Normal Velocity",
        description="Air blown out along the mesh normals (negative sucks in). An open mesh such as a plane acts as a fan",
        default=0.0,
        soft_min=-100.0,
        soft_max=100.0,
    )

    motion_velocity_scale: FloatProperty(
        name="Motion Velocity Scale",
        description="Scale of the source's own motion velocity: 1 uses it fully, 0 ignores it",
        default=1.0,
        min=0.0,
        soft_max=25.0,
    )

    motion_substeps: IntProperty(
        name="Motion Sub-Steps",
        description="Extra source poses sampled between frames; only affects moving or deforming geometry",
        default=8,
        min=1,
        # Flow stores intermediate emitter transforms in a fixed 32-entry array.
        max=32,
        soft_max=16,
    )

    use_scene_frame_range: BoolProperty(
        name="Use Timeline",
        description="Simulate the scene's frame range (plus Warmup) instead of a separate Start and End",
        default=True,
    )

    warmup_frames: IntProperty(
        name="Warmup",
        description="Frames simulated before the timeline's start so the smoke is already established on the first visible frame",
        default=0,
        min=0,
        soft_max=100,
    )

    sim_start_frame: IntProperty(
        name="Start Frame",
        description="First Fumaris simulation frame; can be below 0 for warmup before the visible timeline",
        default=1,
        soft_min=-250,
        soft_max=250,
    )

    sim_end_frame: IntProperty(
        name="End Frame",
        description="Last Fumaris simulation frame, independent from the Blender timeline end frame",
        default=250,
        soft_min=-250,
        soft_max=250,
    )

    resolution: IntProperty(
        name="Resolution",
        description="Detail of the simulation: voxel size is 32 / Resolution (1024 ≈ 3 cm). Higher is finer but needs more VRAM and time",
        default=256,
        min=1,
        # No hard maximum: Resolution is an absolute voxel size and physical-
        # scale scenes need values far above 4096 (exported up to 1e9). A
        # value the emitter cannot be simulated at fills the pool and says so
        # (EmitterMesh's block walk is bounded; it hung frame 1 before, beta 3
        # review R30).
        soft_max=4096,
        soft_min=16,
    )


    preview_resolution_percent: FloatProperty(
        name="Preview Resolution",
        description="Percentage of the domain resolution used for live preview simulation; bake always uses full resolution",
        default=100.0,
        min=0.0,
        subtype="PERCENTAGE",
        soft_max=100.0,
    )


    preview_image_scale: FloatProperty(
        name="Viewport Scale",
        description="Sharpness of the live preview image; lower this to speed up the viewport without changing the simulation or bake",
        default=50.0,
        min=0.001,
        subtype="PERCENTAGE",
        soft_min=25.0,
        soft_max=100.0,
    )


    preview_step_size: FloatProperty(
        name="Step Size",
        description="Distance between preview ray samples, in cells. Smaller is smoother and slower",
        default=1.0, min=0.25, soft_max=2.0,
    )

    preview_shadows: BoolProperty(
        name="Self Shadows",
        description="Smoke shadows itself from the strongest light and the sky. Off is faster. Preview only",
        default=True,
    )

    preview_block_line_width: FloatProperty(
        name="Line Width",
        description="Active Blocks: width of the block outlines, in simulation cells",
        default=0.7, min=0.05, soft_max=2.0,
    )

    preview_field_view: EnumProperty(
        name="Show",
        description="What the live preview shows",
        items=[
            ("look", "Smoke & Fire", "Smoke and fire as the domain's Look settings render them"),
            ("speed", "Speed", "How fast the air moves, drawn as fire: still air is dark, about 10 m/s dim red, 20 m/s red, 50 m/s and faster orange (never white)"),
            ("expansion", "Expansion", "Where burning gas expands (Gas Expansion), drawn as fire: about 4 per second glows at Full Glow"),
            ("fuel", "Fuel", "Unburnt fuel, drawn as fire on a log scale: 1 glows at Full Glow, 0.1 dimly, 8 brightly. Shows where fuel waits for heat or air"),
            ("blocks", "Active Blocks", "The smoke as it looks, with the outlines of the simulated blocks: shows where the solver spends memory and time"),
        ],
        default="look",
    )

    preview_frame_rate: EnumProperty(
        name="Frame Rate",
        description="How fast the live preview plays simulation frames. Preview only; bakes are not paced",
        items=(
            ("SCENE", "Scene", "The scene's frame rate (Output Properties > Frame Rate), so motion plays at real speed"),
            ("UNCAPPED", "Uncapped", "As fast as the GPU simulates: a frame starts as soon as the last one is done"),
            ("CUSTOM", "Custom", "A frame rate of your own (Custom FPS)"),
        ),
        default="SCENE",
    )

    preview_custom_fps: FloatProperty(
        name="FPS",
        description="Live preview frame rate when Frame Rate is Custom",
        default=30.0, min=1.0, soft_max=240.0,
    )

    preview_all_viewports: BoolProperty(
        name="All Viewports",
        description="Show the live preview in every 3D viewport that shows the domain. Each extra viewport costs render time",
        default=False,
    )

    preview_occlusion: BoolProperty(
        name="Scene Occlusion",
        description="Hide preview smoke behind solid objects using the viewport depth. Preview only; transparent objects still occlude",
        default=True,
    )




    preview_lighting: EnumProperty(
        name="Lighting",
        description="What lights the preview smoke",
        items=(
            ("SCENE", "Scene", "The scene's lights and world, as Cycles renders them: the strongest light (or the sun in an HDRI) casts shadows, the world lights the smoke where it sees the sky"),
            ("CUSTOM", "Custom", "One preview light with its own direction and strength, and a neutral sky (Sky), instead of the scene's lights and world: the same lighting in every scene. Preview only"),
        ),
        default="SCENE",
    )

    preview_light_strength: FloatProperty(
        name="Strength",
        description="Strength of the custom preview light, like a Sun light's Strength (W/m²). Preview only",
        default=3.0, min=0.0, soft_max=20.0,
    )

    preview_custom_sky: FloatProperty(
        name="Sky",
        description="Brightness of a uniform grey sky around the smoke, for soft ambient shading. Preview only",
        default=0.3, min=0.0, soft_max=2.0,
    )

    preview_fire_lighting: FloatProperty(
        name="Fire Lighting",
        description="How strongly flames light the surrounding smoke; 1 matches Cycles. Preview only",
        default=1.0, min=0.0, soft_max=2.0,
    )

    preview_exposure_offset: FloatProperty(
        name="Exposure",
        description="Preview brightness in stops, added to the scene's Exposure (Color Management). 0 matches the render. Preview only",
        default=0.0, max=126.0, soft_min=-4.0, soft_max=4.0,
    )

    preview_tone_mapping: EnumProperty(
        name="View",
        description="View transform of the preview. Scene matches the render's color management. Preview only",
        # Fixed numbers: files store the number, and 1.3.1 had filmic, none,
        # agx as 0, 1, 2 (Scene was added first in the list in 2.0).
        items=(
            ("scene", "Scene", "Follow the scene's View Transform, Look and Display, so the preview tone maps like the render", 3),
            ("none", "Standard", "Blender's Standard view transform: plain sRGB, values above the display range clip to white", 1),
            ("khronos", "Khronos PBR Neutral", "Blender's Khronos PBR Neutral view transform", 4),
            ("agx", "AgX", "Blender's AgX view transform", 2),
            ("filmic", "Filmic", "Blender's Filmic view transform", 0),
            ("aces20", "ACES 2.0", "Blender's ACES 2.0 view transform", 5),
            ("aces13", "ACES 1.3", "Blender's ACES 1.3 view transform", 6),
        ),
        default="scene",
    )

    preview_look: EnumProperty(
        name="Look",
        description="Look for the preview's View, as in Color Management. Preview only",
        items=_preview_look_items,
    )


    preview_light_direction: FloatVectorProperty(
        name="Light Direction",
        description="Direction toward the preview light (Z up). Preview only",
        default=(0.57735027, 0.57735027, 0.57735027),
        size=3,
        subtype="DIRECTION",
    )

    preview_light_azimuth: FloatProperty(
        name="Azimuth",
        description="Orbit the preview light around world Z. Preview only",
        subtype="ANGLE", soft_min=-pi, soft_max=pi,
        get=_preview_light_azimuth_get, set=_preview_light_azimuth_set,
        options=set(),
    )

    preview_light_elevation: FloatProperty(
        name="Elevation",
        description="Height of the preview light: 0° level, 90° overhead. Preview only",
        subtype="ANGLE", min=-pi / 2, max=pi / 2,
        get=_preview_light_elevation_get, set=_preview_light_elevation_set,
        options=set(),
    )


    preview_bake: BoolProperty(
        name="Preview While Baking",
        description="Show the live preview while baking. Off bakes faster. Not used by background bakes",
        default=True,
    )

    preview_loop: BoolProperty(
        name="Loop",
        description="Start over at the end of the frame range. Off: pause on the last frame",
        default=True,
    )

    preview_cache_step: EnumProperty(
        name="Cache Every",
        description="Keep every Nth frame of the preview. Fewer frames use less RAM",
        items=(
            ("1", "Every Frame", "Keep every frame: smooth scrubbing and replay"),
            ("2", "Every 2nd Frame", "Keep every 2nd frame"),
            ("3", "Every 3rd Frame", "Keep every 3rd frame"),
            ("4", "Every 4th Frame", "Keep every 4th frame"),
            ("AUTO", "Auto", "As many frames as copying them to system memory allows in about a fifth of the frame time: small scenes keep most frames, large ones every few (fewer on slower PCIe links)"),
        ),
        default="1",
    )

    preview_cache: BoolProperty(
        name="Cache Preview",
        description="Keep the preview's frames in RAM to scrub, replay and save them as a version. Applies from the next Play",
        default=False,
    )



    simulation_memory: FloatProperty(
        name="VRAM",
        description="GPU memory for the simulation, in GB. More lets larger or finer smoke fit before it is cut off",
        default=4.0,
        min=0.1,
        # Largest float32 GiB value whose byte count fits uint64 in the bridge.
        max=17179868160.0,
        soft_max=24.0,
        step=25,
        precision=2,
    )

    simulation_memory_auto: BoolProperty(
        name="Auto VRAM",
        description="Use the GPU memory free at start, minus a small reserve for Blender and other programs",
        default=False,
        update=auto_toggled,
    )

    auto_cell_size: BoolProperty(
        name="Auto Cell Size",
        description="Automatically reduce simulation detail when smoke runs out of blocks, instead of cutting the smoke off",
        default=False,
    )

    color_mixing: BoolProperty(
        # (identifier kept as color_mixing: saved files and scripts use it)
        name="Per-Emitter Smoke Color",
        description=("Give each emitter's smoke its own color, mixing where plumes meet. Flame color is set for the whole domain. Costs about 1.4x the simulation time and a third of the blocks per GB"),
        default=False,
    )

    # Smoke mass correction (2026-11-14): the semi-Lagrangian scheme gains smoke
    # where gas expands and loses it where it swirls; a per-step global scale
    # (within 5%) keeps the transported total. Dissipation and Decay still apply.
    conserve_smoke: BoolProperty(
        name="Conserve Smoke",
        description="Keep the total amount of smoke steady as it moves, so it doesn't drift up or down. No visible cost",
        default=True,
    )

    # Full-Resolution Channels' old name, kept for scripts and old files: it
    # reads and sets Full-Resolution Channels. A file saved with Full-Resolution
    # Velocity on still has that value stored under this name; it counts as on
    # until the toggle is next changed.
    full_resolution_velocity: BoolProperty(
        name="Full-Resolution Channels",
        description="Run motion at full resolution, not half: rounder, finer billows for explosions. About 3-4x the VRAM and 1.5-4x the time",
        get=_full_resolution_get,
        set=_full_resolution_set,
    )


    physics_convex_collision: BoolProperty(
        name="Convex Collision",
        description="Treat closed convex colliders as filled solids",
        default=True,
    )

    sparse_block_min_lifetime: IntProperty(
        name="Block Minimum Lifetime",
        description="How long to keep empty simulation space before freeing it. Higher reallocates less but uses more VRAM",
        default=4,
        min=0,
        soft_max=120,
    )

    allocation_smoke_threshold: FloatProperty(
        name="Smoke Allocation Threshold",
        description="Retain sparse blocks whose smoke exceeds this value",
        default=0.02,
        min=0.0,
        soft_max=1.0,
    )

    allocation_speed_threshold: FloatProperty(
        name="Speed Allocation Threshold",
        description="Retain sparse blocks whose velocity exceeds this value",
        default=1.0,
        min=0.0,
        soft_max=100.0,
    )

    air_margin: FloatProperty(
        name="Air Margin",
        description="Clear air simulated around the smoke. More room lets plumes roll into caps, at more GPU time and memory",
        default=0.5,
        min=0.0,
        soft_max=4.0,
        subtype="DISTANCE",
    )

    follow_object: BoolProperty(
        name="Move With Object",
        description="Simulate in this object's own space, so the simulation travels with it (for vehicles and moving rigs)",
        default=False,
    )

    carry_air: FloatProperty(
        name="Carry Air",
        description="How much the surrounding air moves with the domain: 0 still air (smoke trails behind), 1 carried along",
        default=0.0,
        min=0.0,
        max=1.0,
        subtype="FACTOR",
    )

    simulation_speed: FloatProperty(
        name="Simulation Speed",
        description="Playback speed of the simulation's motion; 1 is normal, 0.5 is half speed. Can be animated for slow motion",
        default=1.0,
        min=0.001,
        soft_max=4.0,
    )

    # Output Settings
    output_prefix: StringProperty(
        name="Output Prefix",
        description="Prefix for output files",
        default="",
    )

    output_dir: StringProperty(
        name="Output Directory",
        description="Directory for output files. New domains start with the last one you set (Fumaris preferences)",
        default="",
        subtype="DIR_PATH",
        update=_output_dir_updated,
    )

    # The stored value of Full-Resolution Channels; the panel shows
    # full_resolution_velocity, which also counts the old option's value.
    full_resolution_channels: BoolProperty(
        name="Full-Resolution Channels",
        description="Run motion at full resolution, not half: rounder, finer billows for explosions. About 3-4x the VRAM and 1.5-4x the time",
        default=False,
    )

    # Not in the panel: tests force all four density fields even where the
    # scene does not use them.
    density_storage: EnumProperty(
        name="Density Storage",
        description="Which fields are stored. Auto keeps only what the scene uses",
        items=(
            ("auto", "Auto", "Only the fields the scene uses"),
            ("all", "All Channels", "Always store temperature, fuel, burn and smoke at full resolution"),
        ),
        default="auto",
    )

    cache_slot: StringProperty(
        name="Version",
        description="The current version, where bakes go. Switching shows it and hides the others",
        default="main",
        update=_cache_slot_updated,
    )

    # Versions: a second version shown with the current one (its eye on).
    # UI state, never in presets.
    compare_slot: StringProperty(
        name="Compared Version",
        description="Version shown with the current one",
        default="",
    )

    compare_view: EnumProperty(
        name="Show",
        description="What the viewport shows while a second version is shown (set by the versions' eyes)",
        items=(
            ("A", "A", "Only the current version"),
            ("B", "B", "Only the compared version"),
            ("SIDE", "Side by Side", "Both, the compared one moved beside the domain"),
        ),
        default="SIDE",
        update=_compare_view_updated,
    )

    compare_selected: StringProperty(
        name="Selected Version",
        description="The entry selected in Versions",
        default="",
        options={"HIDDEN"},
    )

    show_compare_changes: BoolProperty(
        name="What Changed",
        description="List the settings that differ between the two versions shown",
        default=True,
    )

    viewport_visible: BoolProperty(
        name="Show in Viewport",
        description="Show this domain, its preview and its imported volumes in the viewport. Renders are not affected",
        default=True,
        update=_viewport_visible_updated,
    )

    cache_id: StringProperty(
        name="Cache ID",
        description="Stable internal identity for this domain's cache namespace",
        default="",
        options={"HIDDEN"},
    )


    volume_material: PointerProperty(
        name="Volume Material",
        description="Optional material assigned to imported Fumaris volume objects",
        type=bpy.types.Material,
    )

    managed_material: PointerProperty(
        name="Synced Material",
        description="The material Fumaris keeps in step with the domain's Look, shared by the versions with Sync Material on",
        type=bpy.types.Material,
    )

    volume_selectable: BoolProperty(
        name="Selectable Volume",
        description="Allow imported Fumaris volume objects to be selectable in the viewport",
        default=False,
    )

    vdb_compression: EnumProperty(
        name="VDB Compression",
        description="VDB compression. Active Mask writes fastest; ZIP and Blosc give smaller, slower files",
        items=[
            ("active_mask", "Active Mask", "Fast Fumaris default; stores active masks without value compression"),
            ("none", "None", "No OpenVDB compression"),
            ("zip", "ZIP", "ZIP value compression"),
            ("zip_active_mask", "ZIP + Active Mask", "ZIP value compression with active mask compression"),
            ("blosc", "Blosc", "Blosc value compression"),
            ("blosc_active_mask", "Blosc + Active Mask", "Blosc value compression with active mask compression"),
        ],
        default="active_mask",
    )

    vdb_precision: EnumProperty(
        name="VDB Precision",
        description="Number precision of the written VDB files",
        items=[
            ("half", "Half (16-bit)", "Half the disk space and write time; differences are far below what renders show. Default"),
            ("full", "Full (32-bit)", "Exact simulation values, for pipelines that read the VDBs back as data"),
        ],
        default="half",
    )

    # Measured on a 1024 Explosion at 10 GB of GPU memory, 20-thread CPU
    # (output/perf_pass, 2026-11-08): bridge RAM 4.0 GB with 10 writers.
    vdb_writers: EnumProperty(
        name="VDB Writers",
        description="Frames written to VDB at once while baking. Lower it if a bake runs out of RAM",
        items=(
            ("FASTEST", "Fastest", "One writer per two CPU cores, up to 12: the fastest bakes, the most RAM", 0),
            ("BALANCED", "Balanced", "Up to 6 writers: as fast as Fastest with about 22% less RAM (default)", 1),
            ("LOW", "Low Memory", "Up to 4 writers: about 40% less RAM in large bakes (4.0 to 2.4 GB) for about 5% more time", 2),
            ("ULTRA", "Ultra Low Memory", "1 writer, one frame at a time: the least RAM (about half), for several times the bake time", 3),
        ),
        default="BALANCED",
    )

    # Object References
    emitter_collection: PointerProperty(
        name="Emitters Collection",
        description="Collection of the objects that emit into this simulation",
        type=bpy.types.Collection,
    )

    collider_collection: PointerProperty(
        name="Collider Collection",
        description="Collection of mesh objects to use as collision obstacles for this simulation",
        type=bpy.types.Collection,
    )

    collider_margin: FloatProperty(
        name="Collider Margin",
        description="How far from its surface a moving collider pushes the air, so it shoves smoke ahead of it",
        default=0.3,
        min=0.0,
        soft_max=5.0,
        subtype="DISTANCE",
    )

    collider_radius: FloatProperty(
        name="Collider Radius",
        description="Radius for sphere collider participants",
        default=1.0,
        min=1e-12,
        soft_max=20.0,
        subtype="DISTANCE",
        soft_min=0.01,
    )

    collider_solid: BoolProperty(
        name="Solid Boundary",
        description="Block smoke and remove it from inside the collider. Off: only its motion affects the air",
        default=True,
    )

    collider_velocity_influence: FloatProperty(
        name="Velocity Influence",
        description="How strongly collider motion influences nearby smoke velocity",
        default=1.0,
        min=0.0,
        soft_max=10.0,
    )


    effector_collection: PointerProperty(
        name="Effector Collection",
        description="Collection of the effector objects (forces) acting on this simulation",
        type=bpy.types.Collection,
    )

    outflow_collection: PointerProperty(
        name="Outflow Collection",
        description="Collection of mesh objects that remove smoke, fire, and fuel from this simulation",
        type=bpy.types.Collection,
    )

    particle_system_name: StringProperty(
        name="Particle System",
        description="Name of the particle system to use as emitter (for particles emitter type)",
        default="",
    )

    point_radius: FloatProperty(
        name="Point Radius",
        description="Base point emitter radius in meters; an explicitly mapped Radius attribute multiplies this per point",
        default=1.0,
        min=0.0,
        soft_max=10.0,
        subtype="DISTANCE",
    )


    show_attribute_settings: BoolProperty(
        name="Attributes",
        description="Show point-cloud attribute mapping controls",
        default=False,
    )

    attr_position: StringProperty(
        name="Position",
        description="Point position attribute; empty uses position/Position",
        default="",
    )

    attr_velocity: StringProperty(
        name="Velocity",
        description="Point velocity attribute; empty uses velocity/Velocity/vel/v",
        default="",
    )

    attr_radius: StringProperty(
        name="Radius",
        description="Per-point radius scale; empty automatically uses radius, Radius, width, or pscale",
        default="",
    )

    attr_smoke: StringProperty(
        name="Smoke",
        description="Point attribute for Smoke; empty uses the emitter's value",
        default="",
    )

    attr_temperature: StringProperty(
        name="Temperature",
        description="Point attribute for Temperature; empty uses the emitter's value",
        default="",
    )

    attr_fuel: StringProperty(
        name="Fuel",
        description="Point attribute for Fuel; empty uses the emitter's value",
        default="",
    )

    attr_burn: StringProperty(
        name="Burn",
        description="Point attribute for Burn; empty uses the emitter's value",
        default="",
    )

    attr_smoke_coupling: StringProperty(
        name="Smoke Coupling",
        description="Point attribute for Smoke Coupling; empty uses the emitter's value",
        default="",
    )

    attr_temperature_coupling: StringProperty(
        name="Temperature Coupling",
        description="Point attribute for Temperature Coupling; empty uses the emitter's value",
        default="",
    )

    attr_velocity_coupling: StringProperty(
        name="Velocity Coupling",
        description="Point attribute for Velocity Coupling; empty uses the emitter's value",
        default="",
    )

    attr_divergence: StringProperty(
        name="Divergence",
        description="Point attribute for Expansion; empty uses the emitter's value",
        default="",
    )

    attr_mask: StringProperty(
        name="Mask",
        description="Emission mask/enabled attribute; empty uses enabled/mask/emit",
        default="",
    )

    volume_filepath: StringProperty(
        name="OpenVDB File",
        description="OpenVDB file used by this volume emitter",
        default="",
        subtype="FILE_PATH",
    )

    volume_use_sequence: BoolProperty(
        name="Sequence",
        description="Read the file name's number as a frame and load a file per frame",
        default=False,
    )

    volume_frame_offset: IntProperty(
        name="Frame Offset",
        description="File frame loaded on scene frame 0; a positive value starts later in the sequence",
        default=0,
    )

    volume_use_velocity: BoolProperty(
        name="Emit Velocity Grid",
        description="Push the smoke with the file's velocity grid",
        default=False,
    )

    volume_grid_density: StringProperty(
        name="Density Grid",
        description="Grid emitted as smoke; empty uses density, or the first float grid",
        default="",
    )

    volume_grid_temperature: StringProperty(
        name="Temperature Grid",
        description="Grid emitted as temperature; empty uses temperature",
        default="",
    )

    volume_grid_fuel: StringProperty(
        name="Fuel Grid",
        description="Grid emitted as fuel when Fuel is above zero; empty uses fuel",
        default="",
    )

    volume_grid_burn: StringProperty(
        name="Burn Grid",
        description="Grid emitted as burn when Burn and its coupling are above zero; empty uses burn",
        default="",
    )

    volume_grid_velocity: StringProperty(
        name="Velocity Grid",
        description="Vector grid emitted as velocity; empty uses velocity",
        default="",
    )

    mesh_emission_mode: EnumProperty(
        name="Mesh Emission",
        description="How a mesh emits smoke",
        items=[
            ("surface", "Surface", "Emit in a band around the mesh surface"),
            ("volume", "Volume", "Emit inside the mesh, inward from its surface up to Emission Distance"),
        ],
        default="surface",
    )

    mesh_emission_distance: FloatProperty(
        name="Emission Distance",
        description="Distance band used for mesh emission",
        default=0.3,
        min=0.0,
        soft_max=5.0,
        subtype="DISTANCE",
    )

    mesh_emission_mask_attribute: StringProperty(
        name="Emission Mask Attribute",
        description="Vertex group or attribute (0-1) that masks where the mesh emits. Empty emits from the whole mesh",
        default="",
    )

    mesh_velocity_attribute: StringProperty(
        name="Velocity Attribute",
        description="Usually empty: velocity is measured from the mesh's motion. Name a vector attribute to use its own velocity instead",
        default="",
    )

    mesh_emission_mask_threshold: FloatProperty(
        name="Mask Threshold",
        description="Skip triangles whose average emission weight is below this value",
        default=0.001,
        min=0.0,
        max=1.0,
    )

    outflow_coupling: FloatProperty(
        name="Outflow Strength",
        description="How strongly this object removes smoke, temperature, fuel, and burn",
        default=25.0,
        min=0.0,
        soft_max=100.0,
    )

    effector_type: EnumProperty(
        name="Effector Type",
        description="Type of velocity force applied by this effector",
        items=[
            ("force", "Force", "Spherical force away from or toward the object origin"),
            ("wind", "Wind", "Directional force along the object's local Z axis"),
            (
                "vortex",
                "Vortex",
                "Axial vortex with optional inward pull and updraft along local Z",
            ),
            ("turbulence", "Turbulence", "Deterministic noisy force inside the radius"),
            ("drag", "Drag", "Damp velocity toward zero inside the radius"),
        ],
        default="force",
    )

    effector_strength: FloatProperty(
        name="Strength",
        description="Velocity strength of this effector; negative values reverse direction",
        default=10.0,
        soft_min=-100.0,
        soft_max=100.0,
    )

    effector_radius: FloatProperty(
        name="Influence Radius",
        description="Size of the region affected by this effector, not simulation detail or resolution. Increase it to affect a wider area",
        default=4.0,
        min=1e-12,
        soft_max=20.0,
        subtype="DISTANCE",
        soft_min=0.01,
    )

    effector_vortex_height: FloatProperty(
        name="Height",
        description="Total height of the cylindrical vortex field along local Z",
        default=20.0,
        min=1e-12,
        soft_max=100.0,
        subtype="DISTANCE",
        soft_min=0.01,
    )

    effector_vortex_core_radius: FloatProperty(
        name="Core Radius",
        description="Radius of peak rotation; velocity rises inside the core and falls outside it",
        default=1.0,
        min=1e-12,
        soft_max=10.0,
        subtype="DISTANCE",
        soft_min=0.01,
    )

    effector_vortex_inflow: FloatProperty(
        name="Inflow",
        description="Inward velocity that confines smoke around the rotating core",
        default=0.0,
        min=0.0,
        soft_max=100.0,
    )

    effector_vortex_lift: FloatProperty(
        name="Updraft",
        description="Velocity along local +Z; negative values pull toward local -Z",
        default=0.0,
        soft_min=-100.0,
        soft_max=100.0,
    )

    effector_falloff_power: FloatProperty(
        name="Power",
        description="Higher values concentrate the force near the effector origin",
        default=2.0,
        min=0.0,
        soft_max=8.0,
    )

    effector_z_direction: EnumProperty(
        name="Z Direction",
        description="Limit force samples by local Z direction",
        items=[
            ("both", "Both Z", "Affect both sides of local Z"),
            ("positive", "+Z", "Affect only positive local Z"),
            ("negative", "-Z", "Affect only negative local Z"),
        ],
        default="both",
    )

    effector_use_min_distance: BoolProperty(
        name="Use Min Distance",
        description="Keep full strength inside the minimum distance",
        default=False,
    )

    effector_min_distance: FloatProperty(
        name="Min Distance",
        description="Distance inside which force strength stays constant",
        default=0.0,
        min=0.0,
        soft_max=20.0,
        subtype="DISTANCE",
    )

    effector_use_max_distance: BoolProperty(
        name="Use Max Distance",
        description="Stop force influence at a custom maximum distance",
        default=False,
    )

    effector_max_distance: FloatProperty(
        name="Max Distance",
        description="Maximum force influence distance",
        default=0.0,
        min=0.0,
        soft_max=50.0,
        subtype="DISTANCE",
    )

    effector_noise_amount: FloatProperty(
        name="Noise Amount",
        description="Amount of smooth directional noise mixed into force, wind, or vortex direction",
        default=0.0,
        min=0.0,
        soft_max=10.0,
    )

    effector_noise_size: FloatProperty(
        name="Noise Size",
        description="Approximate local-space feature size of the smooth noise pattern",
        default=1.0,
        min=1e-12,
        soft_max=20.0,
        subtype="DISTANCE",
        soft_min=0.001,
    )

    effector_noise_w: FloatProperty(
        name="W",
        description="Fourth-dimensional noise coordinate; animate this value to art-direct the noise evolution",
        default=0.0,
        soft_min=-10.0,
        soft_max=10.0,
    )

    effector_noise_seed: IntProperty(
        name="Seed",
        description="Seed used for effector noise",
        default=102,
        soft_min=0,
        soft_max=1000000,
    )

    effector_coupling: FloatProperty(
        name="Coupling",
        description="How quickly the smoke takes on the effector's velocity: higher values make the force act harder and sooner",
        default=8.0,
        min=0.0,
        soft_max=500.0,
    )

    effector_mask_field: EnumProperty(
        name="Mask By",
        description="Scale this force by a simulated field, so it only acts where the smoke meets a condition",
        items=(
            ("none", "None", "Apply the force everywhere inside its falloff"),
            ("smoke", "Smoke", "Smoke density at each point"),
            ("temperature", "Temperature", "Simulation temperature from 0 to 1, as used by Ignition Temperature"),
            ("fuel", "Fuel", "Unburned fuel at each point"),
            ("burn", "Burn", "Active combustion at each point"),
            ("speed", "Speed", "Smoke speed in meters per second"),
        ),
        default="none",
    )

    effector_mask_min: FloatProperty(
        name="Start",
        description="Field value where the force begins to act",
        default=0.0,
        soft_min=0.0,
        soft_max=5.0,
    )

    effector_mask_max: FloatProperty(
        name="Full",
        description="Field value where the force reaches full strength",
        default=1.0,
        soft_min=0.0,
        soft_max=5.0,
    )

    effector_mask_invert: BoolProperty(
        name="Invert",
        description="Act at full strength below Start and fade out toward Full, for example to confine a force to thin smoke",
        default=False,
    )

    effector_samples: IntProperty(
        name="Samples",
        description="Texture samples per axis for the field. Memory grows with the cube of this",
        default=8,
        min=8,
        max=256,
    )

    # Advanced Settings
    gravity: FloatVectorProperty(
        name="Gravity",
        description="Direction and strength of buoyancy: hot gas rises against it, heavy smoke sinks along it",
        default=(0.0, 0.0, -9.81),
        size=3,
        subtype="ACCELERATION",
    )

    buoyancy_per_temp: FloatProperty(
        name="Temperature Buoyancy",
        description="How strongly hot gas rises (against Gravity), per unit of temperature",
        default=2.0,
        min=0.0,
        soft_max=20.0,
    )

    physical_buoyancy: BoolProperty(
        name="Physical Buoyancy",
        description="Lift hot gas as real air would, from Flame Temperature, instead of Temperature Buoyancy",
        default=False,
    )

    buoyancy_per_smoke: FloatProperty(
        name="Smoke Buoyancy",
        description="Weight of smoke: negative sinks and spreads along the ground, positive rises like a light gas",
        default=0.0,
        soft_min=-20.0,
        soft_max=20.0,
    )



    flame_temperature: FloatProperty(
        name="Flame Temperature",
        # Shown as "2000 K" (Celsius in a scene set to it; stored in Kelvin).
        unit="TEMPERATURE",
        description="Hottest temperature the gas reaches, in Kelvin. Sets flame colour: ~1500 K orange, ~2000 K yellow-orange, 3000 K+ white",
        default=2000.0,
        min=1.0,
        # Temperature is stored as float16 in units of 5000 K.
        max=327520000.0,
        soft_min=1000.0,
        soft_max=3500.0,
    )

    gas_expansion: FloatProperty(
        name="Gas Expansion",
        description="How much burning gas expands and pushes smoke outward. Try 0.3-1",
        default=0.3,
        min=0.0,
        soft_max=1.0,
    )

    fuel_energy: FloatProperty(
        name="Fuel Energy",
        description="Heat released per unit of fuel. Raise for hotter lean fuel, lower for dimmer, smouldering fire",
        default=1.0,
        min=0.0,
        soft_max=4.0,
    )

    oxygen_limit: FloatProperty(
        name="Oxygen Limit",
        description="Burning needs fresh air, so fire forms shells where it mixes instead of a uniformly hot ball. 0 off; try 1-4",
        default=1.0,
        min=0.0,
        soft_max=8.0,
    )

    # 0.1 smooths fire speckle (Axiom's default is the same), 2026-11-07.
    temperature_diffusion: FloatProperty(
        name="Temperature Diffusion",
        description="Evens out heat between neighbouring voxels, smoothing speckled fire. 0 off; try 0.1-0.3",
        default=0.1,
        min=0.0,
        max=1.0,
        subtype="FACTOR",
    )

    oxygen_field: BoolProperty(
        name="Oxygen Field",
        description="Track oxygen so fire starves in burnt gas and burns where it meets fresh air. Needs Oxygen Limit above 0",
        default=False,
    )

    oxygen_use: FloatProperty(
        name="Oxygen Use",
        description="How much oxygen burning uses up: higher leaves smokier, starved pockets",
        default=1.0, min=0.0, soft_max=4.0,
    )

    smoke_detail: EnumProperty(
        name="Smoke Detail",
        description="How smoke and heat are carried between cells each step",
        items=[
            ("standard", "Standard", "Linear interpolation: fastest, softest fine detail"),
            ("sharp", "Sharp", "Cubic interpolation: keeps billows about twice as fine, so lobes nest down to a few voxels instead of fading out near eight; about 1.25x the simulation time. Pair with Edge Sharpness about 0.45 to avoid stair-stepped edges in close-ups"),
        ],
        default="standard",
    )

    edge_sharpness: FloatProperty(
        name="Edge Sharpness",
        description="How strongly each step restores sharp detail. High values can stair-step in close-ups",
        default=0.5,
        min=0.0,
        max=1.0,
    )

    oxygen_mixing: FloatProperty(
        name="Oxygen Mixing",
        description="How fast fresh air mixes back into burnt gas, per second. Lower keeps smoky cores starved longer",
        default=0.5, min=0.0, soft_max=4.0,
    )

    soot_radiation: FloatProperty(
        name="Soot Radiation",
        description="Sooty gas radiates heat away, so dense pockets darken while thin hot gas stays bright. 0 off; try 0.5-1",
        default=1.0,
        min=0.0,
        soft_max=3.0,
    )

    wisp_cooling: FloatProperty(
        name="Wisp Cooling",
        description="Thin gas cools faster, so wisps and edges darken around glowing cores. 0 off; try 2-6",
        default=0.0,
        min=0.0,
        soft_max=10.0,
    )

    fuel_threshold: FloatProperty(
        name="Flame Sheets",
        description="Caps how fast dense fuel burns, so rich cores smoulder while mixing edges burn in glowing sheets. 0 off",
        default=0.0,
        min=0.0,
        soft_max=10.0,
    )

    burn_time: FloatProperty(
        name="Burn Time",
        description="Seconds for burning gas to use up its fuel: how long fire lasts",
        default=2.0,
        min=0.05,
        soft_max=10.0,
        subtype="TIME_ABSOLUTE",
    )

    cooling_time: FloatProperty(
        name="Cooling Time",
        description="Seconds for hot gas to cool once it stops burning",
        default=2.0,
        min=0.05,
        soft_max=10.0,
        subtype="TIME_ABSOLUTE",
    )

    ignition_temperature_k: FloatProperty(
        name="Ignition Temperature",
        unit="TEMPERATURE",
        description="Fuel burns only in gas at least this hot, in Kelvin; fuel carried into cooler air stops burning",
        default=500.0,
        min=0.0,
        soft_max=1500.0,
    )


    # 1 (2026-11-08): at 3 a dense dark cloud buried the fire within a second.
    smoke_per_burn: FloatProperty(
        name="Smoke Amount",
        description="How much smoke burning fuel produces; increase for smokier fire or lower for cleaner flames",
        default=1.0,
        min=0.0,
        soft_max=25.0,
    )


    wind: FloatVectorProperty(
        name="Wind",
        description="Steady wind that carries all air and smoke downwind. Free; use Wind effectors for gusts",
        default=(0.0, 0.0, 0.0),
        size=3,
        subtype="VELOCITY",
        unit="VELOCITY",
    )

    vorticity: FloatProperty(
        name="Vorticity",
        description="Boosts small curls and swirls for more turbulent motion",
        default=2.0,
        min=0.0,
        soft_max=10.0,
    )













    simulation_scale: FloatProperty(
        name="Simulation Scale",
        description="Physical size the scene represents: 2 behaves twice as large, 0.5 half. Changes buoyancy, not voxel size",
        default=1.0,
        min=1e-12,
        soft_min=0.1,
        soft_max=10.0,
    )

    trace_steps: IntProperty(
        name="Trace Steps",
        description="How many sub-steps each step's flow is traced in, so fast curved motion stays accurate. 0: one straight step",
        default=16,
        min=0,
        soft_max=16,
    )

    collider_face_flux: BoolProperty(
        name="Collider Face Flux",
        description="Colliders pass their full velocity to the air, so moving colliders push and drag gas more",
        default=False,
    )

    # Advanced solver and collider options.
    projection_filter: FloatProperty(
        name="Projection Filter",
        description="How much the pressure solve smooths fine motion. Lower lets fire flicker and puff, at the risk of noise",
        default=1.0, min=0.0, max=1.0, subtype="FACTOR",
    )

    open_collider_wall: FloatProperty(
        name="Open Collider Wall",
        description="Thickness of the wall behind an open collider mesh (a plane), in velocity cells, so smoke can't leak through",
        default=1.0, min=0.0,
        soft_max=8.0,
    )

    closed_collider_margin: FloatProperty(
        name="Collider Still Air",
        description="How far, in velocity cells, a resting collider holds the air still. Low lets smoke rest on walls",
        default=0.25, min=0.0,
        soft_max=8.0,
    )

    advection_reflection: BoolProperty(
        name="Preserve Turbulence",
        description="Keeps swirling motion longer for livelier turbulence. About 3% more GPU time; fills VRAM sooner",
        default=False,
    )


    vorticity_mask: EnumProperty(
        name="Vorticity Mask",
        description="Where Vorticity adds swirls",
        items=(
            ("speed", "Fast Motion", "Strongest where the flow is fastest"),
            ("smoke", "Smoke", "In proportion to smoke density, so thin edges stay calm"),
            ("fire", "Flames", "Where the gas is hot, for licking flames over a calmer plume"),
            ("everywhere", "Everywhere", "Evenly through the simulated air"),
        ),
        default="speed",
    )

    # 0.2 with Smoke Decay 0.1 fades smoke over several seconds; 0.5 / 0.3 ended
    # a rising column about 13 m up (2026-11-06).
    dissipation: FloatProperty(
        name="Dissipation",
        description="How fast smoke fades, in density per second, the same for thin and dense smoke",
        default=0.2,
        min=0.0,
        soft_max=1.0,
    )

    velocity_damping: FloatProperty(
        name="Velocity Damping",
        description="Share of motion removed per second. Raise to calm turbulence",
        default=0.01,
        min=0.0,
        max=1.0,
        soft_max=0.99,
        subtype="FACTOR",
    )

    smoke_decay: FloatProperty(
        name="Smoke Decay",
        description="Share of the smoke removed per second, in proportion to its density. Ends at 0.99",
        default=0.1,
        min=0.0,
        max=0.99,
        subtype="FACTOR",
    )

    # VDB export controls. Resolution: grids can be written at half or a
    # quarter of the simulation's (2x2x2 or 4x4x4 averages) for smaller files.
    export_density_vdb: BoolProperty(
        name="Export Density VDB",
        description="Write the smoke density grid. The generated material always includes it",
        default=True,
    )
    export_density_resolution: EnumProperty(
        name="Density Resolution",
        description="Resolution density is written at: Half is about 8 times smaller and visibly softer",
        items=_EXPORT_RESOLUTION_ITEMS,
        default="FULL",
    )
    export_temperature_resolution: EnumProperty(
        name="Temperature Resolution",
        description="Resolution temperature is written at: Half gives about a third smaller files with fire very slightly softer",
        items=_EXPORT_RESOLUTION_ITEMS,
        default="FULL",
    )
    export_fuel_resolution: EnumProperty(
        name="Fuel Resolution",
        description="Resolution fuel is written at",
        items=_EXPORT_RESOLUTION_ITEMS,
        default="FULL",
    )
    export_burn_resolution: EnumProperty(
        name="Burn Resolution",
        description="Resolution burn is written at",
        items=_EXPORT_RESOLUTION_ITEMS,
        default="FULL",
    )
    export_velocity_resolution: EnumProperty(
        name="Velocity Resolution",
        description="Resolution velocity is written at, relative to density",
        items=_velocity_resolution_items,
        default=1,
    )

    export_glow_resolution: EnumProperty(
        name="Glow Resolution",
        description="Detail of the written glow grids. Half and Quarter are smaller files, slightly softer glow",
        items=(
            ("FULL", "Full", "The glow's usual detail", 0),
            ("HALF", "Half", "About 8 times fewer glow voxels", 1),
            ("QUARTER", "Quarter", "About 64 times fewer glow voxels", 2),
        ),
        default="FULL",
    )

    export_glow_vdb: BoolProperty(
        name="Export Glow VDB",
        description="Write the glow grid used by the material's Scatter Glow (light from nearby fire in cooler smoke). Needs temperature",
        default=True,
    )

    export_velocity_vdb: BoolProperty(
        name="Export Velocity VDB",
        description="Export velocity for motion blur and downstream volume effects",
        default=False,
    )

    export_temperature_vdb: BoolProperty(
        name="Export Temperature VDB",
        description="Write temperature for the material's fire. Off: smoke only",
        default=True,
    )

    glow_radius: FloatProperty(
        name="Glow Radius",
        description="How far fire light spreads into the smoke for Scatter Glow. 0 follows the fire's size",
        default=0.0,
        min=0.0,
        soft_max=10.0,
        subtype="DISTANCE",
    )

    glow_source: EnumProperty(
        name="Glow Source",
        description="What lights the smoke in the baked glow grid (Scatter Glow). Changing it needs a re-bake",
        items=(
            ("AUTO", "Auto", "Warm Gas for the Pyro look, Fire Brightness otherwise"),
            ("WIEN", "Fire Brightness", "Light from each voxel's fire brightness (density x Wien's law): the hot core lights the smoke"),
            ("HEAT", "Warm Gas",
             "Light from all warm gas, ramping in from Glow Heat From to To and level above (Houdini pyro's scatter "
             "source): warm caps glow along with the core"),
        ),
        default="AUTO",
    )

    glow_heat_from: FloatProperty(
        name="Glow Heat From",
        description="Warm Gas glow source: gas starts to light the smoke at this temperature (K)",
        default=1500.0,
        min=300.0,
        soft_max=3000.0,
    )

    glow_heat_to: FloatProperty(
        name="Glow Heat To",
        description="Warm Gas glow source: gas lights the smoke fully at and above this temperature (K)",
        default=2000.0,
        min=301.0,
        soft_max=4000.0,
    )

    export_fuel_vdb: BoolProperty(
        name="Export Fuel VDB",
        description="Write the fuel grid (unburnt fuel), for custom shading. Blue Core adds it automatically",
        default=False,
    )

    export_burn_vdb: BoolProperty(
        name="Export Burn VDB",
        description="Write how much fuel burns per voxel, for custom shading",
        default=False,
    )

    flame_temperature_min: FloatProperty(
        name="Start Temperature",
        unit="TEMPERATURE",
        description="Temperature (K) where generated fire emission begins",
        default=800.0,
        min=0.0,
        soft_max=5000.0,
        update=_appearance_updated,
    )

    flame_temperature_max: FloatProperty(
        name="Full Temperature",
        unit="TEMPERATURE",
        description="Temperature (K) where generated fire emission reaches full strength",
        default=3000.0,
        min=1.0,
        soft_max=5000.0,
        update=_appearance_updated,
    )

    shader_smoke_density: FloatProperty(
        name="Density",
        description="Density multiplier used by the generated volume material and live volume preview",
        default=1.0,
        min=0.0,
        soft_max=10.0,
        update=_appearance_updated,
    )

    shader_smoke_color: FloatVectorProperty(
        name="Color",
        description="Smoke color used by the generated volume material and live volume preview",
        default=(0.45, 0.45, 0.45),
        min=0.0,
        max=1.0,
        size=3,
        subtype="COLOR",
        update=_appearance_updated,
    )

    shader_anisotropy: FloatProperty(
        name="Anisotropy",
        description="Light scattering direction: positive forward (backlit edges glow), negative back, 0 even",
        default=0.3,
        min=-0.999999,
        max=0.999999,
        update=_appearance_updated,
        soft_min=-0.95,
        soft_max=0.95,
    )

    # Hot Smoke Density (render only): the material's Look Controls; the live
    # preview does not show it.
    shader_smooth_voxels: BoolProperty(
        name="Smooth Voxels",
        description="Render the volume with cubic interpolation to avoid ring artifacts. About 1.5x render time",
        default=True,
        update=_appearance_updated,
    )

    shader_hot_soot: FloatProperty(
        name="Hot Soot",
        description="Pyro look: how dark hot soot turns (0 keeps hot smoke the Smoke Color, 1 is full dark soot)",
        default=1.0,
        min=0.0,
        max=1.0,
        subtype="FACTOR",
        update=_appearance_updated,
    )

    shader_hot_density: FloatProperty(
        name="Hot Smoke Density",
        description="How much denser hot gas looks than cool smoke, for thicker fire. 1 off. Render only",
        default=1.0,
        min=0.0,
        soft_max=20.0,
        update=_appearance_updated,
    )
    shader_hot_density_from: FloatProperty(
        name="Hot Smoke From",
        unit="TEMPERATURE",
        description="Temperature (K) where smoke starts to thicken (Hot Smoke Density)",
        default=500.0,
        min=0.0,
        soft_max=5000.0,
        update=_appearance_updated,
    )
    shader_hot_density_to: FloatProperty(
        name="Hot Smoke To",
        unit="TEMPERATURE",
        description="Temperature (K) where smoke is Hot Smoke Density times as dense",
        default=1500.0,
        min=0.0,
        soft_max=5000.0,
        update=_appearance_updated,
    )

    shader_flame_enabled: BoolProperty(
        name="Flame",
        description="Show burn-driven fire in the generated material and live volume preview",
        default=True,
        update=_appearance_updated,
    )

    shader_flame_brightness: FloatProperty(
        name="Brightness",
        description="Emission strength multiplier for generated fire",
        default=2.0,
        min=0.0,
        soft_max=100.0,
        update=_appearance_updated,
    )

    shader_glow_temperature: FloatProperty(
        name="Full Glow",
        unit="TEMPERATURE",
        description="Temperature (K) at which fire glows at full Brightness; hotter gas is brighter, cooler gas falls off by Contrast",
        default=2000.0,
        min=300.0,
        soft_min=800.0,
        soft_max=3000.0,
        update=_appearance_updated,
    )

    shader_flame_contrast: FloatProperty(
        name="Contrast",
        description="How fast glow fades as gas cools: 1 thin, sparkly flames; lower for wider, fuller flames",
        default=0.5,
        min=0.05,
        soft_max=2.0,
        update=_appearance_updated,
    )

    shader_scatter_glow: FloatProperty(
        name="Scatter Glow",
        description="Fire light scattered into nearby smoke, so dark billows glow from inside near flames",
        default=1.0,
        min=0.0,
        soft_max=4.0,
        update=_appearance_updated,
    )

    shader_glow_spread: FloatProperty(
        name="Glow Spread",
        description="How far Scatter Glow reaches into the smoke: higher gives a longer, softer glow",
        default=0.0,
        min=0.0,
        max=1.0,
        subtype="FACTOR",
        update=_appearance_updated,
    )

    shader_flame_tint: FloatVectorProperty(
        name="Flame Tint",
        description="Tint for the flame colour (e.g. green copper, blue gas). White keeps the physical colour",
        default=(1.0, 1.0, 1.0),
        size=3,
        min=0.0,
        soft_max=1.0,
        subtype="COLOR",
        update=_appearance_updated,
    )

    # 0.5 keeps sooty caps darker (user, 2026-11-08); 1 hid Scatter Glow in
    # dense smoke (2-5% left in a few density-metres).
    shader_glow_shadowing: FloatProperty(
        name="Glow Shadowing",
        description="How much smoke between a spot and the fire blocks Scatter Glow: higher keeps sooty caps dark",
        default=0.5,
        min=0.0,
        soft_max=10.0,
        update=_appearance_updated,
    )

    shader_flame_sharpness: FloatProperty(
        name="Sharpness",
        description="How sharply flames fade at their edges: higher gives crisp tongues",
        default=2.0,
        min=0.25,
        soft_max=4.0,
        update=_appearance_updated,
    )

    shader_look: EnumProperty(
        name="Look",
        description="How fire is shaded in the generated material",
        items=(
            ("TEMPERATURE", "Temperature", "Gas glows by its own temperature and density (blackbody), with Scatter Glow from nearby fire"),
            ("PYRO", "Pyro", "Houdini Pyro-style: golden fire glowing in the gaps between dark soot billows; gas thinner than Thin Gas glows, denser smoke shows as soot. Same Brightness, Full Glow and Contrast as Temperature. Needs a bake from Fumaris 2.0 (the heat grid)"),
        ),
        default="TEMPERATURE",
        update=_appearance_updated,
    )

    shader_color: EnumProperty(
        name="Color",
        description="Temperature look: how fire is coloured",
        items=(
            ("GOLDEN", "Ramp", "Colour from a ramp you edit, over 0.45-1 x Full Glow (as Houdini's Pyro fire colour ramp; starts deep red to pale gold). Color Spread does not apply; brightness still follows the temperature", 0),
            ("BLACKBODY", "Blackbody", "Real blackbody colour of the gas temperature (Blackbody Scale and Color Spread shift it)", 1),
        ),
        default="BLACKBODY",
        update=_appearance_updated,
    )

    # Pyro look: its own flame controls, separate from the Temperature look's.
    pyro_color: EnumProperty(
        name="Color",
        description="Pyro look: how fire is coloured",
        items=(
            ("GOLDEN", "Ramp", "Colour from a ramp you edit, over 0.45-1 x Full Glow (as Houdini's Pyro fire colour ramp; starts deep red to pale gold). Brightness still follows the heat", 0),
            ("BLACKBODY", "Blackbody", "Real blackbody colour of the gas temperature, as the Temperature look (Blackbody Scale shifts it)", 1),
        ),
        default="BLACKBODY",
        update=_appearance_updated,
    )
    pyro_brightness: FloatProperty(
        name="Brightness",
        description="Pyro look: emission strength of the fire",
        default=1.5,
        min=0.0,
        soft_max=100.0,
        update=_appearance_updated,
    )
    pyro_glow_temperature: FloatProperty(
        name="Full Glow",
        unit="TEMPERATURE",
        description="Pyro look: temperature at which fire glows at full Brightness",
        default=1700.0,
        min=300.0,
        soft_min=800.0,
        soft_max=3000.0,
        update=_appearance_updated,
    )
    pyro_contrast: FloatProperty(
        name="Contrast",
        description="Pyro look: how fast glow falls off as gas cools below Full Glow. Lower values let cooler gas glow too (fuller fire)",
        default=0.5,
        min=0.05,
        soft_max=2.0,
        update=_appearance_updated,
    )
    pyro_temperature_multiplier: FloatProperty(
        name="Blackbody Scale",
        description="Pyro look: scales temperature before the colour ramp. Above 1 paler gold, below 1 deeper red",
        default=1.0,
        min=0.01,
        soft_max=2.0,
        update=_appearance_updated,
    )
    pyro_flame_core: FloatProperty(
        name="Blue Core (Render Only)",
        description="Blue emission where fuel is still burning near the source. Needs fuel; not shown in the preview",
        default=0.0,
        min=0.0,
        max=1.0,
        subtype="FACTOR",
        update=_appearance_updated,
    )
    pyro_flame_tint: FloatVectorProperty(
        name="Flame Tint",
        description="Pyro look: multiplies the fire colour, for stylised or chemical flames. White keeps the golden ramp",
        default=(1.0, 1.0, 1.0),
        size=3,
        min=0.0,
        soft_max=1.0,
        subtype="COLOR",
        update=_appearance_updated,
    )
    pyro_scatter_glow: FloatProperty(
        name="Scatter Glow",
        description="Pyro look: fire light scattered into the surrounding smoke (the baked glow grid, see Glow Radius under Output)",
        default=1.0,
        min=0.0,
        soft_max=4.0,
        update=_appearance_updated,
    )
    pyro_glow_spread: FloatProperty(
        name="Glow Spread",
        description="Pyro look: how far Scatter Glow reaches into the smoke; higher values blend in light spread four times farther",
        default=0.0,
        min=0.0,
        max=1.0,
        subtype="FACTOR",
        update=_appearance_updated,
    )
    pyro_glow_shadowing: FloatProperty(
        name="Glow Shadowing",
        description="Pyro look: how much smoke between a spot and the fire blocks Scatter Glow",
        default=0.5,
        min=0.0,
        soft_max=10.0,
        update=_appearance_updated,
    )

    shader_thin_gas_auto: BoolProperty(
        name="Auto",
        description="Set Thin Gas from each frame's smoke, so the glow looks the same at any smoke amount",
        default=True,
        update=_appearance_updated,
    )

    shader_thin_gas_ratio: FloatProperty(
        name="Thin Gas",
        description="Thin Gas as a share of the frame's typical smoke density: lower glows only the thinnest skins",
        default=0.12,
        min=0.005,
        soft_max=0.5,
        update=_appearance_updated,
    )

    shader_thin_gas: FloatProperty(
        name="Thin Gas",
        description="Smoke density below which fire glows; denser smoke turns to dark soot over the fire",
        default=3.0,
        min=0.001,
        soft_max=20.0,
        update=_appearance_updated,
    )

    # Off (2026-11-08): with Brightness raised it turned fuel-rich scenes lilac.
    shader_flame_core: FloatProperty(
        name="Blue Core (Render Only)",
        description="Blue emission where fuel is still burning near the source. Needs fuel; not shown in the preview",
        default=0.0,
        min=0.0,
        max=1.0,
        subtype="FACTOR",
        update=_appearance_updated,
    )

    shader_color_spread: FloatProperty(
        name="Color Spread",
        description="How far fire colour shifts with temperature: 1 physical blackbody, lower keeps colours closer together",
        default=1.0,
        min=0.05,
        soft_max=2.0,
        update=_appearance_updated,
    )

    shader_temperature_multiplier: FloatProperty(
        name="Blackbody Scale",
        description="Scales temperature before the blackbody colour. Above 1 yellower, below 1 redder",
        default=1.0,
        min=0.01,
        soft_max=2.0,
        update=_appearance_updated,
    )


    # Modifiers stack (detail_stack.py; "Detail & Shaping" before particles
    # joined it): replaces Turbulence Detail, Control Ramps and Soot Burn-off,
    # whose properties remain only as inputs, and the Particles v1 section.
    detail_layers: CollectionProperty(type=FumarisDetailLayer)
    detail_layers_index: IntProperty(name="Active Layer", default=0)
    show_domain_detail_stack: BoolProperty(name="Modifiers", default=False)



    adaptive_cfl: FloatProperty(
        name="Cells per Step",
        description="Voxels the fastest smoke may cross per step before a Sub-Step is added. Lower is more accurate and slower. 0 off",
        default=8.0,
        min=0.0,
        soft_max=8.0,
    )




    # 1 Sub-Step (user, 2026-11-08) with Trace Steps 16: about 35% faster than
    # 2 steps and close to a converged result (output/trace_final).
    num_sub_steps: IntProperty(
        name="Sub-Steps",
        description="Smaller steps per frame for fast motion. With Cells per Step on, the most allowed",
        default=1,
        min=1,
        soft_max=40,
    )


    # Simulation State
    simulation_state: EnumProperty(
        name="Simulation State",
        description="Current state of the simulation",
        items=[
            ("idle", "Idle", "No simulation running"),
            ("baking", "Baking", "Simulation is baking"),
            ("baked", "Baked", "Simulation has been baked"),
            ("stopped", "Stopped", "Simulation was stopped"),
        ],
        default="idle",
    )

    baked_frames: IntProperty(
        name="Baked Frames",
        description="Number of frames that have been baked",
        default=0,
        min=0,
    )

    bake_elapsed: FloatProperty(
        name="Bake Elapsed",
        description="Elapsed bake time in seconds",
        default=0.0,
        options={"HIDDEN"},
    )

    bake_peak_vram_mb: FloatProperty(
        name="Bake Peak VRAM",
        description="Peak Flow device memory used during the last bake in MiB",
        default=0.0,
        options={"HIDDEN"},
    )

    bake_peak_ram_mb: FloatProperty(
        name="Bake Peak RAM",
        description="Peak combined Blender and bridge working set during the last bake in MiB",
        default=0.0,
        options={"HIDDEN"},
    )

    # UI-only organization flags. These do not affect the simulation.
    show_panel_advanced: BoolProperty(
        name="Advanced",
        default=False,
    )

    # Top-level panel foldouts (ui._section): open by default, so Play, Bake and
    # each participant's main settings show on a new object; sub-sections fold.
    show_panel_compare: BoolProperty(name="Versions", default=False)
    show_panel_simulate: BoolProperty(name="Simulate", default=True)
    show_panel_participants: BoolProperty(name="Participants", default=True)
    show_panel_behavior: BoolProperty(name="Smoke Behavior", default=True)
    show_panel_emitter: BoolProperty(name="Emitter", default=True)
    show_panel_emitted_channels: BoolProperty(name="Emitted Channels", default=True)
    show_panel_emitter_velocity: BoolProperty(name="Velocity", default=True)
    show_panel_collider: BoolProperty(name="Collider", default=True)
    show_panel_effector: BoolProperty(name="Effector", default=True)
    show_panel_outflow: BoolProperty(name="Outflow", default=True)

    show_preview_display: BoolProperty(
        name="Preview Settings",
        default=False,
    )

    show_emitter_participants: BoolProperty(name="Emitters", default=False)
    show_collider_participants: BoolProperty(name="Colliders", default=False)
    show_effector_participants: BoolProperty(name="Effectors", default=False)
    show_outflow_participants: BoolProperty(name="Outflows", default=False)

    show_appearance: BoolProperty(
        name="Appearance",
        default=False,
    )

    show_cache_location: BoolProperty(
        name="Output",
        default=False,
    )


    show_panel_noise: BoolProperty(
        name="Noise",
        default=False,
    )

    show_panel_falloff: BoolProperty(
        name="Falloff",
        default=False,
    )

    show_panel_mask: BoolProperty(
        name="Field Mask",
        default=False,
    )

    show_volume_grids: BoolProperty(
        name="Grid Names",
        default=False,
    )

    show_emitter_coupling: BoolProperty(
        name="Emission Blending",
        default=False,
    )

    show_emitter_advanced: BoolProperty(
        name="Advanced Emitter",
        default=False,
    )



    show_domain_combustion: BoolProperty(
        name="Combustion",
        default=False,
    )

    show_combustion_more: BoolProperty(
        name="More Combustion",
        default=False,
    )

    show_advanced_internals: BoolProperty(
        name="Solver Internals",
        default=False,
    )


def register():
    try:
        bpy.utils.unregister_class(FumarisSettings)
    except RuntimeError:
        pass
    from . import detail_stack
    detail_stack.unregister_properties()
    detail_stack.register_properties()
    bpy.utils.register_class(FumarisSettings)
    bpy.types.Object.fumaris = PointerProperty(type=FumarisSettings)
    bpy.types.Scene.fumaris_versions_all = BoolProperty(
        name="All domains",
        description="Show the saved versions of every Fumaris domain in this scene",
        default=False,
    )


def unregister():
    if hasattr(bpy.types.Scene, "fumaris_versions_all"):
        del bpy.types.Scene.fumaris_versions_all
    if hasattr(bpy.types.Object, 'fumaris'):
        del bpy.types.Object.fumaris
    try:
        bpy.utils.unregister_class(FumarisSettings)
    except RuntimeError:
        pass
    from . import detail_stack
    detail_stack.unregister_properties()
