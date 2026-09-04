import bpy
from bpy.app.handlers import persistent
from bpy.props import (
    BoolProperty,
    EnumProperty,
    FloatProperty,
    FloatVectorProperty,
    IntProperty,
    PointerProperty,
    StringProperty,
)
from bpy.types import PropertyGroup


LEGACY_TEMPERATURE_EXPORT_SCALE = 2200.0
TEMPERATURE_EXPORT_SCALE = 5000.0
TEMPERATURE_MAPPING_VERSION = 2


def _preview_display_updated(_self, _context):
    from .preview import refresh_preview_display

    refresh_preview_display()


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


class FumarisSettings(PropertyGroup):
    """Custom PropertyGroup storing all simulation parameters for Fumaris smoke simulation."""

    smoke_object_type: EnumProperty(
        name="Type",
        description="Fumaris smoke role for this object",
        items=[
            ("domain", "Domain", "Bake an isolated simulation domain"),
            ("emitter", "Emitter", "Emit smoke into a domain that references this object"),
            ("collider", "Collider", "Collide with smoke in a domain that references this object"),
            ("effector", "Effector", "Apply velocity forces to smoke in a domain"),
            ("outflow", "Outflow", "Remove smoke, fire, and fuel inside this object"),
            ("none", "None", "Exclude this object from Fumaris simulations"),
        ],
        default="none",
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
            ("mesh", "Mesh", "Evaluated mesh surface or volume emitter"),
            ("box", "Box", "Box emitter using this object's transform"),
            ("sphere", "Sphere", "Sphere emitter at this object's origin"),
            ("particles", "Particles", "Blender particle system emitter"),
            ("geometry_nodes", "Geometry Nodes", "Use this object's evaluated Geometry Nodes output"),
            ("openvdb", "OpenVDB", "Load an OpenVDB file as a volume emitter"),
        ],
        default="mesh",
    )

    gn_subtype: EnumProperty(
        name="Geometry Type",
        description="Type of evaluated Geometry Nodes output to use",
        items=[
            ("point_cloud", "Point Cloud", "Use evaluated points as per-point sphere emitters"),
            ("mesh", "Mesh", "Use the evaluated mesh output as a mesh emitter"),
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
        description="Flow collider primitive used for this participant",
        items=[
            ("mesh", "Mesh", "Use the evaluated mesh as a true Flow collision object"),
            ("box", "Box", "Use this object's transform as a true Flow box collision object"),
            ("sphere", "Sphere", "Use a spherical velocity/pressure influence collider"),
        ],
        default="mesh",
    )

    # Emitter Settings
    emitter_radius: FloatProperty(
        name="Emitter Radius",
        description="Radius of the spherical emitter",
        default=1.0,
        min=0.01,
        subtype="DISTANCE",
    )

    emitter_temperature: FloatProperty(
        name="Emitter Temperature",
        description="Source temperature before the domain Temperature Input Scale is applied",
        default=2.0,
        min=0.0,
        soft_max=5.0,
    )

    emitter_smoke: FloatProperty(
        name="Smoke Density",
        description="Density of emitted smoke",
        default=5.0,
        min=0.0,
        soft_max=5.0,
    )

    emitter_fuel: FloatProperty(
        name="Fuel",
        description="Combustible fuel emitted into the simulation",
        default=0.0,
        min=0.0,
        soft_max=5.0,
    )

    emitter_burn: FloatProperty(
        name="Burn",
        description="Initial burn/flame amount emitted into the simulation",
        default=0.0,
        min=0.0,
        soft_max=5.0,
    )

    emitter_divergence: FloatProperty(
        name="Divergence",
        description="Expansion added by the emitter; positive values push outward and negative values pull inward",
        default=0.0,
        soft_min=-10.0,
        soft_max=10.0,
    )

    # Flow exposes an independent coupling rate for every emitted channel.
    couple_rate_velocity: FloatProperty(
        name="Velocity",
        description="How strongly explicit or participant motion drives grid velocity; stationary emitters do not damp the flow",
        default=200.0,
        min=0.0,
        soft_max=200.0,
    )

    couple_rate_divergence: FloatProperty(
        name="Divergence",
        description="How strongly emitter divergence couples into the grid",
        default=2.0,
        min=0.0,
        soft_max=200.0,
    )

    couple_rate_temperature: FloatProperty(
        name="Temperature",
        description="How strongly emitter temperature couples into the grid",
        default=2.0,
        min=0.0,
        soft_max=200.0,
    )

    couple_rate_fuel: FloatProperty(
        name="Fuel",
        description="How strongly emitter fuel couples into the grid",
        default=2.0,
        min=0.0,
        soft_max=200.0,
    )

    couple_rate_burn: FloatProperty(
        name="Burn",
        description="How strongly emitter burn couples into the grid",
        default=0.0,
        min=0.0,
        soft_max=200.0,
    )

    couple_rate_smoke: FloatProperty(
        name="Smoke",
        description="How strongly emitter smoke couples into the grid",
        default=2.0,
        min=0.0,
        soft_max=200.0,
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
        max=64,
    )

    point_enable_interpolation: BoolProperty(
        name="Interpolate Points",
        description="Interpolate point emitter data to reduce flicker between sparse samples",
        default=True,
    )

    # Simulation Settings
    velocity: FloatVectorProperty(
        name="Initial Velocity",
        description="Initial velocity added by the emitter itself",
        default=(0.0, 0.0, 0.0),
        size=3,
        subtype="VELOCITY",
    )

    normal_velocity: FloatProperty(
        name="Normal Velocity",
        description="Initial velocity emitted away from mesh face normals",
        default=0.0,
        soft_min=-100.0,
        soft_max=100.0,
    )

    motion_velocity_scale: FloatProperty(
        name="Motion Velocity Scale",
        description="Multiplier for velocity derived from emitter/object/point motion; set to 0 to ignore motion velocity",
        default=1.0,
        min=0.0,
        soft_max=25.0,
    )

    motion_substeps: IntProperty(
        name="Motion Sub-Steps",
        description="Intermediate source poses sampled between Blender frames; only affects moving or deforming geometry, is capped by domain Sub-Steps, and increases emitter work",
        default=8,
        min=1,
        max=40,
        soft_max=16,
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
        description="Simulation detail; maps to cell size as 32 divided by this value and is limited in practice by sparse capacity and GPU memory",
        default=256,
        min=16,
        soft_max=4096,
    )

    preview_dot_resolution: FloatProperty(
        name="Preview Dots",
        description="Multiplier for live preview point budget; base budget is derived from simulation resolution",
        default=1.0,
        min=0.05,
        soft_max=4.0,
    )

    preview_mode: EnumProperty(
        name="Preview Mode",
        description="Volume raymarch is the normal interactive preview; points remain available as a lightweight diagnostic fallback",
        items=[
            ("volume", "Volume", "Raymarch the live sparse Flow grid"),
            ("points", "Points", "Draw sampled active voxels as points"),
        ],
        default="volume",
    )

    preview_resolution_percent: FloatProperty(
        name="Preview Resolution",
        description="Percentage of the domain resolution used for live preview simulation; bake always uses full resolution",
        default=100.0,
        min=0.0,
        max=100.0,
        subtype="PERCENTAGE",
    )

    preview_max_points: IntProperty(
        name="Preview Max Dots",
        description="Maximum number of live preview dots drawn per frame after the resolution multiplier is applied",
        default=500000,
        min=512,
        max=16000000,
        soft_max=4000000,
    )

    preview_dot_size: FloatProperty(
        name="Preview Dot Size",
        description="World-space radius multiplier for live preview dots",
        default=5.0,
        min=0.05,
        max=40.0,
        soft_max=40.0,
        update=_preview_display_updated,
    )

    preview_color: FloatVectorProperty(
        name="Preview Color",
        description="Color used by live preview dots",
        default=(0.35, 0.65, 1.0),
        min=0.0,
        max=1.0,
        size=3,
        subtype="COLOR",
        update=_preview_display_updated,
    )

    preview_opacity: FloatProperty(
        name="Preview Opacity",
        description="Opacity multiplier for the live preview",
        default=0.65,
        min=0.0,
        max=1.0,
        subtype="FACTOR",
        update=_preview_display_updated,
    )

    preview_image_scale: FloatProperty(
        name="Viewport Scale",
        description="Fraction of the 3D viewport resolution used by the volume raymarch; doubling it renders and transfers four times as many pixels",
        default=50.0,
        min=25.0,
        max=100.0,
        subtype="PERCENTAGE",
    )

    preview_max_ray_steps: IntProperty(
        name="Ray Steps",
        description="Approximate samples across Fumaris's 32-meter resolution reference; values beyond one sample per 0.75 voxel stop adding work because the source grid has no finer detail",
        default=192,
        min=32,
        max=1024,
        soft_max=384,
    )

    preview_shadows: BoolProperty(
        name="Self Shadows",
        description="Calculate directional self-shadowing through the visible smoke density",
        default=True,
    )

    preview_bake: BoolProperty(
        name="Preview Bake",
        description="Show the selected live preview while baking; disabling this keeps bake as fast as possible",
        default=False,
    )

    sparse_block_capacity: IntProperty(
        name="Sparse Block Capacity",
        description="Small-block-equivalent Flow capacity used as the GPU memory ceiling; standard blocks use one-eighth as many locations because each holds eight times more voxels",
        default=16384,
        min=256,
        max=524288,
        soft_max=131072,
    )

    auto_cell_size: BoolProperty(
        name="Auto Cell Size",
        description="Allow Flow to coarsen cell size when the sparse block capacity is exceeded",
        default=False,
    )

    small_sparse_blocks: BoolProperty(
        name="Small Sparse Blocks",
        description="Use smaller Flow allocation blocks for tighter sparse coverage at the cost of more block-management overhead",
        default=False,
    )


    physics_convex_collision: BoolProperty(
        name="Convex Collision",
        description="Treat closed convex mesh colliders as filled solids using Flow clipping planes; unsuitable meshes fall back to the shell defined by Collider Margin",
        default=True,
    )

    sparse_block_min_lifetime: IntProperty(
        name="Block Minimum Lifetime",
        description="Minimum frames an allocated sparse block remains resident",
        default=4,
        min=0,
        max=120,
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

    allocation_speed_min_smoke: FloatProperty(
        name="Speed Minimum Smoke",
        description="Minimum smoke required before velocity alone retains a sparse block",
        default=0.0,
        min=0.0,
        soft_max=1.0,
    )

    allocate_neighbor_blocks: BoolProperty(
        name="Allocate Neighbor Blocks",
        description="Keep neighboring blocks available for advection; disabling saves memory but can clip fast-moving smoke",
        default=True,
    )

    boundary_safe_advection: BoolProperty(
        name="Boundary-Safe Advection",
        description="Allow fast smoke to advect across sparse block boundaries without Flow's local displacement clamp; costs some simulation performance",
        default=True,
    )

    simulation_speed: FloatProperty(
        name="Simulation Speed",
        description="Multiplier for simulation time step; keyframe this for speed ramps",
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
        description="Directory for output files",
        default="",
        subtype="DIR_PATH",
    )

    cache_slot: StringProperty(
        name="Cache Slot",
        description="Independent baked simulation slot; changing it preserves other baked slots",
        default="main",
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

    volume_selectable: BoolProperty(
        name="Selectable Volume",
        description="Allow imported Fumaris volume objects to be selectable in the viewport",
        default=False,
    )

    vdb_compression: EnumProperty(
        name="VDB Compression",
        description="Compression mode used for written OpenVDB files; Active Mask was fastest in local testing and imports in Blender",
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

    # Object References
    emitter_collection: PointerProperty(
        name="Emitters Collection",
        description="Domain-scoped collection of emitter participant objects",
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
        min=0.01,
        soft_max=20.0,
        subtype="DISTANCE",
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
        description="Collection of Fumaris Smoke effector objects for this simulation",
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
        description="Smoke amount attribute; empty uses smoke/density/mass",
        default="",
    )

    attr_temperature: StringProperty(
        name="Temperature",
        description="Temperature attribute; empty uses temperature/temp/heat",
        default="",
    )

    attr_fuel: StringProperty(
        name="Fuel",
        description="Fuel attribute; empty uses fuel/combustible",
        default="",
    )

    attr_burn: StringProperty(
        name="Burn",
        description="Burn/flame attribute; empty uses burn/flame/fire",
        default="",
    )

    attr_smoke_coupling: StringProperty(
        name="Smoke Coupling",
        description="Smoke coupling attribute; empty uses smoke_coupling/density_coupling",
        default="",
    )

    attr_temperature_coupling: StringProperty(
        name="Temperature Coupling",
        description="Temperature coupling attribute; empty uses temperature_coupling/heat_coupling",
        default="",
    )

    attr_velocity_coupling: StringProperty(
        name="Velocity Coupling",
        description="Velocity coupling attribute; empty uses velocity_coupling/force_strength/strength",
        default="",
    )

    attr_divergence: StringProperty(
        name="Divergence",
        description="Divergence attribute; empty uses divergence/expansion/pressure",
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

    mesh_emission_mode: EnumProperty(
        name="Mesh Emission",
        description="How a mesh emits smoke",
        items=[
            ("surface", "Surface", "Emit in a band around the mesh surface"),
            ("volume", "Volume", "Emit inside the mesh volume up to the distance"),
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
        description="Optional vertex group or evaluated mesh attribute whose 0 to 1 weights scale emission strength",
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
        name="Radius",
        description="Radial area influenced by this effector",
        default=4.0,
        min=0.01,
        soft_max=20.0,
        subtype="DISTANCE",
    )

    effector_vortex_height: FloatProperty(
        name="Height",
        description="Total height of the cylindrical vortex field along local Z",
        default=20.0,
        min=0.01,
        soft_max=100.0,
        subtype="DISTANCE",
    )

    effector_vortex_core_radius: FloatProperty(
        name="Core Radius",
        description="Radius of peak rotation; velocity rises inside the core and falls outside it",
        default=1.0,
        min=0.01,
        soft_max=10.0,
        subtype="DISTANCE",
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
        min=0.001,
        soft_max=20.0,
        subtype="DISTANCE",
    )

    effector_noise_w: FloatProperty(
        name="W",
        description="Fourth-dimensional noise coordinate; animate this value to art-direct the noise evolution",
        default=0.0,
        min=-10000.0,
        max=10000.0,
        soft_min=-10.0,
        soft_max=10.0,
    )

    effector_noise_seed: IntProperty(
        name="Seed",
        description="Seed used for effector noise",
        default=102,
        min=0,
        max=1000000,
    )

    effector_coupling: FloatProperty(
        name="Coupling",
        description="How strongly the force velocity couples into the Flow grid",
        default=200.0,
        min=0.0,
        soft_max=500.0,
    )

    effector_samples: IntProperty(
        name="Samples",
        description="Minimum texture samples per axis; small noise and vortex cores raise this automatically up to 64",
        default=8,
        min=8,
        max=64,
    )

    # Advanced Settings
    gravity: FloatVectorProperty(
        name="Gravity",
        description="Gravity vector for simulation",
        default=(0.0, 0.0, -9.81),
        size=3,
        subtype="ACCELERATION",
    )

    buoyancy_per_temp: FloatProperty(
        name="Temperature Buoyancy",
        description="Upward force applied per unit of smoke temperature",
        default=2.0,
        min=0.0,
        soft_max=20.0,
    )

    buoyancy_per_smoke: FloatProperty(
        name="Smoke Buoyancy",
        description="Force per unit smoke density; negative follows gravity, positive rises against gravity",
        default=0.0,
        soft_min=-20.0,
        soft_max=20.0,
    )

    ignition_temperature: FloatProperty(
        name="Ignition Temperature",
        description="Minimum normalized Flow temperature before fuel begins to burn; Flow temperature is clamped to 1",
        default=0.05,
        min=0.0,
        max=1.0,
    )

    temperature_input_scale: FloatProperty(
        name="Temperature Input Scale",
        description="Converts emitter and point temperatures into Flow's normalized 0 to 1 simulation range; use 0.000125 for attributes reaching 8000",
        default=1.0,
        min=0.0,
        soft_max=1.0,
    )

    burn_per_temp: FloatProperty(
        name="Burn Per Temperature",
        description="Burn amount generated per unit temperature above ignition",
        default=4.0,
        min=0.0,
        soft_max=20.0,
    )

    fuel_per_burn: FloatProperty(
        name="Fuel Per Burn",
        description="Fuel consumed per unit burn",
        default=0.25,
        min=0.0,
        soft_max=5.0,
    )

    temp_per_burn: FloatProperty(
        name="Temperature Per Burn",
        description="Heat released per unit burn",
        default=5.0,
        min=0.0,
        soft_max=25.0,
    )

    smoke_per_burn: FloatProperty(
        name="Smoke Per Burn",
        description="Smoke density generated per unit burn",
        default=3.0,
        min=0.0,
        soft_max=25.0,
    )

    divergence_per_burn: FloatProperty(
        name="Expansion Per Burn",
        description="Expansion generated per unit burn, normalized across simulation sub-steps",
        default=1.0,
        soft_min=-20.0,
        soft_max=20.0,
    )

    cooling_rate: FloatProperty(
        name="Cooling Rate",
        description="Exponential cooling rate applied to temperature",
        default=1.5,
        min=0.0,
        soft_max=10.0,
    )

    vorticity: FloatProperty(
        name="Vorticity",
        description="Vorticity confinement strength",
        default=0.5,
        min=0.0,
        max=10.0,
    )

    dissipation: FloatProperty(
        name="Dissipation",
        description="Smoke dissipation rate",
        default=0.05,
        min=0.0,
        max=1.0,
    )

    # VDB export controls
    export_velocity_vdb: BoolProperty(
        name="Export Velocity VDB",
        description="Export velocity for motion blur and downstream volume effects",
        default=False,
    )

    export_temperature_vdb: BoolProperty(
        name="Export Temperature VDB",
        description="Export nonnegative Flow temperature in a fixed Kelvin-like 0 to 5000 range for direct Blackbody shading",
        default=False,
    )

    export_fuel_vdb: BoolProperty(
        name="Export Fuel VDB",
        description="Export fuel field alongside density",
        default=False,
    )

    export_burn_vdb: BoolProperty(
        name="Export Burn VDB",
        description="Export Flow's raw timestep-dependent combustion amount for custom shading",
        default=False,
    )

    flame_temperature_min: FloatProperty(
        name="Start Temperature",
        description="Kelvin-like temperature where generated fire emission begins",
        default=800.0,
        min=0.0,
        soft_max=5000.0,
        update=_appearance_updated,
    )

    flame_temperature_max: FloatProperty(
        name="Full Temperature",
        description="Kelvin-like temperature where generated fire emission reaches full strength",
        default=3000.0,
        min=1.0,
        soft_max=5000.0,
        update=_appearance_updated,
    )

    shader_smoke_density: FloatProperty(
        name="Density",
        description="Density multiplier used by the generated volume material and live volume preview",
        default=2.0,
        min=0.0,
        max=100.0,
        soft_max=10.0,
        update=_appearance_updated,
    )

    shader_smoke_color: FloatVectorProperty(
        name="Color",
        description="Smoke color used by the generated volume material and live volume preview",
        default=(0.6, 0.6, 0.6),
        min=0.0,
        max=1.0,
        size=3,
        subtype="COLOR",
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
        default=1.0,
        min=0.0,
        max=100.0,
        soft_max=10.0,
        update=_appearance_updated,
    )

    shader_temperature_multiplier: FloatProperty(
        name="Blackbody Scale",
        description="Multiplier applied to Kelvin-like temperature before Blackbody color; 1.0 preserves the baked temperature",
        default=1.0,
        min=0.01,
        max=10.0,
        soft_max=2.0,
        update=_appearance_updated,
    )

    temperature_mapping_version: IntProperty(
        default=0,
        options={"HIDDEN"},
    )

    num_sub_steps: IntProperty(
        name="Sub-Steps",
        description="Complete simulation steps per frame; increase for fast motion or stability, up to 40",
        default=2,
        min=1,
        max=40,
    )

    # Hidden compatibility multiplier for existing point-emitter .blend files.
    velocity_scale: FloatProperty(
        name="Velocity Scale",
        description="Multiplier for point/particle velocity influence",
        default=1.0,
        min=0.0,
        soft_max=10.0,
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

    show_preview_display: BoolProperty(
        name="Preview Settings",
        default=False,
    )

    show_appearance: BoolProperty(
        name="Appearance",
        default=True,
    )

    show_cache_location: BoolProperty(
        name="Output",
        default=False,
    )

    show_domain_simulation: BoolProperty(
        name="Simulation",
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

    show_emitter_coupling: BoolProperty(
        name="Channel Coupling",
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

    show_domain_output: BoolProperty(
        name="VDB Channels",
        default=False,
    )

def _migrate_temperature_mapping():
    for obj in bpy.data.objects:
        props = getattr(obj, "fumaris", None)
        if props is None or props.temperature_mapping_version >= TEMPERATURE_MAPPING_VERSION:
            continue
        if props.temperature_mapping_version == 0:
            if 0.0 <= props.flame_temperature_max <= 1.0:
                props.flame_temperature_min *= TEMPERATURE_EXPORT_SCALE
                props.flame_temperature_max *= TEMPERATURE_EXPORT_SCALE
        elif props.flame_temperature_max <= LEGACY_TEMPERATURE_EXPORT_SCALE:
            # Correct values produced by the short-lived development migration.
            correction = (
                TEMPERATURE_EXPORT_SCALE / LEGACY_TEMPERATURE_EXPORT_SCALE
            )
            props.flame_temperature_min *= correction
            props.flame_temperature_max *= correction
        props.temperature_mapping_version = TEMPERATURE_MAPPING_VERSION


@persistent
def _migrate_temperature_mapping_after_load(_unused):
    _migrate_temperature_mapping()


def _migrate_temperature_mapping_deferred():
    """Run after add-on registration, when Blender exposes normal datablocks."""
    _migrate_temperature_mapping()
    return None


def _remove_handler_named(handlers, name):
    for handler in list(handlers):
        if handler.__name__ == name:
            handlers.remove(handler)


def register():
    try:
        bpy.utils.unregister_class(FumarisSettings)
    except RuntimeError:
        pass
    bpy.utils.register_class(FumarisSettings)
    bpy.types.Object.fumaris = PointerProperty(type=FumarisSettings)
    _remove_handler_named(
        bpy.app.handlers.load_post,
        "_migrate_temperature_mapping_after_load",
    )
    bpy.app.handlers.load_post.append(_migrate_temperature_mapping_after_load)
    bpy.app.timers.register(_migrate_temperature_mapping_deferred, first_interval=0.0)


def unregister():
    _remove_handler_named(
        bpy.app.handlers.load_post,
        "_migrate_temperature_mapping_after_load",
    )
    if hasattr(bpy.types.Object, 'fumaris'):
        del bpy.types.Object.fumaris
    try:
        bpy.utils.unregister_class(FumarisSettings)
    except RuntimeError:
        pass
