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
        description="Act on the smoke already there instead of emitting: steer it toward this emitter's Velocity, heat it to at least its Temperature, and expand it (negative Expansion pulls it in, for wakes), without adding or removing smoke or fuel",
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
        description="Color of this emitter's smoke, times the domain's smoke Color. Used when the domain's Per-Emitter Smoke Color (Look) is on. It colors the smoke, not the flame",
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
        description="The emitter burns where it sits, like a gas jet or a burning surface, without emitting fuel: it heats gas toward Flame Temperature, adds smoke by the domain's Smoke Amount and expands it by Gas Expansion, all at Burn Rate",
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
        description="How fast the emitter adds volume, per second: 1 grows the emitting region's gas by its own volume each second and pushes smoke outward; negative values pull inward. The same at any resolution and Sub-Steps",
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
        description="How strongly the emitter pushes smoke along its velocity or movement; a stationary emitter does not slow existing smoke. With Add it accelerates the air by velocity × coupling every second (a force); with Blend, Maximum and Replace, how fast the air reaches the velocity",
        default=8.0,
        min=0.0,
        soft_max=200.0,
    )

    couple_rate_divergence: FloatProperty(
        name="Expansion",
        description="Whether and how strongly the emitter applies its Expansion. With Maximum any positive value applies the Expansion exactly; with Blend the rate sets how far each step pulls toward it",
        default=2.0,
        min=0.0,
        soft_max=200.0,
    )

    couple_rate_temperature: FloatProperty(
        name="Temperature",
        description="How quickly the emitter heats the gas in its shape. With Add it is the amount added per second (value × coupling); with Blend, Maximum and Replace, how fast the field reaches the value",
        default=10.0,
        min=0.0,
        soft_max=200.0,
    )

    couple_rate_fuel: FloatProperty(
        name="Fuel",
        description="How quickly the emitter supplies its Fuel. With Add it is the amount added per second (value × coupling); with Blend, Maximum and Replace, how fast the field reaches the value",
        default=2.0,
        min=0.0,
        soft_max=200.0,
    )

    couple_rate_burn: FloatProperty(
        name="Burn",
        description="How quickly the emitter applies its Burn to start combustion. With Add it is the amount added per second (value × coupling); with Blend, Maximum and Replace, how fast the field reaches the value",
        default=2.0,
        min=0.0,
        soft_max=200.0,
    )

    couple_rate_smoke: FloatProperty(
        name="Smoke",
        description="How quickly the emitter fills its shape with Smoke. With Add it is the amount added per second (value × coupling); with Blend, Maximum and Replace, how fast the field reaches the value",
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
        description="How this emitter writes velocity into the simulation, per axis. Add accelerates the air every second (a force); Maximum raises each axis toward the velocity but never lowers it, so it suits upward or positive-axis pushes (a negative axis is left alone); Blend pulls the air toward it; Replace sets it",
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
        description="Air blown out along the mesh normals (negative sucks in). On a closed mesh it is a real source that spreads evenly. An open mesh (such as a plane) cannot add air, so it acts as a fan: air passes through it at about this speed (about 90% within a second). Instances push along averaged vertex normals, which can be uneven across hard edges",
        default=0.0,
        soft_min=-100.0,
        soft_max=100.0,
    )

    motion_velocity_scale: FloatProperty(
        name="Motion Velocity Scale",
        description="Scale of source motion velocity: 1 uses the measured velocity (Blender units per second), 0 ignores it. Geometry Nodes velocity attributes must be in local units per second. Fluid response also depends on Velocity Coupling",
        default=1.0,
        min=0.0,
        soft_max=25.0,
    )

    motion_substeps: IntProperty(
        name="Motion Sub-Steps",
        description="Intermediate source poses sampled between Blender frames; only affects moving or deforming geometry, is capped by domain Sub-Steps, and increases emitter work",
        default=8,
        min=1,
        # Flow stores intermediate emitter transforms in a fixed 32-entry array.
        max=32,
        soft_max=16,
    )

    use_scene_frame_range: BoolProperty(
        name="Use Timeline",
        description="Simulate the scene's timeline range, starting Warmup frames early, instead of a separate Start and End. On for new domains",
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
        description="Voxel size: 32 scene units divided by this (1024 is about 3 cm), whatever the domain object's size; the simulation has no box and covers only where smoke is. Higher values give finer detail but need more GPU memory (about 8 times the voxels at twice the value) and simulation time; raise VRAM if the smoke gets cut off",
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
        description="Distance between preview ray samples, in simulation cells (1 is Cycles' Step Rate 1). Preview cost is about proportional to the number of samples: smaller is smoother and slower, larger is faster and starts to show bands",
        default=1.0, min=0.25, soft_max=2.0,
    )

    preview_shadows: BoolProperty(
        name="Self Shadows",
        description="Smoke shadows itself: from the scene's strongest light, and from the sky (dense smoke is darker inside, as in Cycles). Off lights every voxel fully, which is faster",
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
        description="Show the live preview in every 3D viewport that shows the domain instead of one (the one picked with View > Show Fumaris Preview Here, else the largest in the preview's window). Adds overhead: each extra viewport renders the volume again every frame",
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
        description="Custom lighting: a uniform grey sky around the smoke, like a world colour of this value. The smoke shadows it (with Self Shadows), which gives crevices and undersides their soft ambient darkening. 0 lights the smoke with the custom light only. Preview only",
        default=0.3, min=0.0, soft_max=2.0,
    )

    preview_fire_lighting: FloatProperty(
        name="Fire Lighting",
        description="How strongly flames light the smoke around them; 1 is physically matched to the flame brightness, as Cycles renders it with volume bounces. 0 turns it off. Preview only",
        default=1.0, min=0.0, soft_max=2.0,
    )

    preview_exposure_offset: FloatProperty(
        name="Exposure",
        description="Preview brightness in stops, added to the scene's Exposure (Color Management). 0 matches the render. Preview only",
        default=0.0, max=126.0, soft_min=-4.0, soft_max=4.0,
    )

    preview_tone_mapping: EnumProperty(
        name="View",
        description="View transform of the preview image, from Blender's own color management. Scene follows the scene's View, Look and Display, so the preview matches the render. Preview only",
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
        description="Look applied with the preview's View, as in Blender's Color Management (for example AgX with Punchy for more saturated fire). Preview only: set the scene's own Look to render with it",
        items=_preview_look_items,
    )


    preview_light_direction: FloatVectorProperty(
        name="Light Direction",
        description="World-space direction toward the preview light, with Z pointing up; changes preview shading only, not scene lights or baked simulation",
        default=(0.57735027, 0.57735027, 0.57735027),
        size=3,
        subtype="DIRECTION",
    )

    preview_light_azimuth: FloatProperty(
        name="Azimuth",
        description="Orbit the preview light horizontally around world Z without changing its height. 0 degrees is +X, 90 degrees is +Y. Preview only",
        subtype="ANGLE", soft_min=-pi, soft_max=pi,
        get=_preview_light_azimuth_get, set=_preview_light_azimuth_set,
        options=set(),
    )

    preview_light_elevation: FloatProperty(
        name="Elevation",
        description="Height of the preview light above the horizontal plane. 0 degrees is level, +90 is overhead (+Z), -90 is below. Preview only",
        subtype="ANGLE", min=-pi / 2, max=pi / 2,
        get=_preview_light_elevation_get, set=_preview_light_elevation_set,
        options=set(),
    )


    preview_bake: BoolProperty(
        name="Preview While Baking",
        description="Show the live preview while baking, to watch the bake as it runs. Off keeps the bake as fast as possible. Bake only: Bake in Background and Bake All never show a preview",
        default=True,
    )

    preview_loop: BoolProperty(
        name="Loop",
        description="At the end of the frame range the live preview starts over. Off: it pauses on the last frame (Resume starts over). With Cache Preview, an unchanged, whole cache replays instead of simulating; after a change the range simulates again and replaces the stale frames as it goes (the cache is not cleared)",
        default=True,
    )

    preview_cache_step: EnumProperty(
        name="Cache Every",
        description="How often the live preview keeps a frame. Fewer frames use less RAM and preview time; scrubbing and replay hold each kept frame until the next one",
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
        description="Keep the frames the live preview simulates (compressed, full detail), so a paused preview can scrub and play them back and Save Sequence or Save Frame (below this toggle) can keep them as a version that renders in Cycles and compares in Presets & Compare. With Loop on, a whole cache that nothing has changed since replays instead of simulating. Uses system RAM and some preview speed; how often frames are kept is Cache Every (beside this), the RAM limit is in Preferences > Preview Cache RAM. Applies from the next Play",
        default=False,
    )



    simulation_memory: FloatProperty(
        name="VRAM",
        description="GPU memory reserved for the simulation, in GB. It sets how much space the smoke can fill before it is cut off: more memory allows larger or higher-resolution smoke. The full amount is allocated when the simulation starts and stays constant",
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
        description="Use the GPU memory that is free when the simulation starts, less 6% of the GPU's memory (12% for a bake; at least 1 GB) left for Blender, other programs and Fumaris's emitter and export buffers, instead of a fixed amount (at least 0.5 GB). Close other GPU work first for the most space. Without a free-memory reading from the driver, half the GPU memory is used",
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
        description="Each emitter's smoke takes the Smoke Color set on the emitter (Emitted Channels); where plumes meet, the colors mix. It colors the smoke, not the flame (the flame's color is set under Flame, for the whole domain). Bakes gain a color grid and the material and preview use it. Costs about 1.4x the simulation time and a third fewer blocks for the same VRAM. For one color everywhere, leave this off and set Color",
        default=False,
    )

    # Smoke mass correction (2026-11-14): the semi-Lagrangian scheme gains smoke
    # where gas expands and loses it where it swirls; a per-step global scale
    # (within 5%) keeps the transported total. Dissipation and Decay still apply.
    conserve_smoke: BoolProperty(
        name="Conserve Smoke",
        description="Keep the smoke's total through transport: the solver's advection gains a little smoke where gas expands and loses some where it swirls (up to 10% by the end of an explosion); each step scales the moved smoke back to the amount it started with, within 5%. Dissipation, Decay and combustion still add and remove smoke as set. No visible cost",
        default=True,
    )

    # Full-Resolution Channels' old name, kept for scripts and old files: it
    # reads and sets Full-Resolution Channels. A file saved with Full-Resolution
    # Velocity on still has that value stored under this name; it counts as on
    # until the toggle is next changed.
    full_resolution_velocity: BoolProperty(
        name="Full-Resolution Channels",
        description="Simulate everything at the full Resolution: motion (velocity and pressure), and a fire's fuel and burn, like smoke and temperature. Rounder, finer billows and fewer grid-aligned artifacts from Vorticity, at about 3-4 times the memory per block and 1.5-4 times the time per frame; at the same memory, half-resolution motion at about 1.5 times the Resolution looks as detailed. Smoke also rises faster (up to about 20%: Vorticity makes smaller swirls that slow the plume less). Off, motion runs at half the Resolution and a fire's fuel and burn with it. Either way only the fields the scene uses are stored: smoke alone stays smoke alone",
        get=_full_resolution_get,
        set=_full_resolution_set,
    )


    physics_convex_collision: BoolProperty(
        name="Convex Collision",
        description="Treat closed convex mesh colliders as filled solids using Flow clipping planes; unsuitable meshes fall back to the shell defined by Collider Margin",
        default=True,
    )

    sparse_block_min_lifetime: IntProperty(
        name="Block Minimum Lifetime",
        description="How long to keep a chunk of simulation space after it becomes empty; higher values reduce repeated allocation but use more GPU memory",
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
        description="Distance of clear air simulated around the smoke, measured from the smoke itself, and further along its motion. More room lets large plumes roll into caps; it uses more GPU time and memory",
        default=0.5,
        min=0.0,
        soft_max=4.0,
        subtype="DISTANCE",
    )

    follow_object: BoolProperty(
        name="Move With Object",
        description="Simulate in this domain object's own space, so the simulation travels and turns with it and with whatever it is parented to. Emitters and colliders that ride along stay put in the simulation grid, which keeps fast vehicles smooth and stable with few sub-steps. The object's scale is ignored",
        default=False,
    )

    carry_air: FloatProperty(
        name="Carry Air",
        description="How much of the domain's motion the surrounding air shares. 0 leaves the air still in the scene, so smoke trails behind a moving domain as it would outdoors. 1 carries the air along, as inside a closed vehicle",
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
        description="Simulate everything at the full Resolution: motion (velocity and pressure), and a fire's fuel and burn, like smoke and temperature (about 3-4 times the memory per block). Only the fields the scene uses are stored",
        default=False,
    )

    # Not in the panel: tests force all four density fields even where the
    # scene does not use them.
    density_storage: EnumProperty(
        name="Density Storage",
        description="Which fields the simulation stores. Auto keeps only what the scene uses; All Channels stores temperature, fuel, burn and smoke everywhere",
        items=(
            ("auto", "Auto", "Only the fields the scene uses"),
            ("all", "All Channels", "Always store temperature, fuel, burn and smoke at full resolution"),
        ),
        default="auto",
    )

    cache_slot: StringProperty(
        name="Version",
        description="The current version: where bakes go. Each name keeps its own frames; switching shows that version and hides the others without deleting anything",
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
        description="Show this domain, its live preview and every volume imported from its bakes in the viewport. Off hides them all (renders are not affected); its bakes stay linked and editable",
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
        description="Compression of the written VDB files. Active Mask writes fastest and imports in Blender; ZIP and Blosc make smaller files that take longer to write",
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
        description="How many frames are converted and written to VDB at once while baking. Fewer use less system RAM; use them when a bake would otherwise run out of RAM or swap. Results are the same",
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
        description="World-space padding around mesh and box colliders that participates in the smoke collision solve",
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
        description="Block smoke and remove it from inside the collider. Turn off to keep only the influence of collider motion on nearby velocity",
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
        description="Smoke amount attribute; empty uses smoke/density/mass. Each point's value replaces the emitter's Smoke (it does not scale it); without the attribute the emitter's value is used",
        default="",
    )

    attr_temperature: StringProperty(
        name="Temperature",
        description="Temperature attribute; empty uses temperature/temp/heat. Each point's value replaces the emitter's Temperature (it does not scale it); without the attribute the emitter's value is used",
        default="",
    )

    attr_fuel: StringProperty(
        name="Fuel",
        description="Fuel attribute; empty uses fuel/combustible. Each point's value replaces the emitter's Fuel (it does not scale it); without the attribute the emitter's value is used",
        default="",
    )

    attr_burn: StringProperty(
        name="Burn",
        description="Burn/flame attribute; empty uses burn/flame/fire. Each point's value replaces the emitter's Burn (it does not scale it); without the attribute the emitter's value is used",
        default="",
    )

    attr_smoke_coupling: StringProperty(
        name="Smoke Coupling",
        description="Smoke coupling attribute; empty uses smoke_coupling/density_coupling. Each point's value replaces the emitter's Smoke Coupling (it does not scale it); without the attribute the emitter's value is used",
        default="",
    )

    attr_temperature_coupling: StringProperty(
        name="Temperature Coupling",
        description="Temperature coupling attribute; empty uses temperature_coupling/heat_coupling. Each point's value replaces the emitter's Temperature Coupling (it does not scale it); without the attribute the emitter's value is used",
        default="",
    )

    attr_velocity_coupling: StringProperty(
        name="Velocity Coupling",
        description="Velocity coupling attribute; empty uses velocity_coupling/force_strength/strength. Each point's value replaces the emitter's Velocity Coupling (it does not scale it); without the attribute the emitter's value is used",
        default="",
    )

    attr_divergence: StringProperty(
        name="Divergence",
        description="Expansion attribute; empty uses divergence/expansion/pressure. Each point's value replaces the emitter's Expansion (it does not scale it); without the attribute the emitter's value is used",
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
        description="Treat the trailing number in the file name as a frame and load the file for each simulated frame. Frames without a file emit nothing",
        default=False,
    )

    volume_frame_offset: IntProperty(
        name="Frame Offset",
        description="File frame loaded on scene frame 0; a positive value starts later in the sequence",
        default=0,
    )

    volume_use_velocity: BoolProperty(
        name="Emit Velocity Grid",
        description="Push the simulation with the file's velocity grid at the Velocity coupling rate. Off keeps a stationary volume from affecting motion",
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
        description="Leave empty to emit from the whole mesh. Otherwise use a vertex group or mesh attribute with 0 to 1 weights. Geometry created by Geometry Nodes needs weights on that generated mesh; an original vertex group may not carry over",
        default="",
    )

    mesh_velocity_attribute: StringProperty(
        name="Velocity Attribute",
        description="Usually leave empty: velocity is then measured from how the mesh moves (object animation, armatures, deformation, Geometry Nodes). Type a name only if the mesh carries its own vector attribute, such as one stored by Geometry Nodes or an Alembic cache: object-space units per second, on points, faces or face corners. It replaces the measured motion and is scaled by Motion Velocity Scale. A missing name stops Play with an error (shown below the field). Instanced meshes need Realize Instances",
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
        description="Minimum texture samples per axis. Automatic refinement reaches 64; set up to 256 manually for tighter fields. Memory and processing grow with the cube of this value",
        default=8,
        min=8,
        max=256,
    )

    # Advanced Settings
    gravity: FloatVectorProperty(
        name="Gravity",
        description="Direction and strength of buoyancy: hot gas rises against it, heavy smoke sinks along it (Temperature and Smoke Buoyancy scale how much). It acts only through buoyancy: cold smoke ignores it unless Smoke Buoyancy is negative (heavy smoke). This is the domain's own gravity, not the Scene gravity. Wind is different: it carries all air along at a steady speed without any force",
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
        description="Lift hot gas as much as real air would: gas at Flame Temperature is that many times lighter than 293 K air (about 5.8 g of lift at 2000 K), replacing Temperature Buoyancy. Flames rise faster and puff more strongly",
        default=False,
    )

    buoyancy_per_smoke: FloatProperty(
        name="Smoke Buoyancy",
        description="Force per unit smoke density along Gravity: negative makes smoke heavy so it sinks and spreads along the ground even when cold, positive makes it rise like a light gas. 0 = smoke has no weight; it moves only with the air and heat",
        default=0.0,
        soft_min=-20.0,
        soft_max=20.0,
    )



    flame_temperature: FloatProperty(
        name="Flame Temperature",
        # Shown as "2000 K" (Celsius in a scene set to it; stored in Kelvin).
        unit="TEMPERATURE",
        description="Hottest temperature gas reaches, in Kelvin. Sets the flame colour: about 1500 K deep orange, 2000 K yellow-orange (most fuel fires), 3000 K and above pale yellow-white. Emitter Temperature 1 means this temperature",
        default=2000.0,
        min=1.0,
        # Temperature is stored as float16 in units of 5000 K.
        max=327520000.0,
        soft_min=1000.0,
        soft_max=3500.0,
    )

    gas_expansion: FloatProperty(
        name="Gas Expansion",
        description="Expansion of gas where fuel is burning, per unit of fuel burnt per second: burning gas pushes the smoke around it outward, the same at any resolution. 0 = burning does not expand the gas. Start around 0.3-1 for fuel values of 0.5-2, higher for a stronger push. Expanding gas thins its fuel and smoke as it spreads",
        default=0.3,
        min=0.0,
        soft_max=1.0,
    )

    fuel_energy: FloatProperty(
        name="Fuel Energy",
        description="Heat released by burning, per unit of fuel: 1 takes cold air to Flame Temperature, so fuel 1 burns as hot as fire gets while thinner, mixed fuel burns cooler. Raise it to make lean fuel burn hot; lower it for dimmer, smouldering fire. Burn Time sets how long fuel lasts and Cooling Time how fast heat fades; neither changes how hot it burns",
        default=1.0,
        min=0.0,
        soft_max=4.0,
    )

    oxygen_limit: FloatProperty(
        name="Oxygen Limit",
        description="Burning needs fresh air: hot, already-burnt gas burns slower, so fire concentrates in shells where it mixes with air and temperatures vary instead of the whole fireball sitting at Flame Temperature. Fireballs that burned everywhere at once become smaller and leaner. Gas emitted at Flame Temperature burns only as it mixes. 0 = burns evenly everywhere, air or not; try 1-4 (higher limits less)",
        default=1.0,
        min=0.0,
        soft_max=8.0,
    )

    # 0.1 smooths fire speckle (Axiom's default is the same), 2026-11-07.
    temperature_diffusion: FloatProperty(
        name="Temperature Diffusion",
        description="Heat evens out between neighbouring voxels: each 1/24 s, temperature moves this share of the way to its neighbours' average. Flame brightness rises steeply with temperature, so this smooths speckle and noise in fire and softens flame edges; large shapes keep their look. 0 = off; try 0.1-0.3",
        default=0.1,
        min=0.0,
        max=1.0,
        subtype="FACTOR",
    )

    oxygen_field: BoolProperty(
        name="Oxygen Field",
        description="Track the air's oxygen as it moves: burning uses it up and fresh air mixes back in, so fire starves inside thick burnt gas and burns at the edges where it meets new air. Needs an Oxygen Limit above 0",
        default=False,
    )

    oxygen_use: FloatProperty(
        name="Oxygen Use",
        description="How much oxygen burning uses up. 1 breaks a fireball into burning shells with starved, smoky pockets; 3 buries the core in smoke; lower burns more freely",
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
        description="How strongly each step restores detail lost to interpolation (MacCormack correction of smoke and heat). High values give crisp edges that can stair-step in close-ups when Smoke Detail is Sharp; about 0.45 keeps edges near two voxels soft",
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
        description="Sooty gas radiates its heat away (rate per second at Flame Temperature for Smoke 1, falling with the cube of temperature and capped where soot is optically thick), so dense pockets cool and darken while thin hot gas stays bright. Breaks up flat, evenly hot fireballs. 0 = off; try 0.5-1",
        default=1.0,
        min=0.0,
        soft_max=3.0,
    )

    wisp_cooling: FloatProperty(
        name="Wisp Cooling",
        description="Thin gas is mostly mixed-in air, so it loses its heat faster: gas with less than 1 smoke cools up to 1 + this many times faster than Cooling Time, so wisps and edges go dark around glowing, dense cores. 0 = one cooling rate everywhere; try 2-6",
        default=0.0,
        min=0.0,
        soft_max=10.0,
    )

    fuel_threshold: FloatProperty(
        name="Flame Sheets",
        description="Dense fuel burns at most as fast as this much fuel would, so rich cores smoulder while fuel that has thinned out where it mixes with air burns fully, in sheets: sooty puffs with glowing seams instead of one evenly burning ball. Compare with the emitters' Fuel. 0 = everything burns at its full rate; try 1-2 for an emitter Fuel of 8",
        default=0.0,
        min=0.0,
        soft_max=10.0,
    )

    burn_time: FloatProperty(
        name="Burn Time",
        description="Seconds for burning gas to use up 95% of its fuel: how long fire lasts. It does not change how hot fuel burns (see Fuel Energy); about 1-2 s lets a fireball break into sooty smoke with glowing cracks",
        default=2.0,
        min=0.05,
        soft_max=10.0,
        subtype="TIME_ABSOLUTE",
    )

    cooling_time: FloatProperty(
        name="Cooling Time",
        description="Seconds for hot gas to lose 95% of its heat once it has stopped burning. Longer keeps embers glowing and smoke rising for longer; it does not change how hot burning gas is",
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
        description="Steady wind in world space: all air drifts with it, carrying smoke downwind while sources stay put, so plumes lean and trail. It costs nothing (smoke is traced from upwind, no force is added); use Wind effectors for gusts or local pushes",
        default=(0.0, 0.0, 0.0),
        size=3,
        subtype="VELOCITY",
        unit="VELOCITY",
    )

    vorticity: FloatProperty(
        name="Vorticity",
        description="Boost small curls and swirls in the flow; higher values add turbulent motion, while zero leaves this extra boost off. Below Resolution 512 (or a lower Preview Resolution) it is weakened in proportion, so coarse grids swirl and spread like finer ones",
        default=2.0,
        min=0.0,
        soft_max=10.0,
    )













    simulation_scale: FloatProperty(
        name="Simulation Scale",
        description="Physical size represented by the scene: 2 makes the simulation behave twice as large, 0.5 half as large. Changes buoyancy relative to scene size; voxel size, scene dimensions and source/wind speeds in Blender units stay the same",
        default=1.0,
        min=1e-12,
        soft_min=0.1,
        soft_max=10.0,
    )

    trace_steps: IntProperty(
        name="Trace Steps",
        description="Follow the flow back over each step in sub-steps of at most 1.5 cells, up to this many, so fast curved motion stays accurate with fewer Sub-Steps (fewer interpolations keep more fine detail). 0 = one straight step",
        default=16,
        min=0,
        soft_max=16,
    )

    collider_face_flux: BoolProperty(
        name="Collider Face Flux",
        description="Colliders pass their own velocity to the air at their faces in the pressure solve, instead of half of it averaged with the neighbouring air: moving colliders push and drag the gas more fully",
        default=False,
    )

    # Advanced solver and collider options.
    projection_filter: FloatProperty(
        name="Projection Filter",
        description="How strongly the pressure solve smooths out cell-to-cell (checkerboard) motion. 1 is Flow's. Lower lets fine motion through: fire flickers and puffs (about 0.25 restores natural puffing at moderate resolution) and small swirls survive, at the risk of grid-scale noise",
        default=1.0, min=0.0, max=1.0, subtype="FACTOR",
    )

    open_collider_wall: FloatProperty(
        name="Open Collider Wall",
        description="Minimum thickness, in velocity cells, of the wall behind an open (non-closed) collider mesh such as a plane, so smoke cannot leak through it when Collider Margin is thin (a 0.1 m margin let 2.5% of the smoke through a plane at Resolution 256). 0 uses Collider Margin alone",
        default=1.0, min=0.0,
        soft_max=8.0,
    )

    closed_collider_margin: FloatProperty(
        name="Closed Collider Margin",
        description="Closed colliders (and boxes) hold the air still only this many velocity cells out from their surface instead of the full Collider Margin, so smoke and fire resting on them are not slowed by a layer of stuck air (a fire on a closed floor rose 35-42% slower with the full margin). 0 = the full Collider Margin; try 0.25",
        default=0.0, min=0.0,
        soft_max=8.0,
    )

    advection_reflection: BoolProperty(
        name="Preserve Turbulence",
        description="Advection-reflection: keeps swirling motion that the solver would otherwise lose to numerical damping, for livelier, longer-lasting turbulence and larger plumes. Steps run in pairs, so the Sub-Step count is rounded up to an even number (at least 2). About 3% more GPU time per block; the livelier smoke fills more space, so it fills VRAM sooner",
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
        description="How quickly smoke fades away over time, in smoke density per second; zero keeps it from fading through this setting. It removes the same amount from thin and dense smoke, so smoke spread thin (by Wind or fast spreading) clears sooner; Smoke Decay fades in proportion instead. Type a value above 1 for dense smoke that must clear quickly",
        default=0.2,
        min=0.0,
        soft_max=1.0,
    )

    velocity_damping: FloatProperty(
        name="Velocity Damping",
        description="Fraction of the motion removed per second. The default is close to free air; raise it to calm turbulence and shorten how far smoke travels",
        default=0.01,
        min=0.0,
        max=1.0,
        soft_max=0.99,
        subtype="FACTOR",
    )

    smoke_decay: FloatProperty(
        name="Smoke Decay",
        description="Fraction of the smoke removed per second, in proportion to its density. Flow applies whichever removes more each step, this or Dissipation, so dense smoke ignores Dissipation and fades only at this rate. Raise it when dense leftovers linger after the thin smoke is gone. Flow's built-in value is 0.3",
        default=0.1,
        min=0.0,
        max=1.0,
        soft_max=0.99,
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
        description="Resolution velocity is written at, relative to density. Motion is simulated at half resolution unless Full-Resolution Channels (Advanced) is on, which also offers Full",
        items=_velocity_resolution_items,
        default=1,
    )

    export_glow_resolution: EnumProperty(
        name="Glow Resolution",
        description="Detail the glow grids are written at. They are already a coarse, blurred grid that follows the fire's size; Half and Quarter make it 8 or 64 times smaller with the glow slightly softer",
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
        description="Export temperature in Kelvin (0 to 5000) for Blackbody shading: the generated material's fire. Off, the material shows smoke only. Gas below 500 K, which cannot glow, is not stored and reads as 0 K",
        default=True,
    )

    glow_radius: FloatProperty(
        name="Glow Radius",
        description="How far fire light spreads into the smoke for Scatter Glow, baked into a coarse glow grid with the temperature. 0 follows the size of the fire each frame",
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
        description="Write how much fuel burns in each voxel (it depends on Sub-Steps), for custom shading. The generated material does not need it",
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
        description="How much smoke scatters light forward (positive) or back (negative). Real smoke and soot scatter forward, so a backlit plume glows at its edges; 0 scatters evenly",
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
        description="Cycles reads the volume with cubic instead of linear interpolation. Dense smoke and fireballs "
                    "otherwise show fine concentric rings, one per voxel layer, where their surface crosses the "
                    "voxel grid. Renders about 1.5x slower; turn off for faster test renders",
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
        description="Hot gas is this many times denser than cool smoke (x1 at Hot Smoke From, rising to this at Hot "
                    "Smoke To and above), as Houdini's pyro shading thickens fire. 1 is off. Render only: the live "
                    "preview does not show it",
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
        description="How fast glow falls off as gas cools below Full Glow, which sets how wide flames look. 1 matches a camera exposed for the hottest gas (thin, sparkly flames); lower values let cooler gas glow too (wider, fuller flames) without brightening the hottest gas",
        default=0.5,
        min=0.05,
        soft_max=2.0,
        update=_appearance_updated,
    )

    shader_scatter_glow: FloatProperty(
        name="Scatter Glow",
        description="Fire light scattered into the surrounding smoke, so dark billows glow from inside near flames. Cycles barely carries emission through smoke on its own; this uses the baked glow grid (see Glow Radius under Output)",
        default=1.0,
        min=0.0,
        soft_max=4.0,
        update=_appearance_updated,
    )

    shader_glow_spread: FloatProperty(
        name="Glow Spread",
        description="How far Scatter Glow reaches into the smoke. 0 keeps fire light within Glow Radius of the flames; higher values blend in light spread four times farther, so the falloff gets a long, soft tail and thick smoke glows deep inside; 1 is only the wide, soft glow. Needs a bake from Fumaris 2.0 or later",
        default=0.0,
        min=0.0,
        max=1.0,
        subtype="FACTOR",
        update=_appearance_updated,
    )

    shader_flame_tint: FloatVectorProperty(
        name="Flame Tint",
        description="Multiplies the flame colour, for stylised or chemical flames (green copper, blue gas) on top of the blackbody colour. White keeps the physical colour. Scattered glow keeps the fire light's colour",
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
        description="How much smoke between a spot and the fire blocks Scatter Glow. 0 lights all smoke near fire alike, so dense outer billows glow too; higher values keep sooty caps dark and let fire light show only where little smoke lies in between, in the crevices next to the flames. High values can hide Scatter Glow almost entirely in dense smoke; lower this if Scatter Glow or Glow Spread seem to do nothing. About the smoke Density setting is physically consistent. Needs a bake from Fumaris 2.0 or later",
        default=0.5,
        min=0.0,
        soft_max=10.0,
        update=_appearance_updated,
    )

    shader_flame_sharpness: FloatProperty(
        name="Sharpness",
        description="How abruptly flames fade at their edges: 1 glows softly through the fireball, higher values fade weak glow into the smoke and brighten the strongest flames for crisp tongues",
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
        description="Pyro look: temperature (K) at which fire glows at full Brightness and takes the ramp's palest gold; cooler gas falls off by Contrast toward deep red",
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
        description="Pyro look: multiplies temperature before the golden colour ramp: above 1 shifts fire toward pale gold, below 1 toward deep red. Brightness is not changed",
        default=1.0,
        min=0.01,
        soft_max=2.0,
        update=_appearance_updated,
    )
    pyro_flame_core: FloatProperty(
        name="Blue Core (Render Only)",
        description="Pyro look: blue emission where fuel is still burning near the source, as in fresh flames. Needs fuel in the simulation, and the bake then also writes the fuel grid; the live preview does not show it. Zero turns it off",
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
        description="Pyro look: how much smoke between a spot and the fire blocks Scatter Glow. Lower this if Scatter Glow seems to do nothing in dense smoke",
        default=0.5,
        min=0.0,
        soft_max=10.0,
        update=_appearance_updated,
    )

    shader_thin_gas_auto: BoolProperty(
        name="Auto",
        description="Pyro look: set Thin Gas from each frame's smoke (Thin Gas Ratio x its typical density), so the glowing cells look the same at any Smoke Amount, scene scale or time in the effect. Off: Thin Gas is the density below",
        default=True,
        update=_appearance_updated,
    )

    shader_thin_gas_ratio: FloatProperty(
        name="Thin Gas",
        description="Pyro look, Thin Gas Auto: Thin Gas as a share of the frame's typical (density-weighted mean) smoke density. Lower: only the thinnest skins of each billow glow, darker and more cellular; higher: more of the smoke glows",
        default=0.12,
        min=0.005,
        soft_max=0.5,
        update=_appearance_updated,
    )

    shader_thin_gas: FloatProperty(
        name="Thin Gas",
        description="Pyro look: smoke density at which fire glows as in the Temperature look. Gas thinner than this glows (up to twice as bright); denser smoke fades to dark soot over the fire (none above 1.5 x), so dense billow tops read as soot and the fire shows in the gaps between them. Raise it for dense, smoky fire so more of it glows",
        default=3.0,
        min=0.001,
        soft_max=20.0,
        update=_appearance_updated,
    )

    # Off (2026-11-08): with Brightness raised it turned fuel-rich scenes lilac.
    shader_flame_core: FloatProperty(
        name="Blue Core (Render Only)",
        description="Blue emission where fuel is still burning near the source, as in fresh flames. Needs fuel in the simulation, and the bake then also writes the fuel grid (larger files); the live preview does not show it. Zero turns it off",
        default=0.0,
        min=0.0,
        max=1.0,
        subtype="FACTOR",
        update=_appearance_updated,
    )

    shader_color_spread: FloatProperty(
        name="Color Spread",
        description="How far fire colour moves with temperature around Full Glow. 1 is physical blackbody colour; lower values keep hot and cool gas closer to the Full Glow colour (a camera's compressed fire palette: orange body, no white-hot or deep-red extremes); higher values exaggerate the range",
        default=1.0,
        min=0.05,
        soft_max=2.0,
        update=_appearance_updated,
    )

    shader_temperature_multiplier: FloatProperty(
        name="Blackbody Scale",
        description="Multiplies temperature before the blackbody colour: above 1 shifts fire toward yellow-white, below 1 toward deep red. 1 keeps the simulated temperature",
        default=1.0,
        min=0.01,
        soft_max=2.0,
        update=_appearance_updated,
    )


    # Detail & Shaping stack (detail_stack.py): replaces Turbulence Detail,
    # Control Ramps and Soot Burn-off, whose properties remain only as inputs.
    detail_layers: CollectionProperty(type=FumarisDetailLayer)
    detail_layers_index: IntProperty(name="Active Layer", default=0)
    show_domain_detail_stack: BoolProperty(name="Detail & Shaping", default=False)



    adaptive_cfl: FloatProperty(
        name="Cells per Step",
        description="Adaptive Sub-Steps: how many voxels the fastest smoke may cross per step before another Sub-Step is added, up to Max Sub-Steps. Lower is more accurate and slower; 8 bakes explosions in about half the time with a similar result, while 16 loses much of the smoke. 0 = off: always use Max Sub-Steps",
        default=8.0,
        min=0.0,
        soft_max=8.0,
    )




    # 1 Sub-Step (user, 2026-11-08) with Trace Steps 16: about 35% faster than
    # 2 steps and close to a converged result (output/trace_final).
    num_sub_steps: IntProperty(
        name="Sub-Steps",
        description="How many smaller simulation steps run per frame; increase for fast motion or high-resolution accuracy, at the cost of slower simulation. With Cells per Step above 0 it is the most allowed, and steps are added only where motion needs them",
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
    show_panel_compare: BoolProperty(name="Compare", default=False)
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

    show_participant_list: BoolProperty(
        name="Objects",
        default=False,
    )

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


def unregister():
    if hasattr(bpy.types.Object, 'fumaris'):
        del bpy.types.Object.fumaris
    try:
        bpy.utils.unregister_class(FumarisSettings)
    except RuntimeError:
        pass
    from . import detail_stack
    detail_stack.unregister_properties()
